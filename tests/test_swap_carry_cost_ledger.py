"""成本台账离线回归；流水夹具复用实抓 schema，所有文件位于 tmp_path。"""
import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from tests.test_verify_funding_units import BTC_TRANSFER
from engine import swap_carry_cost as cost

TS = '2026-09-04T08:00:00+00:00'
END = '2026-09-05T08:00:00+00:00'


def funding(market='XAUS'):
    return {**BTC_TRANSFER, 'reference_instrument': {
        **BTC_TRANSFER['reference_instrument'], 'underlying': market}}


def fill(rfq='rfq-1', side='buy'):
    # 内部切换台账的真实字段；不是虚构交易所返回。
    return dict(status='succeeded', market='XAUS', side=side, rfq_id=rfq,
                filled_quantity='2', execution_price='101' if side == 'buy' else '99',
                quote_mid='100', reduce_only=side == 'sell')


def test_transfer_actual_cash_not_rate_and_idempotency(tmp_path):
    path = tmp_path/'ledger.jsonl'
    row = funding()
    row['funding_rate'] = '9999999'
    client = AsyncMock()
    client.raw.return_value = {'result': [row, row, funding('BTC')],
                               'pagination': {'object_count': 3}}
    for _ in range(2):
        asyncio.run(cost.scan_transfers(client, path))
    rows, error = cost.load(path)
    assert error is None
    assert len([r for r in rows if r['market'] == 'XAUS']) == 1
    assert cost.summarize(rows)['funding'] == Decimal(BTC_TRANSFER['qty'])
    assert all('/transfers?' in call.args[0] for call in client.raw.await_args_list)


@pytest.mark.parametrize('kind', ['fee', 'funding', 'allocation', 'liquidation_penalty'])
def test_event_types(tmp_path, kind):
    path = tmp_path/'ledger.jsonl'
    row = cost.event('unique', TS, kind, 'XAUS', '-1', '测试证据', detail={'说明': '离线'})
    assert cost.append(path, [row, row]) == 1
    assert cost.append(path, [row]) == 0
    assert cost.summarize(cost.load(path)[0])[kind] == -1


def test_switch_children_are_counted_once(tmp_path):
    path = tmp_path/'ledger.jsonl'
    switch = dict(started_at=TS, measured_wear_usd='-999',
                  close_phase={'legs': [fill('close', 'sell')]},
                  open_phase={'legs': [fill('open')], 'rollback_legs': []})
    cost.record_switch(path, switch)
    cost.record_switch(path, switch)
    rows, _ = cost.load(path)
    summary = cost.summarize(rows)
    assert summary['spread'] == -4
    assert summary['switch'] == -4
    assert summary['total'] == -4
    assert len(rows) == 5
    assert all(r.get('parent_id') for r in rows if r['event_type'] == 'spread')
    assert next(r for r in rows if r['event_type'] == 'switch')['detail']['account_equity_delta'] == '-999'


def test_reconciliation_closes_and_reports_missing_cost():
    rows = [cost.event('fund', TS, 'funding', 'XAUS', '-2', '/transfers'),
            cost.event('spread', TS, 'spread', 'XAU', '-3', '报价对比'),
            cost.event('btc', TS, 'funding', 'BTC', '-100', '/transfers')]
    inputs = dict(start_ts='2026-09-03T08:00:00Z', end_ts=END,
                  start_equity='100', end_equity='106', realized_price_pnl='4',
                  unrealized_change='2', external_cashflow='10', other_strategies='-5',
                  scope='account', source='离线对账证据')
    report = cost.reconcile(rows, inputs)
    assert report['residual'] == 0
    assert not report['warning']
    inputs['end_equity'] = '104'
    report = cost.reconcile(rows, inputs, threshold='1')
    assert report['residual'] == -2
    assert report['warning']
    assert report['funding'] == -2


