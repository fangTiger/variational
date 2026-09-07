"""Variational swap carry 多腿人工执行工具：open / status / close。

结构由具名配置定义，腿顺序就是开仓及优先平仓顺序。第一腿成交后，其余腿按
相对权重配平；任一后续腿失败时，对所有已开腿逐一执行 ``reduce_only`` 回滚。
本工具只供人工值守、小额实盘，不包含持久化状态机。

用法（实盘 accept 必须在放行 IP 上执行）：
    PYTHONPATH=. .venv/bin/python -m tools.hedge_swap_carry status
    PYTHONPATH=. .venv/bin/python -m tools.hedge_swap_carry open --dry-run
    PYTHONPATH=. .venv/bin/python -m tools.hedge_swap_carry open --yes
    PYTHONPATH=. .venv/bin/python -m tools.hedge_swap_carry close --yes
"""

from __future__ import annotations

# 必须在导入交易相关依赖前配好 CA。
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from collections.abc import Mapping, MutableMapping, Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import ROUND_DOWN, Decimal, InvalidOperation  # noqa: E402
from pathlib import Path

from infra.data_paths import data_dir  # noqa: E402
from typing import Any  # noqa: E402

from adapters.base import Position, Side  # noqa: E402
from adapters.variational_client import (  # noqa: E402
    Session,
    VariationalClient,
    VariationalJurisdictionError,
)
from engine.swap_trading_schedule import (  # noqa: E402
    SwapTradingSchedule,
    parse_trading_schedule,
)


# 首仓决策值 $2,000/腿（2026-09-05），见
# docs/plans/2026-09-05-swap-carry-首仓执行计划.md。
# 硬上限取决策值的 1.5 倍：留出加仓余量，同时挡住"多打一个零"这类手滑。
DEFAULT_NOTIONAL_USD = Decimal("50")
MAX_NOTIONAL_USD = Decimal("3000")
PRE_CLOSE_FREEZE = timedelta(minutes=30)
XAUS_MIN_QTY = Decimal("0.00003")
XAUS_QTY_STEP = Decimal("0.00001")
XAU_PROBE_QTY = Decimal("0.001")
XAU_FALLBACK_QTY_STEP = Decimal("0.001")

_CONFIRM_TRIES = 6
_FLAT_TRIES = 6
_POLL_DELAY_S = 1.5
_TRANSFER_PAGE_LIMIT = 100
_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SWAP_CARRY_KILL_SWITCH = data_dir() / "swap_carry.kill"
SWAP_CARRY_GUARD_HEARTBEAT = (
    data_dir() / "swap_carry_guard_heartbeat.json"
)
SWAP_CARRY_GUARD_STATE = data_dir() / "swap_carry_guard_state.json"
SWAP_CARRY_GUARD_STALE_AFTER = timedelta(minutes=15)
_ANSI_RED = "\033[31m"
_ANSI_RESET = "\033[0m"


@dataclass(frozen=True)
class CarryLeg:
    """Swap carry 单腿的 Variational 合约参数与相对权重。"""

    underlying: str
    open_side: Side
    instrument_type: str
    funding_interval_s: int
    kind: str | None
    weight: Decimal

    def __post_init__(self) -> None:
        """拒绝无法参与可靠配平的非法腿配置。"""
        weight = Decimal(str(self.weight))
        if not weight.is_finite() or weight <= 0:
            raise ValueError(f"{self.underlying} 权重必须为有限正数")


@dataclass(frozen=True)
class CarryStructure:
    """具名 carry 结构；腿顺序同时约束开仓和风险退出顺序。"""

    name: str
    legs: tuple[CarryLeg, ...]

    def __post_init__(self) -> None:
        """结构加载时校验唯一标的、受限腿顺序和 delta 中性。"""
        if not self.name.strip():
            raise ValueError("carry 结构名不能为空")
        if len(self.legs) < 2:
            raise ValueError(f"{self.name} 至少需要两条腿")
        underlyings = [leg.underlying for leg in self.legs]
        if len(set(underlyings)) != len(underlyings):
            raise ValueError(f"{self.name} 不允许重复标的")
        if "XAUS" in underlyings and underlyings[0] != "XAUS":
            raise ValueError(f"{self.name} 的受限腿 XAUS 必须排在最前")
        long_weight = sum(
            (leg.weight for leg in self.legs if leg.open_side is Side.BUY),
            Decimal("0"),
        )
        short_weight = sum(
            (leg.weight for leg in self.legs if leg.open_side is Side.SELL),
            Decimal("0"),
        )
        if long_weight != short_weight:
            raise ValueError(
                f"{self.name} 不是 delta 中性结构："
                f"多腿权重={long_weight}，空腿权重={short_weight}"
            )

    @property
    def has_xaus(self) -> bool:
        """返回结构是否包含有休市窗口的 XAUS。"""
        return any(leg.underlying == "XAUS" for leg in self.legs)

    @property
    def neutral_weight(self) -> Decimal:
        """返回 delta 中性结构任意一侧的总权重。"""
        return sum(
            (leg.weight for leg in self.legs if leg.open_side is Side.BUY),
            Decimal("0"),
        )


@dataclass(frozen=True)
class PreparedQuote:
    """已经严格解析、可供人工检查或 accept 的一腿报价。"""

    leg: CarryLeg
    side: Side
    qty: Decimal
    payload: Mapping[str, Any]
    execution_price: Decimal
    notional_usd: Decimal
    initial_margin_ratio: Decimal

    @property
    def required_margin_usd(self) -> Decimal:
        """按报价声明的初始保证金率计算本腿所需抵押。"""
        return self.notional_usd * self.initial_margin_ratio


XAUS_LEG = CarryLeg(
    underlying="XAUS",
    open_side=Side.BUY,
    instrument_type="swap",
    funding_interval_s=0,
    kind="commodity",
    weight=Decimal("1"),
)
XAU_LEG = CarryLeg(
    underlying="XAU",
    open_side=Side.SELL,
    instrument_type="perpetual_rwa_future",
    funding_interval_s=3600,
    kind="commodity",
    weight=Decimal("1"),
)
XAU_LONG_LEG = CarryLeg(
    underlying="XAU",
    open_side=Side.BUY,
    instrument_type="perpetual_rwa_future",
    funding_interval_s=3600,
    kind="commodity",
    weight=Decimal("1"),
)
XAUT_LEG = CarryLeg(
    underlying="XAUT",
    open_side=Side.SELL,
    instrument_type="perpetual_future",
    funding_interval_s=3600,
    kind=None,
    weight=Decimal("1"),
)
XAUT_DOUBLE_LEG = CarryLeg(
    underlying="XAUT",
    open_side=Side.SELL,
    instrument_type="perpetual_future",
    funding_interval_s=3600,
    kind=None,
    weight=Decimal("2"),
)

