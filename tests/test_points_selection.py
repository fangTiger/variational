"""积分择优：沿用真实接口字段，禁止网络并由全局夹具隔离数据。"""
import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from tools import run_swap_carry_guard as guard
from tools import hedge_swap_carry as execution
from tests.test_run_swap_carry_guard import NOW, _metadata, _switch_client


def market(monkeypatch, budget=Decimal('2000'), metadata=None):
    client = _switch_client(positions={}, metadata=metadata, swap_rate=Decimal('-.057'),
                           perp_rate={'XAU': Decimal('.0259'), 'XAUT': Decimal('.1095')})
    original = client.request_quote

    async def quote(underlying, side, qty, **kwargs):
        payload = await original(underlying, side, qty, **kwargs)
        payload.update(bid='4000', ask='4000')
        for direction in ('bid', 'ask'):
            payload['margin_requirements'][direction + '_margin_delta'] = {
                'initial_margin': str(qty * (Decimal('114') if underlying == 'XAUT' else Decimal('200')) + (Decimal('1') if underlying == 'XAUT' and qty == 2 else Decimal('0'))),
                'maintenance_margin': str(qty * Decimal('200')),
            }
        return payload

    async def portfolio(path):
        assert path == '/portfolio'
        return {'balance': str(budget / Decimal('.6')), 'upnl': '0'}

    monkeypatch.setattr(client, 'request_quote', quote)
    monkeypatch.setattr(client, 'raw', portfolio)
    return client


def evaluate(client):
    record = client.metadata['XAUS'][0]
    schedule = guard.parse_trading_schedule(record['trading_sessions'], record['trading_schedule'], record['market_status'], NOW)
    return asyncio.run(guard._evaluate_carry_candidates(client, schedule, record['market_status'], notional=Decimal('4000')))


def test_margin_filter_includes_isolated_target_bucket(monkeypatch):
    candidates, best = evaluate(market(monkeypatch, Decimal('618')))
    assert best.name == 'XAUS_XAUT'
    assert Decimal(candidates['XAUS_XAUT']['required_margin_usd']) == Decimal('618')
    assert not candidates['TRIPLE']['available']
    assert '保证金' in candidates['TRIPLE']['reason']
    assert not candidates['XAUS_XAU']['available']
    assert 'carry' in candidates['XAUS_XAU']['reason']


def test_reported_rates_rank_by_points(monkeypatch):
    candidates, best = evaluate(market(monkeypatch))
    assert best.name == 'TRIPLE'
    assert {name: Decimal(item['points_oi']) for name, item in candidates.items()} == {
        'XAUS_XAU': 12000, 'XAU_XAUT': 8000, 'XAUS_XAUT': 12000, 'TRIPLE': 20000}
    assert candidates['TRIPLE']['selected']
    assert candidates['TRIPLE']['ranking_basis'] == '积分胜出'
    assert Decimal(candidates['TRIPLE']['required_margin_usd']) == 933
    assert Decimal(candidates['TRIPLE']['carry_annual']) * 8000 == Decimal('544.4')


def test_closed_xaus_leaves_only_perpetuals(monkeypatch):
    candidates, best = evaluate(market(monkeypatch, metadata=_metadata(market_status='closed')))
    assert best.name == 'XAU_XAUT'
    assert [name for name, item in candidates.items() if item['available']] == ['XAU_XAUT']


def test_weights_change_tied_points_to_carry_order(monkeypatch):
    client = market(monkeypatch, Decimal('618'))
    assert evaluate(client)[1].name == 'XAUS_XAUT'
    monkeypatch.setitem(guard.POINTS_WEIGHTS, 'XAUS', Decimal('1'))
    candidates, best = evaluate(client)
    assert best.name == 'XAU_XAUT'
    assert candidates[best.name]['ranking_basis'] == 'carry 胜出'


@pytest.mark.parametrize('weight,hours,carry,forced,expected', [
    ('2', 5, '.03', False, True),
    ('1.2', 5, '.03', False, False),
    ('2', 1, '.03', False, False),
    ('2', 4, '.03', False, True),
    ('1.2', 5, '.08', False, True),
    ('1.2', 1, '-.1', True, True),
])
def test_points_or_carry_hysteresis(monkeypatch, weight, hours, carry, forced, expected):
    monkeypatch.setitem(guard.POINTS_WEIGHTS, 'XAUS', Decimal(weight))
    candidates, _ = evaluate(market(monkeypatch))
    candidates['XAUS_XAUT']['carry_annual'] = carry
    candidates['XAU_XAUT']['carry_annual'] = '.04'
    result = guard._carry_switch_decision(candidates, execution.XAUS_XAUT, execution.XAU_XAUT,
        now=NOW, last_switch_at=NOW-timedelta(hours=hours), forced=forced)
    assert result['allowed'] is expected


