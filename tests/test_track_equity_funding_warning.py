"""权益追踪必须保留 Extended 资金费未校准警示。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from adapters.base import Position
from tools import track_equity
from tracking.track_equity_util import build_snapshot


class _StrictVar:
    """仅允许本测试明确配置的 Variational 只读调用。"""

    async def raw(self, path: str):
        if path != "/portfolio":
            raise AssertionError(f"测试桩未配置调用：raw({path!r})")
        return {"balance": "100", "upnl": "1"}

    async def get_funding_rate(self, underlying: str):
        if underlying != "BTC":
            raise AssertionError(
                f"测试桩未配置调用：get_funding_rate({underlying!r})"
            )
        return Decimal("0.066721")

    async def get_position(self, underlying: str):
        if underlying != "BTC":
            raise AssertionError(
                f"测试桩未配置调用：get_position({underlying!r})"
            )
        return Position(market="BTC", signed_size=Decimal("0.01"))

    async def get_points_summary(self):
        return {"total_points": "12.5"}

    def __getattr__(self, name: str):
        raise AssertionError(f"测试桩未配置调用：{name}")


class _StrictInfo:
    """仅允许读取 BTC-USD 当前市场统计。"""

    async def get_market_statistics(self, *, market_name: str):
        if market_name != "BTC-USD":
            raise AssertionError(
                f"测试桩未配置调用：get_market_statistics({market_name!r})"
            )
        return SimpleNamespace(
            data=SimpleNamespace(
                mark_price=Decimal("60000"),
                funding_rate=Decimal("0.000013"),
            )
        )

    def __getattr__(self, name: str):
        raise AssertionError(f"测试桩未配置调用：{name}")


class _StrictExt:
    """仅允许权益快照所需的 Extended 只读调用。"""

    def __init__(self) -> None:
        self._client = SimpleNamespace(info=_StrictInfo())

    async def get_balance(self):
        return SimpleNamespace(equity=Decimal("4.51"))

    async def get_position(self, market: str):
        if market != "BTC-USD":
            raise AssertionError(
                f"测试桩未配置调用：get_position({market!r})"
            )
        return Position(market=market, signed_size=Decimal("-0.01"))

    def __getattr__(self, name: str):
        raise AssertionError(f"测试桩未配置调用：{name}")


def test_equity_snapshot_and_report_keep_uncalibrated_warning(capsys) -> None:
    """序列化快照和终端报告都不能丢掉未校准状态。"""
    snapshot = asyncio.run(build_snapshot(_StrictVar(), _StrictExt()))

    assert snapshot["extended_funding_calibrated"] is False
    assert any("未经校准" in warning for warning in snapshot["funding_warnings"])

    track_equity._report([snapshot])
    assert "Extended 资金费单位未经校准" in capsys.readouterr().out
