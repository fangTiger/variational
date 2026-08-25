"""独立的定时定量双边对冲策略。

本模块只使用固定时间与每轮名义额区间语义，不依赖网格调度、网格库存或网格状态。
所有补单与回滚决策均在重读两侧实际持仓后计算，绝不相信委托量等于成交量。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from adapters.base import PositionPnl
from engine.hedge_engine import HedgeFillResult, maker_first_hedge

logger = logging.getLogger(__name__)

#: 台账读取成交明细的重试次数与间隔。
#: 交易所成交查询有索引延迟，平仓刚完成时查不到，实测 Hyperliquid 会漏记。
#: 不重试的话台账随机缺条，就失去了作为成本量尺的意义。
LEDGER_FILL_RETRIES = 4
LEDGER_FILL_RETRY_DELAY_SECONDS = 3.0

#: 基差门控只使用单实例最近的有限轮次，避免陈旧结构变化稀释当前分布。
BASIS_GATE_HISTORY_ROUNDS = 20
#: 少于五轮无法可靠估计分布，必须直接放行。
BASIS_GATE_MIN_HISTORY = 5
#: 低于该标准差的配对视为稳定结构性基差，门控直接放行。
#:
#: 这条放行分支是必需的：实例 C（Lighter×Variational ETH）的基差是
#: **结构性持久**的（恒为 +0.124%，实测标准差 0.0188%），
#: 等待再久也不会回归。若被门控拦下，只会一路等到 max_wait 强制开仓，
#: 白白推迟每一轮、拉长持仓时间。
#:
#: ⚠️ C 的 0.0188% 距此下限只有 0.0012% 余量，多跑几轮就可能越过。
#: 越过后该实例会被自己的窄门限反复拦截——**这是已知隐患**。
#: 缓解办法是给 C 显式传 `--basis-gate-sigma 0` 关闭门控，
#: 而不是调高本下限（调高会让 A/B 的门控一并失效）。
BASIS_GATE_STD_FLOOR_PCT = Decimal("0.02")


class RoundDirection(Enum):
    """Lighter 主腿的轮次方向。"""

    LONG = "long"
    SHORT = "short"

    def opposite(self) -> "RoundDirection":
        """返回下一轮方向。"""
        return RoundDirection.SHORT if self is RoundDirection.LONG else RoundDirection.LONG

    @property
    def sign(self) -> Decimal:
        """返回用于有符号持仓的方向系数。"""
        return Decimal(1) if self is RoundDirection.LONG else Decimal(-1)


@dataclass
class TimedVolumeConfig:
    """定时定量策略配置。"""

    primary_market: str = "BTC"
    hedge_market: str = "BTC-USD"
    notional_min_usd: int = 2000
    notional_max_usd: int = 2300
    cycle_seconds: float = 7200.0
    initial_direction: RoundDirection = RoundDirection.LONG
    maker_timeout_s: float = 300.0
    maker_poll_s: float = 1.0
    position_tolerance: Decimal = Decimal("0.000001")
    state_path: Path | str = Path("data/timed_volume/state.json")
    convergence_attempts: int = 3
    equity_path: Path | str | None = None
    ledger_path: Path | str | None = None
    heartbeat_path: Path | str | None = None
    instance: str | None = None
    basis_gate_sigma: Decimal = Decimal("0")
    basis_gate_max_wait_s: float = 1800.0

    def __post_init__(self) -> None:
        """归一化配置并拒绝无法安全运行的数值。"""
        self.notional_min_usd = self._normalize_notional_bound(
            self.notional_min_usd,
            "名义额下限",
        )
        self.notional_max_usd = self._normalize_notional_bound(
            self.notional_max_usd,
            "名义额上限",
        )
        self.position_tolerance = Decimal(str(self.position_tolerance))
        self.state_path = Path(self.state_path)
        if self.equity_path is not None:
            self.equity_path = Path(self.equity_path)
        if self.ledger_path is not None:
            self.ledger_path = Path(self.ledger_path)
        if self.heartbeat_path is not None:
            self.heartbeat_path = Path(self.heartbeat_path)
        self.basis_gate_sigma = Decimal(str(self.basis_gate_sigma))
        if self.instance is not None:
            self.instance = str(self.instance).strip() or None
        if self.ledger_path is not None and self.instance is None:
            stem = self.ledger_path.stem
            self.instance = (
                stem[:-7] if stem.lower().endswith("_ledger") else stem
            )
        if not isinstance(self.initial_direction, RoundDirection):
            self.initial_direction = RoundDirection(str(self.initial_direction))
        if self.notional_min_usd <= 0 or self.notional_max_usd <= 0:
            raise ValueError("单边名义额区间上下限必须大于零")
        if self.notional_min_usd > self.notional_max_usd:
            raise ValueError("单边名义额下限不得大于上限")
        if self.cycle_seconds <= 0:
            raise ValueError("轮次周期必须大于零")
        if self.maker_timeout_s < 0 or self.maker_poll_s < 0:
            raise ValueError("maker 超时与轮询间隔不得为负")
        if self.position_tolerance < 0:
            raise ValueError("持仓容差不得为负")
        if self.convergence_attempts <= 0:
            raise ValueError("收敛尝试次数必须大于零")
        if not self.basis_gate_sigma.is_finite() or self.basis_gate_sigma < 0:
            raise ValueError("基差门控标准差倍数必须为有限非负数")
        if (
            not math.isfinite(self.basis_gate_max_wait_s)
            or self.basis_gate_max_wait_s < 0
        ):
            raise ValueError("基差门控最长等待秒数必须为有限非负数")

    @staticmethod
    def _normalize_notional_bound(value: object, label: str) -> int:
        """把名义额边界归一化为整数美元，拒绝小数与非有限值。"""
        try:
            decimal_value = Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(f"{label}必须为整数美元") from exc
        if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
            raise ValueError(f"{label}必须为整数美元")
        return int(decimal_value)


@dataclass
class TimedVolumeState:
    """可持久化的轮次状态。"""

    round_index: int = 0
    last_direction: RoundDirection | None = None
    current_direction: RoundDirection | None = None
    opened_at: float | None = None
    due_at: float | None = None
    current_notional_usd: int | None = None
    ledger_primary_entry: Decimal | None = None
    ledger_hedge_entry: Decimal | None = None
    ledger_primary_size: Decimal | None = None
    ledger_hedge_size: Decimal | None = None
    basis_gate_wait_started_at: float | None = None

    @property
    def is_open(self) -> bool:
        """记录是否表示当前有一轮持仓。"""
        return self.current_direction is not None


@dataclass(frozen=True)
class TimedVolumeResult:
    """单次状态机推进结果，供入口写心跳与测试断言。"""

    action: str
    round_index: int
    direction: RoundDirection | None
    due_at: float | None
    primary_size: Decimal | None
    hedge_size: Decimal | None
    net_exposure: Decimal | None
    hedge_available: bool
    interlock_reason: str
    warnings: tuple[str, ...] = ()
    notional_usd: int | None = None
    primary_pnl: Decimal | None = None
    hedge_pnl: Decimal | None = None
    primary_entry: Decimal | None = None
    hedge_entry: Decimal | None = None
    pair_pnl: Decimal | None = None
    basis_gate_deviation: Decimal | None = None
    basis_gate_waited_seconds: float = 0.0
    basis_gate_state: str = "open"


TradeExecutor = Callable[..., Awaitable[HedgeFillResult]]
AvailabilityCheck = Callable[[], bool | Awaitable[bool]]
RandomInt = Callable[[int, int], int]
AuthReload = Callable[[], object | Awaitable[object]]


class _LegAuthFailure(Exception):
    """保留认证异常所属交易腿，避免把对冲腿误当成主腿重载。"""

    def __init__(self, leg: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.leg = leg
        self.cause = cause


@dataclass(frozen=True)
class _OrderLimits:
    """两侧市场的最小可交易数量。"""

    primary_minimum: Decimal
    hedge_minimum: Decimal


@dataclass(frozen=True)
class _RoundLedgerContext:
    """平仓前冻结的轮次成交记账上下文。"""

    round_index: int
    direction: RoundDirection | None
    notional_usd: int | None
    opened_at: float | None
    primary_entry: Decimal | None
    hedge_entry: Decimal | None
    primary_size: Decimal | None
    hedge_size: Decimal | None


@dataclass(frozen=True)
class _FillSummary:
    """单腿整轮手续费与平仓方向成交均价。"""

    exit_price: Decimal | None
    exit_size: Decimal
    fee: Decimal


class TimedHedgedVolumeStrategy:
    """用固定周期与逐轮金额驱动两侧在容差内反向开平仓。"""

    def __init__(
        self,
        primary,
        hedge,
        config: TimedVolumeConfig,
        *,
        trade_executor: TradeExecutor | None = None,
        hedge_available: AvailabilityCheck | None = None,
        random_int: RandomInt | None = None,
        auth_error_types: tuple[type[Exception], ...] = (),
        on_auth_error: AuthReload | None = None,
        on_hedge_auth_error: AuthReload | None = None,
    ) -> None:
        self.primary = primary
        self.hedge = hedge
        self.config = config
        self._trade_executor = trade_executor or maker_first_hedge
        self._availability_check = hedge_available
        self._random_int = random_int or random.randint
        self._auth_error_types = tuple(auth_error_types)
        self._on_auth_error = on_auth_error
        self._on_hedge_auth_error = on_hedge_auth_error
        self.state = self._load_state()
        self.hedge_interlock_active = False
        self.hedge_interlock_reason = "尚未判定"
        self._primary_auth_interlock_active = False
        self._hedge_auth_interlock_active = False
        self.hedge_tolerance: Decimal | None = None
        self._order_limits: _OrderLimits | None = None
        self._basis_gate_deviation: Decimal | None = None
        self._basis_gate_waited_seconds = 0.0
        self._basis_gate_state = "open"

    def _load_state(self) -> TimedVolumeState:
        """读取轮次记录；损坏记录失败关闭为无记录，后续以实仓恢复。"""
        path = self.config.state_path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return TimedVolumeState()
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("轮次状态读取失败，将以实际持仓恢复：%s", exc)
            return TimedVolumeState()
        if not isinstance(raw, dict):
            logger.warning("轮次状态不是 JSON 对象，将以实际持仓恢复")
            return TimedVolumeState()
        try:
            last_raw = raw.get("last_direction")
            current_raw = raw.get("current_direction")
            current_notional_raw = raw.get("current_notional_usd")
            current_notional = (
                TimedVolumeConfig._normalize_notional_bound(
                    current_notional_raw,
                    "当前轮名义额",
                )
                if current_notional_raw is not None
                else None
            )
            if current_notional is not None and current_notional <= 0:
                raise ValueError("当前轮名义额必须大于零")
            state = TimedVolumeState(
                round_index=max(0, int(raw.get("round_index", 0))),
                last_direction=RoundDirection(last_raw) if last_raw else None,
                current_direction=RoundDirection(current_raw) if current_raw else None,
                current_notional_usd=current_notional,
                opened_at=(
                    float(raw["opened_at"])
                    if raw.get("opened_at") is not None
                    else None
                ),
                due_at=(
                    float(raw["due_at"]) if raw.get("due_at") is not None else None
                ),
            )
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("轮次状态字段无效，将以实际持仓恢复：%s", exc)
            return TimedVolumeState()

        wait_started_raw = raw.get("basis_gate_wait_started_at")
        if wait_started_raw is not None:
            try:
                wait_started = float(wait_started_raw)
                if not math.isfinite(wait_started):
                    raise ValueError("等待起点必须为有限时间戳")
                state.basis_gate_wait_started_at = wait_started
            except (TypeError, ValueError) as exc:
                logger.warning("基差门控等待状态无效，仅清除等待计时：%s", exc)

        if self.config.ledger_path is None:
            return state
        try:
            for key in (
                "ledger_primary_entry",
                "ledger_hedge_entry",
                "ledger_primary_size",
                "ledger_hedge_size",
            ):
                value = raw.get(key)
                if value is None:
                    continue
                decimal_value = Decimal(str(value))
                if not decimal_value.is_finite():
                    raise ValueError(f"{key} 必须为有限十进制数")
                setattr(state, key, decimal_value)
        except (ArithmeticError, TypeError, ValueError) as exc:
            logger.warning("轮次台账元数据读取失败，仅跳过本轮记账：%s", exc)
            self._clear_ledger_state(state)
        return state

    def _save_state(self) -> None:
        """原子写入轮次记录，避免进程中断留下半截 JSON。"""
        path = self.config.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self.state)
        for key in ("last_direction", "current_direction"):
            value = payload[key]
            payload[key] = value.value if value is not None else None
        ledger_keys = (
            "ledger_primary_entry",
            "ledger_hedge_entry",
            "ledger_primary_size",
            "ledger_hedge_size",
        )
        if self.config.ledger_path is None:
            for key in ledger_keys:
                payload.pop(key, None)
        else:
            for key in ledger_keys:
                value = payload[key]
                payload[key] = str(value) if value is not None else None
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _clear_ledger_state(state: TimedVolumeState) -> None:
        """清空已结束轮次的附加记账元数据。"""
        state.ledger_primary_entry = None
        state.ledger_hedge_entry = None
        state.ledger_primary_size = None
        state.ledger_hedge_size = None

    def _is_within_hedge_tolerance(self, net_exposure: Decimal) -> bool:
        """按交易所最小可交易量推导的容差判断净敞口。"""
        if net_exposure == 0:
            return True
        return (
            self.hedge_tolerance is not None
            and abs(net_exposure) < self.hedge_tolerance
        )

    def _is_neutral_pair(self, primary_size: Decimal, hedge_size: Decimal) -> bool:
        """判断两腿是否非空、反向且净敞口在容差内。"""
        if primary_size == 0 or hedge_size == 0:
            return False
        if primary_size * hedge_size >= 0:
            return False
        return self._is_within_hedge_tolerance(primary_size + hedge_size)

    def _is_effectively_flat(self, size: Decimal, minimum: Decimal) -> bool:
        """判断单腿是否为零或低于该市场最小下单量。"""
        return size == 0 or abs(size) < minimum

    async def _read_positions(self) -> tuple[Decimal, Decimal]:
        """并发读取两侧实际持仓。"""
        primary_position, hedge_position = await asyncio.gather(
            self.primary.get_position(self.config.primary_market),
            self.hedge.get_position(self.config.hedge_market),
            return_exceptions=True,
        )
        for leg, result in (
            ("primary", primary_position),
            ("hedge", hedge_position),
        ):
            if isinstance(result, Exception) and self._is_auth_error(result):
                raise _LegAuthFailure(leg, result)
        for result in (primary_position, hedge_position):
            if isinstance(result, Exception):
                raise result
        return (
            Decimal(str(primary_position.signed_size)),
            Decimal(str(hedge_position.signed_size)),
        )

    async def _get_order_limits(
        self,
        warnings: list[str],
    ) -> _OrderLimits | None:
        """查询并缓存两侧最小下单量；未知时互锁而不猜测容差。"""
        if self._order_limits is not None:
            return self._order_limits

        primary_raw, hedge_raw = await asyncio.gather(
            self.primary.get_min_order_size(self.config.primary_market),
            self.hedge.get_min_order_size(self.config.hedge_market),
            return_exceptions=True,
        )
        try:
            if isinstance(primary_raw, Exception):
                if self._is_auth_error(primary_raw):
                    raise _LegAuthFailure("primary", primary_raw)
                raise RuntimeError(f"Lighter 最小下单量查询失败：{primary_raw}")
            if isinstance(hedge_raw, Exception):
                if self._is_auth_error(hedge_raw):
                    raise _LegAuthFailure("hedge", hedge_raw)
                raise RuntimeError(f"Extended 最小下单量查询失败：{hedge_raw}")
            primary_minimum = Decimal(str(primary_raw))
            hedge_minimum = Decimal(str(hedge_raw))
            if not primary_minimum.is_finite() or primary_minimum <= 0:
                raise RuntimeError(f"Lighter 最小下单量无效：{primary_minimum}")
            if not hedge_minimum.is_finite() or hedge_minimum <= 0:
                raise RuntimeError(f"Extended 最小下单量无效：{hedge_minimum}")
        except (ArithmeticError, RuntimeError, ValueError) as exc:
            self.hedge_tolerance = None
            self.hedge_interlock_active = True
            self.hedge_interlock_reason = f"对冲容差查询失败：{exc}"
            self._warn(warnings, self.hedge_interlock_reason)
            return None

        self._order_limits = _OrderLimits(
            primary_minimum=primary_minimum,
            hedge_minimum=hedge_minimum,
        )
        self.hedge_tolerance = max(
            self.config.position_tolerance,
            primary_minimum,
            hedge_minimum,
        )
        return self._order_limits

    async def _check_hedge_available(self, limits: _OrderLimits) -> bool:
        """刷新 Extended 互锁；默认同时核对交易能力、行情与市场元数据。"""
        if self._availability_check is None:
            try:
                if getattr(self.hedge, "supports_trading", True) is False:
                    raise RuntimeError("Extended 适配器不允许交易")
                required = (
                    "get_market_price",
                    "get_min_order_size",
                    "market_order",
                )
                if getattr(self.hedge, "execution_model", "orderbook") != "rfq":
                    required += (
                        "place_limit_order",
                        "get_order_by_id",
                        "cancel_order",
                    )
                missing = [
                    name for name in required if not callable(getattr(self.hedge, name, None))
                ]
                if missing:
                    raise RuntimeError("Extended 缺少交易能力：" + "、".join(missing))
                quote = await self.hedge.get_market_price(self.config.hedge_market)
                minimum = limits.hedge_minimum
                if quote is None:
                    raise RuntimeError("Extended 行情不可用")
                bid = Decimal(str(quote.bid))
                ask = Decimal(str(quote.ask))
                if bid <= 0 or ask <= bid:
                    raise RuntimeError("Extended 行情无效")
                if Decimal(str(minimum)) <= 0:
                    raise RuntimeError("Extended 最小下单量无效")
            except Exception as exc:  # noqa: BLE001 互锁检查必须失败关闭
                self.hedge_interlock_active = True
                self.hedge_interlock_reason = f"Extended 可用性检查失败：{exc}"
                return False
            self.hedge_interlock_active = False
            self.hedge_interlock_reason = "Extended 持仓、行情与交易能力正常"
            return True
        try:
            available = self._availability_check()
            if asyncio.iscoroutine(available):
                available = await available
            is_available = available is True
        except Exception as exc:  # noqa: BLE001 互锁探针异常必须失败关闭
            is_available = False
            self.hedge_interlock_reason = f"Extended 可用性检查失败：{exc}"
        else:
            self.hedge_interlock_reason = (
                "Extended 侧可用" if is_available else "Extended 侧不可用"
            )
        self.hedge_interlock_active = not is_available
        return is_available

    async def _execute(
        self,
        adapter,
        market: str,
        delta: Decimal,
        *,
        reduce_only: bool,
    ) -> HedgeFillResult:
        """统一调用 maker 优先执行器，超时由既有执行器转市价。"""
        return await self._trade_executor(
            adapter,
            market,
            delta,
            timeout_s=self.config.maker_timeout_s,
            poll_s=self.config.maker_poll_s,
            reduce_only=reduce_only,
        )

    async def _execute_pair(
        self,
        primary_delta: Decimal,
        hedge_delta: Decimal,
        *,
        reduce_only: bool,
    ) -> tuple[object, object]:
        """在同一个事件循环节拍并发提交两腿。"""
        return tuple(
            await asyncio.gather(
                self._execute(
                    self.primary,
                    self.config.primary_market,
                    primary_delta,
                    reduce_only=reduce_only,
                ),
                self._execute(
                    self.hedge,
                    self.config.hedge_market,
                    hedge_delta,
                    reduce_only=reduce_only,
                ),
                return_exceptions=True,
            )
        )

    async def _target_quantities(
        self,
        limits: _OrderLimits,
        notional_usd: int,
    ) -> tuple[Decimal, Decimal]:
        """按该轮金额和 Lighter 中间价计算两侧各自的舍入数量。"""
        try:
            quote = await self.primary.get_market_price(self.config.primary_market)
        except Exception as exc:
            if self._is_auth_error(exc):
                raise _LegAuthFailure("primary", exc) from exc
            raise
        if quote is None or Decimal(str(quote.mid)) <= 0:
            raise RuntimeError("Lighter 盘口不可用，无法计算该轮名义额数量")
        raw = Decimal(notional_usd) / Decimal(str(quote.mid))
        primary_qty, hedge_qty = await asyncio.gather(
            self.primary.round_amount(self.config.primary_market, raw),
            self.hedge.round_amount(self.config.hedge_market, raw),
            return_exceptions=True,
        )
        for leg, result in (("primary", primary_qty), ("hedge", hedge_qty)):
            if isinstance(result, Exception) and self._is_auth_error(result):
                raise _LegAuthFailure(leg, result)
        for result in (primary_qty, hedge_qty):
            if isinstance(result, Exception):
                raise result
        primary_qty = Decimal(str(primary_qty))
        hedge_qty = Decimal(str(hedge_qty))
        if not primary_qty.is_finite() or primary_qty < limits.primary_minimum:
            raise RuntimeError("配置名义额换算后的 Lighter 数量低于最小下单量")
        if not hedge_qty.is_finite() or hedge_qty < limits.hedge_minimum:
            raise RuntimeError("配置名义额换算后的 Extended 数量低于最小下单量")
        return primary_qty, hedge_qty

    def _current_or_sampled_notional(self) -> int:
        """返回已持久化轮次金额；新轮次仅取值一次并立即落盘。"""
        if self.state.current_notional_usd is not None:
            return self.state.current_notional_usd
        sampled = self._random_int(
            self.config.notional_min_usd,
            self.config.notional_max_usd,
        )
        normalized = TimedVolumeConfig._normalize_notional_bound(
            sampled,
            "随机名义额",
        )
        if not self.config.notional_min_usd <= normalized <= self.config.notional_max_usd:
            raise ValueError("随机名义额必须落在配置闭区间内")
        self.state.current_notional_usd = normalized
        self._save_state()
        return normalized

    def _warn(self, warnings: list[str], message: str) -> None:
        """记录中文告警并同步写入本轮结果。"""
        warnings.append(message)
        logger.warning(message)

    def _is_auth_error(self, exc: Exception) -> bool:
        """仅把显式注册的异常类型识别为认证失效。"""
        return bool(self._auth_error_types) and isinstance(
            exc,
            self._auth_error_types,
        )

    async def _try_reload_leg(self, leg: str, warnings: list[str]) -> bool:
        """重建并实读验证指定交易腿；失败时保持认证互锁。"""
        is_primary = leg == "primary"
        callback = self._on_auth_error if is_primary else self._on_hedge_auth_error
        market = self.config.primary_market if is_primary else self.config.hedge_market
        label = "主腿" if is_primary else "对冲腿"
        try:
            if callback is None:
                raise RuntimeError(f"未配置{label}认证重载回调")
            replacement = callback()
            if asyncio.iscoroutine(replacement):
                replacement = await replacement
            if replacement is None:
                raise RuntimeError(f"{label}认证重载回调未返回客户端")
            await replacement.get_position(market)
        except Exception as exc:  # noqa: BLE001 认证恢复必须失败关闭
            if is_primary:
                self._primary_auth_interlock_active = True
            else:
                self._hedge_auth_interlock_active = True
            self.hedge_interlock_active = True
            self.hedge_interlock_reason = f"{label}认证重载失败，停止开新仓：{exc}"
            self._warn(warnings, self.hedge_interlock_reason)
            return False

        if is_primary:
            self.primary = replacement
            self._primary_auth_interlock_active = False
        else:
            self.hedge = replacement
            self._hedge_auth_interlock_active = False
        self.hedge_interlock_active = False
        self.hedge_interlock_reason = f"{label}认证已重载，本轮跳过"
        self._warn(warnings, self.hedge_interlock_reason)
        return True

    async def _try_reload_primary(self, warnings: list[str]) -> bool:
        """重建并实读验证主腿。"""
        return await self._try_reload_leg("primary", warnings)

    async def _try_reload_hedge(self, warnings: list[str]) -> bool:
        """重建并实读验证对冲腿。"""
        return await self._try_reload_leg("hedge", warnings)

    async def _restore_leg_position(
        self,
        adapter,
        market: str,
        position_before: Decimal,
        minimum: Decimal,
        warnings: list[str],
        label: str,
    ) -> Decimal:
        """认证失败时把已成交的另一腿恢复到提交前实仓。"""
        current = Decimal(str((await adapter.get_position(market)).signed_size))
        for _ in range(self.config.convergence_attempts):
            delta = position_before - current
            if delta == 0 or abs(delta) < minimum:
                return current
            reduce_only = position_before == 0 or abs(position_before) < abs(current)
            try:
                await self._execute(
                    adapter,
                    market,
                    delta,
                    reduce_only=reduce_only,
                )
            except Exception as exc:  # noqa: BLE001 回滚按实仓重试
                self._warn(warnings, f"{label} 认证事故回滚失败，将按实仓重试：{exc}")
            current = Decimal(str((await adapter.get_position(market)).signed_size))
        if abs(position_before - current) >= minimum:
            self._warn(
                warnings,
                f"{label} 认证事故回滚多次后仍未恢复提交前实仓",
            )
        return current

    async def _flatten_all(
        self,
        warnings: list[str],
        limits: _OrderLimits,
    ) -> tuple[Decimal, Decimal]:
        """按每次重读到的实仓反复平仓；失败时优先恢复中性而非遗留裸仓。"""
        primary_size, hedge_size = await self._read_positions()
        primary_dust_warned = False
        hedge_dust_warned = False
        for _ in range(self.config.convergence_attempts):
            primary_flat = self._is_effectively_flat(
                primary_size,
                limits.primary_minimum,
            )
            hedge_flat = self._is_effectively_flat(
                hedge_size,
                limits.hedge_minimum,
            )
            if primary_flat and primary_size != 0 and not primary_dust_warned:
                self._warn(
                    warnings,
                    "Lighter 平仓残余低于最小下单量，明确放弃不可交易残差："
                    f"残余={primary_size}，最小下单量={limits.primary_minimum}",
                )
                primary_dust_warned = True
            if hedge_flat and hedge_size != 0 and not hedge_dust_warned:
                self._warn(
                    warnings,
                    "Extended 平仓残余低于最小下单量，明确放弃不可交易残差："
                    f"残余={hedge_size}，最小下单量={limits.hedge_minimum}",
                )
                hedge_dust_warned = True
            if primary_flat and hedge_flat:
                return primary_size, hedge_size
            calls: list[tuple[str, Awaitable[HedgeFillResult]]] = []
            if not primary_flat:
                calls.append(
                    (
                        "primary",
                        self._execute(
                            self.primary,
                            self.config.primary_market,
                            -primary_size,
                            reduce_only=True,
                        ),
                    )
                )
            if not hedge_flat:
                calls.append(
                    (
                        "hedge",
                        self._execute(
                            self.hedge,
                            self.config.hedge_market,
                            -hedge_size,
                            reduce_only=True,
                        ),
                    )
                )
            results = await asyncio.gather(
                *(call for _, call in calls),
                return_exceptions=True,
            )
            labelled_results = list(zip((label for label, _ in calls), results))
            auth_failure_legs = [
                label
                for label, result in labelled_results
                if isinstance(result, Exception) and self._is_auth_error(result)
            ]
            for _, result in labelled_results:
                if isinstance(result, Exception):
                    self._warn(warnings, f"平仓执行失败，将按实际持仓重试：{result}")
            for failed_leg in auth_failure_legs:
                reloaded = await self._try_reload_leg(failed_leg, warnings)
                if not reloaded:
                    other_leg = "hedge" if failed_leg == "primary" else "primary"
                    other_succeeded = any(
                        label == other_leg and not isinstance(result, Exception)
                        for label, result in labelled_results
                    )
                    if other_succeeded and other_leg == "hedge":
                        hedge_size = await self._restore_leg_position(
                            self.hedge,
                            self.config.hedge_market,
                            hedge_size,
                            limits.hedge_minimum,
                            warnings,
                            "对冲腿",
                        )
                    elif other_succeeded:
                        primary_size = await self._restore_leg_position(
                            self.primary,
                            self.config.primary_market,
                            primary_size,
                            limits.primary_minimum,
                            warnings,
                            "主腿",
                        )
                    return primary_size, hedge_size
            primary_size, hedge_size = await self._read_positions()

        if self._is_neutral_pair(primary_size, hedge_size):
            return primary_size, hedge_size

        self._warn(warnings, "多次平仓后仍有净敞口，尝试在可用腿恢复容差内反向仓")
        net = primary_size + hedge_size
        if not self._is_within_hedge_tolerance(net):
            if abs(primary_size) <= abs(hedge_size):
                adapter = self.primary
                market = self.config.primary_market
                minimum = limits.primary_minimum
                label = "Lighter"
            else:
                adapter = self.hedge
                market = self.config.hedge_market
                minimum = limits.hedge_minimum
                label = "Extended"
            if abs(net) < minimum:
                self._warn(
                    warnings,
                    f"恢复中性所需数量 {abs(net)} 低于 {label} 最小下单量 "
                    f"{minimum}，明确放弃不可交易补单",
                )
            else:
                recovery = self._execute(
                    adapter,
                    market,
                    -net,
                    reduce_only=False,
                )
                recovered = await asyncio.gather(recovery, return_exceptions=True)
                if isinstance(recovered[0], Exception):
                    self._warn(warnings, f"恢复中性仓位失败：{recovered[0]}")
        return await self._read_positions()

    async def _reconcile_actual_state(
        self,
        now: float,
        primary_size: Decimal,
        hedge_size: Decimal,
        warnings: list[str],
        limits: _OrderLimits,
    ) -> tuple[Decimal, Decimal, bool]:
        """以实际持仓校正记录；返回的布尔值表示本轮执行过风险收敛交易。"""
        actual_flat = self._is_effectively_flat(
            primary_size,
            limits.primary_minimum,
        ) and self._is_effectively_flat(
            hedge_size,
            limits.hedge_minimum,
        )
        if actual_flat:
            if self.state.is_open:
                self._warn(
                    warnings,
                    "持久化状态与实际持仓不一致：记录有仓但实际已归零或仅剩不可交易残余",
                )
                self.state.last_direction = self.state.current_direction
                self.state.current_direction = None
                self.state.current_notional_usd = None
                self.state.opened_at = None
                self.state.due_at = None
                self._clear_ledger_state(self.state)
                self._save_state()
            return primary_size, hedge_size, False

        if self._is_neutral_pair(primary_size, hedge_size):
            actual_direction = (
                RoundDirection.LONG if primary_size > 0 else RoundDirection.SHORT
            )
            record_matches = (
                self.state.current_direction is actual_direction
                and self.state.opened_at is not None
                and self.state.due_at is not None
            )
            if not record_matches:
                self._warn(
                    warnings,
                    "持久化状态与实际持仓不一致：已按两侧实际持仓恢复当前轮次",
                )
                self.state.round_index = max(1, self.state.round_index)
                self.state.current_direction = actual_direction
                self.state.opened_at = now
                self.state.due_at = now + self.config.cycle_seconds
                self._clear_ledger_state(self.state)
                self._save_state()
            return primary_size, hedge_size, False

        self._warn(
            warnings,
            "持久化状态与实际持仓不一致：检测到非中性实仓，立即按实仓收敛",
        )
        if (
            primary_size != 0
            and hedge_size != 0
            and primary_size * hedge_size < 0
        ):
            net = primary_size + hedge_size
            if not self._is_within_hedge_tolerance(net):
                if abs(primary_size) < abs(hedge_size):
                    adapter = self.primary
                    market = self.config.primary_market
                    minimum = limits.primary_minimum
                    label = "Lighter"
                else:
                    adapter = self.hedge
                    market = self.config.hedge_market
                    minimum = limits.hedge_minimum
                    label = "Extended"
                if abs(net) < minimum:
                    self._warn(
                        warnings,
                        f"按实仓差补齐所需数量 {abs(net)} 低于 {label} "
                        f"最小下单量 {minimum}，明确放弃不可交易补单",
                    )
                    result = ()
                else:
                    result = await asyncio.gather(
                        self._execute(
                            adapter,
                            market,
                            -net,
                            reduce_only=False,
                        ),
                        return_exceptions=True,
                    )
                if any(isinstance(item, Exception) for item in result):
                    self._warn(warnings, "按实际持仓差补齐失败，转为回滚两腿")
                primary_size, hedge_size = await self._read_positions()
                if self._is_neutral_pair(primary_size, hedge_size):
                    actual_direction = (
                        RoundDirection.LONG
                        if primary_size > 0
                        else RoundDirection.SHORT
                    )
                    self.state.round_index = max(1, self.state.round_index)
                    record_keeps_schedule = (
                        self.state.current_direction is actual_direction
                        and self.state.opened_at is not None
                        and self.state.due_at is not None
                    )
                    self.state.current_direction = actual_direction
                    if not record_keeps_schedule:
                        self.state.opened_at = now
                        self.state.due_at = now + self.config.cycle_seconds
                        self._clear_ledger_state(self.state)
                    self._save_state()
                    return primary_size, hedge_size, True

        primary_size, hedge_size = await self._flatten_all(warnings, limits)
        if self._is_effectively_flat(
            primary_size,
            limits.primary_minimum,
        ) and self._is_effectively_flat(
            hedge_size,
            limits.hedge_minimum,
        ):
            self.state.last_direction = self.state.current_direction or self.state.last_direction
            self.state.current_direction = None
            self.state.current_notional_usd = None
            self.state.opened_at = None
            self.state.due_at = None
            self._clear_ledger_state(self.state)
            self._save_state()
        return primary_size, hedge_size, True

    @staticmethod
    def _basis_from_heartbeat(payload: dict) -> Decimal | None:
        """从开仓心跳提取实际入场基差，兼容显式基差与双腿入场价。"""
        if payload.get("action") != "opened":
            return None
        explicit = payload.get("entry_basis_pct")
        if explicit is not None:
            basis = Decimal(str(explicit))
        else:
            primary_entry = payload.get("primary_entry")
            hedge_entry = payload.get("hedge_entry")
            if primary_entry is None or hedge_entry is None:
                return None
            primary = Decimal(str(primary_entry))
            hedge = Decimal(str(hedge_entry))
            if not primary.is_finite() or not hedge.is_finite() or hedge <= 0:
                return None
            basis = (primary - hedge) / hedge * Decimal(100)
        return basis if basis.is_finite() else None

    def _read_basis_values(self, path: Path, *, ledger: bool) -> list[Decimal]:
        """读取单个 JSONL 来源中的有效入场基差，损坏行只做隔离跳过。"""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            logger.warning("基差门控历史读取失败，将按样本不足放行：%s", exc)
            return []

        values: list[Decimal] = []
        for line in lines:
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    continue
                if ledger:
                    row_instance = payload.get("instance")
                    if (
                        self.config.instance is not None
                        and row_instance is not None
                        and str(row_instance) != self.config.instance
                    ):
                        continue
                    raw_basis = payload.get("entry_basis_pct")
                    if raw_basis is None:
                        continue
                    basis = Decimal(str(raw_basis))
                    if not basis.is_finite():
                        continue
                else:
                    basis = self._basis_from_heartbeat(payload)
                    if basis is None:
                        continue
            except (ArithmeticError, TypeError, ValueError, json.JSONDecodeError):
                continue
            values.append(basis)
        return values[-BASIS_GATE_HISTORY_ROUNDS:]

    def _read_basis_history(self) -> list[Decimal]:
        """从台账或心跳选择样本更完整的最近开仓基差序列。"""
        ledger_values = (
            self._read_basis_values(self.config.ledger_path, ledger=True)
            if self.config.ledger_path is not None
            else []
        )
        heartbeat_values = (
            self._read_basis_values(self.config.heartbeat_path, ledger=False)
            if self.config.heartbeat_path is not None
            else []
        )
        return (
            heartbeat_values
            if len(heartbeat_values) > len(ledger_values)
            else ledger_values
        )

    @staticmethod
    def _basis_statistics(values: list[Decimal]) -> tuple[Decimal, Decimal]:
        """用 Decimal 计算历史中位数与总体标准差，避免浮点阈值漂移。"""
        ordered = sorted(values)
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / Decimal(2)
        )
        count = Decimal(len(values))
        mean = sum(values, Decimal(0)) / count
        variance = sum(
            ((value - mean) ** 2 for value in values),
            Decimal(0),
        ) / count
        return median, variance.sqrt()

    @staticmethod
    def _basis_mid(quote: object, role: str) -> Decimal:
        """从盘口提取用于开仓门控的有限正中价。"""
        mid = Decimal(str(getattr(quote, "mid")))
        if not mid.is_finite() or mid <= 0:
            raise ValueError(f"{role}开仓中价必须为有限正数")
        return mid

    async def _current_basis(self) -> Decimal:
        """并发读取两腿中价并计算百分比基差。"""
        quotes = await asyncio.gather(
            self.primary.get_market_price(self.config.primary_market),
            self.hedge.get_market_price(self.config.hedge_market),
            return_exceptions=True,
        )
        for leg, quote in zip(("primary", "hedge"), quotes, strict=True):
            if isinstance(quote, Exception) and self._is_auth_error(quote):
                raise _LegAuthFailure(leg, quote)
        for quote in quotes:
            if isinstance(quote, Exception):
                raise quote
        primary_mid = self._basis_mid(quotes[0], "主腿")
        hedge_mid = self._basis_mid(quotes[1], "对冲腿")
        return (primary_mid - hedge_mid) / hedge_mid * Decimal(100)

    def _clear_basis_gate_wait(self) -> None:
        """清除已结束的门控等待；没有等待时不产生额外状态写盘。"""
        if self.state.basis_gate_wait_started_at is None:
            return
        self.state.basis_gate_wait_started_at = None
        self._save_state()

    async def _basis_gate_allows_open(self, now: float) -> bool:
        """评估开仓门控并更新本节拍观测字段。"""
        if self.config.basis_gate_sigma == 0:
            self._clear_basis_gate_wait()
            return True

        history = self._read_basis_history()
        if len(history) < BASIS_GATE_MIN_HISTORY:
            self._clear_basis_gate_wait()
            return True

        current_basis = await self._current_basis()
        median, standard_deviation = self._basis_statistics(history)
        deviation = current_basis - median
        self._basis_gate_deviation = deviation

        wait_started = self.state.basis_gate_wait_started_at
        if wait_started is not None:
            self._basis_gate_waited_seconds = max(0.0, now - wait_started)

        if standard_deviation < BASIS_GATE_STD_FLOOR_PCT:
            self._clear_basis_gate_wait()
            return True

        threshold = self.config.basis_gate_sigma * standard_deviation
        if abs(deviation) <= threshold:
            self._clear_basis_gate_wait()
            return True

        if wait_started is None or now < wait_started:
            self.state.basis_gate_wait_started_at = now
            self._basis_gate_waited_seconds = 0.0
            self._save_state()
        if self._basis_gate_waited_seconds >= self.config.basis_gate_max_wait_s:
            self._basis_gate_state = "forced"
            logger.warning(
                "基差门控等待达到上限，强制开仓：偏离=%s%%，已等待=%.1f秒",
                deviation,
                self._basis_gate_waited_seconds,
            )
            return True

        self._basis_gate_state = "waiting"
        logger.info(
            "基差相对偏离超过门限，延迟开仓：偏离=%s%%，门限=%s%%，已等待=%.1f秒",
            deviation,
            threshold,
            self._basis_gate_waited_seconds,
        )
        return False

    async def _open_round(
        self,
        now: float,
        warnings: list[str],
        limits: _OrderLimits,
    ) -> TimedVolumeResult:
        """同步建立两腿，并按实仓差补齐；无法完成时回滚到零。"""
        direction = (
            self.config.initial_direction
            if self.state.last_direction is None
            else self.state.last_direction.opposite()
        )
        notional_usd = self._current_or_sampled_notional()
        primary_quantity, hedge_quantity = await self._target_quantities(
            limits,
            notional_usd,
        )
        primary_target = direction.sign * primary_quantity
        hedge_target = -direction.sign * hedge_quantity
        primary_before, hedge_before = await self._read_positions()
        outcomes = await self._execute_pair(
            primary_target - primary_before,
            hedge_target - hedge_before,
            reduce_only=False,
        )
        auth_failure_legs = [
            leg
            for leg, outcome in zip(("primary", "hedge"), outcomes)
            if isinstance(outcome, Exception) and self._is_auth_error(outcome)
        ]
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                self._warn(warnings, f"同步开仓执行异常：{outcome}")

        for failed_leg in auth_failure_legs:
            reloaded = await self._try_reload_leg(failed_leg, warnings)
            if not reloaded:
                if failed_leg == "primary":
                    primary_size = primary_before
                    hedge_size = hedge_before
                    if not isinstance(outcomes[1], Exception):
                        hedge_size = await self._restore_leg_position(
                            self.hedge,
                            self.config.hedge_market,
                            hedge_before,
                            limits.hedge_minimum,
                            warnings,
                            "对冲腿",
                        )
                else:
                    primary_size = primary_before
                    hedge_size = hedge_before
                    if not isinstance(outcomes[0], Exception):
                        primary_size = await self._restore_leg_position(
                            self.primary,
                            self.config.primary_market,
                            primary_before,
                            limits.primary_minimum,
                            warnings,
                            "主腿",
                        )
                flat = self._is_effectively_flat(
                    primary_size,
                    limits.primary_minimum,
                ) and self._is_effectively_flat(
                    hedge_size,
                    limits.hedge_minimum,
                )
                if flat:
                    self.state.current_notional_usd = None
                    self._save_state()
                return self._result(
                    "auth_reload_failed" if flat else "convergence_failed",
                    primary_size,
                    hedge_size,
                    warnings,
                )

        primary_size, hedge_size = await self._read_positions()
        primary_flat = self._is_effectively_flat(
            primary_size,
            limits.primary_minimum,
        )
        hedge_flat = self._is_effectively_flat(
            hedge_size,
            limits.hedge_minimum,
        )
        primary_only = not primary_flat and hedge_flat
        hedge_only = primary_flat and not hedge_flat
        if primary_only:
            self._warn(warnings, "Extended 对冲未建立，立即回滚 Lighter 实际持仓")
        elif hedge_only:
            self._warn(warnings, "Lighter 开仓未成交，立即平掉 Extended 实际持仓")

        if (
            not primary_only
            and not hedge_only
            and primary_size * hedge_size < 0
        ):
            net = primary_size + hedge_size
            if net != 0:
                if abs(primary_size) < abs(hedge_size):
                    adapter = self.primary
                    market = self.config.primary_market
                    minimum = limits.primary_minimum
                    label = "Lighter"
                else:
                    adapter = self.hedge
                    market = self.config.hedge_market
                    minimum = limits.hedge_minimum
                    label = "Extended"
                if self._is_within_hedge_tolerance(net):
                    if abs(net) < minimum:
                        self._warn(
                            warnings,
                            f"补齐所需数量 {abs(net)} 低于 {label} 最小下单量 "
                            f"{minimum}，净敞口已在容差内，不提交不可交易补单",
                        )
                elif abs(net) < minimum:
                    self._warn(
                        warnings,
                        f"补齐所需数量 {abs(net)} 低于 {label} 最小下单量 "
                        f"{minimum}，明确放弃不可交易补单",
                    )
                else:
                    supplements = await asyncio.gather(
                        self._execute(
                            adapter,
                            market,
                            -net,
                            reduce_only=False,
                        ),
                        return_exceptions=True,
                    )
                    if any(isinstance(item, Exception) for item in supplements):
                        self._warn(warnings, "部分成交按实际持仓差补齐失败，立即回滚")
            primary_size, hedge_size = await self._read_positions()

        targets_reached = (
            primary_size * direction.sign > 0
            and hedge_size * direction.sign < 0
            and self._is_neutral_pair(primary_size, hedge_size)
        )
        if not targets_reached:
            primary_size, hedge_size = await self._flatten_all(warnings, limits)
            flat = self._is_effectively_flat(
                primary_size,
                limits.primary_minimum,
            ) and self._is_effectively_flat(
                hedge_size,
                limits.hedge_minimum,
            )
            if not flat:
                self._warn(warnings, "开仓失败后的回滚未能归零，保持中性并等待重试")
            else:
                self.state.current_notional_usd = None
                self._save_state()
            return self._result(
                (
                    "auth_reloaded"
                    if flat and auth_failure_legs
                    else "open_failed_flat"
                    if flat
                    else "convergence_failed"
                ),
                primary_size,
                hedge_size,
                warnings,
            )

        self.state.round_index += 1
        self.state.current_direction = direction
        self.state.opened_at = now
        self.state.due_at = now + self.config.cycle_seconds
        self.state.basis_gate_wait_started_at = None
        self._save_state()
        return self._result("opened", primary_size, hedge_size, warnings)

    async def _close_round(
        self,
        warnings: list[str],
        limits: _OrderLimits,
    ) -> TimedVolumeResult:
        """按两侧实仓同步平仓；只剩不可交易残余时也确定性结束本轮。"""
        primary_size, hedge_size = await self._flatten_all(warnings, limits)
        flat = self._is_effectively_flat(
            primary_size,
            limits.primary_minimum,
        ) and self._is_effectively_flat(
            hedge_size,
            limits.hedge_minimum,
        )
        if flat:
            self.state.last_direction = self.state.current_direction
            self.state.current_direction = None
            self.state.current_notional_usd = None
            self.state.opened_at = None
            self.state.due_at = None
            self.state.basis_gate_wait_started_at = None
            self._clear_ledger_state(self.state)
            self._save_state()
            return self._result("closed", primary_size, hedge_size, warnings)
        self._warn(warnings, "到期平仓未能两侧归零，保留轮次并在下一次继续收敛")
        return self._result(
            "close_failed_neutral",
            primary_size,
            hedge_size,
            warnings,
        )

    def _result(
        self,
        action: str,
        primary_size: Decimal | None,
        hedge_size: Decimal | None,
        warnings: list[str],
    ) -> TimedVolumeResult:
        """用当前状态构造不可变结果快照。"""
        net = (
            primary_size + hedge_size
            if primary_size is not None and hedge_size is not None
            else None
        )
        return TimedVolumeResult(
            action=action,
            round_index=self.state.round_index,
            direction=self.state.current_direction,
            due_at=self.state.due_at,
            primary_size=primary_size,
            hedge_size=hedge_size,
            net_exposure=net,
            hedge_available=not self.hedge_interlock_active,
            interlock_reason=self.hedge_interlock_reason,
            notional_usd=self.state.current_notional_usd,
            warnings=tuple(warnings),
            basis_gate_deviation=self._basis_gate_deviation,
            basis_gate_waited_seconds=self._basis_gate_waited_seconds,
            basis_gate_state=self._basis_gate_state,
        )

    async def _run_trading_once(self, *, now: float | None = None) -> TimedVolumeResult:
        """推进一次状态机；到期平仓与下一轮开仓分属相邻的无等待节拍。"""
        current_time = time.time() if now is None else float(now)
        self._basis_gate_deviation = None
        self._basis_gate_waited_seconds = 0.0
        self._basis_gate_state = "open"
        warnings: list[str] = []
        blocked_leg = (
            "primary"
            if self._primary_auth_interlock_active
            else "hedge"
            if self._hedge_auth_interlock_active
            else None
        )
        if blocked_leg is not None:
            reloaded = await self._try_reload_leg(blocked_leg, warnings)
            return self._result(
                "auth_reloaded" if reloaded else "auth_reload_failed",
                None,
                None,
                warnings,
            )
        try:
            primary_size, hedge_size = await self._read_positions()
        except _LegAuthFailure as exc:
            reloaded = await self._try_reload_leg(exc.leg, warnings)
            return self._result(
                "auth_reloaded" if reloaded else "auth_reload_failed",
                None,
                None,
                warnings,
            )
        except Exception as exc:  # noqa: BLE001 持仓事实不完整时禁止任何交易
            self.hedge_interlock_active = True
            self.hedge_interlock_reason = f"两侧实际持仓读取失败：{exc}"
            self._warn(warnings, self.hedge_interlock_reason)
            return self._result("position_read_failed", None, None, warnings)

        try:
            limits = await self._get_order_limits(warnings)
            if limits is None:
                return self._result(
                    "interlocked",
                    primary_size,
                    hedge_size,
                    warnings,
                )
            hedge_available = await self._check_hedge_available(limits)
            primary_size, hedge_size, converged = await self._reconcile_actual_state(
                current_time,
                primary_size,
                hedge_size,
                warnings,
                limits,
            )
            if converged:
                return self._result(
                    "reconciled",
                    primary_size,
                    hedge_size,
                    warnings,
                )

            if self.state.is_open:
                due_at = self.state.due_at
                if due_at is None or current_time >= due_at:
                    return await self._close_round(warnings, limits)
                return self._result("wait", primary_size, hedge_size, warnings)

            if not hedge_available:
                self._warn(warnings, "Extended 侧不可用，对冲互锁跳过新开仓")
                return self._result("interlocked", primary_size, hedge_size, warnings)
            if not await self._basis_gate_allows_open(current_time):
                return self._result(
                    "basis_waiting",
                    primary_size,
                    hedge_size,
                    warnings,
                )
            return await self._open_round(current_time, warnings, limits)
        except _LegAuthFailure as exc:
            reloaded = await self._try_reload_leg(exc.leg, warnings)
            return self._result(
                "auth_reloaded" if reloaded else "auth_reload_failed",
                primary_size,
                hedge_size,
                warnings,
            )
        except Exception as exc:  # noqa: BLE001 成交后事实未知时必须保持循环存活
            self.hedge_interlock_active = True
            self.hedge_interlock_reason = f"策略执行后持仓事实未知：{exc}"
            self._warn(warnings, self.hedge_interlock_reason)
            return self._result("execution_uncertain", None, None, warnings)

    @staticmethod
    async def _read_display_pnl(adapter, market: str) -> PositionPnl | None:
        """读取单腿展示快照；旧测试桩和旧适配器可没有这项能力。"""
        reader = getattr(adapter, "get_position_pnl", None)
        if reader is None:
            return None
        snapshot = await reader(market)
        return snapshot if isinstance(snapshot, PositionPnl) else None

    async def _attach_display_pnl(
        self,
        result: TimedVolumeResult,
    ) -> TimedVolumeResult:
        """附加两腿盈亏；任何失败都只降级展示，不改变策略结果。"""
        snapshots = await asyncio.gather(
            self._read_display_pnl(self.primary, self.config.primary_market),
            self._read_display_pnl(self.hedge, self.config.hedge_market),
            return_exceptions=True,
        )

        normalized: list[PositionPnl | None] = []
        for role, snapshot in zip(("主腿", "对冲腿"), snapshots, strict=True):
            if isinstance(snapshot, BaseException):
                logger.warning("%s盈亏读取失败，仅影响面板展示：%s", role, snapshot)
                normalized.append(None)
            else:
                normalized.append(snapshot)

        primary, hedge = normalized
        primary_pnl = primary.unrealized_pnl if primary is not None else None
        hedge_pnl = hedge.unrealized_pnl if hedge is not None else None
        pair_pnl = (
            primary_pnl + hedge_pnl
            if primary_pnl is not None and hedge_pnl is not None
            else None
        )
        return replace(
            result,
            primary_pnl=primary_pnl,
            hedge_pnl=hedge_pnl,
            primary_entry=primary.entry_price if primary is not None else None,
            hedge_entry=hedge.entry_price if hedge is not None else None,
            pair_pnl=pair_pnl,
        )

    @staticmethod
    def _balance_equity(balance: object, role: str) -> Decimal:
        """从适配器余额对象提取有限 Decimal 权益。"""
        raw_equity = getattr(balance, "equity", None)
        if raw_equity is None:
            raise ValueError(f"{role}余额响应缺少 equity")
        try:
            equity = Decimal(str(raw_equity))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(f"{role}权益不是有效十进制数") from exc
        if not equity.is_finite():
            raise ValueError(f"{role}权益必须为有限数")
        return equity

    async def _record_equity_snapshot(
        self,
        result: TimedVolumeResult,
        *,
        now: float | None,
    ) -> None:
        """仅在平仓后两腿精确归零时追加展示用权益快照。"""
        path = self.config.equity_path
        if (
            path is None
            or result.action != "closed"
            or result.primary_size != 0
            or result.hedge_size != 0
        ):
            return

        primary_balance, hedge_balance = await asyncio.gather(
            self.primary.get_balance(),
            self.hedge.get_balance(),
        )
        primary_equity = self._balance_equity(primary_balance, "主腿")
        hedge_equity = self._balance_equity(hedge_balance, "对冲腿")
        payload = {
            "ts": time.time() if now is None else float(now),
            "round_index": result.round_index,
            "primary_equity": str(primary_equity),
            "hedge_equity": str(hedge_equity),
            "total_equity": str(primary_equity + hedge_equity),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _round_ledger_context(self) -> _RoundLedgerContext:
        """冻结当前轮次的台账字段，供交易完成后的隔离记账使用。"""
        return _RoundLedgerContext(
            round_index=self.state.round_index,
            direction=self.state.current_direction,
            notional_usd=self.state.current_notional_usd,
            opened_at=self.state.opened_at,
            primary_entry=self.state.ledger_primary_entry,
            hedge_entry=self.state.ledger_hedge_entry,
            primary_size=self.state.ledger_primary_size,
            hedge_size=self.state.ledger_hedge_size,
        )

    def _capture_round_ledger_entry(self, result: TimedVolumeResult) -> None:
        """从持仓展示快照保存精确入场均价；失败只影响本轮记账。"""
        if (
            self.config.ledger_path is None
            or not self.state.is_open
            or result.primary_entry is None
            or result.hedge_entry is None
            or result.primary_size is None
            or result.hedge_size is None
        ):
            return
        if all(
            value is not None
            for value in (
                self.state.ledger_primary_entry,
                self.state.ledger_hedge_entry,
                self.state.ledger_primary_size,
                self.state.ledger_hedge_size,
            )
        ):
            return
        values = (
            result.primary_entry,
            result.hedge_entry,
            result.primary_size,
            result.hedge_size,
        )
        if not all(value.is_finite() for value in values):
            raise ValueError("轮次台账入场字段必须为有限十进制数")
        if result.primary_entry <= 0 or result.hedge_entry <= 0:
            raise ValueError("轮次台账入场均价必须大于零")
        if result.primary_size == 0 or result.hedge_size == 0:
            raise ValueError("轮次台账开仓数量不得为零")
        self.state.ledger_primary_entry = result.primary_entry
        self.state.ledger_hedge_entry = result.hedge_entry
        self.state.ledger_primary_size = result.primary_size
        self.state.ledger_hedge_size = result.hedge_size
        self._save_state()

    @staticmethod
    def _fill_decimal(fill: object, key: str) -> Decimal:
        """从成交对象提取有限 Decimal 字段。"""
        value = fill.get(key) if isinstance(fill, dict) else getattr(fill, key, None)
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise ValueError(f"成交字段 {key} 必须为有限十进制数")
        return decimal_value

    async def _read_fill_summary(
        self,
        adapter,
        market: str,
        *,
        opened_at: float,
        closed_at: float,
        opening_size: Decimal,
    ) -> _FillSummary | None:
        """读取整轮成交，并按平仓方向计算数量加权均价。"""
        reader = getattr(adapter, "get_fills_by_time", None)
        if not callable(reader):
            return None
        fills = await reader(market, opened_at, closed_at)
        if not isinstance(fills, list):
            raise ValueError("成交时间窗响应必须为数组")
        fee = Decimal(0)
        exit_quantity = Decimal(0)
        exit_quote = Decimal(0)
        for fill in fills:
            price = self._fill_decimal(fill, "price")
            signed_size = self._fill_decimal(fill, "signed_size")
            fill_fee = self._fill_decimal(fill, "fee")
            if price <= 0 or signed_size == 0:
                raise ValueError("成交价格必须大于零且成交数量不得为零")
            fee += fill_fee
            if signed_size * opening_size < 0:
                quantity = abs(signed_size)
                exit_quantity += quantity
                exit_quote += price * quantity
        exit_price = (
            exit_quote / exit_quantity if exit_quantity > 0 else None
        )
        return _FillSummary(
            exit_price=exit_price,
            exit_size=exit_quantity,
            fee=fee,
        )

    @staticmethod
    def _venue_name(adapter) -> str:
        """返回用于台账的规范化交易所名称。"""
        return str(getattr(adapter, "name", type(adapter).__name__)).strip().lower()

    @staticmethod
    def _mid_price(quote: object, role: str) -> Decimal:
        """从盘口对象提取有效中价。"""
        value = Decimal(str(getattr(quote, "mid")))
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{role}平仓中价必须为有限正数")
        return value

    async def _round_exit_data(
        self,
        context: _RoundLedgerContext,
        *,
        closed_at: float,
        primary_closed_size: Decimal,
        hedge_closed_size: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
        """优先取双腿精确成交均价，否则统一降级为两腿中价。"""
        if (
            context.opened_at is None
            or context.primary_size is None
            or context.hedge_size is None
        ):
            raise ValueError("轮次台账缺少开仓时间或数量")
        summaries = await asyncio.gather(
            self._read_fill_summary(
                self.primary,
                self.config.primary_market,
                opened_at=context.opened_at,
                closed_at=closed_at,
                opening_size=context.primary_size,
            ),
            self._read_fill_summary(
                self.hedge,
                self.config.hedge_market,
                opened_at=context.opened_at,
                closed_at=closed_at,
                opening_size=context.hedge_size,
            ),
            return_exceptions=True,
        )
        primary_venue = self._venue_name(self.primary)
        hedge_venue = self._venue_name(self.hedge)
        normalized: list[_FillSummary | None] = []
        for venue, summary in zip(
            (primary_venue, hedge_venue),
            summaries,
            strict=True,
        ):
            if isinstance(summary, BaseException):
                if venue == "hyperliquid":
                    raise summary
                normalized.append(None)
            else:
                normalized.append(summary)
        primary_summary, hedge_summary = normalized

        def fee_for(venue: str, summary: _FillSummary | None) -> Decimal:
            if venue in {"lighter", "variational"}:
                return Decimal(0)
            if summary is None:
                raise ValueError(f"{venue} 缺少整轮手续费成交明细")
            return summary.fee

        primary_fee = fee_for(primary_venue, primary_summary)
        hedge_fee = fee_for(hedge_venue, hedge_summary)
        summaries_with_targets = (
            (primary_venue, primary_summary, primary_closed_size),
            (hedge_venue, hedge_summary, hedge_closed_size),
        )
        for venue, summary, expected_size in summaries_with_targets:
            complete = (
                summary is not None
                and summary.exit_price is not None
                and summary.exit_size >= expected_size
            )
            if venue == "hyperliquid" and not complete:
                raise ValueError("Hyperliquid 平仓成交尚未完整出现在时间窗内")

        primary_complete = (
            primary_summary is not None
            and primary_summary.exit_price is not None
            and primary_summary.exit_size >= primary_closed_size
        )
        hedge_complete = (
            hedge_summary is not None
            and hedge_summary.exit_price is not None
            and hedge_summary.exit_size >= hedge_closed_size
        )
        if primary_complete and hedge_complete:
            assert primary_summary is not None
            assert primary_summary.exit_price is not None
            assert hedge_summary is not None
            assert hedge_summary.exit_price is not None
            return (
                primary_summary.exit_price,
                hedge_summary.exit_price,
                primary_fee,
                hedge_fee,
                "fill",
            )

        primary_quote, hedge_quote = await asyncio.gather(
            self.primary.get_market_price(self.config.primary_market),
            self.hedge.get_market_price(self.config.hedge_market),
        )
        return (
            self._mid_price(primary_quote, "主腿"),
            self._mid_price(hedge_quote, "对冲腿"),
            primary_fee,
            hedge_fee,
            "mid",
        )

    def _append_round_ledger(self, payload: dict) -> None:
        """向启用的轮次台账追加一条 JSONL。"""
        path = self.config.ledger_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def _record_round_ledger(
        self,
        result: TimedVolumeResult,
        context: _RoundLedgerContext,
        *,
        now: float | None,
    ) -> None:
        """仅在平仓完成后按成交台账计算本轮成本归因。

        交易所的成交查询接口存在索引延迟：平仓刚完成时去查，
        成交可能尚未出现（实测 Hyperliquid 会漏，日志为
        「平仓成交尚未完整出现在时间窗内」）。此处做有界重试，
        否则台账会随机漏记而失去作为成本量尺的价值。

        重试期间不持有任何锁、不影响交易——本方法整体已被
        ``run_once`` 包在 try/except 中。
        """
        if self.config.ledger_path is None or result.action != "closed":
            return

        last_error: Exception | None = None
        for attempt in range(LEDGER_FILL_RETRIES):
            try:
                await self._build_and_write_round_ledger(result, context, now=now)
                return
            except ValueError as exc:
                # 只对「成交尚未出现」这类可自愈的时序问题重试；
                # 其余数据缺失重试也没用，直接抛给上层记警告。
                if "尚未完整出现" not in str(exc):
                    raise
                last_error = exc
                if attempt < LEDGER_FILL_RETRIES - 1:
                    await asyncio.sleep(LEDGER_FILL_RETRY_DELAY_SECONDS)
        if last_error is not None:
            raise last_error

    async def _build_and_write_round_ledger(
        self,
        result: TimedVolumeResult,
        context: _RoundLedgerContext,
        *,
        now: float | None,
    ) -> None:
        """构建并落盘单轮台账；成交未就绪时抛出以便上层重试。"""
        required = (
            context.direction,
            context.notional_usd,
            context.opened_at,
            context.primary_entry,
            context.hedge_entry,
            context.primary_size,
            context.hedge_size,
        )
        if any(value is None for value in required):
            raise ValueError("轮次台账缺少完整入场上下文")
        assert context.direction is not None
        assert context.notional_usd is not None
        assert context.opened_at is not None
        assert context.primary_entry is not None
        assert context.hedge_entry is not None
        assert context.primary_size is not None
        assert context.hedge_size is not None

        closed_at = time.time() if now is None else float(now)
        (
            primary_exit,
            hedge_exit,
            primary_fee,
            hedge_fee,
            exit_price_source,
        ) = await self._round_exit_data(
            context,
            closed_at=closed_at,
            primary_closed_size=abs(
                context.primary_size - (result.primary_size or Decimal(0))
            ),
            hedge_closed_size=abs(
                context.hedge_size - (result.hedge_size or Decimal(0))
            ),
        )
        entry_basis = (
            (context.primary_entry - context.hedge_entry)
            / context.hedge_entry
            * Decimal(100)
        )
        exit_basis = (
            (primary_exit - hedge_exit) / hedge_exit * Decimal(100)
        )
        fee_total = primary_fee + hedge_fee
        realized_pnl = (
            (primary_exit - context.primary_entry) * context.primary_size
            + (hedge_exit - context.hedge_entry) * context.hedge_size
            - fee_total
        )
        symbol = (
            str(self.config.primary_market)
            .upper()
            .split("-", 1)[0]
            .split("/", 1)[0]
        )
        payload = {
            "ts": closed_at,
            "instance": self.config.instance,
            "round_index": context.round_index,
            "direction": context.direction.value,
            "notional_usd": context.notional_usd,
            "symbol": symbol,
            "primary": {
                "venue": self._venue_name(self.primary),
                "entry": str(context.primary_entry),
                "exit": str(primary_exit),
                "size": str(context.primary_size),
                "fee": str(primary_fee),
            },
            "hedge": {
                "venue": self._venue_name(self.hedge),
                "entry": str(context.hedge_entry),
                "exit": str(hedge_exit),
                "size": str(context.hedge_size),
                "fee": str(hedge_fee),
            },
            "entry_basis_pct": str(entry_basis),
            "exit_basis_pct": str(exit_basis),
            "basis_change_pct": str(exit_basis - entry_basis),
            "realized_pnl": str(realized_pnl),
            "fee_total": str(fee_total),
            "exit_price_source": exit_price_source,
            "opened_at": context.opened_at,
            "held_seconds": closed_at - context.opened_at,
        }
        self._append_round_ledger(payload)

    async def run_once(self, *, now: float | None = None) -> TimedVolumeResult:
        """先完成交易状态机，再以完全隔离的只读查询补充面板数据。"""
        ledger_context = (
            self._round_ledger_context()
            if self.config.ledger_path is not None
            else None
        )
        result = await self._run_trading_once(now=now)
        if ledger_context is not None and result.action == "closed":
            try:
                await self._record_round_ledger(
                    result,
                    ledger_context,
                    now=now,
                )
            except Exception as exc:  # noqa: BLE001 台账不得改变任何交易结果
                logger.warning("轮次成交台账记录失败，仅跳过本轮记录：%s", exc)
        try:
            await self._record_equity_snapshot(result, now=now)
        except Exception as exc:  # noqa: BLE001 权益记账不得改变任何交易结果
            logger.warning("权益快照记录失败，仅影响累计盈亏展示：%s", exc)
        try:
            result = await self._attach_display_pnl(result)
        except Exception as exc:  # noqa: BLE001 展示数据不得改变任何交易结果
            logger.warning("盈亏快照整理失败，仅影响面板展示：%s", exc)
        if ledger_context is not None and result.action != "closed":
            try:
                self._capture_round_ledger_entry(result)
            except Exception as exc:  # noqa: BLE001 台账不得改变任何交易结果
                logger.warning("轮次成交台账记录失败，仅跳过本轮记录：%s", exc)
        return result
