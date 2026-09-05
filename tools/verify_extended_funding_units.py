"""只读校准 Extended ``funding_rate`` 的单位口径。

工具同时读取当前市场统计、SDK 提供的公开资金费率历史、当前持仓，以及账户
资金费结算流水。只有一条结算记录能与同一时点的公开费率和仓位名义配对时，
才会在三种候选口径中作出判定；证据不完整时只报告“无法校准”。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.verify_extended_funding_units
"""

from __future__ import annotations

# x10 必须在 CA 配置完成后才能导入。本模块把 x10 导入延迟到 _run，且在这里
# 先执行一次，保证直接导入工具模块也不会破坏该顺序。
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import ssl  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal, InvalidOperation  # noqa: E402
from enum import Enum  # noqa: E402
from statistics import median  # noqa: E402
from typing import Any  # noqa: E402

import certifi  # noqa: E402

_BASE_URL = "https://api.starknet.extended.exchange"
_MILLISECONDS_PER_HOUR = Decimal(60 * 60 * 1000)
_HOURS_PER_YEAR = Decimal(365 * 24)
_MAX_ACCEPTABLE_RELATIVE_ERROR = Decimal("0.20")
_MIN_SECOND_BEST_RELATIVE_ERROR = Decimal("0.50")
_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)

SettlementReader = Callable[..., Awaitable[Sequence[object]]]


class FundingUnit(str, Enum):
    """Extended 原始资金费率的候选单位。"""

    DECIMAL_PER_HOUR = "decimal_per_hour"
    DECIMAL_PER_8_HOURS = "decimal_per_8_hours"
    ANNUAL_DECIMAL = "annual_decimal"

    @property
    def label(self) -> str:
        """返回面向人工报告的中文名称。"""
        return {
            FundingUnit.DECIMAL_PER_HOUR: "小数/1小时",
            FundingUnit.DECIMAL_PER_8_HOURS: "小数/8小时",
            FundingUnit.ANNUAL_DECIMAL: "年化小数",
        }[self]


@dataclass(frozen=True)
class CalibrationEvidence:
    """一条已完成时点、费率、扣款和名义配对的校准证据。"""

    settlement_time_ms: int
    raw_rate: Decimal
    payment: Decimal
    position_notional: Decimal
    interval_hours: Decimal
    observed_period_rate: Decimal


@dataclass(frozen=True)
class CalibrationResult:
    """Extended 资金费单位校准结果。"""

    calibrated: bool
    unit: FundingUnit | None
    current_rate: Decimal
    latest_history_rate: Decimal | None
    evidence_count: int
    settlement_count: int
    has_open_position: bool
    missing: tuple[str, ...]
    warnings: tuple[str, ...]
    scores: tuple[tuple[FundingUnit, Decimal], ...]

    def pretty(self) -> str:
        """输出不会掩盖证据缺口的中文报告。"""
        lines = [
            "Extended 资金费单位只读校准",
            f"- market_statistics.funding_rate 当前值：{self.current_rate}",
        ]
        if self.latest_history_rate is None:
            lines.append("- 最近公开历史费率：无")
        else:
            lines.append(
                f"- 最近公开历史费率：{self.latest_history_rate}"
                "（仅作当期值配对比较，不单独证明单位）"
            )
        lines.append(f"- 账户资金费结算记录：{self.settlement_count} 条")
        lines.append(f"- 可用于单位判定的完整配对：{self.evidence_count} 条")

        if self.calibrated and self.unit is not None:
            lines.append(f"结论：已校准为「{self.unit.label}」。")
            for unit, score in self.scores:
                lines.append(f"- {unit.label} 中位相对误差：{score:.6f}")
        else:
            lines.append("结论：无法校准；不会用数值量级或经验推测填补。")
            if self.missing:
                lines.append("缺少的数据：")
                lines.extend(f"- {item}" for item in self.missing)

        lines.extend(f"⚠️ {warning}" for warning in self.warnings)
        return "\n".join(lines)


def remove_proxy_environment(
    environment: MutableMapping[str, str],
) -> tuple[str, ...]:
    """在任何 Extended 网络访问前移除常见代理环境变量。"""
    removed: list[str] = []
    for name in _PROXY_ENV_NAMES:
        if name in environment:
            environment.pop(name)
            removed.append(name)
    return tuple(removed)


