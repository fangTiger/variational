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


async def maker_first_hedge(
    adapter,
    market: str,
    target_delta: Decimal,
    *,
    timeout_s: float = 15.0,
    poll_s: float = 1.0,
    reduce_only: bool = False,
) -> HedgeFillResult:
    """先挂 maker，超时未成交则撤单、按剩余量吃单。

    成交判定使用订单状态与已成交量，而不是持仓差值。持仓差值遇到部分成交
    会误判成全部成交，导致欠对冲并留下无人跟踪的孤儿单。
    """
    if target_delta == 0:
        return HedgeFillResult(
            filled=Decimal(0),
            used_taker=False,
            note="目标为零",
        )

    side = Side.BUY if target_delta > 0 else Side.SELL
    amount = abs(target_delta)

    # 挂在本方最优价：卖挂 ask、买挂 bid。挂在对手价会立即成交，
    # 从而被 post_only 拒绝。
    quote = await adapter.get_market_price(market)
    price = quote.ask if side is Side.SELL else quote.bid
    response = await adapter.place_limit_order(
        market,
        side,
        amount,
        price,
        post_only=True,
        reduce_only=reduce_only,
    )

    def _field(value, name: str):
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)

    response_data = _field(response, "data") or response
    order_id = _field(response_data, "id") or _field(response, "id")
    if order_id is None:
        raise RuntimeError("maker 下单响应缺少订单 ID，无法安全追踪")

    async def _read_order() -> tuple[Decimal, str]:
        got = await adapter.get_order_by_id(market, order_id)
        data = _field(got, "data") or got
        raw_filled = _field(data, "filled_qty")
        raw_status = _field(data, "status")
        filled_qty = Decimal(str(raw_filled or 0))
        status = str(raw_status or "").upper().rsplit(".", 1)[-1]
        return filled_qty, status

    deadline = time.monotonic() + timeout_s
    filled = Decimal(0)
    terminal_statuses = {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        filled, status = await _read_order()
        if filled >= amount:
            return HedgeFillResult(
                filled=filled,
                used_taker=False,
                note="maker 全部成交",
            )
        if status in terminal_statuses:
            break

    note = ""
    try:
        await adapter.cancel_order(market, order_id)
    except Exception as exc:  # noqa: BLE001
        note = f"撤单失败（{exc}）"
        logger.warning("对冲撤单失败，将按重读结果决定是否吃单：%s", exc)

    # 撤单成功与否都重读一次：撤单前的瞬间可能又成交了一部分，
    # 按陈旧数据吃单会超额对冲。
    filled, _status = await _read_order()
    remaining = amount - filled
    if remaining <= 0:
        return HedgeFillResult(
            filled=filled,
            used_taker=False,
            note=(note + "；重读发现已全部成交，未吃单").lstrip("；"),
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
