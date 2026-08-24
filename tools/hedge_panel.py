"""定时定量对冲实时持仓的本地只读网页面板。"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STALE_AFTER_SECONDS = Decimal("120")
GOOD_EXPOSURE_LIMIT = Decimal("0.0001")
WARN_EXPOSURE_LIMIT = Decimal("0.001")
JSONL_READ_BLOCK_BYTES = 8192
PREVIOUS_READING_MAX_LINES = 200
POSITION_FIELDS = ("primary_size", "hedge_size", "net_exposure")


@dataclass(frozen=True)
class InstanceConfig:
    """一套定时定量对冲实例的只读数据路径。"""

    key: str
    name: str
    primary_exchange: str
    hedge_exchange: str
    heartbeat_path: Path
    state_path: Path
    lock_path: Path


@dataclass(frozen=True)
class PreviousReading:
    """心跳历史中的一条有效字段读数。"""

    value: object
    timestamp: Decimal | None


@dataclass(frozen=True)
class InstanceSnapshot:
    """一次页面渲染所使用的实例快照。"""

    config: InstanceConfig
    heartbeat: dict
    state: dict
    pid: int | None
    running: bool
    previous_readings: dict[str, PreviousReading]

    @property
    def data(self) -> dict:
        """以心跳优先合并持久化状态。"""
        combined = dict(self.state)
        combined.update(self.heartbeat)
        return combined


DEFAULT_INSTANCES = (
    InstanceConfig(
        key="lighter_entropy",
        name="实例 A · Lighter × Entropy",
        primary_exchange="Lighter",
        hedge_exchange="Entropy",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume.jsonl",
        state_path=PROJECT_ROOT / "data" / "timed_volume" / "state.json",
        lock_path=PROJECT_ROOT / "data" / "timed_volume" / "state.json.lock",
    ),
    InstanceConfig(
        key="variational_entropy",
        name="实例 B · Variational × Entropy",
        primary_exchange="Variational",
        hedge_exchange="Entropy",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_var.jsonl",
        state_path=PROJECT_ROOT / "data" / "timed_volume_var" / "state.json",
        lock_path=PROJECT_ROOT / "data" / "timed_volume_var" / "state.json.lock",
    ),
    InstanceConfig(
        key="lighter_variational_eth",
        name="实例 C · Lighter × Variational（ETH）",
        primary_exchange="Lighter",
        hedge_exchange="Variational",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_eth.jsonl",
        state_path=PROJECT_ROOT / "data" / "timed_volume_eth" / "state.json",
        lock_path=PROJECT_ROOT / "data" / "timed_volume_eth" / "state.json.lock",
    ),
)


def _load_json(text: str) -> object:
    """解析 JSON，并将带小数的数值直接保留为 Decimal。"""
    return json.loads(text, parse_float=Decimal)


def read_json_object(path: Path | str) -> dict:
    """读取 JSON 对象；文件缺失、损坏或结构异常时返回空字典。"""
    try:
        value = _load_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_last_jsonl(path: Path | str) -> dict:
    """读取 JSONL 的最后一个非空行；不可读时返回空字典。"""
    for line in _read_recent_jsonl_lines(path, max_lines=1):
        try:
            value = _load_json(line)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _read_recent_jsonl_lines(path: Path | str, *, max_lines: int) -> list[str]:
    """从文件末尾反向读取有限数量的非空 JSONL 文本行。"""
    if max_lines <= 0:
        return []

    recent: list[str] = []
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            remainder = b""

            while position > 0 and len(recent) < max_lines:
                block_size = min(JSONL_READ_BLOCK_BYTES, position)
                position -= block_size
                stream.seek(position)
                chunk = stream.read(block_size)
                parts = (chunk + remainder).split(b"\n")
                remainder = parts[0]

                for raw_line in reversed(parts[1:]):
                    if not raw_line.strip():
                        continue
                    recent.append(raw_line.decode("utf-8"))
                    if len(recent) == max_lines:
                        break

            if position == 0 and len(recent) < max_lines and remainder.strip():
                recent.append(remainder.decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return []
    return recent


def pid_is_alive(pid: int) -> bool:
    """检查 PID 是否存活；无权限探测时按存活处理。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_process_status(lock_path: Path | str) -> tuple[int | None, bool]:
    """从锁文件读取 PID，并以进程实际存活状态为准。"""
    payload = read_json_object(lock_path)
    raw_pid = payload.get("pid")
    if isinstance(raw_pid, bool):
        return None, False
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError, OverflowError):
        return None, False
    return pid, pid_is_alive(pid)


