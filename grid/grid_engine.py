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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from adapters.base import ExchangeAdapter, MarketPrice, Side
from engine.hedge_engine import maker_first_hedge
from grid.band import blocked_side_for_breach, compute_band, is_out_of_band
from grid.fill_log import append_fill, build_fill_record
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
from adapters.market_data import ExtendedCandleSource
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
# 相同库存拒绝只在首次、状态变化或累计到整百次时打 INFO。
_CAP_REJECTION_SUMMARY_EVERY = 100

# 单轮耗时超过这个秒数就单独记一行——慢轮是排查网络超时的关键线索，不能汇总掉
_SLOW_ROUND_LOG_S = 5.0
# 正常轮次每这么多轮汇总一条（2.5 秒轮询下约 8 分钟）
_ROUND_SUMMARY_EVERY = 200
# 状态未变化时 trend-aware 的最小打点间隔（秒）；状态一变立即打，不受此限
_TREND_LOG_MIN_INTERVAL_S = 120.0
# 净值回撤熔断的检查间隔（秒）。回撤是慢变量，不需要每轮查 balance
_EQUITY_CHECK_INTERVAL_S = 60.0
# halted 且仍有仓位时，重试平仓链的间隔（秒）
_HALT_RETRY_INTERVAL_S = 60.0
# 相邻两次权益检查之间的跳变超过此比例，判定为出入金而非行情，重新播种峰值。
# 60 秒内权益变动 8% 在行情上极罕见，在出入金上很常见。
_EQUITY_JUMP_PCT = 0.08
# 交易所时间与本机可能有轻微偏差；对账时允许一分钟时钟偏移。
_RETRY_RECONCILE_CLOCK_SKEW_SECONDS = 60.0
_RETRY_MAX_ATTEMPTS = 10
# 交易时段是策略参数，固定使用 UTC+8，绝不读取本机时区。
_UTC_PLUS_8 = timezone(timedelta(hours=8))
# 计划离场失败会每轮重试，但同类高等级告警最多每分钟一条。
_SCHEDULED_CLOSE_ALERT_INTERVAL_S = 60.0
# 对冲持续失效时每累计这么多轮再聚合告警一次，状态变化仍立即告警。
_HEDGE_INTERLOCK_SUMMARY_EVERY = 100
# 最小量查询失败后短时缓存，避免故障期每轮请求放大交易所限流；过期后允许恢复。
_MIN_ORDER_SIZE_ERROR_CACHE_S = 60.0


@dataclass(frozen=True)
class RiskLayerRequirement:
    """一层风控的能力依赖与显式启用参数。"""

    label: str
    capabilities: tuple[str, ...]
    capability_target: str
    activation_flag: str | None = None
    optional_when_disabled: bool = False


# 自检和运行期缺失告警共用这一张表，避免能力探测再次散落且不可审计。
RISK_LAYER_REQUIREMENTS = {
    "wallet_exposure_cap": RiskLayerRequirement(
        label="权益比例库存上限",
        capabilities=("get_balance",),
        capability_target="adapter",
        optional_when_disabled=True,
    ),
    "equity_drawdown": RiskLayerRequirement(
        label="净值回撤熔断",
        capabilities=("get_balance",),
        capability_target="adapter",
        activation_flag="--max-drawdown",
    ),
    "position_tpsl_equity": RiskLayerRequirement(
        label="整仓 TPSL 权益约束",
        capabilities=("get_balance",),
        capability_target="adapter",
    ),
    "liquidation_constraints": RiskLayerRequirement(
        label="清算相关约束（硬止损 / TPSL）",
        capabilities=("get_liquidation_info",),
        capability_target="adapter",
        activation_flag="--hard-stop-dist",
    ),
    "trend_band": RiskLayerRequirement(
        label="趋势 band 保护",
        capabilities=("get_hourly_candles",),
        capability_target="candle_source",
        activation_flag="--trend-aware",
    ),
    "hedge_interlock": RiskLayerRequirement(
        label="对冲存活互锁",
        capabilities=(
            "hedge_heartbeat_path",
            "hedge_heartbeat_timeout_s",
        ),
        capability_target="config",
        activation_flag="--hedge-heartbeat-path",
        optional_when_disabled=True,
    ),
}


@dataclass
class GridConfig:
    market: str = "BTC-USD"
    spacing_pct: float = 0.02            # 格距
    unit_usd: float = 50.0               # 每格名义
    max_inventory_usd: float = 150.0     # 最大库存名义
    # None 表示不启用权益比例上限，逐单行为保持为纯绝对硬顶。
    wallet_exposure_ratio: float | None = None
    levels_per_side: int = 4             # 上下各挂几格
    adx_period: int = 14
    adx_off: float = 999.0               # 默认禁用 ADX 熔断（只靠库存上限控风险）
    adx_resume: float = 999.0            # 迟滞：OFF 后 ADX 须回落到此值以下才恢复
    donchian_period: int = 48
    candle_lookback: int = 200
    poll_interval: float = 2.5
    slow_interval: float = 30.0
    # 两个边界均为 None 时完全禁用窗口，保持变更前的全天交易行为。
    trading_window_start: str | None = None
    trading_window_end: str | None = None
    # 仅用于计划离场；先等 maker，超时后强制转市价。
    maker_first_timeout_s: float = 15.0
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
    # 净值自峰值回撤熔断。max_equity_loss_pct 是**每腿**保护——TPSL 的单向棘轮
    # 在仓位翻向时重置，而网格库存频繁穿零，连续阴跌里每腿各亏 10% 会 0.9ⁿ 复利。
    # 这是唯一的跨腿约束。设 0 或负数可关闭。
    max_drawdown_pct: float = 0.12
    flat_confirmation_interval: float = 0.05
    max_writes_per_round: int = 10
    max_by_id_lookups_per_round: int = 10
    cancel_backoff_threshold: int = 3
    cancel_backoff_rounds: int = 3
    # 空元组表示默认不放弃任何层；仅允许填写 RISK_LAYER_REQUIREMENTS 的键。
    risk_waivers: tuple[str, ...] = ()
    # None 表示程序化调用没有 CLI 来源；CLI 入口必须传入实际出现过的关键 flag。
    explicit_risk_flags: tuple[str, ...] | None = None
    # 两项都为 None 时禁用互锁，保持变更前逐单行为；只配置一项会被风控自检拒绝。
    hedge_heartbeat_path: str | None = None
    hedge_heartbeat_timeout_s: float | None = None

    def __post_init__(self) -> None:
        """拒绝半配置与歧义边界，避免误把计划停机变成全天交易。"""
        configured = (
            self.trading_window_start is not None,
            self.trading_window_end is not None,
        )
        if configured[0] != configured[1]:
            raise ValueError("交易窗口起点和终点必须同时配置")
        if configured[0]:
            start = _parse_clock_minute(self.trading_window_start, "交易窗口起点")
            end = _parse_clock_minute(self.trading_window_end, "交易窗口终点")
            if start == end:
                raise ValueError("交易窗口起点和终点不能相同")
        if self.maker_first_timeout_s < 0:
            raise ValueError("maker 优先平仓超时不能为负数")


def _parse_clock_minute(value: str | None, label: str) -> int:
    """把严格 HH:MM 边界转换为当日分钟数。"""
    raw = str(value or "")
    if len(raw) != 5 or raw[2] != ":" or not raw[:2].isdigit() or not raw[3:].isdigit():
        raise ValueError(f"{label}必须使用 HH:MM 格式")
    hour = int(raw[:2])
    minute = int(raw[3:])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{label}超出有效时间范围")
    return hour * 60 + minute


