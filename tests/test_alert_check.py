"""告警判据测试。

重点不是"正常时不报警"，而是**异常时确实会报警**——项目已经有过三次
"检测逻辑存在但没人知道"的无声事故，一个永远沉默的告警器等于没有。
每条判据都用构造数据验证一遍。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from tools import alert_check


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把告警模块的数据源指向临时目录，并默认 bot 存活。"""
    live = tmp_path / "grid_live.json"
    monitor = tmp_path / "grid_monitor.jsonl"
    hedge = tmp_path / "lighter_hedge.jsonl"
    mm = tmp_path / "lighter_mm.jsonl"
    monkeypatch.setattr(alert_check, "_ROOT", tmp_path)
    monkeypatch.setattr(alert_check, "_LIVE", live)
    monkeypatch.setattr(alert_check, "_MONITOR", monitor)
    monkeypatch.setattr(alert_check, "_HEDGE_MONITOR", hedge, raising=False)
    monkeypatch.setattr(alert_check, "_MM_MONITOR", mm, raising=False)
    monkeypatch.setattr(alert_check, "_ALERT_STATE", tmp_path / "alert_state.json")
    monkeypatch.setattr(alert_check, "_bot_running", lambda: True)
    return {
        "live": live,
        "monitor": monitor,
        "hedge": hedge,
        "mm": mm,
        "now": 1_786_000_000.0,
    }


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


def _hedge_row(now, **kw):
    """构造一条显式健康的 Lighter 对冲快照。"""
    row = {
        "ts": now,
        "interval": 30.0,
        "primary_size": "1",
        "hedge_size": "-1",
        "net_delta": "0",
        "action": "无需再平衡",
        "primary_read_ok": True,
        "hedge_read_ok": True,
        "primary_notional_exceeded": False,
        "rebalance_threshold_ratio": "0.02",
        "hedge_free_margin_ratio": "0.50",
        "min_hedge_free_margin_ratio": "0.20",
        "hedge_margin_error": None,
    }
    row.update(kw)
    return row


def _write_hedge(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _mm_row(now, **kw):
    """构造一条显式健康的 Lighter 做市心跳。"""
    row = {
        "ts": now,
        "market": "BTC",
        "dry_run": True,
        "levels": 4,
        "unit": 50.0,
        "max_inv": 500.0,
        "interval": 5.0,
        "position_size": "0",
        "inventory_usd": 0.0,
        "open_orders": 8,
        "success": True,
    }
    row.update(kw)
    return row


def _write_mm(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _keys(alerts):
    return {a.key for a in alerts}


def test_healthy_state_is_silent(env):
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(env["hedge"], [_hedge_row(env["now"])])
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


def test_attribution_gap_fires(env, tmp_path, monkeypatch):
    """归因残差超阈值时必须真的产生告警，并保留带符号金额。"""
    monkeypatch.setattr(alert_check, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "attribution.json").write_text(
        json.dumps({"should_stop": False, "has_gap": True, "residual": -1.04}),
        encoding="utf-8",
    )
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))

    alerts = alert_check.collect_alerts(env["now"])

    assert "attribution_gap" in _keys(alerts)
    body = next(alert.body for alert in alerts if alert.key == "attribution_gap")
    assert "-$1.04" in body


def test_verdict_stop_fires(env, tmp_path, monkeypatch):
    """满 4 周判据不通过时必须真的产生停止建议告警。"""
    monkeypatch.setattr(alert_check, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "attribution.json").write_text(
        json.dumps({"should_stop": True, "reason": "闭环年化 9.0% < 15%"}),
        encoding="utf-8",
    )
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))

    alerts = alert_check.collect_alerts(env["now"])

    assert "verdict_stop" in _keys(alerts)
    body = next(alert.body for alert in alerts if alert.key == "verdict_stop")
    assert "闭环年化 9.0% < 15%" in body


def test_invalid_attribution_shape_does_not_break_existing_alerts(
    env,
    tmp_path,
    monkeypatch,
):
    """合法 JSON 但顶层不是对象时，不能让全部告警检查崩溃。"""
    monkeypatch.setattr(alert_check, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "attribution.json").write_text("[]", encoding="utf-8")
    _write_live(env["live"], env["now"], halted=True)
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))

    alerts = alert_check.collect_alerts(env["now"])

    assert "halted" in _keys(alerts)