XAUS_XAU = CarryStructure("XAUS_XAU", (XAUS_LEG, XAU_LEG))
XAU_XAUT = CarryStructure("XAU_XAUT", (XAU_LONG_LEG, XAUT_LEG))
TRIPLE = CarryStructure("TRIPLE", (XAUS_LEG, XAU_LONG_LEG, XAUT_DOUBLE_LEG))
STRUCTURES = {
    structure.name: structure for structure in (XAUS_XAU, XAU_XAUT, TRIPLE)
}
DEFAULT_STRUCTURE = XAU_XAUT


def resolve_structure(value: CarryStructure | str) -> CarryStructure:
    """把结构对象或名称解析为已校验的具名结构。"""
    if isinstance(value, CarryStructure):
        return value
    try:
        return STRUCTURES[str(value).strip().upper()]
    except KeyError as exc:
        choices = "、".join(STRUCTURES)
        raise ValueError(f"未知 carry 结构 {value!r}；可选：{choices}") from exc


def _opening_plan(
    structure: CarryStructure | str = DEFAULT_STRUCTURE,
) -> tuple[CarryLeg, ...]:
    """返回结构声明的不可调换开仓顺序。"""
    return resolve_structure(structure).legs


def remove_proxy_environment(
    environment: MutableMapping[str, str],
) -> tuple[str, ...]:
    """在任何网络访问前移除可能改变出口地区的代理环境变量。"""
    removed: list[str] = []
    for name in _PROXY_ENV_NAMES:
        if name in environment:
            environment.pop(name)
            removed.append(name)
    return tuple(removed)


def _load_environment_without_proxy() -> tuple[str, ...]:
    """加载本地凭证，并在加载前后各清理一次代理变量。"""
    removed = list(remove_proxy_environment(os.environ))
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    removed.extend(remove_proxy_environment(os.environ))
    return tuple(dict.fromkeys(removed))


async def _load() -> VariationalClient:
    """构造真实客户端；调用前保证代理变量已经清除。"""
    removed = _load_environment_without_proxy()
    if removed:
        print(f"已清除代理环境变量：{'、'.join(removed)}")
    return VariationalClient(Session.from_env())


def _decimal(
    value: object,
    *,
    label: str,
    positive: bool = False,
) -> Decimal:
    """严格解析有限 Decimal，可选择要求正数。"""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效十进制数") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} 必须为有限数")
    if positive and parsed <= 0:
        raise ValueError(f"{label} 必须大于 0")
    return parsed


def _validate_notional(notional: Decimal) -> Decimal:
    """在任何网络调用前执行单腿名义硬上限检查。"""
    parsed = _decimal(notional, label="每腿名义", positive=True)
    if parsed > MAX_NOTIONAL_USD:
        raise SystemExit(
            f"❌ 每腿名义 ${parsed} 超过硬上限 ${MAX_NOTIONAL_USD}，已拒绝执行"
        )
    return parsed


def _round_qty(qty: Decimal, step: Decimal) -> Decimal:
    """按数量步长向下对齐，避免放大目标名义。"""
    if step <= 0:
        raise ValueError("数量步长必须大于 0")
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def _format_result(result: object) -> str:
    """把成交响应压缩为适合人工终端查看的文本。"""
    try:
        return json.dumps(result, ensure_ascii=False)[:180]
    except TypeError:
        return str(result)[:180]


def _instrument_record(
    metadata: object,
    leg: CarryLeg,
) -> Mapping[str, Any]:
    """从 supported_assets 精确选择指定标的和工具类型。"""
    if not isinstance(metadata, Mapping):
        raise ValueError("supported_assets 响应不是对象")
    records = next(
        (
            value
            for key, value in metadata.items()
            if str(key).upper() == leg.underlying
        ),
        None,
    )
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError(f"supported_assets 缺少 {leg.underlying} 记录")
    for record in records:
        if (
            isinstance(record, Mapping)
            and str(record.get("instrument_type") or "") == leg.instrument_type
        ):
            return record
    raise ValueError(
        f"supported_assets 缺少 {leg.underlying} {leg.instrument_type} 记录"
    )


def _schedule_from_metadata(
    metadata: object,
    now: datetime,
) -> tuple[Mapping[str, Any], SwapTradingSchedule]:
    """只使用交易所元数据解析 XAUS 时段；异常由解析器失败关闭。"""
    record = _instrument_record(metadata, XAUS_LEG)
    schedule = parse_trading_schedule(
        record.get("trading_sessions"),
        record.get("trading_schedule"),
        record.get("market_status"),
        now,
    )
    return record, schedule


async def _load_schedule(
    var: Any,
    *,
    now: datetime | None = None,
) -> tuple[object, Mapping[str, Any], SwapTradingSchedule]:
    """读取元数据并返回统一 UTC 观察时点与 XAUS 时段。"""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("now 必须包含时区")
    observed_at = observed_at.astimezone(timezone.utc)
    metadata = await var.get_supported_assets()
    record, schedule = _schedule_from_metadata(metadata, observed_at)
    return metadata, record, schedule


def _guard_open_schedule(schedule: SwapTradingSchedule) -> None:
    """休市、元数据失败关闭及临近休市时一律拒绝开仓。"""
    if not schedule.is_tradable:
        raise SystemExit(f"❌ XAUS 当前不可交易，拒绝 open：{schedule.reason}")
    if schedule.time_until_close is None:
        raise SystemExit("❌ XAUS 缺少距下次休市时间，按不可交易处理")
    if schedule.time_until_close < PRE_CLOSE_FREEZE:
        minutes = schedule.time_until_close.total_seconds() / 60
        raise SystemExit(
            "❌ 距 XAUS 休市不足 30 分钟"
            f"（约 {minutes:.1f} 分钟），拒绝 open；"
            "第二腿失败后可能来不及回滚第一腿"
        )


async def _get_position(var: Any, leg: CarryLeg) -> Position:
    """所有本结构仓位查询强制精确匹配 underlying。"""
    return await var.get_position(leg.underlying, exact=True)


async def _get_positions(
    var: Any,
    structure: CarryStructure | str,
) -> dict[str, Position]:
    """按结构顺序读取全部精确仓位。"""
    selected = resolve_structure(structure)
    positions: dict[str, Position] = {}
    for leg in selected.legs:
        positions[leg.underlying] = await _get_position(var, leg)
    return positions


def _positions_net_delta(
    structure: CarryStructure | str,
    positions: Mapping[str, Position],
) -> Decimal:
    """按实际有符号数量加总结构净 delta。"""
    selected = resolve_structure(structure)
    return sum(
        (positions[leg.underlying].signed_size for leg in selected.legs),
        Decimal("0"),
    )