def collect_instance(config: InstanceConfig) -> InstanceSnapshot:
    """只读采集一套实例的心跳、状态和锁文件。"""
    pid, running = read_process_status(config.lock_path)
    heartbeat = read_last_jsonl(config.heartbeat_path)
    state = read_json_object(config.state_path)
    combined = dict(state)
    combined.update(heartbeat)
    needs_history = (
        str(combined.get("action", "")).strip().lower() == "position_read_failed"
        or any(_to_decimal(combined.get(field)) is None for field in POSITION_FIELDS)
    )
    return InstanceSnapshot(
        config=config,
        heartbeat=heartbeat,
        state=state,
        pid=pid,
        running=running,
        previous_readings=(
            read_previous_readings(config.heartbeat_path)
            if needs_history
            else {}
        ),
    )


def _to_decimal(value: object) -> Decimal | None:
    """安全转换普通数值，拒绝布尔值与非有限数。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def read_previous_readings(
    path: Path | str,
    *,
    max_lines: int = PREVIOUS_READING_MAX_LINES,
) -> dict[str, PreviousReading]:
    """在最近有限行中查找各持仓字段上一条有效读数。"""
    readings: dict[str, PreviousReading] = {}
    for line in _read_recent_jsonl_lines(path, max_lines=max_lines):
        try:
            payload = _load_json(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("action", "")).strip().lower() == "position_read_failed":
            continue

        timestamp = _to_decimal(payload.get("ts"))
        for field in POSITION_FIELDS:
            if field in readings:
                continue
            value = payload.get(field)
            if _to_decimal(value) is not None:
                readings[field] = PreviousReading(value=value, timestamp=timestamp)

        if len(readings) == len(POSITION_FIELDS):
            break
    return readings


def _truthy(value: object) -> bool:
    """兼容 JSON 布尔值与常见文本布尔值。"""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _decimal_text(value: object) -> str:
    """以十进制定点形式显示数值，不经过浮点数。"""
    parsed = _to_decimal(value)
    if parsed is None:
        return "—"
    if parsed == 0:
        return "0"
    return format(parsed, "f")


def _local_time(value: object) -> str:
    """按进程本地时区格式化 Unix 时间戳。"""
    timestamp = _to_decimal(value)
    if timestamp is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


def _countdown(due_at: object, now: Decimal) -> str:
    """把到期时间转换为中文倒计时。"""
    due = _to_decimal(due_at)
    if due is None:
        return "—"
    remaining = due - now
    if remaining <= 0:
        return "已到期"
    total_minutes = int(remaining / Decimal("60"))
    if total_minutes == 0:
        return "不足1分"
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分"


def exposure_class(value: object) -> str:
    """按净敞口绝对值返回绿、黄、红三级样式。"""
    exposure = _to_decimal(value)
    if exposure is None:
        return "exposure-missing"
    magnitude = abs(exposure)
    if magnitude < GOOD_EXPOSURE_LIMIT:
        return "exposure-good"
    if magnitude < WARN_EXPOSURE_LIMIT:
        return "exposure-warn"
    return "exposure-bad"


def _pnl_class(value: object) -> str:
    """按盈亏正负返回绿、红或中性样式。"""
    pnl = _to_decimal(value)
    if pnl is None:
        return "pnl-missing"
    if pnl > 0:
        return "pnl-positive"
    if pnl < 0:
        return "pnl-negative"
    return "pnl-flat"


def _money_text(value: object) -> str:
    """把盈亏格式化为带显式正负号的美元金额。"""
    pnl = _to_decimal(value)
    if pnl is None:
        return "—"
    sign = "-" if pnl < 0 else "+"
    return f"{sign}${abs(pnl):,.2f}"


def _field(data: dict, primary: str, *fallbacks: str) -> object:
    """返回首个存在且非空的字段。"""
    for key in (primary, *fallbacks):
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _text(value: object) -> str:
    """把动态内容转换为可安全嵌入 HTML 的文本。"""
    if value is None or value == "":
        return "—"
    return escape(str(value), quote=True)


def _direction(value: object) -> str:
    """把策略方向转换为中文。"""
    normalized = str(value).strip().lower() if value is not None else ""
    return {"long": "多", "short": "空"}.get(normalized, _text(value))


def _action(value: object) -> str:
    """把常见心跳动作转换为中文，同时保留未知动作。"""
    normalized = str(value).strip().lower() if value is not None else ""
    labels = {
        "opened": "已开仓",
        "closed": "已平仓",
        "opening": "开仓中",
        "closing": "平仓中",
        "waiting": "等待中",
        "skipped": "已跳过",
        "idle": "空闲",
        "position_read_failed": "持仓读取失败",
    }
    return labels.get(normalized, _text(value))


def _freshness(heartbeat: dict, now: Decimal) -> tuple[str, str, bool]:
    """返回数据时刻、相对时间文本及是否过期。"""
    timestamp = _to_decimal(heartbeat.get("ts"))
    if timestamp is None:
        return "—", "暂无心跳数据", False
    age = max(Decimal("0"), now - timestamp)
    seconds = int(age)
    return _local_time(timestamp), f"{seconds} 秒前", age > STALE_AFTER_SECONDS


def _heartbeat_is_fresh(heartbeat: dict, now: Decimal) -> bool:
    """判断心跳是否存在且仍在新鲜窗口内。"""
    timestamp = _to_decimal(heartbeat.get("ts"))
    if timestamp is None:
        return False
    return max(Decimal("0"), now - timestamp) <= STALE_AFTER_SECONDS


def _field_state(
    snapshot: InstanceSnapshot,
    field: str,
    now: Decimal,
) -> str:
    """返回持仓字段的有效、读取失败或心跳不可用三态。"""
    if not _heartbeat_is_fresh(snapshot.heartbeat, now):
        return "heartbeat-unavailable"
    action = str(snapshot.data.get("action", "")).strip().lower()
    if action == "position_read_failed" or _to_decimal(snapshot.data.get(field)) is None:
        return "read-failed"
    return "valid"


def _previous_reading_text(
    reading: PreviousReading | None,
    now: Decimal,
) -> str:
    """格式化上一次有效读数及其距当前的时间。"""
    if reading is None:
        return ""
    value_text = _decimal_text(reading.value)
    if reading.timestamp is None:
        return f"上次读数 {value_text}（时间未知）"
    age = max(Decimal("0"), now - reading.timestamp)
    return f"上次读数 {value_text}（{int(age)} 秒前）"


def _render_leg(
    role: str,
    exchange: object,
    size: object,
    notional: object,
    pnl: object,
    entry: object,
    *,
    read_failed: bool,
    previous_reading: PreviousReading | None,
    now: Decimal,
) -> str:
    """渲染单腿持仓：方向、数量、折合美元、盈亏与入场价。

    只显示数量的话看不出这条腿到底是多还是空、值多少钱，
    对冲是否成立要靠心算两个带符号小数，实际用起来很吃力。
    """
    parsed = _to_decimal(size)
    if read_failed:
        side_class, position_text = "leg-read-failed", "⚠ 读取失败"
    elif parsed is None:
        side, side_class, position_text = "", "leg-flat", "—"
    elif parsed > 0:
        side, side_class = "多", "leg-long"
        position_text = f"{side} {_decimal_text(size)}"
    elif parsed < 0:
        side, side_class = "空", "leg-short"
        position_text = f"{side} {_decimal_text(size)}"
    else:
        side, side_class = "空仓", "leg-flat"
        position_text = side

    # 名义额是策略给这一轮定的单边美元数，两腿共用，用它折算即可，
    # 避免面板进程为了取价而去连交易所。
    value = _to_decimal(notional)
    usd = (
        f"≈ {'-' if parsed < 0 else ''}${value:,.0f}"
        if not read_failed and value is not None and parsed not in (None, 0)
        else ""
    )
    pnl_class = _pnl_class(pnl)
    entry_value = _to_decimal(entry)
    entry_html = (
        f'      <small class="leg-entry mono">入场 {_text(_decimal_text(entry_value))}</small>\n'
        if entry_value is not None
        else ""
    )
    previous_text = _previous_reading_text(previous_reading, now)
    previous_html = (
        f'      <small class="previous-reading mono">{_text(previous_text)}</small>\n'
        if read_failed and previous_text
        else ""
    )

    return (
        f'    <div class="leg">\n'
        f"      <span>{_text(role)} · {_text(exchange)}</span>\n"
        f'      <strong class="mono {side_class}">{_text(position_text)}</strong>\n'
        f'      <em class="leg-usd">{_text(usd)}</em>\n'
        f"{previous_html}"
        f'      <div class="leg-pnl-row"><span>未实现盈亏</span><em class="leg-pnl mono {pnl_class}">{_text(_money_text(pnl))}</em></div>\n'
        f"{entry_html}"
        f"    </div>"
    )


def _render_pair_pnl(value: object) -> str:
    """渲染比单腿更醒目的本对合计盈亏。"""
    return (
        '  <section class="pair-pnl-block">\n'
        "    <span>本对盈亏</span>\n"
        f'    <strong class="pair-pnl-value mono {_pnl_class(value)}">{_text(_money_text(value))}</strong>\n'
        "    <small>两腿盈亏相互抵消，本对合计才是真实损益</small>\n"
        "  </section>"
    )


def _render_offset(primary: object, hedge: object) -> str:
    """渲染两腿是否互相抵消的一句话结论。"""
    a, b = _to_decimal(primary), _to_decimal(hedge)
    if a is None or b is None:
        return ""
    if a == 0 and b == 0:
        return '  <p class="offset offset-flat">两腿均为空仓</p>'
    if (a > 0) == (b > 0):
        return (
            '  <p class="offset offset-bad">⚠ 两腿方向相同，未形成对冲</p>'
        )
    return '  <p class="offset offset-ok">两腿方向相反，对冲成立</p>'


def _render_warnings(value: object) -> str:
    """仅在存在告警时渲染告警列表。"""
    if isinstance(value, (list, tuple)):
        warnings = [item for item in value if item not in (None, "")]
    elif value not in (None, ""):
        warnings = [value]
    else:
        warnings = []
    if not warnings:
        return ""
    items = "".join(f"<li>{_text(item)}</li>" for item in warnings)
    return f'<section class="warnings"><h3>告警</h3><ul>{items}</ul></section>'


def _render_instance(snapshot: InstanceSnapshot, now: Decimal) -> str:
    """渲染单个实例卡片。"""
    data = snapshot.data
    interlocked = _truthy(data.get("hedge_interlock_active"))
    primary_state = _field_state(snapshot, "primary_size", now)
    hedge_state = _field_state(snapshot, "hedge_size", now)
    net_state = _field_state(snapshot, "net_exposure", now)
    read_failed = "read-failed" in (primary_state, hedge_state, net_state)
    card_classes = ["instance-card"]
    if interlocked:
        card_classes.append("interlocked")
    if read_failed:
        card_classes.append("read-failed")
    card_class = " ".join(card_classes)
    time_text, age_text, stale = _freshness(snapshot.heartbeat, now)
    freshness_class = "data-time stale" if stale else "data-time"
    stale_warning = "<strong>⚠ 数据可能已过期</strong>" if stale else ""
    read_status_html = (
        '<span class="read-status">⚠ 持仓读取失败</span>'
        if read_failed
        else ""
    )

    if snapshot.running:
        status_class = "status running"
        status_text = f"运行中 · PID {snapshot.pid}"
    elif snapshot.pid is not None:
        status_class = "status stopped"
        status_text = f"未运行 · PID {snapshot.pid} 已退出"
    else:
        status_class = "status stopped"
        status_text = "未运行"

    direction = _field(data, "direction", "current_direction", "last_direction")
    round_index = _field(data, "round_index")
    notional = _field(data, "notional_usd", "current_notional_usd")
    due_at = _field(data, "due_at")
    primary_size = data.get("primary_size") if primary_state == "valid" else None
    hedge_size = data.get("hedge_size") if hedge_state == "valid" else None
    net_exposure = data.get("net_exposure") if net_state == "valid" else None
    if net_state == "read-failed":
        net_text = "⚠ 读取失败"
        net_class = "exposure-read-failed"
        previous_net_text = _previous_reading_text(
            snapshot.previous_readings.get("net_exposure"),
            now,
        )
        net_hint = previous_net_text or "未找到近期有效读数"
    else:
        net_text = _decimal_text(net_exposure)
        net_class = exposure_class(net_exposure)
        net_hint = "绝对值越接近 0 越好"
    interlock_reason = _field(data, "hedge_interlock_reason")

    if interlocked:
        interlock_html = (
            '<section class="interlock-alert">'
            '<strong>⛔ 互锁已激活</strong>'
            f'<span>{_text(interlock_reason)}</span>'
            "</section>"
        )
    else:
        availability = data.get("hedge_available")
        if availability is False:
            interlock_html = (
                '<section class="interlock-note unavailable">对冲侧不可用，互锁未激活</section>'
            )
        else:
            interlock_html = '<section class="interlock-note">互锁未激活</section>'

    return f"""
