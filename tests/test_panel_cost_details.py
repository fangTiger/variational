"""成本面板离线回归，所有文件仅写入临时目录。"""
import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from engine import swap_carry_cost as cost
from panel.providers.swap_carry import _cost_metrics
from panel.types import SystemStatus
from tools.hedge_panel import build_page


def prepare(tmp_path, *, count=9, end='98'):
    start = datetime(2026, 9, 7, tzinfo=timezone.utc)
    ledger = tmp_path / 'ledger.jsonl'
    rows = [cost.event('fee', (start + timedelta(minutes=15)).isoformat(),
                       'fee', 'XAUS', '-2', '测试来源'),
            cost.event('switch', (start + timedelta(minutes=30)).isoformat(),
                       'switch', 'XAUS/XAU/XAUT', '-2', '切换台账')]
    ledger.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    equity = tmp_path / 'portfolio_equity.jsonl'
    times = [start - timedelta(days=2), start - timedelta(days=1)]
    times += [start + timedelta(minutes=15*i) for i in range(count)]
    equity.write_text(''.join(json.dumps({'ts': t.timestamp(), 'accounts': {
        'variational': end if t == times[-1] else '100'}}) + '\n' for t in times))
    return ledger, equity, start


def page(tmp_path, metrics, alerts, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('测试不得联网')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    status = SystemStatus(name='Swap Carry', alive=True, summary='持仓正常',
                          metrics=metrics, alerts=alerts)
    return build_page(instances=(), now=1, swap_carry_status=status,
                      portfolio_equity_path=tmp_path/'missing',
                      portfolio_volume_path=tmp_path/'missing')


def test_latest_continuous_period_details_and_events(tmp_path, monkeypatch):
    ledger, equity, start = prepare(tmp_path)
    metrics, alerts = _cost_metrics(ledger, None, equity_path=equity)
    values = {m.label: m for m in metrics}
    assert values['成本对账区间'].value == f'{start.isoformat()} → {(start + timedelta(hours=2)).isoformat()}'
    assert values['成本合计'].value == '-2.00 USD'
    assert values['期初权益'].value == '+100.00 USD'
    assert values['期末权益'].value == '+98.00 USD'
    assert values['未解释残差'].value == '+0.00 USD'
    output = page(tmp_path, metrics, alerts, monkeypatch)
    for text in ['成本明细', '权益变化', '已解释合计', '残差占比', '最近磨损事件',
                 '测试来源', '不重复计入合计', '证据不完整，不能确认闭合']:
        assert text in output
    assert '已闭合' not in output
    assert 'pnl-negative' in output


def test_excess_residual_is_red_and_alerts(tmp_path, monkeypatch):
    ledger, equity, _ = prepare(tmp_path, end='110')
    metrics, alerts = _cost_metrics(ledger, None, equity_path=equity)
    assert next(m for m in metrics if m.label == '未解释残差').tone == 'bad'
    assert any(a.key == 'swap_carry_cost_residual' for a in alerts)
    output = page(tmp_path, metrics, alerts, monkeypatch)
    assert '超过阈值' in output
    assert 'exposure-bad">+12.00 USD' in output


@pytest.mark.parametrize('count', [1, 3])
def test_short_equity_segment(tmp_path, monkeypatch, count):
    ledger, equity, _ = prepare(tmp_path, count=count)
    metrics, alerts = _cost_metrics(ledger, None, equity_path=equity)
    output = page(tmp_path, metrics, alerts, monkeypatch)
    assert '权益数据不足以对账' in output
    assert '最近磨损事件' in output


@pytest.mark.parametrize('content', [None, '{损坏', '[]'])
def test_bad_ledger_degrades(tmp_path, monkeypatch, content):
    ledger = tmp_path/'ledger.jsonl'
    if content is not None:
        ledger.write_text(content)
    metrics, alerts = _cost_metrics(ledger, None, equity_path=tmp_path/'missing')
    output = page(tmp_path, metrics, alerts, monkeypatch)
    assert '成本台账不可用' in output
    assert '证据不完整，不能确认闭合' in output
    assert '持仓正常' in output


def test_recent_events_limited_sorted_and_escaped(tmp_path, monkeypatch):
    ledger, equity, start = prepare(tmp_path)
    rows = [cost.event(str(i), (start + timedelta(minutes=i)).isoformat(),
                       'funding', 'XAU', '1', '<来源>') for i in range(15)]
    rows += [cost.event('btc', start.isoformat(), 'fee', 'BTC', '-99', '其它策略')]
    ledger.write_text(''.join(json.dumps(r) + '\n' for r in reversed(rows)))
    metrics, alerts = _cost_metrics(ledger, None, equity_path=equity)
    events = [m for m in metrics if m.label.startswith('磨损事件 ')]
    assert len(events) == 10
    assert '00:14:00' in events[0].value
    assert all(m.tone == 'good' for m in events)
    output = page(tmp_path, metrics, alerts, monkeypatch)
    assert '&lt;来源&gt;' in output
    assert '<来源>' not in output


@pytest.mark.parametrize('content', [None, '{损坏', '[]', '{"ts": 1, "errors": []}'])
def test_bad_equity_keeps_costs_and_page(tmp_path, monkeypatch, content):
    ledger, equity, _ = prepare(tmp_path)
    equity.unlink()
    if content is not None:
        equity.write_text(content)
    metrics, alerts = _cost_metrics(ledger, None, equity_path=equity)
    output = page(tmp_path, metrics, alerts, monkeypatch)
    assert '对账不可用' in output
    assert '持仓正常' in output
    assert '测试来源' in output