def test_missing_margin_is_filtered_and_zero_funding_is_readable(monkeypatch):
    client = market(monkeypatch)
    client.swap_rate = Decimal('0')
    original = client.request_quote

    async def broken(underlying, side, qty, **kwargs):
        payload = await original(underlying, side, qty, **kwargs)
        if underlying == 'XAUS':
            del payload['margin_requirements']['ask_margin_delta']['maintenance_margin']
        return payload

    monkeypatch.setattr(client, 'request_quote', broken)
    candidates, best = evaluate(client)
    assert best.name == 'XAU_XAUT'
    assert candidates['XAUS_XAUT']['carry_annual'] is not None
    assert '保证金' in candidates['XAUS_XAUT']['reason']


def test_panel_compares_current_and_best_points(tmp_path):
    import json
    from panel.providers.swap_carry import _selection_metrics
    path = tmp_path / 'heartbeat.json'
    path.write_text(json.dumps({'best_structure': 'XAUS_XAUT', 'candidate_structures': {
        'XAU_XAUT': {'points_oi': '8000'}, 'XAUS_XAUT': {'points_oi': '12000'}},
        'selection_decision': {'reason': '积分胜出'}}))
    metrics = _selection_metrics(path, 'XAU_XAUT')
    assert any(metric.label == '积分 OI（同规模）' and '8,000' in str(metric.value) and '12,000' in str(metric.value) for metric in metrics)


def test_forced_close_ignores_carry_margin_and_hold(monkeypatch, tmp_path):
    import json
    from tests.test_run_swap_carry_guard import _run, _paths
    client = _switch_client(positions={'XAUS': Decimal('.01'), 'XAUT': Decimal('-.01')},
        metadata=_metadata(closure_duration=timedelta(hours=49), time_until_close=timedelta(minutes=45)),
        perp_rate={'XAU': Decimal('.1'), 'XAUT': Decimal('.09')}, accept_script=[{}, {}, {}, {}])
    monkeypatch.setattr(guard, 'MAX_MARGIN_UTILIZATION', Decimal('.001'))
    monkeypatch.setattr(guard, 'notify', lambda *args: True)
    _paths(tmp_path)['state_path'].write_text(json.dumps({'last_switch_at': (NOW-timedelta(hours=1)).isoformat()}))
    assert _run(client, tmp_path, auto_open_notional=Decimal('50')) == 0
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['selection_decision']['forced_long_closure']
    assert heartbeat['auto_switch_attempted']
    assert client.sizes['XAUS'] == 0
    assert client.sizes['XAU'] > 0 and client.sizes['XAUT'] < 0


def test_points_gain_switches_existing_perpetual_pair(monkeypatch, tmp_path):
    import json
    from tests.test_run_swap_carry_guard import _run, _paths
    client = _switch_client(positions={'XAU': Decimal('.01'), 'XAUT': Decimal('-.01')},
        swap_rate=Decimal('-.057'), perp_rate={'XAU': Decimal('.0259'), 'XAUT': Decimal('.1095')},
        accept_script=[{}, {}, {}, {}])
    # 两腿可用预算足够、三腿不足；保持真实报价 schema。
    monkeypatch.setattr(guard, 'MAX_MARGIN_UTILIZATION', Decimal('.5'))
    monkeypatch.setattr(guard, 'notify', lambda *args: True)
    _paths(tmp_path)['state_path'].write_text(json.dumps({'last_switch_at': (NOW-timedelta(hours=5)).isoformat()}))
    assert _run(client, tmp_path) == 0
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['target_structure'] == 'XAUS_XAUT'
    assert heartbeat['selection_decision']['points_gain'] == '0.5'
    assert heartbeat['auto_switch_attempted']
    assert client.sizes['XAUS'] > 0 and client.sizes['XAU'] == 0


def test_margin_quotes_respect_real_quantity_limits(monkeypatch):
    client = market(monkeypatch)
    original = client.request_quote

    async def limited(underlying, side, qty, **kwargs):
        step = Decimal('.00001') if underlying == 'XAUS' else Decimal('.001')
        assert qty % step == 0, '询价数量必须符合真实步长'
        payload = await original(underlying, side, qty, **kwargs)
        payload['mark_price'] = '3999'
        payload['qty_limits'] = {direction: {'min_qty': str(step), 'min_qty_tick': str(step)} for direction in ('bid', 'ask')}
        return payload

    monkeypatch.setattr(client, 'request_quote', limited)
    candidates, best = evaluate(client)
    assert best is not None
    assert all('保证金读取失败' not in item['reason'] for item in candidates.values())
