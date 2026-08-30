"""定时定量对冲策略测试。

测试只使用内存适配器，不连接任何交易所。测试桩分别锁定 Lighter 与
Extended 的真实方法签名，避免虚构生产适配器不存在的能力。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.base import MarketPrice, Position, PositionPnl, Side
from adapters.extended_client import ExtendedClient
from adapters.lighter_client import LighterClient
from adapters.variational_client import VariationalClient
from engine.hedge_engine import HedgeFillResult


class _AdapterCore:
    """共享的内存持仓与订单行为；公开方法由两种真实签名的子类提供。"""

    def __init__(
        self,
        name: str,
        *,
        maker_fill: Decimal = Decimal("1"),
        min_order_size: Decimal = Decimal("0.001"),
        rounded_amount: Decimal | None = None,
        min_order_size_error: Exception | None = None,
        balance_equity: Decimal = Decimal("500"),
        balance_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.position = Decimal("0")
        self.maker_fill = Decimal(str(maker_fill))
        self.min_order_size = Decimal(str(min_order_size))
        self.rounded_amount = (
            Decimal(str(rounded_amount)) if rounded_amount is not None else None
        )
        self.min_order_size_error = min_order_size_error
        self.balance_equity = Decimal(str(balance_equity))
        self.balance_error = balance_error
        self.balance_reads = 0
        self.events: list[tuple] = []
        self._orders: dict[str, SimpleNamespace] = {}

    async def get_market_price(self, market: str):
        return MarketPrice(market, bid=Decimal("99"), ask=Decimal("101"))

    async def get_position(self, market: str):
        return Position(market, self.position)

    async def get_min_order_size(self, market: str):
        del market
        if self.min_order_size_error is not None:
            raise self.min_order_size_error
        return self.min_order_size

    async def round_amount(self, market: str, amount: Decimal):
        del market
        if self.rounded_amount is not None:
            return self.rounded_amount
        return Decimal(str(amount)).quantize(Decimal("0.001"))

    async def get_balance(self):
        """返回测试权益，或按配置模拟展示接口失败。"""
        self.balance_reads += 1
        if self.balance_error is not None:
            raise self.balance_error
        return SimpleNamespace(equity=self.balance_equity)

    async def _place(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool,
        reduce_only: bool,
    ):
        order_id = f"{self.name}-{len(self._orders) + 1}"
        signed = amount if side is Side.BUY else -amount
        filled = amount * self.maker_fill
        self.position += signed * self.maker_fill
        self.events.append(
            ("limit", market, side, amount, price, post_only, reduce_only)
        )
        order = SimpleNamespace(
            id=order_id,
            filled_qty=filled,
            status="FILLED" if filled >= amount else "NEW",
        )
        self._orders[order_id] = order
        return SimpleNamespace(data=SimpleNamespace(id=order_id))

    async def get_order_by_id(self, market: str, order_id):
        del market
        return self._orders[order_id]

    async def cancel_order(self, market: str, order_id) -> None:
        self.events.append(("cancel", market, order_id))

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        signed = amount if side is Side.BUY else -amount
        self.position += signed
        self.events.append(("market", market, side, amount, reduce_only))
        return SimpleNamespace(data=SimpleNamespace(id=f"market-{self.name}"))


class _LighterAdapter(_AdapterCore):
    """公开签名与 ``LighterClient`` 一致。"""

    async def place_limit_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool = True,
        reduce_only: bool = False,
    ):
        return await self._place(
            market,
            side,
            amount,
            price,
            post_only=post_only,
            reduce_only=reduce_only,
        )


class _ExtendedAdapter(_AdapterCore):
    """公开签名与 ``ExtendedClient`` 一致。"""

    async def get_market_price(self, market_name: str):
        return MarketPrice(market_name, bid=Decimal("99"), ask=Decimal("101"))

    async def get_position(self, market_name: str):
        return Position(market_name, self.position)

    async def place_limit_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        price: Decimal,
        *,
        post_only: bool = True,
        expire_days: int = 90,
        reduce_only: bool = False,
    ):
        del expire_days
        return await self._place(
            market,
            side,
            amount,
            price,
            post_only=post_only,
            reduce_only=reduce_only,
        )


class _RfqAdapter:
    """只实现 RFQ 公共能力，不伪造订单簿方法。"""

    execution_model = "rfq"

    def __init__(
        self,
        name: str = "rfq",
        *,
        min_order_size: Decimal = Decimal("0.001"),
    ) -> None:
        self.name = name
        self.position = Decimal("0")
        self.min_order_size = Decimal(str(min_order_size))
        self.events: list[tuple] = []

    async def get_market_price(self, market: str):
        self.events.append(("quote", market))
        return MarketPrice(market, bid=Decimal("99"), ask=Decimal("101"))

    async def get_position(self, market: str):
        return Position(market, self.position)

    async def get_min_order_size(self, market: str):
        del market
        return self.min_order_size

    async def round_amount(self, market: str, amount: Decimal):
        del market
        return Decimal(str(amount)).quantize(Decimal("0.001"))

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
    ):
        signed = amount if side is Side.BUY else -amount
        self.position += signed
        self.events.append(("market", market, side, amount, reduce_only))
        return SimpleNamespace(data=SimpleNamespace(id=f"market-{self.name}"))


class _ScriptedExecutor:
    """按腿模拟全成、部分成交和成交后异常，并记录策略给出的实际差额。"""

    def __init__(self, plans: dict[str, list[dict]] | None = None) -> None:
        self.plans = {name: list(items) for name, items in (plans or {}).items()}
        self.calls: list[dict] = []

    async def __call__(
        self,
        adapter,
        market: str,
        target_delta: Decimal,
        *,
        timeout_s: float,
        poll_s: float,
        reduce_only: bool,
    ) -> HedgeFillResult:
        plan = (
            self.plans.get(adapter.name, []).pop(0)
            if self.plans.get(adapter.name)
            else {}
        )
        fraction = Decimal(str(plan.get("fraction", "1")))
        actual_delta = Decimal(
            str(plan.get("actual_delta", target_delta * fraction))
        )
        adapter.position += actual_delta
        self.calls.append(
            {
                "adapter": adapter.name,
                "market": market,
                "target_delta": target_delta,
                "actual_delta": actual_delta,
                "timeout_s": timeout_s,
                "poll_s": poll_s,
                "reduce_only": reduce_only,
            }
        )
        if error := plan.get("error"):
            if isinstance(error, Exception):
                raise error
            raise RuntimeError(str(error))
        reported = Decimal(str(plan.get("reported_fill", abs(target_delta))))
        return HedgeFillResult(filled=reported, used_taker=False)


class _RestrictedVariationalCloseExecutor:
    """复现代理出口受限时 Variational 拒绝平仓、HL 正常成交的执行器。"""

    def __init__(self) -> None:
        self.block_variational_close = True
        self.calls: list[dict] = []

    async def __call__(
        self,
        adapter,
        market: str,
        target_delta: Decimal,
        *,
        timeout_s: float,
        poll_s: float,
        reduce_only: bool,
    ) -> HedgeFillResult:
        call = {
            "adapter": adapter.name,
            "market": market,
            "target_delta": target_delta,
            "timeout_s": timeout_s,
            "poll_s": poll_s,
            "reduce_only": reduce_only,
        }
        self.calls.append(call)
        if (
            adapter.name == "variational"
            and reduce_only
            and self.block_variational_close
        ):
            raise RuntimeError("Variational 拒绝下单：代理出口位于受限地区")
        adapter.position += target_delta
        return HedgeFillResult(filled=abs(target_delta), used_taker=False)


def _signature_shape(method):
    """只比较调用契约，不要求测试桩复制生产类型注解。"""
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(method).parameters.values()
    ]


@pytest.mark.parametrize(
    ("fake_type", "real_type", "method_name"),
    [
        (_LighterAdapter, LighterClient, "get_market_price"),
        (_LighterAdapter, LighterClient, "get_position"),
        (_LighterAdapter, LighterClient, "get_min_order_size"),
        (_LighterAdapter, LighterClient, "round_amount"),
        (_LighterAdapter, LighterClient, "place_limit_order"),
        (_LighterAdapter, LighterClient, "get_order_by_id"),
        (_LighterAdapter, LighterClient, "cancel_order"),
        (_LighterAdapter, LighterClient, "market_order"),
        (_ExtendedAdapter, ExtendedClient, "get_market_price"),
        (_ExtendedAdapter, ExtendedClient, "get_position"),
        (_ExtendedAdapter, ExtendedClient, "get_min_order_size"),
        (_ExtendedAdapter, ExtendedClient, "round_amount"),
        (_ExtendedAdapter, ExtendedClient, "place_limit_order"),
        (_ExtendedAdapter, ExtendedClient, "get_order_by_id"),
        (_ExtendedAdapter, ExtendedClient, "cancel_order"),
        (_ExtendedAdapter, ExtendedClient, "market_order"),
    ],
)
def test_adapter_stubs_match_real_signatures(fake_type, real_type, method_name) -> None:
    assert _signature_shape(getattr(fake_type, method_name)) == _signature_shape(
        getattr(real_type, method_name)
    )


def _api():
    """延迟导入，让 13 条规格测试分别因策略模块缺失而失败。"""
    return importlib.import_module("timed_volume.strategy")


def _build_strategy(
    tmp_path,
    *,
    lighter: _LighterAdapter | None = None,
    extended: _ExtendedAdapter | None = None,
    executor=None,
    hedge_available=None,
    state_name: str = "state.json",
    initial_direction: str = "long",
    maker_timeout_s: float = 300.0,
    position_tolerance: Decimal = Decimal("0.000001"),
    auth_error_types: tuple[type[Exception], ...] = (),
    on_auth_error=None,
    on_hedge_auth_error=None,
    equity_path=None,
    ledger_path=None,
    heartbeat_path=None,
    instance="test_instance",
    basis_gate_sigma: Decimal = Decimal("0"),
    basis_gate_max_wait_s: float = 1800.0,
    entry_mode: str = "timer",
    signal_sigma: Decimal = Decimal("2.0"),
    signal_lookback_hours: float = 48.0,
    signal_refresh_minutes: float = 15.0,
    signal_min_samples: int = 100,
    max_hold_hours: float = 8.0,
    signal_fallback_hours: float = 4.0,
    candle_loader=None,
):
    api = _api()
    lighter = lighter or _LighterAdapter("lighter")
    extended = extended or _ExtendedAdapter("extended")
    config = api.TimedVolumeConfig(
        primary_market="BTC",
        hedge_market="BTC-USD",
        notional_min_usd=2000,
        notional_max_usd=2000,
        cycle_seconds=7200.0,
        initial_direction=api.RoundDirection(initial_direction),
        maker_timeout_s=maker_timeout_s,
        maker_poll_s=0.0,
        position_tolerance=position_tolerance,
        state_path=tmp_path / state_name,
        equity_path=equity_path,
        ledger_path=ledger_path,
        heartbeat_path=heartbeat_path,
        instance=instance,
        basis_gate_sigma=basis_gate_sigma,
        basis_gate_max_wait_s=basis_gate_max_wait_s,
        entry_mode=entry_mode,
        signal_sigma=signal_sigma,
        signal_lookback_hours=signal_lookback_hours,
        signal_refresh_minutes=signal_refresh_minutes,
        signal_min_samples=signal_min_samples,
        max_hold_hours=max_hold_hours,
        signal_fallback_hours=signal_fallback_hours,
    )
    strategy = api.TimedHedgedVolumeStrategy(
        lighter,
        extended,
        config,
        trade_executor=executor,
        hedge_available=hedge_available,
        auth_error_types=auth_error_types,
        on_auth_error=on_auth_error,
        on_hedge_auth_error=on_hedge_auth_error,
        candle_loader=candle_loader,
    )
    return api, strategy, lighter, extended


def _build_restricted_close_strategy(tmp_path):
    """先正常建仓，再切换到真实事故组合的平仓失败执行器。"""
    variational = _LighterAdapter("variational")
    hyperliquid = _ExtendedAdapter("hyperliquid")
    api, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=variational,
        extended=hyperliquid,
        executor=_ScriptedExecutor(),
    )
    opened = asyncio.run(strategy.run_once(now=0.0))
    assert opened.action == "opened"
    executor = _RestrictedVariationalCloseExecutor()
    strategy._trade_executor = executor
    return api, strategy, variational, hyperliquid, executor


def _enter_close_halt(strategy):
    """连续执行三次失败平仓并返回三次结果。"""
    return [
        asyncio.run(strategy.run_once(now=7200.0 + offset))
        for offset in range(3)
    ]


_HIGH_VARIANCE_BASIS_HISTORY = (
    Decimal("-0.12"),
    Decimal("-0.08"),
    Decimal("-0.04"),
    Decimal("0"),
    Decimal("0.04"),
    Decimal("0.08"),
    Decimal("0.12"),
)


def _write_basis_history(
    path,
    values: tuple[Decimal, ...],
    *,
    heartbeat: bool = False,
) -> None:
    """写入测试专用基差历史，并区分台账与心跳格式。"""
    rows = [
        (
            {
                "action": "opened",
                "entry_basis_pct": str(value),
            }
            if heartbeat
            else {
                "instance": "test_instance",
                "entry_basis_pct": str(value),
            }
        )
        for value in values
    ]
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )


class _LedgerLighter(_LighterAdapter):
    """提供可控入场价、盘口与成交历史的主腿测试桩。"""

    def __init__(
        self,
        name: str = "lighter",
        *,
        entry_price: Decimal = Decimal("100"),
        mid_price: Decimal = Decimal("110"),
        fills: list[dict] | None = None,
    ) -> None:
        super().__init__(name)
        self.entry_price = entry_price
        self.mid_price = mid_price
        self.fills = list(fills or ())
        self.fill_reads = 0

    async def get_market_price(self, market: str):
        return MarketPrice(
            market,
            bid=self.mid_price - Decimal("1"),
            ask=self.mid_price + Decimal("1"),
        )

    async def get_position_pnl(self, market: str):
        del market
        return PositionPnl(
            unrealized_pnl=Decimal("0"),
            entry_price=self.entry_price,
            position_value=None,
        )

    async def get_fills_by_time(
        self,
        market: str,
        start_time: float,
        end_time: float,
    ) -> list[dict]:
        del market, start_time, end_time
        self.fill_reads += 1
        return list(self.fills)


class _LedgerHedge(_ExtendedAdapter):
    """提供可控入场价、盘口与成交历史的对冲腿测试桩。"""

    def __init__(
        self,
        name: str = "hyperliquid",
        *,
        entry_price: Decimal = Decimal("102"),
        mid_price: Decimal = Decimal("105"),
        fills: list[dict] | None = None,
    ) -> None:
        super().__init__(name)
        self.entry_price = entry_price
        self.mid_price = mid_price
        self.fills = list(fills or ())
        self.fill_reads = 0

    async def get_market_price(self, market_name: str):
        return MarketPrice(
            market_name,
            bid=self.mid_price - Decimal("1"),
            ask=self.mid_price + Decimal("1"),
        )

    async def get_position_pnl(self, market: str):
        del market
        return PositionPnl(
            unrealized_pnl=Decimal("0"),
            entry_price=self.entry_price,
            position_value=None,
        )

    async def get_fills_by_time(
        self,
        market: str,
        start_time: float,
        end_time: float,
    ) -> list[dict]:
        del market, start_time, end_time
        self.fill_reads += 1
        return list(self.fills)


class _SignalCandleLoader:
    """返回两腿可控的 Hyperliquid 5 分钟 K 线并记录请求参数。"""

    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, int, int]] = []

    async def __call__(
        self,
        coin: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[dict]:
        self.calls.append((coin, interval, start_time_ms, end_time_ms))
        return list(self.responses[coin])


def _signal_candles(
    premiums: tuple[Decimal, ...],
    *,
    start_ms: int = 1_000_000,
) -> dict[str, list[dict]]:
    """以对冲腿价格 100 构造能精确还原指定百分比基差的 K 线。"""
    primary: list[dict] = []
    hedge: list[dict] = []
    for index, premium in enumerate(premiums):
        timestamp = start_ms + index * 300_000
        primary.append({"t": timestamp, "c": str(Decimal("100") + premium)})
        hedge.append({"t": timestamp, "c": "100"})
    return {"BTC": primary, "BTC-USD": hedge}


_SIGNAL_HISTORY = (
    Decimal("-0.2"),
    Decimal("-0.1"),
    Decimal("0"),
    Decimal("0.1"),
    Decimal("0.2"),
)


def test_hyperliquid_candle_loader_posts_exact_snapshot_request(monkeypatch) -> None:
    """历史统计必须直接使用指定的 Hyperliquid candleSnapshot 请求结构。"""
    from timed_volume import strategy as strategy_module

    captured: dict[str, object] = {}

    class FakeResponse:
        def read(self) -> bytes:
            return b'[{"t":123,"c":"100"}]'

        def close(self) -> None:
            captured["closed"] = True

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["content_type"] = request.get_header("Content-type")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(strategy_module, "urlopen", fake_urlopen)

    candles = strategy_module._load_hyperliquid_candles_sync(
        "io:SNDK",
        "5m",
        1_000,
        2_000,
    )

    assert candles == [{"t": 123, "c": "100"}]
    assert captured == {
        "url": "https://api.hyperliquid.xyz/info",
        "method": "POST",
        "content_type": "application/json",
        "payload": {
            "type": "candleSnapshot",
            "req": {
                "coin": "io:SNDK",
                "interval": "5m",
                "startTime": 1_000,
                "endTime": 2_000,
            },
        },
        "timeout": strategy_module.SIGNAL_HTTP_TIMEOUT_SECONDS,
        "closed": True,
    }


def test_equity_read_failure_does_not_block_close_or_next_open(tmp_path) -> None:
    """权益记账失败不得改变平仓结果、触发互锁或阻止下一轮。"""
    equity_path = tmp_path / "equity.jsonl"
    lighter = _LighterAdapter(
        "lighter",
        balance_error=RuntimeError("权益接口暂时不可用"),
    )
    executor = _ScriptedExecutor()
    api, strategy, _, extended = _build_strategy(
        tmp_path,
        lighter=lighter,
        executor=executor,
        equity_path=equity_path,
    )

    opened = asyncio.run(strategy.run_once(now=0.0))
    closed = asyncio.run(strategy.run_once(now=7200.0))
    next_opened = asyncio.run(strategy.run_once(now=7200.0))

    assert opened.action == "opened"
    assert closed.action == "closed"
    assert closed.hedge_available is True
    assert strategy.hedge_interlock_active is False
    assert not equity_path.exists()
    assert next_opened.action == "opened"
    assert next_opened.direction is api.RoundDirection.SHORT
    assert lighter.position == -extended.position != 0


def test_equity_snapshot_is_written_only_after_both_legs_are_exactly_flat(
    tmp_path,
) -> None:
    """只有两腿实仓精确归零时才允许读取并写入权益。"""
    equity_path = tmp_path / "equity.jsonl"
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=_ScriptedExecutor(),
        equity_path=equity_path,
        position_tolerance=Decimal("0.00020"),
    )
    asyncio.run(strategy.run_once(now=0.0))

    strategy._trade_executor = _ScriptedExecutor(
        {
            "lighter": [{"actual_delta": "-0.00095"}],
            "extended": [{"actual_delta": "0.00095"}],
        }
    )
    closed_with_dust = asyncio.run(strategy.run_once(now=7200.0))

    assert closed_with_dust.action == "closed"
    assert closed_with_dust.primary_size == Decimal("0.00005")
    assert closed_with_dust.hedge_size == Decimal("-0.00005")
    assert lighter.balance_reads == 0
    assert extended.balance_reads == 0
    assert not equity_path.exists()


def test_exact_flat_close_appends_decimal_equity_strings(tmp_path) -> None:
    """精确平仓后按 Decimal 合计两腿权益，并以字符串追加 JSONL。"""
    equity_path = tmp_path / "equity.jsonl"
    lighter = _LighterAdapter(
        "lighter",
        balance_equity=Decimal("561.490000000000000001"),
    )
    extended = _ExtendedAdapter(
        "extended",
        balance_equity=Decimal("552.340000000000000002"),
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=_ScriptedExecutor(),
        equity_path=equity_path,
    )

    asyncio.run(strategy.run_once(now=0.0))
    closed = asyncio.run(strategy.run_once(now=7200.0))

    assert closed.action == "closed"
    row = json.loads(equity_path.read_text(encoding="utf-8"))
    assert row == {
        "ts": 7200.0,
        "round_index": 1,
        "primary_equity": "561.490000000000000001",
        "hedge_equity": "552.340000000000000002",
        "total_equity": "1113.830000000000000003",
    }


def test_ledger_write_failure_does_not_block_close_or_next_open(
    tmp_path,
    monkeypatch,
) -> None:
    """台账写盘异常不得改变平仓、互锁或下一轮方向。"""
    ledger_path = tmp_path / "round_ledger.jsonl"
    lighter = _LedgerLighter(mid_price=Decimal("100"))
    hedge = _LedgerHedge(name="variational", mid_price=Decimal("102"))
    executor = _ScriptedExecutor()
    api, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=hedge,
        executor=executor,
        ledger_path=ledger_path,
    )

    opened = asyncio.run(strategy.run_once(now=0.0))

    write_attempts: list[dict] = []

    def fail_write(payload: dict) -> None:
        write_attempts.append(payload)
        raise OSError("台账磁盘暂时不可写")

    monkeypatch.setattr(strategy, "_append_round_ledger", fail_write)
    closed = asyncio.run(strategy.run_once(now=7200.0))
    next_opened = asyncio.run(strategy.run_once(now=7200.0))

    assert opened.action == "opened"
    assert closed.action == "closed"
    assert closed.hedge_available is True
    assert strategy.hedge_interlock_active is False
    assert len(write_attempts) == 1
    assert write_attempts[0]["round_index"] == 1
    assert not ledger_path.exists()
    assert next_opened.action == "opened"
    assert next_opened.direction is api.RoundDirection.SHORT
    assert lighter.position == -hedge.position != 0


def test_round_ledger_is_written_only_after_close(tmp_path) -> None:
    """开仓和等待节拍不得记账，只有平仓完成才追加一行。"""
    ledger_path = tmp_path / "round_ledger.jsonl"
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("102")),
        executor=_ScriptedExecutor(),
        ledger_path=ledger_path,
    )

    opened = asyncio.run(strategy.run_once(now=0.0))
    waited = asyncio.run(strategy.run_once(now=1.0))

    assert opened.action == "opened"
    assert waited.action == "wait"
    assert not ledger_path.exists()

    closed = asyncio.run(strategy.run_once(now=7200.0))

    assert closed.action == "closed"
    rows = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["round_index"] == 1


def test_round_ledger_basis_change_uses_decimal_subtraction(tmp_path) -> None:
    """基差变化必须严格等于退出基差减入场基差。"""
    ledger_path = tmp_path / "round_ledger.jsonl"
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(
            entry_price=Decimal("100"),
            mid_price=Decimal("110"),
        ),
        extended=_LedgerHedge(
            name="variational",
            entry_price=Decimal("102"),
            mid_price=Decimal("105"),
        ),
        executor=_ScriptedExecutor(),
        ledger_path=ledger_path,
    )

    asyncio.run(strategy.run_once(now=0.0))
    asyncio.run(strategy.run_once(now=7200.0))
    row = json.loads(ledger_path.read_text(encoding="utf-8"))

    entry_basis = (Decimal("100") - Decimal("102")) / Decimal("102") * 100
    exit_basis = (Decimal("110") - Decimal("105")) / Decimal("105") * 100
    assert Decimal(row["entry_basis_pct"]) == entry_basis
    assert Decimal(row["exit_basis_pct"]) == exit_basis
    assert Decimal(row["basis_change_pct"]) == exit_basis - entry_basis
    assert row["exit_price_source"] == "mid"


def test_round_ledger_marks_fill_only_when_both_exit_averages_are_exact(
    tmp_path,
) -> None:
    """两腿成交历史都能聚合平仓均价时才标记 fill，并汇总整轮手续费。"""
    ledger_path = tmp_path / "round_ledger.jsonl"
    lighter = _LedgerLighter(
        mid_price=Decimal("100"),
        fills=[
            {"price": "100", "signed_size": "20", "fee": "9"},
            {"price": "111", "signed_size": "-20", "fee": "9"},
        ]
    )
    hedge = _LedgerHedge(
        fills=[
            {"price": "102", "signed_size": "-20", "fee": "0.3"},
            {"price": "104", "signed_size": "20", "fee": "0.4"},
        ]
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=hedge,
        executor=_ScriptedExecutor(),
        ledger_path=ledger_path,
        instance="lighter_entropy",
    )

    asyncio.run(strategy.run_once(now=0.0))
    asyncio.run(strategy.run_once(now=7200.0))
    row = json.loads(ledger_path.read_text(encoding="utf-8"))

    assert row["ts"] == 7200.0
    assert row["instance"] == "lighter_entropy"
    assert row["round_index"] == 1
    assert row["direction"] == "long"
    assert row["notional_usd"] == 2000
    assert row["symbol"] == "BTC"
    assert row["exit_price_source"] == "fill"
    assert row["primary"]["venue"] == "lighter"
    assert row["primary"]["entry"] == "100"
    assert row["primary"]["exit"] == "111"
    assert row["primary"]["size"] == "20.000"
    assert row["hedge"]["venue"] == "hyperliquid"
    assert row["hedge"]["entry"] == "102"
    assert row["hedge"]["exit"] == "104"
    assert row["hedge"]["size"] == "-20.000"
    assert row["primary"]["fee"] == "0"
    assert row["hedge"]["fee"] == "0.7"
    assert row["fee_total"] == "0.7"
    assert row["opened_at"] == 0.0
    assert row["held_seconds"] == 7200.0
    for field in (
        "entry_basis_pct",
        "exit_basis_pct",
        "basis_change_pct",
        "realized_pnl",
        "fee_total",
    ):
        assert isinstance(row[field], str)
    realized_pnl = (
        (Decimal("111") - Decimal(row["primary"]["entry"]))
        * Decimal(row["primary"]["size"])
        + (Decimal("104") - Decimal(row["hedge"]["entry"]))
        * Decimal(row["hedge"]["size"])
        - Decimal("0.7")
    )
    assert isinstance(row["realized_pnl"], str)
    assert Decimal(row["realized_pnl"]) == realized_pnl


def test_round_ledger_entry_context_survives_strategy_restart(tmp_path) -> None:
    """入场均价与实际数量必须随轮次状态持久化，重启后仍可完成记账。"""
    ledger_path = tmp_path / "round_ledger.jsonl"
    lighter = _LedgerLighter(
        entry_price=Decimal("79502.500000000000000001"),
        mid_price=Decimal("79610"),
    )
    hedge = _LedgerHedge(
        name="variational",
        entry_price=Decimal("79524"),
        mid_price=Decimal("79625"),
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=hedge,
        executor=_ScriptedExecutor(),
        ledger_path=ledger_path,
    )
    asyncio.run(strategy.run_once(now=0.0))

    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=hedge,
        executor=_ScriptedExecutor(),
        ledger_path=ledger_path,
    )
    closed = asyncio.run(restarted.run_once(now=7200.0))
    row = json.loads(ledger_path.read_text(encoding="utf-8"))

    assert closed.action == "closed"
    assert row["primary"]["entry"] == "79502.500000000000000001"
    assert row["hedge"]["entry"] == "79524"
    assert Decimal(row["primary"]["size"]) > 0
    assert Decimal(row["hedge"]["size"]) < 0


def test_missing_ledger_path_preserves_existing_behavior(tmp_path) -> None:
    """未启用台账时不得查询成交历史或在状态文件写入台账元数据。"""
    lighter = _LedgerLighter()
    hedge = _LedgerHedge()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=hedge,
        executor=_ScriptedExecutor(),
        ledger_path=None,
    )

    opened = asyncio.run(strategy.run_once(now=0.0))
    closed = asyncio.run(strategy.run_once(now=7200.0))

    assert opened.action == "opened"
    assert closed.action == "closed"
    assert lighter.fill_reads == 0
    assert hedge.fill_reads == 0
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert not any(key.startswith("ledger_") for key in state)


def test_basis_gate_waits_when_relative_deviation_exceeds_threshold(
    tmp_path,
) -> None:
    """相对偏离超过 1.5 倍标准差时不得调用执行器。"""
    ledger_path = tmp_path / "basis_ledger.jsonl"
    _write_basis_history(ledger_path, _HIGH_VARIANCE_BASIS_HISTORY)
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100160")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=executor,
        ledger_path=ledger_path,
        basis_gate_sigma=Decimal("1.5"),
    )

    _, standard_deviation = strategy._basis_statistics(
        list(_HIGH_VARIANCE_BASIS_HISTORY)
    )
    result = asyncio.run(strategy.run_once(now=100.0))

    assert standard_deviation == Decimal("0.08")
    assert result.action == "basis_waiting"
    assert result.basis_gate_state == "waiting"
    assert result.basis_gate_deviation == Decimal("0.16")
    assert result.basis_gate_waited_seconds == 0.0
    assert strategy.state.basis_gate_wait_started_at == 100.0
    assert executor.calls == []


def test_basis_gate_opens_after_deviation_returns_inside_threshold(tmp_path) -> None:
    """等待中的偏离回到 1.5 倍标准差内后应正常开仓。"""
    ledger_path = tmp_path / "basis_ledger.jsonl"
    _write_basis_history(ledger_path, _HIGH_VARIANCE_BASIS_HISTORY)
    lighter = _LedgerLighter(mid_price=Decimal("100160"))
    executor = _ScriptedExecutor()
    _, strategy, _, hedge = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=executor,
        ledger_path=ledger_path,
        basis_gate_sigma=Decimal("1.5"),
    )

    waiting = asyncio.run(strategy.run_once(now=100.0))
    assert executor.calls == []
    lighter.mid_price = Decimal("100080")
    opened = asyncio.run(strategy.run_once(now=130.0))

    _, standard_deviation = strategy._basis_statistics(
        list(_HIGH_VARIANCE_BASIS_HISTORY)
    )
    assert standard_deviation == Decimal("0.08")
    assert waiting.action == "basis_waiting"
    assert executor.calls
    assert opened.action == "opened"
    assert opened.basis_gate_state == "open"
    assert opened.basis_gate_deviation == Decimal("0.08")
    assert opened.basis_gate_waited_seconds == 30.0
    assert strategy.state.basis_gate_wait_started_at is None
    assert lighter.position == -hedge.position != 0


def test_basis_gate_forces_open_after_max_wait(tmp_path) -> None:
    """异常基差累计等待达到上限后应强制开仓并标记 forced。"""
    ledger_path = tmp_path / "basis_ledger.jsonl"
    _write_basis_history(ledger_path, _HIGH_VARIANCE_BASIS_HISTORY)
    executor = _ScriptedExecutor()
    _, strategy, lighter, hedge = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100160")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=executor,
        ledger_path=ledger_path,
        basis_gate_sigma=Decimal("1.5"),
        basis_gate_max_wait_s=60.0,
    )

    waiting = asyncio.run(strategy.run_once(now=100.0))
    assert executor.calls == []
    forced = asyncio.run(strategy.run_once(now=160.0))

    _, standard_deviation = strategy._basis_statistics(
        list(_HIGH_VARIANCE_BASIS_HISTORY)
    )
    assert standard_deviation == Decimal("0.08")
    assert waiting.action == "basis_waiting"
    assert forced.action == "opened"
    assert forced.basis_gate_state == "forced"
    assert forced.basis_gate_deviation == Decimal("0.16")
    assert forced.basis_gate_waited_seconds == 60.0
    assert strategy.state.basis_gate_wait_started_at is None
    assert executor.calls
    assert lighter.position == -hedge.position != 0


def test_basis_gate_allows_low_standard_deviation_history(tmp_path) -> None:
    """结构性持久基差的历史波动低于下限时应直接放行。"""
    low_variance_history = (
        Decimal("0.104"),
        Decimal("0.114"),
        Decimal("0.124"),
        Decimal("0.134"),
        Decimal("0.144"),
    )
    ledger_path = tmp_path / "basis_ledger.jsonl"
    _write_basis_history(ledger_path, low_variance_history)
    executor = _ScriptedExecutor()
    api, strategy, lighter, hedge = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100400")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=executor,
        ledger_path=ledger_path,
        basis_gate_sigma=Decimal("1.5"),
    )

    _, standard_deviation = strategy._basis_statistics(list(low_variance_history))
    result = asyncio.run(strategy.run_once(now=100.0))

    assert standard_deviation == Decimal("0.0002").sqrt()
    assert standard_deviation < api.BASIS_GATE_STD_FLOOR_PCT
    assert abs(Decimal("0.4") - Decimal("0.124")) > (
        strategy.config.basis_gate_sigma * standard_deviation
    )
    assert result.action == "opened"
    assert result.basis_gate_state == "open"
    assert result.basis_gate_deviation == Decimal("0.276")
    assert executor.calls
    assert lighter.position == -hedge.position != 0


def test_basis_gate_allows_insufficient_history(tmp_path) -> None:
    """历史样本少于最小数量时即使偏离很大也应直接放行。"""
    insufficient_history = (
        Decimal("-0.12"),
        Decimal("-0.04"),
        Decimal("0.04"),
        Decimal("0.12"),
    )
    ledger_path = tmp_path / "basis_ledger.jsonl"
    _write_basis_history(ledger_path, insufficient_history)
    executor = _ScriptedExecutor()
    api, strategy, lighter, hedge = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100200")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=executor,
        ledger_path=ledger_path,
        basis_gate_sigma=Decimal("1.5"),
    )

    _, standard_deviation = strategy._basis_statistics(list(insufficient_history))
    result = asyncio.run(strategy.run_once(now=100.0))

    assert len(insufficient_history) == api.BASIS_GATE_MIN_HISTORY - 1
    assert standard_deviation == Decimal("0.008").sqrt()
    assert standard_deviation > api.BASIS_GATE_STD_FLOOR_PCT
    assert Decimal("0.2") > strategy.config.basis_gate_sigma * standard_deviation
    assert result.action == "opened"
    assert result.basis_gate_state == "open"
    assert result.basis_gate_deviation is None
    assert executor.calls
    assert lighter.position == -hedge.position != 0


def test_basis_gate_falls_back_to_heartbeat_when_ledger_is_missing(
    tmp_path,
) -> None:
    """台账文件不存在时应从心跳读取足量开仓基差。"""
    ledger_path = tmp_path / "missing_ledger.jsonl"
    heartbeat_path = tmp_path / "heartbeat.jsonl"
    _write_basis_history(
        heartbeat_path,
        _HIGH_VARIANCE_BASIS_HISTORY,
        heartbeat=True,
    )
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100160")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=executor,
        ledger_path=ledger_path,
        heartbeat_path=heartbeat_path,
        basis_gate_sigma=Decimal("1.5"),
    )

    history = strategy._read_basis_history()
    _, standard_deviation = strategy._basis_statistics(history)
    result = asyncio.run(strategy.run_once(now=100.0))

    assert not ledger_path.exists()
    assert history == list(_HIGH_VARIANCE_BASIS_HISTORY)
    assert standard_deviation == Decimal("0.08")
    assert result.action == "basis_waiting"
    assert result.basis_gate_state == "waiting"
    assert result.basis_gate_deviation == Decimal("0.16")
    assert executor.calls == []


def test_zero_basis_gate_sigma_matches_disabled_gate(
    tmp_path,
    monkeypatch,
) -> None:
    """sigma 为零时不读取门控历史，开仓结果与未配置历史完全一致。"""
    heartbeat_path = tmp_path / "heartbeat.jsonl"
    _write_basis_history(
        heartbeat_path,
        _HIGH_VARIANCE_BASIS_HISTORY,
        heartbeat=True,
    )
    baseline_executor = _ScriptedExecutor()
    _, baseline, baseline_lighter, baseline_hedge = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100160")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=baseline_executor,
        state_name="baseline_state.json",
    )
    disabled_executor = _ScriptedExecutor()
    _, disabled, disabled_lighter, disabled_hedge = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100160")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=disabled_executor,
        state_name="disabled_state.json",
        heartbeat_path=heartbeat_path,
        basis_gate_sigma=Decimal("0"),
    )
    disabled.state.basis_gate_wait_started_at = 50.0
    disabled._save_state()

    def fail_if_history_is_read():
        raise AssertionError("sigma 为零时不得读取基差历史")

    monkeypatch.setattr(disabled, "_read_basis_history", fail_if_history_is_read)
    _, standard_deviation = baseline._basis_statistics(
        list(_HIGH_VARIANCE_BASIS_HISTORY)
    )
    baseline_result = asyncio.run(baseline.run_once(now=100.0))
    disabled_result = asyncio.run(disabled.run_once(now=100.0))

    assert standard_deviation == Decimal("0.08")
    assert disabled_result == baseline_result
    assert disabled_executor.calls == baseline_executor.calls
    assert disabled_lighter.position == baseline_lighter.position
    assert disabled_hedge.position == baseline_hedge.position
    assert disabled.state.basis_gate_wait_started_at is None


def test_basis_gate_wait_does_not_block_expired_position_close(
    tmp_path,
    monkeypatch,
) -> None:
    """门控已进入等待时，后来发现的到期持仓仍必须优先平仓。"""
    heartbeat_path = tmp_path / "heartbeat.jsonl"
    _write_basis_history(
        heartbeat_path,
        _HIGH_VARIANCE_BASIS_HISTORY,
        heartbeat=True,
    )
    executor = _ScriptedExecutor()
    api, strategy, lighter, hedge = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100160")),
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100000")),
        executor=executor,
        heartbeat_path=heartbeat_path,
        basis_gate_sigma=Decimal("1.5"),
    )

    waiting = asyncio.run(strategy.run_once(now=100.0))
    lighter.position = Decimal("0.020")
    hedge.position = Decimal("-0.020")
    strategy.state.round_index = 1
    strategy.state.current_direction = api.RoundDirection.LONG
    strategy.state.current_notional_usd = 2000
    strategy.state.opened_at = 100.0
    strategy.state.due_at = 200.0
    strategy._save_state()

    async def fail_if_gate_is_evaluated(now: float):
        del now
        raise AssertionError("到期平仓不得评估开仓基差门控")

    monkeypatch.setattr(strategy, "_basis_gate_allows_open", fail_if_gate_is_evaluated)
    closed = asyncio.run(strategy.run_once(now=200.0))

    _, standard_deviation = strategy._basis_statistics(
        list(_HIGH_VARIANCE_BASIS_HISTORY)
    )
    assert standard_deviation == Decimal("0.08")
    assert waiting.action == "basis_waiting"
    assert waiting.basis_gate_state == "waiting"
    assert waiting.basis_gate_deviation == Decimal("0.16")
    assert closed.action == "closed"
    assert closed.basis_gate_deviation is None
    assert [call["reduce_only"] for call in executor.calls] == [True, True]
    assert lighter.position == 0
    assert hedge.position == 0


def test_timer_mode_ignores_all_signal_parameters_and_never_loads_candles(
    tmp_path,
) -> None:
    """默认 timer 必须保持机械交替，所有 signal 参数都不能改变交易路径。"""

    async def forbidden_loader(*args):
        del args
        raise AssertionError("timer 模式不得读取信号 K 线")

    executor = _ScriptedExecutor()
    api, strategy, _, _ = _build_strategy(
        tmp_path,
        executor=executor,
        entry_mode="timer",
        signal_sigma=Decimal("-1"),
        signal_lookback_hours=0,
        signal_refresh_minutes=0,
        signal_min_samples=0,
        max_hold_hours=0,
        signal_fallback_hours=0,
        candle_loader=forbidden_loader,
    )

    first = asyncio.run(strategy.run_once(now=0.0))
    before_due = asyncio.run(strategy.run_once(now=35.0))
    closed = asyncio.run(strategy.run_once(now=7200.0))
    second = asyncio.run(strategy.run_once(now=7200.0))

    assert first.action == "opened"
    assert first.direction is api.RoundDirection.LONG
    assert before_due.action == "wait"
    assert closed.action == "closed"
    assert second.action == "opened"
    assert second.direction is api.RoundDirection.SHORT


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"signal_sigma": Decimal("0")}, "标准差倍数"),
        ({"signal_lookback_hours": 0}, "回看小时数"),
        ({"signal_refresh_minutes": 0}, "刷新分钟数"),
        ({"signal_min_samples": 0}, "最少样本"),
        ({"max_hold_hours": 0}, "最长持仓小时数"),
        ({"signal_fallback_hours": -1}, "兜底小时数"),
    ],
)
def test_signal_mode_rejects_unsafe_numeric_configuration(
    tmp_path,
    overrides,
    message,
) -> None:
    """signal 模式必须在启动时拒绝会破坏统计或风险上限的数值。"""
    with pytest.raises(ValueError, match=message):
        _build_strategy(tmp_path, entry_mode="signal", **overrides)


@pytest.mark.parametrize(
    ("primary_mid", "expected_direction", "expected_primary_sign"),
    [
        (Decimal("100.30"), "short", Decimal("-1")),
        (Decimal("99.70"), "long", Decimal("1")),
    ],
)
def test_signal_entry_direction_follows_concrete_deviation_values(
    tmp_path,
    primary_mid,
    expected_direction,
    expected_primary_sign,
) -> None:
    """正偏离开空基差、负偏离开多基差，具体订单符号不得反转。"""
    loader = _SignalCandleLoader(_signal_candles(_SIGNAL_HISTORY))
    executor = _ScriptedExecutor()
    api, strategy, primary, hedge = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=primary_mid),
        extended=_LedgerHedge(name="hyperliquid", mid_price=Decimal("100")),
        executor=executor,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=0,
        candle_loader=loader,
    )

    result = asyncio.run(strategy.run_once(now=10_000.0))

    assert result.action == "opened"
    assert result.direction is api.RoundDirection(expected_direction)
    assert result.signal_midline == Decimal("0")
    assert result.signal_sigma == Decimal("0.02").sqrt()
    assert result.signal_deviation == primary_mid - Decimal("100")
    assert executor.calls[0]["target_delta"] * expected_primary_sign > 0
    assert primary.position * expected_primary_sign > 0
    assert hedge.position * expected_primary_sign < 0
    assert [call[1] for call in loader.calls] == ["5m", "5m"]


def test_signal_inside_threshold_does_not_open(tmp_path) -> None:
    """偏离位于正负阈值之间时不得调用交易执行器。"""
    loader = _SignalCandleLoader(_signal_candles(_SIGNAL_HISTORY))
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100.10")),
        extended=_LedgerHedge(name="hyperliquid", mid_price=Decimal("100")),
        executor=executor,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=0,
        candle_loader=loader,
    )

    result = asyncio.run(strategy.run_once(now=10_000.0))

    assert result.action == "signal_waiting"
    assert result.signal_state == "within_threshold"
    assert result.signal_deviation == Decimal("0.10")
    assert executor.calls == []


def test_signal_statistics_refresh_only_after_configured_interval(tmp_path) -> None:
    """空仓轮询可每拍读当前中价，但历史 K 线只能按配置周期刷新。"""
    loader = _SignalCandleLoader(_signal_candles(_SIGNAL_HISTORY))
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100.10")),
        extended=_LedgerHedge(name="hyperliquid", mid_price=Decimal("100")),
        executor=_ScriptedExecutor(),
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_refresh_minutes=15,
        signal_fallback_hours=0,
        candle_loader=loader,
    )

    first = asyncio.run(strategy.run_once(now=10_000.0))
    cached = asyncio.run(strategy.run_once(now=10_899.0))
    refreshed = asyncio.run(strategy.run_once(now=10_900.0))

    assert [first.action, cached.action, refreshed.action] == [
        "signal_waiting",
        "signal_waiting",
        "signal_waiting",
    ]
    assert len(loader.calls) == 4
    assert loader.calls[0][2:] == loader.calls[1][2:]
    assert loader.calls[2][2:] == loader.calls[3][2:]
    assert loader.calls[2][3] - loader.calls[0][3] == 900_000


def test_signal_insufficient_aligned_samples_refuses_open_and_explains_heartbeat(
    tmp_path,
) -> None:
    """样本不足既不能开仓也不能触发兜底，心跳必须给出中文原因。"""
    candles = _signal_candles(_SIGNAL_HISTORY)
    candles["BTC-USD"] = candles["BTC-USD"][:4]
    loader = _SignalCandleLoader(candles)
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("101")),
        extended=_LedgerHedge(name="hyperliquid", mid_price=Decimal("100")),
        executor=executor,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=0.001,
        candle_loader=loader,
    )

    first = asyncio.run(strategy.run_once(now=10_000.0))
    after_fallback_deadline = asyncio.run(strategy.run_once(now=20_000.0))

    assert first.action == "signal_waiting"
    assert after_fallback_deadline.action == "signal_waiting"
    assert after_fallback_deadline.signal_state == "insufficient_samples"
    assert after_fallback_deadline.signal_sample_count == 4
    assert after_fallback_deadline.signal_reason == "基差信号样本不足：4/5，拒绝开仓"
    assert executor.calls == []


def test_signal_position_closes_when_positive_deviation_crosses_midline(
    tmp_path,
) -> None:
    """正偏离建立的空基差仓在 deviation 由正变为负时立即平仓。"""
    loader = _SignalCandleLoader(_signal_candles(_SIGNAL_HISTORY))
    executor = _ScriptedExecutor()
    primary = _LedgerLighter(mid_price=Decimal("100.30"))
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        extended=_LedgerHedge(name="hyperliquid", mid_price=Decimal("100")),
        executor=executor,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=0,
        candle_loader=loader,
    )
    opened = asyncio.run(strategy.run_once(now=10_000.0))
    primary.mid_price = Decimal("99.95")

    closed = asyncio.run(strategy.run_once(now=10_030.0))

    assert opened.direction.value == "short"
    assert closed.action == "closed"
    assert closed.close_reason == "reverted"
    assert closed.signal_deviation == Decimal("-0.05")
    assert [call["reduce_only"] for call in executor.calls[-2:]] == [True, True]


def test_signal_position_closes_at_max_hold_even_without_fresh_statistics(
    tmp_path,
) -> None:
    """达到最长持仓时间必须优先强平，不得被行情刷新或样本状态阻塞。"""
    loader = _SignalCandleLoader(_signal_candles(_SIGNAL_HISTORY))
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100.30")),
        extended=_LedgerHedge(name="hyperliquid", mid_price=Decimal("100")),
        executor=executor,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        max_hold_hours=2,
        signal_fallback_hours=0,
        candle_loader=loader,
    )
    opened = asyncio.run(strategy.run_once(now=10_000.0))
    loader.responses = {}

    closed = asyncio.run(strategy.run_once(now=17_200.0))

    assert opened.due_at == 17_200.0
    assert closed.action == "closed"
    assert closed.close_reason == "timeout"


def test_signal_close_failure_keeps_retrying_even_if_deviation_moves_back(
    tmp_path,
) -> None:
    """回归触发平仓后若失败，后续必须沿用原因重试，不能重新变成持仓等待。"""
    loader = _SignalCandleLoader(_signal_candles(_SIGNAL_HISTORY))
    primary = _LedgerLighter(name="variational", mid_price=Decimal("100.30"))
    hedge = _LedgerHedge(name="hyperliquid", mid_price=Decimal("100"))
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        extended=hedge,
        executor=_ScriptedExecutor(),
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=0,
        candle_loader=loader,
    )
    asyncio.run(strategy.run_once(now=10_000.0))
    restricted = _RestrictedVariationalCloseExecutor()
    strategy._trade_executor = restricted
    primary.mid_price = Decimal("99.95")

    failed = asyncio.run(strategy.run_once(now=10_030.0))
    calls_after_trigger = len(restricted.calls)
    primary.mid_price = Decimal("100.30")
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        extended=hedge,
        executor=restricted,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=0,
        candle_loader=loader,
    )
    retried = asyncio.run(restarted.run_once(now=10_031.0))

    assert failed.action == "close_failed_neutral"
    assert restarted.state.pending_close_reason == "reverted"
    assert retried.action in {"close_failed_neutral", "close_halted"}
    assert len(restricted.calls) > calls_after_trigger
    assert retried.close_reason == "reverted"


@pytest.mark.parametrize(
    ("fallback_hours", "expected_action"),
    [(1.0, "opened"), (0.0, "signal_waiting")],
)
def test_signal_fallback_opens_on_deadline_and_zero_disables_it(
    tmp_path,
    fallback_hours,
    expected_action,
) -> None:
    """可靠统计下持续无信号才可兜底，零值必须彻底关闭兜底。"""
    loader = _SignalCandleLoader(_signal_candles(_SIGNAL_HISTORY))
    executor = _ScriptedExecutor()
    api, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=_LedgerLighter(mid_price=Decimal("100.05")),
        extended=_LedgerHedge(name="hyperliquid", mid_price=Decimal("100")),
        executor=executor,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=fallback_hours,
        candle_loader=loader,
    )
    waiting = asyncio.run(strategy.run_once(now=10_000.0))

    result = asyncio.run(strategy.run_once(now=13_600.0))

    assert waiting.action == "signal_waiting"
    assert result.action == expected_action
    if fallback_hours:
        assert result.direction is api.RoundDirection.LONG
        assert result.entry_trigger == "fallback"
        assert executor.calls
        closed = asyncio.run(strategy.run_once(now=result.due_at))
        assert closed.action == "closed"
        assert closed.close_reason == "fallback"
    else:
        assert result.signal_state == "within_threshold"
        assert executor.calls == []


def test_signal_ledger_records_entry_statistics_and_reversion_reason(
    tmp_path,
) -> None:
    """完成的信号轮必须留下可复算的入场统计与退出原因。"""
    ledger_path = tmp_path / "signal_ledger.jsonl"
    loader = _SignalCandleLoader(_signal_candles(_SIGNAL_HISTORY))
    primary = _LedgerLighter(mid_price=Decimal("100.30"))
    _, strategy, _, hedge = _build_strategy(
        tmp_path,
        lighter=primary,
        extended=_LedgerHedge(name="variational", mid_price=Decimal("100")),
        executor=_ScriptedExecutor(),
        ledger_path=ledger_path,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=0,
        candle_loader=loader,
    )
    asyncio.run(strategy.run_once(now=10_000.0))
    primary.mid_price = Decimal("99.95")
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        extended=hedge,
        executor=_ScriptedExecutor(),
        ledger_path=ledger_path,
        entry_mode="signal",
        signal_sigma=Decimal("1"),
        signal_min_samples=5,
        signal_fallback_hours=0,
        candle_loader=loader,
    )

    asyncio.run(restarted.run_once(now=10_030.0))
    row = json.loads(ledger_path.read_text(encoding="utf-8"))

    assert Decimal(row["entry_deviation_pct"]) == Decimal("0.30")
    assert Decimal(row["entry_midline_pct"]) == Decimal("0")
    assert Decimal(row["entry_sigma_pct"]) == Decimal("0.02").sqrt()
    assert row["close_reason"] == "reverted"
    assert row["entry_trigger"] == "signal"


def test_primary_auth_error_reloads_client_and_skips_round(tmp_path) -> None:
    """3.1/3.2：主腿认证失效后重建客户端，本轮不得下单。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    class ExpiredPrimary(_LighterAdapter):
        async def get_position(self, market: str):
            del market
            raise TestAuthError("Cookie 已过期")

    replacement = _LighterAdapter("lighter")
    reload_calls = []

    def reload_primary():
        reload_calls.append(True)
        return replacement

    executor = _ScriptedExecutor()
    _, strategy, _, extended = _build_strategy(
        tmp_path,
        lighter=ExpiredPrimary("lighter"),
        extended=_ExtendedAdapter("extended"),
        executor=executor,
        auth_error_types=(TestAuthError,),
        on_auth_error=reload_primary,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reloaded"
    assert reload_calls == [True]
    assert strategy.primary is replacement
    assert executor.calls == []
    assert extended.position == 0


def test_primary_auth_reload_still_failing_interlocks_open(tmp_path) -> None:
    """3.3：重建后的主腿仍认证失败时必须互锁并告警。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    class ExpiredPrimary(_LighterAdapter):
        async def get_position(self, market: str):
            del market
            raise TestAuthError("Cookie 已过期")

    async def reload_primary():
        return ExpiredPrimary("lighter")

    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=ExpiredPrimary("lighter"),
        executor=executor,
        auth_error_types=(TestAuthError,),
        on_auth_error=reload_primary,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reload_failed"
    assert result.hedge_available is False
    assert "认证重载失败" in result.interlock_reason
    assert any("认证重载失败" in warning for warning in result.warnings)
    assert executor.calls == []


def test_primary_auth_error_from_minimum_query_also_reloads(tmp_path) -> None:
    """3.2：主腿元数据查询的认证异常不得被包装成普通互锁。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    primary = _LighterAdapter(
        "lighter",
        min_order_size_error=TestAuthError("数量约束认证失效"),
    )
    replacement = _LighterAdapter("lighter")
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        executor=_ScriptedExecutor(),
        auth_error_types=(TestAuthError,),
        on_auth_error=lambda: replacement,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reloaded"
    assert strategy.primary is replacement
    assert "认证已重载" in result.interlock_reason


def test_auth_failure_after_other_leg_fill_rolls_back_filled_leg(tmp_path) -> None:
    """3.4：主腿认证拒绝而对冲腿成交时，即使重载失败也必须回滚对冲腿。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    primary = _LighterAdapter("lighter")
    hedge = _ExtendedAdapter("extended")
    executor = _ScriptedExecutor(
        {
            "lighter": [
                {"fraction": "0", "error": TestAuthError("下单认证失效")},
            ],
            "extended": [{}, {}],
        }
    )

    def failed_reload():
        raise TestAuthError("新 Cookie 仍失效")

    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        extended=hedge,
        executor=executor,
        auth_error_types=(TestAuthError,),
        on_auth_error=failed_reload,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reload_failed"
    assert primary.position == 0
    assert hedge.position == 0
    assert executor.calls[-1]["adapter"] == "extended"
    assert executor.calls[-1]["target_delta"] == Decimal("20.000")
    assert executor.calls[-1]["reduce_only"] is True


def test_hedge_auth_failure_after_primary_fill_rolls_back_primary_leg(tmp_path) -> None:
    """Variational 对冲腿认证失效时停止开仓，并回滚已成交主腿。"""

    class TestAuthError(Exception):
        """测试专用认证异常。"""

    primary = _LighterAdapter("lighter")
    hedge = _ExtendedAdapter("variational")
    executor = _ScriptedExecutor(
        {
            "lighter": [{}, {}],
            "variational": [
                {"fraction": "0", "error": TestAuthError("Cookie 已过期")},
            ],
        }
    )

    def failed_reload():
        raise TestAuthError("新 Cookie 仍失效")

    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=primary,
        extended=hedge,
        executor=executor,
        auth_error_types=(TestAuthError,),
        on_hedge_auth_error=failed_reload,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "auth_reload_failed"
    assert result.hedge_available is False
    assert primary.position == 0
    assert hedge.position == 0
    assert executor.calls[-1]["adapter"] == "lighter"
    assert executor.calls[-1]["target_delta"] == Decimal("-20.000")
    assert executor.calls[-1]["reduce_only"] is True


def test_position_before_due_is_not_closed(tmp_path) -> None:
    """1.1：持仓未满周期时不得平仓。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))
    executor.calls.clear()

    result = asyncio.run(strategy.run_once(now=7199.0))

    assert result.action == "wait"
    assert executor.calls == []
    assert lighter.position == Decimal("20.000")
    assert extended.position == Decimal("-20.000")


