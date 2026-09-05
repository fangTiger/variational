"""Variational 资金费单位验证工具测试；全部使用真实数值并离线运行。"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from tools.verify_funding_units import (
    annualize_settled_rate,
    build_report,
    compare_predicted_interpretations,
    fetch_all_transfers,
    filter_funding_transfers,
    infer_payment_evidence,
    is_clean_six_decimal,
    remove_proxy_environment,
)


# 下面是 /transfers 的**真实**记录形状（2026-09-04 实跑抓取后脱敏），
# 字段名照抄接口返回，不要"顺手"改成看起来更合理的名字：
# - 承载合约信息的键是 reference_instrument，不是 ref_instrument；
# - 顶层 funding_interval_s (28800) 与嵌套 funding_interval_s (3600) 并存且不同，
#   年化换算必须取顶层的 28800（1095 期/年），取错会算出 8 倍偏差。
# 曾因为把键写成 ref_instrument，工具对全部 535 条真实记录静默跳过、报告全空，
# 而测试因为夹具用了同样的错名字照样通过。夹具必须钉住真实 schema。
BTC_TRANSFER = {
    "asset": "USDC",
    "created_at": "2026-09-04T08:00:00Z",
    "funding_interval_s": 28800,
    "funding_rate": "0.0000685680365296804",
    "funding_type": None,
    "qty": "-0.264342",
    "ref_instrument_position_qty": "0.047801000000",
    "reference_instrument": {
        "funding_interval_s": 3600,
        "instrument_type": "perpetual_future",
        "settlement_asset": "USDC",
        "underlying": "BTC",
    },
}


def test_interval_precedence_prefers_top_level_field() -> None:
    """顶层 funding_interval_s 必须优先于 reference_instrument 里的值。

    真实记录中两者并存且不同（28800 vs 3600），取错会让年化换算差 8 倍。
    """
    from tools.verify_funding_units import _interval_from_record

    assert _interval_from_record(BTC_TRANSFER) == 28800


def test_real_btc_transfer_implies_matching_notional_and_price() -> None:
    """真实 BTC 流水应闭合扣款、费率、名义与仓位数量关系。"""
    evidence = infer_payment_evidence(BTC_TRANSFER)

    assert evidence.payment_matches is True
    assert evidence.sign_matches is True
    assert evidence.used_observed_price is False
    assert abs(evidence.notional_usdc - Decimal("3855.4")) < Decimal("0.3")
    assert abs(evidence.implied_price - Decimal("80655")) < Decimal("10")
    assert (
        abs(Decimal(BTC_TRANSFER["funding_rate"])) * evidence.notional_usdc
        == abs(Decimal(BTC_TRANSFER["qty"]))
    )


def test_real_btc_transfer_accepts_rounded_observed_price() -> None:
    """真实示例中的当时价 80655 为展示精度，独立校验应允许该舍入误差。"""
    evidence = infer_payment_evidence(
        {**BTC_TRANSFER, "ref_instrument_price": "80655"}
    )

    assert evidence.used_observed_price is True
    assert evidence.payment_matches is True
    assert evidence.payment_difference < Decimal("0.000015")


def test_real_8h_and_4h_rates_restore_clean_six_decimals_only_on_365_basis() -> None:
    """真实 8h/4h 结算值只在 365 天基准下还原成干净六位年化小数。"""
    btc_8h_rate = Decimal("0.0000685680365296804")
    market_4h_rate = Decimal("0.0000286324200913242")

    annual_8h_365 = annualize_settled_rate(btc_8h_rate, 28800, day_count=365)
    annual_4h_365 = annualize_settled_rate(market_4h_rate, 14400, day_count=365)
    annual_8h_360 = annualize_settled_rate(btc_8h_rate, 28800, day_count=360)
    annual_4h_360 = annualize_settled_rate(market_4h_rate, 14400, day_count=360)

    assert abs(annual_8h_365 - Decimal("0.075082")) < Decimal("1e-15")
    assert abs(annual_4h_365 - Decimal("0.062705")) < Decimal("1e-15")
    assert is_clean_six_decimal(annual_8h_365)
    assert is_clean_six_decimal(annual_4h_365)
    assert not is_clean_six_decimal(annual_8h_360)
    assert not is_clean_six_decimal(annual_4h_360)


def test_real_8h_and_4h_caps_restore_the_same_annualized_value() -> None:
    """真实费率帽在不同周期下都应折算为年化小数 0.109500。"""
    cap_8h = annualize_settled_rate(Decimal("0.0001"), 28800, day_count=365)
    cap_4h = annualize_settled_rate(Decimal("0.00005"), 14400, day_count=365)

    assert cap_8h == Decimal("0.1095")
    assert cap_4h == Decimal("0.10950")
    assert cap_8h == cap_4h


def test_predicted_rate_comparison_rejects_old_percent_per_period_reading() -> None:
    """当前真实预测量级只有按年化小数解释才落入历史结算区间。"""
    comparison = compare_predicted_interpretations(
        Decimal("0.066721"),
        28800,
        [Decimal("0.025"), Decimal("0.075082"), Decimal("0.109500")],
    )

    assert comparison.annual_decimal_pct == Decimal("6.672100")
    assert comparison.old_period_pct_annualized_pct == Decimal("73.059495")
    assert comparison.annual_decimal_in_range is True
    assert comparison.old_period_pct_in_range is False


def test_fetches_all_transfer_pages_with_limit_100_then_filters_funding() -> None:
    """工具必须按 limit=100 翻页取全，再筛出 funding_rate 非空的记录。"""
    calls: list[tuple[int, int]] = []
    pages = {
        0: {
            "pagination": {"object_count": 3},
            "result": [
                BTC_TRANSFER,
                {"transfer_type": "deposit", "qty": "100", "funding_rate": None},
            ],
        },
        100: {
            "pagination": {"object_count": 3},
            "result": [
                {
                    **BTC_TRANSFER,
                    "created_at": "2026-09-03T08:00:00Z",
                    "funding_rate": "0.00005",
                }
            ],
        },
    }

    async def fetch_page(limit: int, offset: int) -> object:
        calls.append((limit, offset))
        return pages[offset]

    transfers = asyncio.run(fetch_all_transfers(fetch_page))

    assert calls == [(100, 0), (100, 100)]
    assert len(transfers) == 3
    assert len(filter_funding_transfers(transfers)) == 2


def test_removes_all_proxy_environment_variants() -> None:
    """发请求前必须同时剥掉大小写 HTTP/HTTPS/ALL_PROXY。"""
    environment = {
        "HTTP_PROXY": "http://127.0.0.1:1080",
        "HTTPS_PROXY": "http://127.0.0.1:1080",
        "ALL_PROXY": "socks5://127.0.0.1:1080",
        "http_proxy": "http://127.0.0.1:1080",
        "https_proxy": "http://127.0.0.1:1080",
        "all_proxy": "socks5://127.0.0.1:1080",
        "VARIATIONAL_COOKIE": "保留",
    }

    removed = remove_proxy_environment(environment)

    assert set(removed) == {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
    assert environment == {"VARIATIONAL_COOKIE": "保留"}


def test_chinese_report_contains_365_360_and_prediction_comparison() -> None:
    """报告应同时展示扣款证据、日基准对照和两种预测解释。"""
    # BTC 历史已结算单期费率约为 2.3e-5~1e-4；加入给出的真实样本检查中位值。
    transfers = [
        {**BTC_TRANSFER, "funding_rate": "0.000023"},
        BTC_TRANSFER,
        {**BTC_TRANSFER, "funding_rate": "0.0001"},
    ]
    report = build_report(
        transfers,
        {
            "BTC": {
                "predicted_funding_rate": "0.066721",
                "funding_interval_s": 28800,
            }
        },
    )

    assert "Variational 资金费单位验证报告" in report
    assert "365 天基准" in report
    assert "360 天基准" in report
    assert "年化小数解释：6.672100%" in report
    assert "旧“百分比/周期”解释：73.059495%" in report
    assert "年化小数解释落在历史区间内" in report