@pytest.mark.parametrize('content', [None, '{bad', '{}\n', '[]\n'])
def test_missing_corrupt_cli_panel_degrade(tmp_path, capsys, content):
    from tools.show_cost_ledger import main
    from panel.providers.swap_carry import _cost_metrics
    path = tmp_path/'ledger.jsonl'
    if content is not None:
        path.write_text(content)
    assert main(['--path', str(path), '--summary']) == 0
    assert '不可用' in capsys.readouterr().out
    metrics, alerts = _cost_metrics(path, tmp_path/'recon.json')
    assert any('不可用' in m.value for m in metrics)


def test_since_and_read_only(tmp_path, capsys):
    from tools.show_cost_ledger import main
    path = tmp_path/'ledger.jsonl'
    cost.append(path, [cost.event('a', TS, 'funding', 'XAUS', '-2', '/transfers'),
                       cost.event('b', END, 'funding', 'XAU', '1', '/transfers')])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    main(['--path', str(path), '--since', END, '--summary'])
    output = capsys.readouterr().out
    assert '+1.00' in output
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_corrupt_ledger_refuses_append(tmp_path):
    path = tmp_path/'ledger.jsonl'
    path.write_text('{bad')
    with pytest.raises(ValueError):
        cost.append(path, [cost.event('a', TS, 'fee', 'XAUS', '0', '报价')])
    assert path.read_text() == '{bad'


def test_successful_accept_records_open_close_and_failed_accept_does_not(tmp_path):
    from types import SimpleNamespace
    from tools import hedge_swap_carry as execution
    from adapters.base import Side
    path = tmp_path/'ledger.jsonl'
    client = AsyncMock()
    client._max_slippage = .01
    quote = SimpleNamespace(payload={'quote_id': 'q', 'bid': '99', 'ask': '101'},
                            side=Side.BUY, qty=Decimal('2'), leg=execution.XAUS_LEG)
    token = cost.ACTIVE_PATH.set(path)
    try:
        client.accept_quote.side_effect = RuntimeError('拒绝成交')
        with pytest.raises(RuntimeError):
            asyncio.run(execution._accept_quote(client, quote, reduce_only=False))
        assert not path.exists()
        client.accept_quote.side_effect = None
        client.accept_quote.return_value = {'rfq_id': 'open'}
        asyncio.run(execution._accept_quote(client, quote, reduce_only=False))
        quote.side = Side.SELL
        client.accept_quote.return_value = {'rfq_id': 'close'}
        asyncio.run(execution._accept_quote(client, quote, reduce_only=True))
    finally:
        cost.ACTIVE_PATH.reset(token)
    rows, error = cost.load(path)
    assert error is None
    assert len(rows) == 4
    assert cost.summarize(rows)['spread'] == -4


def test_allocation_only_confirmed_and_zero_is_explained(tmp_path):
    path = tmp_path/'ledger.jsonl'
    result = dict(conversion_id='conversion-1', conversion_status='pending', underlying='XAUS')
    cost.record_allocation(path, TS, 'XAUS', result)
    assert not path.exists()
    result['conversion_status'] = 'confirmed'
    cost.record_allocation(path, TS, 'XAUS', result)
    cost.record_allocation(path, TS, 'XAUS', result)
    rows, error = cost.load(path)
    assert error is None and len(rows) == 1
    assert rows[0]['amount_usd'] == '0'
    assert '内部' in rows[0]['detail']['说明']


def test_actual_snapshot_separates_btc_and_does_not_double_charge_spread(tmp_path):
    path = tmp_path/'ledger.jsonl'
    report_path = tmp_path/'recon.json'
    client = AsyncMock()
    # /portfolio 与 /positions 仅使用项目已经实抓确认的字段。
    client.raw.return_value = {'balance': '100', 'upnl': '0'}
    client.get_positions.return_value = []
    cost.append(path, [cost.event('seed', TS, 'fee', 'XAUS', '0', '规则')])
    asyncio.run(cost.capture_reconciliation(client, path, report_path, now=cost.timestamp(TS)))
    rows = [cost.event('fund', END, 'funding', 'XAUS', '-2', '/transfers'),
            cost.event('btc', END, 'realized_pnl', 'BTC', '-10', '/transfers')]
    rows += cost.fill_events(fill(), END)
    cost.append(path, rows)
    client.raw.return_value = {'balance': '88', 'upnl': '1'}
    client.get_positions.return_value = [
        {'position_info': {'instrument': {'underlying': 'XAUS'}, 'qty': '2'}, 'upnl': '-2'},
        {'position_info': {'instrument': {'underlying': 'BTC'}, 'qty': '1'}, 'upnl': '3'}]
    asyncio.run(cost.capture_reconciliation(client, path, report_path, now=cost.timestamp(END)))
    report, error = cost.read_report(cost.load(path)[0], report_path)
    assert error is None
    assert report['other_strategies'] == -7
    assert report['funding'] == -2
    assert report['spread'] == -2
    assert report['spread_embedded_adjustment'] == 0
    assert report['unrealized_change'] == 0
    assert report['residual'] == 0