def test_position_at_due_time_is_closed(tmp_path) -> None:
    """1.1：持仓达到周期时同步平掉两腿。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))

    result = asyncio.run(strategy.run_once(now=7200.0))

    assert result.action == "closed"
    assert lighter.position == 0
    assert extended.position == 0
    assert [call["reduce_only"] for call in executor.calls[-2:]] == [True, True]


def test_existing_position_does_not_add_another_open(tmp_path) -> None:
    """1.2：周期内已有仓位时不追加开仓。"""
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))
    first_open_calls = len(executor.calls)

    asyncio.run(strategy.run_once(now=100.0))
    asyncio.run(strategy.run_once(now=200.0))

    assert len(executor.calls) == first_open_calls


def test_direction_alternates_and_restart_keeps_sequence(tmp_path) -> None:
    """1.3：平仓后的下一轮反向，重启后不得重复上一方向。"""
    executor = _ScriptedExecutor()
    api, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))
    assert strategy.state.current_direction is api.RoundDirection.LONG
    asyncio.run(strategy.run_once(now=7200.0))
    assert lighter.position == 0
    assert extended.position == 0

    restarted_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=restarted_executor,
    )
    asyncio.run(restarted.run_once(now=7200.0))

    assert restarted.state.current_direction is api.RoundDirection.SHORT
    assert restarted_executor.calls[0]["target_delta"] == Decimal("-20.000")


def test_open_is_delta_neutral_and_close_is_flat(tmp_path) -> None:
    """1.4：开仓后净敞口为零，平仓后两侧均为零。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)

    opened = asyncio.run(strategy.run_once(now=0.0))
    assert opened.net_exposure == 0
    assert lighter.position == -extended.position != 0

    closed = asyncio.run(strategy.run_once(now=7200.0))
    assert closed.net_exposure == 0
    assert lighter.position == 0
    assert extended.position == 0


