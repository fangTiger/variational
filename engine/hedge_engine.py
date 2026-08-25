"""跨所对冲主循环。

策略：primary 腿（Variational）持有仓位赚 OI 积分，hedge 腿（Extended）持有
等额反向仓位对冲方向风险。引擎监控两腿净 delta，超阈值就把 hedge 腿调整到
-primary_size，使组合净敞口回到 ~0。

安全默认：dry_run=True，只计算与记录意图，不真正下单。确认字段与账户后再关闭。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal

from adapters.base import ExchangeAdapter, Position, Side
from engine.risk import RiskAction, RiskManager
from infra.logger import get_logger

logger = get_logger("hedge_engine")

# 两秒间隔用于限制盘口来回跳动时的撤挂频率，避免触发交易所限频或刷单。
MAKER_REPEG_MIN_INTERVAL_S = 2.0
# 单轮最多重挂二十次，防止长超时配置在单边行情中产生无界委托请求。
MAKER_REPEG_MAX_ATTEMPTS = 20


@dataclass
class HedgeConfig:
    """对冲引擎配置（默认值 = 策略阶段 A）。

    详见 docs/superpowers/specs/2026-07-16-交易策略与分阶段放大.md
    """

    market: str = "BTC-USD"                     # hedge 腿标的名（Extended 口径）
    primary_market: str = "BTC"                 # primary 腿 underlying（Variational 口径，非 BTC-PERP）
    poll_interval: float = 15.0                 # 轮询间隔（秒）

    # ---- 阶段 A 资金/杠杆 ----
    capital_per_leg_usd: Decimal = Decimal("300")   # 每腿投入资金
    leverage: Decimal = Decimal("3")                # 杠杆（实用区间 3-5x，默认甜点 3x）
    # 方向：优先在"资金费为正"的一侧做空以收取资金费（覆盖磨损/求正 carry）
    prefer_earn_funding: bool = True

    # 再平衡阈值：净 delta 占单腿名义比例，超过才动手（减少交易磨损）
    rebalance_threshold_ratio: Decimal = Decimal("0.02")
    dry_run: bool = True                        # 只算不下单

    # primary 由人工操作时可限制机器人自动跟随的最大名义金额；None 表示关闭。
    max_primary_notional: Decimal | None = None

    # primary 抛出这些异常时交给 run_forever 的既有自愈回调处理。
    # 默认空元组，避免引擎依赖任何具体交易所的异常类。
    auth_error_types: tuple[type[Exception], ...] = field(default_factory=tuple)

    # ---- 保护（高仓位策略：放宽按比例降险，改为逼近清仓价即两腿一起平）----
    # 主保护：任一腿标记价距其清仓价 < 此比例 → 两腿一起平（delta 中性，净损失仅磨损）
    flatten_proximity: Decimal = Decimal("0.08")
    # 次级：可用保证金率降险，默认 0 = 关闭（放宽）；设 >0 才启用按比例减仓
    min_free_margin_ratio: Decimal = Decimal("0")
    derisk_fraction: Decimal = Decimal("0.35")

    # 对冲下单先挂 maker 等待成交的秒数；0 表示直接吃单（保持原行为）。
    # 放在字段末尾，避免改变既有配置项的位置参数序号。
    maker_first_timeout_s: float = 0.0

    @property
    def target_notional_usd(self) -> Decimal:
        """每腿目标名义仓位 = 资金 × 杠杆。"""
        return self.capital_per_leg_usd * self.leverage

    def target_size(self, price: Decimal) -> Decimal:
        """按当前价把目标名义仓位换算成合约数量。"""
        if price <= 0:
            raise ValueError("price 必须为正")
        return self.target_notional_usd / price


@dataclass
class HedgeState:
    """一轮循环的状态快照。"""

    primary: Position | None = None
    hedge: Position | None = None
    net_delta: Decimal = Decimal(0)
    action_taken: str = ""
    primary_notional_exceeded: bool = False


class HedgeEngine:
    """对冲引擎：primary 持仓 + hedge 对冲。"""

    def __init__(
        self,
        primary: ExchangeAdapter,
        hedge: ExchangeAdapter,
        config: HedgeConfig | None = None,
        risk: RiskManager | None = None,
        on_snapshot=None,
        on_auth_error=None,
    ) -> None:
        self.primary = primary
        self.hedge = hedge
        self.config = config or HedgeConfig()
        self.risk = risk or RiskManager()
        # 每轮结束回调（记录权益快照等）；参数为 HedgeState
        self._on_snapshot = on_snapshot
        # primary 会话失效回调，应返回新的 primary 适配器（Cookie 自愈）
        self._on_auth_error = on_auth_error
        self._running = False

    def _is_auth_error(self, exc: Exception) -> bool:
        """仅按显式配置的异常类型判断是否进入 primary 会话自愈。"""
        return isinstance(exc, self.config.auth_error_types)

    async def connect(self) -> None:
        """连接两腿。"""
        await asyncio.gather(self.primary.connect(), self.hedge.connect())
        logger.info("两腿已连接：primary=%s hedge=%s", self.primary.name, self.hedge.name)

    async def run_once(self) -> HedgeState:
        """执行一轮：读持仓 → 风控 → 必要时再平衡。"""
        state = HedgeState()

        # 1. 读两腿持仓。
        # 关键安全原则：只有两腿都能读到才动仓。任一腿读取失败就跳过本轮——
        # 因为此时无法可靠操作那条腿，贸然平另一腿会造成裸仓（比不动更危险）。
        # primary 若是配置的会话失效异常，抛出交给 run_forever 做自愈。
        read_error = None
        try:
            state.primary = await self.primary.get_position(self.config.primary_market)
        except Exception as exc:  # noqa: BLE001
            if self._is_auth_error(exc):
                raise  # 交给 run_forever：重读 .env 新 Cookie 后下轮继续
            logger.error("读取 primary 持仓失败：%s", exc)
            read_error = "primary"
        try:
            state.hedge = await self.hedge.get_position(self.config.market)
        except Exception as exc:  # noqa: BLE001
            logger.error("读取 hedge 持仓失败：%s", exc)
            read_error = "hedge"

        if read_error:
            state.action_taken = f"跳过本轮（{read_error} 读取失败，不动仓以避免裸仓）"
            logger.warning(state.action_taken)
            return state

        p_size = state.primary.signed_size
        h_size = state.hedge.signed_size
        state.net_delta = p_size + h_size
        logger.info("primary=%s hedge=%s net_delta=%s", p_size, h_size, state.net_delta)

        # 2. 主保护：任一腿逼近清仓价 → 两腿一起平（delta 中性，一起平净损失仅磨损）
        if p_size != 0 or h_size != 0:
            flat = await self._check_liquidation_and_flatten()
            if flat:
                state.action_taken = flat
                return state

        # 3. 保证金自动降险（瓶颈腿可用保证金过低 → 按比例减小两腿）
        if p_size != 0 or h_size != 0:
            derisked = await self._check_margin_and_derisk(p_size, h_size)
            if derisked:
                state.action_taken = derisked
                return state

        # 4. primary 名义上限：仅拒绝跟随变大的 primary 仓位，不阻止降险动作。
        max_notional = self.config.max_primary_notional
        if max_notional is not None and p_size != 0:
            try:
                mark_price = (
                    await self.primary.get_market_price(self.config.primary_market)
                ).mid
            except Exception as exc:  # noqa: BLE001
                state.action_taken = (
                    f"跳过本轮（primary 标记价读取失败：{exc}，不动仓以避免裸仓）"
                )
                logger.error(state.action_taken)
                return state
            primary_notional = abs(p_size) * mark_price
            if primary_notional > max_notional:
                state.primary_notional_exceeded = True
                state.action_taken = (
                    f"⚠️ primary 名义金额 {primary_notional} 超过上限 {max_notional}，"
                    "拒绝再平衡并保持已有对冲仓不动"
                )
                logger.warning(state.action_taken)
                return state

        # 5. 再平衡：把 hedge 腿调整到 -primary_size
        target_hedge = -p_size
        base = max(abs(p_size), abs(h_size))
        threshold = self.config.rebalance_threshold_ratio * base if base > 0 else Decimal(0)
        if abs(target_hedge - h_size) > threshold:
            state.action_taken = await self._rebalance(target_hedge)
        else:
            state.action_taken = "无需再平衡"
        return state

    async def _emit_snapshot(self, state: HedgeState) -> None:
        """触发快照回调（记录权益等），失败不影响主流程。"""
        if self._on_snapshot is None:
            return
        try:
            res = self._on_snapshot(state)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:  # noqa: BLE001
            logger.warning("快照回调异常：%s", exc)

    async def _check_liquidation_and_flatten(self) -> str | None:
        """任一腿标记价距其清仓价 < flatten_proximity 时，两腿一起平仓。"""
        legs = [
            (self.primary, self.config.primary_market),
            (self.hedge, self.config.market),
        ]
        for adapter, market in legs:
            try:
                info = await adapter.get_liquidation_info(market)
            except Exception as exc:  # noqa: BLE001 清仓信息查询失败不阻断
                logger.warning("查询 %s 清仓信息失败：%s", adapter.name, exc)
                continue
            if not info:
                continue
            mark, liq = info
            if mark <= 0:
                continue
            proximity = abs(mark - liq) / mark
            if proximity < self.config.flatten_proximity:
                msg = (f"⚠️ {adapter.name} 逼近清仓价（标记 {mark:.0f} / 清仓 {liq:.0f}，"
                       f"距 {proximity:.1%} < {self.config.flatten_proximity:.0%}）→ 两腿一起平仓")
                if not self.primary.supports_trading:
                    warning = (
                        f"{msg}；primary 只读，需人工处理，两腿保持不动"
                    )
                    logger.warning(warning)
                    return warning
                if self.config.dry_run:
                    logger.warning("[dry_run] %s", msg)
                    return f"[dry_run] {msg}"
                logger.warning(msg)
                await self._emergency_flatten()
                return msg
        return None

    async def _check_margin_and_derisk(self, p_size: Decimal, h_size: Decimal) -> str | None:
        """瓶颈腿(hedge/Extended)可用保证金率过低时，按比例减小两腿。返回动作说明或 None。"""
        try:
            free_ratio = await self.hedge.get_free_margin_ratio()
        except Exception as exc:  # noqa: BLE001 保证金查询失败不阻断主流程
            logger.warning("查询可用保证金失败：%s", exc)
            return None
        if free_ratio is None or free_ratio >= self.config.min_free_margin_ratio:
            return None

        frac = self.config.derisk_fraction
        msg = f"⚠️ 可用保证金率 {free_ratio:.2%} < {self.config.min_free_margin_ratio:.0%}，降险减仓 {frac:.0%}"
        if not self.primary.supports_trading:
            warning = f"{msg}；primary 只读，需人工处理，两腿保持不动"
            logger.warning(warning)
            return warning
        if self.config.dry_run:
            logger.warning("[dry_run] %s", msg)
            return f"[dry_run] {msg}"
        logger.warning(msg)
        await self._reduce_both(p_size, h_size, frac)
        return msg

    async def _reduce_both(self, p_size: Decimal, h_size: Decimal, fraction: Decimal) -> None:
        """把两腿各减掉 fraction 比例（reduce_only，方向为各自反向）。"""
        tasks = []
        if p_size != 0:
            side = Side.BUY if p_size < 0 else Side.SELL
            tasks.append(
                self.primary.market_order(
                    self.config.primary_market, side, abs(p_size) * fraction, reduce_only=True
                )
            )
        if h_size != 0:
            side = Side.BUY if h_size < 0 else Side.SELL
            tasks.append(
                self.hedge.market_order(
                    self.config.market, side, abs(h_size) * fraction, reduce_only=True
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.warning("降险减仓结果：%s", results)

    async def _rebalance(self, target_hedge: Decimal) -> str:
        """把 hedge 腿调到目标有符号数量。"""
        msg = f"再平衡 hedge → {target_hedge}"
        if self.config.dry_run:
            logger.info("[dry_run] %s（未真正下单）", msg)
            return f"[dry_run] {msg}"

        if self.config.maker_first_timeout_s > 0:
            current = (
                await self.hedge.get_position(self.config.market)
            ).signed_size
            delta = target_hedge - current
            reduce_only = (
                abs(target_hedge) < abs(current)
                and target_hedge * current >= 0
            )
            result = await maker_first_hedge(
                self.hedge,
                self.config.market,
                delta,
                timeout_s=self.config.maker_first_timeout_s,
                reduce_only=reduce_only,
            )
            logger.info("%s 完成（maker 优先）：%s", msg, result.note)
            return f"{msg}（{result.note}）"

        result = await self.hedge.hedge(self.config.market, target_hedge)
        logger.info("%s 完成：%s", msg, result)
        return msg

    async def _emergency_flatten(self) -> str:
        """紧急平掉两腿，回到无敞口。"""
        msg = "紧急平仓两腿"
        if self.config.dry_run:
            logger.warning("[dry_run] %s（未真正下单）", msg)
            return f"[dry_run] {msg}"
        logger.warning("执行 %s", msg)
        results = await asyncio.gather(
            self.hedge.close_position(self.config.market),
            self.primary.close_position(self.config.primary_market),
            return_exceptions=True,
        )
        logger.warning("平仓结果：%s", results)
        return msg

    async def run_forever(self) -> None:
        """持续运行主循环，直到 stop()。"""
        self._running = True
        await self.connect()
        logger.info("对冲引擎启动（dry_run=%s）", self.config.dry_run)
        while self._running:
            try:
                state = await self.run_once()
                await self._emit_snapshot(state)
            except Exception as exc:  # noqa: BLE001 循环不因单轮异常退出
                # 配置的 primary 会话失效异常 → 触发既有 Cookie 自愈。
                if self._is_auth_error(exc):
                    logger.warning("primary 会话失效：%s", exc)
                    await self._try_reload_primary()
                else:
                    logger.exception("本轮循环异常：%s", exc)
            await asyncio.sleep(self.config.poll_interval)

    async def _try_reload_primary(self) -> None:
        """调用自愈回调重建 primary 适配器（从 .env 重读新 Cookie）。"""
        if self._on_auth_error is None:
            logger.error("未配置 Cookie 自愈回调，无法恢复会话")
            return
        try:
            res = self._on_auth_error()
            new_primary = await res if asyncio.iscoroutine(res) else res
            if new_primary is not None:
                self.primary = new_primary
                logger.info("已用新 Cookie 重建 primary 会话")
        except Exception as exc:  # noqa: BLE001
            logger.error("Cookie 自愈失败：%s", exc)

    def stop(self) -> None:
        """请求停止主循环。"""
        self._running = False


@dataclass(frozen=True)
class HedgeFillResult:
    """一次对冲下单的结果。

    filled 是最终覆盖掉的数量（maker 成交 + 吃单补足）。
    note 记录异常路径，供排查。
    """

    filled: Decimal
    used_taker: bool
    note: str = ""


def _is_post_only_immediate_match_error(exc: Exception) -> bool:
    """识别不同交易所对 post-only 立即成交竞态的拒绝措辞。"""
    normalized = " ".join(
        str(exc).casefold().replace("-", " ").replace("_", " ").split()
    )
    post_only_markers = (
        "post only",
        "postonly",
        "maker only",
        "maker order",
        "as a taker",
    )
    immediate_match_markers = (
        "immediately match",
        "immediate match",
        "cross the book",
        "take liquidity",
        "immediately execute",
        "immediate execute",
    )
    has_post_only_marker = any(
        marker in normalized for marker in post_only_markers
    )
    has_immediate_match_marker = any(
        marker in normalized for marker in immediate_match_markers
    )
    return has_post_only_marker and has_immediate_match_marker


async def maker_first_hedge(
    adapter,
    market: str,
    target_delta: Decimal,
    *,
    timeout_s: float = 15.0,
    poll_s: float = 1.0,
    reduce_only: bool = False,
    repeg_enabled: bool = True,
    max_repegs: int = MAKER_REPEG_MAX_ATTEMPTS,
) -> HedgeFillResult:
    """先挂 maker 并跟随本方最优价，超时后按实际剩余量吃单。

    每张子订单都以撤单终态的累计成交量为准，只把剩余数量重挂；无法可靠
    单查时才降级使用整个执行周期的实际仓位差，绝不按委托量推断成交量。
    """
    if target_delta == 0:
        return HedgeFillResult(
            filled=Decimal(0),
            used_taker=False,
            note="目标为零",
        )

    side = Side.BUY if target_delta > 0 else Side.SELL
    amount = abs(target_delta)

    if getattr(adapter, "execution_model", "orderbook") == "rfq":
        await adapter.market_order(
            market,
            side,
            amount,
            reduce_only=reduce_only,
        )
        return HedgeFillResult(
            filled=amount,
            used_taker=True,
            note="RFQ 执行模型，已直接市价成交",
        )

    # 挂在本方最优价：卖挂 ask、买挂 bid。挂在对手价会立即成交，
    # 从而被 post_only 拒绝。
    quote = await adapter.get_market_price(market)
    price = quote.ask if side is Side.SELL else quote.bid

    # 订单查询能力缺失或运行时失败时，只能用挂单前后仓位差还原实际成交量。
    # 这次前置读取不能省：maker 一旦挂出，再读到的仓位已经无法区分初始值与
    # 成交增量，按原下单量补市价单会造成双倍对冲。
    try:
        position_before = Decimal(
            str((await adapter.get_position(market)).signed_size)
        )
    except Exception as exc:  # noqa: BLE001 无基线就不能安全尝试 maker
        logger.warning(
            "maker 前仓位读取失败，跳过 maker 并直接吃单：%s",
            exc,
        )
        await adapter.market_order(
            market,
            side,
            amount,
            reduce_only=reduce_only,
        )
        return HedgeFillResult(
            filled=amount,
            used_taker=True,
            note="maker 前仓位不可读，已直接吃单",
        )

    try:
        response = await adapter.place_limit_order(
            market,
            side,
            amount,
            price,
            post_only=True,
            reduce_only=reduce_only,
        )
    except Exception as exc:  # noqa: BLE001 交易所业务拒绝的异常类型不统一
        if not _is_post_only_immediate_match_error(exc):
            raise
        logger.warning(
            "maker post-only 被拒，将按实际仓位差直接吃单补齐：%s",
            exc,
        )
        position_after = Decimal(
            str((await adapter.get_position(market)).signed_size)
        )
        actual_delta = position_after - position_before
        remaining_delta = target_delta - actual_delta
        same_direction = remaining_delta * target_delta > 0
        remaining = abs(remaining_delta) if same_direction else Decimal(0)
        covered = min(amount, abs(actual_delta))
        if remaining <= 0:
            return HedgeFillResult(
                filled=covered,
                used_taker=False,
                note="post-only 被拒；实际仓位差显示无需补单",
            )
        await adapter.market_order(
            market,
            side,
            remaining,
            reduce_only=reduce_only,
        )
        return HedgeFillResult(
            filled=amount,
            used_taker=True,
            note=f"post-only 被拒；按实际仓位差吃单补 {remaining}",
        )
    initial_place_at = time.monotonic()

    def _field(value, name: str):
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)

    def _order_id(response_value):
        response_data = _field(response_value, "data") or response_value
        tracked_id = _field(response_data, "id") or _field(response_value, "id")
        if tracked_id is None:
            raise RuntimeError("maker 下单响应缺少订单 ID，无法安全追踪")
        return tracked_id

    order_id = _order_id(response)

    order_getter = getattr(adapter, "get_order_by_id", None)
    if isinstance(adapter, ExchangeAdapter):
        implementation = getattr(type(adapter), "get_order_by_id", None)
        if implementation is ExchangeAdapter.get_order_by_id:
            order_getter = None
    order_lookup_available = callable(order_getter)

    async def _read_order(tracked_order_id) -> tuple[Decimal, str]:
        if not order_lookup_available:
            raise NotImplementedError("适配器未实现按 ID 查询订单")
        got = await order_getter(market, tracked_order_id)
        if got is None:
            raise LookupError(f"订单 {tracked_order_id} 暂未出现在查询结果中")
        data = _field(got, "data") or got
        raw_filled = _field(data, "filled_qty")
        raw_status = _field(data, "status")
        filled_qty = Decimal(str(raw_filled or 0))
        status = str(raw_status or "").upper().rsplit(".", 1)[-1]
        if status.startswith("CANCELED"):
            status = "CANCELLED" + status[len("CANCELED") :]
        return filled_qty, status

    def _is_cancelled(status: str) -> bool:
        return status.startswith("CANCELLED")

    def _is_terminal(status: str) -> bool:
        return (
            status == "FILLED"
            or _is_cancelled(status)
            or status in {"EXPIRED", "REJECTED"}
        )

    tick_size: Decimal | None = None
    repeg_active = repeg_enabled and max_repegs > 0 and order_lookup_available
    if repeg_active:
        tick_getter = getattr(adapter, "get_price_tick_size", None)
        if callable(tick_getter):
            try:
                raw_tick = await tick_getter(market)
                if raw_tick is not None:
                    parsed_tick = Decimal(str(raw_tick))
                    if parsed_tick > 0:
                        tick_size = parsed_tick
            except Exception as exc:  # noqa: BLE001 行情仍能严格判断价格是否移动
                logger.warning(
                    "maker 跟价读取价格 tick 失败，将按最优价严格变化判断：%s",
                    exc,
                )

    def _best_price(book) -> Decimal:
        return Decimal(str(book.ask if side is Side.SELL else book.bid))

    def _has_moved_behind(order_price: Decimal, best_price: Decimal) -> bool:
        distance = (
            order_price - best_price
            if side is Side.SELL
            else best_price - order_price
        )
        if tick_size is None:
            return distance > 0
        return distance >= tick_size

    deadline = initial_place_at + timeout_s
    current_order_id = order_id
    current_price = price
    current_filled = Decimal(0)
    maker_filled = Decimal(0)
    repeg_count = 0
    last_place_at = initial_place_at
    fallback_reason = ""
    note_parts: list[str] = []
    if order_lookup_available:
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            try:
                current_filled, status = await _read_order(current_order_id)
            except Exception as exc:  # noqa: BLE001 单查失效必须转仓位差降级
                fallback_reason = f"订单状态读取失败（{exc}）"
                logger.warning("%s，将撤单后按实际仓位差补单", fallback_reason)
                break

            observed_best: Decimal | None = None
            if repeg_active:
                try:
                    observed_best = _best_price(
                        await adapter.get_market_price(market)
                    )
                except Exception as exc:  # noqa: BLE001 单次盘口失败只跳过本次跟价
                    logger.warning("maker 跟价读取盘口失败，本次不重挂：%s", exc)

            total_filled = maker_filled + current_filled
            if total_filled >= amount:
                note = "maker 全部成交"
                if repeg_count:
                    note += f"（重挂 {repeg_count} 次）"
                return HedgeFillResult(
                    filled=amount,
                    used_taker=False,
                    note=note,
                )

            # 只把交易所的静默撤单视为可恢复状态；过期或普通拒单仍沿用
            # 原有收尾路径，避免对余额、精度等永久性错误反复下单。
            if _is_terminal(status) and not _is_cancelled(status):
                break

            price_moved = (
                observed_best is not None
                and _has_moved_behind(current_price, observed_best)
            )
            if not price_moved:
                if _is_terminal(status):
                    break
                continue

            now = time.monotonic()
            if now - last_place_at < MAKER_REPEG_MIN_INTERVAL_S:
                logger.debug(
                    "maker 最优价已移动，但距上次挂单不足 %.1f 秒，本次跳过重挂",
                    MAKER_REPEG_MIN_INTERVAL_S,
                )
                continue
            if repeg_count >= max_repegs:
                note_parts.append(f"已达到重挂上限 {max_repegs} 次")
                logger.warning(
                    "maker 已达到重挂上限 %s 次，转入撤单与吃单补齐路径",
                    max_repegs,
                )
                break

            if not _is_cancelled(status):
                try:
                    await adapter.cancel_order(market, current_order_id)
                except Exception as exc:  # noqa: BLE001 旧单可能仍在，禁止再挂新单
                    note_parts.append(f"撤单失败（{exc}）")
                    logger.warning(
                        "maker 跟价撤单失败，禁止重挂并转入安全收尾：%s",
                        exc,
                    )
                    break

                # 撤单请求返回不等于订单已经终止。只有读到终态及其最终累计
                # 成交量，才能安全计算下一张子订单的数量。
                while True:
                    try:
                        current_filled, status = await _read_order(
                            current_order_id
                        )
                    except Exception as exc:  # noqa: BLE001 必须转实际仓位差
                        fallback_reason = f"撤单后订单状态读取失败（{exc}）"
                        logger.warning(
                            "%s，将按实际仓位差补单",
                            fallback_reason,
                        )
                        break
                    if _is_terminal(status):
                        break
                    if time.monotonic() >= deadline:
                        logger.warning(
                            "maker 跟价撤单未在总超时内进入终态，停止重挂"
                        )
                        break
                    await asyncio.sleep(
                        min(poll_s, max(0.0, deadline - time.monotonic()))
                    )
                if fallback_reason or not _is_terminal(status):
                    break
                if status in {"EXPIRED", "REJECTED"}:
                    break

            # 此处的 current_filled 来自交易所终态；即便轮询时只看到部分
            # 成交，也不能用此前快照或原委托量推断剩余数量。
            maker_filled += current_filled
            remaining = amount - maker_filled
            if remaining <= 0:
                note = "maker 全部成交"
                if repeg_count:
                    note += f"（重挂 {repeg_count} 次）"
                return HedgeFillResult(
                    filled=amount,
                    used_taker=False,
                    note=note,
                )

            try:
                response = await adapter.place_limit_order(
                    market,
                    side,
                    remaining,
                    observed_best,
                    post_only=True,
                    reduce_only=reduce_only,
                )
            except Exception as exc:  # noqa: BLE001 交易所业务拒绝类型不统一
                if not _is_post_only_immediate_match_error(exc):
                    raise
                fallback_reason = f"重挂 post-only 被拒（{exc}）"
                current_order_id = None
                logger.warning(
                    "maker 重挂 post-only 被拒，将按实际仓位差吃单补齐：%s",
                    exc,
                )
                break

            current_order_id = _order_id(response)
            current_price = observed_best
            current_filled = Decimal(0)
            repeg_count += 1
            last_place_at = time.monotonic()
            logger.info(
                "maker 跟价重挂第 %s/%s 次：价格=%s 剩余数量=%s",
                repeg_count,
                max_repegs,
                current_price,
                remaining,
            )
    else:
        fallback_reason = "适配器缺少订单状态查询能力"
        if timeout_s > 0:
            await asyncio.sleep(timeout_s)
        logger.warning("%s，将撤单后按实际仓位差补单", fallback_reason)

    if current_order_id is not None:
        try:
            await adapter.cancel_order(market, current_order_id)
        except Exception as exc:  # noqa: BLE001
            note_parts.append(f"撤单失败（{exc}）")
            logger.warning("对冲撤单失败，将按重读结果决定是否吃单：%s", exc)

    # 有单查能力时仍优先重读精确成交量；能力缺失、返回空或请求异常时，
    # 统一退回实际仓位差。撤单失败也不能跳过这一步。
    if not fallback_reason and current_order_id is not None:
        try:
            current_filled, _status = await _read_order(current_order_id)
        except Exception as exc:  # noqa: BLE001 单查瞬时失效同样安全降级
            fallback_reason = f"撤单后订单状态读取失败（{exc}）"
            logger.warning("%s，将按实际仓位差补单", fallback_reason)

    if fallback_reason:
        position_after = Decimal(
            str((await adapter.get_position(market)).signed_size)
        )
        actual_delta = position_after - position_before
        remaining_delta = target_delta - actual_delta
        same_direction = remaining_delta * target_delta > 0
        remaining = abs(remaining_delta) if same_direction else Decimal(0)
        filled = min(amount, abs(actual_delta))
        note_parts.append(fallback_reason)
    else:
        filled = min(amount, maker_filled + current_filled)
        remaining = amount - filled
    note = "；".join(note_parts)
    if remaining <= 0:
        return HedgeFillResult(
            filled=filled,
            used_taker=False,
            note=(note + "；实际仓位差显示无需补单").lstrip("；"),
        )

    await adapter.market_order(
        market,
        side,
        remaining,
        reduce_only=reduce_only,
    )
    return HedgeFillResult(
        filled=amount,
        used_taker=True,
        note=(note + f"；maker 成交 {filled}，吃单补 {remaining}").lstrip("；"),
    )