def test_panel_warning_and_render(tmp_path):
    from panel.providers.swap_carry import _cost_metrics
    from panel.types import SystemStatus
    from tools.hedge_panel import _render_swap_carry
    path = tmp_path/'ledger.jsonl'
    cost.append(path, [cost.event('fee', TS, 'fee', 'XAUS', '0', '规则')])
    evidence = dict(start_ts=TS, end_ts=END, start_equity='100', end_equity='90',
                    realized_price_pnl='0', unrealized_change='0', external_cashflow='0',
                    other_strategies='0', scope='account', source='离线证据')
    report_path = tmp_path/'recon.json'
    report_path.write_text(json.dumps(evidence))
    metrics, alerts = _cost_metrics(path, report_path)
    assert alerts[0].level == 'warning'
    html = _render_swap_carry(SystemStatus(name='Swap Carry', alive=True, summary='测试',
                                         metrics=metrics, alerts=alerts))
    assert '成本明细' in html and '未解释残差' in html and '-10.00' in html


def test_midpoint_attribution_moves_open_spread_when_position_closes():
    opened = cost.fill_events(fill('open'), TS)
    closed = cost.fill_events(fill('close', 'sell'), END)
    first = cost.spread_basis(opened)
    final = cost.spread_basis(opened + closed)
    assert first == {'realized': Decimal('0'), 'unrealized': Decimal('-2')}
    assert final == {'realized': Decimal('-4'), 'unrealized': Decimal('0')}


def test_midpoint_attribution_handles_partial_close_and_unknown_initial_position():
    opened = cost.fill_events(fill('open'), TS)
    closing = fill('close', 'sell')
    closing['filled_quantity'] = '1'
    closed = cost.fill_events(closing, END)
    assert cost.spread_basis(opened + closed) == {'realized': Decimal('-2'), 'unrealized': Decimal('-1')}
    assert cost.spread_basis(closed) == {'realized': Decimal('-1'), 'unrealized': Decimal('0')}


def test_ledger_failure_does_not_change_successful_accept(tmp_path, caplog):
    from types import SimpleNamespace
    from tools import hedge_swap_carry as execution
    from adapters.base import Side
    path = tmp_path/'broken.jsonl'
    path.write_text('{broken')
    client = AsyncMock()
    client._max_slippage = .01
    client.accept_quote.return_value = {'rfq_id': 'accepted'}
    quote = SimpleNamespace(payload={'quote_id': 'q', 'bid': '99', 'ask': '101'},
                            side=Side.BUY, qty=Decimal('1'), leg=execution.XAUS_LEG)
    token = cost.ACTIVE_PATH.set(path)
    try:
        result = asyncio.run(execution._accept_quote(client, quote, reduce_only=False))
    finally:
        cost.ACTIVE_PATH.reset(token)
    assert result == {'rfq_id': 'accepted'}
    assert '成本台账写入失败' in caplog.text
    client.accept_quote.assert_awaited_once()


