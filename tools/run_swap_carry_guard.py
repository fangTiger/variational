"""Swap carry 无人值守周循环守护进程。

launchd 每五分钟以 ``--once`` 启动一轮。既有仓位始终先执行平仓与风控检查；
只有账户两腿均为空且所有入场条件明确满足时，才复用人工执行器尝试自动开仓。
"""

from __future__ import annotations

# 必须在导入交易相关依赖前配好 CA。
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping, Sequence  # noqa: E402

from adapters.base import Position, Side  # noqa: E402
from adapters.variational_client import (  # noqa: E402
    VariationalAuthError,
    VariationalJurisdictionError,
    VariationalRequestError,
)
from engine.swap_trading_schedule import (  # noqa: E402
    SwapTradingSchedule,
    parse_trading_schedule,
)
from tools.alert_check import notify  # noqa: E402
from tools import hedge_swap_carry as execution  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KILL_SWITCH = execution.SWAP_CARRY_KILL_SWITCH
DEFAULT_HEARTBEAT = execution.SWAP_CARRY_GUARD_HEARTBEAT
DEFAULT_STATE = execution.SWAP_CARRY_GUARD_STATE
DEFAULT_AUDIT_LOG = PROJECT_ROOT / "data" / "swap_carry_guard_audit.jsonl"

IMBALANCE_RATIO = Decimal("0.05")
LIQUIDATION_ALERT_RATIO = Decimal("0.015")
ACCOUNT_MARGIN_RATIO_MIN = Decimal("2.0")
PRE_CLOSE_MINUTES = 30
LONG_CLOSURE_THRESHOLD = timedelta(hours=4)
CLOSE_RETRIES = 3
RETRY_DELAY_SECONDS = 1.0
AUTO_OPEN_NOTIONAL_USD = Decimal("500")
MIN_ENTRY_CARRY_ANNUAL = Decimal("0.05")
MIN_TIME_TO_CLOSE = timedelta(hours=2)
MAX_DAILY_OPEN_ATTEMPTS = 20
EXIT_CARRY_ANNUAL = Decimal("0")
EXIT_CARRY_CONSECUTIVE_ROUNDS = 3
_SCHEDULED_FUNDING_INSTRUMENT_TYPES = frozenset(
    {"perpetual_rwa_future", "swap"}
)


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


@dataclass(frozen=True)
class FundingAvailability:
    """单腿费率在当前标的市场时段是否具有经济含义。"""

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


