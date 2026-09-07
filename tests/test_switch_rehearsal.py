"""结构预演失败路径；复用已核实的交易所 schema，全程离线。"""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.test_run_swap_carry_guard import NOW, _metadata, _paths, _switch_client
from tools import run_swap_carry_guard as guard


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """禁止真实通知和继承本地会话。"""
    monkeypatch.delenv("VARIATIONAL_COOKIE", raising=False)
    monkeypatch.delenv("VARIATIONAL_WALLET_ADDRESS", raising=False)
    monkeypatch.setattr(guard, "notify", lambda *args: True)


def client_for_rehearsal():
    """关市前 85 分钟，即计划切换前 25 分钟。"""
    client = _switch_client(
        positions={"XAUS": Decimal("0.01"), "XAU": Decimal("-0.01")},
        metadata=_metadata(closure_duration=timedelta(hours=49),
                           time_until_close=timedelta(minutes=85)),
        swap_rate=Decimal("0.04"),
        perp_rate={"XAU": Decimal("0"), "XAUT": Decimal("0.1095")},
        accept_script=[{}, {}, {}, {}],
    )

    async def raw(path):
        assert path == "/portfolio"
        return {"balance": "1000", "upnl": "0"}

    client.raw = raw
    return client


def run(client, tmp_path, now=NOW, **kwargs):
    return asyncio.run(guard.run_once(client, now=now, **_paths(tmp_path), **kwargs))


def heartbeat(tmp_path):
    return json.loads(_paths(tmp_path)["heartbeat_path"].read_text())


def rehearsals(tmp_path):
    path = _paths(tmp_path)["switch_history_path"]
    return [row for row in map(json.loads, path.read_text().splitlines())
            if row.get("kind") == "rehearsal"]


def test_insufficient_margin_blocks_switch_and_notifies(tmp_path, monkeypatch):
    client = client_for_rehearsal()
    notices = []
    monkeypatch.setattr(guard, "notify", lambda *args: notices.append(args))

    async def raw(path):
        assert path == "/portfolio"
        return {"balance": "9", "upnl": "0"}

    client.raw = raw
    run(client, tmp_path)
    report = heartbeat(tmp_path)["last_rehearsal"]
    assert report["conclusion"] == "blocked"
    assert "保证金不足" in str(report["blocking_reasons"])
    assert heartbeat(tmp_path)["rehearsal_blocked"] is True
    assert notices
    run(client, tmp_path, NOW + timedelta(minutes=25))
    assert client.accept_calls == []
    assert not heartbeat(tmp_path)["auto_switch_attempted"]
    assert len(rehearsals(tmp_path)) == 1
    assert rehearsals(tmp_path)[0]["level"] == "critical"


@pytest.mark.parametrize("failure", ["untradable", "quote", "malformed_margin"])
def test_bad_market_or_quote_blocks(tmp_path, failure):
    client = client_for_rehearsal()
    original = client.request_quote
    if failure == "untradable":
        client.metadata["XAUT"][0]["market_status"] = "closed"

    async def quote(*args, **kwargs):
        if args[0] == "XAUT" and failure == "quote":
            raise RuntimeError("报价接口不可用")
        payload = await original(*args, **kwargs)
        if failure == "malformed_margin":
            payload["margin_requirements"] = {"initial_margin": "1"}
        return payload

    client.request_quote = quote
    run(client, tmp_path)
    assert heartbeat(tmp_path)["last_rehearsal"]["conclusion"] == "blocked"
    assert client.accept_calls == []


def test_ready_deduplicates_across_rounds_and_allows_real_switch(tmp_path):
    client = client_for_rehearsal()
    run(client, tmp_path)
    assert heartbeat(tmp_path)["last_rehearsal"]["conclusion"] == "ready"
    report = rehearsals(tmp_path)[0]
    assert report["before"]["net_delta"] == "0.00"
    assert len(report["close_legs"]) == len(report["open_legs"]) == 2
    assert report["estimated_duration_ms"] is None
    assert report["margin"]["available_usd"] == "992.000"
    assert client.accept_calls == []
    run(client, tmp_path, NOW + timedelta(minutes=5))
    assert len(rehearsals(tmp_path)) == 1
    run(client, tmp_path, NOW + timedelta(minutes=25))
    assert heartbeat(tmp_path)["auto_switch_attempted"] is True
    assert len(client.accept_calls) == 4


