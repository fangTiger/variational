"""实盘异常主动告警：检测到严重异常时弹 macOS 通知。

**为什么需要这个**：项目里检测逻辑早就有了（anchor-check 会查 halted、
快照陈旧、连通性），但结果只 `>>` 进 logs/anchor-check.log，没人看等于没有
告警。已经发生三次无声事故：

- 2026-08-01~03 引擎停摆 35.5 小时
- 2026-08-08 夜间断网 9.8 小时
- 2026-08-10~11 ADX 熔断导致 OFF 停摆 19.5 小时、库存被打成裸多头

三次都是靠人主动来问才发现。所以这里只做一件事：把已知的严重异常
推到通知中心。全只读，不碰账户、不下单。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.alert_check          # 有异常才弹窗
    PYTHONPATH=. .venv/bin/python -m tools.alert_check --dry-run # 只打印不弹窗
    PYTHONPATH=. .venv/bin/python -m tools.alert_check --force   # 忽略冷却期
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIVE = _ROOT / "data" / "grid_live.json"
_MONITOR = _ROOT / "data" / "grid_monitor.jsonl"
_HEDGE_MONITOR = _ROOT / "data" / "lighter_hedge.jsonl"
_ALERT_STATE = _ROOT / "data" / "alert_state.json"

# 同一条告警的静默期：避免每轮调度都弹同样的通知把人训练成无视通知
_COOLDOWN_S = 6 * 3600

# 引擎每 2.5 秒写一次 live 快照，10 分钟没动静就是卡死或进程没了
_LIVE_STALE_S = 600
# 监控每小时一条，2 小时没有就是 launchd 或网络出问题
_MONITOR_STALE_S = 2 * 3600
# 网格被封锁多久算"停摆"——OFF 短暂穿越是正常的，持续数小时不是
_BLOCKED_ALERT_S = 2 * 3600
# 多久没有一轮成功算"连不上交易所"。注意不能用快照时间判断：失败轮次同样会
# 更新 grid_live.json，只有 last_success_ts 才反映真实连通性。
# 2026-08-13 DNS 故障 3 小时、3686 个连接错误，而进程活着、快照在更新，
# 原有六条判据一条都没触发，告警全程静默。
_NO_SUCCESS_ALERT_S = 900
# 库存/权益超过这个倍数就该知会一声（满仓上限由 --max-inv 控制，这里只报警）
_LEVERAGE_WARN = 3.0

# Lighter 对冲默认每 30 秒一轮；实际心跳会携带 interval 并覆盖该兜底值。
_HEDGE_DEFAULT_INTERVAL_S = 30.0
# alert-check 每 900 秒运行一次；保留两个调度周期，避免严重异常在检查前恢复后漏报。
_HEDGE_EVENT_LOOKBACK_S = 30 * 60
# 容忍轻微时钟漂移；更远的未来时间必须视为损坏，不能让 age<0 绕过 stale。
_HEDGE_FUTURE_TOLERANCE_S = 60

_HEDGE_REQUIRED_FIELDS = frozenset(
    {
        "ts",
        "interval",
        "primary_size",
        "hedge_size",
        "net_delta",
        "action",
        "primary_read_ok",
        "hedge_read_ok",
        "primary_notional_exceeded",
        "rebalance_threshold_ratio",
        "hedge_free_margin_ratio",
        "min_hedge_free_margin_ratio",
        "hedge_margin_error",
    }
)


@dataclass
class Alert:
    """一条告警。key 用于冷却去重，title/body 用于通知展示。"""

    key: str
    title: str
    body: str


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  缺失或损坏都当作没有
        return None


def _read_last_monitor() -> dict | None:
    try:
        lines = [x for x in _MONITOR.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("equity"):
            return row
    return None


def _read_recent_jsonl(path: Path, limit: int) -> list[dict]:
    """流式读取最后若干条有效 JSON 对象，坏行不会阻断告警。"""
    rows: deque[dict] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001  部分写入或历史坏行直接跳过
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return list(rows)


def _decimal_field(row: dict, key: str) -> Decimal | None:
    """安全读取 Decimal 字段；缺失或非法值返回 None。"""
    value = row.get(key)
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _finite_float(value) -> float | None:
    """解析有限浮点数，拒绝会绕过比较的 NaN 与 Infinity。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _snapshot_validation_error(row: dict, now: float | None = None) -> str | None:
    """校验告警依赖的快照结构，防止格式变化静默绕过所有判据。"""
    missing = sorted(_HEDGE_REQUIRED_FIELDS - row.keys())
    if missing:
        return f"缺少字段：{', '.join(missing)}"

    timestamp = _finite_float(row.get("ts"))
    interval = _finite_float(row.get("interval"))
    if timestamp is None:
        return "ts 不是有限数"
    if now is not None and timestamp > now + _HEDGE_FUTURE_TOLERANCE_S:
        return "ts 超前当前时间超过 60 秒"
    if interval is None or interval <= 0:
        return "interval 不是正的有限数"

    for key in ("primary_read_ok", "hedge_read_ok", "primary_notional_exceeded"):
        if not isinstance(row.get(key), bool):
            return f"{key} 不是布尔值"
    if not isinstance(row.get("action"), str):
        return "action 不是字符串"

    threshold = _decimal_field(row, "rebalance_threshold_ratio")
    margin_threshold = _decimal_field(row, "min_hedge_free_margin_ratio")
    if threshold is None or threshold < 0:
        return "rebalance_threshold_ratio 无效"
    if margin_threshold is None or margin_threshold < 0:
        return "min_hedge_free_margin_ratio 无效"

    primary_size = _decimal_field(row, "primary_size")
    hedge_size = _decimal_field(row, "hedge_size")
    net_delta = _decimal_field(row, "net_delta")
    if row["primary_read_ok"] and primary_size is None:
        return "primary 读取成功但仓位无效"
    if row["hedge_read_ok"] and hedge_size is None:
        return "hedge 读取成功但仓位无效"
    if row["primary_read_ok"] and row["hedge_read_ok"] and net_delta is None:
        return "两腿读取成功但净敞口无效"

    margin_ratio = row.get("hedge_free_margin_ratio")
    margin_error = row.get("hedge_margin_error")
    if margin_ratio is None:
        if not isinstance(margin_error, str) or not margin_error:
            return "保证金率缺失且没有失败原因"
    elif _decimal_field(row, "hedge_free_margin_ratio") is None:
        return "hedge_free_margin_ratio 无效"
    return None


