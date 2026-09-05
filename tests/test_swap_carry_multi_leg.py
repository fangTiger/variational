"""Swap carry 多腿结构测试；全部离线，未配置调用默认抛错。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import Position, Side


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _metadata(*, xaus_status: str = "open") -> dict[str, object]:
    """构造三种合约的离线元数据。"""
    return {
        "XAUS": [
            {
                "asset": "XAUS",
                "asset_class": "commodity",
                "instrument_type": "swap",
                "funding_interval_s": 0,
                "isolated_only": True,
                "market_status": xaus_status,
                "price": "4000",
                "trading_schedule": {
                    "next_open_at": "2026-09-08T22:00:00Z",
                    "next_close_at": "2026-09-08T20:00:00Z",
                },
                "trading_sessions": [
                    {
                        "open": "2026-09-08T00:00:00Z",
                        "close": "2026-09-08T20:00:00Z",
                    },
                    {
                        "open": "2026-09-08T22:00:00Z",
                        "close": "2026-09-09T20:00:00Z",
                    },
                ],
            }
        ],
        "XAU": [
            {
                "asset": "XAU",
                "asset_class": "commodity",
                "instrument_type": "perpetual_rwa_future",
                "funding_interval_s": 3600,
                "isolated_only": False,
                "price": "4000",
            }
        ],
        "XAUT": [
            {
                "asset": "XAUT",
                "asset_class": "crypto",
                "instrument_type": "perpetual_future",
                "funding_interval_s": 3600,
                "isolated_only": False,
                "price": "4000",
            }
        ],
    }


def _margin_requirements(
    *,
    qty: Decimal,
    isolated: bool,
) -> dict[str, object]:
    """按真实 indicative quote schema 构造保证金字段。"""
    bid_initial = qty * Decimal("3999") * Decimal("0.05")
    ask_initial = qty * Decimal("4001") * Decimal("0.05")
    requirements: dict[str, object] = {
        "existing_margin": {
            "initial_margin": "89.252347",
            "maintenance_margin": "44.626173",
        },
        "bid_margin_delta": {
            "initial_margin": str(bid_initial),
            "maintenance_margin": str(bid_initial / Decimal("2")),
        },
        "ask_margin_delta": {
            "initial_margin": str(ask_initial),
            "maintenance_margin": str(ask_initial / Decimal("2")),
        },
        "bid_max_notional_delta": "1000000",
        "ask_max_notional_delta": "1000000",
        "estimated_fees_bid": "0",
        "estimated_fees_ask": "0",
        "estimated_liquidation_price_bid": "3500",
        "estimated_liquidation_price_ask": "4500",
    }
    if isolated:
        requirements["margin_mode"] = "isolated"
    return requirements


class StrictMultiClient:
    """只执行显式配置的调用，并记录每一笔离线成交。"""

    def __init__(
        self,
        *,
        positions: dict[str, Decimal],
        metadata: object | None = None,
        accept_script: list[object] | None = None,
        rates: dict[str, Decimal] | None = None,
        liquidation_infos: dict[str, object] | None = None,
        equity: Decimal | None = None,
        transfers: list[dict[str, object]] | None = None,
    ) -> None:
        self.sizes = dict(positions)
        self.metadata = metadata
        self.accept_script = (
            list(accept_script) if accept_script is not None else None
        )
        self.rates = rates
        self.liquidation_infos = liquidation_infos
        self.equity = equity
        self.transfers = transfers
        self.position_calls: list[str] = []
        self.metadata_calls = 0
        self.quote_calls: list[
            tuple[str, str, Decimal, str, int, str | None]
        ] = []
        self.accept_calls: list[tuple[str, str, bool]] = []
        self.liquidation_calls: list[tuple[str, bool]] = []
        self.funding_calls: list[str] = []
        self._quotes: dict[str, tuple[str, str, Decimal]] = {}
        self._quote_index = 0
        self._max_slippage = 0.01

    async def get_position(self, underlying: str, *, exact: bool = False) -> Position:
        assert exact is True
        self.position_calls.append(underlying)
        if underlying not in self.sizes:
            raise AssertionError(f"未配置仓位：{underlying}")
        return Position(underlying, self.sizes[underlying])

    async def get_positions(self) -> list[dict[str, object]]:
        """按真实 `/positions` schema 返回账户持仓。"""
        records: list[dict[str, object]] = []
        for underlying, qty in self.sizes.items():
            if qty == 0:
                continue
            metadata = _metadata().get(underlying)
            assert isinstance(metadata, list) and metadata
            record = metadata[0]
            assert isinstance(record, dict)
            instrument = {
                "underlying": underlying,
                "instrument_type": record["instrument_type"],
                "funding_interval_s": record["funding_interval_s"],
                "settlement_asset": "USDC",
            }
            if record["instrument_type"] in {"swap", "perpetual_rwa_future"}:
                instrument["kind"] = record["asset_class"]
            records.append(
                {
                    "position_info": {
                        "instrument": instrument,
                        "qty": str(qty),
                        "avg_entry_price": "4000",
                    },
                    "price_info": {"underlying_price": "4000"},
                    "upnl": "0",
                }
            )
        return records

    async def get_supported_assets(self) -> object:
        self.metadata_calls += 1
        if self.metadata is None:
            raise AssertionError("未配置调用：get_supported_assets")
        return self.metadata

    async def request_quote(
        self,
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str,
        funding_interval_s: int,
        kind: str | None,
    ) -> dict[str, object]:
        self.quote_calls.append(
            (underlying, side, qty, instrument_type, funding_interval_s, kind)
        )
        self._quote_index += 1
        quote_id = f"q-{self._quote_index}"
        self._quotes[quote_id] = (underlying, side, qty)
        return {
            "quote_id": quote_id,
            "bid": "3999",
            "ask": "4001",
            "qty_limits": {
                "bid": {"min_qty": "0.001", "min_qty_tick": "0.001"},
                "ask": {"min_qty": "0.001", "min_qty_tick": "0.001"},
            },
            "margin_requirements": _margin_requirements(
                qty=qty,
                isolated=underlying == "XAUS",
            ),
            "margin_params": {
                "params": {
                    "asset_params": {
                        underlying: {
                            "futures_maintenance_margin": {
                                "XAUS": "0.05",
                                "XAU": "0.025",
                                "XAUT": "0.0142855",
                            }[underlying]
                        }
                    },
                    "default_asset_param": {
                        "futures_maintenance_margin": "0.1"
                    },
                    "use_default_asset_param": False,
                }
            },
        }

    async def get_balance(self) -> object:
        if self.equity is None:
            raise AssertionError("未配置调用：get_balance")
        return SimpleNamespace(equity=self.equity)

    async def accept_quote(
        self,
        *,
        quote_id: str,
        side: str,
        max_slippage: float,
        is_reduce_only: bool,
    ) -> dict[str, str]:
        del max_slippage
        if self.accept_script is None:
            raise AssertionError("未配置调用：accept_quote")
        self.accept_calls.append((quote_id, side, is_reduce_only))
        if not self.accept_script:
            raise AssertionError("accept_quote 调用次数超过编排")
        outcome = self.accept_script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        underlying, quoted_side, qty = self._quotes[quote_id]
        assert quoted_side == side
        current = self.sizes[underlying]
        delta = qty if side == "buy" else -qty
        if is_reduce_only:
            if current > 0 and delta < 0:
                self.sizes[underlying] = max(Decimal("0"), current + delta)
            elif current < 0 and delta > 0:
                self.sizes[underlying] = min(Decimal("0"), current + delta)
        else:
            self.sizes[underlying] = current + delta
        return {"rfq_id": f"rfq-{len(self.accept_calls)}"}

    async def get_swap_funding(self, underlying: str) -> object:
        self.funding_calls.append(underlying)
        if self.rates is None or underlying not in self.rates:
            raise AssertionError(f"未配置调用：get_swap_funding({underlying})")
        return SimpleNamespace(
            upcoming=SimpleNamespace(
                long_rate=SimpleNamespace(
                    normalized_annual_rate=-self.rates[underlying]
                )
            )
        )

    async def get_funding_rate(
        self, underlying: str, instrument_type: str
    ) -> Decimal:
        del instrument_type
        self.funding_calls.append(underlying)
        if self.rates is None or underlying not in self.rates:
            raise AssertionError(f"未配置调用：get_funding_rate({underlying})")
        return self.rates[underlying]

    async def get_liquidation_info(
        self, underlying: str, *, exact: bool = False
    ) -> object:
        self.liquidation_calls.append((underlying, exact))
        if self.liquidation_infos is None or underlying not in self.liquidation_infos:
            raise AssertionError(
                f"未配置调用：get_liquidation_info({underlying}, {exact})"
            )
        result = self.liquidation_infos[underlying]
        if isinstance(result, BaseException):
            raise result
        return result

    async def raw(self, path: str) -> object:
        if self.transfers is None:
            raise AssertionError(f"未配置调用：raw({path})")
        assert path == "/transfers?limit=100&offset=0"
        return {
            "result": self.transfers,
            "pagination": {"object_count": len(self.transfers)},
        }


def _accepted_markets(client: StrictMultiClient) -> list[tuple[str, str, bool]]:
    """把报价编号转换为标的、方向与 reduce_only。"""
    return [
        (client._quotes[quote_id][0], side, reduce_only)
        for quote_id, side, reduce_only in client.accept_calls
    ]


def _guard_paths(tmp_path: Path) -> dict[str, Path]:
    """为守护测试隔离所有持久化文件。"""
    return {
        "kill_switch_path": tmp_path / "kill",
        "heartbeat_path": tmp_path / "heartbeat.json",
        "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl",
    }


def test_non_delta_neutral_structure_is_rejected_at_definition() -> None:
    """多空权重不相等时，结构定义本身就必须失败。"""
    from tools import hedge_swap_carry as carry

    with pytest.raises(ValueError, match="delta 中性"):
        carry.CarryStructure(
            name="BROKEN",
            legs=(
                carry.CarryLeg(
                    "XAU",
                    Side.BUY,
                    "perpetual_rwa_future",
                    3600,
                    "commodity",
                    Decimal("1"),
                ),
                carry.CarryLeg(
                    "XAUT",
                    Side.SELL,
                    "perpetual_future",
                    3600,
                    None,
                    Decimal("2"),
                ),
            ),
        )


def test_named_structures_and_cli_defaults() -> None:
    """三个具名结构必须固定方向、权重与默认选择。"""
    from tools import hedge_swap_carry as carry
    from tools import run_swap_carry_guard as guard

    assert [leg.underlying for leg in carry.XAUS_XAU.legs] == ["XAUS", "XAU"]
    assert [leg.underlying for leg in carry.XAU_XAUT.legs] == ["XAU", "XAUT"]
    assert [leg.weight for leg in carry.TRIPLE.legs] == [
        Decimal("1"),
        Decimal("1"),
        Decimal("2"),
    ]
    assert carry.build_parser().parse_args(["open"]).structure == "XAU_XAUT"
    assert carry.build_parser().parse_args(["status"]).structure == "XAU_XAUT"
    assert carry.build_parser().parse_args(["close"]).structure == "XAU_XAUT"
    assert guard.build_parser().parse_args(["--once"]).structure == "XAU_XAUT"


def test_xau_xaut_open_is_24_7_and_omits_xaut_kind() -> None:
    """无 XAUS 时不得读取时段，XAUT 报价必须传 kind=None。"""
    from tools import hedge_swap_carry as carry

    client = StrictMultiClient(
        positions={"XAU": Decimal("0"), "XAUT": Decimal("0")},
        metadata=None,
        equity=Decimal("1000"),
        accept_script=[{}, {}],
    )

    asyncio.run(
        carry.cmd_open(
            client,
            Decimal("50"),
            structure=carry.XAU_XAUT,
            yes=True,
            now=NOW,
        )
    )

    assert client.metadata_calls == 0
    assert _accepted_markets(client) == [
        ("XAU", "buy", False),
        ("XAUT", "sell", False),
    ]
    assert all(call[5] is None for call in client.quote_calls if call[0] == "XAUT")
    assert client.sizes["XAU"] == -client.sizes["XAUT"]


def test_triple_opens_in_one_one_two_ratio_with_zero_delta() -> None:
    """TRIPLE 必须按 XAUS、XAU、XAUT 的 1:1:2 顺序配平。"""
    from tools import hedge_swap_carry as carry

    client = StrictMultiClient(
        positions={
            "XAUS": Decimal("0"),
            "XAU": Decimal("0"),
            "XAUT": Decimal("0"),
        },
        metadata=_metadata(),
        equity=Decimal("1000"),
        accept_script=[{}, {}, {}],
    )

    asyncio.run(
        carry.cmd_open(
            client,
            Decimal("50"),
            structure=carry.TRIPLE,
            yes=True,
            now=NOW,
        )
    )

    assert _accepted_markets(client) == [
        ("XAUS", "buy", False),
        ("XAU", "buy", False),
        ("XAUT", "sell", False),
    ]
    assert client.sizes["XAUS"] == client.sizes["XAU"]
    assert -client.sizes["XAUT"] == client.sizes["XAUS"] * 2
    assert sum(client.sizes.values(), Decimal("0")) == 0


def test_second_leg_failure_rolls_back_first_leg() -> None:
    """第二腿失败时必须 reduce_only 回滚已开的第一腿。"""
    from tools import hedge_swap_carry as carry

    client = StrictMultiClient(
        positions={"XAU": Decimal("0"), "XAUT": Decimal("0")},
        equity=Decimal("1000"),
        accept_script=[{}, RuntimeError("第二腿失败"), {}],
    )

    with pytest.raises(SystemExit, match="已回滚"):
        asyncio.run(
            carry.cmd_open(
                client,
                Decimal("50"),
                structure=carry.XAU_XAUT,
                yes=True,
                now=NOW,
            )
        )

    assert _accepted_markets(client) == [
        ("XAU", "buy", False),
        ("XAUT", "sell", False),
        ("XAU", "sell", True),
    ]


def test_third_leg_failure_rolls_back_first_two_legs() -> None:
    """第三腿失败时必须逐一回滚前两条已开腿。"""
    from tools import hedge_swap_carry as carry

    client = StrictMultiClient(
        positions={
            "XAUS": Decimal("0"),
            "XAU": Decimal("0"),
            "XAUT": Decimal("0"),
        },
        metadata=_metadata(),
        equity=Decimal("1000"),
        accept_script=[{}, {}, RuntimeError("第三腿失败"), {}, {}],
    )

    with pytest.raises(SystemExit, match="已回滚"):
        asyncio.run(
            carry.cmd_open(
                client,
                Decimal("50"),
                structure=carry.TRIPLE,
                yes=True,
                now=NOW,
            )
        )

    assert _accepted_markets(client) == [
        ("XAUS", "buy", False),
        ("XAU", "buy", False),
        ("XAUT", "sell", False),
        ("XAUS", "sell", True),
        ("XAU", "sell", True),
    ]
    assert all(size == 0 for size in client.sizes.values())


def test_weighted_net_carry_uses_all_legs() -> None:
    """净 carry 按方向和权重求和，再按单侧总权重归一化。"""
    from tools import hedge_swap_carry as carry

    result = carry._weighted_net_carry(
        carry.TRIPLE,
        {
            "XAUS": Decimal("0.0572"),
            "XAU": Decimal("0"),
            "XAUT": Decimal("0.1095"),
        },
    )

    assert result == Decimal("0.0809")


def test_guard_xau_xaut_can_open_when_gold_funding_session_is_open(
    tmp_path: Path,
) -> None:
    """黄金现货开市且费率正常时，XAU_XAUT 自动开仓应继续执行。"""
    from tools import hedge_swap_carry as carry
    from tools import run_swap_carry_guard as guard

    client = StrictMultiClient(
        positions={"XAU": Decimal("0"), "XAUT": Decimal("0")},
        metadata=_metadata(),
        rates={"XAU": Decimal("0.03"), "XAUT": Decimal("0.1095")},
        equity=Decimal("1000"),
        accept_script=[{}, {}],
    )

    result = asyncio.run(
        guard.run_once(
            client,
            structure=carry.XAU_XAUT,
            now=NOW,
            **_guard_paths(tmp_path),
        )
    )

    assert result == 0
    assert client.metadata_calls == 1
    assert _accepted_markets(client) == [
        ("XAU", "buy", False),
        ("XAUT", "sell", False),
    ]


def test_guard_xau_xaut_rejects_zero_rate_entry_while_gold_market_is_closed(
    tmp_path: Path,
) -> None:
    """休市清零费率是伪像，必须按时段元数据拒绝开仓并写明不可用。"""
    from tools import hedge_swap_carry as carry
    from tools import run_swap_carry_guard as guard

    client = StrictMultiClient(
        positions={"XAU": Decimal("0"), "XAUT": Decimal("0")},
        metadata=_metadata(xaus_status="closed"),
        rates={"XAU": Decimal("0"), "XAUT": Decimal("0.1095")},
        equity=Decimal("1000"),
        accept_script=[],
    )

    result = asyncio.run(
        guard.run_once(
            client,
            structure=carry.XAU_XAUT,
            now=NOW,
            **_guard_paths(tmp_path),
        )
    )

    assert result == 0
    assert client.funding_calls == []
    assert client.accept_calls == []
    heartbeat = json.loads(
        _guard_paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "费率不可用" in heartbeat["auto_open_conclusion"]
    assert "费率不可用" in _guard_paths(tmp_path)["audit_path"].read_text(
        encoding="utf-8"
    )


def test_guard_xau_xaut_open_position_has_no_weekend_close_logic(
    tmp_path: Path,
) -> None:
    """无 XAUS 的现有持仓不得触发 pre-close 或周末平仓检查。"""
    from tools import hedge_swap_carry as carry
    from tools import run_swap_carry_guard as guard

    paths = _guard_paths(tmp_path)
    paths["state_path"].write_text(
        json.dumps({"exit_carry_consecutive_rounds": 2}),
        encoding="utf-8",
    )
    client = StrictMultiClient(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        metadata=_metadata(xaus_status="closed"),
        liquidation_infos={
            "XAU": (Decimal("4000"), Decimal("3800")),
            "XAUT": (Decimal("4000"), Decimal("4200")),
        },
        equity=Decimal("1000"),
    )

    result = asyncio.run(
        guard.run_once(
            client,
            structure=carry.XAU_XAUT,
            auto_open=False,
            now=NOW + timedelta(days=4),
            **paths,
        )
    )

    assert result == 0
    assert client.metadata_calls == 1
    assert client.funding_calls == []
    assert client.accept_calls == []
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["exit_carry_consecutive_rounds"] == 2


def test_guard_records_cross_liquidation_without_using_it_to_exit(
    tmp_path: Path,
) -> None:
    """全仓腿可记录 per-leg 强平价，但退出只由账户级保证金率决定。"""
    from tools import hedge_swap_carry as carry
    from tools import run_swap_carry_guard as guard

    client = StrictMultiClient(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        metadata=_metadata(),
        liquidation_infos={
            "XAU": (Decimal("4000"), Decimal("3800")),
            "XAUT": (Decimal("4000"), Decimal("4050")),
        },
        equity=Decimal("1000"),
    )

    result = asyncio.run(
        guard.run_once(
            client,
            structure=carry.XAU_XAUT,
            auto_open=False,
            now=NOW,
            **_guard_paths(tmp_path),
        )
    )

    assert result == 0
    assert client.liquidation_calls == [("XAU", True), ("XAUT", True)]
    assert client.accept_calls == []


def test_panel_xau_xaut_shows_structure_without_xaus_schedule(tmp_path: Path) -> None:
    """24/7 结构的面板显示全部腿，但不显示 XAUS 休市行。"""
    from panel.providers import swap_carry
    from tools import hedge_swap_carry as carry

    heartbeat = tmp_path / "heartbeat.json"
    state = tmp_path / "state.json"
    heartbeat.write_text(
        json.dumps({"timestamp": NOW.isoformat(), "structure": "XAU_XAUT"}),
        encoding="utf-8",
    )
    client = StrictMultiClient(
        positions={"XAU": Decimal("0"), "XAUT": Decimal("0")},
        metadata=_metadata(xaus_status="closed"),
        rates={"XAU": Decimal("0"), "XAUT": Decimal("0.1095")},
        transfers=[],
    )

    status = swap_carry.collect(
        client=client,
        structure=carry.XAU_XAUT,
        heartbeat_path=heartbeat,
        state_path=state,
        now=NOW,
    )
    labels = {metric.label for metric in status.metrics}

    assert status.error is None
    assert "当前结构" in labels
    assert "XAU 多腿" in labels
    assert "XAUT 空腿" in labels
    assert "XAUS 时段" not in labels
