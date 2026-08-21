"""不可交易微仓的 TPSL、硬止损与适配器最小量回归测试。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from adapters.base import MarketPrice, Position
from adapters.extended_client import ExtendedClient
from adapters.lighter_client import LighterClient
from grid.grid_engine import GridConfig, GridEngine
from grid.grid_state import GridState
from grid.regime import GridMode


class RiskExt:
    """记录最小量与风控调用的本地适配器桩。"""

    def __init__(
        self,
        *,
        signed_size: Decimal,
        min_order_size: Decimal = Decimal("0.00020"),
        min_order_size_error: Exception | None = None,
        liquidation_error: Exception | None = None,
        tpsl_error: Exception | None = None,
    ) -> None:
        self.signed_size = signed_size
        self.min_order_size = min_order_size
        self.min_order_size_error = min_order_size_error
        self.liquidation_error = liquidation_error
        self.tpsl_error = tpsl_error
        self.min_order_size_calls = 0
        self.liquidation_calls = 0
        self.tpsl_calls: list[tuple[str, Decimal, Decimal]] = []

    async def get_position(self, market: str) -> Position:
        return Position(market=market, signed_size=self.signed_size)

    async def get_min_order_size(self, market: str) -> Decimal:
        self.min_order_size_calls += 1
        if self.min_order_size_error is not None:
            raise self.min_order_size_error
        return self.min_order_size

    async def get_liquidation_info(self, market: str):
        self.liquidation_calls += 1
        if self.liquidation_error is not None:
            raise self.liquidation_error
        return None

    async def get_balance(self):
        return {"equity": Decimal("1000")}

    async def place_position_stop_loss(
        self,
        market: str,
        signed_size: Decimal,
        trigger_price: Decimal,
    ) -> None:
        self.tpsl_calls.append((market, signed_size, trigger_price))
        if self.tpsl_error is not None:
            raise self.tpsl_error


class DustGridExt(RiskExt):
    """补齐完整做市单轮所需接口，所有读写都只落在内存。"""

    def __init__(self) -> None:
        super().__init__(
            signed_size=Decimal("0.00003"),
            min_order_size=Decimal("0.00020"),
            liquidation_error=AssertionError("微仓不得查询清算价"),
            tpsl_error=AssertionError("微仓不得提交 TPSL"),
        )
        self.placed: list[tuple] = []

    async def get_mark_price(self, market: str) -> Decimal:
        return Decimal("100")

    async def get_market_price(self, market: str) -> MarketPrice:
        return MarketPrice(
            market=market,
            bid=Decimal("99"),
            ask=Decimal("101"),
        )

    async def get_open_orders(self, market: str) -> list:
        return []

    async def get_orders_history(self, market: str, limit: int = 100, **kwargs) -> list:
        return []

    async def place_limit_order(
        self,
        market: str,
        side,
        amount: Decimal,
        price: Decimal,
        **kwargs,
    ):
        self.placed.append((market, side, amount, price, kwargs))
        return SimpleNamespace(
            data=SimpleNamespace(id=f"grid-{len(self.placed)}", status="NEW")
        )


class CandleSource:
    """返回固定已收盘 K 线，避免测试依赖 SDK 或网络。"""

    async def get_hourly_candles(self, market: str, limit: int) -> list:
        return [
            SimpleNamespace(ts=index, high=101.0, low=99.0, close=100.0)
            for index in range(4)
        ]


def _engine(ext, tmp_path, **overrides) -> GridEngine:
    config = GridConfig(
        dry_run=False,
        trend_aware=True,
        exchange_tpsl=True,
        state_path=str(tmp_path / "grid_state.json"),
        **overrides,
    )
    return GridEngine(ext, config, candle_source=CandleSource())


def test_real_adapters_expose_async_minimum_order_size_method() -> None:
    """真实 Extended 与 Lighter 适配器必须具备同名异步方法，不能只补测试桩。"""
    assert inspect.iscoroutinefunction(ExtendedClient.get_min_order_size)
    assert inspect.iscoroutinefunction(LighterClient.get_min_order_size)


def test_lighter_minimum_order_size_uses_market_units_and_cached_meta(
    monkeypatch,
) -> None:
    """Lighter 的 20 个整数单位应换算为 0.00020 BTC，且元数据只请求一次。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "order_books": [
                    {
                        "symbol": "BTC",
                        "market_id": 1,
                        "supported_size_decimals": 5,
                        "supported_price_decimals": 1,
                        "min_base_amount": "0.00020",
                        "min_quote_amount": "10.000000",
                    }
                ],
            },
        )

    fake_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://lighter.test",
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)
    client = LighterClient("0xabc", base_url="https://lighter.test")

    async def scenario() -> tuple[Decimal, Decimal]:
        try:
            first = await client.get_min_order_size("BTC")
            second = await client.get_min_order_size("BTC")
            return first, second
        finally:
            await client.close()

    assert asyncio.run(scenario()) == (Decimal("0.00020"), Decimal("0.00020"))
    assert paths == ["/api/v1/orderBooks"]