def test_pnl_read_failure_does_not_block_open_or_activate_interlock(tmp_path) -> None:
    """展示用盈亏查询失败时仍须正常开仓，并在心跳里写入空值。"""

    class PnlFailingLighter(_LighterAdapter):
        async def get_position_pnl(self, market: str):
            del market
            raise RuntimeError("展示接口暂时不可用")

    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(
        tmp_path,
        lighter=PnlFailingLighter("lighter"),
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))
    payload = importlib.import_module("tools.run_timed_volume").heartbeat_payload(
        result,
        now=1.0,
    )

    assert result.action == "opened"
    assert result.hedge_available is True
    assert lighter.position == -extended.position != 0
    assert payload["primary_pnl"] is None
    assert payload["hedge_pnl"] is None
    assert payload["primary_entry"] is None
    assert payload["hedge_entry"] is None
    assert payload["pair_pnl"] is None


def test_position_pnl_snapshots_are_combined_without_float_conversion(tmp_path) -> None:
    """两腿快照保留 Decimal 精度，且本对盈亏只做十进制加法。"""

    class PnlLighter(_LighterAdapter):
        async def get_position_pnl(self, market: str):
            del market
            return PositionPnl(
                unrealized_pnl=Decimal("4.330000000000000001"),
                entry_price=Decimal("77299.300000000000000001"),
                position_value=Decimal("1680.25"),
            )

    class PnlExtended(_ExtendedAdapter):
        async def get_position_pnl(self, market: str):
            del market
            return PositionPnl(
                unrealized_pnl=Decimal("-5.570000000000000002"),
                entry_price=Decimal("77301.1"),
                position_value=Decimal("1680.30"),
            )

    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=PnlLighter("lighter"),
        extended=PnlExtended("extended"),
        executor=_ScriptedExecutor(),
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.primary_pnl == Decimal("4.330000000000000001")
    assert result.hedge_pnl == Decimal("-5.570000000000000002")
    assert result.primary_entry == Decimal("77299.300000000000000001")
    assert result.hedge_entry == Decimal("77301.1")
    assert result.pair_pnl == Decimal("-1.240000000000000001")


