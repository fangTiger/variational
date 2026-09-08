"""Swap carry 无人值守周循环守护进程。

launchd 每分钟以 ``--once`` 启动，普通窗口每五分钟执行完整轮次。既有仓位始终先执行平仓与风控检查；
只有账户两腿均为空且所有入场条件明确满足时，才复用人工执行器尝试自动开仓。
"""

from __future__ import annotations

# 必须在导入交易相关依赖前配好 CA。
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import fcntl  # noqa: E402
import logging  # noqa: E402
from functools import wraps  # noqa: E402
from engine import swap_carry_cost as cost
from engine.isolated_allocation import required_allocation  # noqa: E402
import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal, ROUND_CEILING  # noqa: E402
from pathlib import Path

from infra.data_paths import data_dir  # noqa: E402
from typing import Any, Mapping, Sequence  # noqa: E402

from adapters.base import Position, Side  # noqa: E402
from adapters.variational_client import (  # noqa: E402
    READ_ONLY_HTTP,
    SessionExpiry,
    VariationalAuthError,
    VariationalJurisdictionError,
    VariationalRequestError,
    get_session_expiry,
)
from engine.swap_trading_schedule import (  # noqa: E402
    SwapTradingSchedule,
    parse_trading_schedule,
)
from tools.alert_check import notify  # noqa: E402
from tools import hedge_swap_carry as execution  # noqa: E402
from tools.swap_carry_rehearsal import (  # noqa: E402
    perform_rehearsal as _perform_rehearsal,
    rehearsal_window,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KILL_SWITCH = execution.SWAP_CARRY_KILL_SWITCH
DEFAULT_HEARTBEAT = execution.SWAP_CARRY_GUARD_HEARTBEAT
DEFAULT_STATE = execution.SWAP_CARRY_GUARD_STATE
DEFAULT_AUDIT_LOG = data_dir() / "swap_carry_guard_audit.jsonl"
DEFAULT_SWITCH_HISTORY = data_dir() / "swap_carry_switch_history.jsonl"

TARGET_LIQUIDATION_DISTANCE = Decimal("0.08")
MAX_ALLOCATION_USD = Decimal("800")
MAX_DAILY_ALLOCATION_ATTEMPTS = 30
MAX_DAILY_ALLOCATION_ERRORS = 5
MIN_ALLOCATION_INTERVAL_SECONDS = 900

IMBALANCE_RATIO = Decimal("0.05")
LIQUIDATION_ALERT_RATIO = Decimal("0.015")
ACCOUNT_MARGIN_RATIO_MIN = Decimal("2.0")
PRE_CLOSE_MINUTES = 30
LONG_CLOSURE_THRESHOLD = timedelta(hours=4)
CLOSE_RETRIES = 3
RETRY_DELAY_SECONDS = 1.0
AUTO_OPEN_NOTIONAL_USD = Decimal("4000")
MIN_ENTRY_CARRY_ANNUAL = Decimal("0.02")
MIN_TIME_TO_CLOSE = timedelta(hours=2)
MAX_DAILY_OPEN_ATTEMPTS = 20
SWITCH_LEAD_TIME = timedelta(minutes=60)
SWITCH_MIN_ADVANTAGE = Decimal("0.025")
MIN_HOLD_AFTER_SWITCH = timedelta(hours=4)
REHEARSAL_LEAD = timedelta(minutes=30)
REHEARSAL_TIMEOUT_SECONDS = 20
REHEARSAL_SLIPPAGE_WARNING_BP = Decimal("20")
EXIT_CARRY_ANNUAL = Decimal("-0.10")
EXIT_CARRY_DURATION = timedelta(hours=24)
REOPEN_COOLDOWN = timedelta(hours=2)
SESSION_WARNING_THRESHOLD = timedelta(hours=24)
SESSION_CRITICAL_THRESHOLD = timedelta(hours=6)
SESSION_WARNING_COOLDOWN = timedelta(hours=2)
SWITCH_NET_DELTA_TOLERANCE = execution.XAUS_QTY_STEP
IGNORED_EXTERNAL_POSITIONS = frozenset({"BTC"})
NORMAL_INTERVAL = 300
CRITICAL_WINDOW_BEFORE_SWITCH = timedelta(minutes=45)
# 已经逼近危险才加密轮询；低于 8% 补仓目标不代表进入关键窗口。
CRITICAL_LIQUIDATION_DISTANCE = Decimal("0.05")
CRITICAL_ACCOUNT_MARGIN_RATIO = Decimal("3.0")


def _planned_switch_at(
    heartbeat: Mapping[str, Any], switch_lead_time: timedelta,
) -> datetime | None:
    """从上轮绝对日程边界计算计划切换时间，不使用会随早退失真的相对秒数。"""
    schedule = heartbeat.get("xaus_schedule") or {}
    if not schedule.get("metadata_is_fresh"):
        return None
    duration = schedule.get("closure_duration_seconds")
    if duration is None or float(duration) <= LONG_CLOSURE_THRESHOLD.total_seconds():
        return None
    if heartbeat.get("structure") == "XAUS_XAU":
        close_at = execution._guard_timestamp(schedule.get("next_close_at"))
        return close_at - switch_lead_time if close_at is not None else None
    return execution._guard_timestamp(schedule.get("next_open_at"))


def _polling_plan(
    heartbeat: Mapping[str, Any] | None, *, now: datetime,
    kill_switch_path: Path, switch_lead_time: timedelta,
    critical_reasons: list[str] | None = None,
) -> tuple[str, datetime]:
    """只依赖本地快照，汇总关键条件，避免旧模式覆盖最新风险判断。"""
    reasons = critical_reasons if critical_reasons is not None else []
    reasons.clear()
    critical = ("critical", now + timedelta(seconds=60))
    if kill_switch_path.exists():
        reasons.append("kill_switch_active")
    if not heartbeat:
        reasons.append("missing_heartbeat")
        return critical
    last_full = execution._guard_timestamp(heartbeat.get("last_full_round_at"))
    if last_full is None or last_full > now:
        reasons.append("invalid_last_full_round_at")
    try:
        status = heartbeat.get("status")
        if status in {
            "incident", "blocked", "flattened", "pending_xaus_close",
            "kill_switch_active", "exit_carry_triggered", "action_failed", "session_expired",
        }:
            reasons.append(f"status:{status}")
        for key in (
            "auto_open_incident", "switch_incident", "rehearsal_blocked",
            "close_attempted", "auto_switch_attempted", "consecutive_failures",
        ):
            if heartbeat.get(key):
                reasons.append(key)
        for word in ("incident", "blocked"):
            if word in str(heartbeat.get("conclusion", "")).lower():
                reasons.append(f"conclusion:{word}")
        for underlying, leg in heartbeat.get("legs", {}).items():
            if leg.get("margin_mode") != "isolated":
                continue
            distance = (leg.get("per_leg_liquidation") or {}).get("distance")
            if distance is not None:
                value = Decimal(str(distance))
                if not value.is_finite() or value < CRITICAL_LIQUIDATION_DISTANCE:
                    reasons.append(f"liquidation_distance:{underlying}")
        for underlying, allocation in heartbeat.get("isolated_allocation", {}).items():
            if int(allocation.get("daily_abnormal_count", 0)) > 0:
                reasons.append(f"allocation_abnormal_count:{underlying}")
            distance = allocation.get("distance")
            if distance is not None:
                value = Decimal(str(distance))
                if not value.is_finite() or value < CRITICAL_LIQUIDATION_DISTANCE:
                    reasons.append(f"allocation_liquidation_distance:{underlying}")
        ratio = (heartbeat.get("account_margin") or {}).get("ratio")
        if ratio is not None:
            value = Decimal(str(ratio))
            if not value.is_finite() or value < CRITICAL_ACCOUNT_MARGIN_RATIO:
                reasons.append("account_margin_ratio")
        planned = _planned_switch_at(heartbeat, switch_lead_time)
        if planned is not None and planned - now <= CRITICAL_WINDOW_BEFORE_SWITCH:
            reasons.append("switch_window")
        if reasons:
            return critical
        next_full = last_full + timedelta(seconds=NORMAL_INTERVAL)
        if planned is not None:
            next_full = min(next_full, planned - CRITICAL_WINDOW_BEFORE_SWITCH)
        return "normal", next_full
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        reasons.append("invalid_risk_snapshot")
        return critical


@dataclass(frozen=True)
class FlattenResult:
    """一次退出动作的结果。"""

    complete: bool
    pending_xaus: bool
    message: str


@dataclass(frozen=True)
class AutoOpenResult:
    """一轮最低优先级自动开仓判定的结果。"""

    attempted: bool
    conclusion: str
    daily_attempts: int
    status: str = "healthy"
    result_code: int = 0
    incident: bool = False
    # 仅表示本轮成功执行过 reduce_only，不表示账户当前为空仓。
    did_close_this_round: bool = False


@dataclass(frozen=True)
class StructureDetection:
    """账户三标的持仓对应的当前结构识别结果。"""

    structure: execution.CarryStructure | None
    error: str | None = None


@dataclass(frozen=True)
class FundingAvailability:
    """单腿费率在当前结构预期持有期内是否可用于 carry 决策。"""

    usable: bool
    reason: str
    schedule_source: str | None = None


@dataclass(frozen=True)
class MarginModeStatus:
    """单腿保证金模式及其权威来源。"""

    mode: str
    source: str

    @property
    def isolated(self) -> bool:
        """返回是否必须执行严格的独立桶强平监控。"""
        return self.mode == "isolated"


@dataclass(frozen=True)
class AccountPositionMargin:
    """账户级保证金计算中的一条真实持仓。"""

    underlying: str
    qty: Decimal
    mark_price: Decimal
    maintenance_rate: Decimal
    maintenance_margin: Decimal


@dataclass(frozen=True)
class AccountMarginHealth:
    """全账户权益与全部持仓维持保证金的比率。"""

    equity: Decimal
    maintenance_margin: Decimal
    ratio: Decimal
    positions: tuple[AccountPositionMargin, ...]


class CloseActionError(RuntimeError):
    """有限次数重试后仍无法完成的平仓错误。"""


class _SessionExpiredPreflight(RuntimeError):
    """会话已过期，必须在任何账户或交易调用前结束本轮。"""


class _SwitchTradeRecorder:
    """透明代理交易客户端，并记录切换期间真正接受的 RFQ。"""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.phase = "close"
        self.records: dict[str, list[dict[str, object]]] = {
            "close": [],
            "open": [],
        }
        self._quotes: dict[str, dict[str, object]] = {}

    def __getattr__(self, name: str) -> Any:
        """未拦截的能力全部委托给真实客户端。"""
        return getattr(self._client, name)

    async def request_quote(
        self,
        underlying: str,
        side: str,
        qty: Decimal,
        **kwargs: object,
    ) -> object:
        """记录报价耗时，并保存 accept 所需的成交上下文。"""
        started = time.perf_counter()
        try:
            payload = await self._client.request_quote(
                underlying,
                side,
                qty,
                **kwargs,
            )
        except Exception as exc:
            self.records[self.phase].append(
                {
                    "status": "failed",
                    "market": underlying,
                    "side": side,
                    "execution_price": None,
                    "quote_mid": None,
                    "rfq_id": None,
                    "filled_quantity": "0",
                    "duration_ms": round(
                        (time.perf_counter() - started) * 1000,
                        3,
                    ),
                    "slippage_bp": None,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            raise
        if not isinstance(payload, Mapping):
            return payload
        quote_id = payload.get("quote_id")
        if isinstance(quote_id, str) and quote_id:
            self._quotes[quote_id] = {
                "market": underlying,
                "side": side,
                "quantity": qty,
                "payload": payload,
                "quote_duration_ms": (time.perf_counter() - started) * 1000,
            }
        return payload

    async def accept_quote(
        self,
        *,
        quote_id: str,
        side: str,
        max_slippage: float,
        is_reduce_only: bool,
    ) -> object:
        """记录成交价、rfq_id、数量、耗时和相对报价中点滑点。"""
        context = self._quotes.get(quote_id, {})
        payload = context.get("payload")
        quote_payload = payload if isinstance(payload, Mapping) else {}
        market = str(context.get("market") or "未知")
        quantity = context.get("quantity")
        execution_price: Decimal | None = None
        quote_mid: Decimal | None = None
        slippage_bp: Decimal | None = None
        try:
            side_value = Side.BUY if side.lower() == "buy" else Side.SELL
            execution_price = execution._quote_price(quote_payload, side_value)
            bid = execution._decimal(
                quote_payload.get("bid"),
                label=f"{market} bid",
                positive=True,
            )
            ask = execution._decimal(
                quote_payload.get("ask"),
                label=f"{market} ask",
                positive=True,
            )
            quote_mid = (bid + ask) / Decimal("2")
            direction = Decimal("1") if side_value is Side.BUY else Decimal("-1")
            slippage_bp = (
                direction
                * (execution_price - quote_mid)
                / quote_mid
                * Decimal("10000")
            )
        except (TypeError, ValueError):
            # 交易安全不依赖观测字段；缺失时在台账中保留 null。
            pass

        started = time.perf_counter()
        try:
            result = await self._client.accept_quote(
                quote_id=quote_id,
                side=side,
                max_slippage=max_slippage,
                is_reduce_only=is_reduce_only,
            )
        except Exception as exc:
            accept_duration_ms = (time.perf_counter() - started) * 1000
            self.records[self.phase].append(
                {
                    "status": "failed",
                    "market": market,
                    "side": side,
                    "reduce_only": is_reduce_only,
                    "execution_price": execution_price,
                    "quote_mid": quote_mid,
                    "rfq_id": None,
                    "filled_quantity": "0",
                    "duration_ms": round(
                        float(context.get("quote_duration_ms", 0))
                        + accept_duration_ms,
                        3,
                    ),
                    "slippage_bp": slippage_bp,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            raise

        accept_duration_ms = (time.perf_counter() - started) * 1000
        rfq_id = (
            result.get("rfq_id")
            if isinstance(result, Mapping)
            else getattr(result, "rfq_id", None)
        )
        self.records[self.phase].append(
            {
                "status": "succeeded",
                "market": market,
                "side": side,
                "reduce_only": is_reduce_only,
                "execution_price": execution_price,
                "quote_mid": quote_mid,
                "rfq_id": str(rfq_id) if rfq_id not in (None, "") else None,
                # Variational RFQ accept 返回成交编号即表示报价数量全量成交。
                "filled_quantity": str(quantity),
                "duration_ms": round(
                    float(context.get("quote_duration_ms", 0))
                    + accept_duration_ms,
                    3,
                ),
                "slippage_bp": slippage_bp,
            }
        )
        return result


def _json_default(value: object) -> str:
    """把审计载荷中的时间和十进制数稳定转换为字符串。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"无法序列化 {type(value).__name__}")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    """原子覆盖单份状态或心跳文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_audit(path: Path, payload: Mapping[str, object]) -> None:
    """追加一条不可覆盖的 JSONL 审计记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n"
        )
    if "close_phase" in payload and "started_at" in payload and payload.get("kind") != "rehearsal":
        try:
            cost.record_switch(cost.ACTIVE_PATH.get(), payload)
        except Exception as exc:  # noqa: BLE001 审计原件已保存，后续扫描可重试
            logging.getLogger(__name__).warning("切换成本记账失败：%s", exc)


def _account_quantities(payload: object) -> dict[str, Decimal]:
    """从真实 `/positions` schema 提取全部非重复标的数量。"""
    quantities: dict[str, Decimal] = {}
    for raw_position in _position_items(payload):
        if not isinstance(raw_position, Mapping):
            raise ValueError("/positions 持仓记录不是对象")
        raw_info = raw_position.get("position_info", raw_position)
        if not isinstance(raw_info, Mapping):
            raise ValueError("/positions.position_info 不是对象")
        raw_instrument = raw_info.get("instrument")
        instrument = raw_instrument if isinstance(raw_instrument, Mapping) else {}
        underlying = str(
            instrument.get("underlying") or raw_info.get("underlying") or ""
        ).strip().upper()
        if not underlying:
            raise ValueError("/positions 持仓缺少 instrument.underlying")
        if underlying in quantities:
            raise ValueError(f"/positions 返回重复标的 {underlying}")
        quantities[underlying] = execution._decimal(
            raw_info.get("qty", raw_info.get("size")),
            label=f"{underlying} 持仓数量",
        )
    return quantities


def _managed_legs() -> tuple[execution.CarryLeg, ...]:
    """返回切换自检与台账覆盖的三个受管标的。"""
    return (
        execution.XAUS_LEG,
        execution.XAU_LONG_LEG,
        execution.XAUT_LEG,
    )


async def _switch_snapshot(
    var: Any,
    *,
    positions_payload: object,
    metadata: object | None,
) -> dict[str, object]:
    """读取一份权益净值和全受管腿仓位快照。"""
    quantities = _account_quantities(positions_payload)
    balance = await var.get_balance()
    equity_value = (
        balance.get("equity")
        if isinstance(balance, Mapping)
        else getattr(balance, "equity", None)
    )
    equity = execution._decimal(equity_value, label="账户权益")
    legs: dict[str, dict[str, object]] = {}
    for leg in _managed_legs():
        quantity = quantities.get(leg.underlying, Decimal("0"))
        price = (
            execution._metadata_price(metadata, leg)
            if metadata is not None
            else None
        )
        notional = abs(quantity) * price if price is not None else None
        legs[leg.underlying] = {
            "quantity": str(quantity),
            "notional_usd": _format_money(notional),
        }
    net_delta = sum(
        (quantities.get(leg.underlying, Decimal("0")) for leg in _managed_legs()),
        Decimal("0"),
    )
    external = {
        underlying: str(quantity)
        for underlying, quantity in quantities.items()
        if underlying not in legs and quantity != 0
    }
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "legs": legs,
        "external_legs": external,
        "net_delta": str(net_delta),
        # get_balance 已包含 balance + upnl，因此这里是结算资金费后的账户净值。
        "equity": str(equity),
    }


async def _await_managed_flat(
    var: Any,
) -> tuple[dict[str, Position], object, int]:
    """轮询确认三个受管标的全部归零，并返回真实轮询次数。"""
    payload: object = []
    positions = {
        leg.underlying: Position(leg.underlying, Decimal("0"))
        for leg in _managed_legs()
    }
    for attempt in range(1, execution._FLAT_TRIES + 1):
        payload = await var.get_positions()
        positions = _carry_positions_from_payload(payload)
        if all(position.is_flat for position in positions.values()):
            return positions, payload, attempt
        if attempt < execution._FLAT_TRIES:
            await asyncio.sleep(execution._POLL_DELAY_S)
    return positions, payload, execution._FLAT_TRIES


def _switch_self_check(
    target: execution.CarryStructure | str,
    *,
    account_positions_payload: object,
    old_structure_was_flat: bool,
    tolerance: Decimal = SWITCH_NET_DELTA_TOLERANCE,
) -> dict[str, object]:
    """在切换后校验旧仓、目标方向权重、净 delta 和结构外残仓。"""
    selected = execution.resolve_structure(target)
    quantities = _account_quantities(account_positions_payload)
    target_positions = {
        leg.underlying: Position(
            leg.underlying,
            quantities.get(leg.underlying, Decimal("0")),
        )
        for leg in selected.legs
    }
    imbalance = _imbalance_reason(selected, target_positions)
    target_open = all(not position.is_flat for position in target_positions.values())
    directions_and_weights_ok = target_open and imbalance is None
    net_delta = sum(
        (position.signed_size for position in target_positions.values()),
        Decimal("0"),
    )
    residual_legs = sorted(
        underlying
        for underlying, quantity in quantities.items()
        if quantity != 0
        and underlying not in target_positions
        and underlying not in IGNORED_EXTERNAL_POSITIONS
    )
    checks: dict[str, dict[str, object]] = {
        "old_structure_flat": {
            "passed": old_structure_was_flat,
            "message": "开新结构前已确认三个受管标的全平",
        },
        "new_structure_directions_and_weights": {
            "passed": directions_and_weights_ok,
            "message": (
                "目标结构方向与权重比例正确"
                if directions_and_weights_ok
                else imbalance or "目标结构未完整开出"
            ),
        },
        "net_delta_within_tolerance": {
            "passed": abs(net_delta) <= tolerance,
            "net_delta": str(net_delta),
            "tolerance": str(tolerance),
        },
        "no_external_residual_legs": {
            "passed": not residual_legs,
            "residual_legs": residual_legs,
        },
    }
    passed = all(bool(check["passed"]) for check in checks.values())
    return {
        "performed": True,
        "passed": passed,
        "status": "通过" if passed else "失败",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


def _residual_state(payload: object) -> tuple[str, bool]:
    """把切换失败后的真实账户快照归类为空仓或残仓。"""
    quantities = _account_quantities(payload)
    has_residual = any(
        quantity != 0
        for underlying, quantity in quantities.items()
        if underlying not in IGNORED_EXTERNAL_POSITIONS
    )
    return ("残仓" if has_residual else "空仓"), has_residual


def _phase_payload(
    recorder: _SwitchTradeRecorder,
    phase: str,
    *,
    started: float,
    status: str,
) -> dict[str, object]:
    """构造带总耗时的平仓或开仓阶段载荷。"""
    records = recorder.records[phase]
    successful = [record for record in records if record.get("status") == "succeeded"]
    rollbacks = [record for record in successful if record.get("reduce_only") is True]
    legs = [record for record in successful if record.get("reduce_only") is (phase == "close")]
    payload: dict[str, object] = {
        "status": status,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "legs": legs,
        "failed_attempts": [
            record for record in records if record.get("status") == "failed"
        ],
    }
    if phase == "open":
        payload["rollback_legs"] = rollbacks
    return payload


def _read_failure_count(path: Path) -> int:
    """读取上一轮连续失败数；损坏状态不冒充健康。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = int(payload.get("consecutive_failures", 0))
        return max(0, value)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _read_auto_open_state(path: Path, observed_at: datetime) -> tuple[int, bool]:
    """读取当日尝试计数与跨轮次 INCIDENT；日期按 UTC 切换。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return 0, False
        incident = payload.get("auto_open_incident") is True
        if payload.get("open_attempt_date") != observed_at.date().isoformat():
            return 0, incident
        attempts = max(0, int(payload.get("daily_open_attempts", 0)))
        return attempts, incident
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return 0, False


def _read_switch_incident(path: Path) -> bool:
    """读取需人工清除的切换事故标记；损坏状态不伪造事故。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return isinstance(payload, Mapping) and payload.get("switch_incident") is True


def _read_state_timestamp(path: Path, key: str) -> datetime | None:
    """读取持久化 UTC 时间；旧轮次状态不推算持续时间。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get(key)
        if not isinstance(raw, str):
            return None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _read_session_alert_at(path: Path) -> datetime | None:
    """读取跨进程通知冷却时间；损坏值按从未通知处理。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("last_session_expiry_alert_at")
        if not isinstance(raw, str):
            return None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _warn_session_expiry(
    expiry: SessionExpiry | None,
    *,
    observed_at: datetime,
    previous_alert_at: datetime | None,
    audit_path: Path,
) -> datetime | None:
    """按剩余时间弹本地通知，并为普通预警应用持久化冷却。"""
    if expiry is None or expiry.remaining >= SESSION_WARNING_THRESHOLD:
        return previous_alert_at

    expired = expiry.remaining <= timedelta(0)
    critical = expiry.remaining < SESSION_CRITICAL_THRESHOLD
    should_notify = critical or previous_alert_at is None
    if not should_notify and previous_alert_at is not None:
        should_notify = observed_at - previous_alert_at >= SESSION_WARNING_COOLDOWN

    if expired:
        title = "Swap carry 会话已过期"
        message = "会话已过期，需人工刷新 Cookie"
    else:
        title = "Swap carry 会话即将过期"
        message = (
            f"Variational 会话剩余 {expiry.hours_left:.1f} 小时；"
            "请按文档重新导出 Cookie"
        )

    notification_sent = False
    if should_notify:
        notification_sent = notify(title, message)
        previous_alert_at = observed_at

    _append_audit(
        audit_path,
        {
            "timestamp": observed_at,
            "event": "session_expiry_warning",
            "level": "critical" if critical else "warning",
            "message": message,
            "session_expires_at": expiry.expires_at.isoformat(),
            "session_hours_left": expiry.hours_left,
            "notification_attempted": should_notify,
            "notification_sent": notification_sent,
        },
    )
    return previous_alert_at


def _state_payload(
    *,
    observed_at: datetime,
    status: str,
    message: str,
    consecutive_failures: int,
    open_attempt_date: str | None = None,
    daily_open_attempts: int = 0,
    auto_open_incident: bool = False,
    switch_incident: bool = False,
    exit_carry_since: datetime | None = None,
    last_closed_at: datetime | None = None,
    last_session_expiry_alert_at: datetime | None = None,
) -> dict[str, object]:
    """构造供人工 status 置顶显示的显著状态。"""
    return {
        "timestamp": observed_at.isoformat(),
        "status": status,
        "message": message,
        "consecutive_failures": consecutive_failures,
        "open_attempt_date": open_attempt_date or observed_at.date().isoformat(),
        "daily_open_attempts": daily_open_attempts,
        "auto_open_incident": auto_open_incident,
        "switch_incident": switch_incident,
        "exit_carry_since": exit_carry_since.isoformat() if exit_carry_since else None,
        "last_closed_at": last_closed_at.isoformat() if last_closed_at else None,
        "last_session_expiry_alert_at": (
            last_session_expiry_alert_at.isoformat()
            if last_session_expiry_alert_at is not None
            else None
        ),
    }


def _schedule_payload(schedule: SwapTradingSchedule | None) -> dict[str, object]:
    """把时段判断转换为心跳可读字段。"""
    if schedule is None:
        return {
            "is_tradable": False,
            "metadata_is_fresh": False,
            "time_until_close_seconds": None,
            "closure_duration_seconds": None,
            "reason": "时段元数据读取失败",
        }
    return {
        "is_tradable": schedule.is_tradable,
        "metadata_is_fresh": schedule.metadata_is_fresh,
        "time_until_close_seconds": (
            schedule.time_until_close.total_seconds()
            if schedule.time_until_close is not None
            else None
        ),
        "closure_duration_seconds": (
            schedule.closure_duration.total_seconds()
            if schedule.closure_duration is not None
            else None
        ),
        "next_close_at": (
            schedule.next_close_at.isoformat()
            if schedule.next_close_at is not None
            else None
        ),
        "next_open_at": (
            schedule.next_open_at.isoformat()
            if schedule.next_open_at is not None
            else None
        ),
        "reason": schedule.reason,
    }


def _position_notional(
    position: Position | None,
    price: Decimal | None,
) -> Decimal | None:
    """按元数据价格计算绝对名义；任一输入未知则保持未知。"""
    if position is None or price is None:
        return None
    return abs(position.signed_size) * price


def _format_money(value: Decimal | None) -> str | None:
    """在心跳中使用固定两位十进制美元字符串。"""
    return format(value, ".2f") if value is not None else None


def _funding_availability_by_leg(
    selected: execution.CarryStructure,
    *,
    target_structure: execution.CarryStructure | None,
    target_reason: str,
) -> dict[str, FundingAvailability]:
    """仅用于入场和切换目标校验；不得用于已持仓的出场评估。"""
    if target_structure is None:
        usable = False
        detail = f"无法判定目标结构：{target_reason}"
    elif selected.name != target_structure.name:
        usable = False
        detail = (
            f"待评估结构 {selected.name} 不是目标结构 "
            f"{target_structure.name}"
        )
    else:
        usable = True
        detail = (
            f"{selected.name} 是目标结构；"
            "当前费率（包括合法零值）可用于持有期 carry"
        )
    return {
        leg.underlying: FundingAvailability(
            usable,
            f"{leg.underlying} 费率{'可用' if usable else '不可用'}：{detail}",
            schedule_source="XAUS",
        )
        for leg in selected.legs
    }


def _unusable_funding_reason(
    availability: Mapping[str, FundingAvailability],
) -> str | None:
    """汇总任一不能用于决策的结构腿。"""
    reasons = [item.reason for item in availability.values() if not item.usable]
    return "；".join(reasons) if reasons else None


def _margin_mode_from_supported_assets(
    metadata: object | None,
    leg: execution.CarryLeg,
) -> MarginModeStatus | None:
    """优先从标的元数据读取是否仅支持 isolated。"""
    if metadata is None:
        return None
    try:
        record = execution._instrument_record(metadata, leg)
    except (TypeError, ValueError):
        return None
    isolated_only = record.get("isolated_only")
    if isolated_only is True:
        return MarginModeStatus("isolated", "supported_assets.isolated_only")
    if isolated_only is False:
        return MarginModeStatus("cross", "supported_assets.isolated_only")
    return None


def _margin_mode_from_quote(
    quote: Mapping[str, Any],
) -> MarginModeStatus | None:
    """从报价保证金要求读取显式模式，未知值不作乐观猜测。"""
    requirements = quote.get("margin_requirements")
    if not isinstance(requirements, Mapping):
        return None
    raw_mode = requirements.get("margin_mode")
    normalized = str(raw_mode or "").strip().lower()
    if normalized in {"isolated", "isolated_margin"}:
        return MarginModeStatus(
            "isolated",
            "报价 margin_requirements.margin_mode",
        )
    if normalized in {"cross", "cross_margin"}:
        return MarginModeStatus(
            "cross",
            "报价 margin_requirements.margin_mode",
        )
    return None


def _quote_cache_key(
    leg: execution.CarryLeg,
    side: Side,
) -> tuple[str, str, int, str | None, str]:
    """用完整合约描述和方向隔离监控报价缓存。"""
    return (
        leg.underlying,
        leg.instrument_type,
        leg.funding_interval_s,
        leg.kind,
        side.value,
    )


async def _monitoring_quote(
    var: Any,
    leg: execution.CarryLeg,
    position: Position,
    quote_cache: dict[
        tuple[str, str, int, str | None, str],
        Mapping[str, Any],
    ],
) -> Mapping[str, Any]:
    """按当前持仓方向请求一次报价，并在本轮账户检查中复用。"""
    if position.is_flat:
        raise ValueError(f"{leg.underlying} 空仓无需请求监控报价")
    side = Side.BUY if position.signed_size > 0 else Side.SELL
    key = _quote_cache_key(leg, side)
    quote = quote_cache.get(key)
    if quote is None:
        quote = await execution._request_quote(
            var,
            leg,
            side,
            abs(position.signed_size),
        )
        quote_cache[key] = quote
    return quote


async def _resolve_margin_mode(
    var: Any,
    *,
    metadata: object | None,
    leg: execution.CarryLeg,
    position: Position,
    quote_cache: dict[
        tuple[str, str, int, str | None, str],
        Mapping[str, Any],
    ],
) -> MarginModeStatus:
    """按元数据、报价、保守默认的固定顺序判定保证金模式。"""
    supported_mode = _margin_mode_from_supported_assets(metadata, leg)
    if supported_mode is not None:
        return supported_mode
    try:
        quote = await _monitoring_quote(var, leg, position, quote_cache)
    except Exception:  # noqa: BLE001 报价不可用等价于第二级证据缺失
        return MarginModeStatus("isolated", "保守默认")
    quote_mode = _margin_mode_from_quote(quote)
    if quote_mode is not None:
        return quote_mode
    return MarginModeStatus("isolated", "保守默认")


def _position_items(payload: object) -> Sequence[object]:
    """兼容 `/positions` 的列表响应与带 positions 键的对象响应。"""
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return payload
    if isinstance(payload, Mapping):
        items = payload.get("positions")
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            return items
    raise ValueError("/positions 响应缺少持仓列表")


def _account_position_leg(
    raw_position: object,
    selected_legs: Mapping[str, execution.CarryLeg],
) -> tuple[execution.CarryLeg, Position, Decimal]:
    """从真实持仓记录还原询价参数、有符号数量和标记价。"""
    if not isinstance(raw_position, Mapping):
        raise ValueError("/positions 持仓记录不是对象")
    raw_info = raw_position.get("position_info", raw_position)
    if not isinstance(raw_info, Mapping):
        raise ValueError("/positions.position_info 不是对象")
    raw_instrument = raw_info.get("instrument")
    instrument = raw_instrument if isinstance(raw_instrument, Mapping) else {}

    underlying_value = instrument.get("underlying") or raw_info.get("underlying")
    underlying = str(underlying_value or "").strip().upper()
    if not underlying:
        raise ValueError("/positions 持仓缺少 instrument.underlying")
    qty_value = raw_info.get("qty", raw_info.get("size"))
    qty = execution._decimal(qty_value, label=f"{underlying} 持仓数量")

    selected_leg = selected_legs.get(underlying)
    if selected_leg is not None:
        leg = selected_leg
    else:
        instrument_type = str(instrument.get("instrument_type") or "").strip()
        if not instrument_type:
            raise ValueError(f"{underlying} 持仓缺少 instrument_type")
        raw_interval = instrument.get("funding_interval_s")
        if isinstance(raw_interval, bool):
            raise ValueError(f"{underlying} funding_interval_s 无效")
        try:
            funding_interval_s = int(raw_interval)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{underlying} funding_interval_s 无效") from exc
        raw_kind = instrument.get("kind")
        kind = str(raw_kind).strip() if raw_kind not in (None, "") else None
        leg = execution.CarryLeg(
            underlying=underlying,
            open_side=Side.BUY if qty >= 0 else Side.SELL,
            instrument_type=instrument_type,
            funding_interval_s=funding_interval_s,
            kind=kind,
            weight=Decimal("1"),
        )

    raw_price_info = raw_position.get("price_info")
    price_info = raw_price_info if isinstance(raw_price_info, Mapping) else {}
    mark_value = (
        price_info.get("underlying_price")
        or raw_position.get("mark_price")
        or raw_info.get("mark_price")
        or raw_info.get("avg_entry_price")
    )
    mark_price = execution._decimal(
        mark_value,
        label=f"{underlying} 标记价",
        positive=True,
    )
    return leg, Position(underlying, qty, raw=raw_position), mark_price


def _carry_positions_from_payload(payload: object) -> dict[str, Position]:
    """从真实 `/positions` 响应提取三个受管标的，缺席标的视为空仓。"""
    monitored_legs = {
        execution.XAUS_LEG.underlying: execution.XAUS_LEG,
        execution.XAU_LONG_LEG.underlying: execution.XAU_LONG_LEG,
        execution.XAUT_LEG.underlying: execution.XAUT_LEG,
    }
    positions = {
        underlying: Position(underlying, Decimal("0"))
        for underlying in monitored_legs
    }
    seen: set[str] = set()
    for raw_position in _position_items(payload):
        leg, position, _mark_price = _account_position_leg(
            raw_position,
            monitored_legs,
        )
        if leg.underlying not in monitored_legs:
            continue
        if leg.underlying in seen:
            raise ValueError(
                f"/positions 返回重复的受管标的 {leg.underlying}"
            )
        seen.add(leg.underlying)
        positions[leg.underlying] = position
    return positions


def _detect_carry_structure(
    positions: Mapping[str, Position],
) -> StructureDetection:
    """按三标的实际方向识别三种结构；权重与缺腿交由失衡检查。"""
    xaus = positions["XAUS"].signed_size
    xau = positions["XAU"].signed_size
    xaut = positions["XAUT"].signed_size
    if xaus == 0 and xau == 0 and xaut == 0:
        return StructureDetection(None)

    xaus_xau_compatible = xaus >= 0 and xau <= 0 and xaut == 0
    xau_xaut_compatible = xaus == 0 and xau >= 0 and xaut <= 0
    if xaus_xau_compatible and (xaus > 0 or xau < 0):
        return StructureDetection(execution.XAUS_XAU)
    if xau_xaut_compatible and (xau > 0 or xaut < 0):
        return StructureDetection(execution.XAU_XAUT)

    if xaus > 0 and xau > 0 and xaut < 0:
        return StructureDetection(execution.TRIPLE)

    detail = f"XAUS={xaus} XAU={xau} XAUT={xaut}"
    return StructureDetection(
        execution.TRIPLE,
        f"检测到两个结构同时持仓或方向异常：{detail}",
    )


async def _evaluate_carry_candidates(
    var: Any,
    schedule: SwapTradingSchedule | None,
    market_status: str | None,
) -> tuple[dict[str, dict[str, Any]], execution.CarryStructure | None]:
    """每腿独立读取一次，合法零值有效，失败只排除受影响的结构。"""
    rates: dict[str, Decimal] = {}
    errors: dict[str, str] = {}
    for leg in execution.TRIPLE.legs:
        try:
            rate = await execution._funding_rate_for_leg(var, leg)
            if not rate.is_finite():
                raise ValueError("费率不是有限数值")
            rates[leg.underlying] = rate
        except Exception as exc:  # 单腿读取失败不得中断其他候选的评估
            errors[leg.underlying] = f"{leg.underlying} 费率读取失败：{type(exc).__name__}: {exc}"
    candidates: dict[str, dict[str, Any]] = {}
    best = None
    best_carry = None
    for structure in execution.STRUCTURES.values():
        reasons = [errors[leg.underlying] for leg in structure.legs if leg.underlying in errors]
        carry = None if reasons else execution._weighted_net_carry(structure, rates)
        if structure.has_xaus:
            if schedule is None or not schedule.metadata_is_fresh or schedule.closure_duration is None:
                reasons.append("XAUS 时段元数据不安全或不完整")
            elif market_status != "open" or not schedule.is_tradable:
                reasons.append("XAUS 当前不可交易")
            elif schedule.time_until_close is None or schedule.time_until_close <= MIN_TIME_TO_CLOSE:
                reasons.append(f"XAUS 距收市未超过最短开仓窗口 {MIN_TIME_TO_CLOSE}")
        candidates[structure.name] = {
            "carry_annual": str(carry) if carry is not None else None,
            "available": not reasons,
            "reason": "；".join(reasons) if reasons else "费率有效且可交易",
        }
        if not reasons and (best_carry is None or carry > best_carry):
            best, best_carry = structure, carry
    return candidates, best


def _carry_switch_decision(
    candidates: Mapping[str, Mapping[str, Any]],
    best: execution.CarryStructure | None,
    current: execution.CarryStructure | None,
    *, now: datetime, last_switch_at: datetime | None, forced: bool,
) -> dict[str, Any]:
    """收益切换同时满足入场阈值、优势和持有期，安全退出跳过收益限制。"""
    best_rate = candidates[best.name]["carry_annual"] if best else None
    current_rate = candidates[current.name]["carry_annual"] if current else None
    advantage = (Decimal(best_rate) - Decimal(current_rate)
                 if best_rate is not None and current_rate is not None else None)
    threshold_blocked = best_rate is not None and Decimal(best_rate) < MIN_ENTRY_CARRY_ANNUAL and not forced
    elapsed = (now - last_switch_at).total_seconds() if last_switch_at else None
    hysteresis_blocked = bool(current and best and current != best and not forced and (
        advantage is None or advantage < SWITCH_MIN_ADVANTAGE
        or (elapsed is not None and elapsed <= MIN_HOLD_AFTER_SWITCH.total_seconds())))
    if forced:
        reason = "长休市强制退出 XAUS，忽略收益阈值、优势与切换持有期"
    elif best is None:
        reason = "所有候选不可评估或不可交易：" + "；".join(f"{name}: {item['reason']}" for name, item in candidates.items())
    elif threshold_blocked:
        reason = f"最优 carry {Decimal(best_rate):.4%} 低于入场阈值 {MIN_ENTRY_CARRY_ANNUAL:.4%}，保持现状"
    elif hysteresis_blocked:
        reason = f"切换滞回阻挡：优势 {advantage if advantage is not None else '未知'}，要求 {SWITCH_MIN_ADVANTAGE}；距上次切换 {elapsed} 秒，要求超过 {MIN_HOLD_AFTER_SWITCH}"
    else:
        reason = f"按净 carry 择优 {best.name}，年化 {Decimal(best_rate):.4%}，优势 {format(advantage, '.4%') if advantage is not None else '无当前持仓对比'}"
    return {"allowed": best is not None and not threshold_blocked and not hysteresis_blocked,
            "advantage_annual": str(advantage) if advantage is not None else None,
            "hysteresis_blocked": hysteresis_blocked, "entry_threshold_blocked": threshold_blocked,
            "forced_long_closure": forced, "hold_elapsed_seconds": elapsed, "reason": reason}


def _target_structure_for_schedule(
    schedule: SwapTradingSchedule | None,
    market_status: str | None,
    *,
    switch_lead_time: timedelta,
) -> tuple[execution.CarryStructure | None, str]:
    """只按权威长休市边界选择目标结构，每日短休市不触发切换。"""
    if schedule is None or not schedule.metadata_is_fresh:
        detail = schedule.reason if schedule is not None else "无解析结果"
        return None, f"XAUS 时段元数据不安全，暂不切换：{detail}"
    if market_status is None:
        return None, "XAUS market_status 缺失，暂不切换"
    if schedule.closure_duration is None:
        return None, "XAUS 缺少完整休市长度，暂不切换"
    if schedule.closure_duration <= LONG_CLOSURE_THRESHOLD:
        if schedule.is_tradable and market_status == "open":
            return execution.XAUS_XAU, "当前开市，目标结构为 XAUS_XAU"
        return None, "当前处于每日短休市，保持现有结构不切换"
    if not schedule.is_tradable:
        if market_status == "open":
            return None, "XAUS 时段状态矛盾，暂不切换"
        return execution.XAU_XAUT, "当前处于长休市，目标结构为 XAU_XAUT"
    if market_status != "open":
        return None, "XAUS market_status 与交易会话矛盾，暂不切换"
    if schedule.time_until_close is None:
        return None, "XAUS 长休市前缺少剩余时间，暂不切换"
    if schedule.time_until_close <= switch_lead_time:
        return (
            execution.XAU_XAUT,
            f"距 XAUS 长休市仅 {schedule.time_until_close}，目标结构为 XAU_XAUT",
        )
    return execution.XAUS_XAU, "XAUS 开市且未临近长休市，目标结构为 XAUS_XAU"


def _target_funding_context(
    target: execution.CarryStructure,
    *,
    target_reason: str,
) -> tuple[dict[str, FundingAvailability], dict[str, Decimal]]:
    """生成目标结构费率上下文；所有数值都由 API 实际读取。"""
    availability = _funding_availability_by_leg(
        target,
        target_structure=target,
        target_reason=target_reason,
    )
    return availability, {}


def _maintenance_rate_from_quote(
    quote: Mapping[str, Any],
    underlying: str,
) -> Decimal:
    """严格读取报价中当前标的专属的期货维持保证金率。"""
    margin_params = quote.get("margin_params")
    params = margin_params.get("params") if isinstance(margin_params, Mapping) else None
    asset_params = params.get("asset_params") if isinstance(params, Mapping) else None
    if not isinstance(asset_params, Mapping):
        raise ValueError(f"{underlying} 报价缺少 margin_params.params.asset_params")
    raw_asset_param = next(
        (
            value
            for asset, value in asset_params.items()
            if str(asset).upper() == underlying and isinstance(value, Mapping)
        ),
        None,
    )
    if raw_asset_param is None:
        raise ValueError(f"{underlying} 报价缺少专属维持保证金参数")
    return execution._decimal(
        raw_asset_param.get("futures_maintenance_margin"),
        label=f"{underlying} 维持保证金率",
        positive=True,
    )


async def _account_margin_health(
    var: Any,
    *,
    selected: execution.CarryStructure,
    positions_payload: object | None = None,
    quote_cache: dict[
        tuple[str, str, int, str | None, str],
        Mapping[str, Any],
    ],
) -> AccountMarginHealth:
    """用全账户真实持仓计算权益相对维持保证金的倍数。"""
    payload = (
        positions_payload
        if positions_payload is not None
        else await var.get_positions()
    )
    selected_legs = {leg.underlying: leg for leg in selected.legs}
    margins: list[AccountPositionMargin] = []
    for raw_position in _position_items(payload):
        leg, position, mark_price = _account_position_leg(
            raw_position,
            selected_legs,
        )
        if position.is_flat:
            continue
        quote = await _monitoring_quote(var, leg, position, quote_cache)
        maintenance_rate = _maintenance_rate_from_quote(
            quote,
            leg.underlying,
        )
        maintenance_margin = (
            abs(position.signed_size) * mark_price * maintenance_rate
        )
        margins.append(
            AccountPositionMargin(
                underlying=leg.underlying,
                qty=position.signed_size,
                mark_price=mark_price,
                maintenance_rate=maintenance_rate,
                maintenance_margin=maintenance_margin,
            )
        )
    if not margins:
        raise ValueError("/positions 未返回任何非零持仓")

    balance = await var.get_balance()
    equity_value = (
        balance.get("equity")
        if isinstance(balance, Mapping)
        else getattr(balance, "equity", None)
    )
    equity = execution._decimal(equity_value, label="账户权益")
    maintenance_margin = sum(
        (item.maintenance_margin for item in margins),
        Decimal("0"),
    )
    if maintenance_margin <= 0:
        raise ValueError("账户总维持保证金必须大于 0")
    return AccountMarginHealth(
        equity=equity,
        maintenance_margin=maintenance_margin,
        ratio=equity / maintenance_margin,
        positions=tuple(margins),
    )


def _per_leg_liquidation_payload(
    *,
    mode: MarginModeStatus,
    status: str,
    distance: Decimal | None = None,
    error: str | None = None,
) -> dict[str, object]:
    """构造统一的单腿强平监控心跳字段。"""
    return {
        "status": status,
        "enforced": mode.isolated,
        "fallback": None if mode.isolated else "账户级保证金率",
        "distance": str(distance) if distance is not None else None,
        "threshold": str(LIQUIDATION_ALERT_RATIO),
        "error": error,
    }


def _account_margin_payload(
    health: AccountMarginHealth | None,
    *,
    error: str | None,
) -> dict[str, object]:
    """把账户级保证金检查结果转换为稳定的心跳结构。"""
    if health is None:
        return {
            "status": "无数据" if error is not None else "未检查",
            "equity": None,
            "maintenance_margin": None,
            "ratio": None,
            "threshold": str(ACCOUNT_MARGIN_RATIO_MIN),
            "positions": [],
            "error": error,
        }
    return {
        "status": (
            "正常" if health.ratio >= ACCOUNT_MARGIN_RATIO_MIN else "低于阈值"
        ),
        "equity": str(health.equity),
        "maintenance_margin": str(health.maintenance_margin),
        "ratio": str(health.ratio),
        "threshold": str(ACCOUNT_MARGIN_RATIO_MIN),
        "positions": [
            {
                "underlying": item.underlying,
                "qty": str(item.qty),
                "mark_price": str(item.mark_price),
                "maintenance_rate": str(item.maintenance_rate),
                "maintenance_margin": str(item.maintenance_margin),
            }
            for item in health.positions
        ],
        "error": None,
    }


def _liquidation_distance(
    info: object,
    position: Position,
    *,
    underlying: str,
) -> Decimal:
    """严格读取 API 权威强平价并计算方向相关距离。"""
    if not isinstance(info, tuple) or len(info) != 2:
        raise ValueError("get_liquidation_info 未返回权威强平价")
    mark = execution._decimal(info[0], label=f"{underlying} mark", positive=True)
    liquidation = execution._decimal(
        info[1], label=f"{underlying} 强平价", positive=True
    )
    if position.signed_size > 0:
        return (mark - liquidation) / mark
    if position.signed_size < 0:
        return (liquidation - mark) / mark
    raise ValueError("空仓没有强平距离")


def _imbalance_reason(
    structure: execution.CarryStructure | str,
    positions: Mapping[str, Position],
) -> str | None:
    """识别缺腿、方向异常及超过阈值的权重比例偏离。"""
    selected = execution.resolve_structure(structure)
    open_legs = [
        leg for leg in selected.legs if not positions[leg.underlying].is_flat
    ]
    if not open_legs:
        return None
    if len(open_legs) != len(selected.legs):
        remaining = "、".join(leg.underlying for leg in open_legs)
        return f"缺腿失衡：只剩 {remaining}"
    direction_errors = []
    for leg in selected.legs:
        size = positions[leg.underlying].signed_size
        correct = size > 0 if leg.open_side is execution.Side.BUY else size < 0
        if not correct:
            direction_errors.append(f"{leg.underlying}={size}")
    if direction_errors:
        return (
            "持仓方向异常：" + " ".join(direction_errors)
        )

    normalized = [
        abs(positions[leg.underlying].signed_size) / leg.weight
        for leg in selected.legs
    ]
    larger = max(normalized)
    if larger == 0:
        return None
    ratio = (larger - min(normalized)) / larger
    if ratio > IMBALANCE_RATIO:
        return f"结构权重比例失衡 {ratio:.2%}，超过阈值 {IMBALANCE_RATIO:.2%}"
    return None


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    """展开包装异常，供业务拒绝与回滚事故精确分类。"""
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _is_skew_rejection(error: BaseException) -> bool:
    """只把 message 含 skew 的 HTTP 422 识别为预期业务拒绝。"""
    for item in _exception_chain(error):
        status = getattr(item, "status", None)
        if (
            isinstance(item, VariationalRequestError) or status is not None
        ) and status == 422 and "skew" in str(item).lower():
            return True
    return False


def _is_rollback_failure(error: BaseException) -> bool:
    """识别人工执行器升级的第一腿回滚事故。"""
    return any("回滚失败" in str(item) for item in _exception_chain(error))


def _is_safe_open_rejection(error: SystemExit) -> bool:
    """识别尚未成交前因条件变化而安全拒绝的开仓结果。"""
    message = str(error)
    markers = (
        "保证金不足",
        "已有目标持仓",
        "当前不可交易",
        "距 XAUS 休市",
        "缺少距下次休市时间",
        "kill switch",
        "目标名义过小",
        "超过硬上限",
    )
    return any(marker in message for marker in markers)


async def _load_funding_rates_with_overrides(
    var: Any,
    structure: execution.CarryStructure,
    overrides: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    """读取目标结构费率，并只应用由权威休市状态确定的覆盖值。"""
    rates: dict[str, Decimal] = {}
    for leg in structure.legs:
        if leg.underlying in overrides:
            rates[leg.underlying] = overrides[leg.underlying]
        else:
            rates[leg.underlying] = await execution._funding_rate_for_leg(var, leg)
    return rates


async def _switch_readiness(
    var: Any,
    *,
    target: execution.CarryStructure,
    funding_availability: Mapping[str, FundingAvailability],
    funding_rate_overrides: Mapping[str, Decimal],
    schedule: SwapTradingSchedule | None,
    schedule_error: str | None,
    market_status: str | None,
    auto_open: bool,
    auto_open_notional: Decimal,
    daily_attempts: int,
    incident: bool,
) -> tuple[bool, str]:
    """在平旧结构前验证不会确定性阻止目标开仓的只读条件。"""
    if not auto_open:
        return False, "自动开仓已关闭，不能执行结构切换"
    if incident:
        return False, (
            "既有 INCIDENT（auto_open_incident / switch_incident）未解除，"
            "不能执行结构切换"
        )
    if daily_attempts >= MAX_DAILY_OPEN_ATTEMPTS:
        return False, f"当日切换开仓尝试已达上限 {MAX_DAILY_OPEN_ATTEMPTS} 次"
    try:
        execution._validate_notional(auto_open_notional)
        # 三腿结构的空腿权重为二，平旧仓前确认其不会必然超过既有硬上限。
        for leg in target.legs:
            weighted_notional = auto_open_notional * leg.weight / target.legs[0].weight
            if weighted_notional > execution.MAX_NOTIONAL_USD:
                return False, f"目标 {target.name} 的 {leg.underlying} 名义 {weighted_notional} 超过硬上限 {execution.MAX_NOTIONAL_USD}，保留原持仓"
    except SystemExit as exc:
        return False, str(exc)

    unavailable_reason = _unusable_funding_reason(funding_availability)
    if unavailable_reason is not None:
        return False, f"目标结构费率不可用，暂不切换：{unavailable_reason}"
    if target.has_xaus:
        if schedule_error is not None:
            return False, f"XAUS 时段元数据不可用，暂不切换：{schedule_error}"
        if schedule is None or not schedule.metadata_is_fresh:
            detail = schedule.reason if schedule is not None else "无解析结果"
            return False, f"XAUS 时段元数据不安全，暂不切换：{detail}"
        if market_status != "open" or not schedule.is_tradable:
            return False, f"XAUS 当前不可交易，暂不切换：{schedule.reason}"
        if schedule.time_until_close is None:
            return False, "XAUS 缺少距下次休市时间，暂不切换"
        if schedule.time_until_close <= MIN_TIME_TO_CLOSE:
            return False, (
                f"XAUS 距休市 {schedule.time_until_close}，"
                f"未超过最短开仓窗口 {MIN_TIME_TO_CLOSE}"
            )
    try:
        await _load_funding_rates_with_overrides(
            var,
            target,
            funding_rate_overrides,
        )
    except Exception as exc:  # noqa: BLE001 费率未刷新时保留旧结构更安全
        return False, f"目标结构费率读取失败，暂不切换：{type(exc).__name__}: {exc}"
    return True, f"目标结构 {target.name} 的切换前置条件已满足"


async def _try_auto_open(
    var: Any,
    *,
    structure: execution.CarryStructure | str,
    target_structure: execution.CarryStructure | None,
    positions: Mapping[str, Position],
    schedule: SwapTradingSchedule | None,
    schedule_error: str | None,
    market_status: str | None,
    funding_availability: Mapping[str, FundingAvailability],
    kill_switch_path: Path,
    auto_open: bool,
    auto_open_notional: Decimal,
    daily_attempts: int,
    incident: bool,
    dry_run: bool,
    audit_path: Path,
    observed_at: datetime,
    funding_rate_overrides: Mapping[str, Decimal] | None = None,
    enforce_min_carry: bool = True,
) -> AutoOpenResult:
    """逐项失败关闭地判定入场，并把成交委托给人工执行器。"""
    selected = execution.resolve_structure(structure)
    recorder = _SwitchTradeRecorder(var)
    recorder.phase = "open"

    def result(*args: Any, **kwargs: Any) -> AutoOpenResult:
        """所有返回分支均以本轮成功接受的平仓单为依据，拒单和空仓不计。"""
        return AutoOpenResult(
            *args,
            **kwargs,
            did_close_this_round=not dry_run and any(
                record.get("reduce_only") is True and record["status"] == "succeeded"
                for record in recorder.records["open"]
            ),
        )

    def skip(
        reason: str,
        *,
        status: str = "healthy",
        result_code: int = 0,
        attempted: bool = False,
        incident_state: bool = incident,
        attempts: int | None = None,
    ) -> AutoOpenResult:
        recorded_attempts = daily_attempts if attempts is None else attempts
        _append_audit(
            audit_path,
            {
                "timestamp": observed_at,
                "event": "auto_open_skipped",
                "reason": reason,
                "attempted": attempted,
                "daily_open_attempts": recorded_attempts,
                "dry_run": dry_run,
            },
        )
        return result(
            attempted=attempted,
            conclusion=reason,
            daily_attempts=recorded_attempts,
            status=status,
            result_code=result_code,
            incident=incident_state,
        )

    if not auto_open:
        return skip("自动开仓已由 --no-auto-open 关闭")
    if incident:
        return skip(
            "自动开仓已因既有 INCIDENT（auto_open_incident / switch_incident）停止，"
            "需人工解除后才能恢复",
            status="incident",
            result_code=1,
            incident_state=True,
        )
    if daily_attempts >= MAX_DAILY_OPEN_ATTEMPTS:
        return skip(
            f"当日自动开仓尝试已达上限 {MAX_DAILY_OPEN_ATTEMPTS} 次"
        )
    if any(not position.is_flat for position in positions.values()):
        detail = " ".join(
            f"{leg.underlying}={positions[leg.underlying].signed_size}"
            for leg in selected.legs
        )
        return skip(
            f"账户并非结构全部为空仓：{detail}"
        )
    if kill_switch_path.exists():
        return skip("kill switch 已激活，禁止自动开仓")

    try:
        target_notional = execution._validate_notional(auto_open_notional)
    except SystemExit as exc:
        return skip(str(exc))

    if target_structure is None:
        return skip("无法判定当前时段目标结构，禁止自动开仓")
    if selected.name != target_structure.name:
        return skip(
            f"待开结构 {selected.name} 不是当前时段目标结构 "
            f"{target_structure.name}，禁止自动开仓"
        )

    unavailable_reason = _unusable_funding_reason(funding_availability)
    if unavailable_reason is not None:
        return skip(f"费率不可用，禁止自动开仓：{unavailable_reason}")

    if selected.has_xaus:
        if schedule_error is not None:
            return skip(f"XAUS 时段元数据不可用，禁止自动开仓：{schedule_error}")
        if schedule is None or not schedule.metadata_is_fresh:
            detail = schedule.reason if schedule is not None else "无解析结果"
            return skip(f"XAUS 时段元数据不安全，禁止自动开仓：{detail}")
        if market_status != "open" or not schedule.is_tradable:
            return skip(f"XAUS 当前不可交易，禁止自动开仓：{schedule.reason}")
        if schedule.time_until_close is None:
            return skip("XAUS 缺少距下次休市时间，禁止自动开仓")
        if schedule.time_until_close <= MIN_TIME_TO_CLOSE:
            return skip(
                f"XAUS 距休市 {schedule.time_until_close}，"
                f"未超过最短开仓窗口 {MIN_TIME_TO_CLOSE}"
            )

    try:
        rates = await _load_funding_rates_with_overrides(
            var,
            selected,
            funding_rate_overrides or {},
        )
        net_carry = execution._weighted_net_carry(selected, rates)
    except Exception as exc:  # noqa: BLE001 入场数据不确定时只跳过，不升级故障
        return skip(f"净 carry 读取失败，按不确定处理：{type(exc).__name__}: {exc}")

    if enforce_min_carry and net_carry < MIN_ENTRY_CARRY_ANNUAL:
        return skip(
            f"净 carry {net_carry:.4%} 低于入场阈值 "
            f"{MIN_ENTRY_CARRY_ANNUAL:.4%}"
        )

    attempt_number = daily_attempts if dry_run else daily_attempts + 1
    _append_audit(
        audit_path,
        {
            "timestamp": observed_at,
            "event": "auto_open_attempt",
            "structure": selected.name,
            "notional_usd": target_notional,
            "net_carry_annual": net_carry,
            "daily_open_attempts": attempt_number,
            "dry_run": dry_run,
        },
    )
    try:
        await execution.cmd_open(
            recorder,
            target_notional,
            structure=selected,
            yes=True,
            dry_run=dry_run,
            now=observed_at,
        )
    except SystemExit as exc:
        if _is_skew_rejection(exc):
            conclusion = (
                f"{selected.legs[0].underlying} 开仓因 OI 偏斜被拒；"
                "本轮结束，下一轮退避后可重试："
                f"{exc}"
            )
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "auto_open_skew_rejected",
                    "message": conclusion,
                    "daily_open_attempts": attempt_number,
                },
            )
            return result(
                True, conclusion, attempt_number
            )
        if _is_rollback_failure(exc):
            conclusion = f"自动开仓回滚失败，进入 INCIDENT：{exc}"
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "auto_open_incident",
                    "message": conclusion,
                    "daily_open_attempts": attempt_number,
                },
            )
            notify("Swap carry 自动开仓 INCIDENT", conclusion)
            return result(
                True,
                conclusion,
                attempt_number,
                status="incident",
                result_code=1,
                incident=True,
            )
        if _is_safe_open_rejection(exc):
            return skip(str(exc), attempted=True, attempts=attempt_number)
        conclusion = f"自动开仓失败：{exc}"
    except Exception as exc:  # noqa: BLE001 报价或执行器未知错误必须显著记录
        if _is_skew_rejection(exc):
            conclusion = (
                f"{selected.legs[0].underlying} 开仓因 OI 偏斜被拒；"
                "本轮结束，下一轮退避后可重试："
                f"{exc}"
            )
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "auto_open_skew_rejected",
                    "message": conclusion,
                    "daily_open_attempts": attempt_number,
                },
            )
            return result(
                True, conclusion, attempt_number
            )
        conclusion = f"自动开仓失败：{type(exc).__name__}: {exc}"
    else:
        conclusion = (
            f"自动开仓 dry-run 完成：每腿名义 ${target_notional}"
            if dry_run
            else f"自动开仓完成：每腿目标名义 ${target_notional}"
        )
        _append_audit(
            audit_path,
            {
                "timestamp": observed_at,
                "event": "auto_open_dry_run" if dry_run else "auto_open_succeeded",
                "message": conclusion,
                "daily_open_attempts": attempt_number,
            },
        )
        return result(
            True,
            conclusion,
            attempt_number,
            status="dry_run" if dry_run else "healthy",
        )

    _append_audit(
        audit_path,
        {
            "timestamp": observed_at,
            "event": "auto_open_failed",
            "message": conclusion,
            "daily_open_attempts": attempt_number,
        },
    )
    notify("Swap carry 自动开仓失败", conclusion)
    return result(
        True,
        conclusion,
        attempt_number,
        status="action_failed",
        result_code=1,
    )


