"""结构择优回归：真实接口 schema，所有状态均隔离到临时目录。"""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from tools import run_swap_carry_guard as guard
from tools import hedge_swap_carry as execution
from tests.test_run_swap_carry_guard import NOW, _metadata, _switch_client, _run, _paths


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """禁止真实通知并隔离本机会话。"""
    monkeypatch.setattr(guard, "notify", lambda *args: True)
    monkeypatch.delenv("VARIATIONAL_COOKIE", raising=False)
    monkeypatch.delenv("VARIATIONAL_WALLET_ADDRESS", raising=False)


def evaluate(client):
    """用生产解析器读取真实会话字段。"""
    record = client.metadata['XAUS'][0]
    schedule = guard.parse_trading_schedule(record['trading_sessions'], record['trading_schedule'], record['market_status'], NOW)
    return asyncio.run(guard._evaluate_carry_candidates(client, schedule, record['market_status']))


def test_open_market_selects_profitable_alternative(tmp_path):
    client = _switch_client(positions={}, swap_rate=Decimal('-.057'),
                            perp_rate={'XAU': Decimal('.032'), 'XAUT': Decimal('.0729')})
    _run(client, tmp_path, dry_run=True)
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['target_structure'] == 'XAU_XAUT'
    assert heartbeat['candidate_structures']['XAUS_XAU']['carry_annual'] == '-0.025'
    assert Decimal(heartbeat['candidate_structures']['XAU_XAUT']['carry_annual']) == Decimal('.0409')
    assert heartbeat['auto_open_attempted'] is True


@pytest.mark.parametrize('metadata', [
    _metadata(market_status='closed'),
    _metadata(time_until_close=timedelta(hours=2)),
])
def test_untradable_or_near_close_excluded(metadata):
    candidates, best = evaluate(_switch_client(positions={}, metadata=metadata, perp_rate={'XAU': Decimal('.03'), 'XAUT': Decimal('.09')}))
    assert best.name == 'XAU_XAUT'
    for name in ('XAUS_XAU', 'TRIPLE'):
        assert candidates[name]['available'] is False
        assert candidates[name]['reason']


def test_failed_leg_excludes_only_affected_structures():
    candidates, best = evaluate(_switch_client(positions={}, swap_rate=RuntimeError('读取失败'), perp_rate={'XAU': Decimal('.03'), 'XAUT': Decimal('.09')}))
    assert best.name == 'XAU_XAUT'
    assert '读取失败' in candidates['TRIPLE']['reason']
    assert candidates['XAU_XAUT']['available'] is True


def test_zero_rate_is_valid():
    candidates, best = evaluate(_switch_client(positions={}, swap_rate=Decimal('0')))
    assert all(item['carry_annual'] is not None for item in candidates.values())
    assert candidates['XAUS_XAU']['carry_annual'] == '0.10'


@pytest.mark.parametrize('advantage,hours,expected', [('0.01', 5, False), ('0.03', 5, True), ('0.03', 1, False)])
def test_switch_hysteresis(advantage, hours, expected, tmp_path, monkeypatch):
    # 关闭积分优势以独立验证原有 carry 滞回，预算允许两腿但不允许三腿。
    monkeypatch.setitem(guard.POINTS_WEIGHTS, 'XAUS', Decimal('1'))
    monkeypatch.setattr(guard, 'MAX_MARGIN_UTILIZATION', Decimal('.75'))
    # 当前 carry 1%，目标 carry 为 1% 加优势，三腿 carry 始终低于最优。
    client = _switch_client(positions={'XAU': Decimal('.01'), 'XAUT': Decimal('-.01')},
                            perp_rate={'XAU': Decimal('.05') + Decimal(advantage),
                                       'XAUT': Decimal('.05') + Decimal(advantage)},
                            accept_script=[{}, {}, {}, {}] if expected else None)
    client.equity = Decimal('1000')
    _paths(tmp_path)['state_path'].write_text(json.dumps({'last_switch_at': (NOW-timedelta(hours=hours)).isoformat()}))
    assert _run(client, tmp_path) == 0
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['auto_switch_attempted'] is expected
    assert heartbeat['selection_decision']['hysteresis_blocked'] is (not expected)
    if expected:
        assert json.loads(_paths(tmp_path)['state_path'].read_text())['last_switch_at'] == NOW.isoformat()


def test_long_close_forces_switch_despite_hysteresis(tmp_path):
    client = _switch_client(positions={'XAUS': Decimal('.01'), 'XAU': Decimal('-.01')},
                            metadata=_metadata(closure_duration=timedelta(hours=49), time_until_close=timedelta(minutes=45)),
                            accept_script=[{}, {}, {}, {}])
    _paths(tmp_path)['state_path'].write_text(json.dumps({'last_switch_at': (NOW-timedelta(hours=1)).isoformat()}))
    assert _run(client, tmp_path) == 0
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['auto_switch_attempted'] is True
    assert heartbeat['selection_decision']['forced_long_closure'] is True


def test_best_below_entry_threshold_stays_flat(tmp_path):
    client = _switch_client(positions={}, swap_rate=Decimal('-.04'), perp_rate={'XAU': Decimal('.05'), 'XAUT': Decimal('.051')})
    assert _run(client, tmp_path) == 0
    assert client.accept_calls == []
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['best_structure'] is None
    assert heartbeat['selection_decision']['entry_threshold_blocked'] is True