def test_lighter_opened_but_extended_failed_rolls_back_lighter(
    tmp_path,
    caplog,
) -> None:
    """1.5：Extended 未建仓时必须按实际持仓回滚 Lighter。"""
    executor = _ScriptedExecutor(
        {
            "lighter": [{}, {}],
            "extended": [{"fraction": "0", "error": "对冲超时"}],
        }
    )
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "open_failed_flat"
    assert lighter.position == 0
    assert extended.position == 0
    assert executor.calls[-1]["adapter"] == "lighter"
    assert executor.calls[-1]["target_delta"] == Decimal("-20.000")
    assert "回滚 Lighter" in caplog.text


def test_extended_opened_but_lighter_failed_closes_extended(
    tmp_path,
    caplog,
) -> None:
    """1.6：Lighter 未成交时必须平掉 Extended。"""
    executor = _ScriptedExecutor(
        {
            "lighter": [{"fraction": "0", "error": "开仓超时"}],
            "extended": [{}, {}],
        }
    )
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "open_failed_flat"
    assert lighter.position == 0
    assert extended.position == 0
    assert executor.calls[-1]["adapter"] == "extended"
    assert executor.calls[-1]["target_delta"] == Decimal("20.000")
    assert "平掉 Extended" in caplog.text


def test_partial_fill_uses_actual_position_difference(tmp_path) -> None:
    """1.7：部分成交的补单量来自两侧实仓差，不相信执行器回报的委托量。"""
    executor = _ScriptedExecutor(
        {
            "lighter": [
                {"fraction": "0.4", "reported_fill": "20"},
                {},
            ],
            "extended": [{}],
        }
    )
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)

    result = asyncio.run(strategy.run_once(now=0.0))

    lighter_calls = [c for c in executor.calls if c["adapter"] == "lighter"]
    assert lighter_calls[1]["target_delta"] == Decimal("12.0000")
    assert result.net_exposure == 0
    assert lighter.position == Decimal("20.0000")
    assert extended.position == Decimal("-20.000")