def test_explicit_transfer_id_survives_metadata_change_and_unknown_is_not_hidden(tmp_path):
    path = tmp_path/'ledger.jsonl'
    # id 作为可选唯一键单独测试；真实无 id 的资金费路径已由 BTC_TRANSFER 钉住。
    row = {**funding(), 'id': 'transfer-id'}
    cost.append(path, cost.transfer_events([row]))
    row['funding_rate'] = '999'
    cost.append(path, cost.transfer_events([row]))
    assert len(cost.load(path)[0]) == 1
    unknown = {**funding(), 'id': 'unknown', 'funding_rate': None, 'qty': '-4'}
    recorded = cost.transfer_events([unknown])[0]
    assert recorded['event_type'] == 'unclassified'
    assert cost.summarize([recorded])['total'] == 0


def test_since_does_not_reuse_wrong_equity_baseline(tmp_path):
    path = tmp_path/'recon.json'
    path.write_text(json.dumps(dict(start_ts=TS)))
    report, error = cost.read_report([], path, since=END)
    assert report is None and '起点不同' in error


def test_switch_live_recorder_decimal_fields_are_serializable(tmp_path):
    # _SwitchTradeRecorder 在写 JSON 前持有 Decimal，不能只用落盘后的字符串夹具。
    path = tmp_path/'ledger.jsonl'
    live = fill()
    live.update(execution_price=Decimal('101'), quote_mid=Decimal('100'),
                slippage_bp=Decimal('100'))
    switch = dict(started_at=TS, close_phase={'legs': []}, open_phase={'legs': [live]})
    cost.record_switch(path, switch)
    assert cost.summarize(cost.load(path)[0])['total'] == -2


def equity_file(tmp_path, *, start='100', end='116'):
    # portfolio_equity 实际 schema：Unix 秒、accounts 下直接保存十进制权益字符串。
    path = tmp_path / 'portfolio_equity.jsonl'
    rows = [dict(schema=6, ts=cost.timestamp(ts).timestamp(),
                 accounts={'variational': value}, sources={'variational': 'platform'},
                 symbols={'variational': ['BTC']}, errors={})
            for ts, value in [(TS, start), (END, end)] if value is not None]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def reconciliation_rows():
    return [cost.event(key, ts, kind, market, amount, '/transfers')
            for key, ts, kind, market, amount in [
                ('baseline', TS, 'fee', 'XAUS', '0'),
                ('cash', END, 'external_cashflow', 'ACCOUNT', '10'),
                ('carry', END, 'funding', 'XAUS', '2'),
                ('btc', END, 'realized_pnl', 'BTC', '3'),
                ('unknown', END, 'unclassified', 'ACCOUNT', '1')]]


def test_snapshot_missing_start_is_not_fabricated(tmp_path):
    report, error = cost.snapshot_report(reconciliation_rows(), equity_file(tmp_path, start=None))
    assert report is None
    assert '缺少期初权益快照' in error
    assert cost.timestamp(TS).isoformat() in error


@pytest.mark.parametrize('scope', ['account', 'swap_carry'])
def test_snapshot_cashflow_scope_and_unclassified_close(tmp_path, scope):
    report, error = cost.snapshot_report(reconciliation_rows(), equity_file(tmp_path), scope=scope)
    assert error is None
    assert report['residual'] == 0
    assert report['external_cashflow'] == 10
    assert report['unclassified'] == 1
    assert report['other_strategies'] == (3 if scope == 'swap_carry' else 0)
    assert report['realized_price_pnl'] == (0 if scope == 'swap_carry' else 3)
    assert report['unrealized_change'] is None
    assert not report['evidence_complete']
    assert report['start_ts'] == cost.timestamp(TS).isoformat()
    assert report['end_ts'] == cost.timestamp(END).isoformat()


def test_snapshot_missing_item_warns_and_cli_prints_red(tmp_path, capsys):
    from tools.show_cost_ledger import main
    rows = [r for r in reconciliation_rows() if r['event_id'] != 'carry']
    path = tmp_path / 'ledger.jsonl'
    cost.append(path, rows)
    equity_file(tmp_path)
    report, error = cost.snapshot_report(rows, tmp_path / 'portfolio_equity.jsonl')
    assert error is None and report['residual'] == 2 and report['warning']
    assert report['residual_ratio'] == Decimal('12.5')
    main(['--path', str(path), '--summary', '--scope', 'swap_carry'])
    output = capsys.readouterr().out
    for text in ['期初权益', '期末权益', '其它策略盈亏', '未分类流水', '12.50%',
                 '\x1b[31m', '漏记科目', '数据源缺口', '口径不一致']:
        assert text in output


