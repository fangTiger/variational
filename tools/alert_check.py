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
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIVE = _ROOT / "data" / "grid_live.json"
_MONITOR = _ROOT / "data" / "grid_monitor.jsonl"
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
    return _read_json(_ALERT_STATE) or {}


def _save_cooldown(state: dict) -> None:
    _ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _ALERT_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_ALERT_STATE)


def notify(title: str, body: str) -> None:
    """弹 macOS 通知。失败不抛错——告警本身不该把调用方搞崩。"""
    script = (
        f'display notification {json.dumps(body)} '
        f'with title {json.dumps(title)} sound name "Basso"'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"  (通知发送失败：{exc})")


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
    for alert in alerts:
        last = float(state.get(alert.key) or 0)
        if not args.force and now - last < _COOLDOWN_S:
            print(f"  (冷却中，跳过) {alert.title}：{alert.body}")
            continue
        print(f"  {alert.title}：{alert.body}")
        if not args.dry_run:
            notify(alert.title, alert.body)
            state[alert.key] = now
        fired.append(alert)

    if fired and not args.dry_run:
        _save_cooldown(state)
    raise SystemExit(1 if fired else 0)


if __name__ == "__main__":
    main()
