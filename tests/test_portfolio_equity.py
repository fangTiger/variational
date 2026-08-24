"""组合级权益记录器测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal

from tools import portfolio_equity


@dataclass(frozen=True)
class FakeHyperliquidBalance:
    """模拟 Hyperliquid 余额分项。"""

    equity: Decimal
    spot_usdc_total: Decimal
    unrealized_pnl_total: Decimal
    perps_account_value: Decimal


class FakeBalanceClient:
    """返回固定余额对象的只读客户端。"""

    def __init__(self, balance: object) -> None:
        self.balance = balance

    async def get_balance(self) -> object:
        """返回测试余额。"""
        return self.balance


def test_hyperliquid_equity_is_spot_plus_unrealized_not_account_value() -> None:
    """Hyperliquid 权益绝不能把统一账户 accountValue 再加到 Spot 上。"""
    balance = FakeHyperliquidBalance(
        equity=Decimal("552.34"),
        spot_usdc_total=Decimal("557.34"),
        unrealized_pnl_total=Decimal("-5.00"),
        perps_account_value=Decimal("62.00"),
    )

    equity = asyncio.run(
        portfolio_equity.read_hyperliquid_equity(FakeBalanceClient(balance))
    )

    assert equity == Decimal("552.34")
    assert equity == balance.spot_usdc_total + balance.unrealized_pnl_total
    assert equity != balance.spot_usdc_total + balance.perps_account_value


def test_failed_account_is_skipped_annotated_and_record_is_still_written(
    tmp_path,
) -> None:
    """单个账户读取失败时仍写部分记录，且单次进程正常返回。"""

    async def lighter_reader() -> Decimal:
        return Decimal("561.49")

    async def variational_reader() -> Decimal:
        raise RuntimeError("临时不可用")

    async def hyperliquid_reader() -> Decimal:
        return Decimal("572.20")

    output = tmp_path / "portfolio_equity.jsonl"
    snapshot = asyncio.run(
        portfolio_equity.record_once(
            {
                "lighter": lighter_reader,
                "variational": variational_reader,
                "hyperliquid": hyperliquid_reader,
            },
            output,
            timestamp=1787590000.0,
        )
    )

    written = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot == written
    assert written["accounts"] == {
        "lighter": "561.49",
        "hyperliquid": "572.20",
    }
    assert written["total_equity"] == "1133.69"
    assert "variational" not in written["accounts"]
    assert written["errors"]["variational"].endswith("临时不可用")


def test_cli_defaults_to_fifteen_minutes_and_two_hyperliquid_prefixes() -> None:
    """命令行默认值对应长期记录路径和两个实盘账户前缀。"""
    args = portfolio_equity.parse_args([])

    assert args.interval == 900
    assert args.once is False
    assert args.output == portfolio_equity.DEFAULT_OUTPUT
    assert args.hyperliquid_prefix == "HYPERLIQUID"
    assert args.hyperliquid_var_prefix == "HYPERLIQUID_VAR"