def test_leverage_just_under_threshold_is_silent(env):
    _write_live(env["live"], env["now"])
    rows = _healthy_monitor(env["now"])
    rows[-1]["inv_usd"] = -2000.0  # 权益 970 → 约 2.1x，低于 3x 阈值
    _write_monitor(env["monitor"], rows)
    assert "leverage" not in _keys(alert_check.collect_alerts(env["now"]))


def test_missing_files_fire_not_crash(env):
    """数据文件缺失时要报警，而不是抛异常把调度任务搞挂。"""
    alerts = alert_check.collect_alerts(env["now"])
    assert {"live_missing", "monitor_missing", "lighter_hedge_missing"} <= _keys(alerts)


def test_lighter_mm_missing_file_is_silent_before_deployment(env):
    """防止做市机器人尚未部署时因心跳文件不存在而持续制造噪音告警。"""
    assert not env["mm"].exists()

    assert alert_check._collect_lighter_mm_alerts(env["now"]) == []


@pytest.mark.parametrize("contents", ["", "不是 JSON\n[]\n"])
def test_lighter_mm_existing_file_without_valid_record_fires(env, contents):
    """防止已部署后的空文件或损坏文件被误判成尚未部署并静默放过。"""
    env["mm"].write_text(contents, encoding="utf-8")

    alerts = alert_check._collect_lighter_mm_alerts(env["now"])

    assert "lighter_mm_missing" in _keys(alerts)


def test_collect_alerts_includes_lighter_mm_failures(env):
    """防止做市收集函数虽存在却未接入总告警入口，继续形成静默盲区。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(env["hedge"], [_hedge_row(env["now"])])
    env["mm"].write_text("损坏心跳\n", encoding="utf-8")

    assert "lighter_mm_missing" in _keys(alert_check.collect_alerts(env["now"]))


def test_lighter_mm_stale_uses_three_heartbeat_intervals(env):
    """防止轮询参数调整后告警仍写死旧常量，导致停摆发现过晚或误报。"""
    _write_mm(
        env["mm"],
        [_mm_row(env["now"] - 31, interval=10.0)],
    )

    alerts = alert_check._collect_lighter_mm_alerts(env["now"])

    assert "lighter_mm_stale" in _keys(alerts)
    alert = next(item for item in alerts if item.key == "lighter_mm_stale")
    assert "做市心跳陈旧" in alert.title
    assert "31 秒" in alert.body
    assert "门槛 30 秒" in alert.body


@pytest.mark.parametrize("bad_interval", [None, 0, -2.5])
def test_lighter_mm_invalid_interval_falls_back_to_five_seconds(
    env,
    bad_interval,
):
    """防止缺失或非正 interval 让陈旧门槛失效，从而永久漏报做市停摆。"""
    row = _mm_row(env["now"] - 16)
    if bad_interval is None:
        row.pop("interval")
    else:
        row["interval"] = bad_interval
    _write_mm(env["mm"], [row])

    alerts = alert_check._collect_lighter_mm_alerts(env["now"])

    assert "lighter_mm_stale" in _keys(alerts)
    body = next(item.body for item in alerts if item.key == "lighter_mm_stale")
    assert "门槛 15 秒" in body


def test_lighter_mm_far_future_timestamp_is_not_fresh(env):
    """防止损坏的远未来时间戳让 age 为负，进而永久绕过做市心跳陈旧告警。"""
    future = env["now"] + alert_check._HEDGE_FUTURE_TOLERANCE_S + 1
    _write_mm(env["mm"], [_mm_row(future, interval=5.0)])

    alerts = alert_check._collect_lighter_mm_alerts(env["now"])

    assert "lighter_mm_stale" in _keys(alerts)


@pytest.mark.parametrize("failure_count", [3, 4])
def test_lighter_mm_three_or_more_consecutive_failures_fire(
    env,
    failure_count,
):
    """防止做市连续失败仍被活跃心跳伪装成健康，且告警轮数与事实不符。"""
    rows = [
        _mm_row(env["now"] - offset, success=False)
        for offset in reversed(range(failure_count))
    ]
    _write_mm(env["mm"], rows)

    alerts = alert_check._collect_lighter_mm_alerts(env["now"])

    assert "lighter_mm_failures" in _keys(alerts)
    body = next(item.body for item in alerts if item.key == "lighter_mm_failures")
    assert f"连续失败 {failure_count} 轮" in body


def test_lighter_mm_inventory_above_ninety_percent_fires(env):
    """防止带符号库存只比较正值，导致接近上限的空头库存完全不告警。"""
    _write_mm(
        env["mm"],
        [_mm_row(env["now"], inventory_usd=-451.0, max_inv=500.0)],
    )

    alerts = alert_check._collect_lighter_mm_alerts(env["now"])

    assert "lighter_mm_inventory" in _keys(alerts)
    alert = next(item for item in alerts if item.key == "lighter_mm_inventory")
    assert "库存逼近上限" in alert.title
    assert "$-451" in alert.body
    assert "$500" in alert.body


def test_lighter_mm_null_inventory_skips_only_inventory_check(env):
    """防止 inventory_usd=null 被当成 0 或抛错，并吞掉同批连续失败告警。"""
    rows = [
        _mm_row(
            env["now"] - offset,
            success=False,
            inventory_usd=None,
        )
        for offset in (2, 1, 0)
    ]
    _write_mm(env["mm"], rows)

    alerts = alert_check._collect_lighter_mm_alerts(env["now"])

    assert "lighter_mm_failures" in _keys(alerts)
    assert "lighter_mm_inventory" not in _keys(alerts)


def test_lighter_mm_inventory_at_exactly_ninety_percent_is_silent(env):
    """防止把“超过 90%”错写成“大于等于”，在精确边界制造库存误报。"""
    rows = [
        _mm_row(
            env["now"] - offset,
            success=False,
            inventory_usd=450.0,
            max_inv=500.0,
        )
        for offset in (2, 1, 0)
    ]
    _write_mm(env["mm"], rows)

    alerts = alert_check._collect_lighter_mm_alerts(env["now"])

    assert "lighter_mm_failures" in _keys(alerts)
    assert "lighter_mm_inventory" not in _keys(alerts)


def test_lighter_hedge_stale_uses_three_intervals(env):
    """默认 30 秒轮询超过 90 秒未写心跳，必须真的产生告警。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [_hedge_row(env["now"] - 91, interval=30.0)],
    )

    alerts = alert_check.collect_alerts(env["now"])

    assert "lighter_hedge_stale" in _keys(alerts)
    body = next(alert.body for alert in alerts if alert.key == "lighter_hedge_stale")
    assert "90" in body