def test_dust_tpsl_is_successful_cached_and_logged_once(
    tmp_path,
    caplog,
) -> None:
    """微仓不挂 TPSL 且返回成功；重复判断复用缓存并限频记录。"""
    ext = RiskExt(signed_size=Decimal("0.00003"))
    engine = _engine(ext, tmp_path, market="BTC")

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        first = asyncio.run(
            engine._maintain_tpsl(Decimal("100"), Decimal("0.00003"))
        )
        second = asyncio.run(
            engine._maintain_tpsl(Decimal("100"), Decimal("0.00003"))
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if "持仓低于最小下单量，不挂 TPSL" in record.getMessage()
    ]
    assert first is True and second is True
    assert ext.tpsl_calls == []
    assert ext.min_order_size_calls == 1
    assert len(messages) == 1
    assert "持仓=0.00003" in messages[0]
    assert "最小下单量=0.00020" in messages[0]


def test_lighter_three_unit_dust_keeps_grid_running_without_failsafe(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    """回放 3 单位持仓与 20 单位门槛：不挂 TPSL、不进 fail-safe，网格照常挂单。"""
    ext = DustGridExt()
    engine = _engine(ext, tmp_path, market="BTC")
    engine._state = GridState(90.0, 110.0, False, None, False)
    monkeypatch.setattr(
        "grid.grid_engine.trend_gate",
        lambda *args, **kwargs: GridMode.NEUTRAL,
    )

    with caplog.at_level(logging.ERROR, logger="grid_engine"):
        result = asyncio.run(engine.run_once())

    assert ext.tpsl_calls == []
    assert ext.liquidation_calls == 0
    assert ext.min_order_size_calls == 1
    assert ext.placed, "微仓存在时网格仍应铺单，不能被 TPSL 失败永久阻塞"
    assert "TPSL 未确认" not in result
    assert "硬止损检查进入 fail-safe" not in caplog.text


@pytest.mark.parametrize("signed_size", [Decimal("0.00020"), Decimal("0.00021")])
def test_position_at_or_above_minimum_keeps_existing_tpsl_failure(
    tmp_path,
    signed_size,
) -> None:
    """达到或超过门槛仍走原 TPSL 下单；交易所拒绝时继续返回失败。"""
    ext = RiskExt(
        signed_size=signed_size,
        tpsl_error=RuntimeError("交易所拒绝 TPSL"),
    )
    engine = _engine(ext, tmp_path)

    result = asyncio.run(engine._maintain_tpsl(Decimal("100"), signed_size))

    assert result is False
    assert ext.min_order_size_calls == 1
    assert len(ext.tpsl_calls) == 1
    assert ext.tpsl_calls[0][:2] == (engine.config.market, signed_size)


def test_minimum_query_failure_conservatively_keeps_tpsl_and_hard_stop(
    tmp_path,
    caplog,
) -> None:
    """最小量未知时不得把非零持仓当微仓；两条风控继续旧路径且不重复查询。"""
    ext = RiskExt(
        signed_size=Decimal("0.00003"),
        min_order_size_error=RuntimeError("市场元数据超时"),
        liquidation_error=RuntimeError("清算价超时"),
        tpsl_error=RuntimeError("TPSL 被拒"),
    )
    engine = _engine(ext, tmp_path)

    with caplog.at_level(logging.ERROR, logger="grid_engine"):
        tpsl_result = asyncio.run(
            engine._maintain_tpsl(Decimal("100"), Decimal("0.00003"))
        )
        hard_stop_result = asyncio.run(
            engine._check_hard_stop(
                signed_size=Decimal("0.00003"),
                mark=100.0,
            )
        )

    assert tpsl_result is False
    assert hard_stop_result is False
    assert ext.min_order_size_calls == 1
    assert len(ext.tpsl_calls) == 1
    assert "硬止损检查进入 fail-safe" in caplog.text


def test_position_at_minimum_keeps_existing_hard_stop_failsafe(
    tmp_path,
    caplog,
) -> None:
    """达到最小量后清算价查询失败仍进入既有 fail-safe，不能被误判成微仓。"""
    ext = RiskExt(
        signed_size=Decimal("0.00020"),
        liquidation_error=RuntimeError("清算价超时"),
    )
    engine = _engine(ext, tmp_path)

    with caplog.at_level(logging.ERROR, logger="grid_engine"):
        result = asyncio.run(
            engine._check_hard_stop(
                signed_size=Decimal("0.00020"),
                mark=100.0,
            )
        )

    assert result is False
    assert ext.min_order_size_calls == 1
    assert "硬止损检查进入 fail-safe" in caplog.text