def _net_delta_status(row: dict) -> tuple[Decimal, Decimal, bool] | None:
    """返回（偏离比例、阈值、是否超限）；读数不完整时返回 None。"""
    if row.get("primary_read_ok") is not True:
        return None
    if row.get("hedge_read_ok") is not True:
        return None
    primary_size = _decimal_field(row, "primary_size")
    hedge_size = _decimal_field(row, "hedge_size")
    net_delta = _decimal_field(row, "net_delta")
    threshold = _decimal_field(row, "rebalance_threshold_ratio")
    if None in (primary_size, hedge_size, net_delta, threshold):
        return None
    single_leg = max(abs(primary_size), abs(hedge_size))
    ratio = abs(net_delta) / single_leg if single_leg > 0 else Decimal(0)
    return ratio, threshold, ratio > threshold


def _collect_lighter_hedge_alerts(now: float) -> list[Alert]:
    """从持久化快照判断 Lighter 对冲的五类严重异常。"""
    rows = _read_recent_jsonl(_HEDGE_MONITOR, limit=512)
    if not rows:
        return [
            Alert(
                "lighter_hedge_missing",
                "⛔ Lighter 对冲心跳缺失",
                "data/lighter_hedge.jsonl 没有有效记录，机器人可能尚未启动或已失联",
            )
        ]

    alerts: list[Alert] = []
    latest = rows[-1]
    latest_validation_error = _snapshot_validation_error(latest, now)
    parsed_latest_ts = _finite_float(latest.get("ts"))
    parsed_interval = _finite_float(latest.get("interval"))
    heartbeat_fields_valid = (
        parsed_latest_ts is not None
        and parsed_latest_ts <= now + _HEDGE_FUTURE_TOLERANCE_S
        and parsed_interval is not None
        and parsed_interval > 0
    )
    latest_ts = parsed_latest_ts if parsed_latest_ts is not None else 0
    interval = parsed_interval
    if interval is None or interval <= 0:
        interval = _HEDGE_DEFAULT_INTERVAL_S
    stale_after = 3 * interval
    age = now - latest_ts
    if not heartbeat_fields_valid or latest_ts <= 0 or age > stale_after:
        alerts.append(
            Alert(
                "lighter_hedge_stale",
                "⛔ Lighter 对冲心跳陈旧",
                f"已 {_fmt_age(max(age, 0))} 没有更新（门槛 {stale_after:.0f} 秒），"
                "机器人可能崩溃或卡死",
            )
        )

    # 保留两个 alert-check 调度周期内的事件。否则一次短暂但严重的超限，
    # 会在 900 秒定时检查到来前被后续健康快照覆盖，重新制造静默盲区。
    event_rows = []
    for row in rows:
        row_ts = _finite_float(row.get("ts"))
        if row_ts is None:
            continue
        if (
            now - _HEDGE_EVENT_LOOKBACK_S
            <= row_ts
            <= now + _HEDGE_FUTURE_TOLERANCE_S
        ):
            event_rows.append(row)

    invalid_error = latest_validation_error
    if invalid_error is None:
        for row in reversed(event_rows):
            invalid_error = _snapshot_validation_error(row, now)
            if invalid_error is not None:
                break
    if invalid_error is not None:
        alerts.append(
            Alert(
                "lighter_hedge_invalid",
                "⛔ Lighter 对冲快照无效",
                f"监控字段缺失或损坏：{invalid_error}；API 或快照格式可能已变化",
            )
        )

    # 单轮偏离通常是再平衡中间态；任意连续两轮都超阈值才喊人。
    net_delta_event = None
    for previous, current in zip(event_rows, event_rows[1:]):
        previous_status = _net_delta_status(previous)
        current_status = _net_delta_status(current)
        if (
            previous_status is not None
            and current_status is not None
            and previous_status[2]
            and current_status[2]
        ):
            net_delta_event = current_status
    if net_delta_event is not None:
        ratio, threshold, _ = net_delta_event
        alerts.append(
            Alert(
                "lighter_hedge_net_delta",
                "⛔ Lighter 对冲净敞口持续偏离",
                f"连续 2 轮未收敛，偏离 {ratio:.1%} > 阈值 {threshold:.1%}",
            )
        )

    notional_event = next(
        (
            row
            for row in reversed(event_rows)
            if row.get("primary_notional_exceeded") is True
        ),
        None,
    )
    if notional_event is not None:
        action = str(notional_event.get("action") or "primary 名义上限已触发")
        alerts.append(
            Alert(
                "lighter_hedge_notional_cap",
                "⛔ Lighter primary 名义超限",
                f"{action}；请立即到 RH Wallet 手动缩仓",
            )
        )

    failure_run = 0
    max_failure_run = 0
    for row in event_rows:
        if row.get("primary_read_ok") is False:
            failure_run += 1
            max_failure_run = max(max_failure_run, failure_run)
        else:
            failure_run = 0
    if max_failure_run >= 3:
        alerts.append(
            Alert(
                "lighter_hedge_primary_read",
                "⛔ Lighter primary 连续读取失败",
                f"曾连续 {max_failure_run} 轮读不到 Lighter，"
                "引擎已停止动仓以避免裸腿",
            )
        )

    margin_event = None
    for row in reversed(event_rows):
        free_margin_ratio = _decimal_field(row, "hedge_free_margin_ratio")
        min_free_margin_ratio = _decimal_field(
            row, "min_hedge_free_margin_ratio"
        )
        if (
            free_margin_ratio is not None
            and min_free_margin_ratio is not None
            and free_margin_ratio < min_free_margin_ratio
        ):
            margin_event = (free_margin_ratio, min_free_margin_ratio)
            break
    if margin_event is not None:
        free_margin_ratio, min_free_margin_ratio = margin_event
        alerts.append(
            Alert(
                "lighter_hedge_margin",
                "⛔ Extended 对冲腿保证金不足",
                f"可用保证金率 {free_margin_ratio:.1%} < 阈值 {min_free_margin_ratio:.1%}",
            )
        )
    return alerts


