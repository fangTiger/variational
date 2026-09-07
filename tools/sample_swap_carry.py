"""每小时采集 XAU/XAUS carry 证据；只读，不会提交或接受订单。

默认执行一轮并追加写入 ``data/swap_carry_samples.jsonl``。传入
``--interval-seconds`` 后在当前进程内循环，适合临时前台观察；launchd 模板采用
每小时重新拉起一次 ``--once`` 的方式。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from infra.data_paths import data_dir
from typing import Any, TypeVar

from adapters.variational_client import (
    Session,
    SwapFundingRate,
    SwapFundingSnapshot,
    VariationalClient,
)
from engine.swap_carry import calculate_carry_returns, derive_perp_accrual_ratio
from engine.swap_trading_schedule import parse_trading_schedule
from tools.verify_funding_units import remove_proxy_environment


logger = logging.getLogger("sample_swap_carry")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = data_dir() / "swap_carry_samples.jsonl"
DEFAULT_QTY = Decimal("0.224")
DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_COST_BPS = Decimal("6")
# 这两个值是命令行默认规划口径，每轮都会原样写入记录；收益函数本身不含固定比例。
DEFAULT_HOLD_RATIO = Decimal("0.705")
DEFAULT_SWAP_RATIO = Decimal("0.5714285714285714285714285714")
DEFAULT_N_ROUNDTRIPS = 52
DEFAULT_N_REBALANCE = 4

T = TypeVar("T")


class _IntervalSecondsAction(argparse.Action):
    """设置循环间隔时关闭默认的单次模式。"""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        setattr(namespace, self.dest, values)
        setattr(namespace, "once", False)


def _utc_iso(value: datetime) -> str:
    """输出固定的 UTC ISO8601 字符串。"""
    if value.tzinfo is None:
        raise ValueError("时间戳必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: object, *, label: str, positive: bool = False) -> Decimal:
    """严格解析有限 Decimal，可选择要求正数。"""
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效十进制数") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} 必须为有限数")
    if positive and parsed <= 0:
        raise ValueError(f"{label} 必须大于 0")
    return parsed


def _error(source: str, exc: Exception) -> dict[str, str]:
    """把单一数据源异常转成可落盘结构。"""
    return {
        "source": source,
        "type": type(exc).__name__,
        "message": str(exc),
    }


async def _capture(
    source: str,
    operation: Callable[[], Awaitable[T]],
    errors: list[dict[str, str]],
) -> T | None:
    """执行一个只读数据源；失败时记错并允许本轮继续。"""
    try:
        return await operation()
    except Exception as exc:  # noqa: BLE001 单侧失败必须保留同轮其他证据
        errors.append(_error(source, exc))
        logger.warning("%s 读取失败：%s", source, exc)
        return None


def _instrument_record(
    metadata: object,
    underlying: str,
    instrument_type: str,
) -> Mapping[str, Any]:
    """从 supported_assets 精确选择某标的、某合约类型的真实记录。"""
    if not isinstance(metadata, Mapping):
        raise ValueError("supported_assets 响应不是对象")
    records = next(
        (
            value
            for key, value in metadata.items()
            if str(key).upper() == underlying.upper()
        ),
        None,
    )
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError(f"supported_assets 缺少 {underlying} 记录")
    for record in records:
        if (
            isinstance(record, Mapping)
            and str(record.get("instrument_type") or "") == instrument_type
        ):
            return record
    raise ValueError(f"supported_assets 缺少 {underlying} {instrument_type} 记录")


def _open_interest(record: Mapping[str, Any]) -> dict[str, str]:
    """读取真实 ``open_interest.long_open_interest/short_open_interest``。"""
    value = record.get("open_interest")
    if not isinstance(value, Mapping):
        raise ValueError("元数据缺少 open_interest")
    long_value = _decimal(value.get("long_open_interest"), label="多头 OI")
    short_value = _decimal(value.get("short_open_interest"), label="空头 OI")
    return {"long": str(long_value), "short": str(short_value)}


def _price(record: Mapping[str, Any], key: str) -> Decimal:
    """读取一个严格为正的元数据价格。"""
    return _decimal(record.get(key), label=f"元数据 {key}", positive=True)


def _basis(perp: Decimal, swap: Decimal) -> dict[str, str]:
    """计算 XAU 减 XAUS 的绝对基差及相对 XAUS 的 bp。"""
    absolute = perp - swap
    return {
        "absolute": str(absolute),
        "bps": str(absolute / swap * Decimal("10000")),
    }


def _quote(payload: object) -> dict[str, str]:
    """保留 RFQ 双边价格，并计算完整点差。"""
    if not isinstance(payload, Mapping):
        raise ValueError("RFQ 响应不是对象")
    bid = _decimal(payload.get("bid"), label="RFQ bid", positive=True)
    ask = _decimal(payload.get("ask"), label="RFQ ask", positive=True)
    if ask < bid:
        raise ValueError("RFQ ask 小于 bid")
    mid = (bid + ask) / Decimal(2)
    return {
        "bid": str(bid),
        "ask": str(ask),
        "full_spread_bps": str((ask - bid) / mid * Decimal("10000")),
        "approx_notional_usd": str(mid * DEFAULT_QTY),
    }


def _funding_rate(rate: SwapFundingRate) -> dict[str, object]:
    """把 swap 单方向费率转成 JSON 友好结构。"""
    return {
        "raw_rate": str(rate.raw_rate),
        "annual_percent": str(rate.normalized_annual_rate * Decimal(100)),
        "coverage_days": rate.coverage_days,
        "day_count_basis": rate.day_count_basis,
        "normalized_annual_rate": str(rate.normalized_annual_rate),
        "apply_time": _utc_iso(rate.apply_time),
        "observed_at": _utc_iso(rate.observed_at),
    }


def _swap_funding(snapshot: object) -> dict[str, object]:
    """把 get_swap_funding 的结构化结果转成 JSON。"""
    if not isinstance(snapshot, SwapFundingSnapshot):
        raise ValueError("get_swap_funding 返回了未知结构")
    upcoming = snapshot.upcoming
    latest = snapshot.latest_applied
    return {
        "long_rate": _funding_rate(upcoming.long_rate),
        "short_rate": _funding_rate(upcoming.short_rate),
        "apply_time": _utc_iso(upcoming.long_rate.apply_time),
        "coverage_days": upcoming.long_rate.coverage_days,
        "trade_date": upcoming.trade_date,
        "basis": upcoming.basis,
        "latest_applied": {
            "long_rate": _funding_rate(latest.long_rate),
            "short_rate": _funding_rate(latest.short_rate),
            "apply_time": _utc_iso(latest.long_rate.apply_time),
            "trade_date": latest.trade_date,
            "basis": latest.basis,
        },
        "observed_at": _utc_iso(snapshot.observed_at),
        "warnings": list(snapshot.warnings),
    }


def _xau_funding(payload: object) -> dict[str, object]:
    """解析永续资金费，同时完整保留 predicted 原始精度。"""
    if not isinstance(payload, Mapping):
        raise ValueError("XAU funding 响应不是对象")
    raw_value = payload.get("predicted_funding_rate")
    raw_rate = _decimal(raw_value, label="XAU predicted_funding_rate")
    try:
        interval = int(payload.get("funding_interval_s"))
    except (TypeError, ValueError) as exc:
        raise ValueError("XAU funding_interval_s 不是整数") from exc
    if interval <= 0:
        raise ValueError("XAU funding_interval_s 必须大于 0")
    next_time = payload.get("next_funding_time")
    if not isinstance(next_time, str) or not next_time:
        raise ValueError("XAU funding 响应缺少 next_funding_time")
    return {
        # 若接口给的是字符串，直接保存该字符串，避免 Decimal 重排其表示形式。
        "raw_rate": raw_value if isinstance(raw_value, str) else str(raw_rate),
        "annual_percent": str(raw_rate * Decimal(100)),
        "funding_interval_s": interval,
        "next_funding_time": next_time,
    }


def _duration_seconds(value: timedelta | None) -> int | None:
    """把整秒级时长转为整数；缺失保持 None。"""
    if value is None:
        return None
    return int(value.total_seconds())


def _schedule_payload(
    record: Mapping[str, Any],
    observed_at: datetime,
) -> tuple[dict[str, object], dict[str, object]]:
    """解析 XAUS 时段并输出状态与元数据覆盖新鲜度。"""
    result = parse_trading_schedule(
        record.get("trading_sessions"),
        record.get("trading_schedule"),
        record.get("market_status"),
        observed_at,
    )

    def optional_time(value: datetime | None) -> str | None:
        return _utc_iso(value) if value is not None else None

    sessions = record.get("trading_sessions")
    latest_close: datetime | None = None
    if isinstance(sessions, Sequence) and not isinstance(sessions, (str, bytes)):
        parsed_closes: list[datetime] = []
        for row in sessions:
            if not isinstance(row, Mapping):
                parsed_closes = []
                break
            raw_close = row.get("close")
            if not isinstance(raw_close, str):
                parsed_closes = []
                break
            try:
                parsed_closes.append(
                    datetime.fromisoformat(raw_close.replace("Z", "+00:00")).astimezone(
                        timezone.utc
                    )
                )
            except ValueError:
                parsed_closes = []
                break
        if parsed_closes:
            latest_close = max(parsed_closes)

    schedule = {
        "is_tradable": result.is_tradable,
        "next_close_at": optional_time(result.next_close_at),
        "next_open_at": optional_time(result.next_open_at),
        "time_until_close_seconds": _duration_seconds(result.time_until_close),
        "closure_duration_seconds": _duration_seconds(result.closure_duration),
        "reason": result.reason,
    }
    freshness = {
        "is_fresh": result.metadata_is_fresh,
        "latest_session_close_at": optional_time(latest_close),
        "coverage_horizon_seconds": (
            _duration_seconds(latest_close - observed_at)
            if latest_close is not None
            else None
        ),
    }
    return schedule, freshness


async def sample_once(
    client: Any,
    *,
    observed_at: datetime | None = None,
    qty: Decimal = DEFAULT_QTY,
    hold_ratio: Decimal = DEFAULT_HOLD_RATIO,
    swap_ratio: Decimal = DEFAULT_SWAP_RATIO,
    cost_bps: Decimal = DEFAULT_COST_BPS,
    n_roundtrips: int = DEFAULT_N_ROUNDTRIPS,
    n_rebalance: int = DEFAULT_N_REBALANCE,
) -> dict[str, object]:
    """采集一轮独立证据；任何单一读取失败都不会丢弃整轮。"""
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("observed_at 必须带时区")
    now = now.astimezone(timezone.utc)
    sample_qty = _decimal(qty, label="RFQ 数量", positive=True)
    errors: list[dict[str, str]] = []

    metadata = await _capture("metadata", client.get_supported_assets, errors)

    async def load_xau_funding() -> dict[str, object]:
        payload = await client.get_funding("XAU", "perpetual_rwa_future")
        return _xau_funding(payload)

    async def load_xaus_funding() -> dict[str, object]:
        payload = await client.get_swap_funding("XAUS")
        return _swap_funding(payload)

    async def load_quote(underlying: str) -> dict[str, str]:
        payload = await client.request_quote(underlying, "buy", sample_qty)
        parsed = _quote(payload)
        # 名义必须按本轮显式数量计算，不能让模块默认数量污染自定义采样。
        mid = (
            Decimal(parsed["bid"]) + Decimal(parsed["ask"])
        ) / Decimal(2)
        parsed["approx_notional_usd"] = str(mid * sample_qty)
        return parsed

    xau_funding = await _capture("xau_funding", load_xau_funding, errors)
    xaus_funding = await _capture("xaus_funding", load_xaus_funding, errors)
    xau_quote = await _capture(
        "xau_rfq", lambda: load_quote("XAU"), errors
    )
    xaus_quote = await _capture(
        "xaus_rfq", lambda: load_quote("XAUS"), errors
    )

    xau_record: Mapping[str, Any] | None = None
    xaus_record: Mapping[str, Any] | None = None
    if metadata is not None:
        try:
            xau_record = _instrument_record(metadata, "XAU", "perpetual_rwa_future")
        except Exception as exc:  # noqa: BLE001 单腿元数据失败不影响另一腿
            errors.append(_error("xau_metadata", exc))
        try:
            xaus_record = _instrument_record(metadata, "XAUS", "swap")
        except Exception as exc:  # noqa: BLE001 单腿元数据失败不影响另一腿
            errors.append(_error("xaus_metadata", exc))

    xau_oi: dict[str, str] | None = None
    xaus_oi: dict[str, str] | None = None
    basis: dict[str, object] | None = None
    market: dict[str, object] | None = None
    if xau_record is not None:
        try:
            xau_oi = _open_interest(xau_record)
        except Exception as exc:  # noqa: BLE001 字段失败须继续保留其他字段
            errors.append(_error("xau_open_interest", exc))
    if xaus_record is not None:
        try:
            xaus_oi = _open_interest(xaus_record)
        except Exception as exc:  # noqa: BLE001 字段失败须继续保留其他字段
            errors.append(_error("xaus_open_interest", exc))
        try:
            schedule, freshness = _schedule_payload(xaus_record, now)
            market = {
                "market_status": xaus_record.get("market_status"),
                "schedule": schedule,
                "metadata_freshness": freshness,
                "trading_schedule": xaus_record.get("trading_schedule"),
                "trading_sessions": xaus_record.get("trading_sessions"),
            }
        except Exception as exc:  # noqa: BLE001 时段解析失败仍落整轮
            errors.append(_error("xaus_trading_schedule", exc))
    if xau_record is not None and xaus_record is not None:
        try:
            basis = {
                "mark": _basis(_price(xau_record, "price"), _price(xaus_record, "price")),
                "index": _basis(
                    _price(xau_record, "index_price"),
                    _price(xaus_record, "index_price"),
                ),
            }
        except Exception as exc:  # noqa: BLE001 基差失败不影响费率证据
            errors.append(_error("basis", exc))

    carry: dict[str, object] = {
        "weekly_flat": None,
        "hold_through": None,
        "hold_ratio": str(hold_ratio),
        "swap_ratio": str(swap_ratio),
        "cost_bps": str(cost_bps),
        "n_roundtrips": n_roundtrips,
        "n_rebalance": n_rebalance,
    }
    perp_accrual_ratio: Decimal | None = None
    if xaus_record is not None:
        try:
            perp_accrual_ratio = derive_perp_accrual_ratio(
                xaus_record.get("trading_sessions"),
                period=timedelta(days=7),
            )
            carry["perp_accrual_ratio"] = str(perp_accrual_ratio)
        except Exception as exc:  # noqa: BLE001 收益参数失败不丢原始费率
            errors.append(_error("perp_accrual_ratio", exc))
    if (
        xau_funding is not None
        and isinstance(xaus_funding, Mapping)
        and perp_accrual_ratio is not None
    ):
        try:
            long_rate = xaus_funding["long_rate"]
            if not isinstance(long_rate, Mapping):
                raise ValueError("XAUS long_rate 结构无效")
            returns = calculate_carry_returns(
                f_perp=Decimal(str(xau_funding["raw_rate"])),
                f_swap=abs(Decimal(str(long_rate["normalized_annual_rate"]))),
                hold_ratio=hold_ratio,
                perp_accrual_ratio=perp_accrual_ratio,
                swap_ratio=swap_ratio,
                cost_bps=cost_bps,
                n_roundtrips=n_roundtrips,
                n_rebalance=n_rebalance,
            )
            carry.update(
                {
                    "weekly_flat": str(returns.weekly_flat),
                    "weekly_flat_annual_percent": str(
                        returns.weekly_flat * Decimal(100)
                    ),
                    "hold_through": str(returns.hold_through),
                    "hold_through_annual_percent": str(
                        returns.hold_through * Decimal(100)
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 收益失败不丢原始费率
            errors.append(_error("carry", exc))

    return {
        "observed_at": _utc_iso(now),
        "xau": {"funding": xau_funding, "open_interest": xau_oi},
        "xaus": {
            "funding": xaus_funding,
            "open_interest": xaus_oi,
            "market": market,
        },
        "basis": basis,
        "rfq": {
            "qty": str(sample_qty),
            "target_notional_usd": "1000",
            "xau": xau_quote,
            "xaus": xaus_quote,
        },
        "carry": carry,
        "errors": errors,
    }


def append_jsonl(path: Path, record: Mapping[str, object]) -> None:
    """以追加模式写入一条完整 JSONL 记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