async def _net_delta(
    var: Any,
    structure: CarryStructure | str = DEFAULT_STRUCTURE,
) -> tuple[Decimal, ...]:
    """兼容返回各腿有符号数量，并在末尾附加净 delta。"""
    selected = resolve_structure(structure)
    positions = await _get_positions(var, selected)
    sizes = tuple(positions[leg.underlying].signed_size for leg in selected.legs)
    return (*sizes, _positions_net_delta(selected, positions))


async def _request_quote(
    var: Any,
    leg: CarryLeg,
    side: Side,
    qty: Decimal,
) -> Mapping[str, Any]:
    """用高层方法询价，并固定传递正确的 swap/RWA 描述符。"""
    payload = await var.request_quote(
        leg.underlying,
        side.value.lower(),
        qty,
        instrument_type=leg.instrument_type,
        funding_interval_s=leg.funding_interval_s,
        kind=leg.kind,
    )
    if not isinstance(payload, Mapping):
        raise ValueError(f"{leg.underlying} 报价响应不是对象")
    return payload


def _quote_price(payload: Mapping[str, Any], side: Side) -> Decimal:
    """按下单方向读取可成交价格，缺失时才降级到 mark。"""
    keys = ("ask", "price", "mark_price", "mark") if side is Side.BUY else (
        "bid",
        "price",
        "mark_price",
        "mark",
    )
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return _decimal(value, label=f"报价 {key}", positive=True)
    raise ValueError("报价缺少可成交价格")


def _quantity_constraints(
    payload: Mapping[str, Any],
    side: Side,
    *,
    fallback_minimum: Decimal,
    fallback_step: Decimal,
) -> tuple[Decimal, Decimal]:
    """读取方向相关数量限制；仅采用已知合约事实作为缺字段降级值。"""
    limits = payload.get("qty_limits")
    side_limits: object = None
    if isinstance(limits, Mapping):
        # Variational 前端约定：买单读 bid 限制，卖单读 ask 限制。
        side_limits = limits.get("bid" if side is Side.BUY else "ask")
    if not isinstance(side_limits, Mapping):
        side_limits = {}
    raw_minimum = side_limits.get("min_qty")
    raw_step = side_limits.get("min_qty_tick")
    minimum = (
        _decimal(raw_minimum, label="min_qty", positive=True)
        if raw_minimum not in (None, "")
        else fallback_minimum
    )
    step = (
        _decimal(raw_step, label="min_qty_tick", positive=True)
        if raw_step not in (None, "")
        else fallback_step
    )
    return minimum, step


def _initial_margin_ratio(
    payload: Mapping[str, Any],
    side: Side,
    notional_usd: Decimal,
) -> Decimal:
    """按方向读取初始保证金金额，并除以本腿名义得到保证金率。"""
    requirements = payload.get("margin_requirements")
    if not isinstance(requirements, Mapping):
        raise ValueError("报价缺少 margin_requirements")

    delta_key = "ask_margin_delta" if side is Side.BUY else "bid_margin_delta"
    margin_delta = requirements.get(delta_key)
    if not isinstance(margin_delta, Mapping):
        raise ValueError(f"报价缺少 margin_requirements.{delta_key}")
    initial_margin = _decimal(
        margin_delta.get("initial_margin"),
        label=f"{delta_key}.initial_margin",
        positive=True,
    )
    notional = _decimal(notional_usd, label="本腿名义", positive=True)
    ratio = initial_margin / notional
    if ratio > 1:
        raise ValueError(
            f"{delta_key}.initial_margin 除以本腿名义后必须是不超过 1 的比例"
        )
    return ratio


def _prepare_quote(
    leg: CarryLeg,
    side: Side,
    qty: Decimal,
    payload: Mapping[str, Any],
    *,
    require_margin: bool = True,
) -> PreparedQuote:
    """把报价转成包含名义和保证金需求的严格结构。

    ``require_margin`` 只对**开仓**为真：开仓必须知道要占用多少保证金，
    缺 ``margin_requirements`` 就该拒绝。

    平仓路径必须传 ``False``：平仓是减少风险的动作，不需要保证金字段，
    更不能因为报价里恰好没带它就抛异常——那等于「平不掉仓」，
    是比缺字段严重得多的后果。此时 ``initial_margin_ratio`` 记为 0。
    """
    price = _quote_price(payload, side)
    notional_usd = qty * price
    margin_ratio = (
        _initial_margin_ratio(payload, side, notional_usd)
        if require_margin
        else Decimal(0)
    )
    return PreparedQuote(
        leg=leg,
        side=side,
        qty=qty,
        payload=payload,
        execution_price=price,
        notional_usd=notional_usd,
        initial_margin_ratio=margin_ratio,
    )


async def _prepare_open_quotes(
    var: Any,
    target_notional: Decimal,
    structure: CarryStructure | str = DEFAULT_STRUCTURE,
    *,
    require_margin: bool = True,
) -> tuple[tuple[PreparedQuote, ...], Decimal]:
    """询价并按相对权重生成全部腿的计划；全程不 accept。"""
    selected = resolve_structure(structure)
    probes: list[Mapping[str, Any]] = []
    constraints: list[tuple[Decimal, Decimal]] = []
    first_leg = selected.legs[0]
    for leg in selected.legs:
        fallback_minimum = (
            XAUS_MIN_QTY if leg.underlying == "XAUS" else XAU_FALLBACK_QTY_STEP
        )
        fallback_step = (
            XAUS_QTY_STEP if leg.underlying == "XAUS" else XAU_FALLBACK_QTY_STEP
        )
        probe_qty = XAUS_MIN_QTY if leg.underlying == "XAUS" else XAU_PROBE_QTY
        payload = await _request_quote(var, leg, leg.open_side, probe_qty)
        probes.append(payload)
        constraints.append(
            _quantity_constraints(
                payload,
                leg.open_side,
                fallback_minimum=fallback_minimum,
                fallback_step=fallback_step,
            )
        )

    first_minimum = max(
        minimum * first_leg.weight / leg.weight
        for leg, (minimum, _step) in zip(selected.legs, constraints, strict=True)
    )
    first_step = max(
        step * first_leg.weight / leg.weight
        for leg, (_minimum, step) in zip(selected.legs, constraints, strict=True)
    )
    try:
        # 目标名义按报价标记价换算数量；最终成交价名义仍会在下方严格校验。
        reference_price = _decimal(
            probes[0].get("mark_price"),
            label=f"{first_leg.underlying} 标记价",
            positive=True,
        )
    except ValueError:
        reference_price = _quote_price(probes[0], first_leg.open_side)
    first_qty = _round_qty(target_notional / reference_price, first_step)
    if first_qty < first_minimum:
        raise SystemExit(
            f"❌ 目标名义过小：第一腿 qty={first_qty}，"
            f"按结构权重折算的最小数量为 {first_minimum}"
        )

    quotes: list[PreparedQuote] = []
    for leg, (_minimum, step) in zip(selected.legs, constraints, strict=True):
        qty = first_qty * leg.weight / first_leg.weight
        if _round_qty(qty, step) != qty:
            raise SystemExit(
                f"❌ {selected.name} 权重无法按 {leg.underlying} 数量步长 {step} 精确配平"
            )
        payload = await _request_quote(var, leg, leg.open_side, qty)
        quotes.append(_prepare_quote(
            leg, leg.open_side, qty, payload, require_margin=require_margin,
        ))

    for quote in quotes:
        if quote.notional_usd > MAX_NOTIONAL_USD:
            raise SystemExit(
                f"❌ {quote.leg.underlying} 报价名义 ${quote.notional_usd:.2f} "
                f"超过硬上限 ${MAX_NOTIONAL_USD}"
            )
    return tuple(quotes), first_step