def _value(record: object, *names: str) -> object | None:
    """兼容 SDK 模型和原始 JSON，读取第一个非空字段。"""
    for name in names:
        if isinstance(record, Mapping):
            value = record.get(name)
        else:
            value = getattr(record, name, None)
        if value not in (None, ""):
            return value
    return None


def _decimal(value: object, *, label: str) -> Decimal:
    """严格解析有限十进制数。"""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效十进制数：{value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{label} 必须是有限数")
    return result


def _timestamp_ms(value: object, *, label: str) -> int:
    """把秒或毫秒时间戳统一成毫秒。"""
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是整数时间戳：{value!r}") from exc
    if timestamp <= 0:
        raise ValueError(f"{label} 必须为正数")
    if timestamp < 100_000_000_000:
        timestamp *= 1000
    return timestamp


def _position_notional(record: object) -> Decimal | None:
    """从结算记录提取该结算时点的仓位名义；不使用当前仓位反推历史。"""
    direct = _value(
        record,
        "positionNotional",
        "position_notional",
        "positionValue",
        "position_value",
        "notional",
    )
    if direct not in (None, ""):
        notional = abs(_decimal(direct, label="结算时点仓位名义"))
        return notional if notional > 0 else None

    size = _value(record, "positionSize", "position_size", "size", "qty")
    price = _value(record, "markPrice", "mark_price", "price")
    if size in (None, "") or price in (None, ""):
        return None
    notional = abs(
        _decimal(size, label="结算时点仓位数量")
        * _decimal(price, label="结算时点价格")
    )
    return notional if notional > 0 else None


def _history_points(rate_history: Sequence[object]) -> list[tuple[int, Decimal]]:
    """解析并按时间排序公开资金费率历史。"""
    points: list[tuple[int, Decimal]] = []
    for index, record in enumerate(rate_history):
        timestamp = _timestamp_ms(
            _value(record, "timestamp", "time", "ts"),
            label=f"公开费率历史第 {index + 1} 条时间",
        )
        rate = _decimal(
            _value(record, "funding_rate", "fundingRate", "rate"),
            label=f"公开费率历史第 {index + 1} 条费率",
        )
        points.append((timestamp, rate))
    return sorted(points)


def _history_interval_hours(points: Sequence[tuple[int, Decimal]]) -> Decimal | None:
    """从相邻公开记录推导真实结算间隔，不硬编码一小时。"""
    gaps = [
        Decimal(current[0] - previous[0]) / _MILLISECONDS_PER_HOUR
        for previous, current in zip(points, points[1:])
        if current[0] > previous[0]
    ]
    return median(gaps) if gaps else None


def _candidate_period_rate(
    raw_rate: Decimal,
    interval_hours: Decimal,
    unit: FundingUnit,
) -> Decimal:
    """按候选单位计算一个实际结算间隔应收付的小数费率。"""
    if unit is FundingUnit.DECIMAL_PER_HOUR:
        return raw_rate * interval_hours
    if unit is FundingUnit.DECIMAL_PER_8_HOURS:
        return raw_rate * interval_hours / Decimal(8)
    return raw_rate * interval_hours / _HOURS_PER_YEAR


def _median_scores(
    evidence: Sequence[CalibrationEvidence],
) -> tuple[tuple[FundingUnit, Decimal], ...]:
    """计算每种候选口径相对真实扣款率的中位相对误差。"""
    scores: list[tuple[FundingUnit, Decimal]] = []
    for unit in FundingUnit:
        errors: list[Decimal] = []
        for item in evidence:
            expected = abs(
                _candidate_period_rate(item.raw_rate, item.interval_hours, unit)
            )
            if expected == 0:
                continue
            errors.append(abs(item.observed_period_rate - expected) / expected)
        if errors:
            scores.append((unit, median(errors)))
    return tuple(sorted(scores, key=lambda item: item[1]))


def evaluate_calibration(
    *,
    current_rate: Decimal,
    rate_history: Sequence[object],
    settlements: Sequence[object],
    has_open_position: bool,
) -> CalibrationResult:
    """配对只读证据并在三种候选单位中作出保守判定。"""
    current = _decimal(current_rate, label="当前 Extended funding_rate")
    points = _history_points(rate_history)
    interval_hours = _history_interval_hours(points)
    evidence: list[CalibrationEvidence] = []
    warnings: list[str] = []
    missing_notional = 0
    unmatched_settlements = 0

    if points and interval_hours is not None:
        maximum_pair_distance_ms = int(
            max(interval_hours, Decimal(1)) * _MILLISECONDS_PER_HOUR
        )
        for index, settlement in enumerate(settlements):
            try:
                settlement_time = _timestamp_ms(
                    _value(settlement, "paidTime", "paid_time", "timestamp", "ts"),
                    label=f"结算记录第 {index + 1} 条时间",
                )
                payment = _decimal(
                    _value(settlement, "fundingFee", "funding_fee", "fee", "amount"),
                    label=f"结算记录第 {index + 1} 条金额",
                )
                notional = _position_notional(settlement)
            except ValueError as exc:
                warnings.append(str(exc))
                continue
            if notional is None:
                missing_notional += 1
                continue
            nearest_time, nearest_rate = min(
                points,
                key=lambda point: abs(point[0] - settlement_time),
            )
            if abs(nearest_time - settlement_time) > maximum_pair_distance_ms:
                unmatched_settlements += 1
                continue
            observed_rate = abs(payment) / notional
            if nearest_rate == 0 or observed_rate == 0:
                warnings.append(
                    f"结算记录第 {index + 1} 条费率或扣款为零，不能识别单位"
                )
                continue
            evidence.append(
                CalibrationEvidence(
                    settlement_time_ms=settlement_time,
                    raw_rate=nearest_rate,
                    payment=payment,
                    position_notional=notional,
                    interval_hours=interval_hours,
                    observed_period_rate=observed_rate,
                )
            )

    scores = _median_scores(evidence)
    calibrated = False
    unit: FundingUnit | None = None
    if len(scores) >= 2:
        best_unit, best_error = scores[0]
        second_error = scores[1][1]
        if (
            best_error <= _MAX_ACCEPTABLE_RELATIVE_ERROR
            and second_error >= _MIN_SECOND_BEST_RELATIVE_ERROR
        ):
            calibrated = True
            unit = best_unit

    missing: list[str] = []
    if not calibrated:
        if not has_open_position:
            missing.append("当前账户无持仓；需要探针仓跨过至少一次资金费结算")
        if not settlements:
            missing.append("账户无资金费结算记录")
        if not points:
            missing.append("SDK 未返回公开资金费率历史")
        elif interval_hours is None:
            missing.append("公开费率历史不足两条，无法确定实际结算间隔")
        if missing_notional:
            missing.append(
                f"{missing_notional} 条结算记录缺少结算时点仓位名义或数量与价格"
            )
        if unmatched_settlements:
            missing.append(
                f"{unmatched_settlements} 条结算记录无法与同一时点公开费率配对"
            )
        if evidence and not calibrated:
            missing.append("现有完整配对未唯一支持三种候选口径中的一种")

    latest_history_rate = points[-1][1] if points else None
    return CalibrationResult(
        calibrated=calibrated,
        unit=unit,
        current_rate=current,
        latest_history_rate=latest_history_rate,
        evidence_count=len(evidence),
        settlement_count=len(settlements),
        has_open_position=has_open_position,
        missing=tuple(missing),
        warnings=tuple(warnings),
        scores=scores,
    )


async def collect_calibration(
    client: Any,
    *,
    settlement_reader: SettlementReader,
    market: str = "BTC-USD",
    lookback_hours: int = 72,
    settlement_limit: int = 200,
    now: datetime | None = None,
) -> CalibrationResult:
    """从 Extended 的只读端点收集证据并执行校准。"""
    if lookback_hours <= 0:
        raise ValueError("回看小时数必须为正数")
    if settlement_limit <= 0:
        raise ValueError("结算记录上限必须为正数")
    end_time = now or datetime.now(timezone.utc)
    if end_time.tzinfo is None:
        raise ValueError("当前时间必须带时区")
    start_time = end_time - timedelta(hours=lookback_hours)

    stats_response = await client._client.info.get_market_statistics(
        market_name=market
    )
    history_response = await client._client.info.get_funding_rates_history(
        market_name=market,
        start_time=start_time,
        end_time=end_time,
    )
    positions_response = await client._client.account.get_positions(
        market_names=[market]
    )
    settlements = await settlement_reader(market=market, limit=settlement_limit)

    stats = stats_response.data
    if stats is None:
        raise RuntimeError("Extended market_statistics 响应缺少 data")
    history = history_response.data or []
    positions = positions_response.data or []
    has_open_position = any(
        abs(_decimal(_value(position, "size", "qty") or 0, label="当前持仓数量"))
        > 0
        for position in positions
    )
    return evaluate_calibration(
        current_rate=_decimal(
            _value(stats, "funding_rate", "fundingRate"),
            label="market_statistics.funding_rate",
        ),
        rate_history=history,
        settlements=settlements,
        has_open_position=has_open_position,
    )


def _read_private_settlements(*, market: str, limit: int) -> list[object]:
    """读取 SDK 尚未封装的账户资金费结算端点；失败必须抛错。"""
    remove_proxy_environment(os.environ)
    api_key = os.environ.get("X10_API_KEY")
    if not api_key:
        raise RuntimeError("缺少环境变量 X10_API_KEY，无法读取资金费结算记录")
    query = urllib.parse.urlencode({"market": market, "limit": limit})
    request = urllib.request.Request(
        f"{_BASE_URL}/api/v1/user/funding/history?{query}",
        headers={
            "X-Api-Key": api_key,
            "User-Agent": "extended-funding-unit-verifier/1.0",
            "Accept": "application/json",
        },
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=25, context=context) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, Mapping):
        raise RuntimeError("Extended 资金费结算响应不是对象")
    records = payload.get("data")
    if not isinstance(records, list):
        raise RuntimeError("Extended 资金费结算响应缺少 data 列表")
    return records