<article class="{card_class}" data-instance="{_text(snapshot.config.key)}">
  <div class="card-top">
    <div class="data-block">
      <div class="{freshness_class}">数据时间：<span class="mono">{time_text}</span>（{age_text}） {stale_warning}</div>
    </div>
    <div class="card-statuses">{read_status_html}<span class="{status_class}">{_text(status_text)}</span></div>
  </div>
  <div class="title-row">
    <div>
      <p class="eyebrow">定时定量对冲</p>
      <h2>{_text(snapshot.config.name)}</h2>
    </div>
    <div class="round">第 <span class="mono">{_text(round_index)}</span> 轮</div>
  </div>

  <section class="net-block">
    <span>净敞口</span>
    <strong class="net-value mono {net_class}">{_text(net_text)}</strong>
    <small class="{'previous-reading mono' if net_state == 'read-failed' else ''}">{_text(net_hint)}</small>
  </section>

  <section class="facts">
    <div><span>当前动作</span><strong>{_action(data.get("action"))}</strong></div>
    <div><span>方向</span><strong>{_direction(direction)}</strong></div>
    <div><span>本轮名义额</span><strong class="mono">{_text(_decimal_text(notional))} USD</strong></div>
    <div><span>距平仓</span><strong class="mono countdown">{_text(_countdown(due_at, now))}</strong></div>
  </section>

  <section class="legs" aria-label="两腿持仓">
{_render_leg("主腿", snapshot.config.primary_exchange, primary_size, notional, data.get("primary_pnl"), data.get("primary_entry"), read_failed=primary_state == "read-failed", previous_reading=snapshot.previous_readings.get("primary_size"), now=now)}
{_render_leg("对冲腿", snapshot.config.hedge_exchange, hedge_size, notional, data.get("hedge_pnl"), data.get("hedge_entry"), read_failed=hedge_state == "read-failed", previous_reading=snapshot.previous_readings.get("hedge_size"), now=now)}
  </section>
  {_render_pair_pnl(data.get("pair_pnl"))}
  {_render_offset(primary_size, hedge_size)}

  {interlock_html}
  {_render_warnings(data.get("warnings"))}