def test_large_slippage_warns_but_allows_switch(tmp_path):
    client = client_for_rehearsal()
    original = client.request_quote

    async def quote(*args, **kwargs):
        payload = await original(*args, **kwargs)
        payload.update(bid="3960", ask="4040")
        return payload

    client.request_quote = quote
    run(client, tmp_path)
    assert heartbeat(tmp_path)["last_rehearsal"]["conclusion"] == "warning"
    assert not heartbeat(tmp_path)["rehearsal_blocked"]
    run(client, tmp_path, NOW + timedelta(minutes=25))
    assert heartbeat(tmp_path)["auto_switch_attempted"]
    records = list(map(json.loads, _paths(tmp_path)["switch_history_path"].read_text().splitlines()))
    assert records[-1]["rehearsal"]["conclusion"] == "warning"


def test_exception_is_blocked_and_later_close_risk_still_runs(tmp_path, monkeypatch):
    client = client_for_rehearsal()

    async def broken(*args, **kwargs):
        raise RuntimeError("预演内部异常")

    monkeypatch.setattr(guard, "_perform_rehearsal", broken, raising=False)
    run(client, tmp_path)
    assert heartbeat(tmp_path)["last_rehearsal"]["conclusion"] == "blocked"
    assert "预演内部异常" in str(rehearsals(tmp_path)[0]["blocking_reasons"])
    run(client, tmp_path, NOW + timedelta(minutes=60))
    assert len(client.accept_calls) == 2
    assert all(item[2] for item in client.accept_calls)
    assert not heartbeat(tmp_path)["auto_switch_attempted"]


def test_kill_switch_preempts_rehearsal(tmp_path, monkeypatch):
    client = client_for_rehearsal()

    async def forbidden(*args, **kwargs):
        pytest.fail("平仓风控不得等待预演")

    monkeypatch.setattr(guard, "_perform_rehearsal", forbidden, raising=False)
    _paths(tmp_path)["kill_switch_path"].touch()
    run(client, tmp_path)
    assert len(client.accept_calls) == 2
    assert all(item[2] for item in client.accept_calls)


def test_next_window_rehearses_again_and_clears_block(tmp_path):
    client = client_for_rehearsal()
    client.metadata["XAUT"][0]["market_status"] = "closed"
    run(client, tmp_path)
    assert heartbeat(tmp_path)["rehearsal_blocked"]
    client = client_for_rehearsal()
    # 新的一周使用同样的真实时段 schema，仅移动时间边界。
    for session in client.metadata["XAUS"][0]["trading_sessions"]:
        for key in ("open", "close"):
            from datetime import datetime
            session[key] = (datetime.fromisoformat(session[key]) + timedelta(days=7)).isoformat()
    for key, value in client.metadata["XAUS"][0]["trading_schedule"].items():
        client.metadata["XAUS"][0]["trading_schedule"][key] = (datetime.fromisoformat(value) + timedelta(days=7)).isoformat()
    run(client, tmp_path, NOW + timedelta(days=7))
    assert heartbeat(tmp_path)["last_rehearsal"]["conclusion"] == "ready"
    assert not heartbeat(tmp_path)["rehearsal_blocked"]
    assert len(rehearsals(tmp_path)) == 2


def test_median_ignores_rehearsals_and_dry_runs(tmp_path):
    client = client_for_rehearsal()
    records = [
        {"status": "completed", "total_duration_ms": 1000},
        {"status": "completed", "total_duration_ms": 3000},
        {"status": "completed", "kind": "rehearsal", "total_duration_ms": 999999},
        {"status": "dry_run", "total_duration_ms": 999999},
    ]
    _paths(tmp_path)["switch_history_path"].write_text("\n".join(map(json.dumps, records)) + "\n")
    run(client, tmp_path)
    assert rehearsals(tmp_path)[-1]["estimated_duration_ms"] == 2000