def test_lighter_hedge_two_consecutive_net_delta_breaches_fire(env):
    """连续两轮 10% 净敞口超过 2% 阈值，必须告警。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [
            _hedge_row(
                env["now"] - 30,
                hedge_size="-0.9",
                net_delta="0.1",
            ),
            _hedge_row(
                env["now"],
                hedge_size="-0.9",
                net_delta="0.1",
            ),
        ],
    )

    alerts = alert_check.collect_alerts(env["now"])

    assert "lighter_hedge_net_delta" in _keys(alerts)
    body = next(
        alert.body for alert in alerts if alert.key == "lighter_hedge_net_delta"
    )
    assert "10.0%" in body


def test_lighter_hedge_single_net_delta_breach_does_not_fire(env):
    """前一轮已收敛、仅最新一轮偏离是再平衡中间态，不应过早告警。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [
            _hedge_row(env["now"] - 30),
            _hedge_row(
                env["now"],
                hedge_size="-0.9",
                net_delta="0.1",
            ),
        ],
    )

    assert "lighter_hedge_net_delta" not in _keys(
        alert_check.collect_alerts(env["now"])
    )


def test_lighter_hedge_primary_notional_cap_fires_immediately(env):
    """最新一轮门禁触发即要求人工去 RH Wallet 缩仓。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [
            _hedge_row(
                env["now"],
                action="primary 名义金额 2100 超过上限 2000",
                primary_notional_exceeded=True,
            )
        ],
    )

    alerts = alert_check.collect_alerts(env["now"])

    assert "lighter_hedge_notional_cap" in _keys(alerts)
    body = next(
        alert.body for alert in alerts if alert.key == "lighter_hedge_notional_cap"
    )
    assert "RH Wallet" in body


def test_lighter_hedge_three_consecutive_primary_failures_fire(env):
    """连续三轮读不到 Lighter 时必须告警，不能把停动仓伪装成安全。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    rows = [
        _hedge_row(
            env["now"] - offset,
            primary_size=None,
            net_delta=None,
            primary_read_ok=False,
            action="跳过本轮（primary 读取失败）",
        )
        for offset in (60, 30, 0)
    ]
    _write_hedge(env["hedge"], rows)

    alerts = alert_check.collect_alerts(env["now"])

    assert "lighter_hedge_primary_read" in _keys(alerts)
    body = next(
        alert.body for alert in alerts if alert.key == "lighter_hedge_primary_read"
    )
    assert "3" in body