async def _check_margin(
    var: Any,
    *quotes: PreparedQuote,
) -> None:
    """确认账户权益足以覆盖全部腿报价的初始保证金。"""
    balance = await var.get_balance()
    equity = _decimal(getattr(balance, "equity", None), label="账户权益", positive=True)
    required = sum(
        (quote.required_margin_usd for quote in quotes),
        Decimal("0"),
    )
    print(
        f"保证金检查：{len(quotes)} 腿所需≈${required:.4f}，"
        f"当前可用抵押依据（账户权益）=${equity:.4f}"
    )
    if equity < required:
        raise SystemExit(
            f"❌ 可用保证金不足：全部腿需要约 ${required:.4f}，"
            f"账户权益 ${equity:.4f}"
        )


def _print_open_plan(
    structure: CarryStructure | str,
    quotes: Sequence[PreparedQuote],
) -> None:
    """在任何 accept 前输出完整多腿动作。"""
    selected = resolve_structure(structure)
    print(f"swap carry 开仓计划：结构={selected.name}（固定顺序，不可调换）")
    for index, quote in enumerate(quotes, 1):
        action = "买入" if quote.side is Side.BUY else "卖出"
        mode = "isolated" if quote.leg.underlying == "XAUS" else "全仓"
        print(
            f"  [{index}/{len(quotes)}] {action} {quote.leg.underlying} "
            f"{quote.qty}（权重 {quote.leg.weight}），"
            f"名义≈${quote.notional_usd:.2f}，{mode}"
        )
    print(
        f"  预计初始保证金合计≈$"
        f"{sum((quote.required_margin_usd for quote in quotes), Decimal('0')):.4f}"
    )


async def _accept_quote(var: Any, quote: PreparedQuote, *, reduce_only: bool) -> Any:
    """接受一笔已经打印过动作的 RFQ；缺 quote_id 时失败关闭。"""
    quote_id = quote.payload.get("quote_id")
    if not isinstance(quote_id, str) or not quote_id:
        raise ValueError(f"{quote.leg.underlying} 报价缺少 quote_id，拒绝 accept")
    result = await var.accept_quote(
        quote_id=quote_id,
        side=quote.side.value.lower(),
        max_slippage=float(getattr(var, "_max_slippage", 0.01)),
        is_reduce_only=reduce_only,
    )
    # 记账失败不得把已成交误报成下单失败，从而触发错误的重试或回滚。
    from engine import swap_carry_cost as cost
    import logging
    try:
        rfq_id = result.get("rfq_id") if isinstance(result, Mapping) else getattr(result, "rfq_id", None)
        parent = getattr(var, "cost_parent_id", None)
        fill = dict(status="succeeded", market=quote.leg.underlying,
                    side=quote.side.value.lower(), rfq_id=rfq_id,
                    filled_quantity=str(quote.qty), reduce_only=reduce_only,
                    execution_price=str(_quote_price(quote.payload, quote.side)),
                    quote_mid=str((_decimal(quote.payload.get("bid"), label="bid", positive=True)
                                  + _decimal(quote.payload.get("ask"), label="ask", positive=True)) / 2))
        cost.append(cost.ACTIVE_PATH.get(), cost.fill_events(
            fill, datetime.now(timezone.utc).isoformat(),
            parent_id=parent if isinstance(parent, str) else None))
    except Exception as exc:  # noqa: BLE001 成交结果优先，成本缺口必须留告警
        logging.getLogger(__name__).warning("成交已成功，但成本台账写入失败：%s", exc)
    return result


async def _confirm_first_leg_qty(
    var: Any,
    expected_qty: Decimal,
    leg: CarryLeg = XAUS_LEG,
) -> Decimal:
    """轮询第一腿实仓数量；持续延迟时按 RFQ 的全量成交数量继续配平。"""
    for attempt in range(1, _CONFIRM_TRIES + 1):
        position = await _get_position(var, leg)
        correct_direction = (
            position.signed_size > 0
            if leg.open_side is Side.BUY
            else position.signed_size < 0
        )
        if correct_direction:
            print(
                f"   第 {attempt} 次回读确认 {leg.underlying} "
                f"成交数量 Q={abs(position.signed_size)}"
            )
            return abs(position.signed_size)
        if position.signed_size != 0:
            raise RuntimeError(
                f"{leg.underlying} 回读方向异常：{position.signed_size}，"
                "停止自动动作并人工检查"
            )
        if attempt < _CONFIRM_TRIES:
            await asyncio.sleep(_POLL_DELAY_S)
    print(
        f"⚠️ /positions 在轮询窗口内仍未反映 {leg.underlying} 成交；"
        f"RFQ accept 已返回成交编号，按已报全量 Q={expected_qty} 继续配平，"
        "禁止据即时零仓误判失败"
    )
    return expected_qty


async def _rollback_opened_legs(
    var: Any,
    opened_quotes: Sequence[PreparedQuote],
) -> None:
    """按结构顺序逐一 reduce_only 回滚所有已确认开出的腿。"""
    failures: list[str] = []
    for opened in opened_quotes:
        close_side = Side.SELL if opened.side is Side.BUY else Side.BUY
        try:
            payload = await _request_quote(
                var,
                opened.leg,
                close_side,
                opened.qty,
            )
            quote = _prepare_quote(
                opened.leg,
                close_side,
                opened.qty,
                payload,
                require_margin=False,
            )
            print(
                f">>> 回滚：reduce_only {close_side.value.lower()} "
                f"{opened.leg.underlying} {opened.qty}"
            )
            result = await _accept_quote(var, quote, reduce_only=True)
            print(
                f"   {opened.leg.underlying} 已回滚：{_format_result(result)}"
            )
        except Exception as exc:  # noqa: BLE001 必须继续尝试回滚其余已开腿
            failures.append(f"{opened.leg.underlying}: {exc}")
    if failures:
        message = (
            "🚨🚨🚨 最高级别告警：后续腿失败且已开腿 reduce_only 回滚失败！\n"
            f"   回滚错误：{'；'.join(failures)}\n"
            "   已停止一切自动动作，请立即人工处理当前裸仓。"
        )
        print(message)
        raise SystemExit(message)


