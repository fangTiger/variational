"""定时定量对冲实时持仓的本地只读网页面板。"""

from __future__ import annotations

import argparse
import html
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
DEFAULT_PORTFOLIO_EQUITY_PATH = PROJECT_ROOT / "data" / "portfolio_equity.jsonl"
DEFAULT_PORTFOLIO_VOLUME_PATH = PROJECT_ROOT / "data" / "portfolio_volume.jsonl"
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
class PortfolioEquitySummary:
    """各账户在同一 schema 内分别做首末差后的组合统计。"""

    cumulative_pnl: Decimal | None = None
    started_at: Decimal | None = None
    first_equity: Decimal | None = None
    latest_equity: Decimal | None = None
    snapshot_count: int = 0
    computed_accounts: frozenset[str] = frozenset()

    @property
    def ready(self) -> bool:
        """至少两条有效组合快照才形成可解释的累计盈亏。"""
        return self.snapshot_count >= 2 and self.cumulative_pnl is not None


@dataclass(frozen=True)
class PortfolioVolumeSummary:
    """组合最新一条累计成交量快照的按币种统计。"""

    totals_by_symbol: dict[str, Decimal] | None = None
    estimated_symbols: frozenset[str] = frozenset()

    @property
    def ready(self) -> bool:
        """至少存在一个有效币种金额时才展示累计成交量。"""
        return bool(self.totals_by_symbol)


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
    #: 首条心跳时间戳，用于算已运行天数。取心跳而非进程启动时间，
    #: 这样重启不会把计数清零。
    first_seen_ts: Decimal | None = None

    @property
    def data(self) -> dict:
        """以心跳优先合并持久化状态。"""
        combined = dict(self.state)
        combined.update(self.heartbeat)
        return combined


