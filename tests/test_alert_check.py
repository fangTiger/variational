"""告警判据测试。

重点不是"正常时不报警"，而是**异常时确实会报警**——项目已经有过三次
"检测逻辑存在但没人知道"的无声事故，一个永远沉默的告警器等于没有。
每条判据都用构造数据验证一遍。
"""

from __future__ import annotations

import json
import time

import pytest

from tools import alert_check


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把告警模块的数据源指向临时目录，并默认 bot 存活。"""
    live = tmp_path / "grid_live.json"
    monitor = tmp_path / "grid_monitor.jsonl"
    monkeypatch.setattr(alert_check, "_LIVE", live)
    monkeypatch.setattr(alert_check, "_MONITOR", monitor)
    monkeypatch.setattr(alert_check, "_ALERT_STATE", tmp_path / "alert_state.json")
    monkeypatch.setattr(alert_check, "_bot_running", lambda: True)
    return {"live": live, "monitor": monitor, "now": 1_786_000_000.0}


def _write_live(path, now, **kw):
    payload = {
        "ts": now,
        "mode": "neutral",
        "halted": False,
        "effective_blocked_side": None,
        "last_success_ts": now,
        "consecutive_failures": 0,
    }
    payload.update(kw)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_monitor(path, rows):
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def _healthy_monitor(now, hours=6, **kw):
    rows = []
    for i in range(hours, 0, -1):
        row = {
            "ts": now - i * 3600,
            "equity": 970.0,
            "inv_usd": -100.0,
            "mode": "neutral",
            "blocked_side": None,
        }
        row.update(kw)
        rows.append(row)
    return rows


def _keys(alerts):
    return {a.key for a in alerts}


def test_healthy_state_is_silent(env):
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    assert alert_check.collect_alerts(env["now"]) == []


def test_bot_down_fires(env, monkeypatch):
    monkeypatch.setattr(alert_check, "_bot_running", lambda: False)
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    assert "bot_down" in _keys(alert_check.collect_alerts(env["now"]))


def test_stale_live_snapshot_fires(env):
    # 引擎每 2.5 秒写一次，20 分钟没动静必然是卡死
    _write_live(env["live"], env["now"] - 1200)
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    assert "live_stale" in _keys(alert_check.collect_alerts(env["now"]))


def test_halted_fires(env):
    _write_live(env["live"], env["now"], halted=True)
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    assert "halted" in _keys(alert_check.collect_alerts(env["now"]))


def test_long_off_stall_fires(env):
    """回归 2026-08-10：OFF 停摆 19.5 小时，frozen=False，旧检测完全没反应。"""
    _write_live(env["live"], env["now"], mode="off", effective_blocked_side="BOTH")
    _write_monitor(
        env["monitor"],
        _healthy_monitor(env["now"], hours=6, mode="off", blocked_side="BOTH"),
    )
    alerts = alert_check.collect_alerts(env["now"])
    assert "grid_blocked" in _keys(alerts)
    body = next(a.body for a in alerts if a.key == "grid_blocked")
    assert "小时" in body


def test_brief_off_does_not_fire(env):
    """OFF 短暂穿越是正常的，不该扰民。"""
    _write_live(env["live"], env["now"], mode="off", effective_blocked_side="BOTH")
    rows = _healthy_monitor(env["now"], hours=6)
    rows[-1].update(mode="off", blocked_side="BOTH")  # 只有最近 1 小时是 OFF
    _write_monitor(env["monitor"], rows)
    assert "grid_blocked" not in _keys(alert_check.collect_alerts(env["now"]))


def test_no_success_fires_even_though_process_alive(env):
    """回归 2026-08-13：DNS 故障 3 小时、3686 个连接错误，但进程活着、
    快照仍在更新（失败轮次也写 live），六条判据全部静默。

    只有 last_success_ts 反映真实连通性，ts 不行。
    """
    _write_live(
        env["live"],
        env["now"],                       # 快照是新鲜的
        last_success_ts=env["now"] - 3 * 3600,  # 但 3 小时没有一轮成功
        consecutive_failures=1200,
    )
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    alerts = alert_check.collect_alerts(env["now"])
    assert "no_success" in _keys(alerts)
    body = next(a.body for a in alerts if a.key == "no_success")
    assert "1200" in body


def test_brief_connection_hiccup_does_not_fire(env):
    """几分钟的抖动是常态，不该扰民。"""
    _write_live(
        env["live"],
        env["now"],
        last_success_ts=env["now"] - 300,
        consecutive_failures=30,
    )
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    assert "no_success" not in _keys(alert_check.collect_alerts(env["now"]))


def test_stale_monitor_fires(env):
    _write_live(env["live"], env["now"])
    rows = _healthy_monitor(env["now"])
    for r in rows:
        r["ts"] -= 4 * 3600
    _write_monitor(env["monitor"], rows)
    assert "monitor_stale" in _keys(alert_check.collect_alerts(env["now"]))


def test_high_leverage_fires(env):
    _write_live(env["live"], env["now"])
    rows = _healthy_monitor(env["now"])
    rows[-1]["inv_usd"] = 4000.0  # 权益 970 → 约 4.1x
    _write_monitor(env["monitor"], rows)
    alerts = alert_check.collect_alerts(env["now"])
    assert "leverage" in _keys(alerts)


def test_leverage_just_under_threshold_is_silent(env):
    _write_live(env["live"], env["now"])
    rows = _healthy_monitor(env["now"])
    rows[-1]["inv_usd"] = -2000.0  # 权益 970 → 约 2.1x，低于 3x 阈值
    _write_monitor(env["monitor"], rows)
    assert "leverage" not in _keys(alert_check.collect_alerts(env["now"]))


def test_missing_files_fire_not_crash(env):
    """数据文件缺失时要报警，而不是抛异常把调度任务搞挂。"""
    alerts = alert_check.collect_alerts(env["now"])
    assert {"live_missing", "monitor_missing"} <= _keys(alerts)


def test_notify_failure_does_not_raise(monkeypatch):
    def boom(*a, **kw):
        raise OSError("osascript 不可用")

    monkeypatch.setattr(alert_check.subprocess, "run", boom)
    alert_check.notify("标题", "内容")  # 不抛错即通过
