"""只读验证 Variational 资金费单位口径并输出中文报告。

本工具只访问 ``/transfers`` 与 ``/funding/v2``，不会报价或下单。它用已结算
``funding_rate`` 对照 365/360 天基准，并把当前 ``predicted_funding_rate`` 的
“年化小数”与旧“百分比/周期”两种解释放进历史区间比较。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.verify_funding_units
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

_SECONDS_PER_DAY = Decimal(24 * 60 * 60)
_SIX_DECIMALS = Decimal("0.000001")
_CLEAN_TOLERANCE = Decimal("1e-12")
_PAYMENT_ABS_TOLERANCE = Decimal("0.000002")
# 历史价格通常是展示精度；允许 1bp 相对误差，仍足以识别单位级别的错配。
_PAYMENT_REL_TOLERANCE = Decimal("0.0001")
_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)

TransferPageReader = Callable[[int, int], Awaitable[object]]


@dataclass(frozen=True)
class PaymentEvidence:
    """一条资金费流水推导出的扣款证据。"""

    notional_usdc: Decimal
    implied_price: Decimal
    expected_payment: Decimal
    payment_difference: Decimal
    payment_matches: bool
    sign_matches: bool
    used_observed_price: bool


@dataclass(frozen=True)
class PredictionComparison:
    """当前预测费率两种单位解释与历史区间的比较。"""

    annual_decimal_pct: Decimal
    old_period_pct_annualized_pct: Decimal
    historical_min_pct: Decimal
    historical_max_pct: Decimal
    annual_decimal_in_range: bool
    old_period_pct_in_range: bool


def _decimal(value: object, *, label: str) -> Decimal:
    """严格解析有限十进制数。"""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效十进制数：{value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{label} 必须是有限数")
    return result


def _positive_interval(value: object, *, label: str) -> int:
    """解析正整数结算周期。"""
    try:
        interval_s = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是整数：{value!r}") from exc
    if interval_s <= 0:
        raise ValueError(f"{label} 必须大于 0")
    return interval_s


def annualize_settled_rate(
    settled_rate: Decimal,
    interval_s: int,
    *,
    day_count: int = 365,
) -> Decimal:
    """把已结算单期小数费率换算为指定天数基准的年化小数。"""
    interval = _positive_interval(interval_s, label="资金费周期")
    if day_count <= 0:
        raise ValueError("年化天数基准必须大于 0")
    return settled_rate * (Decimal(day_count) * _SECONDS_PER_DAY / Decimal(interval))


def is_clean_six_decimal(value: Decimal) -> bool:
    """判断数值是否在误差内等于一个六位小数。"""
    return abs(value - value.quantize(_SIX_DECIMALS)) <= _CLEAN_TOLERANCE


def remove_proxy_environment(environment: MutableMapping[str, str]) -> tuple[str, ...]:
    """移除会让 Variational 请求从受限地区出口的代理变量。"""
    removed: list[str] = []
    for name in _PROXY_ENV_NAMES:
        if name in environment:
            environment.pop(name)
            removed.append(name)
    return tuple(removed)


def _nested_mappings(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """返回可能承载合约或价格字段的嵌套对象。"""
    result: list[Mapping[str, Any]] = [record]
    # 注意：/transfers 里承载合约信息的字段名是 reference_instrument，
    # 不是 ref_instrument（后者只在 ref_instrument_position_qty 这类扁平字段里出现）。
    # 写错这个键会让全部记录被静默跳过，报告变成空表。
    for key in (
        "reference_instrument",
        "ref_instrument",
        "instrument",
        "position_info",
        "price_info",
    ):
        value = record.get(key)
        if isinstance(value, Mapping):
            result.append(value)
            nested_instrument = value.get("instrument")
            if isinstance(nested_instrument, Mapping):
                result.append(nested_instrument)
    return result


def _first_value(record: Mapping[str, Any], names: Sequence[str]) -> object | None:
    """从流水及已知嵌套对象中读取第一个非空字段。"""
    for container in _nested_mappings(record):
        for name in names:
            value = container.get(name)
            if value not in (None, ""):
                return value
    return None


def _underlying(record: Mapping[str, Any]) -> str:
    """从流水结构中提取标的。"""
    value = _first_value(record, ("underlying",))
    if not isinstance(value, str) or not value.strip():
        raise ValueError("资金费流水缺少 underlying")
    return value.strip().upper()


def _interval_from_record(record: Mapping[str, Any]) -> int | None:
    """读取流水自带的结算周期；缺失时由当前 funding 响应补齐。"""
    value = _first_value(record, ("funding_interval_s",))
    if value in (None, ""):
        return None
    return _positive_interval(value, label="流水 funding_interval_s")


def _observed_price(record: Mapping[str, Any]) -> Decimal | None:
    """读取可独立校验名义的历史价格，不把流水资产价格误当标的价格。"""
    value = _first_value(
        record,
        ("ref_instrument_price", "underlying_price", "mark_price"),
    )
    if value in (None, ""):
        return None
    price = _decimal(value, label="流水历史价格")
    if price <= 0:
        raise ValueError("流水历史价格必须大于 0")
    return price


def infer_payment_evidence(record: Mapping[str, Any]) -> PaymentEvidence:
    """验证资金费扣款，并在缺少历史价格时报告反推的隐含价格。

    若流水含独立历史价格，则用 ``仓位数量 × 历史价格`` 计算名义并检查误差；
    若不含价格，则按需求用 ``abs(qty) / abs(funding_rate)`` 反推名义。后者只闭合
    数量级和隐含价格，不作为独立等式证据，调用方必须查看
    ``used_observed_price``。
    """
    payment = _decimal(record.get("qty"), label="资金费扣款")
    funding_rate = _decimal(record.get("funding_rate"), label="已结算单期费率")
    position_qty = _decimal(
        record.get("ref_instrument_position_qty"),
        label="结算参考仓位数量",
    )
    if funding_rate == 0:
        raise ValueError("已结算单期费率不能为 0")
    if position_qty == 0:
        raise ValueError("结算参考仓位数量不能为 0")

    payment_abs = abs(payment)
    rate_abs = abs(funding_rate)
    position_abs = abs(position_qty)
    implied_notional = payment_abs / rate_abs
    implied_price = implied_notional / position_abs
    observed_price = _observed_price(record)
    used_observed_price = observed_price is not None

    if observed_price is None:
        notional = implied_notional
        expected_payment = payment_abs
    else:
        notional = position_abs * observed_price
        expected_payment = rate_abs * notional

    difference = abs(payment_abs - expected_payment)
    tolerance = max(_PAYMENT_ABS_TOLERANCE, payment_abs * _PAYMENT_REL_TOLERANCE)
    expected_sign = -(funding_rate * position_qty)
    sign_matches = payment == 0 or (payment > 0) == (expected_sign > 0)
    return PaymentEvidence(
        notional_usdc=notional,
        implied_price=implied_price,
        expected_payment=expected_payment,
        payment_difference=difference,
        payment_matches=difference <= tolerance,
        sign_matches=sign_matches,
        used_observed_price=used_observed_price,
    )


async def fetch_all_transfers(fetch_page: TransferPageReader) -> list[Mapping[str, Any]]:
    """固定 ``limit=100`` 翻页读取全部 Variational 流水。"""
    limit = 100
    offset = 0
    object_count: int | None = None
    transfers: list[Mapping[str, Any]] = []

    while True:
        payload = await fetch_page(limit, offset)
        if not isinstance(payload, Mapping):
            raise ValueError("Variational 流水响应不是对象")
        result = payload.get("result")
        pagination = payload.get("pagination")
        if not isinstance(result, list):
            raise ValueError("Variational 流水响应缺少 result 数组")
        if not isinstance(pagination, Mapping):
            raise ValueError("Variational 流水响应缺少 pagination 对象")

        try:
            current_count = int(pagination.get("object_count"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Variational 流水总数不是整数") from exc
        if current_count < 0:
            raise ValueError("Variational 流水总数不能为负数")
        object_count = current_count if object_count is None else max(object_count, current_count)

        for index, transfer in enumerate(result):
            if not isinstance(transfer, Mapping):
                raise ValueError(f"Variational 第 {len(transfers) + index + 1} 条流水不是对象")
            transfers.append(transfer)

        if len(transfers) >= object_count:
            return transfers
        if not result:
            raise ValueError("Variational 流水未取满总数，分页无法继续推进")
        offset += limit


def filter_funding_transfers(
    transfers: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """筛出 funding_rate 非空的已结算资金费流水。"""
    return [row for row in transfers if row.get("funding_rate") not in (None, "")]


def compare_predicted_interpretations(
    predicted_rate: Decimal,
    interval_s: int,
    historical_annual_rates: Sequence[Decimal],
) -> PredictionComparison:
    """比较当前预测值的新旧解释分别是否落入历史已结算年化区间。"""
    if not historical_annual_rates:
        raise ValueError("缺少历史年化费率，无法比较当前预测")
    interval = _positive_interval(interval_s, label="当前资金费周期")
    historical_min_pct = min(historical_annual_rates) * 100
    historical_max_pct = max(historical_annual_rates) * 100
    annual_decimal_pct = predicted_rate * 100
    periods_per_year = Decimal(365) * _SECONDS_PER_DAY / Decimal(interval)
    # 旧口径把原始数字直接视为“百分比/周期”，因此乘周期数后单位仍为百分比。
    old_period_pct_annualized_pct = predicted_rate * periods_per_year
    return PredictionComparison(
        annual_decimal_pct=annual_decimal_pct,
        old_period_pct_annualized_pct=old_period_pct_annualized_pct,
        historical_min_pct=historical_min_pct,
        historical_max_pct=historical_max_pct,
        annual_decimal_in_range=(
            historical_min_pct <= annual_decimal_pct <= historical_max_pct
        ),
        old_period_pct_in_range=(
            historical_min_pct
            <= old_period_pct_annualized_pct
            <= historical_max_pct
        ),
    )


def _current_interval(
    current_funding: Mapping[str, Mapping[str, Any]],
    underlying: str,
) -> int | None:
    """从当前 funding 响应读取周期，供历史流水缺字段时使用。"""
    current = current_funding.get(underlying)
    if not isinstance(current, Mapping):
        return None
    value = current.get("funding_interval_s")
    if value in (None, ""):
        return None
    return _positive_interval(value, label=f"{underlying} 当前 funding_interval_s")


def _format_ratio(numerator: int, denominator: int) -> str:
    """格式化计数比例。"""
    ratio = Decimal(numerator) * 100 / Decimal(denominator) if denominator else Decimal(0)
    return f"{numerator}/{denominator}（{ratio:.1f}%）"


def _range_text(values: Sequence[Decimal]) -> str:
    """输出年化百分比的最小值、中位数和最大值。"""
    return (
        f"min={min(values) * 100:.6f}% / "
        f"中位={median(values) * 100:.6f}% / "
        f"max={max(values) * 100:.6f}%"
    )


def build_report(
    transfers: Sequence[Mapping[str, Any]],
    current_funding: Mapping[str, Mapping[str, Any]],
) -> str:
    """根据离线数据生成完整中文单位验证报告。"""
    funding_transfers = filter_funding_transfers(transfers)
    grouped: dict[str, list[tuple[Decimal, Decimal, PaymentEvidence | None]]] = {}
    skipped: list[str] = []

    for index, transfer in enumerate(funding_transfers, start=1):
        try:
            underlying = _underlying(transfer)
            interval_s = _interval_from_record(transfer) or _current_interval(
                current_funding,
                underlying,
            )
            if interval_s is None:
                raise ValueError("流水和当前 funding 响应都缺少结算周期")
            settled_rate = _decimal(
                transfer.get("funding_rate"),
                label="已结算单期费率",
            )
            annual_365 = annualize_settled_rate(settled_rate, interval_s, day_count=365)
            annual_360 = annualize_settled_rate(settled_rate, interval_s, day_count=360)
            try:
                evidence = infer_payment_evidence(transfer)
            except ValueError:
                evidence = None
            grouped.setdefault(underlying, []).append((annual_365, annual_360, evidence))
        except ValueError as exc:
            skipped.append(f"第 {index} 条：{exc}")

    evidence_rows = [
        evidence
        for rows in grouped.values()
        for _, _, evidence in rows
        if evidence is not None
    ]
    independent = [row for row in evidence_rows if row.used_observed_price]
    inferred = [row for row in evidence_rows if not row.used_observed_price]
    independent_matches = sum(row.payment_matches for row in independent)
    sign_matches = sum(row.sign_matches for row in evidence_rows)

    lines = [
        "Variational 资金费单位验证报告",
        "=" * 36,
        "",
        "一、流水与扣款证据",
        f"- 全部流水 {len(transfers)} 条；funding_rate 非空 {len(funding_transfers)} 条。",
        f"- 可计算扣款关系 {len(evidence_rows)} 条；方向符号一致 {_format_ratio(sign_matches, len(evidence_rows))}。",
        (
            f"- 含独立历史价格 {len(independent)} 条，abs(qty) ≈ abs(rate) × 名义 "
            f"成立 {_format_ratio(independent_matches, len(independent))}。"
        ),
        (
            f"- 缺少历史价格 {len(inferred)} 条：按 qty/funding_rate 反推名义与隐含价；"
            "这部分是数量级核对，不计作独立等式验证。"
        ),
    ]

    if evidence_rows:
        sample = evidence_rows[0]
        lines.append(
            f"- 示例反推：名义 {sample.notional_usdc:.3f} USDC，"
            f"隐含价格 {sample.implied_price:.2f}。"
        )
    if skipped:
        lines.append(f"- 因字段不足跳过 {len(skipped)} 条；首条原因：{skipped[0]}")

    lines.extend(["", "二、365 天与 360 天基准对照"])
    for underlying in sorted(grouped):
        rows = grouped[underlying]
        annual_365_values = [row[0] for row in rows]
        annual_360_values = [row[1] for row in rows]
        clean_365 = sum(is_clean_six_decimal(value) for value in annual_365_values)
        clean_360 = sum(is_clean_six_decimal(value) for value in annual_360_values)
        lines.extend(
            [
                f"- {underlying}：共 {len(rows)} 条",
                (
                    f"  365 天基准：六位小数还原 {_format_ratio(clean_365, len(rows))}；"
                    f"年化分布 {_range_text(annual_365_values)}"
                ),
                (
                    f"  360 天基准：六位小数还原 {_format_ratio(clean_360, len(rows))}；"
                    f"年化分布 {_range_text(annual_360_values)}"
                ),
            ]
        )

    lines.extend(["", "三、当前 /funding/v2 与历史区间对比"])
    for underlying in sorted(grouped):
        current = current_funding.get(underlying)
        if not isinstance(current, Mapping):
            lines.append(f"- {underlying}：当前 funding 读取失败或缺失，无法比较。")
            continue
        try:
            predicted = _decimal(
                current.get("predicted_funding_rate"),
                label=f"{underlying} 当前 predicted_funding_rate",
            )
            interval_s = _positive_interval(
                current.get("funding_interval_s"),
                label=f"{underlying} 当前 funding_interval_s",
            )
            historical = [row[0] for row in grouped[underlying]]
            comparison = compare_predicted_interpretations(
                predicted,
                interval_s,
                historical,
            )
        except ValueError as exc:
            lines.append(f"- {underlying}：当前 funding 字段无效，无法比较：{exc}")
            continue

        lines.extend(
            [
                (
                    f"- {underlying}：历史已结算年化区间 "
                    f"{comparison.historical_min_pct:.6f}%~{comparison.historical_max_pct:.6f}%"
                ),
                f"  年化小数解释：{comparison.annual_decimal_pct:.6f}%（{'区间内' if comparison.annual_decimal_in_range else '区间外'}）",
                (
                    f"  旧“百分比/周期”解释："
                    f"{comparison.old_period_pct_annualized_pct:.6f}%"
                    f"（{'区间内' if comparison.old_period_pct_in_range else '区间外'}）"
                ),
            ]
        )
        if comparison.annual_decimal_in_range and not comparison.old_period_pct_in_range:
            lines.append("  结论：年化小数解释落在历史区间内，旧解释不在。")
        elif comparison.old_period_pct_in_range and not comparison.annual_decimal_in_range:
            lines.append("  结论：旧解释落在历史区间内，需停止并复核单位假设。")
        else:
            lines.append("  结论：两种解释未形成唯一判别，需结合结算前后直接配对继续核对。")

    if not grouped:
        lines.append("- 没有具备 underlying、费率和周期的已结算记录，无法形成统计。")
    return "\n".join(lines)


async def _run() -> int:
    """调用只读端点并打印报告。"""
    from infra.runtime import ensure_ssl_cert

    ensure_ssl_cert()
    from adapters.variational_client import Session, VariationalClient

    client = VariationalClient(Session.from_env())

    async def fetch_page(limit: int, offset: int) -> object:
        return await client.raw(f"/transfers?limit={limit}&offset={offset}")

    try:
        transfers = await fetch_all_transfers(fetch_page)
        underlyings: set[str] = set()
        for transfer in filter_funding_transfers(transfers):
            try:
                underlyings.add(_underlying(transfer))
            except ValueError:
                continue

        current_funding: dict[str, Mapping[str, Any]] = {}
        current_errors: dict[str, str] = {}
        for underlying in sorted(underlyings):
            try:
                response = await client.get_funding(underlying)
                if not isinstance(response, Mapping):
                    raise ValueError("响应不是对象")
                current_funding[underlying] = response
            except Exception as exc:  # noqa: BLE001 单标的失败不丢弃其它证据
                current_errors[underlying] = f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()

    print(build_report(transfers, current_funding))
    if current_errors:
        print("\n四、当前 funding 读取异常")
        for underlying, error in sorted(current_errors.items()):
            print(f"- {underlying}：{error}")
    return 0


def _load_environment_without_proxy() -> tuple[str, ...]:
    """加载 .env，并确保代理变量在任何网络请求前都已被清除。"""
    removed = list(remove_proxy_environment(os.environ))
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    removed.extend(remove_proxy_environment(os.environ))
    return tuple(dict.fromkeys(removed))


def main(argv: Sequence[str] | None = None) -> None:
    """解析参数、清除代理并执行只读验证。"""
    parser = argparse.ArgumentParser(
        description="只读验证 Variational 资金费单位（不会报价或下单）"
    )
    parser.parse_args(argv)
    removed = _load_environment_without_proxy()
    if removed:
        print(f"已清除代理环境变量：{'、'.join(removed)}")
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