def test_completed_close_switch_does_not_rehearse_old_open_window(tmp_path):
    """已切到周末结构后，不得又把本周开市当作待执行窗口。"""
    client = client_for_rehearsal()
    run(client, tmp_path)
    run(client, tmp_path, NOW + timedelta(minutes=25))
    run(client, tmp_path, NOW + timedelta(minutes=30))
    assert len(rehearsals(tmp_path)) == 1


def test_early_round_does_not_rehearse(tmp_path):
    client = client_for_rehearsal()
    run(client, tmp_path, NOW - timedelta(minutes=10))
    assert heartbeat(tmp_path)["last_rehearsal"] is None
    assert not _paths(tmp_path)["switch_history_path"].exists()


def test_blocked_flat_account_cannot_reopen_in_same_window(tmp_path):
    client = client_for_rehearsal()
    client.metadata["XAUT"][0]["market_status"] = "closed"
    run(client, tmp_path)
    run(client, tmp_path, NOW + timedelta(minutes=60))
    assert all(size == 0 for size in client.sizes.values())
    client.metadata["XAUT"][0]["market_status"] = "open"
    run(client, tmp_path, NOW + timedelta(minutes=65))
    assert len(client.accept_calls) == 2
    assert heartbeat(tmp_path)["rehearsal_blocked"]
    assert len(rehearsals(tmp_path)) == 1


def test_rehearsal_has_no_accept_capability():
    from tools.swap_carry_rehearsal import IndicativeOnly
    readonly = IndicativeOnly(client_for_rehearsal())
    assert not hasattr(readonly, "accept_quote")
    assert not hasattr(readonly, "raw")


def test_dry_run_preserves_no_post_contract(tmp_path):
    client = client_for_rehearsal()
    client.quote_enabled = False
    run(client, tmp_path, dry_run=True)
    assert client.quote_calls == [] and client.accept_calls == []
    assert heartbeat(tmp_path)["last_rehearsal"] is None


def test_notification_failure_does_not_lose_block(tmp_path, monkeypatch):
    client = client_for_rehearsal()
    client.metadata["XAUT"][0]["market_status"] = "closed"

    def fail(*args):
        raise RuntimeError("通知不可用")

    monkeypatch.setattr(guard, "notify", fail)
    run(client, tmp_path)
    assert heartbeat(tmp_path)["last_rehearsal"]["conclusion"] == "blocked"
    assert len(rehearsals(tmp_path)) == 1


def test_ledger_failure_does_not_prevent_preclose_flatten(tmp_path, monkeypatch):
    client = client_for_rehearsal()
    original = guard._append_audit

    def fail(path, payload):
        if path == _paths(tmp_path)["switch_history_path"]:
            raise OSError("台账不可写")
        return original(path, payload)

    monkeypatch.setattr(guard, "_append_audit", fail)
    run(client, tmp_path, NOW + timedelta(minutes=60))
    assert heartbeat(tmp_path)["last_rehearsal"]["conclusion"] == "blocked"
    assert len(client.accept_calls) == 2
    assert all(item[2] for item in client.accept_calls)


def test_reverse_existing_leg_uses_standalone_margin_not_negative_delta(tmp_path):
    """反向 XAU 报价先抵消旧仓；负增量不能当新结构的保证金需求。"""
    client = client_for_rehearsal()
    client.sizes.update(XAUS=Decimal("0.5"), XAU=Decimal("-0.5"))
    original = client.request_quote

    async def quote(*args, **kwargs):
        payload = await original(*args, **kwargs)
        # 参数结构来自真实 margin_params；初始率与同标的维持率并列。
        payload["margin_params"]["params"]["asset_params"][args[0]]["futures_initial_margin"] = "0.05"
        if args[0] == "XAU" and args[1] == "buy":
            payload["margin_requirements"]["ask_margin_delta"]["initial_margin"] = "-100"
        return payload

    client.request_quote = quote
    run(client, tmp_path)
    report = rehearsals(tmp_path)[0]
    assert report["conclusion"] == "ready"
    assert Decimal(report["margin"]["required_usd"]) == Decimal("200")
    xau = next(leg for leg in report["open_legs"] if leg["market"] == "XAU")
    assert Decimal(xau["required_margin_usd"]) > 0
    assert xau["quoted_margin_delta_usd"] == "-100"
    assert client.accept_calls == []


