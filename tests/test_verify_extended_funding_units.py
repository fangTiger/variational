"""Extended 资金费单位校准工具测试，全程使用严格离线桩。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tools.verify_extended_funding_units import (
    FundingUnit,
    collect_calibration,
    evaluate_calibration,
    remove_proxy_environment,
)


class _StrictObject:
    """未显式配置的属性或调用一律报错，禁止假客户端默认成功。"""

    def __getattr__(self, name: str):
        raise AssertionError(f"测试桩未配置调用：{name}")


class _StrictInfo(_StrictObject):
    def __init__(self, calls: list[tuple]) -> None:
        self.calls = calls

    async def get_market_statistics(self, *, market_name: str):
        self.calls.append(("get_market_statistics", market_name))
        return SimpleNamespace(
            data=SimpleNamespace(funding_rate=Decimal("0.000013"))
        )

    async def get_funding_rates_history(
        self,
        *,
        market_name: str,
        start_time: datetime,
        end_time: datetime,
    ):
        self.calls.append(
            ("get_funding_rates_history", market_name, start_time, end_time)
        )
        return SimpleNamespace(data=[])


class _StrictAccount(_StrictObject):
    def __init__(self, calls: list[tuple]) -> None:
        self.calls = calls

    async def get_positions(self, *, market_names: list[str]):
        self.calls.append(("get_positions", tuple(market_names)))
        return SimpleNamespace(data=[])


class _StrictExtendedClient(_StrictObject):
    def __init__(self, calls: list[tuple]) -> None:
        self._client = SimpleNamespace(
            info=_StrictInfo(calls),
            account=_StrictAccount(calls),
        )


@pytest.mark.parametrize(
    ("payment", "expected"),
    [
        (Decimal("0.8"), FundingUnit.DECIMAL_PER_HOUR),
        (Decimal("0.1"), FundingUnit.DECIMAL_PER_8_HOURS),
        (Decimal("0.000091324200913242"), FundingUnit.ANNUAL_DECIMAL),
    ],
)
def test_evaluate_calibration_distinguishes_supported_units(
    payment: Decimal,
    expected: FundingUnit,
) -> None:
    """同一小时结算额应能区分每小时、每 8 小时和年化小数。"""
    history = [
        {"timestamp": 1_700_000_000_000, "funding_rate": "0.00008"},
        {"timestamp": 1_700_003_600_000, "funding_rate": "0.00008"},
    ]
    settlements = [
        {
            "paidTime": 1_700_003_600_000,
            "fundingFee": str(payment),
            "positionNotional": "10000",
        }
    ]

    result = evaluate_calibration(
        current_rate=Decimal("0.00008"),
        rate_history=history,
        settlements=settlements,
        has_open_position=True,
    )

    assert result.calibrated is True
    assert result.unit is expected
    assert result.evidence_count == 1


def test_empty_account_reports_unable_without_guessing() -> None:
    """无持仓且无结算记录时必须如实列出缺失证据。"""
    calls: list[tuple] = []
    client = _StrictExtendedClient(calls)

    async def strict_settlement_reader(*, market: str, limit: int):
        calls.append(("get_funding_settlements", market, limit))
        return []

    result = asyncio.run(
        collect_calibration(
            client,
            settlement_reader=strict_settlement_reader,
            market="BTC-USD",
            now=datetime(2026, 9, 5, tzinfo=timezone.utc),
        )
    )

    assert result.calibrated is False
    assert result.unit is None
    report = result.pretty()
    assert "无法校准" in report
    assert "无持仓" in report
    assert "无资金费结算记录" in report
    assert calls[0] == ("get_market_statistics", "BTC-USD")
    assert calls[1][0:2] == ("get_funding_rates_history", "BTC-USD")
    assert calls[2] == ("get_positions", ("BTC-USD",))
    assert calls[3] == ("get_funding_settlements", "BTC-USD", 200)


def test_remove_proxy_environment_covers_upper_and_lower_case() -> None:
    """所有常见代理变量都必须在网络访问前移除。"""
    environment = {
        "HTTP_PROXY": "http://upper",
        "https_proxy": "http://lower",
        "ALL_PROXY": "socks5://all",
        "X10_API_KEY": "secret",
    }

    removed = remove_proxy_environment(environment)

    assert set(removed) == {"HTTP_PROXY", "https_proxy", "ALL_PROXY"}
    assert environment == {"X10_API_KEY": "secret"}