async def fetch_extended_settlements(
    *, market: str, limit: int
) -> Sequence[object]:
    """异步包装账户资金费结算只读请求。"""
    return await asyncio.to_thread(
        _read_private_settlements,
        market=market,
        limit=limit,
    )


def _load_environment_without_proxy() -> tuple[str, ...]:
    """加载本地环境，并在加载前后都清除代理变量。"""
    removed = list(remove_proxy_environment(os.environ))
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    removed.extend(remove_proxy_environment(os.environ))
    return tuple(dict.fromkeys(removed))


async def _run(*, market: str, lookback_hours: int) -> int:
    """构造 Extended 客户端并执行一次只读校准。"""
    remove_proxy_environment(os.environ)
    ensure_ssl_cert()
    # 必须保持在 ensure_ssl_cert() 之后，避免 x10 初始化错误的 CA 环境。
    from adapters.extended_client import ExtendedClient

    client: ExtendedClient | None = None
    try:
        client = ExtendedClient.from_env()
        result = await collect_calibration(
            client,
            settlement_reader=fetch_extended_settlements,
            market=market,
            lookback_hours=lookback_hours,
        )
    except Exception as exc:  # noqa: BLE001 只读诊断需把缺失条件转成明确报告
        print("Extended 资金费单位只读校准")
        print("结论：无法校准；读取校准证据失败。")
        print(f"- {type(exc).__name__}: {exc}")
        return 2
    finally:
        if client is not None:
            await client.close()

    print(result.pretty())
    return 0 if result.calibrated else 2


def main(argv: Sequence[str] | None = None) -> None:
    """解析参数、剥离代理并运行只读校准。"""
    parser = argparse.ArgumentParser(
        description="只读校准 Extended funding_rate 单位，不会报价或下单"
    )
    parser.add_argument("--market", default="BTC-USD", help="Extended 市场名称")
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=72,
        help="公开费率历史回看小时数（默认 72）",
    )
    args = parser.parse_args(argv)
    removed = _load_environment_without_proxy()
    if removed:
        print(f"已在网络访问前移除代理环境变量：{', '.join(removed)}")
    raise SystemExit(
        asyncio.run(_run(market=args.market, lookback_hours=args.lookback_hours))
    )


if __name__ == "__main__":
    main()