def test_blocked_window_skips_readiness_before_emergency_close(tmp_path, monkeypatch):
    client = client_for_rehearsal()
    client.metadata["XAUT"][0]["market_status"] = "closed"
    run(client, tmp_path)

    async def unexpected(*args, **kwargs):
        raise RuntimeError("已经 blocked，不应再检查开仓准入")

    monkeypatch.setattr(guard, "_switch_readiness", unexpected)
    run(client, tmp_path, NOW + timedelta(minutes=60))
    assert len(client.accept_calls) == 2
    assert all(item[2] for item in client.accept_calls)


def test_kill_switch_arriving_during_rehearsal_preempts_switch(tmp_path, monkeypatch):
    client = client_for_rehearsal()
    original = guard._perform_rehearsal

    async def rehearse(*args, **kwargs):
        result = await original(*args, **kwargs)
        _paths(tmp_path)["kill_switch_path"].touch()
        return result

    monkeypatch.setattr(guard, "_perform_rehearsal", rehearse)
    run(client, tmp_path, NOW + timedelta(minutes=25))
    assert len(client.accept_calls) == 2
    assert all(item[2] for item in client.accept_calls)


def test_real_adapter_rehearsal_http_only_indicative(tmp_path):
    from adapters.variational_client import VariationalClient
    client = client_for_rehearsal()
    original = client.request_quote
    adapter = object.__new__(VariationalClient)
    calls = []

    async def post(path, body):
        calls.append(path)
        assert path == "/quotes/indicative"
        instrument = body["instrument"]
        return await original(instrument["underlying"], body["side"], Decimal(body["qty"]))

    adapter._post = post
    client.request_quote = adapter.request_quote
    run(client, tmp_path)
    assert heartbeat(tmp_path)["last_rehearsal"]["conclusion"] == "ready"
    assert calls and set(calls) == {"/quotes/indicative"}
    assert client.accept_calls == []


def test_opening_window_closed_market_block_survives_reopening(tmp_path):
    """预演时不可交易按要求阻断，开市本身不清除该窗口的结论。"""
    from datetime import datetime
    client = client_for_rehearsal()
    client.sizes.update(XAUS=Decimal("0"), XAU=Decimal("0.01"), XAUT=Decimal("-0.01"))
    client.metadata = _metadata(closure_duration=timedelta(hours=49), market_status="closed")
    opens = datetime.fromisoformat(client.metadata["XAUS"][0]["trading_schedule"]["next_open_at"])
    run(client, tmp_path, opens - timedelta(minutes=25))
    report = heartbeat(tmp_path)["last_rehearsal"]
    assert report["conclusion"] == "blocked"
    assert "XAUS 当前不可交易" in report["blocking_reasons"]
    client.metadata["XAUS"][0]["market_status"] = "open"
    run(client, tmp_path, opens)
    assert heartbeat(tmp_path)["rehearsal_blocked"]
    assert len(rehearsals(tmp_path)) == 1
    assert client.accept_calls == []


def test_rehearsal_lead_configuration(monkeypatch):
    assert guard.REHEARSAL_LEAD == timedelta(minutes=30)
    monkeypatch.setenv("REHEARSAL_LEAD", "45")
    assert guard.build_parser().parse_args([]).rehearsal_lead_minutes == Decimal("45")
    assert guard.build_parser().parse_args(["--rehearsal-lead-minutes", "20"]).rehearsal_lead_minutes == Decimal("20")