def _read_exit_carry_rounds(path: Path) -> int:
    """读取跨进程保存的连续非正 carry 轮次。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = int(payload.get("exit_carry_consecutive_rounds", 0))
        return max(0, value)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _state_payload(
    *,
    observed_at: datetime,
    status: str,
    message: str,
    consecutive_failures: int,
    open_attempt_date: str | None = None,
    daily_open_attempts: int = 0,
    auto_open_incident: bool = False,
    exit_carry_consecutive_rounds: int = 0,
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
        "exit_carry_consecutive_rounds": exit_carry_consecutive_rounds,
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


def _schedule_record_for_funding_leg(
    metadata: object,
    leg: execution.CarryLeg,
) -> tuple[Mapping[str, Any], str]:
    """选择费率时段元数据；XAU 与 XAUS 明确共享黄金现货时段。"""
    candidates = [leg]
    if leg.underlying == "XAU":
        candidates.append(execution.XAUS_LEG)
    elif leg.underlying == "XAUS":
        candidates.append(execution.XAU_LONG_LEG)

    errors: list[str] = []
    for candidate in candidates:
        try:
            record = execution._instrument_record(metadata, candidate)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if (
            record.get("trading_sessions") is not None
            and record.get("trading_schedule") is not None
            and isinstance(record.get("market_status"), str)
            and str(record.get("market_status")).strip()
        ):
            return record, candidate.underlying
        errors.append(
            f"{candidate.underlying} 缺少完整的交易会话、日程或 market_status"
        )
    raise ValueError("；".join(errors) or f"{leg.underlying} 缺少时段元数据")


def _funding_availability_by_leg(
    metadata: object | None,
    selected: execution.CarryStructure,
    observed_at: datetime,
    *,
    metadata_error: str | None,
) -> dict[str, FundingAvailability]:
    """仅以交易时段元数据判定费率是否可用于入场或退出。"""
    result: dict[str, FundingAvailability] = {}
    for leg in selected.legs:
        if leg.instrument_type not in _SCHEDULED_FUNDING_INSTRUMENT_TYPES:
            result[leg.underlying] = FundingAvailability(
                True,
                f"{leg.underlying} 为 24/7 合约，费率可用",
            )
            continue
        if metadata is None:
            detail = metadata_error or "supported_assets 无数据"
            result[leg.underlying] = FundingAvailability(
                False,
                f"{leg.underlying} 费率不可用：{detail}",
            )
            continue
        try:
            record, source = _schedule_record_for_funding_leg(metadata, leg)
            schedule = parse_trading_schedule(
                record.get("trading_sessions"),
                record.get("trading_schedule"),
                record.get("market_status"),
                observed_at,
            )
        except (TypeError, ValueError) as exc:
            result[leg.underlying] = FundingAvailability(
                False,
                f"{leg.underlying} 费率不可用：时段元数据异常：{exc}",
            )
            continue
        usable = schedule.metadata_is_fresh and schedule.is_tradable
        result[leg.underlying] = FundingAvailability(
            usable,
            (
                f"{leg.underlying} 费率可用：{schedule.reason}"
                if usable
                else f"{leg.underlying} 费率不可用：{schedule.reason}"
            ),
            schedule_source=source,
        )
    return result


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
    quote_cache: dict[
        tuple[str, str, int, str | None, str],
        Mapping[str, Any],
    ],
) -> AccountMarginHealth:
    """用全账户真实持仓计算权益相对维持保证金的倍数。"""
    payload = await var.get_positions()
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


async def _try_auto_open(
    var: Any,
    *,
    structure: execution.CarryStructure | str,
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
) -> AutoOpenResult:
    """逐项失败关闭地判定入场，并把成交委托给人工执行器。"""
    selected = execution.resolve_structure(structure)

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
        return AutoOpenResult(
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
            "自动开仓已因既有 INCIDENT 停止，需人工解除后才能恢复",
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
        rates = await execution._load_funding_rates(var, selected)
        net_carry = execution._weighted_net_carry(selected, rates)
    except Exception as exc:  # noqa: BLE001 入场数据不确定时只跳过，不升级故障
        return skip(f"净 carry 读取失败，按不确定处理：{type(exc).__name__}: {exc}")

    if net_carry < MIN_ENTRY_CARRY_ANNUAL:
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
            var,
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
            return AutoOpenResult(True, conclusion, attempt_number)
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
            return AutoOpenResult(
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
            return AutoOpenResult(True, conclusion, attempt_number)
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
        return AutoOpenResult(
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
    return AutoOpenResult(
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


async def run_once(
    var: Any,
    *,
    structure: execution.CarryStructure | str = execution.XAUS_XAU,
    dry_run: bool = False,
    auto_open: bool = True,
    auto_open_notional: Decimal = AUTO_OPEN_NOTIONAL_USD,
    now: datetime | None = None,
    kill_switch_path: Path = DEFAULT_KILL_SWITCH,
    heartbeat_path: Path = DEFAULT_HEARTBEAT,
    state_path: Path = DEFAULT_STATE,
    audit_path: Path = DEFAULT_AUDIT_LOG,
) -> int:
    """先执行全部平仓风控，再以最低优先级判定自动开仓。"""
    selected = execution.resolve_structure(structure)
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("now 必须包含时区")
    observed_at = observed_at.astimezone(timezone.utc)
    previous_failures = _read_failure_count(state_path)
    daily_open_attempts, auto_open_incident = _read_auto_open_state(
        state_path, observed_at
    )
    exit_carry_rounds = _read_exit_carry_rounds(state_path)
    consecutive_failures = previous_failures
    conclusion = "本轮尚未完成"
    auto_open_attempted = False
    auto_open_conclusion = "未进入自动开仓判定"
    result_code = 1
    positions: dict[str, Position] = {}
    prices: dict[str, Decimal | None] = {
        leg.underlying: None for leg in selected.legs
    }
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
    account_margin: AccountMarginHealth | None = None
    account_margin_error: str | None = None
    quote_cache: dict[
        tuple[str, str, int, str | None, str],
        Mapping[str, Any],
    ] = {}

    def persist_state(status: str, message: str, failures: int) -> None:
        """写状态时始终保留每日计数和不可自动清除的 INCIDENT。"""
        effective_status = "incident" if auto_open_incident else status
        _write_json(
            state_path,
            _state_payload(
                observed_at=observed_at,
                status=effective_status,
                message=message,
                consecutive_failures=failures,
                open_attempt_date=observed_at.date().isoformat(),
                daily_open_attempts=daily_open_attempts,
                auto_open_incident=auto_open_incident,
                exit_carry_consecutive_rounds=exit_carry_rounds,
            ),
        )

    _append_audit(
        audit_path,
        {
            "timestamp": observed_at,
            "event": "round_started",
            "structure": selected.name,
            "dry_run": dry_run,
            "kill_switch": kill_switch_path.exists(),
            "auto_open": auto_open,
            "auto_open_notional": auto_open_notional,
            "daily_open_attempts": daily_open_attempts,
            "auto_open_incident": auto_open_incident,
            "exit_carry_consecutive_rounds": exit_carry_rounds,
        },
    )
    try:
        positions = await execution._get_positions(var, selected)

        try:
            metadata = await var.get_supported_assets()
            for leg in selected.legs:
                prices[leg.underlying] = execution._metadata_price(metadata, leg)
        except Exception as exc:  # noqa: BLE001 费率有效性按失败关闭，持仓仍继续降险
            metadata_error = f"{type(exc).__name__}: {exc}"

        funding_availability = _funding_availability_by_leg(
            metadata,
            selected,
            observed_at,
            metadata_error=metadata_error,
        )
        for leg in selected.legs:
            mode = _margin_mode_from_supported_assets(metadata, leg)
            if mode is not None:
                margin_modes[leg.underlying] = mode

        if selected.has_xaus:
            if metadata is None:
                schedule_error = metadata_error or "supported_assets 无数据"
            else:
                try:
                    record = execution._instrument_record(metadata, execution.XAUS_LEG)
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

        notionals = {
            leg.underlying: _position_notional(
                positions[leg.underlying], prices[leg.underlying]
            )
            for leg in selected.legs
        }

        reason: str | None = None
        state_status = "healthy"

        # 优先级 1：kill switch 无条件高于所有其他判断。
        if kill_switch_path.exists():
            reason = "kill switch 已激活"
            state_status = "kill_switch_active"
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
                    reason = (
                        f"XAUS 长休市 {schedule.closure_duration} 将在 "
                        f"{schedule.time_until_close} 后开始"
                    )

        # 优先级 5：只有全部腿费率在当前时段有效时才更新退出连续计数。
        if reason is None and not all_flat:
            unavailable_reason = _unusable_funding_reason(funding_availability)
            if unavailable_reason is not None:
                exit_carry_observation = (
                    f"费率不可用，本轮退出 carry 不计数也不清零："
                    f"{unavailable_reason}"
                )
            else:
                try:
                    rates = await execution._load_funding_rates(var, selected)
                    net_carry = execution._weighted_net_carry(selected, rates)
                except Exception as exc:  # noqa: BLE001 读取失败必须保持原计数
                    exit_carry_observation = (
                        "退出 carry 读取失败，本轮不计数也不清零："
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    if net_carry <= EXIT_CARRY_ANNUAL:
                        exit_carry_rounds += 1
                        exit_carry_observation = (
                            f"净 carry {net_carry:.4%} 不高于退出阈值 "
                            f"{EXIT_CARRY_ANNUAL:.4%}，连续第 "
                            f"{exit_carry_rounds}/{EXIT_CARRY_CONSECUTIVE_ROUNDS} 轮"
                        )
                        if exit_carry_rounds >= EXIT_CARRY_CONSECUTIVE_ROUNDS:
                            reason = exit_carry_observation
                            state_status = "exit_carry_triggered"
                    else:
                        exit_carry_rounds = 0
                        exit_carry_observation = (
                            f"净 carry {net_carry:.4%} 高于退出阈值 "
                            f"{EXIT_CARRY_ANNUAL:.4%}，连续计数已清零"
                        )
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "exit_carry_observed",
                    "message": exit_carry_observation,
                    "net_carry_annual": net_carry,
                    "threshold_annual": EXIT_CARRY_ANNUAL,
                    "consecutive_rounds": exit_carry_rounds,
                    "required_rounds": EXIT_CARRY_CONSECUTIVE_ROUNDS,
                },
            )

        if reason is None and all_flat:
            exit_carry_rounds = 0
            open_result = await _try_auto_open(
                var,
                structure=selected,
                positions=positions,
                schedule=schedule,
                schedule_error=schedule_error,
                market_status=market_status,
                funding_availability=funding_availability,
                kill_switch_path=kill_switch_path,
                auto_open=auto_open,
                auto_open_notional=auto_open_notional,
                daily_attempts=daily_open_attempts,
                incident=auto_open_incident,
                dry_run=dry_run,
                audit_path=audit_path,
                observed_at=observed_at,
            )
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
                    exit_carry_rounds = 0
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
    except (VariationalJurisdictionError, VariationalAuthError) as exc:
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
        consecutive_failures = previous_failures + 1
        conclusion = f"守护轮次失败，无法确认已安全平仓：{type(exc).__name__}: {exc}"
        persist_state("action_failed", conclusion, consecutive_failures)
        notify("Swap carry 守护进程失败", conclusion)
        print(f"🚨 {conclusion}")
        result_code = 1
    finally:
        # 成交或部分失败后重新读仓，令心跳反映本轮结束时的真实快照。
        try:
            positions = await execution._get_positions(var, selected)
        except Exception as exc:  # noqa: BLE001 心跳仍需保存其他已知字段
            conclusion = f"{conclusion}；结束读仓失败：{type(exc).__name__}: {exc}"

        notionals = {
            leg.underlying: _position_notional(
                positions.get(leg.underlying), prices[leg.underlying]
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
        heartbeat = {
            "timestamp": observed_at.isoformat(),
            "structure": selected.name,
            "conclusion": conclusion,
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
                _schedule_payload(schedule) if selected.has_xaus else None
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
            "exit_carry_consecutive_rounds": exit_carry_rounds,
            "exit_carry_required_rounds": EXIT_CARRY_CONSECUTIVE_ROUNDS,
            "exit_carry_observation": exit_carry_observation,
            "consecutive_failures": consecutive_failures,
            "dry_run": dry_run,
            "auto_open_attempted": auto_open_attempted,
            "auto_open_conclusion": auto_open_conclusion,
            "daily_open_attempts": daily_open_attempts,
            "auto_open_incident": auto_open_incident,
        }
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
    """构造真实客户端，执行一轮并始终释放 HTTP 会话。"""
    var = await execution._load()
    try:
        return await run_once(
            var,
            structure=args.structure,
            dry_run=args.dry_run,
            auto_open=args.auto_open,
            auto_open_notional=args.auto_open_notional,
            kill_switch_path=args.kill_switch,
            heartbeat_path=args.heartbeat,
            state_path=args.state,
            audit_path=args.audit_log,
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
    parser.add_argument("--dry-run", action="store_true", help="判定并询价，但不 accept")
    parser.add_argument(
        "--no-auto-open",
        dest="auto_open",
        action="store_false",
        default=True,
        help="关闭自动开仓，但保留全部自动平仓与风控",
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
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
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
