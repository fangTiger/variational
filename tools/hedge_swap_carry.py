"""Variational swap carry 双腿人工执行工具：open / status / close。

固定结构为先多 XAUS swap，再空 XAU RWA 永续。两腿使用相同黄金数量，
第二腿失败时立即用 ``reduce_only`` 回滚 XAUS；回滚失败则停止全部动作并要求
人工介入。本工具只供人工值守、小额实盘，不包含自动入场退出或持久化状态机。

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
from pathlib import Path  # noqa: E402
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
SWAP_CARRY_KILL_SWITCH = PROJECT_ROOT / "data" / "swap_carry.kill"
SWAP_CARRY_GUARD_HEARTBEAT = (
    PROJECT_ROOT / "data" / "swap_carry_guard_heartbeat.json"
)
SWAP_CARRY_GUARD_STATE = PROJECT_ROOT / "data" / "swap_carry_guard_state.json"
SWAP_CARRY_GUARD_STALE_AFTER = timedelta(minutes=15)
_ANSI_RED = "\033[31m"
_ANSI_RESET = "\033[0m"


@dataclass(frozen=True)
class CarryLeg:
    """Swap carry 单腿的固定 Variational 合约参数。"""

    underlying: str
    open_side: Side
    instrument_type: str
    funding_interval_s: int
    kind: str


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
)
XAU_LEG = CarryLeg(
    underlying="XAU",
    open_side=Side.SELL,
    instrument_type="perpetual_rwa_future",
    funding_interval_s=3600,
    kind="commodity",
)


def _opening_plan() -> tuple[CarryLeg, CarryLeg]:
    """返回不可调换的开仓顺序，并守住唯一允许方向。"""
    plan = (XAUS_LEG, XAU_LEG)
    if XAUS_LEG.open_side is not Side.BUY or XAU_LEG.open_side is not Side.SELL:
        raise RuntimeError("swap carry 方向配置错误：只允许多 XAUS + 空 XAU")
    return plan


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


async def _net_delta(var: Any) -> tuple[Decimal, Decimal, Decimal]:
    """返回 XAUS、XAU 有符号数量及两者净 delta。"""
    xaus = await _get_position(var, XAUS_LEG)
    xau = await _get_position(var, XAU_LEG)
    return xaus.signed_size, xau.signed_size, xaus.signed_size + xau.signed_size


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


def _initial_margin_ratio(payload: Mapping[str, Any]) -> Decimal:
    """严格读取报价声明的初始保证金率；缺失时禁止开仓。"""
    requirements = payload.get("margin_requirements")
    if not isinstance(requirements, Mapping):
        raise ValueError("报价缺少 margin_requirements")
    ratio = _decimal(
        requirements.get("initial_margin"),
        label="initial_margin",
        positive=True,
    )
    if ratio > 1:
        raise ValueError("initial_margin 必须是不超过 1 的比例")
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
    margin_ratio = _initial_margin_ratio(payload) if require_margin else Decimal(0)
    return PreparedQuote(
        leg=leg,
        side=side,
        qty=qty,
        payload=payload,
        execution_price=price,
        notional_usd=qty * price,
        initial_margin_ratio=margin_ratio,
    )


async def _prepare_open_quotes(
    var: Any,
    target_notional: Decimal,
) -> tuple[PreparedQuote, PreparedQuote, Decimal]:
    """询价并生成两腿同数量计划；全程不 accept。"""
    xaus_probe = await _request_quote(var, XAUS_LEG, Side.BUY, XAUS_MIN_QTY)
    xau_probe = await _request_quote(var, XAU_LEG, Side.SELL, XAU_PROBE_QTY)
    xaus_minimum, xaus_step = _quantity_constraints(
        xaus_probe,
        Side.BUY,
        fallback_minimum=XAUS_MIN_QTY,
        fallback_step=XAUS_QTY_STEP,
    )
    xau_minimum, xau_step = _quantity_constraints(
        xau_probe,
        Side.SELL,
        fallback_minimum=XAU_FALLBACK_QTY_STEP,
        fallback_step=XAU_FALLBACK_QTY_STEP,
    )
    common_step = max(xaus_step, xau_step)
    common_minimum = max(xaus_minimum, xau_minimum)
    reference_price = _quote_price(xaus_probe, Side.BUY)
    qty = _round_qty(target_notional / reference_price, common_step)
    if qty < common_minimum:
        raise SystemExit(
            f"❌ 目标名义过小：qty={qty}，双腿共同最小数量为 {common_minimum}"
        )

    xaus_payload = await _request_quote(var, XAUS_LEG, Side.BUY, qty)
    xau_payload = await _request_quote(var, XAU_LEG, Side.SELL, qty)
    xaus_quote = _prepare_quote(XAUS_LEG, Side.BUY, qty, xaus_payload)
    xau_quote = _prepare_quote(XAU_LEG, Side.SELL, qty, xau_payload)
    for quote in (xaus_quote, xau_quote):
        if quote.notional_usd > MAX_NOTIONAL_USD:
            raise SystemExit(
                f"❌ {quote.leg.underlying} 报价名义 ${quote.notional_usd:.2f} "
                f"超过硬上限 ${MAX_NOTIONAL_USD}"
            )
    return xaus_quote, xau_quote, common_step


async def _check_margin(
    var: Any,
    xaus_quote: PreparedQuote,
    xau_quote: PreparedQuote,
) -> None:
    """确认账户权益足以覆盖两腿报价的初始保证金。"""
    balance = await var.get_balance()
    equity = _decimal(getattr(balance, "equity", None), label="账户权益", positive=True)
    required = xaus_quote.required_margin_usd + xau_quote.required_margin_usd
    print(
        f"保证金检查：两腿所需≈${required:.4f}，"
        f"当前可用抵押依据（账户权益）=${equity:.4f}"
    )
    if equity < required:
        raise SystemExit(
            f"❌ 可用保证金不足：两腿需要约 ${required:.4f}，账户权益 ${equity:.4f}"
        )


def _print_open_plan(
    xaus_quote: PreparedQuote,
    xau_quote: PreparedQuote,
) -> None:
    """在任何 accept 前输出完整双腿动作。"""
    print("swap carry 开仓计划（固定顺序，不可调换）：")
    print(
        f"  [1/2] 买入 XAUS {xaus_quote.qty}，"
        f"名义≈${xaus_quote.notional_usd:.2f}，isolated"
    )
    print(
        f"  [2/2] 卖出 XAU  {xau_quote.qty}，"
        f"名义≈${xau_quote.notional_usd:.2f}，全仓"
    )
    print(
        f"  预计初始保证金合计≈$"
        f"{xaus_quote.required_margin_usd + xau_quote.required_margin_usd:.4f}"
    )


async def _accept_quote(var: Any, quote: PreparedQuote, *, reduce_only: bool) -> Any:
    """接受一笔已经打印过动作的 RFQ；缺 quote_id 时失败关闭。"""
    quote_id = quote.payload.get("quote_id")
    if not isinstance(quote_id, str) or not quote_id:
        raise ValueError(f"{quote.leg.underlying} 报价缺少 quote_id，拒绝 accept")
    return await var.accept_quote(
        quote_id=quote_id,
        side=quote.side.value.lower(),
        max_slippage=float(getattr(var, "_max_slippage", 0.01)),
        is_reduce_only=reduce_only,
    )


async def _confirm_first_leg_qty(
    var: Any,
    expected_qty: Decimal,
) -> Decimal:
    """轮询第一腿实仓数量；持续延迟时按 RFQ 的全量成交数量继续配平。"""
    for attempt in range(1, _CONFIRM_TRIES + 1):
        position = await _get_position(var, XAUS_LEG)
        if position.signed_size > 0:
            print(
                f"   第 {attempt} 次回读确认 XAUS 成交数量 Q={position.signed_size}"
            )
            return position.signed_size
        if position.signed_size < 0:
            raise RuntimeError(
                f"XAUS 回读方向异常：{position.signed_size}，停止自动动作并人工检查"
            )
        if attempt < _CONFIRM_TRIES:
            await asyncio.sleep(_POLL_DELAY_S)
    print(
        "⚠️ /positions 在轮询窗口内仍未反映 XAUS 成交；"
        f"RFQ accept 已返回成交编号，按已报全量 Q={expected_qty} 继续配平，"
        "禁止据即时零仓误判失败"
    )
    return expected_qty


async def _rollback_first_leg(var: Any, qty: Decimal) -> None:
    """对第一腿执行 reduce_only 回滚；任何失败都升级为人工事故。"""
    try:
        payload = await _request_quote(var, XAUS_LEG, Side.SELL, qty)
        quote = _prepare_quote(XAUS_LEG, Side.SELL, qty, payload)
        print(f">>> 回滚：reduce_only 卖出 XAUS {qty}，避免留下裸 isolated 腿")
        result = await _accept_quote(var, quote, reduce_only=True)
        print(f"   XAUS 已回滚：{_format_result(result)}")
    except Exception as exc:  # noqa: BLE001 回滚失败必须统一升级，不得继续任何动作
        message = (
            "🚨🚨🚨 最高级别告警：第二腿失败且 XAUS reduce_only 回滚失败！\n"
            f"   回滚错误：{exc}\n"
            "   已停止一切自动动作，请立即人工处理当前裸 XAUS isolated 仓位。"
        )
        print(message)
        raise SystemExit(message) from exc


async def _await_net_delta(
    var: Any,
    tolerance: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """轮询最终净 delta，容忍第二腿后的 /positions 最终一致延迟。"""
    result = await _net_delta(var)
    for attempt in range(_FLAT_TRIES):
        if abs(result[2]) <= tolerance and result[0] != 0 and result[1] != 0:
            return result
        if attempt + 1 < _FLAT_TRIES:
            await asyncio.sleep(_POLL_DELAY_S)
            result = await _net_delta(var)
    return result


async def cmd_open(
    var: Any,
    notional: Decimal = DEFAULT_NOTIONAL_USD,
    *,
    yes: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> None:
    """检查全部前置条件，并按固定顺序开出两腿。"""
    if SWAP_CARRY_KILL_SWITCH.exists():
        raise SystemExit(
            f"❌ kill switch 已激活（{SWAP_CARRY_KILL_SWITCH}），拒绝 open"
        )
    target_notional = _validate_notional(notional)
    _metadata, _record, schedule = await _load_schedule(var, now=now)
    _guard_open_schedule(schedule)

    xaus_size, xau_size, _net = await _net_delta(var)
    if xaus_size != 0 or xau_size != 0:
        raise SystemExit(
            f"❌ 已有目标持仓（XAUS={xaus_size} XAU={xau_size}），请先处理后再 open"
        )

    xaus_quote, xau_quote, tolerance = await _prepare_open_quotes(
        var, target_notional
    )
    await _check_margin(var, xaus_quote, xau_quote)
    _print_open_plan(xaus_quote, xau_quote)
    print(
        f"XAUS 距下次休市 {schedule.time_until_close}，"
        f"休市时刻={schedule.next_close_at.isoformat() if schedule.next_close_at else '无数据'}"
    )

    if dry_run:
        print("[DRY-RUN] 全部检查与双腿报价已完成；未调用 accept，不会成交。")
        return
    if not yes:
        print("未提供 --yes：仅完成检查与报价，未调用 accept。确认后请加 --yes。")
        return

    print(f">>> [1/2] 买入受限腿 XAUS {xaus_quote.qty} …")
    try:
        first_result = await _accept_quote(var, xaus_quote, reduce_only=False)
    except VariationalJurisdictionError as exc:
        raise SystemExit(
            f"❌ XAUS accept 被地区封锁：{exc}\n   需在放行 IP 上执行。"
        ) from exc
    except Exception as exc:  # noqa: BLE001 第一腿未确认成交时不得继续
        raise SystemExit(f"❌ XAUS 第一腿下单失败，已停止：{exc}") from exc
    print(f"   XAUS accept 成功：{_format_result(first_result)}")

    filled_qty = await _confirm_first_leg_qty(var, xaus_quote.qty)
    if filled_qty != xau_quote.qty:
        # RFQ 按设计全量成交；若权威仓位返回不同 Q，则必须按 Q 重新询价第二腿。
        second_payload = await _request_quote(var, XAU_LEG, Side.SELL, filled_qty)
        xau_quote = _prepare_quote(XAU_LEG, Side.SELL, filled_qty, second_payload)
        if xau_quote.notional_usd > MAX_NOTIONAL_USD:
            print(
                f"❌ 回读 Q 对应 XAU 名义 ${xau_quote.notional_usd:.2f} 超过硬上限"
            )
            await _rollback_first_leg(var, filled_qty)
            raise SystemExit("第一腿已回滚，未继续第二腿。")

    print(f">>> [2/2] 按成交 Q={filled_qty} 卖出 XAU …")
    try:
        second_result = await _accept_quote(var, xau_quote, reduce_only=False)
        print(f"   XAU accept 成功：{_format_result(second_result)}")
    except VariationalJurisdictionError as exc:
        print(f"❌ XAU 第二腿被地区封锁：{exc}；需在放行 IP 上执行。")
        await _rollback_first_leg(var, filled_qty)
        raise SystemExit(
            "第二腿被地区封锁，本次 XAUS 已回滚；后续需在放行 IP 上执行。"
        ) from exc
    except Exception as exc:  # noqa: BLE001 明确失败统一走第一腿回滚
        print(f"❌ XAU 第二腿下单失败：{exc}")
        await _rollback_first_leg(var, filled_qty)
        raise SystemExit("第二腿失败，本次 XAUS 已回滚，未留下目标裸仓。") from exc

    xaus_size, xau_size, net = await _await_net_delta(var, tolerance)
    neutral = abs(net) <= tolerance
    print(
        f"\n开仓完成：XAUS={xaus_size} XAU={xau_size} 净 delta={net} "
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
) -> dict[str, Decimal]:
    """分页读取 /transfers，并按真实已结算 qty 汇总两腿资金费。

    ``since`` 用于面板的本周口径；人工 status 不传时继续展示全部历史。
    """
    if since is not None:
        if since.tzinfo is None:
            raise ValueError("资金费起始时间必须包含时区")
        since = since.astimezone(timezone.utc)
    totals = {XAUS_LEG.underlying: Decimal("0"), XAU_LEG.underlying: Decimal("0")}
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


async def cmd_status(var: Any, *, now: datetime | None = None) -> None:
    """输出仓位、强平、carry、时段及实际已结算资金费快照。"""
    observed_at = now or datetime.now(timezone.utc)
    _print_guard_status(observed_at)
    xaus_position = await _get_position(var, XAUS_LEG)
    xau_position = await _get_position(var, XAU_LEG)
    net = xaus_position.signed_size + xau_position.signed_size
    print("swap carry 状态（目标：多 XAUS + 空 XAU）")
    if xaus_position.is_flat != xau_position.is_flat:
        remaining = "XAU" if xaus_position.is_flat else "XAUS"
        print(
            f"🚨🚨🚨 单腿裸仓告警：只剩 {remaining} 腿！"
            "请停止开仓并立即人工处理。"
        )

    metadata: object | None = None
    schedule: SwapTradingSchedule | None = None
    try:
        metadata, _record, schedule = await _load_schedule(var, now=now)
    except Exception as exc:  # noqa: BLE001 status 必须保留其他只读信息
        print(f"⚠️ XAUS 时段元数据读取失败，按不可交易处理：{exc}")

    xaus_price = _metadata_price(metadata, XAUS_LEG) if metadata is not None else None
    xau_price = _metadata_price(metadata, XAU_LEG) if metadata is not None else None
    xaus_notional = (
        abs(xaus_position.signed_size) * xaus_price if xaus_price is not None else None
    )
    xau_notional = (
        abs(xau_position.signed_size) * xau_price if xau_price is not None else None
    )
    print(
        f"XAUS 数量={xaus_position.signed_size}，名义="
        f"{'$' + format(xaus_notional, '.2f') if xaus_notional is not None else '无数据'}"
    )
    print(
        f"XAU  数量={xau_position.signed_size}，名义="
        f"{'$' + format(xau_notional, '.2f') if xau_notional is not None else '无数据'}"
    )
    print(f"净 delta={net} {'✅ 近似中性' if abs(net) <= XAUS_QTY_STEP else '⚠️ 有敞口'}")

    for leg, position in ((XAUS_LEG, xaus_position), (XAU_LEG, xau_position)):
        try:
            info = await var.get_liquidation_info(leg.underlying, exact=True)
            text = _format_liquidation(info, position)
        except Exception as exc:  # noqa: BLE001 isolated 缺值必须如实显示
            text = f"无数据（{exc}）"
        print(f"{leg.underlying} 强平：{text}")

    xaus_rate: Decimal | None = None
    xau_rate: Decimal | None = None
    try:
        xaus_rate = _swap_long_rate(await var.get_swap_funding(XAUS_LEG.underlying))
        print(f"XAUS 多头当前资金费率={xaus_rate:.4%} 年化")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ XAUS 资金费率读取失败：{exc}")
    try:
        xau_rate = _decimal(
            await var.get_funding_rate(
                XAU_LEG.underlying, XAU_LEG.instrument_type
            ),
            label="XAU 永续资金费率",
        )
        print(f"XAU 空头当前资金费率收益={xau_rate:.4%} 年化")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ XAU 资金费率读取失败：{exc}")
    if xaus_rate is not None and xau_rate is not None:
        print(f"净 carry={xaus_rate + xau_rate:.4%} 年化（未扣摩擦）")
    else:
        print("净 carry=无数据")

    if schedule is not None:
        print(
            f"XAUS 时段={'可交易' if schedule.is_tradable else '不可交易'}；"
            f"距下次休市={_format_duration(schedule.time_until_close)}；"
            f"原因={schedule.reason}"
        )
    else:
        print("XAUS 时段=不可交易；距下次休市=无数据")

    try:
        settled = await _settled_funding_by_leg(var)
        total = settled[XAUS_LEG.underlying] + settled[XAU_LEG.underlying]
        print(
            "累计已结算资金费（/transfers 实际扣款）："
            f"XAUS={settled[XAUS_LEG.underlying]} USDC，"
            f"XAU={settled[XAU_LEG.underlying]} USDC，"
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


async def _await_flat(var: Any) -> tuple[Decimal, Decimal, Decimal]:
    """轮询两腿归零，容忍平仓后的 /positions 最终一致延迟。"""
    result = await _net_delta(var)
    for attempt in range(_FLAT_TRIES):
        if result[0] == 0 and result[1] == 0:
            return result
        if attempt + 1 < _FLAT_TRIES:
            await asyncio.sleep(_POLL_DELAY_S)
            result = await _net_delta(var)
    return result


async def cmd_close(
    var: Any,
    *,
    yes: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> None:
    """优先平受限 XAUS，再平 XAU；时段元数据异常不阻挡减仓尝试。"""
    schedule: SwapTradingSchedule | None = None
    market_status: str | None = None
    try:
        _metadata, record, schedule = await _load_schedule(var, now=now)
        market_status = str(record.get("market_status") or "").strip().lower()
    except Exception as exc:  # noqa: BLE001 平仓不能被元数据缺失阻挡
        print(f"⚠️ XAUS 时段状态无法确认，但平仓优先，将继续尝试：{exc}")

    if market_status and market_status != "open":
        message = (
            "⚠️ XAUS 腿此刻平不掉（交易所 market_status 非 open），"
            "XAU 腿 24/7 可平。为避免自动制造另一条裸腿，本工具未下任何单，"
            "请人工决定是否单独处理 XAU。"
        )
        print(message)
        raise SystemExit(message)
    if schedule is not None and not schedule.is_tradable:
        print(
            f"⚠️ XAUS 时段守卫报告不可交易（{schedule.reason}），"
            "但 close 不受开仓守卫阻挡，将继续尝试 reduce_only。"
        )

    xaus_position = await _get_position(var, XAUS_LEG)
    xau_position = await _get_position(var, XAU_LEG)
    xaus_quote = await _close_position_quote(var, XAUS_LEG, xaus_position)
    xau_quote = await _close_position_quote(var, XAU_LEG, xau_position)
    print("swap carry 平仓计划（受限腿优先）：")
    print(
        f"  [1/2] XAUS："
        f"{xaus_quote.side.value.lower() + ' ' + str(xaus_quote.qty) if xaus_quote else '无持仓'}"
        "，reduce_only"
    )
    print(
        f"  [2/2] XAU ："
        f"{xau_quote.side.value.lower() + ' ' + str(xau_quote.qty) if xau_quote else '无持仓'}"
        "，reduce_only"
    )

    if dry_run:
        print("[DRY-RUN] 平仓报价已完成；未调用 accept，不会成交。")
        return
    if not yes:
        print("未提供 --yes：仅完成平仓报价，未调用 accept。确认后请加 --yes。")
        return

    if xaus_quote is not None:
        print(
            f">>> [1/2] reduce_only {xaus_quote.side.value.lower()} "
            f"XAUS {xaus_quote.qty} …"
        )
        try:
            result = await _accept_quote(var, xaus_quote, reduce_only=True)
            print(f"   XAUS 平仓 accept 成功：{_format_result(result)}")
        except VariationalJurisdictionError as exc:
            raise SystemExit(
                f"❌ XAUS 平仓被地区封锁：{exc}\n   需在放行 IP 上执行；已停止平 XAU。"
            ) from exc
        except Exception as exc:  # noqa: BLE001 XAUS 未平时不能自动拆 XAU
            raise SystemExit(f"❌ XAUS 平仓失败：{exc}；已停止平 XAU。") from exc

    if xau_quote is not None:
        print(
            f">>> [2/2] reduce_only {xau_quote.side.value.lower()} "
            f"XAU {xau_quote.qty} …"
        )
        try:
            result = await _accept_quote(var, xau_quote, reduce_only=True)
            print(f"   XAU 平仓 accept 成功：{_format_result(result)}")
        except VariationalJurisdictionError as exc:
            raise SystemExit(
                f"❌ XAU 平仓被地区封锁：{exc}\n   需在放行 IP 上执行。"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"❌ XAU 平仓失败：{exc}，请立即人工检查。") from exc

    xaus_size, xau_size, net = await _await_flat(var)
    flat = xaus_size == 0 and xau_size == 0
    print(
        f"\n平仓后：XAUS={xaus_size} XAU={xau_size} 净 delta={net} "
        f"{'✅ 均已归零' if flat else '🚨 仍有持仓，请立即人工检查'}"
    )


async def _main(args: argparse.Namespace) -> int:
    """构造客户端并分发命令，始终释放 HTTP 会话。"""
    var = await _load()
    try:
        if args.cmd == "status":
            await cmd_status(var)
        elif args.cmd == "open":
            await cmd_open(
                var,
                Decimal(str(args.notional)),
                yes=args.yes,
                dry_run=args.dry_run,
            )
        elif args.cmd == "close":
            await cmd_close(var, yes=args.yes, dry_run=args.dry_run)
        return 0
    finally:
        await var.close()


def _add_execution_flags(parser: argparse.ArgumentParser) -> None:
    """为可能成交的子命令添加统一人工确认参数。"""
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


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        description="Variational XAUS swap / XAU 永续人工 carry 对冲"
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    subparsers.add_parser("status", help="查看仓位、强平、资金费与交易时段")
    open_parser = subparsers.add_parser("open", help="先多 XAUS，再空 XAU")
    open_parser.add_argument(
        "--notional",
        type=Decimal,
        default=DEFAULT_NOTIONAL_USD,
        help=f"每腿目标名义美元，默认 {DEFAULT_NOTIONAL_USD}，硬上限 {MAX_NOTIONAL_USD}",
    )
    _add_execution_flags(open_parser)
    close_parser = subparsers.add_parser("close", help="先平 XAUS，再平 XAU")
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
