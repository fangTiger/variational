"""Variational swap 最小适配层测试；所有夹具均为实测接口形状。"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from adapters.variational_client import VariationalClient
from adapters.base import Side


SWAP_METADATA = {
    "XAUS": [
        {
            "asset": "XAUS",
            "asset_class": "commodity",
            "funding_interval_s": 0,
            "instrument_type": "swap",
            "market_status": "open",
        }
    ]
}

SWAP_FUNDING = {
    "upcoming": {
        "trade_date": "2026-09-04",
        "apply_time": "2026-09-04T21:05:00Z",
        "basis": "rates",
        "long_rate": "-0.057214",
        "short_rate": "0.025031",
    },
    "latest_applied": {
        "trade_date": "2026-09-03",
        "apply_time": "2026-09-03T21:05:00Z",
        "basis": "rates",
        "long_rate": "-0.056914",
        "short_rate": "0.024731",
    },
}


def _strict_client(
    *,
    gets: dict[str, object] | None = None,
) -> tuple[VariationalClient, list[tuple[str, str, object]]]:
    """构造默认拒绝一切未配置调用的离线客户端。"""
    client = object.__new__(VariationalClient)
    client._supported_assets_loaded = False
    client._supported_assets_metadata = None
    calls: list[tuple[str, str, object]] = []
    configured_gets = gets or {}

    async def strict_get(path: str) -> object:
        calls.append(("GET", path, None))
        if path not in configured_gets:
            raise AssertionError(f"未配置的 GET 调用：{path}")
        value = configured_gets[path]
        if isinstance(value, Exception):
            raise value
        return value

    async def strict_post(path: str, body: dict | None = None) -> object:
        calls.append(("POST", path, body))
        raise AssertionError(f"未配置的 POST 调用：{path}")

    client._get = strict_get
    client._post = strict_post
    return client, calls


def test_swap_instrument_rejects_missing_kind_before_request() -> None:
    """swap 缺 kind 必须本地失败，不能把坏请求交给后端。"""
    with pytest.raises(ValueError, match="swap.*kind"):
        VariationalClient._instrument(
            "XAUS",
            instrument_type="swap",
            funding_interval_s=0,
        )


def test_instrument_interval_default_is_none_sentinel() -> None:
    """工具构造器签名不得继续暴露非法的 3600 默认周期。"""
    parameter = inspect.signature(VariationalClient._instrument).parameters[
        "funding_interval_s"
    ]

    assert parameter.default is None


def test_swap_instrument_rejects_nonzero_interval_before_request() -> None:
    """swap 的非零结算周期必须在发请求前被拒。"""
    with pytest.raises(ValueError, match="swap.*funding_interval_s.*0"):
        VariationalClient._instrument(
            "XAUS",
            instrument_type="swap",
            funding_interval_s=3600,
            kind="commodity",
        )


def test_swap_quote_resolves_real_metadata_and_sends_exact_identifier() -> None:
    """XAUS 默认参数应由真实元数据补全为 swap + commodity + 零周期。"""
    client, calls = _strict_client(
        gets={"/metadata/supported_assets": SWAP_METADATA}
    )

    async def post(path: str, body: dict | None = None) -> object:
        calls.append(("POST", path, body))
        if path != "/quotes/indicative":
            raise AssertionError(f"未配置的 POST 调用：{path}")
        return {"bid": "4434.7", "ask": "4435.3"}

    client._post = post

    asyncio.run(client.request_quote("XAUS", "buy", Decimal("0.224")))

    quote_call = calls[-1]
    assert quote_call == (
        "POST",
        "/quotes/indicative",
        {
            "instrument": {
                "underlying": "XAUS",
                "instrument_type": "swap",
                "settlement_asset": "USDC",
                "kind": "commodity",
                "funding_interval_s": 0,
            },
            "qty": "0.224",
            "side": "buy",
        },
    )


def test_explicit_swap_type_still_resolves_kind_from_metadata() -> None:
    """显式传 swap 时也必须读取 asset_class 补全 kind。"""
    client, calls = _strict_client(
        gets={"/metadata/supported_assets": SWAP_METADATA}
    )

    resolved = asyncio.run(client._resolve_instrument_params("XAUS", "swap", None))

    assert resolved == ("swap", "commodity")
    assert calls == [("GET", "/metadata/supported_assets", None)]


def test_explicit_swap_nonzero_interval_never_posts_quote() -> None:
    """即使类型与 kind 显式给出，非法周期也不能触发 RFQ 请求。"""
    client, calls = _strict_client()

    with pytest.raises(ValueError, match="swap.*funding_interval_s.*0"):
        asyncio.run(
            client.request_quote(
                "XAUS",
                "buy",
                Decimal("0.224"),
                instrument_type="swap",
                funding_interval_s=3600,
                kind="commodity",
            )
        )

    assert not [call for call in calls if call[0] == "POST"]


def test_explicit_swap_nonzero_interval_never_reads_metadata() -> None:
    """非法周期的显式 swap 应在任何网络访问前立即失败。"""
    client, calls = _strict_client()

    with pytest.raises(ValueError, match="swap.*funding_interval_s.*0"):
        asyncio.run(
            client.request_quote(
                "XAUS",
                "buy",
                Decimal("0.224"),
                instrument_type="swap",
                funding_interval_s=3600,
            )
        )

    assert calls == []


def test_swap_market_order_rejects_nonzero_interval_before_any_call() -> None:
    """成交入口也必须先阻断非法 swap 标识，不能依赖报价入口兜底。"""
    client, calls = _strict_client()
    client._max_slippage = 0.01

    with pytest.raises(ValueError, match="swap.*funding_interval_s.*0"):
        asyncio.run(
            client.market_order(
                "XAUS",
                Side.BUY,
                Decimal("0.224"),
                instrument_type="swap",
                funding_interval_s=3600,
            )
        )

    assert calls == []


def test_get_funding_rate_rejects_swap_without_calling_perpetual_endpoint() -> None:
    """永续便捷方法不得把 swap 静默路由到错误端点。"""
    client, calls = _strict_client()

    with pytest.raises(ValueError, match="get_swap_funding"):
        asyncio.run(client.get_funding_rate("XAUS", "swap"))

    assert calls == []


def test_get_funding_rate_rejects_every_non_perpetual_type() -> None:
    """便捷接口应只接受两类永续，而不是仅特判 swap。"""
    client, calls = _strict_client()

    with pytest.raises(ValueError, match="只支持永续"):
        asyncio.run(client.get_funding_rate("XAUS", "future"))

    assert calls == []


def test_get_swap_funding_preserves_raw_rates_and_calendar_coverage() -> None:
    """真实响应中的年化小数须原样保留，覆盖天数只由日期差决定。"""
    client, calls = _strict_client(gets={"/funding/swap?underlying=XAUS": SWAP_FUNDING})

    result = asyncio.run(client.get_swap_funding("XAUS"))

    assert calls == [("GET", "/funding/swap?underlying=XAUS", None)]
    assert result.upcoming.long_rate.raw_rate == Decimal("-0.057214")
    assert result.upcoming.short_rate.raw_rate == Decimal("0.025031")
    assert result.upcoming.long_rate.normalized_annual_rate == Decimal("-0.057214")
    assert result.upcoming.long_rate.coverage_days == 1
    assert result.upcoming.long_rate.day_count_basis == 365
    assert result.upcoming.long_rate.apply_time == datetime(
        2026, 9, 4, 21, 5, tzinfo=timezone.utc
    )
    assert result.upcoming.long_rate.observed_at.tzinfo is timezone.utc
    assert result.latest_applied.long_rate.coverage_days is None
    assert result.warnings == ()


def test_swap_coverage_uses_calendar_days_not_rate_magnitude() -> None:
    """跨三天但费率幅度不变时，coverage_days 仍必须为三。"""
    payload = {
        **SWAP_FUNDING,
        "upcoming": {
            **SWAP_FUNDING["upcoming"],
            "apply_time": "2026-09-06T21:05:00Z",
            "long_rate": "-0.056914",
        },
    }
    client, _ = _strict_client(gets={"/funding/swap?underlying=XAUS": payload})

    result = asyncio.run(client.get_swap_funding("XAUS"))

    assert result.upcoming.long_rate.coverage_days == 3
    assert result.upcoming.long_rate.normalized_annual_rate == Decimal("-0.056914")


def test_one_day_threefold_rate_is_warned_not_treated_as_multiday(caplog) -> None:
    """一天内约三倍费率是突变告警，不能伪装成多日计提。"""
    payload = {
        **SWAP_FUNDING,
        "upcoming": {
            **SWAP_FUNDING["upcoming"],
            "long_rate": "-0.170742",
        },
    }
    client, _ = _strict_client(gets={"/funding/swap?underlying=XAUS": payload})

    result = asyncio.run(client.get_swap_funding("XAUS"))

    assert result.upcoming.long_rate.coverage_days == 1
    assert result.warnings
    assert "费率突变" in result.warnings[0]
    assert "费率突变" in caplog.text


def test_coverage_days_is_backward_looking_and_cannot_settle_weekend_question() -> None:
    """coverage_days 只表示「距上次结算过了几天」，不能推断本次向前覆盖几天。

    方案 §6.2 的开放问题是：平台在周五预收 Fri/Sat/Sun，还是在周一追补 Sat/Sun。
    这两种情形下相邻 apply_time 的日历差完全一致，本字段无法区分，
    所以任何「周末平仓能躲掉几次计提」的推断都不得依赖它。
    """
    def snapshot(latest_apply: str, upcoming_apply: str):
        payload = {
            "latest_applied": {
                **SWAP_FUNDING["latest_applied"],
                "apply_time": latest_apply,
            },
            "upcoming": {**SWAP_FUNDING["upcoming"], "apply_time": upcoming_apply},
        }
        client, _ = _strict_client(
            gets={"/funding/swap?underlying=XAUS": payload}
        )
        return asyncio.run(client.get_swap_funding("XAUS"))

    # 周五观测：与周四相隔 1 个日历天
    friday = snapshot("2026-09-03T21:05:00Z", "2026-09-04T21:05:00Z")
    # 周一观测：与周五相隔 3 个日历天
    monday = snapshot("2026-09-04T21:05:00Z", "2026-09-07T21:05:00Z")

    assert friday.upcoming.long_rate.coverage_days == 1
    assert monday.upcoming.long_rate.coverage_days == 3

    # 关键：这两个数值只由 apply_time 的日历差决定。
    # 「周五预收 Fri/Sat/Sun」与「周一追补 Sat/Sun」两种机制下，
    # 平台返回的 apply_time 序列完全一致 —— 故本字段对该问题零信息量，
    # 不得用它推断周末平仓能躲掉几次计提。
    assert friday.upcoming.long_rate.coverage_days == 1, (
        "周五的 1 天只说明距上次结算过了 1 个日历天，"
        "不能据此断定周末两天不由周五这次承担"
    )