def test_incident_rounding_gap_is_accepted_and_round_advances(tmp_path) -> None:
    """事故回放：跨所相差一个数量步长仍应在容差内完成轮次。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00129"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00128"),
    )
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert result.action != "execution_uncertain"
    assert result.round_index == 1
    assert lighter.position == Decimal("0.00129")
    assert extended.position == Decimal("-0.00128")
    assert result.net_exposure == Decimal("0.00001")
    assert len(executor.calls) == 2

    replayed = asyncio.run(strategy.run_once(now=1.0))
    assert replayed.action == "wait"
    assert replayed.action != "execution_uncertain"
    assert len(executor.calls) == 2


def test_exposure_above_dynamic_tolerance_supplements_from_actual_positions(
    tmp_path,
) -> None:
    """净敞口超过动态容差时，必须按重读实仓补齐较小的一腿。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
    )
    lighter.position = Decimal("0.00129")
    extended.position = Decimal("-0.00098")
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=100.0))

    assert result.action == "reconciled"
    assert len(executor.calls) == 1
    assert executor.calls[0]["adapter"] == "extended"
    assert executor.calls[0]["target_delta"] == Decimal("-0.00031")
    assert result.net_exposure == 0


def test_exposure_equal_to_dynamic_tolerance_still_supplements(tmp_path) -> None:
    """净敞口恰等于动态容差不算达成，必须继续按实仓补齐。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00010"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
    )
    lighter.position = Decimal("0.00100")
    extended.position = Decimal("-0.00080")
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
        position_tolerance=Decimal("0.00020"),
    )

    result = asyncio.run(strategy.run_once(now=100.0))

    assert result.action == "reconciled"
    assert len(executor.calls) == 1
    assert executor.calls[0]["adapter"] == "extended"
    assert executor.calls[0]["target_delta"] == Decimal("-0.00020")
    assert result.net_exposure == 0


def test_tradeable_neutral_legs_are_not_hidden_by_configured_tolerance(
    tmp_path,
) -> None:
    """两腿达到各自最小量时，即使小于配置容差也必须识别为真实对冲仓。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00010"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
    )
    lighter.position = Decimal("0.00015")
    extended.position = Decimal("-0.00015")
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
        position_tolerance=Decimal("0.00020"),
    )

    result = asyncio.run(strategy.run_once(now=100.0))

    assert result.action == "wait"
    assert result.round_index == 1
    assert result.primary_size == Decimal("0.00015")
    assert result.hedge_size == Decimal("-0.00015")
    assert executor.calls == []