async def _close_leg(
    var: Any,
    position: Position,
    leg: execution.CarryLeg,
    *,
    dry_run: bool,
    audit_path: Path,
    observed_at: datetime,
) -> None:
    """有限重试 reduce_only 平掉一腿；认证类错误绝不静默重试。"""
    if position.is_flat:
        return
    last_error: Exception | None = None
    for attempt in range(1, CLOSE_RETRIES + 1):
        try:
            quote = await execution._close_position_quote(var, leg, position)
            if quote is None:
                return
            action = (
                f"reduce_only {quote.side.value.lower()} "
                f"{leg.underlying} {quote.qty}"
            )
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "close_attempt",
                    "market": leg.underlying,
                    "attempt": attempt,
                    "action": action,
                    "dry_run": dry_run,
                },
            )
            if dry_run:
                print(f"[DRY-RUN] 将执行：{action}")
                return
            print(f">>> 守护进程执行：{action}")
            result = await execution._accept_quote(var, quote, reduce_only=True)
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "close_succeeded",
                    "market": leg.underlying,
                    "attempt": attempt,
                    "result": execution._format_result(result),
                },
            )
            return
        except (VariationalJurisdictionError, VariationalAuthError):
            raise
        except Exception as exc:  # noqa: BLE001 平仓必须覆盖报价和 accept 的失败
            last_error = exc
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "close_failed",
                    "market": leg.underlying,
                    "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            if attempt < CLOSE_RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
    raise CloseActionError(
        f"{leg.underlying} 平仓连续失败 {CLOSE_RETRIES} 次：{last_error}"
    ) from last_error