def _bot_running() -> bool:
    """只认 launchd 里的 grid-bot 有真实 PID，避免匹配到同名的等待脚本。"""
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001
        return True  # 查不出来就不误报
    for line in out.splitlines():
        if "com.variational.grid-bot" in line:
            pid = line.split("\t")[0].strip()
            return pid.isdigit()
    return False


def _fmt_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def collect_alerts(now: float | None = None) -> list[Alert]:
    """汇总当前所有严重异常。纯函数，便于测试。"""
    now = time.time() if now is None else now
    alerts: list[Alert] = []

    if not _bot_running():
        alerts.append(
            Alert("bot_down", "⛔ 网格引擎未运行", "launchd 里 grid-bot 没有 PID，实盘已停")
        )

    live = _read_json(_LIVE)
    if live is None:
        alerts.append(Alert("live_missing", "⛔ 引擎快照缺失", "data/grid_live.json 读不到"))
    else:
        age = now - float(live.get("ts") or 0)
        if age > _LIVE_STALE_S:
            alerts.append(
                Alert(
                    "live_stale",
                    "⛔ 引擎快照陈旧",
                    f"已 {_fmt_age(age)} 没有更新，引擎可能卡死",
                )
            )

        if live.get("halted"):
            alerts.append(Alert("halted", "⛔ 引擎已 halted", "硬止损触发，需人工介入"))

        # 连通性：进程活着 ≠ 连得上交易所
        last_success = live.get("last_success_ts")
        if isinstance(last_success, (int, float)) and last_success > 0:
            since_success = now - float(last_success)
            if since_success > _NO_SUCCESS_ALERT_S:
                fails = live.get("consecutive_failures") or 0
                alerts.append(
                    Alert(
                        "no_success",
                        "⛔ 连不上交易所",
                        f"已 {_fmt_age(since_success)} 没有一轮成功"
                        f"（连续失败 {fails} 轮），网格无法挂单成交",
                    )
                )

        # 停摆判据：mode=off 或双向封锁。这是 8/10 那次 19.5 小时停摆的盲区——
        # 当时 frozen=False，只看 frozen/halted 的旧检测完全没反应。
        blocked = live.get("effective_blocked_side")
        mode = str(live.get("mode") or "")
        if mode == "off" or blocked:
            since = _blocked_since(now)
            if since is not None and since > _BLOCKED_ALERT_S:
                alerts.append(
                    Alert(
                        "grid_blocked",
                        "⚠️ 网格已停摆",
                        f"mode={mode} blocked={blocked}，已持续 {_fmt_age(since)}，期间不铺新单",
                    )
                )

    monitor = _read_last_monitor()
    if monitor is None:
        alerts.append(Alert("monitor_missing", "⚠️ 监控无数据", "grid_monitor.jsonl 没有有效记录"))
    else:
        age = now - float(monitor.get("ts") or 0)
        if age > _MONITOR_STALE_S:
            alerts.append(
                Alert(
                    "monitor_stale",
                    "⚠️ 监控快照陈旧",
                    f"最新快照是 {_fmt_age(age)} 前，监控或网络异常",
                )
            )
        equity = float(monitor.get("equity") or 0)
        inv_usd = abs(float(monitor.get("inv_usd") or 0))
        if equity > 0 and inv_usd / equity > _LEVERAGE_WARN:
            alerts.append(
                Alert(
                    "leverage",
                    "⚠️ 库存杠杆偏高",
                    f"库存 ${inv_usd:.0f} / 权益 ${equity:.0f} = {inv_usd / equity:.1f}x",
                )
            )

    alerts.extend(_collect_lighter_hedge_alerts(now))

    # 4 周判据与恒等式残差（由 tools/pnl_attribution.py 每小时写入）。
    # 放在现有网格和 Lighter 检查之后，避免改变它们的判断路径。
    attribution = _read_json(_ROOT / "data" / "attribution.json")
    if isinstance(attribution, dict):
        if attribution.get("should_stop"):
            alerts.append(
                Alert(
                    "verdict_stop",
                    "⛔ 策略未达判据",
                    f"{attribution.get('reason', '')}；建议停",
                )
            )
        elif attribution.get("has_gap"):
            residual = _finite_float(attribution.get("residual"))
            if residual is None:
                residual_text = "未知"
            else:
                sign = "+" if residual >= 0 else "-"
                residual_text = f"{sign}${abs(residual):.2f}"
            alerts.append(
                Alert(
                    "attribution_gap",
                    "⚠️ 归因存在缺口",
                    f"残差 {residual_text}，数据不可信",
                )
            )
    return alerts