async def run(args: argparse.Namespace) -> None:
    """运行单次或循环采样，并确保关闭客户端。"""
    from infra.runtime import ensure_ssl_cert

    ensure_ssl_cert()
    client = VariationalClient(Session.from_env())
    output = Path(args.output)
    try:
        while True:
            record = await sample_once(
                client,
                qty=args.qty,
                hold_ratio=args.hold_ratio,
                swap_ratio=args.swap_ratio,
                cost_bps=args.cost_bps,
                n_roundtrips=args.n_roundtrips,
                n_rebalance=args.n_rebalance,
            )
            append_jsonl(output, record)
            logger.info("已追加一轮 swap carry 采样：%s", output)
            if args.once:
                return
            await asyncio.sleep(args.interval_seconds)
    finally:
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001 关闭失败不影响已落盘证据
            logger.warning("关闭 Variational 客户端失败：%s", exc)


def _positive_decimal(value: str) -> Decimal:
    """argparse 使用的正 Decimal 转换器。"""
    try:
        return _decimal(value, label="参数", positive=True)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _ratio(value: str) -> Decimal:
    """argparse 使用的零到一比例转换器。"""
    try:
        parsed = _decimal(value, label="比例")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not Decimal(0) <= parsed <= Decimal(1):
        raise argparse.ArgumentTypeError("比例必须在 0 到 1 之间")
    return parsed