async def _flatten(
    var: Any,
    *,
    structure: execution.CarryStructure | str,
    positions: Mapping[str, Position],
    xaus_known_closed: bool,
    dry_run: bool,
    audit_path: Path,
    observed_at: datetime,
) -> FlattenResult:
    """按时段能力清空仓位；XAUS 明确休市时保留显著待处理状态。"""
    selected = execution.resolve_structure(structure)
    if all(position.is_flat for position in positions.values()):
        return FlattenResult(True, False, "当前已空仓")

    if selected.has_xaus and xaus_known_closed:
        for leg in selected.legs:
            if leg.underlying == "XAUS":
                continue
            await _close_leg(
                var,
                positions[leg.underlying],
                leg,
                dry_run=dry_run,
                audit_path=audit_path,
                observed_at=observed_at,
            )
        if not positions["XAUS"].is_flat:
            message = "XAUS 当前休市无法平仓，已记录待处理状态"
            print(f"🚨 {message}")
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "pending_xaus_close",
                    "message": message,
                    "dry_run": dry_run,
                },
            )
            return FlattenResult(False, True, message)
        return FlattenResult(True, False, "其余腿已平仓")

    for leg in selected.legs:
        await _close_leg(
            var,
            positions[leg.underlying],
            leg,
            dry_run=dry_run,
            audit_path=audit_path,
            observed_at=observed_at,
        )
    return FlattenResult(True, False, "平仓动作已完成" if not dry_run else "已列出平仓动作")