def test_supplement_below_side_minimum_is_skipped_and_warned(
    tmp_path,
    caplog,
) -> None:
    """容差内缺口低于该侧最小下单量时，不得发送注定失败的补单。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    executor = _ScriptedExecutor(
        {"lighter": [{"actual_delta": "0.00095"}]}
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert result.net_exposure == Decimal("-0.00005")
    assert len(executor.calls) == 2
    assert "补齐所需数量" in caplog.text
    assert "低于 Lighter 最小下单量" in caplog.text


def test_neutral_partial_fills_do_not_trigger_target_gap_supplements(
    tmp_path,
) -> None:
    """两腿部分成交但净敞口已在容差内时，不得再按各自目标缺口补单。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    executor = _ScriptedExecutor(
        {
            "lighter": [
                {"actual_delta": "0.00050"},
                {"fraction": "0"},
            ],
            "extended": [
                {"actual_delta": "-0.00050"},
                {"fraction": "0"},
            ],
        }
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert result.round_index == 1
    assert result.primary_size == Decimal("0.00050")
    assert result.hedge_size == Decimal("-0.00050")
    assert len(executor.calls) == 2


def test_flatten_dust_below_each_side_minimum_closes_without_retry(
    tmp_path,
    caplog,
) -> None:
    """平仓只剩不可交易微仓时应结束轮次，不得反复提交残差。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00020"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    opening_executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=opening_executor,
        position_tolerance=Decimal("0.00020"),
    )
    opened = asyncio.run(strategy.run_once(now=0.0))
    assert opened.action == "opened"

    closing_executor = _ScriptedExecutor(
        {
            "lighter": [{"actual_delta": "-0.00095"}],
            "extended": [{"actual_delta": "0.00095"}],
        }
    )
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=closing_executor,
        position_tolerance=Decimal("0.00020"),
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(restarted.run_once(now=7200.0))

    assert result.action == "closed"
    assert result.primary_size == Decimal("0.00005")
    assert result.hedge_size == Decimal("-0.00005")
    assert restarted.state.current_direction is None
    assert len(closing_executor.calls) == 2
    assert "低于最小下单量" in caplog.text


def test_tradeable_close_residual_is_not_hidden_by_configured_tolerance(
    tmp_path,
) -> None:
    """配置数值容差大于最小量时，仍必须平掉达到最小量的可交易残余。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    extended = _ExtendedAdapter(
        "extended",
        min_order_size=Decimal("0.00010"),
        rounded_amount=Decimal("0.00100"),
    )
    opening_executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=opening_executor,
        position_tolerance=Decimal("0.00020"),
    )
    opened = asyncio.run(strategy.run_once(now=0.0))
    assert opened.action == "opened"
    lighter.position = Decimal("0.00015")
    extended.position = Decimal("0")

    closing_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=closing_executor,
        position_tolerance=Decimal("0.00020"),
    )

    result = asyncio.run(restarted.run_once(now=7200.0))

    assert result.action == "reconciled"
    assert lighter.position == 0
    assert extended.position == 0
    assert len(closing_executor.calls) == 1
    assert closing_executor.calls[0]["target_delta"] == Decimal("-0.00015")
    assert closing_executor.calls[0]["reduce_only"] is True


def test_minimum_query_failure_interlocks_without_uncertain_retry(
    tmp_path,
    caplog,
) -> None:
    """容差元数据未知时失败关闭并告警，不得落入成交未知死循环。"""
    lighter = _LighterAdapter(
        "lighter",
        min_order_size_error=RuntimeError("Lighter 市场元数据超时"),
    )
    extended = _ExtendedAdapter("extended")
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "interlocked"
    assert result.action != "execution_uncertain"
    assert result.hedge_available is False
    assert executor.calls == []
    assert "对冲容差" in caplog.text
    assert "市场元数据超时" in caplog.text