class GridEngine:
    def __init__(
        self,
        ext,
        config: GridConfig | None = None,
        fng_provider=None,
        candle_source=None,
        clock=None,
    ) -> None:
        self.ext = ext
        self.config = config or GridConfig()
        self._fng_provider = fng_provider
        # 默认用交易所自身的 K 线；Lighter 的 K 线接口返回 403，
        # 做市场景由调用方传入指向 Extended 的行情源。
        self._candles = candle_source or ExtendedCandleSource(ext)
        # 注入点用于边界、跨日与周末测试；默认值始终生成 UTC 感知时间。
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._orders: dict[int, dict] = {}   # level -> {"id":..., "side": Side}
        self._retry: dict[int, dict] = {}
        self._reject_cooldown: dict[int, dict] = {}
        self._cancel_counts: dict[int, int] = {}
        self._cancel_backoff: dict[int, dict] = {}
        self._market_price_snapshot: MarketPrice | None = None
        self._counters = {
            "fill_detected": 0,
            "replacement_placed": 0,
            "replacement_failed": 0,
            "terminal_unknown": 0,
            "rejected_terminal": 0,
            "dup_skipped": 0,
            "reject_cooldown_skipped": 0,
            "bad_snapshot": 0,
            "orphan_adopted": 0,
            "write_budget_exhausted_rounds": 0,
        }
        # 以下观测值仅在当前进程内累计，进程重启后与计数器一起清零。
        self._counters_started_at = time.time()
        # 每次进程启动生成，用来识别一段数据出自哪次运行
        self._engine_run_id = f"run-{int(self._counters_started_at)}"
        self._closed_loops = 0
        self._realized_pnl_net = Decimal("0")
        self._max_abs_inv_usd = 0.0
        self._loop_fills: dict[int, dict] = {}
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
        self._last_liquidation_price: float | None = None
        self._current_tpsl: tuple[Decimal, Decimal] | None = None
        self._min_order_size_cache: dict[str, Decimal | None] = {}
        self._min_order_size_failure_ts: dict[str, float] = {}
        self._tpsl_dust_log_signature: tuple[str, Decimal] | None = None
        self._cap_rejection_state: dict[Side, dict] = {}
        self._cap_equity_fallback_state: dict | None = None
        self._risk_capability_missing_state: dict[str, dict] = {}
        self._equity_peak: float | None = None
        self._last_equity_check_ts: float = 0.0
        self._last_equity_drawdown_processed_ts: float = 0.0
        self._last_equity_seen: float | None = None
        self._last_halt_retry_ts: float = 0.0
        # 确认平仓链是否已跑完（空仓 + 残单清净 + TPSL 已撤）。收干净就别再空跑；
        # 进程重启后复位为 False，会再确认一次——幂等，且能兜住重启前的残留。
        self._halt_settled = False
        self._round_stats = {"n": 0, "sum": 0.0, "max": 0.0}
        self._last_trend_log: tuple[float, tuple] | None = None
        self._consecutive_failures = 0
        self._last_success_ts = time.time()
        self._connectivity_critical = False
        self._write_budget = self.config.max_writes_per_round
        self._by_id_lookup_budget = self.config.max_by_id_lookups_per_round
        self._write_budget_exhausted_this_round = False
        self._scheduled_stop_active = False
        self._scheduled_orders_cancelled = False
        self._scheduled_stop_flat = False
        self._last_scheduled_close_alert_ts = 0.0
        self._hedge_interlock_active = False
        self._hedge_interlock_reason = "未配置"
        self._hedge_interlock_failure_count = 0

    def _trading_window_enabled(self) -> bool:
        """仅当两个边界都已配置时启用；半配置属于启动错误。"""
        start = self.config.trading_window_start
        end = self.config.trading_window_end
        if start is None and end is None:
            return False
        if start is None or end is None:
            raise ValueError("交易窗口起点和终点必须同时配置")
        return True

    def _is_trading_window_open(self, at: datetime | None = None) -> bool:
        """按固定 UTC+8 判断时段；起点包含、终点不包含。"""
        if not self._trading_window_enabled():
            return True
        raw_now = self._clock() if at is None else at
        if raw_now.tzinfo is None:
            # 注入的无时区时间按 UTC 解释，绝不借用本机时区。
            raw_now = raw_now.replace(tzinfo=timezone.utc)
        local_now = raw_now.astimezone(_UTC_PLUS_8)
        minute = local_now.hour * 60 + local_now.minute
        start = _parse_clock_minute(
            self.config.trading_window_start,
            "交易窗口起点",
        )
        end = _parse_clock_minute(
            self.config.trading_window_end,
            "交易窗口终点",
        )
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end

    @property
    def trading_window_state(self) -> str:
        """返回供启动摘要、live 快照与心跳共同使用的稳定状态值。"""
        if not self._trading_window_enabled():
            return "disabled"
        return "open" if self._is_trading_window_open() else "planned_stop"

    def trading_window_summary(self) -> str:
        """返回不含本机时区歧义的中文窗口摘要。"""
        if not self._trading_window_enabled():
            return "未启用（全天交易）"
        state = "窗口内" if self._is_trading_window_open() else "计划停机"
        return (
            f"UTC+8 {self.config.trading_window_start}–"
            f"{self.config.trading_window_end}，当前={state}，"
            f"maker 超时={self.config.maker_first_timeout_s:g}秒"
        )

    @property
    def hedge_interlock_active(self) -> bool:
        """返回当前对冲互锁是否生效，供入口心跳与状态面板读取。"""
        return self._hedge_interlock_active

    @property
    def hedge_interlock_reason(self) -> str:
        """返回最近一次对冲存活判定原因。"""
        return self._hedge_interlock_reason

    def _hedge_interlock_requested(self) -> bool:
        """任一互锁参数出现即视为声明启用，半配置必须失败关闭。"""
        return (
            self.config.hedge_heartbeat_path is not None
            or self.config.hedge_heartbeat_timeout_s is not None
        )

    def _set_hedge_interlock(self, active: bool, reason: str) -> None:
        """更新互锁状态；变化立即告警，持续失效按轮次聚合。"""
        was_active = self._hedge_interlock_active
        self._hedge_interlock_active = active
        self._hedge_interlock_reason = reason
        if active:
            self._hedge_interlock_failure_count += 1
            count = self._hedge_interlock_failure_count
            if not was_active:
                logger.warning(
                    "对冲互锁已触发：%s；已停止扩大敞口的新挂单，"
                    "仅允许撤单与缩小敞口挂单",
                    reason,
                )
            elif count % _HEDGE_INTERLOCK_SUMMARY_EVERY == 0:
                logger.warning(
                    "对冲互锁持续生效：%s；同类累计=%d轮",
                    reason,
                    count,
                )
            else:
                logger.debug("对冲互锁持续生效：%s；累计=%d轮", reason, count)
            return

        self._hedge_interlock_failure_count = 0
        if was_active:
            logger.warning("对冲心跳已恢复：%s；对冲互锁已解除", reason)

    def _refresh_hedge_interlock(self) -> bool:
        """读取对冲 JSONL 末行并失败关闭地刷新互锁状态。"""
        if not self._hedge_interlock_requested():
            self._hedge_interlock_active = False
            self._hedge_interlock_reason = "未配置"
            self._hedge_interlock_failure_count = 0
            return False

        heartbeat_path = str(self.config.hedge_heartbeat_path or "").strip()
        timeout = self.config.hedge_heartbeat_timeout_s
        try:
            timeout_s = float(timeout)
        except (TypeError, ValueError):
            timeout_s = 0.0
        if not heartbeat_path or not math.isfinite(timeout_s) or timeout_s <= 0:
            self._set_hedge_interlock(True, "互锁配置不完整：缺少心跳路径或有效超时")
            return True

        path = Path(heartbeat_path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            self._set_hedge_interlock(True, f"对冲心跳文件不存在：{path}")
            return True
        except (OSError, UnicodeError) as exc:
            self._set_hedge_interlock(True, f"对冲心跳文件读取失败：{exc}")
            return True

        if not lines:
            self._set_hedge_interlock(True, "对冲心跳内容损坏：文件为空")
            return True
        try:
            heartbeat = json.loads(lines[-1])
        except (json.JSONDecodeError, TypeError) as exc:
            self._set_hedge_interlock(True, f"对冲心跳末行损坏：{exc}")
            return True
        if not isinstance(heartbeat, dict):
            self._set_hedge_interlock(True, "对冲心跳末行损坏：不是 JSON 对象")
            return True

        raw_ts = heartbeat.get("ts")
        try:
            heartbeat_ts = float(raw_ts)
        except (TypeError, ValueError):
            self._set_hedge_interlock(True, "对冲心跳缺少有效时间戳")
            return True
        if not math.isfinite(heartbeat_ts) or heartbeat_ts <= 0:
            self._set_hedge_interlock(True, "对冲心跳缺少有效时间戳")
            return True

        age = time.time() - heartbeat_ts
        if age > timeout_s:
            self._set_hedge_interlock(
                True,
                f"对冲心跳陈旧：已 {age:.1f} 秒未更新，超时阈值 {timeout_s:g} 秒",
            )
            return True
        if (
            heartbeat.get("primary_read_ok") is not True
            or heartbeat.get("hedge_read_ok") is not True
        ):
            self._set_hedge_interlock(
                True,
                "对冲心跳自报两腿读取失败："
                f"primary_read_ok={heartbeat.get('primary_read_ok')!r}，"
                f"hedge_read_ok={heartbeat.get('hedge_read_ok')!r}",
            )
            return True

        self._set_hedge_interlock(
            False,
            f"心跳新鲜（年龄 {max(0.0, age):.1f} 秒）且两腿读取正常",
        )
        return False

    def _hedge_interlock_allows(
        self,
        side: Side,
        inv_usd: float,
        order_notional_usd: float,
    ) -> bool:
        """互锁生效时仅允许让绝对敞口严格减小的方向与数量。"""
        if not self._hedge_interlock_active:
            return True
        signed_order = (
            order_notional_usd if side is Side.BUY else -order_notional_usd
        )
        projected = inv_usd + signed_order
        return abs(projected) < abs(inv_usd)

    @property
    def _log_step(self) -> float:
        return math.log(1 + self.config.spacing_pct)

    def _level_price(self, level: int) -> Decimal:
        return Decimal(str(math.exp(level * self._log_step)))

    def _price_level(self, price: float) -> int:
        return round(math.log(price) / self._log_step)

    def _risk_layer_enabled(self, layer: str) -> bool:
        """按现有配置判断风控层是否开启，不改变任何触发阈值。"""
        if layer == "wallet_exposure_cap":
            return self.config.wallet_exposure_ratio is not None
        if layer == "equity_drawdown":
            return self.config.max_drawdown_pct > 0
        if layer == "position_tpsl_equity":
            return (
                self.config.exchange_tpsl
                and self.config.max_equity_loss_pct >= 0
            )
        if layer == "liquidation_constraints":
            return self.config.hard_stop_dist > 0 or self.config.exchange_tpsl
        if layer == "trend_band":
            return self.config.trend_aware
        if layer == "hedge_interlock":
            return (
                self.config.hedge_heartbeat_path is not None
                or self.config.hedge_heartbeat_timeout_s is not None
            )
        raise KeyError(f"未知风控层：{layer}")

    def _risk_capability_target(self, layer: str):
        requirement = RISK_LAYER_REQUIREMENTS[layer]
        if requirement.capability_target == "adapter":
            return self.ext
        if requirement.capability_target == "candle_source":
            return self._candles
        if requirement.capability_target == "config":
            return self.config
        raise ValueError(
            f"风控层 {layer} 使用未知能力目标：{requirement.capability_target}"
        )

    def _has_risk_capability(self, layer: str, capability: str) -> bool:
        """判断能力是否真实实现，基类的空默认实现不算可用能力。"""
        target = self._risk_capability_target(layer)
        if RISK_LAYER_REQUIREMENTS[layer].capability_target == "config":
            value = getattr(target, capability, None)
            if capability == "hedge_heartbeat_path":
                return bool(str(value or "").strip())
            if capability == "hedge_heartbeat_timeout_s":
                try:
                    return math.isfinite(float(value)) and float(value) > 0
                except (TypeError, ValueError):
                    return False
            return value is not None
        method = getattr(target, capability, None)
        if not callable(method):
            return False
        if isinstance(target, ExchangeAdapter):
            implementation = getattr(type(target), capability, None)
            base_implementation = getattr(ExchangeAdapter, capability, None)
            if implementation is base_implementation:
                return False
        return True

    def validate_risk_controls(self) -> dict[str, str]:
        """启动前校验全部风控层，记录摘要并拒绝未声明的缺失。

        CLI 入口还会提供实际出现过的关键 flag。这样即使配置对象有
        12% 等历史默认值，也不能把「运维未显式配置」伪装成已确认启用。
        程序化调用没有 CLI 来源时仍按配置的真实开关和能力判断。
        """
        waivers = set(self.config.risk_waivers)
        unknown_waivers = waivers - RISK_LAYER_REQUIREMENTS.keys()
        if unknown_waivers:
            names = "、".join(sorted(unknown_waivers))
            raise RuntimeError(f"未知的风控知情放弃层：{names}")

        explicit_flags = (
            None
            if self.config.explicit_risk_flags is None
            else set(self.config.explicit_risk_flags)
        )
        statuses: dict[str, str] = {}
        issues: list[tuple[str, list[str]]] = []

        for layer, requirement in RISK_LAYER_REQUIREMENTS.items():
            enabled = self._risk_layer_enabled(layer)
            hard_stop_closed = (
                layer == "liquidation_constraints"
                and enabled
                and self.config.hard_stop_dist <= 0
            )
            missing_capabilities = (
                [
                    capability
                    for capability in requirement.capabilities
                    if not self._has_risk_capability(layer, capability)
                ]
                if enabled
                else []
            )
            missing_explicit_flag = (
                explicit_flags is not None
                and requirement.activation_flag is not None
                and requirement.activation_flag not in explicit_flags
                and (enabled or not requirement.optional_when_disabled)
            )

            if not enabled:
                status = "未配置" if requirement.optional_when_disabled else "关闭"
            elif missing_capabilities:
                status = "不可用"
            elif hard_stop_closed:
                status = "部分关闭"
            elif missing_explicit_flag:
                status = "未确认"
            else:
                status = "启用"

            reasons: list[str] = []
            if not enabled and not requirement.optional_when_disabled:
                reasons.append("配置已关闭")
            if hard_stop_closed:
                reasons.append("硬止损配置已关闭（hard_stop_dist <= 0）")
            if missing_capabilities:
                if layer == "hedge_interlock":
                    reasons.append(
                        "缺少心跳路径或有效超时配置（"
                        + "、".join(missing_capabilities)
                        + "）"
                    )
                else:
                    reasons.append(
                        "缺少能力 " + "、".join(missing_capabilities)
                    )
            if missing_explicit_flag:
                reasons.append(
                    f"未显式配置 {requirement.activation_flag}"
                )

            if reasons and layer in waivers:
                status += "（知情放弃）"
                logger.warning(
                    "风控层已知情放弃：%s；%s",
                    requirement.label,
                    "；".join(reasons),
                )
            elif reasons:
                issues.append((layer, reasons))
            statuses[layer] = status

        summary = "；".join(
            f"{RISK_LAYER_REQUIREMENTS[layer].label}={status}"
            for layer, status in statuses.items()
        )
        logger.info("风控状态摘要：%s", summary)

        if issues:
            details = "；".join(
                f"{RISK_LAYER_REQUIREMENTS[layer].label}：{'，'.join(reasons)}"
                for layer, reasons in issues
            )
            raise RuntimeError(
                f"风控完整性自检失败（{len(issues)} 层）：{details}。"
                "如确需无此保护运行，必须显式声明对应知情放弃层"
            )
        return statuses

    def _log_risk_capability_missing(
        self,
        layer: str,
        capability: str,
    ) -> None:
        """运行期能力缺失按状态签名聚合，首次与整百次输出 WARNING。"""
        requirement = RISK_LAYER_REQUIREMENTS[layer]
        signature = (layer, capability)
        previous = self._risk_capability_missing_state.get(layer)
        repeated = previous is not None and previous["signature"] == signature
        count = int(previous["count"]) + 1 if repeated else 1
        self._risk_capability_missing_state[layer] = {
            "signature": signature,
            "count": count,
        }
        log = (
            logger.warning
            if not repeated or count % _CAP_REJECTION_SUMMARY_EVERY == 0
            else logger.debug
        )
        log(
            "运行期风控跳过：%s 缺少能力 %s；同类累计=%d",
            requirement.label,
            capability,
            count,
        )

    async def connect(self) -> None:
        await self.ext.connect()
        pos = await self.ext.get_position(self.config.market)
        if not self.config.dry_run and pos.signed_size != 0:
            mark = await self.ext.get_mark_price(self.config.market)
            notional = abs(float(pos.signed_size)) * float(mark)
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
            self._orders[lv] = {"id": getattr(o, "id", None), "side": side,
                                "reduce_only": bool(getattr(o, "reduce_only", False)),
                                "qty": getattr(o, "qty", None) or getattr(o, "amount", None)}
        return orders

    async def _inv(self, position=None) -> tuple[Decimal, float]:
        """一次读取仓位；mark 取实时行情，失败时才回退仓位值。"""
        pos = position or await self.ext.get_position(self.config.market)
        raw = getattr(pos, "raw", None)
        raw_mark = getattr(raw, "mark_price", None)
        raw_liq = getattr(raw, "liquidation_price", None)
        self._last_liquidation_price = (
            float(raw_liq) if raw_liq is not None and float(raw_liq) > 0 else None
        )
        try:
            price = float(await self.ext.get_mark_price(self.config.market))
            if price <= 0:
                raise ValueError(f"实时 mark 非正数：{price}")
        except Exception as exc:  # noqa: BLE001
            if raw_mark is None or float(raw_mark) <= 0:
                raise
            price = float(raw_mark)
            logger.warning(
                "实时 mark 获取失败，回落到 position.mark_price=%s（可能陈旧）：%s",
                price,
                exc,
            )
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

    async def _round_market_price(self, price: Decimal) -> Decimal:
        """优先使用适配器的 tick 对齐能力；旧桩缺失该能力时原样降级。"""
        price = Decimal(str(price))
        rounder = getattr(self.ext, "round_price", None)
        if not callable(rounder):
            return price
        try:
            rounded = Decimal(
                str(await rounder(self.config.market, price))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("按市场 tick 对齐价格失败，暂用原值 %s：%s", price, exc)
            return price
        if rounded <= 0:
            logger.warning("适配器返回无效取整价格 %s，暂用原值 %s", rounded, price)
            return price
        return rounded

    async def _price_tick_size(self) -> Decimal | None:
        """读取适配器 tick，用于交易所回读比较的半 tick 容差兜底。"""
        getter = getattr(self.ext, "get_price_tick_size", None)
        if not callable(getter):
            return None
        try:
            tick = await getter(self.config.market)
            tick = Decimal(str(tick)) if tick is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取市场价格 tick 失败，将只使用固定容差：%s", exc)
            return None
        return tick if tick is not None and tick > 0 else None

    async def _get_min_order_size(self) -> Decimal | None:
        """查询并缓存市场最小下单量；未知时返回 None 供风控失败关闭。"""
        market = self.config.market
        if market in self._min_order_size_cache:
            cached = self._min_order_size_cache[market]
            if cached is not None:
                return cached
            failed_at = self._min_order_size_failure_ts.get(market, 0.0)
            if time.monotonic() - failed_at < _MIN_ORDER_SIZE_ERROR_CACHE_S:
                return None

        getter = getattr(self.ext, "get_min_order_size", None)
        try:
            if not callable(getter):
                raise RuntimeError("适配器缺少 get_min_order_size 能力")
            minimum = Decimal(str(await getter(market)))
            if not minimum.is_finite() or minimum <= 0:
                raise ValueError(f"返回无效值 {minimum}")
        except Exception as exc:  # noqa: BLE001 最小量未知时必须保守执行原风控
            self._min_order_size_cache[market] = None
            self._min_order_size_failure_ts[market] = time.monotonic()
            logger.warning(
                "查询市场 %s 最小下单量失败，保守按需要 TPSL 与硬止损处理；"
                "%.0f 秒内不重复查询：%s",
                market,
                _MIN_ORDER_SIZE_ERROR_CACHE_S,
                exc,
            )
            return None

        self._min_order_size_cache[market] = minimum
        self._min_order_size_failure_ts.pop(market, None)
        return minimum

    async def _dust_position_minimum(
        self,
        signed_size: Decimal,
    ) -> Decimal | None:
        """若持仓低于已知市场最小量则返回该门槛，否则返回 None。"""
        minimum = await self._get_min_order_size()
        if minimum is None:
            return None
        return minimum if abs(Decimal(str(signed_size))) < minimum else None

    async def _advance_band(
        self,
        mark: float,
        mode: GridMode,
        *,
        signed_size: Decimal | None = None,
    ) -> None:
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

        if signed_size is None:
            pos = await self.ext.get_position(self.config.market)
            signed_size = pos.signed_size
        if signed_size != 0:
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
            self._tpsl_dust_log_signature = None
            return True
        if not self.config.exchange_tpsl:
            return True

        dust_minimum = await self._dust_position_minimum(signed_size)
        if dust_minimum is not None:
            # 不保留旧内存命中，否则仓位重新达到可交易量时可能误判已有保护。
            self._current_tpsl = None
            signature = (self.config.market, dust_minimum)
            if self._tpsl_dust_log_signature != signature:
                logger.info(
                    "持仓低于最小下单量，不挂 TPSL：市场=%s 持仓=%s "
                    "最小下单量=%s",
                    self.config.market,
                    signed_size,
                    dust_minimum,
                )
                self._tpsl_dust_log_signature = signature
            return True
        self._tpsl_dust_log_signature = None

        mark_decimal = Decimal(str(mark))
        stop_distance = Decimal(str(self.config.hard_stop_dist))
        liq_buffer = Decimal(str(self.config.liq_buffer_pct))
        max_loss_pct = Decimal(str(self.config.max_equity_loss_pct))

        liq_price = (
            Decimal(str(self._last_liquidation_price))
            if self._last_liquidation_price is not None
            else None
        )
        liquidation_getter = getattr(self.ext, "get_liquidation_info", None)
        if liq_price is None:
            if not self._has_risk_capability(
                "liquidation_constraints",
                "get_liquidation_info",
            ):
                self._log_risk_capability_missing(
                    "liquidation_constraints",
                    "get_liquidation_info",
                )
            elif callable(liquidation_getter):
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
        if not self._has_risk_capability(
            "position_tpsl_equity",
            "get_balance",
        ):
            self._log_risk_capability_missing(
                "position_tpsl_equity",
                "get_balance",
            )
        elif callable(balance_getter):
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

        # 引擎、适配器和交易所必须共享同一个 tick 对齐目标值。
        trigger_price = await self._round_market_price(trigger_price)
        price_tick = await self._price_tick_size()

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
                        exchange_trigger = await self._round_market_price(
                            exchange_trigger
                        )
                        exchange_tolerance = max(
                            _TPSL_EXCHANGE_TRIGGER_TOLERANCE_ABS,
                            abs(previous_trigger)
                            * _TPSL_EXCHANGE_TRIGGER_TOLERANCE_PCT,
                            price_tick / 2
                            if price_tick is not None
                            else Decimal("0"),
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

    def _log_round_complete(self, elapsed: float) -> None:
        """正常轮次汇总记，慢轮单独记。

        2026-08-11 实测：每轮一行「本轮完成」占了全天日志的 90%
        （21095/23354 行、约 30MB/天），把成交和异常这些真正要看的行淹没了。
        慢轮仍逐条保留——排查 TLS 握手超时全靠它。
        """
        if elapsed >= _SLOW_ROUND_LOG_S:
            logger.info("本轮完成（慢），耗时 %.2f 秒", elapsed)
            return
        stats = self._round_stats
        stats["n"] += 1
        stats["sum"] += elapsed
        stats["max"] = max(stats["max"], elapsed)
        if stats["n"] >= _ROUND_SUMMARY_EVERY:
            logger.info(
                "近 %d 轮正常完成：平均 %.2f 秒，最慢 %.2f 秒",
                stats["n"],
                stats["sum"] / stats["n"],
                stats["max"],
            )
            stats.update(n=0, sum=0.0, max=0.0)

    def _should_log_trend(self, signature: tuple, now: float) -> bool:
        """trend-aware 打点节流：状态一变立即打，没变则按最小间隔降频。

        这些行是事后还原时间线的主要依据（8/10 那次停摆就是靠它定位的），
        所以只降稳态频率，绝不丢状态变化。
        """
        last = self._last_trend_log
        if last is not None and last[1] == signature:
            if now - last[0] < _TREND_LOG_MIN_INTERVAL_S:
                return False
        self._last_trend_log = (now, signature)
        return True

    @staticmethod
    def _effective_blocked_side(
        mode: GridMode,
        state: "GridState | None",
    ) -> str | None:
        """本轮**真正生效**的冻结方向（唯一真相源，日志与快照共用）。

        与只记 band 突破的 state.blocked_side 不是一回事：OFF 要合成 BOTH、
        frozen 无方向时兜底 BOTH。2026-08-11 的停摆里引擎日志是 blocked=BOTH
        而快照写的 state.blocked_side 是 None，面板因此把 OFF 显示成正常——
        所以这里抽成一处，避免两边再次漂移。
        """
        if mode is GridMode.OFF:
            return "BOTH"
        if state is None:
            return "BOTH"
        if state.halted:
            return "BOTH"
        if state.frozen:
            return state.blocked_side or "BOTH"
        return state.blocked_side

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
        inv_usd = (
            inv_btc * mark_float
            if inv_btc is not None and mark_float is not None
            else None
        )
        if inv_usd is not None:
            self._max_abs_inv_usd = max(self._max_abs_inv_usd, abs(inv_usd))
        retry_exhausted = sum(
            1 for entry in self._retry.values() if entry.get("exhausted")
        )
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
            "inv_usd": inv_usd,
            "band_low": state.band_low if state is not None else None,
            "band_high": state.band_high if state is not None else None,
            "frozen": state.frozen if state is not None else False,
            "blocked_side": state.blocked_side if state is not None else None,
            "effective_blocked_side": self._effective_blocked_side(mode, state),
            "halted": state.halted if state is not None else False,
            "trading_window_state": self.trading_window_state,
            "planned_stop": self.trading_window_state == "planned_stop",
            "hedge_interlock_active": self.hedge_interlock_active,
            "hedge_interlock_reason": self.hedge_interlock_reason,
            "dist_to_liq_pct": self._last_dist_to_liq,
            "cfg": {
                "unit": self.config.unit_usd,
                "levels": self.config.levels_per_side,
                "max_inv": self.config.max_inventory_usd,
                "spacing": self.config.spacing_pct,
                "hard_stop_dist": self.config.hard_stop_dist,
                "adx_off": self.config.adx_off,
            },
            **self._counters,
            "retry_pending": len(self._retry) - retry_exhausted,
            "retry_exhausted": retry_exhausted,
            "counters_started_at": self._counters_started_at,
            "closed_loops": self._closed_loops,
            "realized_pnl_net": float(self._realized_pnl_net),
            "max_abs_inv_usd": self._max_abs_inv_usd,
            # 纯价差合计，未计手续费（maker 为 0）也未计资金费。
            "funding_included": False,
            "consecutive_failures": self._consecutive_failures,
            "last_success_ts": self._last_success_ts,
        }
        tmp = live_path.with_suffix(live_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, live_path)

    def _dump_connectivity(self) -> None:
        """即使整轮失败，也只更新失联字段并原子写入 live 快照。"""
        live_path = Path(self.config.state_path).parent / "grid_live.json"
        live_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(live_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            payload = {}
        payload.update(
            consecutive_failures=self._consecutive_failures,
            last_success_ts=self._last_success_ts,
            trading_window_state=self.trading_window_state,
            planned_stop=self.trading_window_state == "planned_stop",
            hedge_interlock_active=self.hedge_interlock_active,
            hedge_interlock_reason=self.hedge_interlock_reason,
        )
        tmp = live_path.with_suffix(live_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, live_path)

    async def _run_once_trend_aware(self, include_slow: bool = True) -> str:
        """趋势感知单轮：每轮保护性执行，按需追加 K 线与补格慢路径。"""
        if self._state is None:
            self._state = load_state(self.config.state_path)
        if self._state is not None and self._state.halted:
            pos = await self.ext.get_position(self.config.market)
            inv = pos.signed_size
            self._last_inv = inv
            # 空仓 ≠ 收摊完成：撤单或撤 TPSL 失败会留下普通挂单，它们成交后
            # 会重新开仓，halted 就白停了。所以只要还没确认收干净，无论有无
            # 仓位都要把确认链跑完；跑通一次后置位，不再空转。
            if not self._halt_settled and self._halt_retry_due():
                logger.warning("halted 未收摊完成（持仓=%s），重试确认平仓链", inv)
                self._halt_settled = await self._go_off_confirmed(inv)
            if inv == 0:
                self._current_tpsl = None
            else:
                inv, mark = await self._inv(position=pos)
                self._last_mark = mark
                await self._maintain_tpsl(mark, inv)
                # halted 只禁止新增报价，既有硬止损风险管理仍继续执行。
                hard_stop_kwargs = {"signed_size": inv, "mark": mark}
                if pos is not None:
                    hard_stop_kwargs["position"] = pos
                if await self._check_hard_stop(**hard_stop_kwargs):
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

        pos = None
        position_getter = getattr(self.ext, "get_position", None)
        if callable(position_getter):
            pos = await position_getter(self.config.market)
            inv, mark = await self._inv(position=pos)
        else:
            inv, mark = await self._inv()
        self._last_inv = inv
        self._last_mark = mark
        inv_usd = float(inv) * mark

        hard_stop_kwargs = {"signed_size": inv, "mark": mark}
        if pos is not None:
            hard_stop_kwargs["position"] = pos
        if await self._check_hard_stop(**hard_stop_kwargs):
            await self._dump_live(
                mark=self._last_mark,
                mode=self._mode,
                inv=self._last_inv,
                closes=self._last_closes,
            )
            return "HALTED：硬止损已触发"
        # 跨腿保护：硬止损与 TPSL 都是"单腿"约束，只有它盯着累计净值
        if await self._check_equity_drawdown():
            await self._dump_live(
                mark=self._last_mark,
                mode=self._mode,
                inv=self._last_inv,
                closes=self._last_closes,
            )
            return "HALTED：净值回撤熔断已触发"
        if self._state is not None and self._state.halted:
            await self._dump_live(
                mark=self._last_mark,
                mode=self._mode,
                inv=self._last_inv,
                closes=self._last_closes,
            )
            return "HALTED：禁止新增报价"

        tpsl_confirmed = await self._maintain_tpsl(mark, inv)
        state = self._state
        frozen = state is not None and state.frozen
        if self._mode is GridMode.OFF:
            blocked_side = "BOTH"
        elif frozen:
            # fail-closed 哨兵没有可判定的突破方向，必须冻结双侧。
            blocked_side = state.blocked_side or "BOTH"
        else:
            # K 线尚未读取，保护性动作沿用上一轮迟滞门控结果。
            blocked_side = state.blocked_side if state is not None else "BOTH"
        # 互锁撤单优先占用写预算，避免成交翻单先耗尽预算后危险陈单仍留在盘口。
        await self._cancel_interlock_expanding_orders(inv_usd)
        # _handle_fills 内部会先 drain retry；外层不得重复调用。
        await self._handle_fills(inv_usd, blocked_side=blocked_side)

        if not include_slow:
            return "快速执行完成"

        try:
            candle_getter = getattr(self._candles, "get_hourly_candles", None)
            if not callable(candle_getter):
                self._log_risk_capability_missing(
                    "trend_band",
                    "get_hourly_candles",
                )
                raise RuntimeError("K 线源缺少 get_hourly_candles 能力")
            candles = await candle_getter(
                self.config.market,
                self.config.candle_lookback,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("K线获取失败，已处理成交并跳过补格：%s", exc)
            await self._dump_live(
                mark=mark,
                mode=self._mode,
                inv=inv,
                closes=self._last_closes,
            )
            return "K线获取失败：已处理成交，跳过补格"

        highs = drop_forming_candle([float(k.high) for k in candles])
        lows = drop_forming_candle([float(k.low) for k in candles])
        closes = drop_forming_candle([float(k.close) for k in candles])
        self._last_highs = highs
        self._last_lows = lows
        self._last_closes = closes
        self._current_closed_bar_key = (
            int(candles[-2].ts) if len(candles) >= 2 else None
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

        await self._advance_band(mark, mode, signed_size=inv)

        state = self._state
        if state is not None and state.halted:
            await self._dump_live(mark=mark, mode=mode, inv=inv, closes=closes)
            return "HALTED：禁止新增报价"

        frozen = state is not None and state.frozen
        valid_active = (
            state is not None
            and not state.frozen
            and self._has_valid_band(state)
        )
        blocked_side = self._effective_blocked_side(mode, state)

        # 库存变动不进签名：它几乎每轮都在动，进了签名节流就形同虚设。
        # 真正要立即可见的是 mode/frozen/blocked/band 这些状态量。
        trend_signature = (
            mode.value,
            frozen,
            blocked_side,
            state.band_low if state is not None else None,
            state.band_high if state is not None else None,
        )
        if self._should_log_trend(trend_signature, time.monotonic()):
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

        if frozen:
            await self._maintain_recovery_ladder(mark, inv, inv_usd)

        if mode is GridMode.OFF or not valid_active:
            await self._dump_live(mark=mark, mode=mode, inv=inv, closes=closes)
            return (
                "OFF：暂停新增报价"
                if mode is GridMode.OFF
                else "FROZEN：仅处理已有订单"
            )

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

    async def _refresh_market_price(self) -> None:
        """每轮只读取一次盘口；任何不可用情况都降级为整轮放行。"""
        self._market_price_snapshot = None
        getter = getattr(self.ext, "get_market_price", None)
        if not callable(getter):
            logger.warning("盘口读取能力不可用，本轮降级放行所有挂单")
            return
        try:
            market_price = await getter(self.config.market)
            if market_price is None:
                raise ValueError("返回空盘口")
            bid = Decimal(str(market_price.bid))
            ask = Decimal(str(market_price.ask))
            if bid <= 0 or ask <= 0 or bid >= ask:
                raise ValueError(f"无效盘口 bid={bid} ask={ask}")
            self._market_price_snapshot = MarketPrice(
                market=self.config.market,
                bid=bid,
                ask=ask,
            )
        except Exception as exc:  # noqa: BLE001 保护数据不可用时必须失败开放
            logger.warning("盘口读取失败，本轮降级放行所有挂单：%s", exc)

    def _advance_cancel_backoffs(self) -> None:
        """按成交检查轮次推进退避，到期时清零连续取消计数。"""
        for level, state in list(self._cancel_backoff.items()):
            remaining = int(state.get("remaining_rounds", 0))
            if remaining <= 0:
                self._cancel_backoff.pop(level, None)
                self._cancel_counts.pop(level, None)
                logger.info("格%d 取消退避期满，恢复正常挂单", level)
                continue
            state["remaining_rounds"] = remaining - 1

    def _record_cancelled(self, level: int) -> bool:
        """记录同格连续取消；返回是否已达到阈值并进入退避。"""
        count = self._cancel_counts.get(level, 0) + 1
        self._cancel_counts[level] = count
        threshold = max(1, int(self.config.cancel_backoff_threshold))
        if count < threshold:
            return False
        rounds = max(1, int(self.config.cancel_backoff_rounds))
        self._cancel_backoff[level] = {"remaining_rounds": rounds}
        logger.warning(
            "格%d 已连续取消 %d 次，进入退避 %d 轮并暂停原格重挂",
            level,
            count,
            rounds,
        )
        return True

    def _reset_cancel_state(self, level: int, *, reason: str) -> None:
        """成交或非取消终态会打断连续取消序列。"""
        had_backoff = level in self._cancel_backoff
        had_count = level in self._cancel_counts
        self._cancel_backoff.pop(level, None)
        self._cancel_counts.pop(level, None)
        if had_backoff:
            logger.info("格%d 因%s退出取消退避，连续计数已归零", level, reason)
        elif had_count:
            logger.debug("格%d 因%s将连续取消计数归零", level, reason)

    def _market_side_allows(self, level: int, side: Side, price: Decimal) -> bool:
        """使用本轮盘口快照校验 post-only 目标价；无快照时失败开放。"""
        market_price = self._market_price_snapshot
        if market_price is None:
            return True
        wrong_side = (
            side is Side.SELL and price <= market_price.ask
        ) or (
            side is Side.BUY and price >= market_price.bid
        )
        if not wrong_side:
            return True
        self._retry.pop(level, None)
        logger.info(
            "盘口侧校验跳过挂单：格%d 方向=%s 目标价=%s，"
            "最优买价=%s、最优卖价=%s；不进入重试队列",
            level,
            side.value,
            price,
            market_price.bid,
            market_price.ask,
        )
        return False

    def _log_scheduled_close_failure(
        self,
        reason: str,
        inventory: Decimal | None,
    ) -> None:
        """计划离场失败每轮留痕，高等级告警按一分钟限频。"""
        now = time.monotonic()
        message = (
            "计划离场平仓失败：%s；库存=%s，未归零，下轮继续重试"
        )
        if (
            self._last_scheduled_close_alert_ts == 0.0
            or now - self._last_scheduled_close_alert_ts
            >= _SCHEDULED_CLOSE_ALERT_INTERVAL_S
        ):
            self._last_scheduled_close_alert_ts = now
            logger.critical(message, reason, inventory)
        else:
            logger.debug(message, reason, inventory)

    def _reset_scheduled_stop(self) -> None:
        """窗口恢复时清理计划离场的进程内状态，不触碰 halted。"""
        self._scheduled_stop_active = False
        self._scheduled_orders_cancelled = False
        self._scheduled_stop_flat = False
        self._last_scheduled_close_alert_ts = 0.0

    async def _run_scheduled_stop_cycle(self) -> str:
        """执行一次计划离场确认：撤单、平仓至零、停止开仓。"""
        if not self._scheduled_stop_active:
            self._scheduled_stop_active = True
            self._scheduled_orders_cancelled = False
            self._scheduled_stop_flat = False
            logger.warning(
                "已进入计划停机时段：开始执行撤单 → 平仓至零 → 停止开仓"
            )

        if not self._scheduled_orders_cancelled:
            logger.warning("计划离场步骤 1/3：撤销全部网格挂单")
            if self.config.dry_run:
                logger.warning("[dry_run] 计划离场仅模拟撤单，不发送交易所写请求")
            else:
                # 批量撤单接口会刻意保留 reduce-only 保护单；恢复阶梯也使用
                # reduce-only，但它仍属于网格，必须先按引擎记录逐单撤掉。
                for level, record in list(self._orders.items()):
                    try:
                        await self.ext.cancel_order(
                            self.config.market,
                            record["id"],
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._log_scheduled_close_failure(
                            f"撤销格 {level} 订单异常：{exc}",
                            None,
                        )
                        return "计划停机：撤单失败，等待下轮重试"
                    self._orders.pop(level, None)
                try:
                    # 再扫一次交易所，清理未能按价格接管的普通孤儿网格单。
                    await self.ext.cancel_grid_orders(self.config.market)
                except Exception as exc:  # noqa: BLE001
                    self._log_scheduled_close_failure(
                        f"批量清理网格挂单异常：{exc}",
                        None,
                    )
                    return "计划停机：撤单失败，等待下轮重试"
                # 交易所已确认撤单，不能把旧重试留给窗口恢复后的铺单路径。
                self._orders.clear()
                self._retry.clear()
                self._reject_cooldown.clear()
            self._scheduled_orders_cancelled = True
            logger.warning("计划离场步骤 1/3 完成：网格挂单已撤销")

        try:
            position = await self.ext.get_position(self.config.market)
        except Exception as exc:  # noqa: BLE001
            self._log_scheduled_close_failure(f"查询库存异常：{exc}", None)
            return "计划停机：库存未知，等待下轮重试"

        remaining = Decimal(str(position.signed_size))
        self._last_inv = remaining
        if remaining != 0:
            self._scheduled_stop_flat = False
            logger.warning(
                "计划离场步骤 2/3：平仓至零，当前库存=%s；"
                "maker 优先，超时 %.1f 秒后转市价",
                remaining,
                self.config.maker_first_timeout_s,
            )
            if self.config.dry_run:
                self._log_scheduled_close_failure(
                    "dry_run 不执行真实平仓",
                    remaining,
                )
                return "计划停机（dry_run）：库存未归零"
            try:
                result = await maker_first_hedge(
                    self.ext,
                    self.config.market,
                    -remaining,
                    timeout_s=self.config.maker_first_timeout_s,
                    reduce_only=True,
                )
                logger.warning("计划离场平仓委托完成：%s", result.note)
            except Exception as exc:  # noqa: BLE001
                self._log_scheduled_close_failure(
                    f"maker / 市价平仓异常：{exc}",
                    remaining,
                )
                return "计划停机：平仓异常，等待下轮重试"

            try:
                position = await self.ext.get_position(self.config.market)
            except Exception as exc:  # noqa: BLE001
                self._log_scheduled_close_failure(
                    f"平仓后复核库存异常：{exc}",
                    None,
                )
                return "计划停机：平仓结果未知，等待下轮重试"
            remaining = Decimal(str(position.signed_size))
            self._last_inv = remaining
            if remaining != 0:
                self._log_scheduled_close_failure(
                    "平仓委托后仍有剩余库存",
                    remaining,
                )
                return "计划停机：库存未归零，等待下轮重试"
            logger.warning("计划离场步骤 2/3 完成：库存已归零")

        if not self._scheduled_stop_flat:
            cancel_tpsl = getattr(self.ext, "cancel_tpsl", None)
            if not self.config.dry_run and callable(cancel_tpsl):
                try:
                    await cancel_tpsl(self.config.market)
                except Exception as exc:  # noqa: BLE001
                    self._log_scheduled_close_failure(
                        f"空仓后撤销 TPSL 异常：{exc}",
                        remaining,
                    )
                    return "计划停机：保护单清理失败，等待下轮重试"
            self._current_tpsl = None
            self._scheduled_stop_flat = True
            logger.warning(
                "计划离场步骤 3/3 完成：已停止开仓，库存为零；"
                "进程继续运行并输出计划停机心跳"
            )
        return "计划停机：窗口外，库存已归零，进程保持运行"

    async def run_once(self, include_slow: bool = True) -> str:
        # 未配置时只复位内存状态且不触碰文件；配置后每轮都以末行判活。
        self._refresh_hedge_interlock()
        if self._trading_window_enabled():
            if not self._is_trading_window_open():
                return await self._run_scheduled_stop_cycle()
            if self._scheduled_stop_active:
                logger.warning("交易窗口已恢复：无需重启，恢复网格维护与铺单")
                self._reset_scheduled_stop()
        self._write_budget = self.config.max_writes_per_round
        self._by_id_lookup_budget = self.config.max_by_id_lookups_per_round
        self._write_budget_exhausted_this_round = False
        await self._refresh_market_price()
        if self.config.wallet_exposure_ratio is not None:
            # legacy 与 trend-aware 都从这里刷新比例上限；trend-aware 随后的
            # 回撤检查会处理同一代缓存，不会增加第二次 balance 请求。
            await self._refresh_equity_cache()
        if self.config.trend_aware:
            return await self._run_once_trend_aware(include_slow=include_slow)

        # 指标 + regime
        candles = await self._candles.get_hourly_candles(
            self.config.market, self.config.candle_lookback
        )
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
        self._max_abs_inv_usd = max(self._max_abs_inv_usd, abs(inv_usd))
        logger.info("price=%.0f ADX=%s mode=%s inv=%s(≈$%.0f) 挂单=%d",
                    price, f"{a[-1]:.1f}" if a[-1] else None, mode.value, inv, inv_usd, len(self._orders))

        if mode is GridMode.OFF:
            return await self._go_off(inv)

        # 1) 互锁撤单优先；未配置时是纯内存空操作，不改变既有调用实参。
        await self._cancel_interlock_expanding_orders(inv_usd)
        # 2) 处理成交：已挂但盘口消失的单=成交，翻到反向一格
        await self._handle_fills(inv_usd)
        # 3) 维护阶梯：当前价上下各挂 N 格
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
            if self._by_id_lookup_budget <= 0:
                logger.debug("本轮按 ID 终态查询预算已耗尽，剩余订单下轮继续")
                break
            self._by_id_lookup_budget -= 1
            try:
                order = await order_getter(self.config.market, order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("按 ID 查询订单 %s 失败，下轮重试：%s", order_id, exc)
                continue
            if order is not None:
                statuses[order_id] = order
        return statuses

    def _queue_retry(
        self,
        level: int,
        side: Side,
        qty: Decimal | None,
        why: str,
        is_replacement: bool = False,
    ) -> dict:
        """保留同档既有重试次数，只刷新本次挂单意图。"""
        entry = self._retry.setdefault(
            level,
            {
                "side": side,
                "qty": qty,
                "why": why,
                "is_replacement": is_replacement,
                "attempts": 0,
                "exhausted": False,
                "requested_at": time.time(),
            },
        )
        entry["side"], entry["qty"], entry["why"] = side, qty, why
        entry["is_replacement"] = is_replacement
        entry.setdefault("attempts", 0)
        entry.setdefault("exhausted", False)
        entry.setdefault("requested_at", time.time())
        return entry

    async def _drain_retry(
        self,
        inv_usd: float,
        blocked_side: str | None,
    ) -> None:
        """完全 ACTIVE 时双源对账翻单意图，确认不存在后才重试。"""
        if blocked_side is not None or not self._retry:
            return

        try:
            open_snapshot = await self.ext.get_open_orders(self.config.market)
            hist_snapshot = await self.ext.get_orders_history(
                self.config.market,
                limit=100,
                order_type="LIMIT",
                sort="UPDATED_AT",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("翻单重试双源对账失败，本轮不重挂：%s", exc)
            return

        def field(order, *names):
            for name in names:
                value = (
                    order.get(name)
                    if isinstance(order, dict)
                    else getattr(order, name, None)
                )
                if value is not None:
                    return value
            return None

        def normalized(value) -> str:
            value = getattr(value, "value", value)
            return str(value or "").upper().rsplit(".", 1)[-1]

        def close_decimal(left: Decimal, right: Decimal) -> bool:
            tolerance = max(
                Decimal("1e-12"),
                abs(right) * Decimal("1e-9"),
            )
            return abs(left - right) <= tolerance

        def within_request_window(order, requested_at: float) -> bool:
            if requested_at <= 0:
                return True
            raw = field(
                order,
                "created_at",
                "created_time",
                "timestamp",
                "updated_at",
                "updated_time",
            )
            if raw is None:
                # 旧桩或旧 SDK 没暴露时间字段时，仍由三元匹配保护。
                return True
            if isinstance(raw, datetime):
                order_time = raw.timestamp()
            else:
                try:
                    order_time = float(raw)
                except (TypeError, ValueError):
                    try:
                        order_time = datetime.fromisoformat(
                            str(raw).replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        return True
                while order_time > 100_000_000_000:
                    order_time /= 1000
            return (
                order_time + _RETRY_RECONCILE_CLOCK_SKEW_SECONDS
                >= requested_at
            )

        price_rounder = getattr(self.ext, "round_price", None)
        amount_rounder = getattr(self.ext, "round_amount", None)

        async def aligned_price(value) -> Decimal:
            result = Decimal(str(value))
            if callable(price_rounder):
                result = await price_rounder(self.config.market, result)
            return Decimal(str(result))

        async def aligned_amount(value) -> Decimal:
            result = Decimal(str(value))
            if callable(amount_rounder):
                result = await amount_rounder(self.config.market, result)
            return Decimal(str(result))

        async def matches(order, entry, target_price, target_qty) -> bool:
            if normalized(field(order, "side")) != entry["side"].value:
                return False
            raw_price = field(order, "price")
            raw_qty = field(
                order,
                "qty",
                "amount",
                "quantity",
                "size",
                "amount_of_synthetic",
                "filled_qty",
            )
            if raw_price is None or raw_qty is None:
                return False
            candidate_price = await aligned_price(raw_price)
            candidate_qty = await aligned_amount(raw_qty)
            return (
                close_decimal(candidate_price, target_price)
                and close_decimal(candidate_qty, target_qty)
                and within_request_window(
                    order,
                    float(entry.get("requested_at", 0) or 0),
                )
            )

        terminal_statuses = {
            "FILLED",
            "CANCELLED",
            "CANCELLED-POST-ONLY",
            "CANCELLED-SELF-TRADE",
            "EXPIRED",
            "REJECTED",
        }
        for level, entry in list(self._retry.items()):
            if entry.get("exhausted"):
                continue
            side = entry["side"]
            price = self._level_price(level)
            qty = entry.get("qty")
            if qty is None:
                qty = Decimal(str(self.config.unit_usd)) / price
            try:
                target_price = await aligned_price(price)
                target_qty = await aligned_amount(qty)
                open_matches = [
                    order
                    for order in open_snapshot
                    if await matches(order, entry, target_price, target_qty)
                ]
                hist_matches = [
                    order
                    for order in hist_snapshot
                    if await matches(order, entry, target_price, target_qty)
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning("格%d 翻单精度对齐失败，本轮保留队列：%s", level, exc)
                continue

            if len(open_matches) > 1 or len(hist_matches) > 1:
                logger.critical(
                    "格%d 翻单双源对账命中多笔：open=%d history=%d；"
                    "保留队列等待人工处理",
                    level,
                    len(open_matches),
                    len(hist_matches),
                )
                continue

            if len(open_matches) == 1:
                order = open_matches[0]
                self._orders[level] = {"id": field(order, "id"), "side": side}
                self._counters["orphan_adopted"] += 1
                self._retry.pop(level, None)
                logger.warning(
                    "格%d 翻单在盘口找到既有订单 %s，已接管且不重挂",
                    level,
                    field(order, "id"),
                )
                continue

            if len(hist_matches) == 1:
                order = hist_matches[0]
                status = normalized(field(order, "status"))
                if status in terminal_statuses:
                    self._retry.pop(level, None)
                    logger.warning(
                        "格%d 翻单在历史中找到终态订单 %s（%s），已闭环且不重挂",
                        level,
                        field(order, "id"),
                        status,
                    )
                else:
                    logger.warning(
                        "格%d 翻单在历史中命中非终态订单 %s（%s），"
                        "本轮保留队列不重挂",
                        level,
                        field(order, "id"),
                        status or "未知",
                    )
                continue

            # 库存上限是正常风控，既不下单也不消耗重试次数。
            if not self._within_cap(side, inv_usd):
                self._log_cap_rejection(level, side, inv_usd, entry.get("why", ""))
                continue
            if self._write_budget <= 0:
                logger.debug("本轮写请求预算已耗尽，剩余翻单重试下轮继续")
                break
            placed = await self._place(
                level,
                side,
                inv_usd,
                why=entry.get("why", ""),
                qty=entry.get("qty"),
                retrying=True,
                is_replacement=bool(entry.get("is_replacement", False)),
            )
            if placed is not None:
                self._retry.pop(level, None)
                continue
            if level not in self._retry:
                # 市价侧校验主动丢弃了重试意图，不应计作一次失败。
                continue
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            if entry["attempts"] >= _RETRY_MAX_ATTEMPTS:
                entry["exhausted"] = True
                logger.critical(
                    "格%d 翻单已连续重试 %d 次失败；保留意图并暂停自动重试，"
                    "请人工处理",
                    level,
                    entry["attempts"],
                )

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
        self._max_abs_inv_usd = max(self._max_abs_inv_usd, abs(inv_usd))
        self._advance_cancel_backoffs()
        if self.config.dry_run:
            logger.debug("dry_run：跳过成交终态处理")
            return
        await self._drain_retry(inv_usd, blocked_side)
        open_ids = await self._open_ids()
        if len(self._orders) >= 3 and not any(
            rec["id"] in open_ids for rec in self._orders.values()
        ):
            self._counters["bad_snapshot"] += 1
            logger.warning(
                "本地跟踪的 %d 张订单均未出现在盘口，疑似坏快照；"
                "继续查询历史终态",
                len(self._orders),
            )
        for lv, rec in self._orders.items():
            if rec["id"] in open_ids:
                # 订单重新出现在盘口时解除终态待确认标记。
                was_pending = bool(rec.get("status_pending"))
                rec.pop("status_pending", None)
                rec.pop("status_lookup_attempts", None)
                if was_pending:
                    logger.info(
                        "格%d 订单 %s 已重新出现在盘口，解除终态待确认",
                        lv,
                        rec["id"],
                    )
                else:
                    logger.debug("格%d 订单 %s 仍在盘口，不处理", lv, rec["id"])
        missing = {lv: rec for lv, rec in self._orders.items() if rec["id"] not in open_ids}
        if not missing:
            logger.debug("成交检查完成：没有从盘口消失的跟踪单")
            return
        statuses = await self._order_statuses({rec["id"] for rec in missing.values()})
        cancelled_statuses = {
            "CANCELLED",
            "CANCELLED-POST-ONLY",
            "CANCELLED-SELF-TRADE",
        }
        terminal_statuses = {"FILLED", "EXPIRED", "REJECTED"} | cancelled_statuses
        for lv, rec in missing.items():
            o = statuses.get(rec["id"])
            status = str(getattr(o, "status", "") or "").upper() if o else ""
            filled = Decimal(str(getattr(o, "filled_qty", 0) or 0)) if o else Decimal("0")
            if status not in terminal_statuses:
                attempts = int(rec.get("status_lookup_attempts", 0)) + 1
                rec["status_lookup_attempts"] = attempts
                rec["status_pending"] = True
                if attempts == 1:
                    self._counters["terminal_unknown"] += 1
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
                elif attempts == 1:
                    logger.warning(
                        "格%d 订单 %s 终态=%s，保留记录下轮重试",
                        lv,
                        rec["id"],
                        status or "未知",
                    )
                else:
                    logger.debug(
                        "格%d 订单 %s 终态仍为%s，已重试 %d 轮；继续冻结该格",
                        lv,
                        rec["id"],
                        status or "未知",
                        attempts,
                    )
                continue

            is_fill = status == "FILLED" or (
                status in ({"EXPIRED"} | cancelled_statuses) and filled > 0
            )
            needs_write = is_fill or status in {
                "EXPIRED",
                "CANCELLED",
                "CANCELLED-SELF-TRADE",
            }
            if needs_write and self._write_budget <= 0:
                rec["status_pending"] = True
                logger.debug(
                    "格%d 订单 %s 已确认终态=%s，但本轮写请求预算已耗尽；"
                    "保留记录下轮处理",
                    lv,
                    rec["id"],
                    status,
                )
                continue

            self._orders.pop(lv, None)
            if rec.get("reduce_only"):
                if is_fill:
                    self._counters["fill_detected"] += 1
                    self._reset_cancel_state(lv, reason="成交")
                elif status not in cancelled_statuses:
                    self._reset_cancel_state(lv, reason=status)
                if status == "REJECTED":
                    self._counters["rejected_terminal"] += 1
                logger.info("格%d reduce-only 减仓单 %s 终态=%s、filled_qty=%s，闭环不翻单",
                            lv, rec["id"], status, filled)
                continue
            if is_fill:
                self._reset_cancel_state(lv, reason="成交")
                self._counters["fill_detected"] += 1
                raw_fill_price = (
                    getattr(o, "average_price", None)
                    or getattr(o, "filled_price", None)
                    or getattr(o, "fill_price", None)
                )
                fill_price = (
                    Decimal(str(raw_fill_price))
                    if raw_fill_price is not None
                    else None
                )
                if fill_price is not None and filled > 0:
                    append_fill(
                        Path(self.config.state_path).parent / "fills.jsonl",
                        build_fill_record(
                            fill_id=str(rec["id"]),
                            ts=time.time(),
                            # 闭环键取较低格：BUY@lv 与 SELL@(lv+1) 属同一闭环
                            level=lv if rec["side"] is Side.BUY else lv - 1,
                            side=rec["side"].value,
                            price=fill_price,
                            qty=filled,
                            engine_run_id=self._engine_run_id,
                        ),
                    )
                previous_fill = self._loop_fills.get(lv)
                unmatched_fill_qty = filled
                if (
                    previous_fill is not None
                    and previous_fill["side"] is not rec["side"]
                    and fill_price is not None
                    and filled > 0
                ):
                    self._loop_fills.pop(lv, None)
                    buy_price = (
                        fill_price
                        if rec["side"] is Side.BUY
                        else previous_fill["price"]
                    )
                    sell_price = (
                        fill_price
                        if rec["side"] is Side.SELL
                        else previous_fill["price"]
                    )
                    loop_qty = min(filled, previous_fill["qty"])
                    self._realized_pnl_net += (sell_price - buy_price) * loop_qty
                    self._closed_loops += 1
                    previous_remaining = previous_fill["qty"] - loop_qty
                    if previous_remaining > 0:
                        self._loop_fills[lv] = {
                            **previous_fill,
                            "qty": previous_remaining,
                        }
                    unmatched_fill_qty = filled - loop_qty
                if rec["side"] is Side.BUY:      # 买成交 → 上一格挂卖止盈
                    next_level, next_side = lv + 1, Side.SELL
                    fill_why = f"{lv}买成交→挂卖"
                else:                             # 卖成交 → 下一格挂买
                    next_level, next_side = lv - 1, Side.BUY
                    fill_why = f"{lv}卖成交→挂买"
                logger.info(
                    "格%d 订单 %s 终态=%s、filled_qty=%s，判定为成交；"
                    "计划格%d %s 翻单",
                    lv,
                    rec["id"],
                    status,
                    filled,
                    next_level,
                    next_side.value,
                )
                if fill_price is not None and unmatched_fill_qty > 0:
                    existing_fill = self._loop_fills.get(next_level)
                    if (
                        existing_fill is not None
                        and existing_fill["side"] is rec["side"]
                    ):
                        merged_qty = existing_fill["qty"] + unmatched_fill_qty
                        self._loop_fills[next_level] = {
                            "side": rec["side"],
                            "price": (
                                existing_fill["price"] * existing_fill["qty"]
                                + fill_price * unmatched_fill_qty
                            )
                            / merged_qty,
                            "qty": merged_qty,
                        }
                    else:
                        self._loop_fills[next_level] = {
                            "side": rec["side"],
                            "price": fill_price,
                            "qty": unmatched_fill_qty,
                        }
                if self._side_is_blocked(next_side, blocked_side):
                    logger.info(
                        "格%d 成交后的 %s 翻单被当前冻结方向 %s 阻止，已进入重试队列",
                        lv,
                        next_side.value,
                        blocked_side,
                    )
                    qty = filled if filled > 0 else None
                    self._queue_retry(
                        next_level,
                        next_side,
                        qty,
                        fill_why,
                        is_replacement=True,
                    )
                    continue
                qty = filled if filled > 0 else None
                await self._place(
                    next_level,
                    next_side,
                    inv_usd,
                    why=fill_why,
                    qty=qty,
                    is_replacement=True,
                )
            elif status == "CANCELLED-POST-ONLY":
                self._record_cancelled(lv)
                logger.warning(
                    "格%d 订单 %s 因 post-only 冲突取消且无成交，"
                    "不执行原格重挂",
                    lv,
                    rec["id"],
                )
            elif status in ({"EXPIRED"} | cancelled_statuses):
                if status == "EXPIRED":
                    self._reset_cancel_state(lv, reason="过期")
                    entered_backoff = False
                else:
                    entered_backoff = self._record_cancelled(lv)
                logger.info(
                    "格%d 订单 %s 终态=%s 且无成交，准备原格 %s 重挂",
                    lv,
                    rec["id"],
                    status,
                    rec["side"].value,
                )
                if entered_backoff:
                    continue
                if self._side_is_blocked(rec["side"], blocked_side):
                    logger.info(
                        "格%d %s 原格重挂被当前冻结方向 %s 阻止",
                        lv,
                        rec["side"].value,
                        blocked_side,
                    )
                    continue
                await self._place(lv, rec["side"], inv_usd, why=f"格{lv}{status}重挂")
            else:
                self._reset_cancel_state(lv, reason="被拒")
                self._counters["rejected_terminal"] += 1
                logger.warning("格%d 订单 %s 终态=REJECTED，仅移除不翻单", lv, rec["id"])

    async def _cancel_interlock_expanding_orders(self, inv_usd: float) -> None:
        """互锁生效后立即撤掉仍可能扩大敞口的已跟踪挂单。"""
        if not self._hedge_interlock_active:
            return
        for level, record in list(self._orders.items()):
            if record.get("status_pending"):
                continue
            raw_qty = record.get("qty")
            notional = (
                float(Decimal(str(raw_qty)) * self._level_price(level))
                if raw_qty is not None
                else float(self.config.unit_usd)
            )
            if self._hedge_interlock_allows(record["side"], inv_usd, notional):
                continue
            await self._cancel(level, why="对冲互锁撤扩大敞口挂单")

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
                if not self.config.dry_run and self._write_budget <= 0:
                    logger.debug("本轮写请求预算已耗尽，剩余陈单下轮继续撤")
                    break
                await self._cancel(lv, why="撤陈单/越界单/冻结侧单")

        # 按距离从近到远交替补 BUY/SELL，剩余档位由下一轮继续补齐。
        for distance in range(1, n + 1):
            levels = (
                (center - distance, Side.BUY, "补买格"),
                (center + distance, Side.SELL, "补卖格"),
            )
            for lv, side, why in levels:
                if not self.config.dry_run and self._write_budget <= 0:
                    logger.debug("本轮写请求预算已耗尽，剩余档位下轮继续补")
                    return "；".join(acted) if acted else "阶梯已就位"
                if lv not in self._orders and allowed(lv, side):
                    m = await self._place(lv, side, inv_usd, why=why)
                    if m:
                        acted.append(m)
        return "；".join(acted) if acted else "阶梯已就位"

    async def _maintain_recovery_ladder(
        self,
        price: float,
        signed_size: Decimal,
        inv_usd: float,
    ) -> str | None:
        state = self._state
        signed_size = Decimal(str(signed_size))
        if (
            state is None
            or not state.frozen
            or not self._has_valid_band(state)
            or state.blocked_side not in ("BUY", "SELL")
            or signed_size == 0
        ):
            return None
        unit = Decimal(str(self.config.unit_usd))
        if unit <= 0 or self.config.levels_per_side <= 0:
            return None

        side = Side.SELL if signed_size > 0 else Side.BUY
        direction = 1 if signed_size > 0 else -1
        remaining = abs(signed_size)
        levels = min(
            self.config.levels_per_side,
            math.ceil(float((remaining * Decimal(str(price))) / unit)),
        )
        center = self._price_level(price)
        acted: list[str] = []

        for distance in range(1, levels + 1):
            if remaining <= 0:
                break
            level = center + direction * distance
            existing = self._orders.get(level)
            if existing is not None:
                if existing.get("reduce_only") and existing["side"] is side:
                    existing_qty = Decimal(str(existing.get("qty") or 0))
                    remaining -= min(remaining, max(Decimal("0"), existing_qty))
                continue
            if not self.config.dry_run and self._write_budget <= 0:
                logger.debug("本轮写请求预算已耗尽，剩余 FROZEN 减仓阶梯下轮继续")
                break
            level_price = self._level_price(level)
            qty = remaining if distance == levels else min(remaining, unit / level_price)
            placed = await self._place(
                level,
                side,
                inv_usd,
                why="FROZEN减仓",
                qty=qty,
                reduce_only=True,
            )
            if placed is None:
                if not self.config.dry_run and self._write_budget <= 0:
                    break
                continue
            remaining -= qty
            acted.append(placed)
        return "；".join(acted) if acted else None

    def _log_cap_equity_fallback(self, reason: str) -> None:
        """比例保护降级按状态聚合，避免每轮重复输出 WARNING。"""
        previous = self._cap_equity_fallback_state
        count = int(previous["count"]) + 1 if previous is not None else 1
        self._cap_equity_fallback_state = {
            "count": count,
            "reason": reason,
        }
        log = (
            logger.warning
            if count == 1 or count % _CAP_REJECTION_SUMMARY_EVERY == 0
            else logger.debug
        )
        log(
            "库存权益比例保护当前未生效：%s；已退回绝对硬顶 $%.2f；"
            "同类累计=%d",
            reason,
            self.config.max_inventory_usd,
            count,
        )

    def _effective_inventory_cap(self) -> tuple[float, str]:
        """返回当前库存上限及来源；权益不可用时安全退回绝对硬顶。"""
        hard_cap = self.config.max_inventory_usd
        ratio = self.config.wallet_exposure_ratio
        if ratio is None:
            return hard_cap, "绝对硬顶（未配置权益比例）"

        equity = self._last_equity_seen
        if equity is None or equity <= 0:
            self._log_cap_equity_fallback("共享权益缓存中没有有效权益")
            return hard_cap, "绝对硬顶（权益不可用降级）"

        ratio_cap = float(Decimal(str(equity)) * Decimal(str(ratio)))
        if ratio_cap < hard_cap:
            return (
                ratio_cap,
                f"权益比例（权益=${equity:.2f} × {ratio:g}x）",
            )
        return (
            hard_cap,
            f"绝对硬顶（权益比例计算值=${ratio_cap:.2f}）",
        )

    def _cap_components(
        self,
        side: Side,
        inv_usd: float,
    ) -> tuple[float, float, float, float, str]:
        """返回库存上限算式及来源，供判断与日志共享。"""
        unit = self.config.unit_usd
        inventory_usd = inv_usd if side is Side.BUY else -inv_usd
        pending_usd = sum(unit for rec in self._orders.values()
                          if rec["side"] is side and not rec.get("reduce_only"))
        cap, source = self._effective_inventory_cap()
        return inventory_usd, pending_usd, unit, cap, source

    def _within_cap(self, side: Side, inv_usd: float) -> bool:
        """严格库存上限：真实持仓与该侧挂单全部成交后仍不超上限。"""
        inventory_usd, pending_usd, unit, cap, _ = self._cap_components(
            side,
            inv_usd,
        )
        return inventory_usd + pending_usd + unit <= cap

    def _log_cap_rejection(
        self,
        level: int,
        side: Side,
        inv_usd: float,
        why: str,
    ) -> None:
        """库存拒绝按状态变化记录，重复事件只做 DEBUG/整百次聚合。"""
        inventory_usd, pending_usd, unit, cap, source = self._cap_components(
            side,
            inv_usd,
        )
        projected = inventory_usd + pending_usd + unit
        signature = (inventory_usd, pending_usd, unit, cap, source, why)
        previous = self._cap_rejection_state.get(side)
        repeated = previous is not None and previous["signature"] == signature
        count = int(previous["count"]) + 1 if repeated else 1
        self._cap_rejection_state[side] = {
            "signature": signature,
            "count": count,
        }
        log = (
            logger.info
            if not repeated or count % _CAP_REJECTION_SUMMARY_EVERY == 0
            else logger.debug
        )
        log(
            "库存上限拒绝挂单：格%d 方向=%s 原因=%s；"
            "inventory_usd=%.2f + pending_usd=%.2f + unit=%.2f"
            " = %.2f > effective_cap=%.2f；上限来源=%s；"
            "max_inventory_usd=%.2f（绝对硬顶）；同类累计=%d",
            level,
            side.value,
            why or "未说明",
            inventory_usd,
            pending_usd,
            unit,
            projected,
            cap,
            source,
            self.config.max_inventory_usd,
            count,
        )

    async def _place(
        self,
        level: int,
        side: Side,
        inv_usd: float,
        why: str = "",
        qty: Decimal | None = None,
        retrying: bool = False,
        is_replacement: bool = False,
        reduce_only: bool = False,
    ) -> str | None:
        if level in self._orders:
            self._counters["dup_skipped"] += 1
            logger.debug("格%d 已有跟踪单，跳过", level)
            return None
        # retrying=True 是 drain 的专用通道：只绕过 _retry 去重；
        # _orders 去重与 attempts 累加照常，绝不临时移除队列记录。
        if not retrying and level in self._retry:
            self._counters["dup_skipped"] += 1
            logger.debug("格%d 在重试队列中，普通补格跳过", level)
            return None
        backoff = self._cancel_backoff.get(level)
        if backoff is not None:
            logger.debug(
                "格%d 处于取消退避，剩余 %d 轮，跳过挂单",
                level,
                int(backoff.get("remaining_rounds", 0)),
            )
            return None
        cooldown = self._reject_cooldown.get(level)
        now = time.monotonic()
        if cooldown and now < cooldown["until"]:
            self._counters["reject_cooldown_skipped"] += 1
            logger.debug(
                "格%d 处于拒单冷却期，剩余 %.1f 秒，跳过挂单",
                level,
                cooldown["until"] - now,
            )
            return None
        lp = self._level_price(level)
        qty = qty if qty is not None else Decimal(str(self.config.unit_usd)) / lp
        order_notional = float(qty * lp)
        if not self._hedge_interlock_allows(side, inv_usd, order_notional):
            projected = (
                inv_usd + order_notional
                if side is Side.BUY
                else inv_usd - order_notional
            )
            logger.info(
                "对冲互锁拒绝扩大敞口挂单：格%d 方向=%s 原因=%s；"
                "当前敞口=$%.2f，订单名义=$%.2f，预计敞口=$%.2f；互锁原因=%s",
                level,
                side.value,
                why or "未说明",
                inv_usd,
                order_notional,
                projected,
                self._hedge_interlock_reason,
            )
            return None
        if not reduce_only and not self._within_cap(side, inv_usd):
            self._log_cap_rejection(level, side, inv_usd, why)
            return None
        if not self._market_side_allows(level, side, lp):
            return None
        msg = f"{why}：{side.value} {qty:.6f}@{lp:.0f}(格{level}){' reduce-only' if reduce_only else ''}"

        def remember_failed() -> str:
            if reduce_only:
                return ""
            self._queue_retry(level, side, qty, why, is_replacement)
            return "，已进入重试队列"

        if self.config.dry_run:
            logger.info("[dry_run] %s", msg)
            self._reject_cooldown.pop(level, None)
            self._orders[level] = {"id": f"dry-{level}", "side": side, "reduce_only": reduce_only, "qty": qty}
            return msg
        if self._write_budget <= 0:
            if not self._write_budget_exhausted_this_round:
                self._counters["write_budget_exhausted_rounds"] += 1
                self._write_budget_exhausted_this_round = True
            logger.debug("本轮写请求预算已耗尽，跳过挂单：%s", msg)
            return None
        self._write_budget -= 1
        if self._write_budget == 0 and not self._write_budget_exhausted_this_round:
            self._counters["write_budget_exhausted_rounds"] += 1
            self._write_budget_exhausted_this_round = True
        try:
            res = await self.ext.place_limit_order(self.config.market, side, qty, lp,
                                                   reduce_only=reduce_only)
            data = getattr(res, "data", None)
            oid = getattr(data, "id", None) or getattr(res, "id", None)
            status = str(
                getattr(data, "status", None) or getattr(res, "status", "") or ""
            ).upper()
            if status.rsplit(".", 1)[-1] == "REJECTED":
                previous = self._reject_cooldown.get(level)
                count = int(previous["count"]) + 1 if previous else 1
                cooldown_seconds = min(30 * 2 ** min(count - 1, 4), 300)
                self._reject_cooldown[level] = {
                    "until": time.monotonic() + cooldown_seconds,
                    "count": count,
                }
                note = remember_failed()
                if is_replacement:
                    self._counters["replacement_failed"] += 1
                logger.warning(
                    "%s → 被拒(%s)%s",
                    msg,
                    getattr(data, "status_reason", None),
                    note,
                )
                return None
            if not oid:
                note = remember_failed()
                if is_replacement:
                    self._counters["replacement_failed"] += 1
                logger.warning("%s → 响应缺少订单 ID%s", msg, note)
                return None
            self._reject_cooldown.pop(level, None)
            self._orders[level] = {"id": oid, "side": side, "reduce_only": reduce_only, "qty": qty}
            if is_replacement:
                self._counters["replacement_placed"] += 1
            logger.info("%s → id=%s", msg, oid)
            return msg
        except Exception as exc:  # noqa: BLE001
            note = remember_failed()
            if is_replacement:
                self._counters["replacement_failed"] += 1
            logger.warning("挂单失败 %s：%s%s", msg, exc, note)
            return None

    async def _cancel(self, level: int, why: str = "") -> None:
        rec = self._orders.get(level)
        if not rec:
            return
        if self.config.dry_run:
            logger.info("[dry_run] %s：撤 格%d", why, level)
            self._orders.pop(level, None)
            return
        if self._write_budget <= 0:
            if not self._write_budget_exhausted_this_round:
                self._counters["write_budget_exhausted_rounds"] += 1
                self._write_budget_exhausted_this_round = True
            logger.debug("本轮写请求预算已耗尽，跳过撤单：格%d", level)
            return
        self._write_budget -= 1
        if self._write_budget == 0 and not self._write_budget_exhausted_this_round:
            self._counters["write_budget_exhausted_rounds"] += 1
            self._write_budget_exhausted_this_round = True
        try:
            await self.ext.cancel_order(self.config.market, rec["id"])
            self._orders.pop(level, None)  # 撤单成功后才删记录
        except Exception as exc:  # noqa: BLE001
            logger.warning("撤单失败 格%d：%s（保留记录下轮重试）", level, exc)

    def _halt_retry_due(self) -> bool:
        """收摊重试的统一节流闸。到点返回 True 并立即占用窗口。

        必须是唯一入口：halted 分支、硬止损、回撤熔断三条路径都会调用
        _go_off_confirmed，各自计时就会在同一分钟内叠加成撤单/API 风暴
        （实测 halted 下硬止损路径每轮都触发，绕过了原来只加在一处的限频）。
        """
        now = time.time()
        if now - self._last_halt_retry_ts < _HALT_RETRY_INTERVAL_S:
            return False
        self._last_halt_retry_ts = now
        return True

    def _peak_path(self) -> Path:
        return Path(self.config.state_path).parent / "equity_peak.json"

    def _load_equity_peak(self) -> float | None:
        """读回历史峰值权益。

        必须持久化：峰值只存内存的话，重启就重置，连续阴跌里每重启一次
        保护就失效一次——而实盘恰恰会因为网络问题反复重启。
        """
        if self._equity_peak is not None:
            return self._equity_peak
        try:
            data = json.loads(self._peak_path().read_text(encoding="utf-8"))
            peak = float(data.get("peak") or 0)
        except Exception:  # noqa: BLE001  缺失或损坏都当作没有历史峰值
            return None
        self._equity_peak = peak if peak > 0 else None
        return self._equity_peak

    def _save_equity_peak(self, peak: float) -> None:
        self._equity_peak = peak
        path = self._peak_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"peak": peak, "ts": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    @staticmethod
    def drawdown_breached(equity: float, peak: float, limit: float) -> bool:
        """纯判定：净值自峰值回撤是否达到熔断线。limit<=0 表示关闭。"""
        if limit <= 0 or peak <= 0 or equity <= 0:
            return False
        return (peak - equity) / peak >= limit

    async def _refresh_equity_cache(self) -> tuple[float | None, bool]:
        """按既有限频刷新共享权益缓存，返回（权益，本轮是否完成新查询）。"""
        ratio_enabled = self.config.wallet_exposure_ratio is not None
        drawdown_enabled = self.config.max_drawdown_pct > 0
        if not ratio_enabled and not drawdown_enabled:
            return self._last_equity_seen, False

        now = time.time()
        if now - self._last_equity_check_ts < _EQUITY_CHECK_INTERVAL_S:
            return self._last_equity_seen, False

        layer = "equity_drawdown" if drawdown_enabled else "wallet_exposure_cap"
        balance_getter = getattr(self.ext, "get_balance", None)
        if not self._has_risk_capability(layer, "get_balance"):
            self._last_equity_seen = None
            self._log_risk_capability_missing(layer, "get_balance")
            if ratio_enabled:
                self._log_cap_equity_fallback("适配器缺少 get_balance 能力")
            return None, False

        # 先记时间戳再发请求：失败也要走限频，否则网络差时会每轮重试、
        # 把 balance 查询压成 2.5 秒一次。
        self._last_equity_check_ts = now
        try:
            balance = await balance_getter()
        except Exception as exc:  # noqa: BLE001
            self._last_equity_seen = None
            if drawdown_enabled:
                # 查不到权益就不判定回撤，不可凭陈旧数据主动平仓。
                logger.warning("回撤熔断取权益失败，本次跳过：%s", exc)
            if ratio_enabled:
                self._log_cap_equity_fallback(f"权益查询失败：{exc}")
            return None, True

        raw_equity = (
            balance.get("equity")
            if isinstance(balance, dict)
            else getattr(balance, "equity", None)
        )
        try:
            equity = float(raw_equity) if raw_equity is not None else None
        except (TypeError, ValueError, OverflowError):
            equity = None
        if equity is None or not math.isfinite(equity) or equity <= 0:
            self._last_equity_seen = None
            if ratio_enabled:
                self._log_cap_equity_fallback(
                    f"权益值无效：{raw_equity!r}"
                )
            return None, True

        self._last_equity_seen = equity
        # 恢复后的再次失效是新的告警状态，不能沿用旧计数静默掉。
        self._cap_equity_fallback_state = None
        return equity, True

    async def _check_equity_drawdown(self) -> bool:
        """净值自峰值回撤熔断（跨腿保护），触发则全平停机。

        触发后走与硬止损相同的 _go_off_confirmed 落地路径，并持久化 halted，
        **不会自动恢复**——自动恢复会在同一段行情里反复触发、越平越亏。
        复位需人工处理 data/grid_state.json 的 halted 字段。

        这是一次性 kill switch，不是方向性冻结：它把两侧一起停掉并平仓，
        不存在"只冻结一侧导致成交后无法翻单"的失效模式（见 2026-08-10 事故）。
        """
        limit = self.config.max_drawdown_pct
        equity, refreshed = await self._refresh_equity_cache()
        if limit <= 0 or equity is None:
            return False
        if (
            not refreshed
            and self._last_equity_drawdown_processed_ts
            == self._last_equity_check_ts
        ):
            return False
        # 启动摘要可能已预热缓存；同一代权益仍要在首轮完成一次回撤判定，
        # 之后才按既有 60 秒节奏等待下一次新查询。
        self._last_equity_drawdown_processed_ts = self._last_equity_check_ts

        # 【曾经在这里用"权益跳变 ≥8% 判为出入金"来避免出金误熔断，已移除】
        # 那是个危险的猜测：真实的快速亏损同样会 ≥8%。实测 1000→900→810 两次
        # 10% 亏损会被逐次判成出入金、逐次重置峰值，熔断**永不触发**——
        # 比没有熔断更糟，因为它给出虚假的安全感。
        #
        # 现在的取舍很直白：宁可在出入金后误触发一次（平仓+停机+人工复位，
        # 损失小且可恢复），也不能让真实亏损绕过熔断（损失大且不可逆）。
        # 出入金后请人工删除 data/equity_peak.json 重新播种。
        # TODO: 交易所 /api/v1/user/assetOperations 提供真实资金流水，
        #       接入后可精确扣除出入金，届时才谈得上自动处理。
        peak = self._load_equity_peak()
        if peak is None or equity > peak:
            self._save_equity_peak(equity)
            return False

        if not self.drawdown_breached(equity, peak, limit):
            return False

        logger.critical(
            "触发净值回撤熔断：当前权益 %.2f，历史峰值 %.2f，回撤 %.2f%% ≥ 阈值 %.2f%%；"
            "将全平并停机，需人工复位",
            equity,
            peak,
            (peak - equity) / peak * 100,
            limit * 100,
        )
        try:
            pos = await self.ext.get_position(self.config.market)
            signed_size = pos.signed_size
        except Exception as exc:  # noqa: BLE001
            logger.exception("回撤熔断取仓位失败，仍强制停机：%s", exc)
            signed_size = Decimal("0")
        # _go_off_confirmed 在撤单/查仓/平仓任一失败时返回 False。绝不能忽略它——
        # 谎报"已熔断"会让残仓继续成交，而 halted 分支又提前 return，
        # 平仓将永不重试（2026-08-10 "冻结但库存卡死"的同类失效）。
        self._halt_retry_due()  # 占用节流窗口，避免本轮之后立刻又被重试一次
        flattened = await self._go_off_confirmed(signed_size)
        self._halt_settled = flattened
        if not flattened:
            logger.critical(
                "回撤熔断平仓链未完成（持仓=%s）；halted 已持久化，"
                "将按 %.0f 秒间隔持续重试直到收摊完成",
                signed_size,
                _HALT_RETRY_INTERVAL_S,
            )
        return True

    async def _check_hard_stop(
        self,
        *,
        signed_size: Decimal | None = None,
        mark: float | None = None,
        position=None,
    ) -> bool:
        """检查距强平价距离；可复用本轮已读取的仓位与 mark。"""
        pos = position
        position_liq_seen = False
        position_mark = None
        if pos is None and signed_size is None:
            try:
                pos = await self.ext.get_position(self.config.market)
            except Exception as exc:  # noqa: BLE001
                self._last_dist_to_liq = None
                logger.error(
                    "硬止损检查进入 fail-safe：无法取得仓位数据：%s",
                    exc,
                )
                return False
        if pos is not None:
            signed_size = pos.signed_size
        if signed_size is None:
            self._last_dist_to_liq = None
            logger.error("硬止损检查进入 fail-safe：无法取得仓位数据")
            return False
        signed_size = Decimal(str(signed_size))
        self._last_inv = signed_size
        if signed_size == 0:
            self._last_dist_to_liq = None
            return False

        dust_minimum = await self._dust_position_minimum(signed_size)
        if dust_minimum is not None:
            self._last_dist_to_liq = None
            return False

        if pos is not None:
            raw = getattr(pos, "raw", None)
            if raw is not None:
                raw_mark = getattr(raw, "mark_price", None)
                if raw_mark is not None and float(raw_mark) > 0:
                    position_mark = float(raw_mark)
                if hasattr(raw, "liquidation_price"):
                    position_liq_seen = True
                    raw_liq = getattr(raw, "liquidation_price", None)
                    raw_liq_float = float(raw_liq) if raw_liq is not None else None
                    self._last_liquidation_price = (
                        raw_liq_float
                        if raw_liq_float is not None and raw_liq_float > 0
                        else None
                    )
        liq = self._last_liquidation_price
        if liq is None and position_liq_seen:
            self._last_dist_to_liq = None
            if mark is not None:
                self._last_mark = float(mark)
            logger.debug(
                "当前仓位不存在强平价（仓位规模远小于权益），跳过硬止损判定：持仓=%s",
                signed_size,
            )
            return False
        if liq is None:
            liquidation_getter = getattr(
                self.ext,
                "get_liquidation_info",
                None,
            )
            if not self._has_risk_capability(
                "liquidation_constraints",
                "get_liquidation_info",
            ):
                self._last_dist_to_liq = None
                self._log_risk_capability_missing(
                    "liquidation_constraints",
                    "get_liquidation_info",
                )
                return False
            try:
                liquidation_info = await liquidation_getter(self.config.market)
            except Exception as exc:  # noqa: BLE001
                self._last_dist_to_liq = None
                logger.error(
                    "硬止损检查进入 fail-safe：当前有仓位 %s，但查询清算价失败：%s",
                    signed_size,
                    exc,
                )
                return False
            if liquidation_info is not None:
                liquidation_mark, liq = map(float, liquidation_info)
                if position_mark is None and liquidation_mark > 0:
                    position_mark = liquidation_mark
                self._last_liquidation_price = liq
        if liq is None:
            self._last_dist_to_liq = None
            logger.error(
                "硬止损检查进入 fail-safe：当前有仓位 %s，但无法取得清算价",
                signed_size,
            )
            return False
        if mark is None:
            try:
                mark = float(await self.ext.get_mark_price(self.config.market))
                if mark <= 0:
                    raise ValueError(f"实时 mark 非正数：{mark}")
            except Exception as exc:  # noqa: BLE001
                if position_mark is None or position_mark <= 0:
                    self._last_dist_to_liq = None
                    logger.error(
                        "硬止损检查进入 fail-safe：当前有仓位 %s，但无法取得 mark：%s",
                        signed_size,
                        exc,
                    )
                    return False
                mark = position_mark
                logger.warning(
                    "实时 mark 获取失败，回落到 position.mark_price=%s（可能陈旧）：%s",
                    mark,
                    exc,
                )

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
        # 已 halted 说明收摊在进行中，此时每轮再调一次平仓链只会造成撤单风暴；
        # 首次触发（尚未 halted）必须立刻执行，不受节流限制。
        already_halting = self._state is not None and self._state.halted
        if already_halting and not self._halt_retry_due():
            return True
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
        self.validate_risk_controls()
        self._running = True
        await self.connect()
        # 仅在显式启用比例时预热共享缓存；未配置比例的 legacy 路径不得因
        # 本变更新增 balance 请求。run_once 会命中同一限频周期，不会重查。
        if self.config.wallet_exposure_ratio is not None:
            await self._refresh_equity_cache()
        effective_cap, cap_source = self._effective_inventory_cap()
        logger.info(
            "库存上限状态：当前生效值=$%.2f；上限来源=%s；绝对硬顶=$%.2f",
            effective_cap,
            cap_source,
            self.config.max_inventory_usd,
        )
        logger.info("网格引擎启动（dry_run=%s，格距%.1f%%，每边%d格，库存硬顶$%.0f）",
                    self.config.dry_run, self.config.spacing_pct * 100,
                    self.config.levels_per_side, self.config.max_inventory_usd)
        next_fast = time.monotonic()
        last_slow = 0.0
        while self._running:
            started = time.monotonic()
            include_slow = (
                started - last_slow >= self.config.slow_interval
            )
            try:
                if self.config.trend_aware:
                    await self.run_once(include_slow=include_slow)
                else:
                    # 旧模式仍保持每轮完整执行，避免改变其既有语义。
                    await self.run_once()
                if include_slow:
                    last_slow = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                self._consecutive_failures += 1
                elapsed = time.monotonic() - started
                logger.exception("本轮异常（耗时 %.2f 秒）：%s", elapsed, exc)
                disconnected_seconds = time.time() - self._last_success_ts
                if (
                    disconnected_seconds >= self.config.slow_interval * 2
                    and not self._connectivity_critical
                ):
                    self._connectivity_critical = True
                    logger.critical(
                        "引擎连续失败 %d 轮，已失联 %.1f 分钟",
                        self._consecutive_failures,
                        disconnected_seconds / 60,
                    )
                try:
                    self._dump_connectivity()
                except Exception as snapshot_exc:  # noqa: BLE001
                    logger.exception("失败轮次写入 live 快照失败：%s", snapshot_exc)
            else:
                self._consecutive_failures = 0
                self._last_success_ts = time.time()
                self._connectivity_critical = False
                self._log_round_complete(time.monotonic() - started)
                try:
                    self._dump_connectivity()
                except Exception as snapshot_exc:  # noqa: BLE001
                    logger.exception("成功轮次更新 live 快照失败：%s", snapshot_exc)
            next_fast += self.config.poll_interval
            now = time.monotonic()
            if now - next_fast > self.config.poll_interval:
                # 丢弃已错过的轮次，避免故障恢复后连续空转追赶。
                next_fast = now
            await asyncio.sleep(max(0.0, next_fast - time.monotonic()))

    def stop(self) -> None:
        self._running = False