def _dry_run_http_guard(function):
    """在整个守护轮次阻止真实客户端的所有非 GET 请求，包括询价回退。"""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        token = READ_ONLY_HTTP.set(READ_ONLY_HTTP.get() or kwargs.get("dry_run", False))
        try:
            return await function(*args, **kwargs)
        finally:
            READ_ONLY_HTTP.reset(token)
    return wrapped


async def _available_account_margin(var: Any) -> Decimal:
    """按真实接口字段计算全账户可用保证金。

    /portfolio 无 available_margin，必须自算：权益为 balance + upnl；
    此处扣除 /positions 全部持仓的公式要求值 initial_margin（不含 allocation 追加部分）。
    这是基于公式要求值的可用额度估计，不能据此认定实际隔离桶金额；
    必须遍历全部持仓，包括结构外的 BTC 和隔离腿。
    """
    def read_field(record: object, field: str, source: str) -> Decimal:
        """缺失和非法数值均报告具体接口字段，禁止默认按零处理。"""
        label = f"{source}.{field}"
        if not isinstance(record, Mapping) or field not in record:
            raise ValueError(f"缺少字段 {label}")
        return execution._decimal(record[field], label=label)

    portfolio = await var.raw("/portfolio")
    equity = read_field(portfolio, "balance", "/portfolio") + read_field(
        portfolio, "upnl", "/portfolio"
    )
    occupied = Decimal("0")
    for index, position in enumerate(_position_items(await var.get_positions())):
        source = f"/positions[{index}]"
        initial_margin = read_field(position, "initial_margin", source)
        if initial_margin < 0:
            raise ValueError(f"{source}.initial_margin 不得为负")
        occupied += initial_margin
    return equity - occupied


def _actual_allocation(snapshot: Mapping[str, Any]) -> Decimal:
    """实际强平距离反推真实桶；initial_margin 是公式要求值，不含 allocation 追加部分。"""
    distance = execution._decimal(snapshot["distance"], label="实际强平距离")
    notional = execution._decimal(snapshot["notional"], label="名义", positive=True)
    maintenance = execution._decimal(snapshot["maintenance_margin"], label="维持保证金")
    if not 0 <= maintenance < notional:
        raise ValueError("维持保证金必须在零和名义之间")
    return distance * (notional - maintenance) + maintenance