def test_variational_missing_minimum_interlocks_without_submitting_orders(
    tmp_path,
    caplog,
) -> None:
    """Variational 未返回最小量时策略必须明确拒绝，不能按零下单。"""
    variational = object.__new__(VariationalClient)
    variational._quantity_limits = {}

    async def get_position(market: str):
        return Position(market, Decimal("0"))

    async def request_quote(
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ):
        del underlying, side, qty, instrument_type, funding_interval_s, kind
        return {"margin_requirements": {"initial_margin": "0.2"}}

    async def get_supported_assets():
        return {"BTC": [{"asset": "BTC", "price": "60000"}]}

    variational.get_position = get_position
    variational.request_quote = request_quote
    variational.get_supported_assets = get_supported_assets
    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=variational,
        executor=executor,
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "interlocked"
    assert result.hedge_available is False
    assert executor.calls == []
    # 只断言稳定的关键信息（字段名 + 拒绝语义），不锁死具体文案措辞。
    assert "min_qty" in caplog.text
    assert "拒绝" in caplog.text


def test_rfq_availability_does_not_require_orderbook_capabilities(tmp_path) -> None:
    """RFQ 对冲腿缺少限价、单查和撤单能力时仍可通过可用性检查。"""
    rfq = _RfqAdapter()
    assert not hasattr(rfq, "place_limit_order")
    assert not hasattr(rfq, "get_order_by_id")
    assert not hasattr(rfq, "cancel_order")
    executor = _ScriptedExecutor()
    _, strategy, lighter, _ = _build_strategy(
        tmp_path,
        extended=rfq,
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert result.hedge_available is True
    assert lighter.position == -rfq.position != 0


@pytest.mark.parametrize(
    "capability",
    ["market_order", "get_market_price", "get_min_order_size"],
)
def test_rfq_availability_still_requires_common_capabilities(
    tmp_path,
    capability: str,
) -> None:
    """RFQ 也必须失败关闭缺失的公共交易能力。"""
    api = _api()
    rfq = _RfqAdapter()
    setattr(rfq, capability, None)
    _, strategy, _, _ = _build_strategy(tmp_path, extended=rfq)
    limits = api._OrderLimits(Decimal("0.001"), Decimal("0.001"))

    available = asyncio.run(strategy._check_hedge_available(limits))

    assert available is False
    assert strategy.hedge_interlock_active is True
    assert capability in strategy.hedge_interlock_reason


def test_mixed_execution_models_submit_both_legs_concurrently(tmp_path) -> None:
    """RFQ 与订单簿两腿用事件栅栏证明仍在同一节拍并发提交。"""

    async def scenario() -> tuple[object, object, _RfqAdapter, _LighterAdapter]:
        rfq_started = asyncio.Event()
        orderbook_started = asyncio.Event()

        class ConcurrentRfqAdapter(_RfqAdapter):
            async def market_order(
                self,
                market: str,
                side: Side,
                amount: Decimal,
                *,
                reduce_only: bool = False,
            ):
                rfq_started.set()
                await orderbook_started.wait()
                return await super().market_order(
                    market,
                    side,
                    amount,
                    reduce_only=reduce_only,
                )

        class ConcurrentOrderbookAdapter(_LighterAdapter):
            async def place_limit_order(
                self,
                market: str,
                side: Side,
                amount: Decimal,
                price: Decimal,
                *,
                post_only: bool = True,
                reduce_only: bool = False,
            ):
                orderbook_started.set()
                await rfq_started.wait()
                return await super().place_limit_order(
                    market,
                    side,
                    amount,
                    price,
                    post_only=post_only,
                    reduce_only=reduce_only,
                )

        rfq = ConcurrentRfqAdapter("variational")
        orderbook = ConcurrentOrderbookAdapter("lighter")
        _, strategy, _, _ = _build_strategy(
            tmp_path,
            lighter=rfq,
            extended=orderbook,
            maker_timeout_s=0.1,
        )
        results = await asyncio.wait_for(
            strategy._execute_pair(
                Decimal("1"),
                Decimal("-1"),
                reduce_only=False,
            ),
            timeout=0.5,
        )
        return results[0], results[1], rfq, orderbook

    rfq_result, orderbook_result, rfq, orderbook = asyncio.run(scenario())

    assert isinstance(rfq_result, HedgeFillResult)
    assert isinstance(orderbook_result, HedgeFillResult)
    assert rfq_result.used_taker is True
    assert orderbook_result.used_taker is False
    assert [event[0] for event in rfq.events] == ["market"]
    assert [event[0] for event in orderbook.events] == ["limit"]
    assert rfq.position == -orderbook.position == Decimal("1")


def test_restart_before_due_does_not_open_again(tmp_path) -> None:
    """1.8：未到期轮次重启后只等待原到期时刻。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))

    restarted_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=restarted_executor,
    )
    result = asyncio.run(restarted.run_once(now=7199.0))

    assert result.action == "wait"
    assert restarted_executor.calls == []


def test_restart_after_due_closes_immediately(tmp_path) -> None:
    """1.9：已过期轮次重启后第一轮立即平仓。"""
    executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(tmp_path, executor=executor)
    asyncio.run(strategy.run_once(now=0.0))

    restarted_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=restarted_executor,
    )
    result = asyncio.run(restarted.run_once(now=7201.0))

    assert result.action == "closed"
    assert lighter.position == 0
    assert extended.position == 0
    assert len(restarted_executor.calls) == 2


def test_persisted_state_mismatch_uses_actual_positions_and_warns(
    tmp_path,
    caplog,
) -> None:
    """1.10：记录称空仓但实仓已对冲时，以实仓恢复轮次并中文告警。"""
    api = _api()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 7,
                "last_direction": "short",
                "current_direction": None,
                "opened_at": None,
                "due_at": None,
            }
        ),
        encoding="utf-8",
    )
    lighter = _LighterAdapter("lighter")
    extended = _ExtendedAdapter("extended")
    lighter.position = Decimal("20")
    extended.position = Decimal("-20")
    executor = _ScriptedExecutor()
    config = api.TimedVolumeConfig(state_path=state_path, cycle_seconds=7200.0)
    strategy = api.TimedHedgedVolumeStrategy(
        lighter,
        extended,
        config,
        trade_executor=executor,
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(strategy.run_once(now=1000.0))

    assert result.action == "wait"
    assert strategy.state.current_direction is api.RoundDirection.LONG
    assert strategy.state.due_at == 8200.0
    assert executor.calls == []
    assert "持久化状态与实际持仓不一致" in caplog.text


def test_interlock_blocks_open_but_does_not_block_expired_close(tmp_path) -> None:
    """1.11：Extended 不可用只禁止开仓，不得阻断到期平仓。"""
    executor = _ScriptedExecutor()
    _, blocked, lighter, extended = _build_strategy(
        tmp_path,
        executor=executor,
        hedge_available=lambda: False,
    )
    skipped = asyncio.run(blocked.run_once(now=0.0))
    assert skipped.action == "interlocked"
    assert executor.calls == []

    healthy_executor = _ScriptedExecutor()
    _, healthy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=healthy_executor,
        hedge_available=lambda: True,
        state_name="open-state.json",
    )
    asyncio.run(healthy.run_once(now=0.0))

    close_executor = _ScriptedExecutor()
    _, expired, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=close_executor,
        hedge_available=lambda: False,
        state_name="open-state.json",
    )
    closed = asyncio.run(expired.run_once(now=7200.0))
    assert closed.action == "closed"
    assert lighter.position == 0
    assert extended.position == 0


def test_orders_use_maker_first_and_timeout_then_market(tmp_path) -> None:
    """1.12：两腿先挂 maker，只有未成交并到达超时才补市价。"""
    lighter = _LighterAdapter("lighter", maker_fill=Decimal("0"))
    extended = _ExtendedAdapter("extended", maker_fill=Decimal("0"))
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        maker_timeout_s=0.0,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "opened"
    assert [event[0] for event in lighter.events] == ["limit", "cancel", "market"]
    assert [event[0] for event in extended.events] == ["limit", "cancel", "market"]
    assert lighter.events[0][5] is True
    assert extended.events[0][5] is True
    assert lighter.position == -extended.position != 0


def test_close_failure_rebuilds_other_leg_instead_of_leaving_naked(tmp_path) -> None:
    """平仓腿持续故障时，已平的一侧必须重建反向仓以恢复净敞口为零。"""
    opening_executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(
        tmp_path,
        executor=opening_executor,
    )
    asyncio.run(strategy.run_once(now=0.0))

    closing_executor = _ScriptedExecutor(
        {
            "extended": [
                {"fraction": "0", "error": "Extended 平仓失败"},
                {"fraction": "0", "error": "Extended 平仓失败"},
                {"fraction": "0", "error": "Extended 平仓失败"},
                {"fraction": "0", "error": "Extended 平仓失败"},
            ]
        }
    )
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=closing_executor,
    )

    result = asyncio.run(restarted.run_once(now=7200.0))

    lighter_calls = [
        call for call in closing_executor.calls if call["adapter"] == "lighter"
    ]
    assert result.action == "close_failed_neutral"
    assert lighter_calls[-1]["target_delta"] == Decimal("20.000")
    assert lighter.position == Decimal("20.000")
    assert extended.position == Decimal("-20.000")
    assert result.net_exposure == 0


def test_restricted_variational_close_enters_halt_and_stops_flatten_calls(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    """连续三次真实部分失败后，后续节拍不得再次执行整轮平仓。"""
    _, strategy, _, _, _ = _build_restricted_close_strategy(tmp_path)
    flatten_calls = 0
    original_flatten = strategy._flatten_all

    async def counted_flatten(warnings, limits):
        nonlocal flatten_calls
        flatten_calls += 1
        return await original_flatten(warnings, limits)

    monkeypatch.setattr(strategy, "_flatten_all", counted_flatten)
    with caplog.at_level(logging.WARNING):
        failed = _enter_close_halt(strategy)
        halted = asyncio.run(strategy.run_once(now=7203.0))

    assert [result.action for result in failed] == [
        "close_failed_neutral",
        "close_failed_neutral",
        "close_halted",
    ]
    assert halted.action == "close_halted"
    assert flatten_calls == 3
    assert strategy.state.consecutive_close_failures == 3
    assert strategy.state.close_halted_since == 7202.0
    persisted = json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )
    assert persisted["consecutive_close_failures"] == 3
    assert persisted["close_halted_since"] == 7202.0
    assert sum("连续平仓失败已达 3 次" in message for message in caplog.messages) == 1


def test_close_halt_never_opens_a_new_round(tmp_path) -> None:
    """退避仍有效时，即使实际两腿已空，也不得在同一节拍开新轮次。"""
    _, strategy, variational, hyperliquid, executor = (
        _build_restricted_close_strategy(tmp_path)
    )
    _enter_close_halt(strategy)
    calls_before = len(executor.calls)
    variational.position = Decimal("0")
    hyperliquid.position = Decimal("0")

    result = asyncio.run(strategy.run_once(now=7203.0))

    assert result.action != "opened"
    assert len(executor.calls) == calls_before


def test_close_halt_expiry_retry_success_clears_persisted_state(tmp_path) -> None:
    """退避期满后只重试一次；成功平仓须清零计数并解除退避。"""
    _, strategy, variational, hyperliquid, executor = (
        _build_restricted_close_strategy(tmp_path)
    )
    _enter_close_halt(strategy)
    halted_since = strategy.state.close_halted_since
    assert halted_since == 7202.0
    executor.block_variational_close = False

    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=variational,
        extended=hyperliquid,
        executor=executor,
    )
    calls_before = len(executor.calls)
    still_halted = asyncio.run(
        restarted.run_once(
            now=halted_since + 899.0,
        )
    )
    recovered = asyncio.run(
        restarted.run_once(
            now=halted_since + 900.0,
        )
    )

    assert still_halted.action == "close_halted"
    assert len(executor.calls) == calls_before + 2
    assert recovered.action == "closed"
    assert restarted.state.consecutive_close_failures == 0
    assert restarted.state.close_halted_since is None
    persisted = json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )
    assert persisted["consecutive_close_failures"] == 0
    assert persisted["close_halted_since"] is None


def test_close_halt_rehedges_net_exposure_at_most_once_per_cycle(tmp_path) -> None:
    """退避中恢复一次中性；重启后同周期再次漂移仍不得重复下单。"""
    _, strategy, variational, hyperliquid, executor = (
        _build_restricted_close_strategy(tmp_path)
    )
    _enter_close_halt(strategy)
    halted_since = strategy.state.close_halted_since
    assert halted_since == 7202.0
    recovery_calls_before = sum(
        call["adapter"] == "hyperliquid" and not call["reduce_only"]
        for call in executor.calls
    )

    hyperliquid.position = Decimal("0")
    rehedged = asyncio.run(strategy.run_once(now=halted_since + 1))
    recovery_calls_after_first_drift = sum(
        call["adapter"] == "hyperliquid" and not call["reduce_only"]
        for call in executor.calls
    )

    assert rehedged.action == "close_halted"
    assert rehedged.net_exposure == 0
    assert variational.position == Decimal("20.000")
    assert recovery_calls_after_first_drift == recovery_calls_before + 1
    assert strategy.state.close_halt_rehedged_since == halted_since
    persisted = json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )
    assert persisted["close_halt_rehedged_since"] == halted_since

    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=variational,
        extended=hyperliquid,
        executor=executor,
    )
    hyperliquid.position = Decimal("0")
    repeated = asyncio.run(restarted.run_once(now=halted_since + 2))
    recovery_calls_after_restart = sum(
        call["adapter"] == "hyperliquid" and not call["reduce_only"]
        for call in executor.calls
    )

    assert repeated.action == "close_halted"
    assert repeated.net_exposure == Decimal("20.000")
    assert recovery_calls_after_restart == recovery_calls_after_first_drift


def test_failed_retry_starts_new_halt_cycle_with_one_rehedge_allowance(
    tmp_path,
) -> None:
    """期满重试再次失败须重置计时，并给新周期一次再对冲额度。"""
    _, strategy, variational, hyperliquid, executor = (
        _build_restricted_close_strategy(tmp_path)
    )
    _enter_close_halt(strategy)
    first_halted_since = strategy.state.close_halted_since
    assert first_halted_since == 7202.0
    recoveries_before_retry = sum(
        call["adapter"] == "hyperliquid" and not call["reduce_only"]
        for call in executor.calls
    )

    retried = asyncio.run(strategy.run_once(now=first_halted_since + 900.0))
    second_halted_since = strategy.state.close_halted_since
    recoveries_after_retry = sum(
        call["adapter"] == "hyperliquid" and not call["reduce_only"]
        for call in executor.calls
    )
    hyperliquid.position = Decimal("0")
    rehedged = asyncio.run(strategy.run_once(now=second_halted_since + 1.0))
    recoveries_in_new_cycle = sum(
        call["adapter"] == "hyperliquid" and not call["reduce_only"]
        for call in executor.calls
    )

    assert retried.action == "close_halted"
    assert second_halted_since == first_halted_since + 900.0
    assert strategy.state.consecutive_close_failures == 4
    assert recoveries_after_retry == recoveries_before_retry + 1
    assert rehedged.action == "close_halted"
    assert rehedged.net_exposure == 0
    assert recoveries_in_new_cycle == recoveries_after_retry + 1


def test_one_day_replay_has_twelve_alternating_rounds_and_48000_volume(
    tmp_path,
) -> None:
    """4.3：2 小时、2000 美元回放一天，完成 12 轮且刷量 48000 美元。"""
    executor = _ScriptedExecutor()
    api, strategy, _, _ = _build_strategy(tmp_path, executor=executor)
    directions = []
    volume = Decimal("0")

    opened = asyncio.run(strategy.run_once(now=0.0))
    directions.append(opened.direction)
    volume += Decimal("2000")
    for round_number in range(1, 13):
        boundary = round_number * 7200.0
        closed = asyncio.run(strategy.run_once(now=boundary))
        assert closed.action == "closed"
        volume += Decimal("2000")
        if round_number < 12:
            opened = asyncio.run(strategy.run_once(now=boundary))
            assert opened.action == "opened"
            directions.append(opened.direction)
            volume += Decimal("2000")

    assert len(directions) == 12
    assert directions == [
        api.RoundDirection.LONG if index % 2 == 0 else api.RoundDirection.SHORT
        for index in range(12)
    ]
    assert volume == Decimal("48000")


def test_restart_partial_position_repair_keeps_original_due_time(tmp_path) -> None:
    """未到期轮次按实仓差修复后不得重置计时、延长持仓周期。"""
    opening_executor = _ScriptedExecutor()
    _, strategy, lighter, extended = _build_strategy(
        tmp_path,
        executor=opening_executor,
    )
    asyncio.run(strategy.run_once(now=0.0))
    lighter.position = Decimal("8")

    repair_executor = _ScriptedExecutor()
    _, restarted, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=repair_executor,
    )
    result = asyncio.run(restarted.run_once(now=1000.0))

    assert result.action == "reconciled"
    assert repair_executor.calls[0]["target_delta"] == Decimal("12.000")
    assert restarted.state.due_at == 7200.0
    assert result.net_exposure == 0


def test_post_order_position_read_failure_is_retried_and_converged(tmp_path) -> None:
    """下单后暂时读不到实仓时进程不得退出，下一节拍须按恢复的实仓回滚。"""

    class FlakyLighter(_LighterAdapter):
        def __init__(self):
            super().__init__("lighter")
            self.read_count = 0

        async def get_position(self, market: str):
            self.read_count += 1
            if self.read_count == 3:
                raise RuntimeError("下单后持仓暂不可读")
            return await super().get_position(market)

    lighter = FlakyLighter()
    extended = _ExtendedAdapter("extended")
    executor = _ScriptedExecutor(
        {
            "lighter": [{}, {}],
            "extended": [{"fraction": "0", "error": "对冲失败"}],
        }
    )
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        lighter=lighter,
        extended=extended,
        executor=executor,
    )

    uncertain = asyncio.run(strategy.run_once(now=0.0))
    assert uncertain.action == "execution_uncertain"
    assert uncertain.net_exposure is None

    converged = asyncio.run(strategy.run_once(now=1.0))
    assert converged.action == "reconciled"
    assert lighter.position == 0
    assert extended.position == 0
    assert converged.net_exposure == 0


def test_default_interlock_blocks_open_when_extended_market_is_unavailable(
    tmp_path,
) -> None:
    """Extended 仅持仓可读但交易行情不可用时仍应失败关闭。"""

    class UnavailableExtended(_ExtendedAdapter):
        async def get_market_price(self, market_name: str):
            raise RuntimeError("Extended 行情不可用")

    executor = _ScriptedExecutor()
    _, strategy, _, _ = _build_strategy(
        tmp_path,
        extended=UnavailableExtended("extended"),
        executor=executor,
    )

    result = asyncio.run(strategy.run_once(now=0.0))

    assert result.action == "interlocked"
    assert result.hedge_available is False
    assert "行情不可用" in result.interlock_reason
    assert executor.calls == []


def _closed_result():
    """构造一个 action=closed 的最小结果对象。"""
    from timed_volume.strategy import TimedVolumeResult

    return TimedVolumeResult(
        action="closed",
        round_index=1,
        direction=None,
        due_at=None,
        primary_size=Decimal("0"),
        hedge_size=Decimal("0"),
        net_exposure=Decimal("0"),
        hedge_available=True,
        interlock_reason="",
        warnings=[],
        notional_usd=2000,
    )


def _ledger_context():
    """构造一个字段齐全的台账上下文。"""
    from timed_volume.strategy import _RoundLedgerContext, RoundDirection

    return _RoundLedgerContext(
        round_index=1,
        direction=RoundDirection.LONG,
        notional_usd=2000,
        opened_at=0.0,
        primary_entry=Decimal("100"),
        hedge_entry=Decimal("100"),
        primary_size=Decimal("20"),
        hedge_size=Decimal("-20"),
    )


def test_ledger_retries_when_fills_not_yet_indexed(tmp_path, monkeypatch) -> None:
    """成交查询有索引延迟时应重试，而不是丢掉整条台账。

    实测：Hyperliquid 在平仓刚完成时查 userFillsByTime 可能查不到成交，
    日志为「平仓成交尚未完整出现在时间窗内」。不重试的话台账随机缺条，
    就失去了作为成本量尺的意义——而它正是用来验证各项优化的。
    """
    from timed_volume import strategy as strategy_module

    monkeypatch.setattr(strategy_module, "LEDGER_FILL_RETRY_DELAY_SECONDS", 0.0)

    lighter = _LighterAdapter("lighter")
    extended = _ExtendedAdapter("extended")
    _, strategy, _, _ = _build_strategy(
        tmp_path, lighter=lighter, extended=extended, executor=_ScriptedExecutor()
    )
    strategy.config.ledger_path = tmp_path / "ledger.jsonl"

    attempts: list[int] = []

    async def flaky(result, context, *, now):
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("Hyperliquid 平仓成交尚未完整出现在时间窗内")

    monkeypatch.setattr(
        strategy, "_build_and_write_round_ledger", flaky, raising=True
    )

    asyncio.run(
        strategy._record_round_ledger(
            _closed_result(), _ledger_context(), now=0.0
        )
    )

    assert len(attempts) == 3, "应重试到成功，而不是首次失败就放弃"


def test_ledger_does_not_retry_unrecoverable_errors(tmp_path, monkeypatch) -> None:
    """非时序类错误重试也没用，应立即抛出而不是空转。"""
    from timed_volume import strategy as strategy_module

    monkeypatch.setattr(strategy_module, "LEDGER_FILL_RETRY_DELAY_SECONDS", 0.0)

    lighter = _LighterAdapter("lighter")
    extended = _ExtendedAdapter("extended")
    _, strategy, _, _ = _build_strategy(
        tmp_path, lighter=lighter, extended=extended, executor=_ScriptedExecutor()
    )
    strategy.config.ledger_path = tmp_path / "ledger.jsonl"

    attempts: list[int] = []

    async def broken(result, context, *, now):
        attempts.append(1)
        raise ValueError("轮次台账缺少开仓时间或数量")

    monkeypatch.setattr(
        strategy, "_build_and_write_round_ledger", broken, raising=True
    )

    with pytest.raises(ValueError, match="缺少开仓时间"):
        asyncio.run(
            strategy._record_round_ledger(
                _closed_result(), _ledger_context(), now=0.0
            )
        )

    assert len(attempts) == 1, "不可自愈的错误不该重试"