def test_lighter_hedge_two_primary_failures_do_not_fire(env):
    """只有两轮读取失败时尚未达到明确的三轮门槛。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    rows = [
        _hedge_row(env["now"] - 60),
        _hedge_row(
            env["now"] - 30,
            primary_size=None,
            net_delta=None,
            primary_read_ok=False,
        ),
        _hedge_row(
            env["now"],
            primary_size=None,
            net_delta=None,
            primary_read_ok=False,
        ),
    ]
    _write_hedge(env["hedge"], rows)

    assert "lighter_hedge_primary_read" not in _keys(
        alert_check.collect_alerts(env["now"])
    )


def test_lighter_hedge_low_free_margin_fires(env):
    """Extended 可用保证金率 15% 低于 20% 阈值时必须告警。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [
            _hedge_row(
                env["now"],
                hedge_free_margin_ratio="0.15",
                min_hedge_free_margin_ratio="0.20",
            )
        ],
    )

    alerts = alert_check.collect_alerts(env["now"])

    assert "lighter_hedge_margin" in _keys(alerts)
    body = next(alert.body for alert in alerts if alert.key == "lighter_hedge_margin")
    assert "15.0%" in body
    assert "20.0%" in body


def test_lighter_hedge_net_delta_event_survives_until_scheduled_check(env):
    """两轮偏离后即使已收敛，也不能在 900 秒调度到来前丢失事件。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [
            _hedge_row(env["now"] - 330, hedge_size="-0.9", net_delta="0.1"),
            _hedge_row(env["now"] - 300, hedge_size="-0.9", net_delta="0.1"),
            _hedge_row(env["now"]),
        ],
    )

    assert "lighter_hedge_net_delta" in _keys(
        alert_check.collect_alerts(env["now"])
    )


def test_lighter_hedge_notional_event_survives_until_scheduled_check(env):
    """名义门禁事件不能被后续健康快照覆盖后漏报。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [
            _hedge_row(
                env["now"] - 300,
                action="primary 名义金额 2100 超过上限 2000",
                primary_notional_exceeded=True,
            ),
            _hedge_row(env["now"]),
        ],
    )

    assert "lighter_hedge_notional_cap" in _keys(
        alert_check.collect_alerts(env["now"])
    )


def test_lighter_hedge_primary_failure_event_survives_recovery(env):
    """三连读取失败后即使恢复，也必须让下一次定时检查看见事故。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    rows = [
        _hedge_row(
            env["now"] - offset,
            primary_size=None,
            net_delta=None,
            primary_read_ok=False,
        )
        for offset in (360, 330, 300)
    ]
    rows.append(_hedge_row(env["now"]))
    _write_hedge(env["hedge"], rows)

    assert "lighter_hedge_primary_read" in _keys(
        alert_check.collect_alerts(env["now"])
    )


def test_lighter_hedge_margin_event_survives_until_scheduled_check(env):
    """保证金曾跌破阈值时，后续恢复不能让当次风险静默消失。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [
            _hedge_row(
                env["now"] - 300,
                hedge_free_margin_ratio="0.15",
                min_hedge_free_margin_ratio="0.20",
            ),
            _hedge_row(env["now"]),
        ],
    )

    assert "lighter_hedge_margin" in _keys(
        alert_check.collect_alerts(env["now"])
    )


def test_lighter_hedge_missing_schema_fields_fire_invalid_snapshot(env):
    """API 或快照格式变化导致关键字段缺失时，必须告警而非 fail-open。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [{"ts": env["now"], "interval": 30.0}],
    )

    assert "lighter_hedge_invalid" in _keys(
        alert_check.collect_alerts(env["now"])
    )


def test_lighter_hedge_non_finite_decimal_fires_invalid_not_crash(env):
    """NaN 不能让净敞口比较抛异常并终止整次告警检查。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [
            _hedge_row(
                env["now"] - 30,
                primary_size="NaN",
                net_delta="NaN",
            ),
            _hedge_row(
                env["now"],
                primary_size="NaN",
                net_delta="NaN",
            ),
        ],
    )

    alerts = alert_check.collect_alerts(env["now"])

    assert "lighter_hedge_invalid" in _keys(alerts)