def _positive_seconds(value: str) -> int:
    """argparse 使用的正整数秒数转换器。"""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("间隔秒数必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("间隔秒数必须是正整数")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """构造只读采样器命令行参数。"""
    parser = argparse.ArgumentParser(description="只读采集 XAU/XAUS swap carry 证据")
    parser.set_defaults(once=True, interval_seconds=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true", help="采一轮后退出（默认）")
    parser.add_argument(
        "--interval-seconds",
        action=_IntervalSecondsAction,
        type=_positive_seconds,
        help="按指定秒数循环采样；传入即关闭 --once",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSONL 追加输出路径")
    parser.add_argument("--qty", type=_positive_decimal, default=DEFAULT_QTY, help="双腿 RFQ 数量")
    parser.add_argument(
        "--hold-ratio",
        type=_ratio,
        default=DEFAULT_HOLD_RATIO,
        help="weekly-flat 永续腿持仓比例",
    )
    parser.add_argument(
        "--swap-ratio",
        type=_ratio,
        default=DEFAULT_SWAP_RATIO,
        help="weekly-flat swap 腿计息比例",
    )
    parser.add_argument(
        "--cost-bps",
        type=_positive_decimal,
        default=DEFAULT_COST_BPS,
        help="双腿完整往返成本 bp（默认 6）",
    )
    parser.add_argument("--n-roundtrips", type=int, default=DEFAULT_N_ROUNDTRIPS)
    parser.add_argument("--n-rebalance", type=int, default=DEFAULT_N_REBALANCE)
    return parser


def _load_environment_without_proxy(
    environment: MutableMapping[str, str] = os.environ,
) -> tuple[str, ...]:
    """加载 .env，并在任何网络访问前清除全部代理变量。"""
    removed = list(remove_proxy_environment(environment))
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    removed.extend(remove_proxy_environment(environment))
    return tuple(dict.fromkeys(removed))


def main(argv: Sequence[str] | None = None) -> None:
    """解析参数、剥离代理并运行只读采样。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    removed = _load_environment_without_proxy()
    if removed:
        logger.info("已清除代理环境变量：%s", "、".join(removed))
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