async def _rollback_first_leg(var: Any, qty: Decimal) -> None:
    """保留旧调用入口，并委托给通用多腿回滚。"""
    await _rollback_opened_legs(
        var,
        (
            PreparedQuote(
                leg=XAUS_LEG,
                side=Side.BUY,
                qty=qty,
                payload={},
                execution_price=Decimal("0"),
                notional_usd=Decimal("0"),
                initial_margin_ratio=Decimal("0"),
            ),
        ),
    )


async def _await_net_delta(
    var: Any,
    tolerance: Decimal,
    structure: CarryStructure | str = DEFAULT_STRUCTURE,
) -> tuple[Decimal, ...]:
    """轮询最终净 delta，容忍后续腿的 /positions 最终一致延迟。"""
    selected = resolve_structure(structure)
    result = await _net_delta(var, selected)
    for attempt in range(_FLAT_TRIES):
        if abs(result[-1]) <= tolerance and all(size != 0 for size in result[:-1]):
            return result
        if attempt + 1 < _FLAT_TRIES:
            await asyncio.sleep(_POLL_DELAY_S)
            result = await _net_delta(var, selected)
    return result


async def cmd_open(
    var: Any,
    notional: Decimal = DEFAULT_NOTIONAL_USD,
    *,
    structure: CarryStructure | str = DEFAULT_STRUCTURE,
    yes: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> None:
    """检查全部前置条件，并按结构顺序开出所有腿。"""
    selected = resolve_structure(structure)
    if SWAP_CARRY_KILL_SWITCH.exists():
        raise SystemExit(
            f"❌ kill switch 已激活（{SWAP_CARRY_KILL_SWITCH}），拒绝 open"
        )
    target_notional = _validate_notional(notional)
    schedule: SwapTradingSchedule | None = None
    if selected.has_xaus:
        _metadata, _record, schedule = await _load_schedule(var, now=now)
        _guard_open_schedule(schedule)

    initial_positions = await _get_positions(var, selected)
    if any(not position.is_flat for position in initial_positions.values()):
        detail = " ".join(
            f"{leg.underlying}={initial_positions[leg.underlying].signed_size}"
            for leg in selected.legs
        )
        raise SystemExit(
            f"❌ 已有目标持仓（{detail}），请先处理后再 open"
        )

    quotes, tolerance = await _prepare_open_quotes(
        var,
        target_notional,
        selected,
    )
    await _check_margin(var, *quotes)
    _print_open_plan(selected, quotes)
    if schedule is not None:
        print(
            f"XAUS 距下次休市 {schedule.time_until_close}，"
            "休市时刻="
            f"{schedule.next_close_at.isoformat() if schedule.next_close_at else '无数据'}"
        )

    if dry_run:
        print("[DRY-RUN] 全部检查与多腿报价已完成；未调用 accept，不会成交。")
        return
    if not yes:
        print("未提供 --yes：仅完成检查与报价，未调用 accept。确认后请加 --yes。")
        return

    opened: list[PreparedQuote] = []
    first_quote = quotes[0]
    print(
        f">>> [1/{len(quotes)}] {first_quote.side.value.lower()} "
        f"{first_quote.leg.underlying} {first_quote.qty} …"
    )
    try:
        first_result = await _accept_quote(var, first_quote, reduce_only=False)
    except VariationalJurisdictionError as exc:
        raise SystemExit(
            f"❌ {first_quote.leg.underlying} accept 被地区封锁：{exc}\n"
            "   需在放行 IP 上执行。"
        ) from exc
    except Exception as exc:  # noqa: BLE001 第一腿未确认成交时不得继续
        raise SystemExit(
            f"❌ {first_quote.leg.underlying} 第一腿下单失败，已停止：{exc}"
        ) from exc
    opened.append(first_quote)
    print(
        f"   {first_quote.leg.underlying} accept 成功："
        f"{_format_result(first_result)}"
    )

    try:
        filled_qty = await _confirm_first_leg_qty(
            var,
            first_quote.qty,
            first_quote.leg,
        )
    except Exception as exc:  # noqa: BLE001 首腿回读异常也必须回滚
        await _rollback_opened_legs(var, opened)
        raise SystemExit(f"第一腿回读失败，已回滚：{exc}") from exc

    for index, planned_quote in enumerate(quotes[1:], 2):
        expected_qty = (
            filled_qty * planned_quote.leg.weight / first_quote.leg.weight
        )
        quote = planned_quote
        try:
            if expected_qty != planned_quote.qty:
                payload = await _request_quote(
                    var,
                    planned_quote.leg,
                    planned_quote.side,
                    expected_qty,
                )
                quote = _prepare_quote(
                    planned_quote.leg,
                    planned_quote.side,
                    expected_qty,
                    payload,
                )
                if quote.notional_usd > MAX_NOTIONAL_USD:
                    raise ValueError(
                        f"回读 Q 对应名义 ${quote.notional_usd:.2f} 超过硬上限"
                    )
            print(
                f">>> [{index}/{len(quotes)}] 按第一腿成交 Q={filled_qty} "
                f"{quote.side.value.lower()} {quote.leg.underlying} {quote.qty} …"
            )
            result = await _accept_quote(var, quote, reduce_only=False)
            opened.append(quote)
            print(
                f"   {quote.leg.underlying} accept 成功：{_format_result(result)}"
            )
        except VariationalJurisdictionError as exc:
            print(
                f"❌ {quote.leg.underlying} 第 {index} 腿被地区封锁：{exc}；"
                "需在放行 IP 上执行。"
            )
            await _rollback_opened_legs(var, opened)
            raise SystemExit(
                f"第 {index} 腿被地区封锁，已开腿已回滚；"
                "后续需在放行 IP 上执行。"
            ) from exc
        except Exception as exc:  # noqa: BLE001 任一后续腿失败都回滚全部已开腿
            print(f"❌ {quote.leg.underlying} 第 {index} 腿下单失败：{exc}")
            await _rollback_opened_legs(var, opened)
            raise SystemExit(
                f"第 {index} 腿失败，已开腿已回滚，未留下目标裸仓。"
            ) from exc

    result = await _await_net_delta(var, tolerance, selected)
    sizes = result[:-1]
    net = result[-1]
    neutral = abs(net) <= tolerance
    detail = " ".join(
        f"{leg.underlying}={size}"
        for leg, size in zip(selected.legs, sizes, strict=True)
    )
    print(
        f"\n开仓完成：结构={selected.name} {detail} 净 delta={net} "
        f"{'✅ 近似中性' if neutral else '🚨 有敞口，请立即人工检查'}"
    )


def _format_duration(value: timedelta | None) -> str:
    """把时长格式化为人工易读文本。"""
    if value is None:
        return "无数据"
    total_seconds = max(0, int(value.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f"{hours}小时{minutes}分钟"


def _metadata_price(metadata: object, leg: CarryLeg) -> Decimal | None:
    """尽力读取元数据 mark；失败时由 status 如实显示无数据。"""
    try:
        record = _instrument_record(metadata, leg)
        for key in ("price", "mark_price", "index_price"):
            value = record.get(key)
            if value not in (None, ""):
                return _decimal(value, label=f"{leg.underlying} {key}", positive=True)
    except (TypeError, ValueError):
        return None
    return None


def _format_liquidation(
    info: object,
    position: Position,
) -> str:
    """展示 API 权威强平价与按当前 mark 计算的方向距离。"""
    if info is None:
        return "无数据"
    if not isinstance(info, tuple) or len(info) != 2:
        raise ValueError("get_liquidation_info 返回结构无效")
    mark = _decimal(info[0], label="强平信息 mark", positive=True)
    liquidation = _decimal(info[1], label="强平价", positive=True)
    if position.signed_size > 0:
        distance = (mark - liquidation) / mark
    elif position.signed_size < 0:
        distance = (liquidation - mark) / mark
    else:
        return "无持仓"
    return f"强平价={liquidation}，距离={distance:.2%}（mark={mark}）"


def _swap_long_rate(snapshot: object) -> Decimal:
    """从结构化 swap funding 快照读取我方多头年化费率。"""
    try:
        value = snapshot.upcoming.long_rate.normalized_annual_rate  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise ValueError("swap funding 响应缺少 upcoming.long_rate") from exc
    return _decimal(value, label="XAUS 多头资金费率")


async def _funding_rate_for_leg(var: Any, leg: CarryLeg) -> Decimal:
    """读取统一口径费率：正数表示多头支付、空头收取。"""
    if leg.instrument_type == "swap":
        if leg.open_side is not Side.BUY:
            raise ValueError("当前只支持读取 swap 多腿费率")
        signed_long_rate = _swap_long_rate(await var.get_swap_funding(leg.underlying))
        return -signed_long_rate
    return _decimal(
        await var.get_funding_rate(leg.underlying, leg.instrument_type),
        label=f"{leg.underlying} 永续资金费率",
    )


def _weighted_net_carry(
    structure: CarryStructure | str,
    rates: Mapping[str, Decimal],
) -> Decimal:
    """按方向和权重计算净 carry，并按单侧中性权重归一化。"""
    selected = resolve_structure(structure)
    numerator = sum(
        (
            rates[leg.underlying] * leg.weight
            * (Decimal("1") if leg.open_side is Side.SELL else Decimal("-1"))
            for leg in selected.legs
        ),
        Decimal("0"),
    )
    return numerator / selected.neutral_weight


async def _load_funding_rates(
    var: Any,
    structure: CarryStructure | str,
) -> dict[str, Decimal]:
    """按结构顺序读取全部腿的统一口径费率。"""
    selected = resolve_structure(structure)
    rates: dict[str, Decimal] = {}
    for leg in selected.legs:
        rates[leg.underlying] = await _funding_rate_for_leg(var, leg)
    return rates


def _transfer_underlying(row: Mapping[str, Any]) -> str | None:
    """按真实 /transfers schema 提取 reference_instrument.underlying。"""
    reference = row.get("reference_instrument")
    if isinstance(reference, Mapping):
        value = reference.get("underlying")
        if value not in (None, ""):
            return str(value).upper()
    value = row.get("underlying")
    return str(value).upper() if value not in (None, "") else None


async def _settled_funding_by_leg(
    var: Any,
    *,
    since: datetime | None = None,
    structure: CarryStructure | str = DEFAULT_STRUCTURE,
) -> dict[str, Decimal]:
    """分页读取 /transfers，并按真实已结算 qty 汇总结构各腿资金费。

    ``since`` 用于面板的本周口径；人工 status 不传时继续展示全部历史。
    """
    if since is not None:
        if since.tzinfo is None:
            raise ValueError("资金费起始时间必须包含时区")
        since = since.astimezone(timezone.utc)
    selected = resolve_structure(structure)
    totals = {leg.underlying: Decimal("0") for leg in selected.legs}
    offset = 0
    object_count: int | None = None
    seen = 0
    while True:
        payload = await var.raw(
            f"/transfers?limit={_TRANSFER_PAGE_LIMIT}&offset={offset}"
        )
        if not isinstance(payload, Mapping):
            raise ValueError("/transfers 响应不是对象")
        rows = payload.get("result")
        pagination = payload.get("pagination")
        if not isinstance(rows, list) or not isinstance(pagination, Mapping):
            raise ValueError("/transfers 缺少 result 或 pagination")
        try:
            current_count = int(pagination.get("object_count"))
        except (TypeError, ValueError) as exc:
            raise ValueError("/transfers object_count 无效") from exc
        object_count = current_count if object_count is None else max(
            object_count, current_count
        )
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("/transfers 流水不是对象")
            if row.get("funding_rate") in (None, ""):
                continue
            if since is not None:
                created_at = _guard_timestamp(row.get("created_at"))
                if created_at is None:
                    raise ValueError("/transfers 资金费流水 created_at 无效")
                if created_at < since:
                    continue
            underlying = _transfer_underlying(row)
            if underlying not in totals:
                continue
            totals[underlying] += _decimal(row.get("qty"), label="资金费流水 qty")
        seen += len(rows)
        if seen >= object_count:
            return totals
        if not rows:
            raise ValueError("/transfers 未取满且分页无法推进")
        offset += _TRANSFER_PAGE_LIMIT


async def cmd_status(
    var: Any,
    *,
    structure: CarryStructure | str | None = None,
    now: datetime | None = None,
) -> None:
    """输出结构、仓位、强平、carry 与实际已结算资金费快照。"""
    selected = _current_structure(structure)
    observed_at = now or datetime.now(timezone.utc)
    _print_guard_status(observed_at)
    if selected is None:
        print("swap carry 状态：结构未知（守护心跳不可用）")
        print("请检查守护进程是否在运行；可用 --structure 显式指定结构。")
        payload = await var.get_positions()
        items = payload.get("positions") if isinstance(payload, Mapping) else payload
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise ValueError("/positions 响应缺少持仓列表")
        print("全部实际持仓（/positions 原始记录）：")
        for item in items:
            print(json.dumps(item, ensure_ascii=False, default=str))
        if not items:
            print("无持仓")
        return
    positions = await _get_positions(var, selected)
    net = _positions_net_delta(selected, positions)
    print(f"swap carry 状态：结构={selected.name}")
    flat_legs = [
        leg.underlying for leg in selected.legs if positions[leg.underlying].is_flat
    ]
    if flat_legs and len(flat_legs) != len(selected.legs):
        remaining = "、".join(
            leg.underlying
            for leg in selected.legs
            if not positions[leg.underlying].is_flat
        )
        print(
            f"🚨🚨🚨 缺腿裸仓告警：只剩 {remaining}！"
            "请停止开仓并立即人工处理。"
        )

    metadata: object | None = None
    schedule: SwapTradingSchedule | None = None
    if selected.has_xaus:
        try:
            metadata, _record, schedule = await _load_schedule(var, now=now)
        except Exception as exc:  # noqa: BLE001 status 必须保留其他只读信息
            print(f"⚠️ XAUS 时段元数据读取失败，按不可交易处理：{exc}")
    else:
        try:
            metadata = await var.get_supported_assets()
        except Exception as exc:  # noqa: BLE001 名义失败不遮蔽仓位
            print(f"⚠️ 合约元数据读取失败：{exc}")

    for leg in selected.legs:
        position = positions[leg.underlying]
        price = _metadata_price(metadata, leg) if metadata is not None else None
        notional = abs(position.signed_size) * price if price is not None else None
        print(
            f"{leg.underlying:<4} 数量={position.signed_size}，权重={leg.weight}，名义="
            f"{'$' + format(notional, '.2f') if notional is not None else '无数据'}"
        )
    print(f"净 delta={net} {'✅ 近似中性' if abs(net) <= XAUS_QTY_STEP else '⚠️ 有敞口'}")

    for leg in selected.legs:
        position = positions[leg.underlying]
        try:
            info = await var.get_liquidation_info(leg.underlying, exact=True)
            text = _format_liquidation(info, position)
        except Exception as exc:  # noqa: BLE001 isolated 缺值必须如实显示
            text = f"无数据（{exc}）"
        print(f"{leg.underlying} 强平：{text}")

    rates: dict[str, Decimal] = {}
    for leg in selected.legs:
        try:
            rate = await _funding_rate_for_leg(var, leg)
            rates[leg.underlying] = rate
            side_name = "多头" if leg.open_side is Side.BUY else "空头"
            contribution = rate if leg.open_side is Side.SELL else -rate
            print(
                f"{leg.underlying} {side_name}当前资金费率收益="
                f"{contribution:.4%} 年化"
            )
        except Exception as exc:  # noqa: BLE001 单腿失败后继续展示其余腿
            print(f"⚠️ {leg.underlying} 资金费率读取失败：{exc}")
    if len(rates) == len(selected.legs):
        print(
            f"净 carry={_weighted_net_carry(selected, rates):.4%} "
            "年化（未扣摩擦）"
        )
    else:
        print("净 carry=无数据")

    if selected.has_xaus:
        if schedule is not None:
            print(
                f"XAUS 时段={'可交易' if schedule.is_tradable else '不可交易'}；"
                f"距下次休市={_format_duration(schedule.time_until_close)}；"
                f"原因={schedule.reason}"
            )
        else:
            print("XAUS 时段=不可交易；距下次休市=无数据")

    try:
        settled = await _settled_funding_by_leg(var, structure=selected)
        total = sum(settled.values(), Decimal("0"))
        detail = "，".join(
            f"{leg.underlying}={settled[leg.underlying]} USDC"
            for leg in selected.legs
        )
        print(
            "累计已结算资金费（/transfers 实际扣款）："
            f"{detail}，"
            f"合计={total} USDC"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 累计已结算资金费读取失败：{exc}")


def _read_guard_json(path: Path) -> Mapping[str, Any] | None:
    """读取守护状态；缺失或损坏时返回 None 并由调用方醒目提示。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _current_structure(
    explicit: CarryStructure | str | None,
) -> CarryStructure | None:
    """人工指定优先，否则只接受心跳中的合法结构，不推测默认结构。"""
    if explicit is not None:
        return resolve_structure(explicit)
    heartbeat = _read_guard_json(SWAP_CARRY_GUARD_HEARTBEAT)
    value = heartbeat.get("structure") if heartbeat is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return resolve_structure(value)
    except ValueError:
        return None


def _guard_timestamp(value: object) -> datetime | None:
    """解析守护心跳 UTC 时间戳。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _print_guard_status(now: datetime) -> None:
    """在人工 status 顶部显示事故状态和心跳新鲜度。"""
    observed_at = now.astimezone(timezone.utc)
    state = _read_guard_json(SWAP_CARRY_GUARD_STATE)
    if state is not None and state.get("status") not in (None, "", "healthy"):
        message = str(state.get("message") or "守护进程存在未清除状态")
        failures = state.get("consecutive_failures", 0)
        print(
            f"{_ANSI_RED}🚨 守护状态={state.get('status')}：{message}；"
            f"连续失败={failures}{_ANSI_RESET}"
        )

    heartbeat = _read_guard_json(SWAP_CARRY_GUARD_HEARTBEAT)
    timestamp = _guard_timestamp(heartbeat.get("timestamp")) if heartbeat else None
    if timestamp is None:
        print(f"{_ANSI_RED}🚨 守护进程心跳缺失或损坏{_ANSI_RESET}")
        return
    age = max(timedelta(0), observed_at - timestamp)
    age_minutes = age.total_seconds() / 60
    message = f"守护进程上次运行于 {age_minutes:.1f} 分钟前"
    if age > SWAP_CARRY_GUARD_STALE_AFTER:
        print(f"{_ANSI_RED}🚨 {message}，已超过 15 分钟{_ANSI_RESET}")
    else:
        print(f"✅ {message}")


async def _close_position_quote(
    var: Any,
    leg: CarryLeg,
    position: Position,
) -> PreparedQuote | None:
    """为已有仓位准备 reduce_only 报价；空仓返回 None。"""
    if position.is_flat:
        return None
    side = Side.SELL if position.signed_size > 0 else Side.BUY
    qty = abs(position.signed_size)
    payload = await _request_quote(var, leg, side, qty)
    return _prepare_quote(leg, side, qty, payload, require_margin=False)


async def _await_flat(
    var: Any,
    structure: CarryStructure | str = DEFAULT_STRUCTURE,
) -> tuple[Decimal, ...]:
    """轮询结构全部腿归零，容忍平仓后的 /positions 最终一致延迟。"""
    selected = resolve_structure(structure)
    result = await _net_delta(var, selected)
    for attempt in range(_FLAT_TRIES):
        if all(size == 0 for size in result[:-1]):
            return result
        if attempt + 1 < _FLAT_TRIES:
            await asyncio.sleep(_POLL_DELAY_S)
            result = await _net_delta(var, selected)
    return result


async def cmd_close(
    var: Any,
    *,
    structure: CarryStructure | str | None = None,
    yes: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> None:
    """按结构顺序平全部腿；XAUS 时段元数据异常不阻挡减仓尝试。"""
    selected = _current_structure(structure)
    if selected is None:
        raise SystemExit(
            "拒绝平仓：结构未知（守护心跳不可用）。"
            "请检查守护进程是否在运行，或显式指定 --structure 后重试。"
        )
    schedule: SwapTradingSchedule | None = None
    market_status: str | None = None
    if selected.has_xaus:
        try:
            _metadata, record, schedule = await _load_schedule(var, now=now)
            market_status = str(record.get("market_status") or "").strip().lower()
        except Exception as exc:  # noqa: BLE001 平仓不能被元数据缺失阻挡
            print(f"⚠️ XAUS 时段状态无法确认，但平仓优先，将继续尝试：{exc}")

        if market_status and market_status != "open":
            message = (
                "⚠️ XAUS 腿此刻平不掉（交易所 market_status 非 open），"
                "其余腿 24/7 可平。为避免自动制造裸腿，本工具未下任何单，"
                "请人工决定是否单独处理其他腿。"
            )
            print(message)
            raise SystemExit(message)
        if schedule is not None and not schedule.is_tradable:
            print(
                f"⚠️ XAUS 时段守卫报告不可交易（{schedule.reason}），"
                "但 close 不受开仓守卫阻挡，将继续尝试 reduce_only。"
            )

    positions = await _get_positions(var, selected)
    quotes = [
        await _close_position_quote(var, leg, positions[leg.underlying])
        for leg in selected.legs
    ]
    print(f"swap carry 平仓计划：结构={selected.name}（结构顺序）：")
    for index, (leg, quote) in enumerate(zip(selected.legs, quotes, strict=True), 1):
        action = (
            f"{quote.side.value.lower()} {quote.qty}" if quote is not None else "无持仓"
        )
        print(f"  [{index}/{len(quotes)}] {leg.underlying}：{action}，reduce_only")

    if dry_run:
        print("[DRY-RUN] 平仓报价已完成；未调用 accept，不会成交。")
        return
    if not yes:
        print("未提供 --yes：仅完成平仓报价，未调用 accept。确认后请加 --yes。")
        return

    for index, quote in enumerate(quotes, 1):
        if quote is None:
            continue
        print(
            f">>> [{index}/{len(quotes)}] reduce_only "
            f"{quote.side.value.lower()} {quote.leg.underlying} {quote.qty} …"
        )
        try:
            result = await _accept_quote(var, quote, reduce_only=True)
            print(
                f"   {quote.leg.underlying} 平仓 accept 成功："
                f"{_format_result(result)}"
            )
        except VariationalJurisdictionError as exc:
            raise SystemExit(
                f"❌ {quote.leg.underlying} 平仓被地区封锁：{exc}\n"
                "   需在放行 IP 上执行；已停止后续腿。"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                f"❌ {quote.leg.underlying} 平仓失败：{exc}，"
                "已停止后续腿，请立即人工检查。"
            ) from exc

    result = await _await_flat(var, selected)
    sizes = result[:-1]
    net = result[-1]
    flat = all(size == 0 for size in sizes)
    detail = " ".join(
        f"{leg.underlying}={size}"
        for leg, size in zip(selected.legs, sizes, strict=True)
    )
    print(
        f"\n平仓后：结构={selected.name} {detail} 净 delta={net} "
        f"{'✅ 均已归零' if flat else '🚨 仍有持仓，请立即人工检查'}"
    )


async def _main(args: argparse.Namespace) -> int:
    """构造客户端并分发命令，始终释放 HTTP 会话。"""
    from engine import swap_carry_cost as cost
    var = await _load()
    token = cost.ACTIVE_PATH.set(getattr(args, "cost_ledger", None) or cost.DEFAULT_PATH)
    try:
        if args.cmd == "status":
            await cmd_status(var, structure=args.structure)
        elif args.cmd == "open":
            await cmd_open(
                var,
                Decimal(str(args.notional)),
                structure=args.structure,
                yes=args.yes,
                dry_run=args.dry_run,
            )
        elif args.cmd == "close":
            await cmd_close(
                var,
                structure=args.structure,
                yes=args.yes,
                dry_run=args.dry_run,
            )
        return 0
    finally:
        cost.ACTIVE_PATH.reset(token)
        await var.close()


def _add_execution_flags(parser: argparse.ArgumentParser) -> None:
    """为可能成交的子命令添加统一人工确认参数。"""
    parser.add_argument("--cost-ledger", type=Path, help="逐笔成本台账路径")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认真正调用 accept；缺省只检查和报价",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="走完全部检查与报价，但绝不调用 accept",
    )


def _add_structure_flag(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = None,
) -> None:
    """为命令添加统一具名结构选择。"""
    parser.add_argument(
        "--structure",
        choices=tuple(STRUCTURES),
        default=default,
        help=(
            f"carry 结构，默认 {default}"
            if default
            else "carry 结构，缺省读取守护心跳；显式指定优先"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        description="Variational 可配置多腿 swap carry 对冲"
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    status_parser = subparsers.add_parser(
        "status", help="查看结构、仓位、强平、资金费与交易时段"
    )
    _add_structure_flag(status_parser)
    open_parser = subparsers.add_parser("open", help="按结构顺序开仓")
    _add_structure_flag(open_parser, default=DEFAULT_STRUCTURE.name)
    open_parser.add_argument(
        "--notional",
        type=Decimal,
        default=DEFAULT_NOTIONAL_USD,
        help=(
            f"第一腿目标名义美元，默认 {DEFAULT_NOTIONAL_USD}，"
            f"任一腿硬上限 {MAX_NOTIONAL_USD}"
        ),
    )
    _add_execution_flags(open_parser)
    close_parser = subparsers.add_parser("close", help="按结构顺序平仓")
    _add_structure_flag(close_parser)
    _add_execution_flags(close_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """解析参数，清理代理环境并执行命令。"""
    args = build_parser().parse_args(argv)
    removed = _load_environment_without_proxy()
    if removed:
        print(f"已清除代理环境变量：{'、'.join(removed)}")
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
