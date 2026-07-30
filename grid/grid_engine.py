"""实盘限价单网格引擎（Extended，BTC）。

真正的网格：在盘口挂一排限价单——当前价下方挂买单、上方挂卖单（几何等距）。
成交即翻单：买单成交→在上一格挂卖单止盈；卖单成交→在下一格挂买单。maker 费。
regime 急停：强趋势/突破 → 撤所有单 + 市价平库存。库存上限封趋势亏损。

安全：专用账户；重启自动接管账户已有持仓与挂单（对账后续挂）。
dry_run 只打印意图不真正挂单。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from adapters.base import Side
from grid.band import blocked_side_for_breach, compute_band, is_out_of_band
from grid.grid_state import GridState, load_state, save_state
from grid.regime import (
    GridMode,
    _wilder_atr,
    adx,
    decide_mode,
    donchian_prev,
    drop_forming_candle,
    trend_gate,
    close_slope,
)
from grid.risk import dist_to_liq_pct, hard_stop_triggered
from infra.logger import get_logger

logger = get_logger("grid_engine")

# TPSL 触发价相对变化不超过千分之一时不重挂，避免细小行情抖动冲刷历史订单。
_TPSL_REPRICE_THRESHOLD_PCT = Decimal("0.001")
# BTC 触发价按 0.1 tick 取整；同时保留百万分之一相对容差以兼容高价位。
_TPSL_EXCHANGE_TRIGGER_TOLERANCE_ABS = Decimal("0.1")
_TPSL_EXCHANGE_TRIGGER_TOLERANCE_PCT = Decimal("0.000001")
# 连续多轮无法解析单笔终态时升级告警，但仍不得猜测终态或补重复单。
_ORDER_STATUS_CRITICAL_RETRIES = 3


@dataclass
class GridConfig:
    market: str = "BTC-USD"
    spacing_pct: float = 0.02            # 格距
    unit_usd: float = 50.0               # 每格名义
    max_inventory_usd: float = 150.0     # 最大库存名义
    levels_per_side: int = 4             # 上下各挂几格
    adx_period: int = 14
    adx_off: float = 999.0               # 默认禁用 ADX 熔断（只靠库存上限控风险）
    adx_resume: float = 999.0            # 迟滞：OFF 后 ADX 须回落到此值以下才恢复
    donchian_period: int = 48
    candle_lookback: int = 200
    poll_interval: float = 30.0
    dry_run: bool = True
    trend_aware: bool = False
    band_k: float = 1.75
    min_half_frac: float = 0.04
    hard_stop_dist: float = 0.12
    recenter_bars: int = 3
    state_path: str = "data/grid_state.json"
    exchange_tpsl: bool = True
    liq_buffer_pct: float = 0.12
    max_equity_loss_pct: float = 0.10
    flat_confirmation_interval: float = 0.05


class GridEngine:
    def __init__(self, ext, config: GridConfig | None = None, fng_provider=None) -> None:
        self.ext = ext
        self.config = config or GridConfig()
        self._fng_provider = fng_provider
        self._orders: dict[int, dict] = {}   # level -> {"id":..., "side": Side}
        self._mode = GridMode.NEUTRAL        # 上一轮 regime（迟滞用）
        self._running = False
        self._state: GridState | None = None
        self._latest_atr = 0.0
        self._neutral_bars = 0
        self._current_closed_bar_key: int | None = None
        self._last_recenter_bar_key: int | None = None
        self._last_dist_to_liq: float | None = None
        self._last_highs: list[float] = []
        self._last_lows: list[float] = []
        self._last_closes: list[float] = []
        self._last_mark: float | None = None
        self._last_inv: Decimal | float | None = None
        self._current_tpsl: tuple[Decimal, Decimal] | None = None

    @property
    def _log_step(self) -> float:
        return math.log(1 + self.config.spacing_pct)

    def _level_price(self, level: int) -> Decimal:
        return Decimal(str(math.exp(level * self._log_step)))

    def _price_level(self, price: float) -> int:
        return round(math.log(price) / self._log_step)

    async def connect(self) -> None:
        await self.ext.connect()
        pos = await self.ext.get_position(self.config.market)
        if not self.config.dry_run and pos.signed_size != 0:
            stats = await self.ext._client.info.get_market_statistics(market_name=self.config.market)
            notional = abs(float(pos.signed_size)) * float(stats.data.mark_price)
            if notional > 2 * self.config.max_inventory_usd:
                raise RuntimeError(
                    f"账户已有持仓 {pos.signed_size}(≈${notional:.0f})远超库存上限，疑似外部仓，请先平掉。")
            logger.warning("接管账户已有持仓 %s 作为网格库存", pos.signed_size)
        # 接管账户上已存在的本网格挂单（重启对账）
        open_orders = await self._adopt_open_orders()
        if self.config.trend_aware:
            self._state = load_state(self.config.state_path)
            unsafe_without_state = (
                pos.signed_size != 0
                or open_orders is None
                or bool(open_orders)
            )
            if self._state is None and unsafe_without_state:
                # band_low/high=0 表示没有可安全恢复的有效 band；该状态不会自动重建。
                self._state = GridState(0.0, 0.0, True, None, False)
                save_state(self.config.state_path, self._state)
                logger.error(
                    "趋势感知状态缺失/损坏，且账户有仓、挂单或挂单状态未知；"
                    "已 fail-closed，等待人工介入"
                )
        logger.info("网格引擎连接完成（dry_run=%s，持仓=%s，接管挂单=%d）",
                    self.config.dry_run, pos.signed_size, len(self._orders))

    async def _adopt_open_orders(self) -> list | None:
        """把交易所上已有的挂单按格价归入本引擎状态（重启接管）。"""
        try:
            orders = await self.ext.get_open_orders(self.config.market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询挂单失败：%s", exc)
            return None
        for o in orders:
            price = float(getattr(o, "price", 0) or 0)
            if price <= 0:
                continue
            lv = self._price_level(price)
            side = Side.BUY if "BUY" in str(getattr(o, "side", "")).upper() else Side.SELL
            self._orders[lv] = {"id": getattr(o, "id", None), "side": side}
        return orders

    async def _inv(self) -> tuple[Decimal, float]:
        pos = await self.ext.get_position(self.config.market)
        stats = await self.ext._client.info.get_market_statistics(market_name=self.config.market)
        price = float(stats.data.mark_price)
        return pos.signed_size, price

    @staticmethod
    def _has_valid_band(state: GridState) -> bool:
        """判断状态里是否有可使用的 band；0/0 是 fail-closed 哨兵。"""
        return state.band_low > 0 and state.band_high > state.band_low

    @staticmethod
    def _is_non_reduce_only_order(order) -> bool:
        """TPSL/条件单与 reduce-only 单不是重建 band 的阻碍。"""
        if bool(getattr(order, "reduce_only", False)):
            return False
        order_type = str(getattr(order, "type", "") or "").upper()
        return "TPSL" not in order_type and "CONDITIONAL" not in order_type

    @staticmethod
    def _side_is_blocked(side: Side, blocked_side: str | None) -> bool:
        """blocked_side='BOTH' 供趋势 OFF 使用，禁止双侧任何补单。"""
        return blocked_side == "BOTH" or side.value == blocked_side

    def _count_neutral_bar(self, mode: GridMode) -> None:
        """按已收盘 K 线去重累计连续 NEUTRAL，避免 30 秒轮询重复计数。"""
        if mode is not GridMode.NEUTRAL:
            self._neutral_bars = 0
            self._last_recenter_bar_key = self._current_closed_bar_key
            return
        if self._current_closed_bar_key is None:
            # 单元测试或独立调用没有 candle key 时，每次调用视为一根新 bar。
            self._neutral_bars += 1
            return
        if self._current_closed_bar_key != self._last_recenter_bar_key:
            self._neutral_bars += 1
            self._last_recenter_bar_key = self._current_closed_bar_key

    async def _advance_band(self, mark: float, mode: GridMode) -> None:
        """推进 ACTIVE/FROZEN 两态；既有 band 在越界时绝不随 mark 移动。"""
        if self._state is None:
            if mode is not GridMode.NEUTRAL:
                return
            low, high = compute_band(
                mark,
                self._latest_atr,
                self.config.band_k,
                self.config.min_half_frac,
            )
            self._state = GridState(low, high, False, None, False)
            save_state(self.config.state_path, self._state)
            self._neutral_bars = 0
            self._last_recenter_bar_key = self._current_closed_bar_key
            logger.info("新建固定 band：[%.2f, %.2f]", low, high)
            return

        state = self._state
        if state.halted:
            return

        if not state.frozen:
            if (
                self._has_valid_band(state)
                and is_out_of_band(mark, state.band_low, state.band_high)
            ):
                blocked_side = blocked_side_for_breach(
                    mark,
                    state.band_low,
                    state.band_high,
                )
                # 先持久化 FROZEN，避免撤单网络调用期间崩溃后按现价重建。
                self._state = GridState(
                    state.band_low,
                    state.band_high,
                    True,
                    blocked_side,
                    state.halted,
                )
                save_state(self.config.state_path, self._state)
                self._neutral_bars = 0
                self._last_recenter_bar_key = self._current_closed_bar_key
                try:
                    if self.config.dry_run:
                        for level in list(self._orders):
                            await self._cancel(level, why="band 越界冻结")
                    else:
                        await self.ext.cancel_grid_orders(self.config.market)
                        self._orders.clear()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("band 越界后撤网格单失败，保持 FROZEN：%s", exc)
                logger.warning(
                    "mark=%.2f 越出固定 band [%.2f, %.2f]，冻结 %s；band 中心不追价",
                    mark,
                    state.band_low,
                    state.band_high,
                    blocked_side,
                )
            return

        # 0/0 表示 connect 时无法恢复有效 band 的 fail-closed 状态，只能人工处理。
        if not self._has_valid_band(state):
            return

        self._count_neutral_bar(mode)
        if self._neutral_bars < max(1, self.config.recenter_bars):
            return

        pos = await self.ext.get_position(self.config.market)
        if pos.signed_size != 0:
            return
        try:
            open_orders = await self.ext.get_open_orders(self.config.market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重建 band 前查询挂单失败，保持 FROZEN：%s", exc)
            return
        if any(self._is_non_reduce_only_order(order) for order in open_orders):
            return

        low, high = compute_band(
            mark,
            self._latest_atr,
            self.config.band_k,
            self.config.min_half_frac,
        )
        self._state = GridState(low, high, False, None, False)
        save_state(self.config.state_path, self._state)
        self._neutral_bars = 0
        self._last_recenter_bar_key = self._current_closed_bar_key
        logger.info("冷却满足，重建固定 band：[%.2f, %.2f]", low, high)

    async def _maintain_tpsl(self, mark: float, signed_size: Decimal) -> bool:
        """维护交易所端整仓止损；本任务以挂单调用成功视为已确认。"""
        signed_size = Decimal(str(signed_size))
        if signed_size == 0:
            # 空仓后旧保护已经失去对应仓位，新开同尺寸仓位时必须重新挂单。
            self._current_tpsl = None
            return True
        if not self.config.exchange_tpsl:
            return True

        mark_decimal = Decimal(str(mark))
        stop_distance = Decimal(str(self.config.hard_stop_dist))
        liq_buffer = Decimal(str(self.config.liq_buffer_pct))
        max_loss_pct = Decimal(str(self.config.max_equity_loss_pct))

        liq_price: Decimal | None = None
        liquidation_getter = getattr(self.ext, "get_liquidation_info", None)
        if callable(liquidation_getter):
            try:
                liquidation_info = await liquidation_getter(self.config.market)
            except Exception as exc:  # noqa: BLE001
                logger.warning("计算 TPSL 时查询清算价失败，将使用其他可用约束：%s", exc)
            else:
                if liquidation_info is not None:
                    candidate_liq = Decimal(str(liquidation_info[1]))
                    if candidate_liq > 0:
                        liq_price = candidate_liq

        equity: Decimal | None = None
        balance_getter = getattr(self.ext, "get_balance", None)
        if callable(balance_getter):
            try:
                balance = await balance_getter()
            except Exception as exc:  # noqa: BLE001
                logger.warning("计算 TPSL 时查询权益失败，将使用其他可用约束：%s", exc)
            else:
                raw_equity = (
                    balance.get("equity")
                    if isinstance(balance, dict)
                    else getattr(balance, "equity", None)
                )
                if raw_equity is not None:
                    candidate_equity = Decimal(str(raw_equity))
                    if candidate_equity > 0:
                        equity = candidate_equity

        constraints: list[Decimal] = []
        if signed_size > 0:
            if liq_price is not None and Decimal("0") <= liq_buffer < Decimal("1"):
                constraints.append(liq_price / (Decimal("1") - liq_buffer))
            if equity is not None and max_loss_pct >= 0:
                equity_floor = equity * (Decimal("1") - max_loss_pct)
                equity_trigger = mark_decimal - (
                    (equity - equity_floor) / abs(signed_size)
                )
                if equity_trigger > 0:
                    constraints.append(equity_trigger)
            trigger_price = (
                max(constraints)
                if constraints
                else mark_decimal * (Decimal("1") - stop_distance)
            )
        else:
            if liq_price is not None and liq_buffer >= 0:
                constraints.append(liq_price / (Decimal("1") + liq_buffer))
            if equity is not None and max_loss_pct >= 0:
                equity_floor = equity * (Decimal("1") - max_loss_pct)
                equity_trigger = mark_decimal + (
                    (equity - equity_floor) / abs(signed_size)
                )
                if equity_trigger > 0:
                    constraints.append(equity_trigger)
            trigger_price = (
                min(constraints)
                if constraints
                else mark_decimal * (Decimal("1") + stop_distance)
            )

        previous = self._current_tpsl
        if previous is not None:
            previous_size, previous_trigger = previous
            same_direction = (previous_size > 0) == (signed_size > 0)
            if not same_direction:
                # 仓位翻向后旧方向的单向收紧约束必须失效。
                self._current_tpsl = None
                previous = None
            else:
                # 同方向持仓期间只允许把止损朝更早触发的方向移动。
                if signed_size > 0:
                    trigger_price = max(trigger_price, previous_trigger)
                else:
                    trigger_price = min(trigger_price, previous_trigger)

        if previous is not None:
            previous_size, previous_trigger = previous
            relative_change = (
                abs(trigger_price - previous_trigger) / previous_trigger
                if previous_trigger > 0
                else Decimal("Infinity")
            )
            if (
                signed_size == previous_size
                and relative_change <= _TPSL_REPRICE_THRESHOLD_PCT
            ):
                position_tpsl_getter = getattr(
                    self.ext,
                    "get_position_tpsl",
                    None,
                )
                if not callable(position_tpsl_getter):
                    return True

                exchange_matches = False
                try:
                    exchange_tpsl = await position_tpsl_getter(self.config.market)
                    exchange_trigger = (
                        getattr(exchange_tpsl, "trigger_price", None)
                        if exchange_tpsl is not None
                        else None
                    )
                    if exchange_trigger is None and exchange_tpsl is not None:
                        stop_loss = getattr(exchange_tpsl, "stop_loss", None)
                        exchange_trigger = getattr(stop_loss, "trigger_price", None)
                    if exchange_trigger is not None:
                        exchange_trigger = Decimal(str(exchange_trigger))
                        exchange_tolerance = max(
                            _TPSL_EXCHANGE_TRIGGER_TOLERANCE_ABS,
                            abs(previous_trigger)
                            * _TPSL_EXCHANGE_TRIGGER_TOLERANCE_PCT,
                        )
                        exchange_matches = (
                            abs(exchange_trigger - previous_trigger)
                            <= exchange_tolerance
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "交易所整仓 TPSL 校验失败，将重新挂单：%s",
                        exc,
                    )
                if exchange_matches:
                    return True

        if self.config.dry_run:
            logger.info("[dry_run] 维护整仓TPSL 触发价=%s", trigger_price)
            return True

        try:
            await self.ext.place_position_stop_loss(
                self.config.market,
                signed_size,
                trigger_price,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "交易所整仓 TPSL 维护失败：持仓=%s 触发价=%s；本轮禁止新增风险仓：%s",
                signed_size,
                trigger_price,
                exc,
            )
            return False

        self._current_tpsl = (signed_size, trigger_price)
        logger.info(
            "交易所整仓 TPSL 挂单请求已成功提交：持仓=%s 触发价=%s",
            signed_size,
            trigger_price,
        )
        return True

    async def _dump_live(
        self,
        mark: float | None,
        mode: GridMode,
        inv: Decimal | float | None,
        closes: list[float],
    ) -> None:
        """把趋势感知单轮状态原子写入面板读取的 live 快照。"""
        state = self._state
        live_path = Path(self.config.state_path).parent / "grid_live.json"
        live_path.parent.mkdir(parents=True, exist_ok=True)

        adx_val = None
        if (
            self._last_highs
            and self._last_lows
            and len(self._last_highs) == len(closes)
            and len(self._last_lows) == len(closes)
        ):
            adx_values = adx(
                self._last_highs,
                self._last_lows,
                closes,
                self.config.adx_period,
            )
            adx_val = adx_values[-1] if adx_values else None

        mark_float = float(mark) if mark is not None else None
        inv_btc = float(inv) if inv is not None else None
        payload = {
            "ts": time.time(),
            "mark": mark_float,
            "mode": mode.value,
            "adx": adx_val,
            "slope_short": close_slope(closes, 6),
            "slope_long": close_slope(closes, 24),
            "atr_pct": (
                self._latest_atr / mark_float
                if mark_float is not None and mark_float > 0
                else None
            ),
            "inv_btc": inv_btc,
            "inv_usd": (
                inv_btc * mark_float
                if inv_btc is not None and mark_float is not None
                else None
            ),
            "band_low": state.band_low if state is not None else None,
            "band_high": state.band_high if state is not None else None,
            "frozen": state.frozen if state is not None else False,
            "blocked_side": state.blocked_side if state is not None else None,
            "halted": state.halted if state is not None else False,
            "dist_to_liq_pct": self._last_dist_to_liq,
            "cfg": {
                "unit": self.config.unit_usd,
                "levels": self.config.levels_per_side,
                "max_inv": self.config.max_inventory_usd,
                "spacing": self.config.spacing_pct,
                "hard_stop_dist": self.config.hard_stop_dist,
                "adx_off": self.config.adx_off,
            },
        }
        tmp = live_path.with_suffix(live_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, live_path)

    async def _run_once_trend_aware(self) -> str:
        """趋势感知单轮：硬止损前置，再做门控、band 推进与受限铺单。"""
        if self._state is None:
            self._state = load_state(self.config.state_path)
        if self._state is not None and self._state.halted:
            pos = await self.ext.get_position(self.config.market)
            inv = pos.signed_size
            self._last_inv = inv
            if inv == 0:
                self._current_tpsl = None
            else:
                stats = await self.ext._client.info.get_market_statistics(
                    market_name=self.config.market
                )
                mark = float(stats.data.mark_price)
                self._last_mark = mark
                await self._maintain_tpsl(mark, inv)
                # halted 只禁止新增报价，既有硬止损风险管理仍继续执行。
                if await self._check_hard_stop():
                    await self._dump_live(
                        mark=self._last_mark,
                        mode=self._mode,
                        inv=self._last_inv,
                        closes=self._last_closes,
                    )
                    return "HALTED：硬止损已触发"
            await self._dump_live(
                mark=self._last_mark,
                mode=self._mode,
                inv=self._last_inv,
                closes=self._last_closes,
            )
            return "HALTED：禁止新增报价"

        if await self._check_hard_stop():
            await self._dump_live(
                mark=self._last_mark,
                mode=self._mode,
                inv=self._last_inv,
                closes=self._last_closes,
            )
            return "HALTED：硬止损已触发"
        if self._state is not None and self._state.halted:
            await self._dump_live(
                mark=self._last_mark,
                mode=self._mode,
                inv=self._last_inv,
                closes=self._last_closes,
            )
            return "HALTED：禁止新增报价"

        r = await self.ext._client.info.get_candles_history(
            market_name=self.config.market,
            candle_type="trades",
            interval="PT1H",
            limit=self.config.candle_lookback,
        )
        candles = sorted(r.data, key=lambda k: int(k.timestamp))
        highs = drop_forming_candle([float(k.high) for k in candles])
        lows = drop_forming_candle([float(k.low) for k in candles])
        closes = drop_forming_candle([float(k.close) for k in candles])
        self._last_highs = highs
        self._last_lows = lows
        self._last_closes = closes
        self._current_closed_bar_key = (
            int(candles[-2].timestamp) if len(candles) >= 2 else None
        )

        mode = trend_gate(
            highs,
            lows,
            closes,
            adx_off=self.config.adx_off,
            adx_resume=self.config.adx_resume,
            prev_mode=self._mode,
            adx_period=self.config.adx_period,
        )
        self._mode = mode
        atr_values = _wilder_atr(highs, lows, closes, self.config.adx_period)
        self._latest_atr = next(
            (float(value) for value in reversed(atr_values) if value is not None),
            0.0,
        )

        inv, mark = await self._inv()
        self._last_inv = inv
        self._last_mark = mark
        inv_usd = float(inv) * mark
        await self._advance_band(mark, mode)

        state = self._state
        if state is not None and state.halted:
            await self._dump_live(mark=mark, mode=mode, inv=inv, closes=closes)
            return "HALTED：禁止新增报价"

        tpsl_confirmed = await self._maintain_tpsl(mark, inv)
        frozen = state is not None and state.frozen
        valid_active = (
            state is not None
            and not state.frozen
            and self._has_valid_band(state)
        )
        if mode is GridMode.OFF:
            blocked_side = "BOTH"
        elif frozen:
            # fail-closed 哨兵没有可判定的突破方向，必须冻结双侧。
            blocked_side = state.blocked_side or "BOTH"
        else:
            blocked_side = state.blocked_side if state is not None else "BOTH"

        logger.info(
            "trend-aware mark=%.0f mode=%s inv=%s(≈$%.0f) band=%s frozen=%s blocked=%s",
            mark,
            mode.value,
            inv,
            inv_usd,
            (
                f"[{state.band_low:.0f},{state.band_high:.0f}]"
                if state is not None and self._has_valid_band(state)
                else None
            ),
            frozen,
            blocked_side,
        )

        if mode is GridMode.OFF or not valid_active:
            await self._handle_fills(inv_usd, blocked_side=blocked_side)
            await self._dump_live(mark=mark, mode=mode, inv=inv, closes=closes)
            return (
                "OFF：暂停新增报价"
                if mode is GridMode.OFF
                else "FROZEN：仅处理已有订单"
            )

        await self._handle_fills(inv_usd, blocked_side=state.blocked_side)
        if not tpsl_confirmed:
            await self._dump_live(mark=mark, mode=mode, inv=inv, closes=closes)
            return "TPSL 未确认：仅处理已有订单"
        result = await self._maintain_ladder(
            mark,
            inv_usd,
            band=(state.band_low, state.band_high),
            blocked_side=state.blocked_side,
        )
        await self._dump_live(mark=mark, mode=mode, inv=inv, closes=closes)
        return result

    async def run_once(self) -> str:
        if self.config.trend_aware:
            return await self._run_once_trend_aware()

        # 指标 + regime
        r = await self.ext._client.info.get_candles_history(
            market_name=self.config.market, candle_type="trades",
            interval="PT1H", limit=self.config.candle_lookback)
        candles = sorted(r.data, key=lambda k: int(k.timestamp))
        highs = [float(k.high) for k in candles]; lows = [float(k.low) for k in candles]
        closes = [float(k.close) for k in candles]
        a = adx(highs, lows, closes, self.config.adx_period)
        up, lo = donchian_prev(highs, lows, self.config.donchian_period)
        price = closes[-1]
        fng = self._fng_provider() if self._fng_provider else None
        mode = decide_mode(adx_val=a[-1], close=price, donchian_up=up[-1], donchian_lo=lo[-1],
                           fng=fng, adx_off=self.config.adx_off,
                           adx_resume=self.config.adx_resume, prev_mode=self._mode)
        self._mode = mode

        inv, mark = await self._inv()
        inv_usd = float(inv) * mark
        logger.info("price=%.0f ADX=%s mode=%s inv=%s(≈$%.0f) 挂单=%d",
                    price, f"{a[-1]:.1f}" if a[-1] else None, mode.value, inv, inv_usd, len(self._orders))

        if mode is GridMode.OFF:
            return await self._go_off(inv)

        # 1) 处理成交：已挂但盘口消失的单=成交，翻到反向一格
        await self._handle_fills(inv_usd)
        # 2) 维护阶梯：当前价上下各挂 N 格
        return await self._maintain_ladder(price, inv_usd)

    async def _open_ids(self) -> set:
        orders = await self.ext.get_open_orders(self.config.market)
        return {getattr(o, "id", None) for o in orders}

    async def _order_statuses(self, order_ids: set) -> dict:
        """订单 id → 订单对象；批量历史未命中时按 ID 单查兜底。"""
        try:
            orders = await self.ext.get_orders_history(
                self.config.market,
                limit=100,
                order_type="LIMIT",
                sort="UPDATED_AT",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询订单历史失败：%s", exc)
            orders = []
        statuses = {getattr(o, "id", None): o for o in orders}

        order_getter = getattr(self.ext, "get_order_by_id", None)
        if not callable(order_getter):
            return statuses
        for order_id in order_ids - statuses.keys():
            try:
                order = await order_getter(self.config.market, order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("按 ID 查询订单 %s 失败，下轮重试：%s", order_id, exc)
                continue
            if order is not None:
                statuses[order_id] = order
        return statuses

    async def _handle_fills(self, inv_usd: float, blocked_side: str | None = None) -> None:
        """处理从盘口消失的跟踪单——按交易所终态区分，只有真成交才翻单。

        订单会因 EXPIRED(有效期到)/CANCELLED(急停或手动)消失，误当成交翻单
        会挂出穿越盘口的反向单（2026-07-20 实盘事故）。
        - FILLED 或有成交量：买成交→上一格挂卖；卖成交→下一格挂买。
        - EXPIRED/CANCELLED：原格原方向重挂。
        - REJECTED：确认终态后移除记录，交给补格逻辑按当前中心重铺。
        - 未知或非终态：保留记录并在下轮继续查询，禁止同格重复补单。
        - blocked_side：跳过即将挂出的同方向新单。
        """
        if self.config.dry_run:
            return
        open_ids = await self._open_ids()
        for rec in self._orders.values():
            if rec["id"] in open_ids:
                # 订单重新出现在盘口时解除终态待确认标记。
                rec.pop("status_pending", None)
                rec.pop("status_lookup_attempts", None)
        missing = {lv: rec for lv, rec in self._orders.items() if rec["id"] not in open_ids}
        if not missing:
            return
        statuses = await self._order_statuses({rec["id"] for rec in missing.values()})
        for lv, rec in missing.items():
            o = statuses.get(rec["id"])
            status = str(getattr(o, "status", "") or "").upper() if o else ""
            filled = Decimal(str(getattr(o, "filled_qty", 0) or 0)) if o else Decimal("0")
            if status not in ("FILLED", "EXPIRED", "CANCELLED", "REJECTED"):
                attempts = int(rec.get("status_lookup_attempts", 0)) + 1
                rec["status_lookup_attempts"] = attempts
                rec["status_pending"] = True
                if (
                    attempts == _ORDER_STATUS_CRITICAL_RETRIES
                    or attempts % 100 == 0
                ):
                    logger.critical(
                        "格%d 订单 %s 已连续 %d 轮无法解析终态；"
                        "该格保持冻结以防重复单，请人工对账",
                        lv,
                        rec["id"],
                        attempts,
                    )
                else:
                    logger.warning(
                        "格%d 订单 %s 终态=%s，保留记录下轮重试",
                        lv,
                        rec["id"],
                        status or "未知",
                    )
                continue

            self._orders.pop(lv, None)
            if status == "FILLED" or (
                status in ("EXPIRED", "CANCELLED") and filled > 0
            ):
                if rec["side"] is Side.BUY:      # 买成交 → 上一格挂卖止盈
                    next_level, next_side = lv + 1, Side.SELL
                    fill_why = f"{lv}买成交→挂卖"
                else:                             # 卖成交 → 下一格挂买
                    next_level, next_side = lv - 1, Side.BUY
                    fill_why = f"{lv}卖成交→挂买"
                if self._side_is_blocked(next_side, blocked_side):
                    continue
                qty = filled if filled > 0 else None
                await self._place(next_level, next_side, inv_usd, why=fill_why, qty=qty)
            elif status in ("EXPIRED", "CANCELLED"):
                if self._side_is_blocked(rec["side"], blocked_side):
                    continue
                await self._place(lv, rec["side"], inv_usd, why=f"格{lv}{status}重挂")
            else:
                logger.warning("格%d 订单 %s 终态=REJECTED，仅移除不翻单", lv, rec["id"])

    async def _maintain_ladder(
        self,
        price: float,
        inv_usd: float,
        band: tuple[float, float] | None = None,
        blocked_side: str | None = None,
    ) -> str:
        center = self._price_level(price)
        n = self.config.levels_per_side
        acted = []

        def allowed(level: int, side: Side) -> bool:
            if self._side_is_blocked(side, blocked_side):
                return False
            if band is None:
                return True
            level_price = float(self._level_price(level))
            return band[0] <= level_price <= band[1]

        # 下方挂买（_place 内部按库存上限守卫）
        for lv in range(center - 1, center - n - 1, -1):
            if lv not in self._orders and allowed(lv, Side.BUY):
                m = await self._place(lv, Side.BUY, inv_usd, why="补买格")
                if m:
                    acted.append(m)
        # 上方挂卖
        for lv in range(center + 1, center + n + 1):
            if lv not in self._orders and allowed(lv, Side.SELL):
                m = await self._place(lv, Side.SELL, inv_usd, why="补卖格")
                if m:
                    acted.append(m)
        # 撤掉远离中心、落在 band 外或属于冻结方向的陈单。
        for lv, rec in list(self._orders.items()):
            if rec.get("status_pending"):
                # 已从盘口消失但终态未知的订单必须等待单查结果，不能按陈单误删。
                continue
            stale = lv < center - n - 2 or lv > center + n + 2
            constrained_out = (
                band is not None
                and not (band[0] <= float(self._level_price(lv)) <= band[1])
            )
            side_blocked = self._side_is_blocked(rec["side"], blocked_side)
            if stale or constrained_out or side_blocked:
                await self._cancel(lv, why="撤陈单/越界单/冻结侧单")
        return "；".join(acted) if acted else "阶梯已就位"

    def _within_cap(self, side: Side, inv_usd: float) -> bool:
        """严格库存上限：真实持仓与该侧挂单全部成交后仍不超上限。"""
        unit = self.config.unit_usd
        inventory_usd = inv_usd if side is Side.BUY else -inv_usd
        pending_usd = sum(unit for rec in self._orders.values() if rec["side"] is side)
        return inventory_usd + pending_usd + unit <= self.config.max_inventory_usd

    async def _place(
        self,
        level: int,
        side: Side,
        inv_usd: float,
        why: str = "",
        qty: Decimal | None = None,
    ) -> str | None:
        if not self._within_cap(side, inv_usd):
            return None  # 超库存上限，不挂
        lp = self._level_price(level)
        qty = qty if qty is not None else Decimal(str(self.config.unit_usd)) / lp
        msg = f"{why}：{side.value} {qty:.6f}@{lp:.0f}(格{level})"
        if self.config.dry_run:
            logger.info("[dry_run] %s", msg)
            self._orders[level] = {"id": f"dry-{level}", "side": side}
            return msg
        try:
            res = await self.ext.place_limit_order(self.config.market, side, qty, lp)
            data = getattr(res, "data", None)
            oid = getattr(data, "id", None) or getattr(res, "id", None)
            if str(getattr(data, "status", "") or "") == "REJECTED":
                logger.warning("%s → 被拒(%s)，不入账", msg, getattr(data, "status_reason", None))
                return None
            self._orders[level] = {"id": oid, "side": side}
            logger.info("%s → id=%s", msg, oid)
            return msg
        except Exception as exc:  # noqa: BLE001
            logger.warning("挂单失败 %s：%s", msg, exc)
            return None

    async def _cancel(self, level: int, why: str = "") -> None:
        rec = self._orders.get(level)
        if not rec:
            return
        if self.config.dry_run:
            logger.info("[dry_run] %s：撤 格%d", why, level)
            self._orders.pop(level, None)
            return
        try:
            await self.ext.cancel_order(rec["id"])
            self._orders.pop(level, None)  # 撤单成功后才删记录
        except Exception as exc:  # noqa: BLE001
            logger.warning("撤单失败 格%d：%s（保留记录下轮重试）", level, exc)

    async def _check_hard_stop(self) -> bool:
        """检查距强平价距离；空仓与有仓但缺失清算信息必须区别处理。"""
        pos = await self.ext.get_position(self.config.market)
        signed_size = pos.signed_size
        self._last_inv = signed_size
        if signed_size == 0:
            self._last_dist_to_liq = None
            return False

        liquidation_info = await self.ext.get_liquidation_info(self.config.market)
        if liquidation_info is None:
            self._last_dist_to_liq = None
            logger.error(
                "硬止损检查进入 fail-safe：当前有仓位 %s，但无法取得清算价",
                signed_size,
            )
            return False

        mark, liq = liquidation_info
        self._last_mark = float(mark)
        self._last_dist_to_liq = dist_to_liq_pct(
            float(mark),
            float(liq),
            float(signed_size),
        )
        if not hard_stop_triggered(
            float(mark),
            float(liq),
            float(signed_size),
            self.config.hard_stop_dist,
        ):
            return False

        logger.critical(
            "触发硬止损：mark=%s liq=%s 持仓=%s 阈值=%.2f%%",
            mark,
            liq,
            signed_size,
            self.config.hard_stop_dist * 100,
        )
        await self._go_off_confirmed(signed_size)
        return True

    async def _go_off_confirmed(self, inv: Decimal) -> bool:
        """持久化急停并反复减仓；空仓且残单清净后才撤 TPSL。"""
        current_state = self._state or load_state(self.config.state_path)
        if current_state is None:
            halted_state = GridState(
                band_low=0.0,
                band_high=0.0,
                frozen=True,
                blocked_side=None,
                halted=True,
            )
        else:
            halted_state = GridState(
                band_low=current_state.band_low,
                band_high=current_state.band_high,
                frozen=current_state.frozen,
                blocked_side=current_state.blocked_side,
                halted=True,
            )
        self._state = halted_state
        save_state(self.config.state_path, halted_state)

        logger.critical("确认平仓链启动：初始持仓=%s，已持久化 halted=true", inv)
        if self.config.dry_run:
            logger.warning(
                "[dry_run] 确认平仓链：撤网格单，平库存=%s，确认空仓后撤 TPSL",
                inv,
            )
            return True

        try:
            await self.ext.cancel_grid_orders(self.config.market)
        except Exception as exc:  # noqa: BLE001
            logger.critical(
                "只撤网格单失败，保留 TPSL 并终止确认平仓链：%s",
                exc,
                exc_info=True,
            )
            return False

        max_close_attempts = 3
        close_attempts = 0
        consecutive_flat_reads = 0
        while True:
            try:
                pos = await self.ext.get_position(self.config.market)
            except Exception as exc:  # noqa: BLE001
                logger.critical(
                    "确认平仓时查询仓位失败；保留 TPSL：%s",
                    exc,
                    exc_info=True,
                )
                return False
            remaining = pos.signed_size
            if remaining == 0:
                consecutive_flat_reads += 1
                if consecutive_flat_reads < 2:
                    await asyncio.sleep(
                        max(0.0, float(self.config.flat_confirmation_interval))
                    )
                    continue

                try:
                    open_orders = await self.ext.get_open_orders(self.config.market)
                    residual_orders = [
                        order
                        for order in open_orders
                        if self._is_non_reduce_only_order(order)
                    ]
                except Exception as exc:  # noqa: BLE001
                    logger.critical(
                        "连续两次确认空仓，但查询残余普通挂单失败；保留 TPSL：%s",
                        exc,
                        exc_info=True,
                    )
                    return False
                if residual_orders:
                    logger.critical(
                        "连续两次确认空仓，但盘口仍有 %d 个普通挂单；保留 TPSL",
                        len(residual_orders),
                    )
                    return False

                try:
                    final_pos = await self.ext.get_position(self.config.market)
                except Exception as exc:  # noqa: BLE001
                    logger.critical(
                        "盘口已无普通挂单，但最终空仓复核失败；保留 TPSL：%s",
                        exc,
                        exc_info=True,
                    )
                    return False
                if final_pos.signed_size != 0:
                    logger.critical(
                        "盘口查询期间仓位重新出现 %s；保留 TPSL",
                        final_pos.signed_size,
                    )
                    return False

                try:
                    await self.ext.cancel_tpsl(self.config.market)
                except Exception as exc:  # noqa: BLE001
                    logger.critical(
                        "空仓且普通挂单已清净，但撤 TPSL 失败：%s",
                        exc,
                        exc_info=True,
                    )
                    return False
                self._current_tpsl = None
                logger.critical("已连续两次确认空仓，撤除 TPSL，确认平仓链完成")
                return True

            consecutive_flat_reads = 0
            if close_attempts >= max_close_attempts:
                logger.critical(
                    "确认平仓失败：%d 次 reduce_only 下单后仍有仓位 %s；保留 TPSL",
                    close_attempts,
                    remaining,
                )
                return False

            side = Side.SELL if remaining > 0 else Side.BUY
            close_attempts += 1
            try:
                await self.ext.market_order(
                    self.config.market,
                    side,
                    abs(remaining),
                    reduce_only=True,
                )
                logger.warning(
                    "确认平仓第 %d/%d 次：%s %s（reduce_only）",
                    close_attempts,
                    max_close_attempts,
                    side.value,
                    abs(remaining),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "确认平仓第 %d/%d 次下单失败：%s",
                    close_attempts,
                    max_close_attempts,
                    exc,
                )

    async def _go_off(self, inv: Decimal) -> str:
        # 撤所有挂单
        if self._orders:
            logger.warning("急停：撤 %d 个挂单", len(self._orders))
        for lv in list(self._orders.keys()):
            await self._cancel(lv, why="急停撤单")
        # 平库存
        if inv != 0:
            side = Side.SELL if inv > 0 else Side.BUY
            if self.config.dry_run:
                logger.warning("[dry_run] 急停平库存 %s %s", side.value, abs(inv))
            else:
                await self.ext.market_order(self.config.market, side, abs(inv), reduce_only=True)
                logger.warning("急停已平库存 %s %s", side.value, abs(inv))
        return "OFF：已撤单+平库存"

    async def run_forever(self) -> None:
        self._running = True
        await self.connect()
        logger.info("网格引擎启动（dry_run=%s，格距%.1f%%，每边%d格，库存上限$%.0f）",
                    self.config.dry_run, self.config.spacing_pct * 100,
                    self.config.levels_per_side, self.config.max_inventory_usd)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("本轮异常：%s", exc)
            await asyncio.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._running = False