def test_lighter_hedge_non_finite_time_fires_invalid_and_stale(env):
    """NaN 时间或 Infinity 间隔不能把陈旧心跳伪装成健康。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [_hedge_row("NaN", interval="Infinity")],
    )

    alerts = alert_check.collect_alerts(env["now"])

    assert {"lighter_hedge_invalid", "lighter_hedge_stale"} <= _keys(alerts)


def test_lighter_hedge_infinite_interval_alone_fires_invalid_and_stale(env):
    """时间戳正常时，Infinity interval 也必须独立触发陈旧告警。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [_hedge_row(env["now"], interval="Infinity")],
    )

    alerts = alert_check.collect_alerts(env["now"])

    assert {"lighter_hedge_invalid", "lighter_hedge_stale"} <= _keys(alerts)


def test_lighter_hedge_far_future_timestamp_fires_invalid_and_stale(env):
    """远未来时间不能让 age 为负后永久绕过心跳陈旧判据。"""
    _write_live(env["live"], env["now"])
    _write_monitor(env["monitor"], _healthy_monitor(env["now"]))
    _write_hedge(
        env["hedge"],
        [_hedge_row(env["now"] + 1_000_000)],
    )

    alerts = alert_check.collect_alerts(env["now"])

    assert {"lighter_hedge_invalid", "lighter_hedge_stale"} <= _keys(alerts)


def test_notify_failure_does_not_raise(monkeypatch):
    def boom(*a, **kw):
        raise OSError("osascript 不可用")

    monkeypatch.setattr(alert_check.subprocess, "run", boom)
    assert alert_check.notify("标题", "内容") is False


def test_notify_nonzero_exit_is_failure(monkeypatch):
    """osascript 返回非零退出码时不能谎报发送成功。"""
    failed = subprocess.CompletedProcess(
        args=["osascript"],
        returncode=1,
        stdout="",
        stderr="通知权限被拒绝",
    )
    monkeypatch.setattr(alert_check.subprocess, "run", lambda *a, **kw: failed)

    assert alert_check.notify("标题", "内容") is False


def test_notification_failure_does_not_start_six_hour_cooldown(monkeypatch):
    """通知没有送达时不得写冷却，否则下一轮会继续静默。"""
    monkeypatch.setattr(
        alert_check,
        "collect_alerts",
        lambda _now: [alert_check.Alert("danger", "标题", "内容")],
    )
    monkeypatch.setattr(alert_check, "_load_cooldown", lambda: {})
    monkeypatch.setattr(alert_check, "notify", lambda _title, _body: False)
    saved = []
    monkeypatch.setattr(alert_check, "_save_cooldown", saved.append)
    monkeypatch.setattr(sys, "argv", ["alert_check"])

    with pytest.raises(SystemExit, match="1"):
        alert_check.main()

    assert saved == []


@pytest.mark.parametrize("bad_state", [[], "损坏", 42])
def test_load_cooldown_rejects_non_dict(monkeypatch, bad_state):
    """alert_state JSON 即使语法有效但类型错误，也必须回退为空状态。"""
    monkeypatch.setattr(alert_check, "_read_json", lambda _path: bad_state)

    assert alert_check._load_cooldown() == {}


@pytest.mark.parametrize("bad_last", ["损坏", "NaN", "Infinity", 1_786_900_000.0])
def test_invalid_or_future_cooldown_does_not_suppress_notification(
    monkeypatch,
    bad_last,
) -> None:
    """损坏、非有限或未来冷却时间不得压掉真实告警。"""
    now = 1_786_000_000.0
    monkeypatch.setattr(
        alert_check,
        "collect_alerts",
        lambda _now: [alert_check.Alert("danger", "标题", "内容")],
    )
    monkeypatch.setattr(alert_check, "_load_cooldown", lambda: {"danger": bad_last})
    notified = []
    monkeypatch.setattr(
        alert_check,
        "notify",
        lambda title, body: notified.append((title, body)) or True,
    )
    monkeypatch.setattr(alert_check, "_save_cooldown", lambda _state: None)
    monkeypatch.setattr(alert_check.time, "time", lambda: now)
    monkeypatch.setattr(sys, "argv", ["alert_check"])

    with pytest.raises(SystemExit, match="1"):
        alert_check.main()

    assert notified == [("标题", "内容")]
