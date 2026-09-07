"""自适应轮询只读取临时本地心跳，交易所客户端全部替换为假对象。"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from tools import run_swap_carry_guard as guard

NOW = datetime(2026, 9, 7, 8, tzinfo=timezone.utc)


@pytest.fixture
def polling(tmp_path, monkeypatch):
    """冻结进程启动时间，在客户端构造边界检测任何交易所访问。"""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(guard, "datetime", Clock)
    args = guard.build_parser().parse_args([
        "--once", "--heartbeat", str(tmp_path / "heartbeat.json"),
        "--kill-switch", str(tmp_path / "kill"),
        "--state", str(tmp_path / "state.json"),
    ])
    client = AsyncMock()
    load = AsyncMock(return_value=client)
    full = AsyncMock(return_value=0)
    monkeypatch.setattr(guard.execution, "_load", load)
    monkeypatch.setattr(guard, "run_once", full)
    heartbeat = {
        "timestamp": (NOW - timedelta(seconds=30)).isoformat(),
        "last_full_round_at": (NOW - timedelta(seconds=30)).isoformat(),
        "status": "healthy", "conclusion": "无需动作：仓位与风控检查正常",
        "structure": "XAUS_XAU",
        "legs": {"XAUS": {"margin_mode": "isolated", "per_leg_liquidation": {"distance": "0.20"}}},
        "account_margin": {"ratio": "4.0"},
        "xaus_schedule": {"metadata_is_fresh": True, "closure_duration_seconds": 49 * 3600,
                          "next_close_at": (NOW + timedelta(hours=8)).isoformat()},
    }
    return args, heartbeat, load, full


def run(polling):
    """只把测试快照写入临时目录，再走真实命令入口。"""
    args, heartbeat, load, full = polling
    args.heartbeat.write_text(json.dumps(heartbeat), encoding="utf-8")
    assert asyncio.run(guard._main(args)) == 0
    return json.loads(args.heartbeat.read_text(encoding="utf-8"))


def test_normal_recent_round_skips_without_exchange_requests(polling):
    saved = run(polling)
    args, old, load, full = polling
    load.assert_not_awaited()
    full.assert_not_awaited()
    assert saved["last_seen"] == NOW.isoformat()
    assert saved["timestamp"] == NOW.isoformat()
    assert saved["last_full_round_at"] == old["last_full_round_at"]
    assert saved["polling_mode"] == "normal"
    assert saved["skipped_reason"]
    assert saved["critical_reasons"] == []
    assert saved["next_full_round_at"] == (NOW + timedelta(seconds=270)).isoformat()
    assert saved["legs"] == old["legs"]


@pytest.mark.parametrize("trigger", ["elapsed", "switch", "liquidation", "margin", "incident", "blocked", "close", "kill", "missing", "corrupt", "future", "reopen"])
def test_critical_or_due_round_never_skips(polling, trigger):
    args, heartbeat, load, full = polling
    if trigger == "elapsed":
        heartbeat["last_full_round_at"] = (NOW - timedelta(seconds=400)).isoformat()
    elif trigger == "switch":
        # 默认提前 60 分钟切换，因此距休市 90 分钟就是距切换 30 分钟。
        heartbeat["xaus_schedule"]["next_close_at"] = (NOW + timedelta(minutes=90)).isoformat()
    elif trigger == "reopen":
        heartbeat["structure"] = "XAU_XAUT"
        heartbeat["xaus_schedule"]["next_open_at"] = (NOW + timedelta(minutes=30)).isoformat()
    elif trigger == "liquidation":
        heartbeat["legs"]["XAUS"]["per_leg_liquidation"]["distance"] = "0.045"
    elif trigger == "margin":
        heartbeat["account_margin"]["ratio"] = "2.5"
    elif trigger in {"incident", "blocked"}:
        heartbeat["status"] = trigger
    elif trigger == "close":
        heartbeat["close_attempted"] = True
    elif trigger == "kill":
        heartbeat["last_full_round_at"] = (NOW - timedelta(seconds=10)).isoformat()
        args.kill_switch.touch()
    elif trigger == "future":
        heartbeat["last_full_round_at"] = (NOW + timedelta(seconds=30)).isoformat()
    if trigger in {"missing", "corrupt"}:
        if trigger == "corrupt":
            args.heartbeat.write_text("{损坏", encoding="utf-8")
        assert asyncio.run(guard._main(args)) == 0
    else:
        run(polling)
    load.assert_awaited_once()
    full.assert_awaited_once()
    assert full.call_args.kwargs["polling_mode"] == ("normal" if trigger == "elapsed" else "critical")
    load.return_value.close.assert_awaited_once()


def test_skips_do_not_postpone_full_round(polling):
    run(polling)
    args, heartbeat, load, full = polling
    saved = json.loads(args.heartbeat.read_text())
    saved["last_full_round_at"] = (NOW - timedelta(seconds=300)).isoformat()
    polling = args, saved, load, full
    run(polling)
    full.assert_awaited_once()


def test_real_full_round_writes_polling_heartbeat(tmp_path):
    """实际完整轮次产出的字段必须能供下一次启动消费。"""
    from tests.test_run_swap_carry_guard import _healthy_client, _run, _paths
    assert _run(_healthy_client(), tmp_path, auto_open=False, auto_switch=False) == 0
    saved = json.loads(_paths(tmp_path)["heartbeat_path"].read_text())
    assert saved["last_seen"] == saved["last_full_round_at"] == saved["timestamp"]
    assert saved["polling_mode"] in {"normal", "critical"}
    assert saved["skipped_reason"] is None
    assert guard.execution._guard_timestamp(saved["next_full_round_at"]) > guard.execution._guard_timestamp(saved["last_full_round_at"])


@pytest.mark.parametrize("change", ["allocation", "nan", "malformed", "overdue", "rehearsal"])
def test_additional_local_risk_stays_critical(polling, change):
    args, heartbeat, load, full = polling
    if change == "allocation":
        heartbeat["isolated_allocation"] = {"XAUS": {"distance": "0.045"}}
    elif change == "nan":
        heartbeat["account_margin"]["ratio"] = "NaN"
    elif change == "malformed":
        heartbeat["legs"] = []
    elif change == "overdue":
        heartbeat["xaus_schedule"]["next_close_at"] = (NOW + timedelta(minutes=50)).isoformat()
    else:
        heartbeat["rehearsal_blocked"] = True
    run(polling)
    run(polling)
    assert full.await_count == 2
    assert full.call_args.kwargs["polling_mode"] == "critical"


def test_critical_boundary_limits_expected_next_round(polling):
    args, heartbeat, load, full = polling
    heartbeat["xaus_schedule"]["next_close_at"] = (NOW + timedelta(minutes=107)).isoformat()
    saved = run(polling)
    assert saved["next_full_round_at"] == (NOW + timedelta(minutes=2)).isoformat()
    full.assert_not_awaited()


def test_cross_margin_leg_does_not_trigger_isolated_threshold(polling):
    args, heartbeat, load, full = polling
    heartbeat["legs"]["XAUS"].update(
        margin_mode="cross", per_leg_liquidation={"distance": "0.01"},
    )
    run(polling)
    load.assert_not_awaited()


def test_real_close_round_records_critical_mode(tmp_path, monkeypatch):
    """真实平仓分支留下明确动作标记，不能因平仓成功便回到慢轮询。"""
    from tests.test_run_swap_carry_guard import _healthy_client, _run, _paths
    paths = _paths(tmp_path)
    paths["kill_switch_path"].touch()
    monkeypatch.setattr(guard, "notify", lambda *_args: True)
    assert _run(_healthy_client(accept_script=[{}, {}]), tmp_path) == 0
    saved = json.loads(paths["heartbeat_path"].read_text())
    assert saved["close_attempted"] is True
    assert saved["critical_reasons"]
    paths["kill_switch_path"].unlink()
    mode, _ = guard._polling_plan(
        saved, now=guard.execution._guard_timestamp(saved["last_full_round_at"]) + timedelta(seconds=10),
        kill_switch_path=paths["kill_switch_path"], switch_lead_time=guard.SWITCH_LEAD_TIME,
    )
    assert mode == "critical"


def test_recovered_round_predicts_next_round_from_new_snapshot(tmp_path):
    """首次保守启动后若确认安全，下次预计时间必须符合新的本地判定。"""
    from tests.test_run_swap_carry_guard import _flat_open_client, _run, _paths
    assert _run(_flat_open_client(), tmp_path, auto_open=False, auto_switch=False,
                polling_mode="critical") == 0
    paths = _paths(tmp_path)
    saved = json.loads(paths["heartbeat_path"].read_text())
    completed = guard.execution._guard_timestamp(saved["last_full_round_at"])
    mode, expected = guard._polling_plan(
        saved, now=completed + timedelta(seconds=60),
        kill_switch_path=paths["kill_switch_path"], switch_lead_time=guard.SWITCH_LEAD_TIME,
    )
    assert mode == "normal"
    assert saved["polling_mode"] == "normal"
    assert saved["critical_reasons"] == []
    assert saved["next_full_round_at"] == expected.isoformat()


@pytest.mark.parametrize('distance,errors,expected', [('0.074',0,'normal'), ('0.045',0,'critical'), ('0.081',1,'critical')])
def test_danger_threshold_and_failed_protection(polling, distance, errors, expected):
    args, heartbeat, load, full = polling
    heartbeat['legs']['XAUS']['per_leg_liquidation']['distance'] = distance
    heartbeat['isolated_allocation'] = {'XAUS': {'distance':distance, 'daily_abnormal_count':errors}}
    saved = run(polling)
    if expected == 'normal':
        assert saved['polling_mode'] == 'normal'
        full.assert_not_awaited()
        load.assert_not_awaited()
    else:
        assert full.call_args.kwargs['polling_mode'] == 'critical'


@pytest.mark.parametrize('distance,expected', [('0.074', []), ('0.045', ['liquidation_distance:XAUS'])])
def test_polling_reasons_explain_distance(polling, distance, expected):
    args, heartbeat, load, full = polling
    heartbeat['legs']['XAUS']['per_leg_liquidation']['distance'] = distance
    reasons = []
    mode, _ = guard._polling_plan(heartbeat, now=NOW, kill_switch_path=args.kill_switch,
                                switch_lead_time=guard.SWITCH_LEAD_TIME, critical_reasons=reasons)
    assert reasons == expected
    assert mode == ('critical' if expected else 'normal')
    if not expected:
        assert run(polling)['critical_reasons'] == []
        full.assert_not_awaited()