@pytest.mark.parametrize('kill', [True, False])
def test_safety_exit_precedes_candidate_evaluation(tmp_path, monkeypatch, kill):
    async def forbidden(*args):
        pytest.fail('风控命中后不得选择结构')
    monkeypatch.setattr(guard, '_evaluate_carry_candidates', forbidden)
    client = _switch_client(positions={'XAUS': Decimal('.01'), 'XAU': Decimal('-.01') if kill else Decimal('0')}, accept_script=[{}, {}])
    if kill:
        _paths(tmp_path)['kill_switch_path'].write_text('停止')
    assert _run(client, tmp_path) == 0
    assert client.accept_calls
    assert all(call[2] for call in client.accept_calls)


def test_valid_triple_is_recognized():
    positions = {name: guard.Position(name, Decimal(size)) for name, size in [('XAUS', '.01'), ('XAU', '.01'), ('XAUT', '-.02')]}
    detection = guard._detect_carry_structure(positions)
    assert detection.structure == execution.TRIPLE
    assert detection.error is None


def test_panel_explains_current_best_difference(tmp_path):
    from panel.providers.swap_carry import _selection_metrics
    path = tmp_path / 'heartbeat.json'
    path.write_text(json.dumps({'best_structure': 'XAU_XAUT', 'selection_decision': {'reason': '切换滞回阻挡'}, 'auto_switch_conclusion': '切换滞回阻挡'}))
    metrics = _selection_metrics(path, 'XAUS_XAU')
    assert any(metric.label == '最优结构' and metric.value == 'XAU_XAUT' for metric in metrics)
    assert any('滞回' in str(metric.value) for metric in metrics)


def test_three_reported_carries_choose_best(monkeypatch):
    # 固定给定的三个计算结果，验证生产候选排序，而非重写排序算法。
    carries = {'XAUS_XAU': Decimal('-.025'), 'XAU_XAUT': Decimal('.0409'), 'TRIPLE': Decimal('.016'), 'XAUS_XAUT': Decimal('.01')}
    monkeypatch.setattr(execution, '_weighted_net_carry', lambda structure, rates: carries[structure.name])
    candidates, best = evaluate(_switch_client(positions={}))
    assert best == execution.XAU_XAUT
    assert {name: Decimal(item['carry_annual']) for name, item in candidates.items()} == carries


def test_best_triple_opens_and_survives_next_round(tmp_path):
    client = _switch_client(positions={}, swap_rate=Decimal('-.04'),
                            perp_rate={'XAU': Decimal('.09'), 'XAUT': Decimal('.13')},
                            accept_script=[{}, {}, {}])
    client.equity = Decimal('5000')
    assert _run(client, tmp_path, auto_open_notional=Decimal('2000')) == 0
    assert client.sizes['XAUS'] == client.sizes['XAU'] > 0
    assert client.sizes['XAUT'] == -2 * client.sizes['XAUS']
    assert _run(client, tmp_path, auto_open_notional=Decimal('2000')) == 0
    assert len(client.accept_calls) == 3
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['observed_structure'] == heartbeat['best_structure'] == 'TRIPLE'


def test_long_close_unreadable_target_exits_before_close(tmp_path):
    client = _switch_client(positions={'XAUS': Decimal('.01'), 'XAU': Decimal('-.01')},
                            metadata=_metadata(closure_duration=timedelta(hours=49), time_until_close=timedelta(minutes=45)),
                            perp_rate={'XAU': Decimal('.10'), 'XAUT': RuntimeError('读取失败')},
                            accept_script=[{}, {}])
    assert _run(client, tmp_path) == 0
    assert len(client.accept_calls) == 2
    assert all(call[2] for call in client.accept_calls)
    assert all(size == 0 for size in client.sizes.values())


def test_below_threshold_keeps_existing_position(tmp_path):
    client = _switch_client(positions={'XAU': Decimal('.01'), 'XAUT': Decimal('-.01')},
                            swap_rate=Decimal('-.04'),
                            perp_rate={'XAU': Decimal('.05'), 'XAUT': Decimal('.051')})
    assert _run(client, tmp_path) == 0
    assert client.accept_calls == []
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['selection_decision']['entry_threshold_blocked']


def test_unknown_current_carry_does_not_switch_for_profit(tmp_path):
    client = _switch_client(positions={'XAUS': Decimal('.01'), 'XAU': Decimal('-.01')},
                            swap_rate=RuntimeError('读取失败'),
                            perp_rate={'XAU': Decimal('.03'), 'XAUT': Decimal('.09')})
    assert _run(client, tmp_path) == 0
    assert client.accept_calls == []
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['best_structure'] == 'XAU_XAUT'
    assert heartbeat['selection_decision']['advantage_annual'] is None
    assert heartbeat['selection_decision']['hysteresis_blocked']


def test_triple_exceeding_existing_leg_cap_does_not_close_old_structure(tmp_path):
    client = _switch_client(positions={'XAU': Decimal('.01'), 'XAUT': Decimal('-.01')},
                            swap_rate=Decimal('-.04'),
                            perp_rate={'XAU': Decimal('.09'), 'XAUT': Decimal('.13')})
    client.equity = Decimal('5000')
    assert _run(client, tmp_path) == 0
    assert client.accept_calls == []
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['best_structure'] == 'TRIPLE'
    assert '硬上限' in heartbeat['auto_switch_conclusion']