</article>"""


def build_page(
    *,
    instances: Iterable[InstanceConfig] = DEFAULT_INSTANCES,
    now: Decimal | int | str | None = None,
) -> str:
    """采集全部实例并渲染自包含深色 HTML。"""
    current = _to_decimal(now)
    if current is None:
        current = Decimal(str(time.time()))
    snapshots = tuple(collect_instance(config) for config in instances)

    exposures: list[Decimal] = []
    exposures_complete = bool(snapshots)
    for snapshot in snapshots:
        parsed = _to_decimal(snapshot.data.get("net_exposure"))
        if _field_state(snapshot, "net_exposure", current) != "valid" or parsed is None:
            exposures_complete = False
            break
        exposures.append(parsed)
    total_exposure = (
        sum(exposures, Decimal("0"))
        if exposures_complete
        else None
    )
    total_text = _decimal_text(total_exposure)
    total_class = exposure_class(total_exposure)
    pair_pnls = [
        parsed
        for snapshot in snapshots
        if (parsed := _to_decimal(snapshot.data.get("pair_pnl"))) is not None
    ]
    total_pair_pnl = (
        sum(pair_pnls, Decimal("0"))
        if snapshots and len(pair_pnls) == len(snapshots)
        else None
    )
    any_interlocked = any(
        _truthy(snapshot.data.get("hedge_interlock_active")) for snapshot in snapshots
    )
    interlock_summary_class = "summary-danger" if any_interlocked else "summary-ok"
    interlock_summary = "有互锁" if any_interlocked else "无互锁"
    cards = "".join(_render_instance(snapshot, current) for snapshot in snapshots)
    rendered_at = datetime.fromtimestamp(int(current)).strftime("%Y-%m-%d %H:%M:%S")

    css = """
      :root {
        color-scheme: dark;
        --bg: #080b10;
        --panel: #111821;
        --panel-2: #17212d;
        --line: #293748;
        --text: #edf4ff;
        --muted: #92a3b6;
        --green: #42d392;
        --yellow: #f6c453;
        --red: #ff5573;
        --blue: #63a8ff;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        background: radial-gradient(circle at 50% -20%, #182536 0, var(--bg) 46%);
        color: var(--text);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .mono, .net-value {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        font-variant-numeric: tabular-nums;
      }
      /* 放宽容器：三对及以上时要能一屏并排看完，不用横向或纵向滚动 */
      .shell { width: min(1720px, calc(100vw - 28px)); margin: 0 auto; padding: 22px 0 32px; }
      .page-head { margin-bottom: 16px; }
      h1, h2, h3, p { margin: 0; }
      h1 { font-size: clamp(22px, 2.6vw, 30px); line-height: 1.1; }
      h2 { margin-top: 4px; font-size: clamp(16px, 1.6vw, 20px); }
      .rendered-at { margin-top: 10px; color: var(--muted); }
      .manual-note { margin-top: 5px; color: var(--blue); font-size: 13px; }
      .overview {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 10px;
        margin: 12px 0;
      }
      .summary-item, .instance-card {
        background: rgba(17, 24, 33, 0.96);
        border: 1px solid var(--line);
        border-radius: 10px;
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
      }
      .summary-item { padding: 11px 14px; }
      .summary-item span { display: block; color: var(--muted); font-size: 12px; }
      .summary-item strong { display: block; margin-top: 4px; font-size: 19px; }
      .summary-ok { color: var(--green); }
      .summary-danger { color: var(--red); }
      /* 列数随宽度自适应：够宽就把所有实例排在同一行，不逼用户下拉。
         写死列数会在加第 N 对时突然折行——那正是这次要修的问题。 */
      .cards {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
        gap: 14px;
        align-items: start;
      }
      .instance-card { min-width: 0; padding: 15px; }
      .instance-card.read-failed:not(.interlocked) {
        border-color: rgba(246, 196, 83, 0.8);
        box-shadow: 0 0 0 1px rgba(246, 196, 83, 0.12), 0 18px 48px rgba(0, 0, 0, 0.28);
      }
      .instance-card.interlocked {
        border: 2px solid var(--red);
        box-shadow: 0 0 0 2px rgba(255, 85, 115, 0.18), 0 18px 52px rgba(255, 85, 115, 0.18);
      }
      .card-top, .title-row { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
      .card-top { min-height: 38px; margin-bottom: 13px; }
      .data-time { color: var(--text); font-size: 14px; font-weight: 700; }
      .data-time.stale { color: var(--red); }
      .data-time.stale strong { display: block; margin-top: 4px; }
      .card-statuses { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
      .status, .round {
        flex: 0 0 auto;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 6px 9px;
        font-size: 12px;
        white-space: nowrap;
      }
      .status.running { color: var(--green); border-color: rgba(66, 211, 146, 0.45); }
      .status.stopped { color: var(--muted); }
      .read-status {
        flex: 0 0 auto;
        color: var(--yellow);
        border: 1px solid rgba(246, 196, 83, 0.62);
        border-radius: 999px;
        padding: 6px 9px;
        font-size: 12px;
        white-space: nowrap;
        background: rgba(246, 196, 83, 0.1);
      }
      .eyebrow { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
      .round { color: var(--blue); }
      .net-block {
        display: flex;
        flex-direction: column;
        align-items: center;
        margin: 12px 0 10px;
        padding: 12px 10px;
        background: var(--panel-2);
        border: 1px solid var(--line);
        border-radius: 9px;
      }
      .net-block > span, .net-block small { color: var(--muted); }
      .net-value {
        max-width: 100%;
        margin: 4px 0;
        font-size: clamp(28px, 3.2vw, 40px);
        line-height: 1.08;
        overflow-wrap: anywhere;
      }
      .exposure-good { color: var(--green); }
      .exposure-warn { color: var(--yellow); }
      .exposure-bad { color: var(--red); }
      .exposure-missing { color: var(--muted); }
      .exposure-read-failed { color: var(--yellow); font-size: clamp(19px, 2.4vw, 27px); }
      .facts, .legs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
      .facts > div, .leg {
        min-width: 0;
        padding: 11px;
        background: var(--panel-2);
        border: 1px solid var(--line);
        border-radius: 8px;
      }
      .facts span, .leg span { display: block; margin-bottom: 5px; color: var(--muted); font-size: 12px; }
      .facts strong, .leg strong { overflow-wrap: anywhere; }
      .legs { margin-top: 7px; }
      .leg-long { color: var(--green); }
      .leg-short { color: var(--red); }
      .leg-flat { color: var(--muted); }
      .leg-read-failed { color: var(--yellow); }
      .leg-usd { display: block; margin-top: 3px; font-style: normal; font-size: 12px; color: var(--muted); }
      .previous-reading { display: block; margin-top: 5px; color: var(--yellow) !important; font-size: 12px; }
      .leg-pnl-row { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; margin-top: 9px; padding-top: 8px; border-top: 1px solid var(--line); }
      .leg-pnl-row span { margin: 0; }
      .leg-pnl { font-style: normal; font-size: 16px; font-weight: 800; }
      .leg-entry { display: block; margin-top: 4px; color: var(--muted); font-size: 12px; }
      .pnl-positive { color: var(--green); }
      .pnl-negative { color: var(--red); }
      .pnl-flat { color: var(--text); }
      .pnl-missing { color: var(--muted); }
      .pair-pnl-block {
        display: grid;
        grid-template-columns: auto 1fr;
        align-items: center;
        gap: 2px 14px;
        margin-top: 12px;
        padding: 14px 16px;
        background: linear-gradient(135deg, rgba(99, 168, 255, 0.14), rgba(23, 33, 45, 0.95));
        border: 1px solid rgba(99, 168, 255, 0.52);
        border-radius: 9px;
      }
      .pair-pnl-block > span { color: var(--text); font-size: 15px; font-weight: 800; }
      .pair-pnl-value { justify-self: end; font-size: clamp(27px, 4vw, 38px); line-height: 1; }
      .pair-pnl-block small { grid-column: 1 / -1; margin-top: 5px; color: var(--muted); }
      .offset { margin: 9px 0 0; font-size: 13px; }
      .offset-ok { color: var(--green); }
      .offset-bad { color: var(--red); font-weight: 600; }
      .offset-flat { color: var(--muted); }
      .leg strong { font-size: 21px; }
      .interlock-note, .interlock-alert {
        display: flex;
        flex-wrap: wrap;
        gap: 7px 12px;
        margin-top: 12px;
        padding: 10px 12px;
        border-radius: 8px;
      }
      .interlock-note { color: var(--green); background: rgba(66, 211, 146, 0.08); }
      .interlock-note.unavailable { color: var(--yellow); background: rgba(246, 196, 83, 0.1); }
      .interlock-alert { color: #ffdce3; background: rgba(255, 85, 115, 0.18); border: 1px solid var(--red); }
      .warnings { margin-top: 12px; padding: 12px; color: var(--yellow); background: rgba(246, 196, 83, 0.09); border-radius: 8px; }
      .warnings h3 { font-size: 14px; }
      .warnings ul { margin: 7px 0 0; padding-left: 20px; }
      .warnings li + li { margin-top: 5px; }
      @media (max-width: 1080px) {
        .cards { grid-template-columns: 1fr; }
        .overview { grid-template-columns: 1fr; }
      }
      @media (max-width: 520px) {
        .shell { width: min(100% - 20px, 1180px); padding-top: 18px; }
        .overview, .facts, .legs { grid-template-columns: 1fr; }
        .card-top, .title-row { flex-direction: column; }
        .status { white-space: normal; }
        .instance-card { padding: 14px; }
      }
    """

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>定时定量对冲持仓面板</title>
  <style>{css}</style>
</head>
<body>
  <main class="shell">
    <header class="page-head">
      <h1>定时定量对冲 · 实时持仓</h1>
      <p class="rendered-at">页面渲染时间：<span class="mono">{rendered_at}</span>（本地时间）</p>
      <p class="manual-note">本页不会自动刷新，请按 F5 获取最新数据。</p>
    </header>
    <section class="overview" aria-label="总览">
      <div class="summary-item">
        <span>{len(snapshots)} 个实例净敞口合计</span>
        <strong class="mono {total_class}">{_text(total_text)}</strong>
      </div>
      <div class="summary-item">
        <span>{len(snapshots)} 对合计盈亏</span>
        <strong class="mono {_pnl_class(total_pair_pnl)}">{_text(_money_text(total_pair_pnl))}</strong>
      </div>
      <div class="summary-item">
        <span>互锁总览</span>
        <strong class="{interlock_summary_class}">{interlock_summary}</strong>
      </div>
    </section>
    <section class="cards">{cards}</section>
  </main>
</body>
</html>"""


def render_html(
    instances: Iterable[InstanceConfig] = DEFAULT_INSTANCES,
    *,
    now: Decimal | int | str | None = None,
) -> str:
    """提供与其他本地面板一致的 HTML 渲染入口。"""
    return build_page(instances=instances, now=now)


class HedgePanelHandler(http.server.BaseHTTPRequestHandler):
    """定时定量对冲面板 HTTP handler。"""

    instances = DEFAULT_INSTANCES

    def do_GET(self) -> None:
        """只响应面板首页。"""
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return
        body = build_page(instances=self.instances).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        """关闭默认的英文访问日志。"""
        return


def main() -> None:
    """启动仅监听本机的只读面板。"""
    parser = argparse.ArgumentParser(description="启动定时定量对冲持仓面板")
    parser.add_argument("--port", type=int, default=8787, help="本地监听端口")
    args = parser.parse_args()

    with http.server.HTTPServer(("localhost", args.port), HedgePanelHandler) as server:
        print(f"对冲面板已启动：http://localhost:{args.port}（Ctrl+C 停止）", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