#: ⚠️ 这份清单必须与实际启动参数的 --heartbeat-path / --state-path 一致。
#: 改了实例配对或标的却忘了改这里，面板会静默显示陈旧或空白数据——
#: 2026-08-25 就发生过：A/B 停用、C 从 ETH 改成 BTC 后面板仍指向旧路径。
DEFAULT_INSTANCES = (
    InstanceConfig(
        key="entropy_xyz_sndk",
        name="Entropy × XYZ（SNDK）",
        primary_exchange="Entropy",
        hedge_exchange="XYZ",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_sndk_xyz.jsonl",
        state_path=PROJECT_ROOT / "data" / "timed_volume_sndk_xyz" / "state.json",
        lock_path=(
            PROJECT_ROOT / "data" / "timed_volume_sndk_xyz" / "state.json.lock"
        ),
    ),
    InstanceConfig(
        key="lighter_variational_btc",
        name="Lighter × Variational（BTC）",
        primary_exchange="Lighter",
        hedge_exchange="Variational",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_btc.jsonl",
        state_path=PROJECT_ROOT / "data" / "timed_volume_btc" / "state.json",
        lock_path=PROJECT_ROOT / "data" / "timed_volume_btc" / "state.json.lock",
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


def read_portfolio_equity_summary(path: Path | str) -> PortfolioEquitySummary:
    """按最新 schema 为每个账户计算最新值减首条值，再做汇总。"""
    legacy_by_schema: dict[int, list[object]] = {}
    account_by_schema: dict[int, dict[str, object]] = {}
    latest_schema: int | None = None
    try:
        stream = Path(path).open("r", encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return PortfolioEquitySummary()

    with stream:
        try:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    payload = _load_json(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                timestamp = _to_decimal(payload.get("ts"))
                schema = _portfolio_equity_schema(payload.get("schema"))
                if timestamp is None or schema is None:
                    continue

                if schema >= 4:
                    raw_accounts = payload.get("accounts")
                    raw_sources = payload.get("sources")
                    if not isinstance(raw_accounts, dict) or not isinstance(
                        raw_sources, dict
                    ):
                        continue
                    parsed_accounts: dict[str, tuple[Decimal, str]] = {}
                    for raw_name, raw_value in raw_accounts.items():
                        if not isinstance(raw_name, str) or not raw_name.strip():
                            continue
                        name = raw_name.strip()
                        value = _to_decimal(raw_value)
                        source = raw_sources.get(raw_name)
                        if value is None or source not in {"platform", "computed"}:
                            continue
                        parsed_accounts[name] = (value, source)
                    if not parsed_accounts:
                        continue

                    state = account_by_schema.setdefault(
                        schema,
                        {"snapshot_count": 0, "accounts": {}},
                    )
                    state["snapshot_count"] = int(state["snapshot_count"]) + 1
                    account_states = state["accounts"]
                    if not isinstance(account_states, dict):
                        continue
                    for name, (value, source) in parsed_accounts.items():
                        account_state = account_states.get(name)
                        if not isinstance(account_state, list):
                            account_states[name] = [
                                timestamp,
                                value,
                                value,
                                1,
                                source,
                            ]
                            continue
                        account_state[2] = value
                        account_state[3] = int(account_state[3]) + 1
                        if source == "computed":
                            account_state[4] = source
                else:
                    total_equity = _to_decimal(payload.get("total_equity"))
                    if total_equity is None:
                        continue
                    snapshot = (timestamp, total_equity)
                    state = legacy_by_schema.setdefault(
                        schema,
                        [snapshot, snapshot, 0],
                    )
                    state[1] = snapshot
                    state[2] = int(state[2]) + 1
                latest_schema = schema
        except (OSError, UnicodeDecodeError):
            return PortfolioEquitySummary()

    if latest_schema is None:
        return PortfolioEquitySummary()

    if latest_schema >= 4:
        state = account_by_schema.get(latest_schema)
        if not isinstance(state, dict):
            return PortfolioEquitySummary()
        raw_count = int(state.get("snapshot_count", 0))
        raw_accounts = state.get("accounts")
        if not isinstance(raw_accounts, dict):
            return PortfolioEquitySummary(snapshot_count=raw_count)

        eligible: list[list[object]] = []
        computed_accounts: set[str] = set()
        for name, account_state in raw_accounts.items():
            if not isinstance(account_state, list) or len(account_state) != 5:
                continue
            if int(account_state[3]) < 2:
                continue
            eligible.append(account_state)
            if account_state[4] == "computed":
                computed_accounts.add(str(name))
        if not eligible:
            return PortfolioEquitySummary(snapshot_count=raw_count)

        first_equity = sum(
            (state[1] for state in eligible if isinstance(state[1], Decimal)),
            Decimal("0"),
        )
        latest_equity = sum(
            (state[2] for state in eligible if isinstance(state[2], Decimal)),
            Decimal("0"),
        )
        started_at = min(
            state[0] for state in eligible if isinstance(state[0], Decimal)
        )
        return PortfolioEquitySummary(
            cumulative_pnl=latest_equity - first_equity,
            started_at=started_at,
            first_equity=first_equity,
            latest_equity=latest_equity,
            snapshot_count=raw_count,
            computed_accounts=frozenset(computed_accounts),
        )

    first, latest, raw_count = legacy_by_schema[latest_schema]
    if not isinstance(first, tuple) or not isinstance(latest, tuple):
        return PortfolioEquitySummary()
    snapshot_count = int(raw_count)
    cumulative_pnl = latest[1] - first[1] if snapshot_count >= 2 else None
    return PortfolioEquitySummary(
        cumulative_pnl=cumulative_pnl,
        started_at=first[0],
        first_equity=first[1],
        latest_equity=latest[1],
        snapshot_count=snapshot_count,
    )


def _portfolio_equity_schema(value: object) -> int | None:
    """解析权益快照版本；旧记录缺少字段时按 schema 1 处理。"""
    if value is None:
        return 1
    if isinstance(value, bool):
        return None
    parsed = _to_decimal(value)
    if parsed is None or parsed <= 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def read_portfolio_volume_summary(path: Path | str) -> PortfolioVolumeSummary:
    """从最近的有效本地快照读取按币种官方成交量。"""
    for line in _read_recent_jsonl_lines(
        path,
        max_lines=PREVIOUS_READING_MAX_LINES,
    ):
        try:
            payload = _load_json(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        raw_totals = payload.get("totals_by_symbol")
        if not isinstance(raw_totals, dict) or not raw_totals:
            continue

        totals: dict[str, Decimal] = {}
        valid = True
        for raw_symbol, raw_amount in raw_totals.items():
            if not isinstance(raw_symbol, str) or not raw_symbol.strip():
                valid = False
                break
            symbol = raw_symbol.strip().upper()
            amount = _to_decimal(raw_amount)
            if amount is None or amount < 0:
                valid = False
                break
            totals[symbol] = amount
        if not valid or not totals:
            continue

        return PortfolioVolumeSummary(
            totals_by_symbol=totals,
        )
    return PortfolioVolumeSummary()


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
        first_seen_ts=read_first_heartbeat_ts(config.heartbeat_path),
    )


def read_first_heartbeat_ts(path: Path | str) -> Decimal | None:
    """读取心跳文件首行的时间戳；不可读或缺失时返回 None。

    只读第一行，不加载整个文件——心跳是逐轮追加的，长期运行后会很大。
    """
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    payload = _load_json(line)
                except json.JSONDecodeError:
                    return None
                if not isinstance(payload, dict):
                    return None
                return _to_decimal(payload.get("ts"))
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _run_days(first_ts: object, now: Decimal) -> Decimal | None:
    """由首条心跳时间算出已运行天数。"""
    start = _to_decimal(first_ts)
    if start is None:
        return None
    elapsed = now - start
    return elapsed / Decimal("86400") if elapsed >= 0 else None


def _run_days_text(first_ts: object, now: Decimal) -> str:
    """把运行时长渲染成中文，不足一天时用小时表示。"""
    days = _run_days(first_ts, now)
    if days is None:
        return "—"
    if days < 1:
        return f"{days * 24:.1f} 小时"
    return f"{days:.1f} 天"


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


def _equity_start_time(value: object) -> str:
    """把累计权益统计起点格式化为月日和时分。"""
    timestamp = _to_decimal(value)
    if timestamp is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%m-%d %H:%M")
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


def _equity_text(value: object) -> str:
    """把总权益格式化为不带盈亏符号的美元金额。"""
    equity = _to_decimal(value)
    if equity is None:
        return "—"
    return f"${equity:,.2f}"


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
        "    <small>当前这一轮两腿未实现盈亏合计；两腿盈亏相互抵消，本对合计才是真实损益</small>\n"
        "  </section>"
    )


def _render_portfolio_pnl(summary: PortfolioEquitySummary) -> str:
    """把组合级累计盈亏渲染为页面最醒目的主指标。"""
    if summary.ready:
        value_text = _money_text(summary.cumulative_pnl)
        value_class = _pnl_class(summary.cumulative_pnl)
        source_hint = (
            "平台官方口径与本地计算口径分开统计；本地计算口径："
            + "、".join(sorted(summary.computed_accounts))
            if summary.computed_accounts
            else "全部账户均使用平台官方口径"
        )
        hint = (
            f"自 {_equity_start_time(summary.started_at)} 起 · "
            f"各账户最新累计值减首条累计值后汇总 · {source_hint}"
        )
    else:
        value_text = "累计中…"
        value_class = "pnl-missing"
        hint = "等待至少两条组合权益快照"
    return (
        '  <section class="portfolio-pnl-block" aria-label="组合累计盈亏">\n'
        "    <span>累计盈亏</span>\n"
        f'    <strong class="portfolio-pnl-value mono {value_class}">{_text(value_text)}</strong>\n'
        f"    <small>{_text(hint)}</small>\n"
        "  </section>"
    )


def _render_portfolio_volume(summary: PortfolioVolumeSummary) -> str:
    """把按币种累计成交量渲染为仅次于累计盈亏的醒目指标。"""
    if not summary.ready or summary.totals_by_symbol is None:
        return (
            '  <section class="portfolio-volume-block" aria-label="组合累计成交量">\n'
            "    <span>累计成交量</span>\n"
            '    <strong class="portfolio-volume-value mono volume-missing">统计中…</strong>\n'
            "    <small>等待首条累计成交量快照</small>\n"
            "  </section>"
        )

    parts: list[str] = []
    for symbol in sorted(summary.totals_by_symbol):
        amount = summary.totals_by_symbol[symbol]
        parts.append(f"{symbol} ${amount:,.0f}")
    total = sum(summary.totals_by_symbol.values(), Decimal("0"))
    return (
        '  <section class="portfolio-volume-block" aria-label="组合累计成交量">\n'
        "    <span>累计成交量</span>\n"
        f'    <strong class="portfolio-volume-value mono">{_text(" · ".join(parts))}</strong>\n'
        f'    <b class="portfolio-volume-total mono">合计 ${total:,.0f}</b>\n'
        "    <small>全部金额均来自交易所成交记录</small>\n"
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

    run_days = _run_days_text(snapshot.first_seen_ts, now)
    if snapshot.running:
        status_class = "status running"
        status_text = f"运行中 · PID {snapshot.pid} · 已跑 {run_days}"
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
    net_status_class = (
        "net-status net-status-danger"
        if net_class == "exposure-bad"
        else "net-status"
    )
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

  <section class="{net_status_class}">
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


#: 强平距离配色阈值。io:SNDK 被交易所强制逐仓（strictIsolated），
#: 爆仓时**无法动用现货余额补保证金**，所以它的强平距离必须持续可见。
#: 实测 SNDK 在 12 小时窗口触及 4.8% 单向逆向波动的概率高达 20.9%，
#: 故 io 腿刻意用 5 倍而非上限 10 倍（强平距离约 15.8% 而非 4.8%）。
LIQUIDATION_DISTANCE_WARN_PCT = Decimal("12")
LIQUIDATION_DISTANCE_DANGER_PCT = Decimal("8")

#: 面板是同步渲染的，行情接口必须设超时，否则一个慢请求会卡住整页。
MARGIN_FETCH_TIMEOUT_SECONDS = 5.0


def _hl_info(body: dict, *, timeout: float = MARGIN_FETCH_TIMEOUT_SECONDS) -> object:
    """调用 Hyperliquid 公开只读接口。"""
    import json as _json
    import urllib.request

    request = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _json.loads(response.read())


def read_margin_snapshot(address: str, dexes: tuple[str, ...] = ("io", "xyz")) -> dict:
    """读取各 dex 持仓的保证金与强平距离，以及现货抵押余额。

    只读公开接口，仅凭公开账户地址即可，不涉及任何签名凭据。
    任何一步失败都返回 ``error`` 字段而不抛出——
    面板是只读监控，可用性优先，绝不能因为这一个区块把整页搞挂。
    """
    result: dict = {"legs": [], "spot": None, "error": None}
    try:
        # 本机 Python 缺少系统根证书时 HTTPS 会失败；与其它工具用同一套修复。
        from infra.runtime import ensure_ssl_cert

        ensure_ssl_cert()
        spot = _hl_info({"type": "spotClearinghouseState", "user": address})
        for balance in (spot or {}).get("balances", []):
            if balance.get("coin") == "USDC":
                total = _to_decimal(balance.get("total"))
                hold = _to_decimal(balance.get("hold")) or Decimal("0")
                if total is not None:
                    result["spot"] = {
                        "total": total,
                        "hold": hold,
                        "free": total - hold,
                    }
                break
        for dex in dexes:
            # 各 dex 的标记价互相独立（io 有专属 oracle，xyz 没有），实测系统性
            # 相差约 0.15%，而盘口中价只差约 0.02%。未实现盈亏按标记价算会被
            # 放大约 8 倍，但平仓是按盘口成交的，故同时取中价算预估平仓盈亏。
            mids: dict[str, Decimal] = {}
            try:
                meta = _hl_info({"type": "metaAndAssetCtxs", "dex": dex})
                for asset, ctx in zip(meta[0]["universe"], meta[1]):
                    mid = _to_decimal(ctx.get("midPx"))
                    if mid is not None:
                        mids[asset["name"]] = mid
            except Exception:  # noqa: BLE001 取不到中价只是少一列，不影响主体
                mids = {}
            state = _hl_info(
                {"type": "clearinghouseState", "user": address, "dex": dex}
            )
            for entry in (state or {}).get("assetPositions", []):
                position = entry.get("position") or {}
                size = _to_decimal(position.get("szi"))
                entry_px = _to_decimal(position.get("entryPx"))
                if size is None or not size:
                    continue
                liq = _to_decimal(position.get("liquidationPx"))
                distance = None
                if liq is not None and entry_px:
                    distance = (liq - entry_px) / entry_px * Decimal("100")
                leverage = position.get("leverage") or {}
                mid = mids.get(str(position.get("coin")))
                est = (
                    size * (mid - entry_px)
                    if mid is not None and entry_px is not None
                    else None
                )
                result["legs"].append(
                    {
                        "mid": mid,
                        "est_pnl": est,
                        "coin": position.get("coin"),
                        "size": size,
                        "entry": entry_px,
                        "margin": _to_decimal(position.get("marginUsed")),
                        "liquidation": liq,
                        "distance": distance,
                        "leverage_value": leverage.get("value"),
                        "leverage_type": leverage.get("type"),
                        "upnl": _to_decimal(position.get("unrealizedPnl")),
                    }
                )
    except Exception as exc:  # noqa: BLE001 监控区块失败不得影响整页
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _signed(value: object) -> str:
    """带符号渲染金额；缺失时显示破折号。"""
    parsed = _to_decimal(value)
    return "—" if parsed is None else f"{parsed:+.2f}"


def _distance_class(distance: object) -> str:
    """按强平距离绝对值给出配色档位。"""
    value = _to_decimal(distance)
    if value is None:
        return "margin-normal"
    magnitude = abs(value)
    if magnitude < LIQUIDATION_DISTANCE_DANGER_PCT:
        return "margin-danger"
    if magnitude < LIQUIDATION_DISTANCE_WARN_PCT:
        return "margin-warn"
    return "margin-normal"


def _render_margin(snapshot: dict) -> str:
    """渲染保证金与强平距离区块。"""
    if snapshot.get("error"):
        body = (
            '      <p class="margin-error">读取失败：'
            f"{html.escape(str(snapshot['error']))}</p>\n"
        )
    elif not snapshot.get("legs"):
        body = '      <p class="margin-empty">当前无持仓</p>\n'
    else:
        rows = []
        total_upnl = Decimal("0")
        total_est = Decimal("0")
        est_complete = True
        for leg in snapshot["legs"]:
            upnl = leg.get("upnl")
            if upnl is not None:
                total_upnl += upnl
            if leg.get("est_pnl") is None:
                est_complete = False
            else:
                total_est += leg["est_pnl"]
            size = leg.get("size") or Decimal("0")
            side = "多" if size > 0 else "空"
            lev_type = leg.get("leverage_type")
            lev_label = {"isolated": "逐仓", "cross": "全仓"}.get(
                str(lev_type), str(lev_type or "—")
            )
            distance = leg.get("distance")
            dist_text = f"{distance:+.1f}%" if distance is not None else "—"
            rows.append(
                "        <tr>\n"
                f"          <td>{html.escape(str(leg.get('coin') or '—'))}</td>\n"
                f"          <td>{side} {abs(size)}</td>\n"
                f"          <td>{html.escape(str(leg.get('leverage_value') or '—'))}x"
                f"（{lev_label}）</td>\n"
                f"          <td class=\"mono\">{leg.get('margin') or '—'}</td>\n"
                f"          <td class=\"mono\">{leg.get('liquidation') or '—'}</td>\n"
                f"          <td class=\"mono {_distance_class(distance)}\">"
                f"{dist_text}</td>\n"
                f"          <td class=\"mono\">{_signed(leg.get('upnl'))}</td>\n"
                f"          <td class=\"mono\">{_signed(leg.get('est_pnl'))}</td>\n"
                "        </tr>\n"
            )
        totals = (
            "        <tr class=\"margin-total\">\n"
            "          <td colspan=\"6\">两腿合计</td>\n"
            f"          <td class=\"mono\">{_signed(total_upnl)}</td>\n"
            f"          <td class=\"mono\">"
            f"{_signed(total_est) if est_complete else '—'}</td>\n"
            "        </tr>\n"
        )
        body = (
            '      <table class="margin-table">\n'
            "        <tr><th>标的</th><th>持仓</th><th>杠杆</th>"
            "<th>占用保证金</th><th>强平价</th><th>强平距离</th>"
            "<th>未实现（标记价）</th><th>预估平仓（盘口）</th></tr>\n"
            + "".join(rows)
            + totals
            + "      </table>\n"
        )
    spot = snapshot.get("spot")
    spot_line = ""
    if spot:
        spot_line = (
            '      <p class="margin-spot">抵押余额 '
            f"总 {spot['total']} · 占用 {spot['hold']} · "
            f"可用 {spot['free']}</p>\n"
        )
    return (
        '  <section class="margin-block" aria-label="保证金与强平距离">\n'
        "    <h3>保证金与强平距离</h3>\n"
        f"{body}{spot_line}"
        "    <small>强平距离绝对值低于 12% 转警示、低于 8% 转危险。"
        "io 腿被交易所强制逐仓，爆仓时无法动用现货余额补保证金。<br>"
        "两个 dex 的标记价互相独立，实测系统性相差约 0.15%，"
        "而盘口中价只差约 0.02%——所以「未实现（标记价）」会把浮亏放大约 8 倍，"
        "<b>实际平仓成本以「预估平仓（盘口）」为准</b>。</small>\n"
        "  </section>\n"
    )


def read_ledger_realized(instances: Iterable[InstanceConfig]) -> list[dict]:
    """从各实例的轮次台账汇总【已实现】磨损。

    ⚠️ 这与页面上的「组合累计盈亏」口径不同，务必区分：
    - 台账口径：只记策略完成的轮次，是**策略本身的真实成本**
    - 账户口径：平台累计盈亏，**含未实现浮动，也含手工干预与测试单**

    2026-08-31 对账发现两者相差 $9.7，全部来自未平仓浮动，而浮动又被各平台
    互不一致的标记价放大（io 标记价长期低于自身盘口约 0.4%）。
    只看账户口径会高估策略磨损。
    """
    results: list[dict] = []
    for config in instances:
        ledger = config.heartbeat_path.with_name(
            config.heartbeat_path.stem + "_ledger.jsonl"
        )
        realized = Decimal("0")
        volume = Decimal("0")
        rounds = 0
        try:
            text = ledger.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = _load_json(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            pnl = _to_decimal(row.get("realized_pnl"))
            notional = _to_decimal(row.get("notional_usd"))
            if pnl is None or notional is None:
                continue
            realized += pnl
            volume += notional * 2   # 单侧成交量 = 开 + 平
            rounds += 1
        if rounds:
            results.append(
                {
                    "name": config.name,
                    "rounds": rounds,
                    "realized": realized,
                    "volume": volume,
                    "per_10k": -realized / volume * Decimal("10000"),
                }
            )
    return results


def _render_ledger_realized(items: list[dict]) -> str:
    """渲染台账口径的已实现磨损。"""
    if not items:
        return ""
    rows = []
    total_pnl = Decimal("0")
    total_vol = Decimal("0")
    total_rounds = 0
    for item in items:
        total_pnl += item["realized"]
        total_vol += item["volume"]
        total_rounds += item["rounds"]
        rows.append(
            "        <tr>\n"
            f"          <td>{html.escape(item['name'])}</td>\n"
            f"          <td class=\"mono\">{item['rounds']}</td>\n"
            f"          <td class=\"mono\">{item['realized']:+.2f}</td>\n"
            f"          <td class=\"mono\">{item['volume']:,.0f}</td>\n"
            f"          <td class=\"mono\">{item['per_10k']:.2f}</td>\n"
            "        </tr>\n"
        )
    overall = (
        -total_pnl / total_vol * Decimal("10000") if total_vol else Decimal("0")
    )
    rows.append(
        "        <tr class=\"margin-total\">\n"
        "          <td>合计</td>\n"
        f"          <td class=\"mono\">{total_rounds}</td>\n"
        f"          <td class=\"mono\">{total_pnl:+.2f}</td>\n"
        f"          <td class=\"mono\">{total_vol:,.0f}</td>\n"
        f"          <td class=\"mono\">{overall:.2f}</td>\n"
        "        </tr>\n"
    )
    return (
        '  <section class="margin-block" aria-label="策略已实现磨损">\n'
        "    <h3>策略已实现磨损（轮次台账口径）</h3>\n"
        '      <table class="margin-table">\n'
        "        <tr><th>对</th><th>轮次</th><th>已实现盈亏</th>"
        "<th>单侧成交量</th><th>每万元磨损</th></tr>\n"
        + "".join(rows)
        + "      </table>\n"
        "    <small>只统计已平仓轮次，是策略本身的真实成本。"
        "与上方「组合累计盈亏」口径不同——后者是账户级，"
        "含未实现浮动、手工干预与测试单，且受各平台标记价差异影响。</small>\n"
        "  </section>\n"
    )


def build_page(
    *,
    instances: Iterable[InstanceConfig] = DEFAULT_INSTANCES,
    portfolio_equity_path: Path | str = DEFAULT_PORTFOLIO_EQUITY_PATH,
    portfolio_volume_path: Path | str = DEFAULT_PORTFOLIO_VOLUME_PATH,
    now: Decimal | int | str | None = None,
    margin_snapshot: dict | None = None,
) -> str:
    """采集全部实例并渲染自包含深色 HTML。

    ``margin_snapshot`` 由调用方提供（见 ``read_margin_snapshot``）。
    刻意不在此处发网络请求——``build_page`` 必须保持纯函数，
    否则测试会真的去打行情接口，既慢又不确定。
    """
    current = _to_decimal(now)
    if current is None:
        current = Decimal(str(time.time()))
    configs = tuple(instances)
    snapshots = tuple(collect_instance(config) for config in configs)

    # 累计运行按「实例·天」求和：三个实例各跑一天，等于累计三天的刷量时长。
    run_day_values = [
        days
        for snapshot in snapshots
        if (days := _run_days(snapshot.first_seen_ts, current)) is not None
    ]
    total_run_days_text = (
        f"{sum(run_day_values):.1f} 实例·天" if run_day_values else "—"
    )

    exposures: list[Decimal] = []
    exposures_complete = bool(snapshots)
    any_bad_exposure = False
    for snapshot in snapshots:
        parsed = _to_decimal(snapshot.data.get("net_exposure"))
        if _field_state(snapshot, "net_exposure", current) != "valid" or parsed is None:
            exposures_complete = False
            continue
        exposures.append(parsed)
        if exposure_class(parsed) == "exposure-bad":
            any_bad_exposure = True
    total_exposure = (
        sum(exposures, Decimal("0"))
        if exposures_complete
        else None
    )
    total_text = _decimal_text(total_exposure)
    total_class = exposure_class(total_exposure)
    exposure_summary_class = (
        "exposure-summary exposure-summary-danger"
        if any_bad_exposure
        else "exposure-summary exposure-summary-normal"
    )
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
    portfolio_summary = read_portfolio_equity_summary(portfolio_equity_path)
    portfolio_volume_summary = read_portfolio_volume_summary(portfolio_volume_path)
    any_interlocked = any(
        _truthy(snapshot.data.get("hedge_interlock_active")) for snapshot in snapshots
    )
    interlock_summary_class = "summary-danger" if any_interlocked else "summary-ok"
    interlock_summary = "有互锁" if any_interlocked else "无互锁"
    cards = "".join(_render_instance(snapshot, current) for snapshot in snapshots)
    margin_block = (
        _render_margin(margin_snapshot) if margin_snapshot is not None else ""
    )
    ledger_block = _render_ledger_realized(read_ledger_realized(configs))
    portfolio_pnl = _render_portfolio_pnl(portfolio_summary)
    portfolio_volume = _render_portfolio_volume(portfolio_volume_summary)
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
      .margin-block { margin-top: 14px; padding: 14px 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }
      .margin-block h3 { font-size: 15px; margin-bottom: 8px; }
      .margin-table { width: 100%; border-collapse: collapse; font-size: 13px; }
      .margin-table th { text-align: left; color: var(--muted); font-weight: 500; padding: 4px 8px 6px 0; border-bottom: 1px solid var(--line); }
      .margin-table td { padding: 6px 8px 6px 0; border-bottom: 1px solid var(--panel-2); }
      .margin-total td { border-top: 1px solid var(--line); border-bottom: none; color: var(--muted); font-weight: 600; }
      .margin-normal { color: var(--green); }
      .margin-warn { color: var(--yellow); font-weight: 600; }
      .margin-danger { color: var(--red); font-weight: 700; }
      .margin-error { color: var(--yellow); font-size: 13px; }
      .margin-empty { color: var(--muted); font-size: 13px; }
      .margin-spot { color: var(--muted); font-size: 12px; margin-top: 8px; }
      .margin-block small { display: block; margin-top: 8px; color: var(--muted); font-size: 11px; line-height: 1.5; }
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
      .portfolio-pnl-block {
        display: flex;
        flex-direction: column;
        align-items: center;
        margin: 16px 0 12px;
        padding: 22px 18px 20px;
        background: linear-gradient(135deg, rgba(99, 168, 255, 0.14), rgba(17, 24, 33, 0.98));
        border: 1px solid rgba(99, 168, 255, 0.55);
        border-radius: 12px;
        box-shadow: 0 22px 60px rgba(0, 0, 0, 0.34);
      }
      .portfolio-pnl-block > span { color: var(--text); font-size: 17px; font-weight: 800; }
      .portfolio-pnl-value {
        margin: 8px 0 7px;
        font-size: clamp(52px, 8vw, 86px);
        line-height: 0.98;
        letter-spacing: -0.045em;
      }
      .portfolio-pnl-block small { color: var(--muted); font-size: 13px; }
      .portfolio-volume-block {
        display: grid;
        grid-template-columns: auto 1fr auto;
        align-items: center;
        gap: 5px 18px;
        margin: 12px 0;
        padding: 16px 18px;
        background: linear-gradient(135deg, rgba(66, 211, 146, 0.11), rgba(17, 24, 33, 0.98));
        border: 1px solid rgba(66, 211, 146, 0.48);
        border-radius: 11px;
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
      }
      .portfolio-volume-block > span { font-size: 16px; font-weight: 800; }
      .portfolio-volume-value { font-size: clamp(22px, 3.3vw, 36px); text-align: center; }
      .portfolio-volume-total { font-size: clamp(18px, 2.3vw, 28px); color: var(--green); }
      .portfolio-volume-block small { grid-column: 1 / -1; color: var(--muted); font-size: 12px; }
      .volume-missing { color: var(--muted); }
      .overview {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 10px;
        margin: 12px 0;
      }
      .summary-item, .exposure-summary, .instance-card {
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
      .exposure-summary { padding: 10px 14px; }
      .exposure-summary span, .exposure-summary strong { display: block; }
      .exposure-summary-normal { color: var(--muted); font-size: 12px; }
      .exposure-summary-normal strong { margin-top: 3px; color: var(--muted); font-size: 14px; font-weight: 600; }
      .exposure-summary-danger {
        color: var(--red);
        border-color: var(--red);
        background: rgba(255, 85, 115, 0.14);
        box-shadow: 0 0 0 2px rgba(255, 85, 115, 0.14), 0 18px 48px rgba(0, 0, 0, 0.28);
      }
      .exposure-summary-danger span { font-size: 15px; font-weight: 800; }
      .exposure-summary-danger strong { margin-top: 4px; color: var(--red); font-size: clamp(28px, 4vw, 42px); }
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
      .net-status {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 5px 9px;
        margin: 12px 0 10px;
        padding: 7px 10px;
        background: var(--panel-2);
        border: 1px solid var(--line);
        border-radius: 9px;
        color: var(--muted);
        font-size: 12px;
      }
      .net-status > span::after { content: "："; }
      .net-status small { margin-left: auto; color: var(--muted); }
      .net-value {
        max-width: 100%;
        color: var(--muted);
        font-size: 13px;
        line-height: 1.2;
        overflow-wrap: anywhere;
      }
      .exposure-good { color: var(--green); }
      .exposure-warn { color: var(--yellow); }
      .exposure-bad { color: var(--red); }
      .exposure-missing { color: var(--muted); }
      .net-status:not(.net-status-danger) .net-value { color: var(--muted); }
      .net-status .exposure-read-failed { color: var(--yellow); }
      .net-status-danger {
        padding: 12px;
        color: var(--red);
        border-color: var(--red);
        background: rgba(255, 85, 115, 0.15);
      }
      .net-status-danger > span { font-size: 15px; font-weight: 800; }
      .net-status-danger .net-value { color: var(--red); font-size: clamp(28px, 3.2vw, 40px); }
      .net-status-danger small { color: #ffc0cc; }
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
      .pair-pnl-value { justify-self: end; font-size: clamp(23px, 3vw, 34px); line-height: 1; }
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
        .portfolio-volume-block { grid-template-columns: 1fr; }
        .portfolio-volume-value { text-align: left; }
        .portfolio-volume-block small { grid-column: auto; }
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
    {portfolio_pnl}
    {portfolio_volume}
    {ledger_block}
    {margin_block}
    <section class="overview" aria-label="总览">
      <div class="{exposure_summary_class}">
        <span>{len(snapshots)} 个实例净敞口合计</span>
        <strong class="mono {total_class}">{_text(total_text)}</strong>
      </div>
      <div class="summary-item">
        <span>{len(snapshots)} 对合计盈亏</span>
        <strong class="mono {_pnl_class(total_pair_pnl)}">{_text(_money_text(total_pair_pnl))}</strong>
      </div>
      <div class="summary-item">
        <span>累计运行</span>
        <strong class="mono">{_text(total_run_days_text)}</strong>
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
    portfolio_equity_path: Path | str = DEFAULT_PORTFOLIO_EQUITY_PATH,
    portfolio_volume_path: Path | str = DEFAULT_PORTFOLIO_VOLUME_PATH,
    now: Decimal | int | str | None = None,
) -> str:
    """提供与其他本地面板一致的 HTML 渲染入口。"""
    return build_page(
        instances=instances,
        portfolio_equity_path=portfolio_equity_path,
        portfolio_volume_path=portfolio_volume_path,
        now=now,
    )


class HedgePanelHandler(http.server.BaseHTTPRequestHandler):
    """定时定量对冲面板 HTTP handler。"""

    instances = DEFAULT_INSTANCES
    portfolio_equity_path = DEFAULT_PORTFOLIO_EQUITY_PATH
    portfolio_volume_path = DEFAULT_PORTFOLIO_VOLUME_PATH
    #: 公开账户地址，由 --margin-address 注入；留空则不渲染保证金区块。
    margin_address = ""

    def do_GET(self) -> None:
        """只响应面板首页。"""
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return
        # 只有真实服务时才去打行情接口；build_page 本身保持纯函数。
        address = str(self.margin_address or "").strip()
        margin_snapshot = read_margin_snapshot(address) if address else None
        body = build_page(
            instances=self.instances,
            portfolio_equity_path=self.portfolio_equity_path,
            portfolio_volume_path=self.portfolio_volume_path,
            margin_snapshot=margin_snapshot,
        ).encode("utf-8")
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
    parser.add_argument(
        "--margin-address",
        default="",
        help=(
            "用于查询保证金与强平距离的公开账户地址；留空则不显示该区块。"
            "只读公开接口，不涉及任何签名凭据"
        ),
    )
    args = parser.parse_args()
    HedgePanelHandler.margin_address = args.margin_address.strip()

    with http.server.HTTPServer(("localhost", args.port), HedgePanelHandler) as server:
        print(f"对冲面板已启动：http://localhost:{args.port}（Ctrl+C 停止）", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