def _blocked_since(now: float) -> float | None:
    """从监控历史回推：当前这段连续封锁持续了多久。

    用每小时一条的 monitor 历史估算，精度 1 小时足够——我们要区分的是
    "OFF 穿越几分钟"和"OFF 卡了半天"，不需要秒级。
    """
    try:
        lines = [x for x in _MONITOR.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return None
    oldest_blocked_ts = None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not row.get("equity"):
            continue
        if str(row.get("mode") or "") == "off" or row.get("blocked_side"):
            oldest_blocked_ts = float(row.get("ts") or 0)
            continue
        break  # 遇到第一条正常记录，往前的不算这一段
    if oldest_blocked_ts is None:
        return None
    return now - oldest_blocked_ts


def _load_cooldown() -> dict:
    state = _read_json(_ALERT_STATE)
    return state if isinstance(state, dict) else {}


def _save_cooldown(state: dict) -> None:
    _ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _ALERT_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_ALERT_STATE)


def notify(title: str, body: str) -> bool:
    """弹 macOS 通知；返回是否真正发送成功，失败不抛错。"""
    script = (
        f'display notification {json.dumps(body)} '
        f'with title {json.dumps(title)} sound name "Basso"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  (通知发送失败：{exc})")
        return False
    if result.returncode != 0:
        error = (
            result.stderr.decode(errors="replace")
            if isinstance(result.stderr, bytes)
            else result.stderr
        )
        print(f"  (通知发送失败，osascript={result.returncode}：{error or '无错误信息'})")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="实盘异常主动告警")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不弹通知")
    parser.add_argument("--force", action="store_true", help="忽略冷却期")
    args = parser.parse_args()

    now = time.time()
    alerts = collect_alerts(now)
    if not alerts:
        print(f"[{time.strftime('%H:%M:%S')}] 无异常")
        return

    state = _load_cooldown()
    fired = []
    cooldown_changed = False
    for alert in alerts:
        last = _finite_float(state.get(alert.key))
        elapsed = now - last if last is not None else None
        if (
            not args.force
            and elapsed is not None
            and 0 <= elapsed < _COOLDOWN_S
        ):
            print(f"  (冷却中，跳过) {alert.title}：{alert.body}")
            continue
        print(f"  {alert.title}：{alert.body}")
        if not args.dry_run:
            if notify(alert.title, alert.body):
                state[alert.key] = now
                cooldown_changed = True
        fired.append(alert)

    if cooldown_changed:
        _save_cooldown(state)
    raise SystemExit(1 if fired else 0)


if __name__ == "__main__":
    main()