def test_referral_reward_reclassifies_existing_rows_without_writing(tmp_path):
    raw = {**funding(), 'reference_instrument': None, 'funding_rate': None,
           'transfer_type': 'referral_reward', 'qty': '0.206334'}
    row = cost.transfer_events([raw])[0]
    assert row['event_type'] == 'referral_reward'
    row['event_type'] = 'unclassified'
    path = tmp_path / 'ledger.jsonl'
    cost.append(path, [row])
    before = path.read_bytes()
    loaded, error = cost.load(path)
    assert error is None and loaded[0]['event_type'] == 'referral_reward'
    assert path.read_bytes() == before


@pytest.mark.parametrize('end', [None, 'NaN'])
def test_missing_or_invalid_end_snapshot(tmp_path, end):
    report, error = cost.snapshot_report(reconciliation_rows(), equity_file(tmp_path, end=end))
    assert report is None and '期末权益快照' in error


def test_nearest_snapshot_offset_is_visible_without_changing_period(tmp_path):
    path = equity_file(tmp_path)
    data = [json.loads(line) for line in path.read_text().splitlines()]
    data[0]['ts'] -= 60
    data[1]['ts'] += 60
    path.write_text(''.join(json.dumps(row) + '\n' for row in data))
    report, error = cost.snapshot_report(reconciliation_rows(), path, since=TS)
    assert error is None
    assert report['start_ts'] == cost.timestamp(TS).isoformat()
    assert report['snapshot_offsets_seconds'] == (-60, 60)
    assert not report['evidence_complete']
    data[0]['ts'] -= 901
    path.write_text(''.join(json.dumps(row) + '\n' for row in data))
    report, error = cost.snapshot_report(reconciliation_rows(), path)
    assert report is None and '缺少期初权益快照' in error


@pytest.mark.parametrize('scope', ['account', 'swap_carry'])
def test_account_level_fee_reward_and_spread_are_not_lost(tmp_path, scope):
    rows = reconciliation_rows() + [
        cost.event('account-fee', END, 'fee', 'ACCOUNT', '-2', '/transfers'),
        cost.event('reward', END, 'referral_reward', 'ACCOUNT', '4', '/transfers'),
        cost.event('spread', END, 'spread', 'XAUS', '-1', '报价对比')]
    report, error = cost.snapshot_report(rows, equity_file(tmp_path, end='118'), scope=scope)
    assert error is None
    assert report['residual'] == 0
    assert report['fee'] == -2
    assert report['referral_reward'] == 4
    assert report['spread_embedded_adjustment'] == 1


def test_strict_reconcile_still_rejects_missing_evidence():
    inputs = dict(start_ts=TS, end_ts=END, scope='account', source='离线证据',
                  start_equity='100', end_equity='116', realized_price_pnl='3',
                  unrealized_change=None, external_cashflow='10', other_strategies='0')
    with pytest.raises(ValueError):
        cost.reconcile(reconciliation_rows(), inputs)
    inputs['unrealized_change'] = '0'
    report = cost.reconcile(reconciliation_rows(), inputs)
    assert report['residual'] == 0 and report['evidence_complete']


def test_since_zero_change_and_same_snapshot(tmp_path):
    rows = reconciliation_rows()
    path = equity_file(tmp_path, end='100')
    report, error = cost.snapshot_report(rows, path)
    assert error is None and report['residual_ratio'] is None
    assert report['warning']
    report, error = cost.snapshot_report(rows, path, since='2026-09-04T09:00:00Z')
    assert report is None and '缺少期初权益快照' in error
    short_rows = [rows[0], cost.event('end', '2026-09-04T08:01:00Z', 'fee', 'XAUS', '0', '规则')]
    report, error = cost.snapshot_report(short_rows, path)
    assert report is None and '同一时点' in error