def _allocation_state_path(state_path: Path) -> Path:
    """兼容旧文件名一次；同时持有新旧锁，旧进程占锁时拒绝迁移。"""
    stem = state_path.stem
    prefix = stem[:-6] if stem.endswith("_state") else stem
    canonical = state_path.with_name(prefix + "_allocation_state.json")
    legacy = state_path.with_suffix(state_path.suffix + ".allocation.json")
    if legacy.exists() and not canonical.exists():
        with canonical.with_suffix(".json.lock").open("a") as new_lock:
            fcntl.flock(new_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with legacy.with_suffix(".json.lock").open("a") as old_lock:
                fcntl.flock(old_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if legacy.exists() and not canonical.exists():
                    # 原子改名完整保留台账，包括未识别字段；损坏台账仍由读仓分支拒绝。
                    legacy.rename(canonical)
    return canonical


def _reset_allocation_counters(ledger_path: Path, now: datetime) -> None:
    """显式修复旧口径误计的异常额度；不连接交易所，不启动完整轮次。"""
    if not ledger_path.exists():
        print("没有补仓台账，无需重置")
        return
    with ledger_path.with_suffix(".json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = json.loads(ledger_path.read_text(encoding="utf-8"))
        previous = saved.get("abnormal_count", 0)
        if type(previous) is not int or previous < 0:
            raise ValueError("异常补仓计数损坏，拒绝重置")
        if previous:
            saved["counter_reset"] = {
                "at": now.isoformat(), "previous_abnormal_count": previous,
                "reason": "实际强平距离口径修正，显式重置异常计数",
            }
            saved["abnormal_count"] = 0
            _write_json(ledger_path, saved)
    print(f"异常补仓计数已重置：{previous} → 0；常规用量保留")


async def _maintain_isolated_allocation(
    var: Any, *, underlying: str, mode: MarginModeStatus, dry_run: bool,
    observed_at: datetime, ledger_path: Path, audit_path: Path,
) -> dict[str, object]:
    """单腿最多发送一次目标总额请求；计数先落盘，异常不越过本分支。"""
    result: dict[str, object] = {"underlying": underlying, "level": "info", "attempted": False}
    lock = None
    ledger_ready = False
    charged = False
    regular = abnormal = attempts = 0
    last_success = None

    def persist_counts():
        """旧 attempts 仅供兼容展示；异常预占在成功回读后转为常规计数。"""
        _write_json(ledger_path, {"date": today, "attempts": attempts,
                    "regular_count": regular, "abnormal_count": abnormal,
                    "last_success_at": last_success})
        result.update(daily_attempts=attempts, daily_regular_count=regular,
                      daily_abnormal_count=abnormal, last_success_at=last_success)

    try:
        if not mode.isolated:
            result["message"] = "全仓腿无独立保证金桶，由账户级保证金率兜底"
            return result
        if mode.source == "保守默认":
            result.update(level="warning", message=(
                "保证金模式未经确认：supported_assets.isolated_only 与报价 "
                "margin_requirements.margin_mode 均未提供有效模式证据，保守拒绝补仓"
            ))
            return result
        target_distance = execution._decimal(
            os.environ.get("TARGET_LIQUIDATION_DISTANCE", str(TARGET_LIQUIDATION_DISTANCE)),
            label="目标强平距离", positive=True,
        )
        # 环境只能收紧金额与次数硬上限，不允许提高。
        cap = min(MAX_ALLOCATION_USD, execution._decimal(
            os.environ.get("MAX_ALLOCATION_USD", str(MAX_ALLOCATION_USD)),
            label="目标桶上限", positive=True,
        ))
        limit = min(MAX_DAILY_ALLOCATION_ATTEMPTS, int(os.environ.get(
            "MAX_DAILY_ALLOCATION_ATTEMPTS", str(MAX_DAILY_ALLOCATION_ATTEMPTS))))
        if limit < 0:
            raise ValueError("每日补仓次数不得为负")
        # 锁覆盖读仓、余额检查、计数和回读，避免重叠轮次重复补同一账户。
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        lock = ledger_path.with_suffix(ledger_path.suffix + ".lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        today = observed_at.date().isoformat()
        saved = {}
        if ledger_path.exists():
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
            saved_date = datetime.strptime(saved["date"], "%Y-%m-%d").date()
            saved_attempts = saved["attempts"]
            if type(saved_attempts) is not int or saved_attempts < 0 or saved_date > observed_at.date():
                raise ValueError("补仓计数台账损坏，禁止写接口")
            if saved_date == observed_at.date():
                attempts = saved_attempts
        if saved:
            # 旧版总次数无法区分成功失败，保留为常规用量，不凭空记异常。
            for field in ("regular_count", "abnormal_count"):
                value = saved.get(field, saved["attempts"] if field == "regular_count" else 0)
                if type(value) is not int or value < 0:
                    raise ValueError("补仓分类计数台账损坏，禁止写接口")
            if saved_date == observed_at.date():
                regular = saved.get("regular_count", attempts)
                abnormal = saved.get("abnormal_count", 0)
            last_success = saved.get("last_success_at")
            if last_success is not None:
                success_time = execution._guard_timestamp(last_success)
                if success_time is None or success_time > observed_at:
                    raise ValueError("上次成功补仓时间无效，禁止写接口")
        ledger_ready = True
        result.update(daily_attempts=attempts, daily_regular_count=regular,
                      daily_abnormal_count=abnormal, last_success_at=last_success)
        if abnormal >= MAX_DAILY_ALLOCATION_ERRORS:
            result.update(level="critical", message="当日异常补仓次数已达上限，停止补仓")
            return result
        current = await var.get_isolated_allocation(underlying)
        target = required_allocation(current["notional"], current["maintenance_margin"], target_distance)
        # 向上取整到美分，避免截断导致目标距离略低于阈值。
        target = target.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        initial = _actual_allocation(current)
        distance = execution._decimal(current["distance"], label="当前强平距离")
        if initial < 0:
            raise ValueError("当前桶不得为负")
        result.update(initial_margin=current["initial_margin"],
                      current_allocation=initial, target_allocation=target,
                      distance=distance, target_distance=target_distance,
                      before_allocation=initial, before_distance=distance)
        if distance >= target_distance:
            result["message"] = "强平距离已达标，无需补仓"
        elif target > cap:
            result.update(level="warning", message="目标桶超过 MAX_ALLOCATION_USD，拒绝补仓，保留平仓风控兜底")
        elif target <= initial:
            raise ValueError("公式目标不高于当前桶，禁止减保证金，等待下一轮风控")
        elif regular >= limit:
            result.update(level="warning", message="当日常规补仓次数已达上限")
        elif last_success and (observed_at - execution._guard_timestamp(last_success)).total_seconds() < MIN_ALLOCATION_INTERVAL_SECONDS:
            result["message"] = "距离上次成功补仓不足 15 分钟，跳过常规补仓"
        else:
            available = await _available_account_margin(var)
            result["available_margin"] = available
            result["additional_allocation"] = target - initial
            if available < target - initial:
                result.update(level="warning", message="账户可用保证金不足，拒绝补仓")
            elif dry_run:
                result["message"] = "dry-run：仅计算目标桶，不发送 POST"
            else:
                # 写请求前预占异常额度；转换等待超时单独释放，本轮不重发。
                attempts += 1
                abnormal += 1
                charged = True
                persist_counts()
                result.update(attempted=True)
                _append_audit(audit_path, {"timestamp": observed_at, "event": "allocation_attempt", **result})
                result["post_succeeded"] = False
                conversion_id = await var.set_isolated_allocation(underlying, target)
                result.update(post_succeeded=True, conversion_id=conversion_id)
                try:
                    conversion_status = await var.wait_allocation_conversion(
                        conversion_id, timeout=float(os.environ.get(
                            "ALLOCATION_CONVERSION_TIMEOUT_SECONDS", "30")),
                    )
                except TimeoutError:
                    # 未确认不能认定失败，也不能回读旧仓位后误扣异常额度。
                    abnormal -= 1
                    persist_counts()
                    result.update(level="warning", conversion_status="pending",
                                  message="保证金转换确认超时，本轮不再重试，不计异常额度")
                    return result
                result["conversion_status"] = conversion_status
                if conversion_status == "rejected":
                    result["post_succeeded"] = False
                    raise ValueError("保证金转换被拒绝（rejected）")
                if conversion_status != "confirmed":
                    raise ValueError(f"未知保证金转换状态：{conversion_status!r}")
                try:
                    cost.record_allocation(cost.ACTIVE_PATH.get(), observed_at.isoformat(), underlying, result)
                except Exception as exc:  # noqa: BLE001 不干扰已确认转换的读回检查
                    logging.getLogger(__name__).warning("保证金转换成本记账失败：%s", exc)
                # 目标总额幂等；本轮只 POST 一次，沿用成交确认的最终一致轮询。
                for attempt in range(1, execution._FLAT_TRIES + 1):
                    result["readback_attempts"] = attempt
                    try:
                        # 客户端每次调用均重新 GET /positions，无本地仓位缓存。
                        updated = await var.get_isolated_allocation(underlying)
                        new_distance = execution._decimal(updated["distance"], label="补仓后强平距离")
                        new_allocation = _actual_allocation(updated)
                        result.update(distance=new_distance, current_allocation=new_allocation,
                                      after_distance=new_distance, after_allocation=new_allocation)
                        result.pop("readback_error", None)
                    except Exception as exc:  # noqa: BLE001 暂时读仓失败也允许有限重读
                        result["readback_error"] = f"{type(exc).__name__}: {exc}"
                    if "readback_error" not in result and new_distance >= target_distance:
                        previous_success = last_success
                        regular += 1
                        abnormal -= 1
                        last_success = observed_at.isoformat()
                        try:
                            persist_counts()
                        except OSError:
                            # 台账写失败不是读仓失败，不得重试迁移或重复扣减额度。
                            regular -= 1
                            abnormal += 1
                            last_success = previous_success
                            raise
                        result["message"] = "POST 成功，补仓后回读确认强平距离达标"
                        break
                    if attempt < execution._FLAT_TRIES:
                        await asyncio.sleep(execution._POLL_DELAY_S)
                else:
                    result.update(level="critical", message=(
                        "POST 成功但回读未达标，本轮不再重复 POST"
                        if "readback_error" not in result else
                        f"POST 成功但回读校验失败，本轮不再重复 POST：{result['readback_error']}"
                    ))
    except Exception as exc:  # noqa: BLE001 补仓失败不得阻断平仓风控
        if ledger_ready and not charged and not dry_run:
            abnormal += 1
            try:
                persist_counts()
            except OSError:
                logging.getLogger(__name__).critical("异常补仓计数写入失败", exc_info=True)
        if result.get("post_succeeded"):
            prefix = "POST 成功但回读校验失败，本轮不再重复 POST"
        elif result.get("post_succeeded") is False:
            prefix = "POST 失败（含超时结果未知），补仓已停止，本轮不重试"
        else:
            prefix = "补仓已停止，本轮不重试"
        result.update(level="critical" if result["attempted"] or abnormal >= MAX_DAILY_ALLOCATION_ERRORS else "warning",
                      message=f"{prefix}：{type(exc).__name__}: {exc}")
    finally:
        if lock is not None:
            lock.close()
        result.setdefault("daily_regular_count", regular)
        result.setdefault("daily_abnormal_count", abnormal)
        message = str(result.get("message", "补仓未执行"))
        detail = f"；今日常规 {regular}/{MAX_DAILY_ALLOCATION_ATTEMPTS}，异常 {abnormal}/{MAX_DAILY_ALLOCATION_ERRORS}"
        if "target_allocation" in result:
            detail += (
                f"；当前桶 ${result['current_allocation']} / 目标桶 ${result['target_allocation']}"
                f" / 距离 {result['distance']:.2%}"
            )
        if "initial_margin" in result:
            detail += f"；IM 公式要求值 ${result['initial_margin']}（不含 allocation 追加部分）"
        if "after_allocation" in result:
            detail += (
                f"；补仓前后桶 ${result['before_allocation']} → ${result['after_allocation']}"
                f" / 距离 {result['before_distance']:.2%} → {result['after_distance']:.2%}"
            )
        print(f"{underlying} 保证金：{message}{detail}")
        logging.getLogger(__name__).log(
            {"info": logging.INFO, "warning": logging.WARNING, "critical": logging.CRITICAL}[str(result["level"])],
            "%s 保证金：%s%s", underlying, message, detail,
        )
        try:
            _append_audit(audit_path, {"timestamp": observed_at, "event": "allocation_result", **result})
        except OSError:
            logging.getLogger(__name__).critical("保证金审计日志写入失败", exc_info=True)
        if result["level"] == "critical":
            try:
                notify(f"{underlying} 保证金补仓告警", message + detail)
            except Exception:  # noqa: BLE001 通知失败不得阻断平仓风控
                logging.getLogger(__name__).exception("保证金补仓通知失败")
    return result


@cost.ledger_session
@_dry_run_http_guard
async def run_once(
    var: Any,
    *,
    structure: execution.CarryStructure | str = execution.XAUS_XAU,
    dry_run: bool = False,
    auto_open: bool = True,
    auto_switch: bool = True,
    auto_open_notional: Decimal = AUTO_OPEN_NOTIONAL_USD,
    switch_lead_time: timedelta = SWITCH_LEAD_TIME,
    rehearsal_lead: timedelta = REHEARSAL_LEAD,
    now: datetime | None = None,
    kill_switch_path: Path | None = None,
    heartbeat_path: Path | None = None,
    state_path: Path | None = None,
    audit_path: Path | None = None,
    switch_history_path: Path | None = None,
    cost_ledger_path: Path | None = None,
    cost_reconciliation_path: Path | None = None,
    funding_recon_path: Path | None = None,
    funding_samples_path: Path | None = None,
    funding_deviation_threshold: Decimal = Decimal(".20"),
    polling_mode: str | None = None,
) -> int:
    """先执行全部平仓风控，再按净 carry 择优并原子切换结构。"""
    kill_switch_path = DEFAULT_KILL_SWITCH if kill_switch_path is None else Path(kill_switch_path)
    heartbeat_path = DEFAULT_HEARTBEAT if heartbeat_path is None else Path(heartbeat_path)
    state_path = DEFAULT_STATE if state_path is None else Path(state_path)
    audit_path = DEFAULT_AUDIT_LOG if audit_path is None else Path(audit_path)
    switch_history_path = DEFAULT_SWITCH_HISTORY if switch_history_path is None else Path(switch_history_path)
    configured_structure = execution.resolve_structure(structure)
    selected = configured_structure
    if switch_lead_time < timedelta(0):
        raise ValueError("switch_lead_time 不得为负数")
    if rehearsal_lead < timedelta(0):
        raise ValueError("rehearsal_lead 不得为负数")
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("now 必须包含时区")
    observed_at = observed_at.astimezone(timezone.utc)
    session_expiry = get_session_expiry(now=observed_at)
    session_expires_at = (
        session_expiry.expires_at.isoformat()
        if session_expiry is not None
        else None
    )
    session_hours_left = (
        session_expiry.hours_left if session_expiry is not None else None
    )
    session_is_expired = (
        session_expiry is not None
        and session_expiry.remaining <= timedelta(0)
    )
    previous_failures = _read_failure_count(state_path)
    daily_open_attempts, auto_open_incident = _read_auto_open_state(
        state_path, observed_at
    )
    switch_incident = _read_switch_incident(state_path)
    try:
        saved_rehearsals = json.loads(state_path.read_text(encoding="utf-8"))
        rehearsal_windows = dict(saved_rehearsals.get("rehearsal_windows", {}))
        last_rehearsal = saved_rehearsals.get("last_rehearsal")
    except FileNotFoundError:
        rehearsal_windows, last_rehearsal = {}, None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        rehearsal_windows = {}
        last_rehearsal = {
            "conclusion": "blocked", "blocking_reasons": [f"预演状态读取失败：{exc}"],
            "timestamp": observed_at.isoformat(),
        }
    rehearsal_blocked = bool(last_rehearsal and last_rehearsal.get("conclusion") == "blocked")
    exit_carry_since = _read_state_timestamp(state_path, "exit_carry_since")
    last_closed_at = _read_state_timestamp(state_path, "last_closed_at")
    last_switch_at = _read_state_timestamp(state_path, "last_switch_at")
    last_session_expiry_alert_at = _warn_session_expiry(
        session_expiry,
        observed_at=observed_at,
        previous_alert_at=_read_session_alert_at(state_path),
        audit_path=audit_path,
    )
    consecutive_failures = previous_failures
    conclusion = "本轮尚未完成"
    auto_open_attempted = False
    auto_open_conclusion = "未进入自动开仓判定"
    auto_switch_attempted = False
    auto_switch_conclusion = (
        "未进入自动切换判定" if auto_switch else "自动切换已由 --no-auto-switch 关闭"
    )
    result_code = 1
    round_status = "blocked"
    close_attempted = False
    positions: dict[str, Position] = {}
    prices: dict[str, Decimal | None] = {
        leg.underlying: None for leg in configured_structure.legs
    }
    portfolio_positions: dict[str, Position] = {}
    account_positions_payload: object | None = None
    current_structure: execution.CarryStructure | None = None
    target_structure: execution.CarryStructure | None = None
    target_structure_reason = "尚未评估候选结构"
    candidate_structures: dict[str, dict[str, Any]] = {}
    best_structure: execution.CarryStructure | None = None
    selection_decision: dict[str, Any] = {"allowed": False, "reason": "优先执行持仓风控，尚未选择结构"}
    structure_detection_error: str | None = None
    target_funding_availability: dict[str, FundingAvailability] = {}
    target_funding_overrides: dict[str, Decimal] = {}
    schedule: SwapTradingSchedule | None = None
    schedule_error: str | None = None
    market_status: str | None = None
    metadata: object | None = None
    metadata_error: str | None = None
    funding_availability: dict[str, FundingAvailability] = {}
    net_carry: Decimal | None = None
    exit_carry_observation = "本轮未评估退出 carry"
    margin_modes: dict[str, MarginModeStatus] = {}
    per_leg_liquidation: dict[str, dict[str, object]] = {}
    allocation_results: dict[str, dict[str, object]] = {}
    account_margin: AccountMarginHealth | None = None
    account_margin_error: str | None = None
    quote_cache: dict[
        tuple[str, str, int, str | None, str],
        Mapping[str, Any],
    ] = {}
    switch_record: dict[str, object] | None = None
    switch_recorder: _SwitchTradeRecorder | None = None
    switch_stage = "before_snapshot"
    switch_started = 0.0
    switch_phase_started = 0.0
    switch_record_written = False

    def persist_state(status: str, message: str, failures: int) -> None:
        """写状态时始终保留每日计数和不可自动清除的 INCIDENT。"""
        nonlocal round_status
        effective_status = (
            "incident" if auto_open_incident or switch_incident else status
        )
        round_status = effective_status
        _write_json(
            state_path,
            {**_state_payload(
                observed_at=observed_at,
                status=effective_status,
                message=message,
                consecutive_failures=failures,
                open_attempt_date=observed_at.date().isoformat(),
                daily_open_attempts=daily_open_attempts,
                auto_open_incident=auto_open_incident,
                switch_incident=switch_incident,
                exit_carry_since=exit_carry_since,
                last_closed_at=last_closed_at,
                last_session_expiry_alert_at=last_session_expiry_alert_at,
            ), "last_switch_at": last_switch_at, "last_rehearsal": last_rehearsal,
                "rehearsal_windows": rehearsal_windows,
                "rehearsal_blocked": rehearsal_blocked},
        )

    async def finalize_switch_failure(error: BaseException) -> None:
        """用当前可读事实补齐失败台账；记录失败不得遮蔽原始异常。"""
        nonlocal switch_record_written
        if switch_record is None or switch_record_written:
            return
        recorder = switch_recorder
        if recorder is not None:
            if switch_stage == "close":
                switch_record["close_phase"] = _phase_payload(
                    recorder,
                    "close",
                    started=switch_phase_started,
                    status="failed",
                )
            elif switch_stage in {"open", "self_check"}:
                if not isinstance(switch_record.get("open_phase"), Mapping) or (
                    switch_record["open_phase"].get("status") == "pending"  # type: ignore[union-attr]
                ):
                    switch_record["open_phase"] = _phase_payload(
                        recorder,
                        "open",
                        started=switch_phase_started,
                        status="failed",
                    )

        residual_state = "未知"
        has_residual: bool | None = None
        try:
            final_payload = await var.get_positions()
            residual_state, has_residual = _residual_state(final_payload)
            switch_record["after"] = await _switch_snapshot(
                var,
                positions_payload=final_payload,
                metadata=metadata,
            )
        except Exception as snapshot_error:  # noqa: BLE001 原错误优先
            switch_record["after_capture_error"] = (
                f"{type(snapshot_error).__name__}: {snapshot_error}"
            )
            known_positions = positions.values()
            if known_positions:
                has_residual = any(not position.is_flat for position in known_positions)
                residual_state = "残仓" if has_residual else "空仓"

        before = switch_record.get("before")
        after = switch_record.get("after")
        if isinstance(before, Mapping) and isinstance(after, Mapping):
            try:
                switch_record["measured_wear_usd"] = str(
                    execution._decimal(after.get("equity"), label="切换后权益")
                    - execution._decimal(before.get("equity"), label="切换前权益")
                )
            except ValueError:
                switch_record["measured_wear_usd"] = None

        if switch_stage == "open" and has_residual is False:
            switch_record["self_check"] = {
                "performed": True,
                "passed": None,
                "status": "开仓失败，账户为空仓",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "checks": {},
            }
        switch_record["status"] = "failed"
        switch_record["failure"] = {
            "stage": switch_stage,
            "error_type": type(error).__name__,
            "message": str(error),
            "residual_state": residual_state,
            "has_residual_position": has_residual,
        }
        switch_record["total_duration_ms"] = round(
            (time.perf_counter() - switch_started) * 1000,
            3,
        )
        try:
            _append_audit(switch_history_path, switch_record)
        except Exception as ledger_error:  # noqa: BLE001 原始交易错误优先
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "switch_ledger_write_failed",
                    "level": "critical",
                    "message": f"{type(ledger_error).__name__}: {ledger_error}",
                },
            )
        else:
            switch_record_written = True

    _append_audit(
        audit_path,
        {
            "timestamp": observed_at,
            "event": "round_started",
            "structure": selected.name,
            "dry_run": dry_run,
            "kill_switch": kill_switch_path.exists(),
            "auto_open": auto_open,
            "auto_switch": auto_switch,
            "switch_lead_time_seconds": switch_lead_time.total_seconds(),
            "auto_open_notional": auto_open_notional,
            "daily_open_attempts": daily_open_attempts,
            "auto_open_incident": auto_open_incident,
            "switch_incident": switch_incident,
            "exit_carry_since": exit_carry_since,
            "last_closed_at": last_closed_at,
            "session_expires_at": session_expires_at,
            "session_hours_left": session_hours_left,
        },
    )
    try:
        if session_is_expired:
            raise _SessionExpiredPreflight
        if auto_switch:
            account_positions_payload = await var.get_positions()
            portfolio_positions = _carry_positions_from_payload(
                account_positions_payload
            )
            detection = _detect_carry_structure(portfolio_positions)
            current_structure = detection.structure
            structure_detection_error = detection.error
            if current_structure is not None:
                selected = current_structure
            positions = {
                leg.underlying: portfolio_positions[leg.underlying]
                for leg in selected.legs
            }
        else:
            positions = await execution._get_positions(var, selected)

        try:
            metadata = await var.get_supported_assets()
        except Exception as exc:  # noqa: BLE001 费率有效性按失败关闭，持仓仍继续降险
            metadata_error = f"{type(exc).__name__}: {exc}"

        if metadata is None:
            schedule_error = metadata_error or "supported_assets 无数据"
        else:
            try:
                record = execution._instrument_record(
                    metadata,
                    execution.XAUS_LEG,
                )
                schedule = parse_trading_schedule(
                    record.get("trading_sessions"),
                    record.get("trading_schedule"),
                    record.get("market_status"),
                    observed_at,
                )
            except Exception as exc:  # noqa: BLE001 时段读取失败后仍要尝试降险
                schedule_error = f"{type(exc).__name__}: {exc}"
            else:
                raw_market_status = record.get("market_status")
                if (
                    not isinstance(raw_market_status, str)
                    or not raw_market_status.strip()
                ):
                    schedule_error = "XAUS market_status 元数据缺失"
                else:
                    market_status = raw_market_status.strip().lower()

        if not auto_switch:
            target_structure, target_structure_reason = _target_structure_for_schedule(
                schedule, market_status, switch_lead_time=switch_lead_time,
            )

        prices = {
            leg.underlying: execution._metadata_price(metadata, leg)
            for leg in selected.legs
        }
        funding_availability = _funding_availability_by_leg(
            selected,
            target_structure=target_structure,
            target_reason=target_structure_reason,
        )
        for leg in selected.legs:
            mode = _margin_mode_from_supported_assets(metadata, leg)
            if mode is not None:
                margin_modes[leg.underlying] = mode

        if target_structure is not None:
            (
                target_funding_availability,
                target_funding_overrides,
            ) = _target_funding_context(
                target_structure,
                target_reason=target_structure_reason,
            )

        notionals = {
            leg.underlying: _position_notional(
                positions[leg.underlying], prices[leg.underlying]
            )
            for leg in selected.legs
        }

        reason: str | None = None
        state_status = "healthy"
        long_close_due = False
        switch_ready = False

        # 优先级 1：kill switch 无条件高于所有其他判断。
        if kill_switch_path.exists():
            reason = "kill switch 已激活"
            state_status = "kill_switch_active"
        elif structure_detection_error is not None:
            reason = structure_detection_error
        else:
            # 优先级 2：缺腿、方向或结构权重比例失衡。
            reason = _imbalance_reason(selected, positions)

        all_flat = all(position.is_flat for position in positions.values())

        # 优先级 3：isolated 腿严格使用单腿强平价；全仓腿只记录该值。
        if reason is None and not all_flat:
            for leg in selected.legs:
                position = positions[leg.underlying]
                if position.is_flat:
                    continue
                mode = margin_modes.get(leg.underlying)
                if mode is None:
                    mode = await _resolve_margin_mode(
                        var,
                        metadata=metadata,
                        leg=leg,
                        position=position,
                        quote_cache=quote_cache,
                    )
                    margin_modes[leg.underlying] = mode
                try:
                    liquidation_info = await var.get_liquidation_info(
                        leg.underlying,
                        exact=True,
                    )
                    distance = _liquidation_distance(
                        liquidation_info,
                        position,
                        underlying=leg.underlying,
                    )
                    below_threshold = distance < LIQUIDATION_ALERT_RATIO
                    per_leg_liquidation[leg.underlying] = (
                        _per_leg_liquidation_payload(
                            mode=mode,
                            status=(
                                "低于阈值" if below_threshold else "有数据"
                            ),
                            distance=distance,
                        )
                    )
                    if mode.isolated and below_threshold:
                        reason = (
                            f"{leg.underlying} 强平距离 {distance:.2%} 低于阈值 "
                            f"{LIQUIDATION_ALERT_RATIO:.2%}"
                        )
                        break
                except Exception as exc:  # noqa: BLE001 是否退出取决于保证金模式
                    error = f"{type(exc).__name__}: {exc}"
                    per_leg_liquidation[leg.underlying] = (
                        _per_leg_liquidation_payload(
                            mode=mode,
                            status="无数据",
                            error=error,
                        )
                    )
                    if mode.isolated:
                        reason = (
                            f"{leg.underlying} 强平价不可用，按不安全处理：{exc}"
                        )
                        break

        # 每日短休市冻结窗内不主动询价，避免把计划内报价空窗误判成账户风险。
        in_short_closure_freeze = (
            selected.has_xaus
            and schedule is not None
            and schedule.metadata_is_fresh
            and schedule.closure_duration is not None
            and schedule.closure_duration <= LONG_CLOSURE_THRESHOLD
            and schedule.time_until_close is not None
            and schedule.time_until_close <= timedelta(minutes=PRE_CLOSE_MINUTES)
        )

        # 优先级 3b：全仓腿退出只看全账户权益对全部持仓维持保证金的倍数。
        if (
            reason is None
            and not all_flat
            and not in_short_closure_freeze
            and any(
            not margin_modes[leg.underlying].isolated
            for leg in selected.legs
            if not positions[leg.underlying].is_flat
            )
        ):
            try:
                account_margin = await _account_margin_health(
                    var,
                    selected=selected,
                    positions_payload=account_positions_payload,
                    quote_cache=quote_cache,
                )
            except Exception as exc:  # noqa: BLE001 全仓安全数据缺失必须降险
                account_margin_error = f"{type(exc).__name__}: {exc}"
                reason = (
                    "账户保证金率不可用，无法确认全仓腿安全："
                    f"{account_margin_error}"
                )
            else:
                if account_margin.ratio < ACCOUNT_MARGIN_RATIO_MIN:
                    reason = (
                        f"账户保证金率 {account_margin.ratio:.4f} 低于阈值 "
                        f"{ACCOUNT_MARGIN_RATIO_MIN}"
                    )

        if (
            reason is None
            and selected.has_xaus
            and not all_flat
            and any(value is None for value in notionals.values())
        ):
            reason = "无法确认结构各腿名义，不能验证失衡阈值"

        # 优先级 4：交易时段必须新鲜、完整；每日短休市明确穿越。
        if reason is None and selected.has_xaus and not all_flat:
            if schedule_error is not None:
                reason = f"XAUS 时段元数据不可用：{schedule_error}"
            elif schedule is None or not schedule.metadata_is_fresh:
                detail = schedule.reason if schedule is not None else "无解析结果"
                reason = f"XAUS 时段元数据不安全：{detail}"
            elif market_status == "open" and not schedule.is_tradable:
                reason = "XAUS market_status 与 trading_sessions 状态矛盾"
            elif schedule.closure_duration is None:
                reason = "XAUS 时段元数据缺少完整休市长度"
            elif schedule.closure_duration > LONG_CLOSURE_THRESHOLD:
                if not schedule.is_tradable:
                    reason = "XAUS 已进入长休市且仍有持仓"
                elif schedule.time_until_close is None:
                    reason = "XAUS 长休市前缺少剩余时间"
                elif schedule.time_until_close <= timedelta(
                    minutes=PRE_CLOSE_MINUTES
                ):
                    long_close_due = True
                    if not (auto_switch and auto_open):
                        reason = (
                            f"XAUS 长休市 {schedule.closure_duration} 将在 "
                            f"{schedule.time_until_close} 后开始"
                        )

        # 优先级 5：持仓出场只要求费率可读，与入场的时段目标校验独立。
        if reason is None and not all_flat:
            try:
                rates = await execution._load_funding_rates(var, selected)
                net_carry = execution._weighted_net_carry(selected, rates)
            except Exception as exc:  # noqa: BLE001 读取失败保留原计时
                exit_carry_observation = (
                    "退出 carry 读取失败，保留原计时，本轮不触发熔断："
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                if net_carry <= EXIT_CARRY_ANNUAL:
                    if exit_carry_since is None or exit_carry_since > observed_at:
                        exit_carry_since = observed_at
                    elapsed = observed_at - exit_carry_since
                    exit_carry_observation = (
                        f"净 carry {net_carry:.4%} 不高于退出阈值 "
                        f"{EXIT_CARRY_ANNUAL:.4%}，已持续 "
                        f"{elapsed}，熔断要求 {EXIT_CARRY_DURATION}"
                    )
                    if elapsed >= EXIT_CARRY_DURATION:
                        reason = exit_carry_observation
                        state_status = "exit_carry_triggered"
                else:
                    exit_carry_since = None
                    exit_carry_observation = (
                        f"净 carry {net_carry:.4%} 高于退出阈值 "
                        f"{EXIT_CARRY_ANNUAL:.4%}，持续计时已清零"
                    )
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "exit_carry_observed",
                    "message": exit_carry_observation,
                    "net_carry_annual": net_carry,
                    "threshold_annual": EXIT_CARRY_ANNUAL,
                    "exit_carry_since": exit_carry_since,
                    "required_duration_seconds": EXIT_CARRY_DURATION.total_seconds(),
                },
            )

        # 收益优化必须排在所有平仓风控之后，不能延误 kill switch 或缺腿退出。
        if reason is not None:
            selection_decision["reason"] = f"优先平仓，跳过结构选择：{reason}"
        selection_metadata_safe = bool(
            schedule and schedule.metadata_is_fresh and schedule.closure_duration is not None
            and schedule_error is None and market_status is not None
            and (market_status == "open") == schedule.is_tradable
        )
        if reason is None and auto_switch and not selection_metadata_safe:
            auto_switch_conclusion = f"XAUS 时段元数据不安全，暂不选择结构：{schedule_error or (schedule.reason if schedule else '无解析结果')}"
            selection_decision["reason"] = auto_switch_conclusion
        if reason is None and auto_switch and selection_metadata_safe:
            candidate_structures, best_structure = await _evaluate_carry_candidates(var, schedule, market_status)
            forced = bool(current_structure and current_structure.has_xaus and schedule
                          and schedule.metadata_is_fresh and schedule.is_tradable
                          and schedule.closure_duration is not None
                          and schedule.closure_duration > LONG_CLOSURE_THRESHOLD
                          and schedule.time_until_close is not None
                          and schedule.time_until_close <= switch_lead_time)
            target_structure = execution.XAU_XAUT if forced else best_structure
            selection_decision = _carry_switch_decision(
                candidate_structures, target_structure, current_structure,
                now=observed_at, last_switch_at=last_switch_at, forced=forced,
            )
            if forced:
                long_close_due = True
                # 无法建立安全结构时仍须在 lead time 内平掉 XAUS。
                selection_decision["allowed"] = candidate_structures["XAU_XAUT"]["available"]
            target_structure_reason = selection_decision["reason"]
            auto_switch_conclusion = target_structure_reason
            if target_structure is not None:
                target_funding_availability, target_funding_overrides = _target_funding_context(
                    target_structure, target_reason=target_structure_reason,
                )
                if all_flat:
                    selected = target_structure
                    positions = {leg.underlying: portfolio_positions[leg.underlying] for leg in selected.legs}
                    prices = {leg.underlying: execution._metadata_price(metadata, leg) for leg in selected.legs}
                    notionals = {leg.underlying: _position_notional(positions[leg.underlying], prices[leg.underlying]) for leg in selected.legs}
                    funding_availability = target_funding_availability
                    for leg in selected.legs:
                        mode = _margin_mode_from_supported_assets(metadata, leg)
                        if mode is not None:
                            margin_modes[leg.underlying] = mode
            _append_audit(audit_path, {"timestamp": observed_at, "event": "carry_structure_selection",
                                      "current_structure": current_structure.name if current_structure else None,
                                      "candidate_structures": candidate_structures,
                                      "best_structure": best_structure.name if best_structure else None,
                                      "target_structure": target_structure.name if target_structure else None,
                                      "selection_decision": selection_decision})

        # 所有持仓风控检查之后才预演；blocked 不得写入平仓 reason。
        if reason is None and auto_switch and not dry_run:
            window = None
            try:
                rehearsal_source = current_structure
                # 风控平仓后仍保留窗口禁入；下一次相反方向窗口可以重新预演。
                if rehearsal_source is None and rehearsal_blocked and last_rehearsal:
                    previous_target = last_rehearsal.get("direction", {}).get("to")
                    if previous_target in execution.STRUCTURES:
                        rehearsal_source = execution.resolve_structure(previous_target)
                if rehearsal_source is not None:
                    window = rehearsal_window(
                        rehearsal_source, schedule, metadata, observed_at,
                        switch_lead_time, rehearsal_lead,
                    )
                if window is not None:
                    window_id = window["window_id"]
                    if window_id in rehearsal_windows:
                        last_rehearsal = rehearsal_windows[window_id]
                    else:
                        report = {
                            "schema_version": 1, "kind": "rehearsal", **window,
                            "timestamp": observed_at.isoformat(),
                            "started_at": observed_at.isoformat(),
                            "conclusion": "blocked",
                            "blocking_reasons": ["预演中断或尚未完成"], "warnings": [],
                        }
                        # 先原子占用窗口；进程中断后同窗口保持阻断，不重复询价。
                        last_rehearsal = dict(report)
                        rehearsal_windows[window_id] = last_rehearsal
                        rehearsal_blocked = True
                        persist_state("rehearsal_blocked", "预演尚未完成", consecutive_failures)
                        report["blocking_reasons"] = []
                        try:
                            await asyncio.wait_for(
                                _perform_rehearsal(
                                    var, report=report, source=rehearsal_source,
                                    metadata=metadata, positions_payload=account_positions_payload,
                                    now=observed_at, notional=auto_open_notional,
                                    history_path=switch_history_path,
                                    slippage_warning_bp=REHEARSAL_SLIPPAGE_WARNING_BP,
                                ), timeout=REHEARSAL_TIMEOUT_SECONDS,
                            )
                        except (Exception, SystemExit) as exc:
                            report["conclusion"] = "blocked"
                            report["blocking_reasons"].append(
                                f"预演异常：{type(exc).__name__}: {exc}")
                        report["level"] = {
                            "ready": "info", "warning": "warning", "blocked": "critical",
                        }[report["conclusion"]]
                        _append_audit(switch_history_path, report)
                        _append_audit(audit_path, {**report, "event": "switch_rehearsal"})
                        last_rehearsal = {
                            key: report[key] for key in (
                                "timestamp", "window_id", "planned_at", "direction",
                                "conclusion", "blocking_reasons", "warnings",
                            )
                        }
                        rehearsal_windows[window_id] = last_rehearsal
                        rehearsal_blocked = report["conclusion"] == "blocked"
                        persist_state("rehearsal_blocked" if rehearsal_blocked else "healthy",
                                      f"预演 {report['conclusion']}", consecutive_failures)
                        logging.getLogger(__name__).log(
                            {"info": logging.INFO, "warning": logging.WARNING,
                             "critical": logging.CRITICAL}[report["level"]],
                            "结构切换预演 %s：%s", report["conclusion"],
                            report["blocking_reasons"] or report["warnings"] or "全部检查通过",
                        )
                        if rehearsal_blocked:
                            try:
                                notify("Swap carry 预演阻断", "；".join(report["blocking_reasons"]))
                            except Exception:
                                logging.getLogger(__name__).exception("预演阻断通知发送失败")
            except Exception as exc:
                # 包括预演落盘异常；不得跳过后面的关市平仓分支。
                last_rehearsal = {
                    **(window or {}), "timestamp": observed_at.isoformat(),
                    "conclusion": "blocked", "blocking_reasons": [f"预演流程异常：{exc}"],
                }
                rehearsal_blocked = True
                if window:
                    rehearsal_windows[window["window_id"]] = last_rehearsal
                logging.getLogger(__name__).critical("预演流程异常，禁止切换：%s", exc)
                try:
                    _append_audit(switch_history_path, {
                        **last_rehearsal, "kind": "rehearsal", "level": "critical",
                    })
                    notify("Swap carry 预演阻断", str(exc))
                except Exception:
                    logging.getLogger(__name__).exception("预演异常记录或通知失败")
            rehearsal_blocked = bool(last_rehearsal and last_rehearsal.get("conclusion") == "blocked")

        if (
            reason is None
            and not all_flat
            and auto_switch
            and current_structure is not None
            and target_structure is not None
            and current_structure.name != target_structure.name
        ):
            if not selection_decision["allowed"]:
                switch_ready = False
                auto_switch_conclusion = selection_decision["reason"]
            elif rehearsal_blocked:
                switch_ready = False
                auto_switch_conclusion = "本窗口预演 blocked，禁止真实切换：" + "；".join(
                    last_rehearsal.get("blocking_reasons", []))
            else:
                switch_ready, auto_switch_conclusion = await _switch_readiness(
                    var,
                    target=target_structure,
                    funding_availability=target_funding_availability,
                    funding_rate_overrides=target_funding_overrides,
                    schedule=schedule,
                    schedule_error=schedule_error,
                    market_status=market_status,
                    auto_open=auto_open,
                    auto_open_notional=auto_open_notional,
                    daily_attempts=daily_open_attempts,
                    incident=auto_open_incident or switch_incident,
                )
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "rehearsal": last_rehearsal,
                    "event": "auto_switch_ready" if switch_ready else "auto_switch_deferred",
                    "current_structure": current_structure.name,
                    "target_structure": target_structure.name,
                    "reason": auto_switch_conclusion,
                    "daily_open_attempts": daily_open_attempts,
                },
            )

        if reason is None and long_close_due and not switch_ready:
            reason = (
                f"XAUS 长休市 {schedule.closure_duration} 将在 "
                f"{schedule.time_until_close} 后开始，目标结构暂不可开"
            )

        # 所有平仓原因确定后才补桶；临近强制长休市退出时不延后平仓或切换。
        if reason is None and not all_flat and not long_close_due:
            for leg in selected.legs:
                if positions[leg.underlying].is_flat:
                    continue
                mode = margin_modes.get(leg.underlying)
                if mode is None or not mode.isolated:
                    continue
                allocation_results[leg.underlying] = await _maintain_isolated_allocation(
                    var, underlying=leg.underlying, mode=mode, dry_run=dry_run,
                    observed_at=observed_at,
                    ledger_path=_allocation_state_path(state_path),
                    audit_path=audit_path,
                )
                allocation = allocation_results[leg.underlying]
                if allocation.get("attempted"):
                    # 补仓改变了余额快照；本轮不复用补仓前的切换准入结果。
                    switch_ready = False
                    auto_switch_conclusion = "本轮已尝试补保证金，下一轮重新检查切换条件"
                if allocation.get("distance") is not None:
                    per_leg_liquidation[leg.underlying] = _per_leg_liquidation_payload(
                        mode=mode, status=str(allocation.get("message")),
                        distance=allocation["distance"],
                    )

        if reason is None and switch_ready:
            assert current_structure is not None
            assert target_structure is not None
            auto_switch_attempted = True
            switch_started = time.perf_counter()
            switch_phase_started = switch_started
            switch_record = {
                "schema_version": 1,
                "started_at": observed_at.isoformat(),
                "status": "in_progress",
                "kind": "switch",
                "rehearsal": last_rehearsal,
                "direction": {
                    "from": current_structure.name,
                    "to": target_structure.name,
                },
                "trigger": {
                    "types": ["时段边界", "目标结构变化"],
                    "detail": target_structure_reason,
                },
                "before": None,
                "close_phase": {
                    "status": "pending",
                    "duration_ms": 0,
                    "legs": [],
                    "failed_attempts": [],
                },
                "flat_confirmation": {
                    "all_flat": False,
                    "confirmed_at": None,
                    "poll_count": 0,
                },
                "open_phase": {
                    "status": "pending",
                    "duration_ms": 0,
                    "legs": [],
                    "rollback_legs": [],
                    "failed_attempts": [],
                },
                "after": None,
                "measured_wear_usd": None,
                "total_duration_ms": 0,
                "self_check": {
                    "performed": False,
                    "passed": None,
                    "status": "未执行",
                    "checks": {},
                },
                "failure": None,
            }
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "auto_switch_started",
                    "current_structure": current_structure.name,
                    "target_structure": target_structure.name,
                    "dry_run": dry_run,
                },
            )
            before_payload = (
                account_positions_payload
                if account_positions_payload is not None
                else await var.get_positions()
            )
            switch_record["before"] = await _switch_snapshot(
                var,
                positions_payload=before_payload,
                metadata=metadata,
            )
            switch_recorder = _SwitchTradeRecorder(var)
            switch_recorder.cost_parent_id = "switch:" + str(switch_record["started_at"])
            switch_stage = "close"
            switch_phase_started = time.perf_counter()
            close_attempted = True
            flatten_result = await _flatten(
                switch_recorder,
                structure=current_structure,
                positions=positions,
                xaus_known_closed=False,
                dry_run=dry_run,
                audit_path=audit_path,
                observed_at=observed_at,
            )
            switch_record["close_phase"] = _phase_payload(
                switch_recorder,
                "close",
                started=switch_phase_started,
                status="completed" if flatten_result.complete else "failed",
            )
            if not flatten_result.complete:
                raise CloseActionError(
                    f"切换旧结构未能全平：{flatten_result.message}"
                )
            if dry_run:
                conclusion = (
                    f"dry-run：将先平 {current_structure.name}，确认全平后再开 "
                    f"{target_structure.name}"
                )
                auto_switch_conclusion = conclusion
                auto_open_conclusion = "dry-run 未实际平仓，因此未发送目标开仓"
                consecutive_failures = 0
                persist_state("dry_run", conclusion, 0)
                result_code = 0
            else:
                flat_positions, flat_payload, flat_poll_count = (
                    await _await_managed_flat(switch_recorder)
                )
                old_net_delta = sum(
                    (
                        position.signed_size
                        for position in flat_positions.values()
                    ),
                    Decimal("0"),
                )
                if any(
                    not position.is_flat for position in flat_positions.values()
                ):
                    detail = " ".join(
                        f"{underlying}={position.signed_size}"
                        for underlying, position in flat_positions.items()
                    )
                    raise CloseActionError(
                        "切换平仓 accept 已返回，但旧结构仍未归零："
                        f"{detail} 净 delta={old_net_delta}"
                    )
                # 旧结构确实从有仓变为空仓；强制切换仍在本分支直接开目标结构。
                last_closed_at = observed_at
                switch_record["flat_confirmation"] = {
                    "all_flat": True,
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                    "poll_count": flat_poll_count,
                }
                _append_audit(
                    audit_path,
                    {
                        "timestamp": observed_at,
                        "event": "auto_switch_flat_confirmed",
                        "current_structure": current_structure.name,
                        "target_structure": target_structure.name,
                        "poll_count": flat_poll_count,
                    },
                )

                selected = target_structure
                positions = {
                    leg.underlying: flat_positions[leg.underlying]
                    for leg in selected.legs
                }
                prices = {
                    leg.underlying: execution._metadata_price(metadata, leg)
                    for leg in selected.legs
                }
                funding_availability = target_funding_availability
                switch_stage = "open"
                switch_recorder.phase = "open"
                switch_phase_started = time.perf_counter()
                open_result = await _try_auto_open(
                    switch_recorder,
                    structure=selected,
                    target_structure=target_structure,
                    positions=positions,
                    schedule=schedule,
                    schedule_error=schedule_error,
                    market_status=market_status,
                    funding_availability=funding_availability,
                    kill_switch_path=kill_switch_path,
                    auto_open=auto_open,
                    auto_open_notional=auto_open_notional,
                    daily_attempts=daily_open_attempts,
                    incident=auto_open_incident or switch_incident,
                    dry_run=False,
                    audit_path=audit_path,
                    observed_at=observed_at,
                    funding_rate_overrides=target_funding_overrides,
                    enforce_min_carry=not selection_decision.get("forced_long_closure", False),
                )
                switch_record["open_phase"] = _phase_payload(
                    switch_recorder,
                    "open",
                    started=switch_phase_started,
                    status="completed" if open_result.result_code == 0 else "failed",
                )
                auto_open_attempted = open_result.attempted
                auto_open_conclusion = open_result.conclusion
                daily_open_attempts = open_result.daily_attempts
                auto_open_incident = open_result.incident
                result_code = open_result.result_code
                consecutive_failures = (
                    previous_failures + 1 if result_code != 0 else 0
                )
                if result_code == 0:
                    switch_stage = "self_check"
                    account_positions_payload = await var.get_positions()
                    latest_positions = _carry_positions_from_payload(
                        account_positions_payload
                    )
                    positions = {
                        leg.underlying: latest_positions[leg.underlying]
                        for leg in selected.legs
                    }
                    switch_record["after"] = await _switch_snapshot(
                        var,
                        positions_payload=account_positions_payload,
                        metadata=metadata,
                    )
                    before_snapshot = switch_record["before"]
                    after_snapshot = switch_record["after"]
                    assert isinstance(before_snapshot, Mapping)
                    assert isinstance(after_snapshot, Mapping)
                    switch_record["measured_wear_usd"] = str(
                        execution._decimal(
                            after_snapshot.get("equity"),
                            label="切换后权益",
                        )
                        - execution._decimal(
                            before_snapshot.get("equity"),
                            label="切换前权益",
                        )
                    )
                    self_check = _switch_self_check(
                        target_structure,
                        account_positions_payload=account_positions_payload,
                        old_structure_was_flat=True,
                    )
                    switch_record["self_check"] = self_check
                    if self_check["passed"] is True:
                        last_switch_at = observed_at
                        auto_switch_conclusion = (
                            f"已从 {current_structure.name} 切换为 "
                            f"{target_structure.name}，切换后自检通过"
                        )
                        conclusion = auto_switch_conclusion
                        switch_record["status"] = "completed"
                        switch_record["completed_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                        _append_audit(
                            audit_path,
                            {
                                "timestamp": observed_at,
                                "event": "auto_switch_succeeded",
                                "current_structure": current_structure.name,
                                "target_structure": target_structure.name,
                                "daily_open_attempts": daily_open_attempts,
                                "self_check": self_check,
                            },
                        )
                    else:
                        switch_incident = True
                        result_code = 1
                        consecutive_failures = previous_failures + 1
                        auto_switch_conclusion = (
                            f"{current_structure.name}→{target_structure.name} "
                            "切换后自检失败，已设置 switch_incident"
                        )
                        conclusion = auto_switch_conclusion
                        residual_state, has_residual = _residual_state(
                            account_positions_payload
                        )
                        switch_record["status"] = "failed"
                        switch_record["failure"] = {
                            "stage": "self_check",
                            "error_type": "SwitchSelfCheckError",
                            "message": auto_switch_conclusion,
                            "residual_state": residual_state,
                            "has_residual_position": has_residual,
                        }
                        _append_audit(
                            audit_path,
                            {
                                "timestamp": observed_at,
                                "event": "auto_switch_self_check_failed",
                                "level": "critical",
                                "current_structure": current_structure.name,
                                "target_structure": target_structure.name,
                                "message": auto_switch_conclusion,
                                "self_check": self_check,
                            },
                        )
                        notify(
                            "Swap carry 切换后自检失败",
                            f"{auto_switch_conclusion}；请立即人工核对切换台账",
                        )
                else:
                    auto_switch_conclusion = (
                        f"{current_structure.name} 已全平，但 "
                        f"{target_structure.name} 开仓失败；账户保持空仓："
                        f"{open_result.conclusion}"
                    )
                    conclusion = auto_switch_conclusion
                    _append_audit(
                        audit_path,
                        {
                            "timestamp": observed_at,
                            "event": "auto_switch_open_failed",
                            "current_structure": current_structure.name,
                            "target_structure": target_structure.name,
                            "message": open_result.conclusion,
                            "daily_open_attempts": daily_open_attempts,
                        },
                    )
                    await finalize_switch_failure(
                        RuntimeError(open_result.conclusion)
                    )
                exit_carry_since = None
                if not switch_record_written:
                    switch_record["total_duration_ms"] = round(
                        (time.perf_counter() - switch_started) * 1000,
                        3,
                    )
                    _append_audit(switch_history_path, switch_record)
                    switch_record_written = True
                persist_state(
                    "incident" if switch_incident else open_result.status,
                    conclusion,
                    consecutive_failures,
                )
        elif reason is None and all_flat:
            exit_carry_since = None
            if rehearsal_blocked:
                open_result = AutoOpenResult(
                    False, "本窗口预演 blocked，禁止平仓后绕过阻断重新开仓",
                    daily_open_attempts, status="rehearsal_blocked",
                )
            elif switch_incident and not auto_open_incident:
                open_result = AutoOpenResult(
                    False,
                    "自动开仓已因既有 switch_incident 停止，"
                    "需人工核对并清除标记后才能恢复",
                    daily_open_attempts,
                    status="incident",
                )
            elif last_closed_at is not None and observed_at - last_closed_at < REOPEN_COOLDOWN:
                open_result = AutoOpenResult(
                    False,
                    f"平仓后重开冷却中，最早可重开时间 {(last_closed_at + REOPEN_COOLDOWN).isoformat()}",
                    daily_open_attempts,
                    status="reopen_cooldown",
                )
            elif auto_switch and (target_structure is None or not selection_decision["allowed"]):
                open_result = AutoOpenResult(
                    False,
                    auto_switch_conclusion,
                    daily_open_attempts,
                )
            else:
                open_result = await _try_auto_open(
                    var,
                    structure=selected,
                    target_structure=target_structure,
                    positions=positions,
                    schedule=schedule,
                    schedule_error=schedule_error,
                    market_status=market_status,
                    funding_availability=funding_availability,
                    kill_switch_path=kill_switch_path,
                    auto_open=auto_open,
                    auto_open_notional=auto_open_notional,
                    daily_attempts=daily_open_attempts,
                    incident=auto_open_incident or switch_incident,
                    dry_run=dry_run,
                    audit_path=audit_path,
                    observed_at=observed_at,
                    funding_rate_overrides=target_funding_overrides,
                )
            if open_result.did_close_this_round:
                last_closed_at = observed_at
            auto_open_attempted = open_result.attempted
            auto_open_conclusion = open_result.conclusion
            daily_open_attempts = open_result.daily_attempts
            auto_open_incident = open_result.incident
            conclusion = open_result.conclusion
            result_code = open_result.result_code
            consecutive_failures = (
                previous_failures + 1 if result_code != 0 else 0
            )
            persist_state(open_result.status, conclusion, consecutive_failures)
        elif reason is None:
            auto_open_conclusion = "已有结构持仓，自动开仓不适用"
            conclusion = "无需动作：仓位与风控检查正常"
            consecutive_failures = 0
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "auto_open_skipped",
                    "reason": auto_open_conclusion,
                    "attempted": False,
                    "daily_open_attempts": daily_open_attempts,
                },
            )
            persist_state("healthy", conclusion, 0)
            result_code = 0
        else:
            auto_open_conclusion = f"平仓/风控检查优先命中：{reason}"
            print(f"守护进程命中风控：{reason}")
            close_attempted = True
            flatten_result = await _flatten(
                var,
                structure=selected,
                positions=positions,
                xaus_known_closed=(
                    selected.has_xaus
                    and schedule is not None
                    and schedule.metadata_is_fresh
                    and market_status is not None
                    and market_status != "open"
                ),
                dry_run=dry_run,
                audit_path=audit_path,
                observed_at=observed_at,
            )
            conclusion = f"{reason}；{flatten_result.message}"
            if flatten_result.complete:
                if not dry_run:
                    flat_result = await execution._await_flat(var, selected)
                    sizes = flat_result[:-1]
                    net_delta = flat_result[-1]
                    positions = {
                        leg.underlying: Position(leg.underlying, size)
                        for leg, size in zip(selected.legs, sizes, strict=True)
                    }
                    if any(size != 0 for size in sizes):
                        detail = " ".join(
                            f"{leg.underlying}={size}"
                            for leg, size in zip(
                                selected.legs, sizes, strict=True
                            )
                        )
                        raise CloseActionError(
                            "平仓 accept 已返回，但轮询后仓位仍未归零："
                            f"{detail} 净 delta={net_delta}"
                        )
                    _append_audit(
                        audit_path,
                        {
                            "timestamp": observed_at,
                            "event": "flat_confirmed",
                            "structure": selected.name,
                            "positions": {
                                leg.underlying: size
                                for leg, size in zip(
                                    selected.legs, sizes, strict=True
                                )
                            },
                            "net_delta": net_delta,
                        },
                    )
                    exit_carry_since = None
                    # 仅真实持仓平仓确认后开始冷却，空仓风控轮次不延长。
                    if not all_flat:
                        last_closed_at = observed_at
                consecutive_failures = 0
                completed_status = (
                    "dry_run"
                    if dry_run
                    else state_status if state_status != "healthy" else "flattened"
                )
                persist_state(completed_status, conclusion, 0)
                result_code = 0
            else:
                consecutive_failures = previous_failures + 1
                persist_state(
                    "pending_xaus_close", conclusion, consecutive_failures
                )
                result_code = 1
    except _SessionExpiredPreflight:
        consecutive_failures = previous_failures + 1
        conclusion = "会话已过期，需人工刷新 Cookie"
        auto_open_conclusion = f"{conclusion}；禁止自动开仓"
        auto_switch_conclusion = f"{conclusion}；禁止自动切换"
        persist_state("session_expired", conclusion, consecutive_failures)
        result_code = 1
    except (VariationalJurisdictionError, VariationalAuthError) as exc:
        await finalize_switch_failure(exc)
        consecutive_failures = previous_failures + 1
        category = (
            "地区封锁"
            if isinstance(exc, VariationalJurisdictionError)
            else "会话失效"
        )
        conclusion = f"{category}导致守护进程无法平仓：{exc}"
        persist_state("action_failed", conclusion, consecutive_failures)
        notify("Swap carry 无法自动平仓", conclusion)
        print(f"🚨 {conclusion}")
        result_code = 1
    except Exception as exc:  # noqa: BLE001 任意未知错误都不得静默
        await finalize_switch_failure(exc)
        consecutive_failures = previous_failures + 1
        conclusion = f"守护轮次失败，无法确认已安全平仓：{type(exc).__name__}: {exc}"
        persist_state("action_failed", conclusion, consecutive_failures)
        notify("Swap carry 守护进程失败", conclusion)
        print(f"🚨 {conclusion}")
        result_code = 1
    finally:
        # 成交或部分失败后重新读仓，令心跳反映本轮结束时的真实快照。
        if not session_is_expired:
            try:
                positions = await execution._get_positions(var, selected)
            except Exception as exc:  # noqa: BLE001 心跳仍需保存其他已知字段
                conclusion = (
                    f"{conclusion}；结束读仓失败：{type(exc).__name__}: {exc}"
                )

        notionals = {
            leg.underlying: _position_notional(
                positions.get(leg.underlying), prices.get(leg.underlying)
            )
            for leg in selected.legs
        }
        net_delta = (
            sum(
                (
                    positions[leg.underlying].signed_size
                    for leg in selected.legs
                ),
                Decimal("0"),
            )
            if len(positions) == len(selected.legs)
            else None
        )
        for leg in selected.legs:
            mode = margin_modes.get(leg.underlying)
            if mode is None:
                mode = _margin_mode_from_supported_assets(metadata, leg)
                if mode is None:
                    mode = MarginModeStatus("isolated", "保守默认")
                margin_modes[leg.underlying] = mode
            if leg.underlying not in per_leg_liquidation:
                per_leg_liquidation[leg.underlying] = (
                    _per_leg_liquidation_payload(
                        mode=mode,
                        status="未检查",
                    )
                )
        # 成本采集失败只降级，绝不阻断平仓风控。只读面板不会触发这些写入。
        cost_status = "dry-run：不写成本台账"
        if not dry_run:
            try:
                ledger_path = cost.ACTIVE_PATH.get()
                await cost.scan_transfers(var, ledger_path)
                from tools.show_switch_history import load_switch_history
                history, history_error = load_switch_history(switch_history_path)
                if history_error:
                    raise ValueError(history_error)
                for item in history:
                    cost.record_switch(ledger_path, item)
                report_path = cost_reconciliation_path or ledger_path.with_name(cost.DEFAULT_RECON_PATH.name)
                await cost.capture_reconciliation(var, ledger_path, report_path)
                report, report_error = cost.read_report(cost.load(ledger_path)[0], report_path)
                cost_status = report_error or f"未解释残差 {report['residual']:+.2f} USD"
                if report and report["warning"]:
                    logging.getLogger(__name__).warning("成本对账告警：%s", cost_status)
            except Exception as exc:  # noqa: BLE001 缺失来源不能补零造平账
                cost_status = f"成本对账不可用：{type(exc).__name__}: {exc}"
                logging.getLogger(__name__).warning("%s", cost_status)
        from tools.swap_carry_funding_recon import reconcile
        try:
            funding_recon = await reconcile(
                var, samples_path=funding_samples_path or data_dir() / "swap_carry_samples.jsonl",
                output_path=funding_recon_path or data_dir() / "swap_carry_funding_recon.jsonl",
                deviation_threshold=funding_deviation_threshold,
            )
            for record in funding_recon["records"]:
                _append_audit(audit_path, {"timestamp": observed_at, "event": "funding_reconciliation", **record})
                if record["level"] == "warning":
                    logging.getLogger(__name__).warning("XAUS 资金费对账：%s", record["conclusion"])
                    notify("XAUS 资金费对账告警", record["conclusion"])
        except Exception as exc:  # noqa: BLE001 对账失败不能阻断平仓风控或心跳
            funding_recon = {"level": "warning", "conclusion": f"资金费对账读取失败：{type(exc).__name__}: {exc}"}
        print(f"XAUS 资金费对账：{funding_recon['conclusion']}")
        heartbeat = {
            "timestamp": observed_at.isoformat(),
            "last_seen": observed_at.isoformat(),
            "last_full_round_at": observed_at.isoformat(),
            "status": round_status,
            "close_attempted": close_attempted,
            "funding_reconciliation": funding_recon,
            "cost_reconciliation": cost_status,
            "structure": selected.name,
            "conclusion": conclusion,
            "isolated_allocation": allocation_results,
            "legs": {
                leg.underlying: {
                    "size": str(positions[leg.underlying].signed_size)
                    if leg.underlying in positions
                    else None,
                    "weight": str(leg.weight),
                    "notional": _format_money(notionals[leg.underlying]),
                    "margin_mode": margin_modes[leg.underlying].mode,
                    "margin_mode_source": margin_modes[leg.underlying].source,
                    "per_leg_liquidation": per_leg_liquidation[
                        leg.underlying
                    ],
                }
                for leg in selected.legs
            },
            # 保留旧字段，避免既有 XAUS_XAU 监控消费者失效。
            "xaus_notional": _format_money(notionals.get("XAUS")),
            "xau_notional": _format_money(notionals.get("XAU")),
            "net_delta": str(net_delta) if net_delta is not None else None,
            "account_margin": _account_margin_payload(
                account_margin,
                error=account_margin_error,
            ),
            "xaus_schedule": (
                _schedule_payload(schedule)
                if auto_switch or selected.has_xaus
                else None
            ),
            "funding_availability": {
                underlying: {
                    "usable": item.usable,
                    "reason": item.reason,
                    "schedule_source": item.schedule_source,
                }
                for underlying, item in funding_availability.items()
            },
            "net_carry_annual": (
                str(net_carry) if net_carry is not None else None
            ),
            "exit_carry_annual": str(EXIT_CARRY_ANNUAL),
            "exit_carry_since": exit_carry_since,
            "last_closed_at": last_closed_at,
            "exit_carry_duration_seconds": EXIT_CARRY_DURATION.total_seconds(),
            "reopen_cooldown_seconds": REOPEN_COOLDOWN.total_seconds(),
            "exit_carry_observation": exit_carry_observation,
            "consecutive_failures": consecutive_failures,
            "dry_run": dry_run,
            "auto_open_attempted": auto_open_attempted,
            "auto_open_conclusion": auto_open_conclusion,
            "daily_open_attempts": daily_open_attempts,
            "auto_open_incident": auto_open_incident,
            "switch_incident": switch_incident,
            "last_rehearsal": last_rehearsal,
            "rehearsal_blocked": rehearsal_blocked,
            "auto_switch": auto_switch,
            "auto_switch_attempted": auto_switch_attempted,
            "auto_switch_conclusion": auto_switch_conclusion,
            "candidate_structures": candidate_structures,
            "best_structure": best_structure.name if best_structure else None,
            "selection_decision": selection_decision,
            "last_switch_at": last_switch_at,
            "observed_structure": (
                current_structure.name if current_structure is not None else None
            ),
            "target_structure": (
                target_structure.name if target_structure is not None else None
            ),
            "session_expires_at": session_expires_at,
            "session_hours_left": session_hours_left,
        }
        critical_reasons: list[str] = []
        next_mode, next_full = _polling_plan(
            heartbeat, now=observed_at, kill_switch_path=kill_switch_path,
            switch_lead_time=switch_lead_time, critical_reasons=critical_reasons,
        )
        heartbeat.update(
            polling_mode=next_mode, critical_reasons=critical_reasons, skipped_reason=None,
            next_full_round_at=next_full.isoformat(),
        )
        _write_json(heartbeat_path, heartbeat)
        _append_audit(
            audit_path,
            {
                "timestamp": observed_at,
                "event": "round_finished",
                "result_code": result_code,
                **heartbeat,
            },
        )
    return result_code


async def _main(args: argparse.Namespace) -> int:
    """先做零网络的本地轮询判定，需要完整轮次才创建交易所客户端。"""
    now = datetime.now(timezone.utc)
    if args.reset_reopen_cooldown:
        # 严格读取并保留其余字段；损坏或缺失时拒绝覆盖，不加载交易客户端。
        saved = json.loads(args.state.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("冷却状态必须为 JSON 对象，拒绝重置")
        if args.dry_run:
            print(f"dry-run：将清空 {args.state} 的 last_closed_at，未修改状态")
            return 0
        saved["reopen_cooldown_reset"] = {
            "timestamp": now.isoformat(),
            "previous_last_closed_at": saved.get("last_closed_at"),
        }
        saved["last_closed_at"] = None
        _write_json(args.state, saved)
        print(f"已清空 {args.state} 的重开冷却，保留其它状态并退出")
        return 0
    ledger_path = _allocation_state_path(args.state)
    if args.reset_allocation_counters:
        if args.dry_run:
            print("dry-run：不重置异常补仓计数，不发送 POST")
            return 0
        _reset_allocation_counters(ledger_path, now)
        return 0
    heartbeat = execution._read_guard_json(args.heartbeat)
    critical_reasons: list[str] = []
    mode, next_full = _polling_plan(
        heartbeat, now=now, kill_switch_path=args.kill_switch,
        switch_lead_time=timedelta(minutes=float(args.switch_lead_minutes)),
        critical_reasons=critical_reasons,
    )
    if mode == "normal" and now < next_full:
        # 保留上轮风险快照与完整轮次时间，旧面板继续用 timestamp 判断存活。
        _write_json(args.heartbeat, {
            **heartbeat, "timestamp": now.isoformat(), "last_seen": now.isoformat(),
            "polling_mode": mode, "critical_reasons": critical_reasons, "skipped_reason": "距上次完整轮次不足普通轮询间隔",
            "next_full_round_at": next_full.isoformat(),
        })
        return 0
    var = await execution._load()
    try:
        return await run_once(
            var,
            polling_mode=mode,
            structure=args.structure,
            dry_run=args.dry_run,
            auto_open=args.auto_open,
            auto_switch=args.auto_switch,
            auto_open_notional=args.auto_open_notional,
            switch_lead_time=timedelta(minutes=float(args.switch_lead_minutes)),
            rehearsal_lead=timedelta(minutes=float(args.rehearsal_lead_minutes)),
            kill_switch_path=args.kill_switch,
            heartbeat_path=args.heartbeat,
            state_path=args.state,
            audit_path=args.audit_log,
            switch_history_path=args.switch_history,
            cost_ledger_path=args.cost_ledger,
            cost_reconciliation_path=args.cost_reconciliation,
            funding_recon_path=args.funding_recon,
            funding_samples_path=args.funding_samples,
            funding_deviation_threshold=args.funding_deviation_threshold,
        )
    finally:
        await var.close()


def build_parser() -> argparse.ArgumentParser:
    """构造单轮周循环守护命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Variational swap carry 无人值守周循环守护进程"
    )
    parser.add_argument("--once", action="store_true", help="执行一轮后退出")
    parser.add_argument(
        "--structure",
        choices=tuple(execution.STRUCTURES),
        default=execution.DEFAULT_STRUCTURE.name,
        help=f"carry 结构，默认 {execution.DEFAULT_STRUCTURE.name}",
    )
    parser.add_argument("--dry-run", action="store_true", help="只读判定与计算，禁止全部 POST（含询价）")
    parser.add_argument(
        "--no-auto-open",
        dest="auto_open",
        action="store_false",
        default=True,
        help="关闭自动开仓，但保留全部自动平仓与风控",
    )
    parser.add_argument(
        "--no-auto-switch",
        dest="auto_switch",
        action="store_false",
        default=True,
        help="关闭结构自动切换，但保留自动开仓与全部平仓风控",
    )
    parser.add_argument(
        "--switch-lead-minutes",
        type=Decimal,
        default=os.environ.get(
            "SWITCH_LEAD_TIME",
            str(int(SWITCH_LEAD_TIME.total_seconds() // 60)),
        ),
        help="距 XAUS 长休市多少分钟开始切换；默认 60，可由 SWITCH_LEAD_TIME 覆盖",
    )
    parser.add_argument(
        "--rehearsal-lead-minutes", type=Decimal,
        default=os.environ.get("REHEARSAL_LEAD", "30"),
        help="提前多少分钟预演计划切换，默认 30，可由 REHEARSAL_LEAD 覆盖",
    )
    parser.add_argument(
        "--auto-open-notional",
        type=Decimal,
        default=os.environ.get(
            "AUTO_OPEN_NOTIONAL_USD", str(AUTO_OPEN_NOTIONAL_USD)
        ),
        help=(
            "自动开仓每腿目标名义美元；默认读取 AUTO_OPEN_NOTIONAL_USD，"
            f"未配置时为 {AUTO_OPEN_NOTIONAL_USD}，硬上限 "
            f"{execution.MAX_NOTIONAL_USD}"
        ),
    )
    parser.add_argument("--kill-switch", type=Path, default=DEFAULT_KILL_SWITCH)
    parser.add_argument("--heartbeat", type=Path, default=DEFAULT_HEARTBEAT)
    parser.add_argument("--cost-ledger", type=Path, help="逐笔成本台账路径")
    parser.add_argument("--cost-reconciliation", type=Path, help="成本对账证据路径")
    parser.add_argument("--funding-recon", type=Path, help="资金费对账 JSONL 路径")
    parser.add_argument("--funding-samples", type=Path, help="资金费采样 JSONL 路径")
    parser.add_argument("--funding-deviation-threshold", type=Decimal, default=Decimal(".20"),
                        help="偏差比例告警阈值，默认 0.20；周五/周一三日模式按三倍预测校验")
    parser.add_argument("--reset-reopen-cooldown", action="store_true",
                        help="暂停守护进程后使用：仅本地清空 last_closed_at 并退出，保留其它状态和重置记录")
    parser.add_argument("--reset-allocation-counters", action="store_true",
                        help="仅本地重置异常补仓计数并退出，保留常规用量和重置记录")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    parser.add_argument("--switch-history", type=Path, default=DEFAULT_SWITCH_HISTORY,
                        help="切换与预演共用台账路径")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """清理代理变量后运行单轮周循环守护。"""
    args = build_parser().parse_args(argv)
    removed = execution._load_environment_without_proxy()
    if removed:
        print(f"已清除代理环境变量：{'、'.join(removed)}")
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
