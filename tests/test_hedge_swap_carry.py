"""Swap carry 人工值守工具测试；全部离线，未编排调用默认抛错。"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.base import Position, Side
from adapters.variational_client import VariationalJurisdictionError


OPEN_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
NEAR_CLOSE_NOW = datetime(2026, 9, 7, 18, 10, tzinfo=timezone.utc)


def _metadata(*, market_status: str = "open") -> dict[str, object]:
    """返回包含当前及后续会话的严格 XAUS 元数据。"""
    return {
        "XAUS": [
            {
                "asset": "XAUS",
                "asset_class": "commodity",
                "instrument_type": "swap",
                "funding_interval_s": 0,
                "market_status": market_status,
                "price": "4000",
                "trading_schedule": {
                    "next_open_at": "2026-09-07T22:00:00Z",
                    "next_close_at": "2026-09-07T18:30:00Z",
                },
                "trading_sessions": [
                    {
                        "open": "2026-09-07T00:00:00Z",
                        "close": "2026-09-07T18:30:00Z",
                    },
                    {
                        "open": "2026-09-07T22:00:00Z",
                        "close": "2026-09-08T18:30:00Z",
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
                "price": "4000",
            }
        ],
    }


def _margin_requirements(
    *,
    bid_initial: Decimal,
    ask_initial: Decimal,
    margin_mode: str | None = None,
) -> dict[str, object]:
    """按真实 indicative quote schema 构造保证金字段。"""
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
    if margin_mode is not None:
        requirements["margin_mode"] = margin_mode
    return requirements


class StrictFakeVariational:
    """仅执行显式配置的调用；任何漏配都立即失败。"""

    def __init__(
        self,
        *,
        metadata: object | None = None,
        positions: dict[str, Decimal] | None = None,
        quote_enabled: bool = False,
        equity: Decimal | None = None,
        accept_script: list[object] | None = None,
        hidden_xaus_reads: int = 0,
        include_quote_margin: bool = True,
    ) -> None:
        self.metadata = metadata
        self.sizes = positions or {"XAUS": Decimal("0"), "XAU": Decimal("0")}
        self.quote_enabled = quote_enabled
        self.equity = equity
        self.accept_script = list(accept_script) if accept_script is not None else None
        self.hidden_xaus_reads = hidden_xaus_reads
        self.include_quote_margin = include_quote_margin
        self.position_calls: list[tuple[str, bool]] = []
        self.quote_calls: list[tuple[str, str, Decimal, str, int, str | None]] = []
        self.accept_calls: list[tuple[str, str, bool]] = []
        self._quotes: dict[str, tuple[str, str, Decimal]] = {}
        self._quote_index = 0
        self._first_leg_written = False
        self._hidden_reads_done = 0
        self._max_slippage = 0.01

    async def get_supported_assets(self) -> object:
        if self.metadata is None:
            raise AssertionError("未配置调用：get_supported_assets")
        return self.metadata

    async def get_position(self, underlying: str, *, exact: bool = False) -> Position:
        self.position_calls.append((underlying, exact))
        assert exact is True
        if (
            underlying == "XAUS"
            and self._first_leg_written
            and self._hidden_reads_done < self.hidden_xaus_reads
        ):
            self._hidden_reads_done += 1
            return Position(underlying, Decimal("0"))
        if underlying not in self.sizes:
            raise AssertionError(f"未配置仓位：{underlying}")
        return Position(underlying, self.sizes[underlying])

    async def request_quote(
        self,
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int | None = None,
        kind: str | None = None,
    ) -> dict[str, object]:
        if not self.quote_enabled:
            raise AssertionError("未配置调用：request_quote")
        self.quote_calls.append(
            (underlying, side, qty, instrument_type, int(funding_interval_s or 0), kind)
        )
        self._quote_index += 1
        quote_id = f"q-{self._quote_index}-{underlying}-{side}"
        self._quotes[quote_id] = (underlying, side, qty)
        result: dict[str, object] = {
            "quote_id": quote_id,
            "bid": "3999",
            "ask": "4001",
            "mark_price": "4000",
            "qty_limits": {
                "bid": {
                    "min_qty": "0.00003",
                    "min_qty_tick": "0.00001",
                },
                "ask": {
                    "min_qty": "0.00003",
                    "min_qty_tick": "0.00001",
                },
            },
        }
        if self.include_quote_margin:
            result["margin_requirements"] = _margin_requirements(
                bid_initial=qty * Decimal("3999") * Decimal("0.05"),
                ask_initial=qty * Decimal("4001") * Decimal("0.05"),
                margin_mode="isolated" if underlying == "XAUS" else None,
            )
        return result

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
        assert side == quoted_side
        current = self.sizes[underlying]
        delta = qty if side == "buy" else -qty
        if is_reduce_only:
            if current > 0 and delta < 0:
                self.sizes[underlying] = max(Decimal("0"), current + delta)
            elif current < 0 and delta > 0:
                self.sizes[underlying] = min(Decimal("0"), current + delta)
        else:
            self.sizes[underlying] = current + delta
        if underlying == "XAUS" and side == "buy" and not is_reduce_only:
            self._first_leg_written = True
        return {"rfq_id": f"rfq-{len(self.accept_calls)}"}

    async def get_liquidation_info(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("未配置调用：get_liquidation_info")

    async def get_swap_funding(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("未配置调用：get_swap_funding")

    async def get_funding_rate(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("未配置调用：get_funding_rate")

    async def raw(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("未配置调用：raw")


def _open_client(
    *,
    metadata: object | None = None,
    accept_script: list[object] | None = None,
    hidden_xaus_reads: int = 0,
) -> StrictFakeVariational:
    """构造一套完整的正常开仓前置数据。"""
    return StrictFakeVariational(
        metadata=metadata if metadata is not None else _metadata(),
        positions={"XAUS": Decimal("0"), "XAU": Decimal("0")},
        quote_enabled=True,
        equity=Decimal("1000"),
        accept_script=accept_script,
        hidden_xaus_reads=hidden_xaus_reads,
    )


def _accepted_markets(client: StrictFakeVariational) -> list[tuple[str, str, bool]]:
    """把 accept 调用还原为稳定的标的、方向、reduce_only 三元组。"""
    return [
        (client._quotes[quote_id][0], side, reduce_only)
        for quote_id, side, reduce_only in client.accept_calls
    ]


def test_all_structure_parameter_defaults_use_default_structure() -> None:
    """所有可省略 structure 的 Python 调用入口必须与 CLI 默认结构一致。"""
    from tools import hedge_swap_carry

    functions = {
        name: value
        for name, value in vars(hedge_swap_carry).items()
        if inspect.isfunction(value)
        and "structure" in inspect.signature(value).parameters
        and inspect.signature(value).parameters["structure"].default
        is not inspect.Parameter.empty
    }

    assert functions
    assert {
        name: inspect.signature(value).parameters["structure"].default
        for name, value in functions.items()
    } == {name: hedge_swap_carry.DEFAULT_STRUCTURE for name in functions}


def test_open_rejects_notional_over_hard_cap_without_any_order() -> None:
    """超过硬上限必须在任何行情或下单调用前拒绝。"""
    from tools import hedge_swap_carry

    client = StrictFakeVariational()
    with pytest.raises(SystemExit, match="超过.*上限"):
        asyncio.run(
            hedge_swap_carry.cmd_open(
                client,
                hedge_swap_carry.MAX_NOTIONAL_USD + Decimal("0.01"),
                yes=True,
                now=OPEN_NOW,
            )
        )

    assert client.quote_calls == []
    assert client.accept_calls == []


def test_open_rejects_active_kill_switch_before_any_network_call(
    monkeypatch, tmp_path
) -> None:
    """kill switch 激活后人工 open 也必须停用，不能等待下一轮再平。"""
    from tools import hedge_swap_carry

    kill_switch = tmp_path / "swap_carry.kill"
    kill_switch.write_text("停止\n", encoding="utf-8")
    monkeypatch.setattr(hedge_swap_carry, "SWAP_CARRY_KILL_SWITCH", kill_switch)
    client = StrictFakeVariational()

    with pytest.raises(SystemExit, match="kill switch"):
        asyncio.run(
            hedge_swap_carry.cmd_open(
                client,
                Decimal("50"),
                structure=hedge_swap_carry.XAUS_XAU,
                yes=True,
                now=OPEN_NOW,
            )
        )

    assert client.position_calls == []
    assert client.quote_calls == []
    assert client.accept_calls == []


def test_open_rejects_when_xaus_market_is_closed() -> None:
    """XAUS 休市时不得询价或下单。"""
    from tools import hedge_swap_carry

    client = _open_client(metadata=_metadata(market_status="closed"), accept_script=[])
    with pytest.raises(SystemExit, match="不可交易"):
        asyncio.run(
            hedge_swap_carry.cmd_open(
                client,
                Decimal("50"),
                structure=hedge_swap_carry.XAUS_XAU,
                yes=True,
                now=OPEN_NOW,
            )
        )

    assert client.quote_calls == []
    assert client.accept_calls == []


def test_open_rejects_with_less_than_thirty_minutes_to_close() -> None:
    """距休市不足 30 分钟时不得制造可能跨休市的裸腿。"""
    from tools import hedge_swap_carry

    client = _open_client(accept_script=[])
    with pytest.raises(SystemExit, match="不足 30 分钟"):
        asyncio.run(
            hedge_swap_carry.cmd_open(
                client,
                Decimal("50"),
                structure=hedge_swap_carry.XAUS_XAU,
                yes=True,
                now=NEAR_CLOSE_NOW,
            )
        )

    assert client.quote_calls == []
    assert client.accept_calls == []


def test_second_leg_rejection_rolls_back_first_leg_reduce_only(monkeypatch) -> None:
    """第二腿明确拒绝后必须立即用 reduce_only 回滚 XAUS。"""
    from tools import hedge_swap_carry

    monkeypatch.setattr(hedge_swap_carry, "_POLL_DELAY_S", 0)
    client = _open_client(
        accept_script=[{"ok": True}, RuntimeError("第二腿拒绝"), {"ok": True}]
    )

    with pytest.raises(SystemExit, match="已回滚"):
        asyncio.run(
            hedge_swap_carry.cmd_open(
                client,
                Decimal("50"),
                structure=hedge_swap_carry.XAUS_XAU,
                yes=True,
                now=OPEN_NOW,
            )
        )

    assert _accepted_markets(client) == [
        ("XAUS", "buy", False),
        ("XAU", "sell", False),
        ("XAUS", "sell", True),
    ]
    assert client.sizes == {"XAUS": Decimal("0"), "XAU": Decimal("0")}


def test_second_leg_and_rollback_failure_is_loud_nonzero(monkeypatch, capsys) -> None:
    """第二腿与回滚同时失败必须非零退出并打印最高醒目告警。"""
    from tools import hedge_swap_carry

    monkeypatch.setattr(hedge_swap_carry, "_POLL_DELAY_S", 0)
    client = _open_client(
        accept_script=[{"ok": True}, RuntimeError("第二腿拒绝"), RuntimeError("回滚拒绝")]
    )

    with pytest.raises(SystemExit) as exc:
        asyncio.run(
            hedge_swap_carry.cmd_open(
                client,
                Decimal("50"),
                structure=hedge_swap_carry.XAUS_XAU,
                yes=True,
                now=OPEN_NOW,
            )
        )

    output = capsys.readouterr().out + str(exc.value)
    assert exc.value.code != 0
    assert "回滚失败" in output
    assert "立即人工处理" in output
    assert "🚨" in output
    assert len(client.accept_calls) == 3


def test_position_delay_is_polled_then_second_leg_opens(monkeypatch) -> None:
    """第一腿成交后即时仓位为零时要轮询，不能误判并留下裸仓。"""
    from tools import hedge_swap_carry

    monkeypatch.setattr(hedge_swap_carry, "_POLL_DELAY_S", 0)
    client = _open_client(
        accept_script=[{"ok": True}, {"ok": True}],
        hidden_xaus_reads=2,
    )

    asyncio.run(
        hedge_swap_carry.cmd_open(
            client,
            Decimal("50"),
            structure=hedge_swap_carry.XAUS_XAU,
            yes=True,
            now=OPEN_NOW,
        )
    )

    assert _accepted_markets(client) == [
        ("XAUS", "buy", False),
        ("XAU", "sell", False),
    ]
    assert client._hidden_reads_done == 2
    assert client.sizes["XAUS"] == -client.sizes["XAU"]
    assert client.sizes["XAUS"] > 0


def test_jurisdiction_403_has_dedicated_message() -> None:
    """地区封锁必须和普通下单失败分流，并明确提示放行 IP。"""
    from tools import hedge_swap_carry

    client = _open_client(
        accept_script=[VariationalJurisdictionError("restricted jurisdiction")]
    )

    with pytest.raises(SystemExit) as exc:
        asyncio.run(
            hedge_swap_carry.cmd_open(
                client,
                Decimal("50"),
                structure=hedge_swap_carry.XAUS_XAU,
                yes=True,
                now=OPEN_NOW,
            )
        )

    assert "地区封锁" in str(exc.value)
    assert "需在放行 IP 上执行" in str(exc.value)
    assert len(client.accept_calls) == 1


def test_dry_run_completes_checks_and_quotes_without_accept() -> None:
    """dry-run 必须走完时段、仓位、保证金和双腿报价，但绝不 accept。"""
    from tools import hedge_swap_carry

    client = _open_client(accept_script=None)

    asyncio.run(
        hedge_swap_carry.cmd_open(
            client,
            Decimal("50"),
            structure=hedge_swap_carry.XAUS_XAU,
            yes=True,
            dry_run=True,
            now=OPEN_NOW,
        )
    )

    assert {call[0] for call in client.quote_calls} == {"XAUS", "XAU"}
    assert client.accept_calls == []


def test_status_warns_loudly_when_only_one_leg_remains(capsys) -> None:
    """状态发现单腿时必须醒目告警，即使其他只读数据源不可用。"""
    from tools import hedge_swap_carry

    client = StrictFakeVariational(
        positions={"XAUS": Decimal("0.0125"), "XAU": Decimal("0")}
    )

    asyncio.run(
        hedge_swap_carry.cmd_status(
            client,
            structure=hedge_swap_carry.XAUS_XAU,
            now=OPEN_NOW,
        )
    )

    output = capsys.readouterr().out
    assert "🚨" in output
    assert "缺腿裸仓告警" in output
    assert "XAUS" in output


def test_open_rejects_when_equity_cannot_cover_both_initial_margins() -> None:
    """两腿所需初始保证金高于可用权益时不得 accept。"""
    from tools import hedge_swap_carry

    client = _open_client(accept_script=[])
    client.equity = Decimal("1")

    with pytest.raises(SystemExit, match="保证金不足"):
        asyncio.run(
            hedge_swap_carry.cmd_open(
                client,
                Decimal("50"),
                structure=hedge_swap_carry.XAUS_XAU,
                yes=True,
                now=OPEN_NOW,
            )
        )

    assert client.accept_calls == []


def test_initial_margin_ratio_uses_real_directional_margin_delta_schema() -> None:
    """保证金率必须由真实方向金额除以本腿名义得到，不能回退默认比例。"""
    from tools import hedge_swap_carry

    requirements = _margin_requirements(
        bid_initial=Decimal("63.207623"),
        ask_initial=Decimal("110.956"),
    )
    payload = {"margin_requirements": requirements}

    assert "initial_margin" not in requirements
    buy_ratio = hedge_swap_carry._initial_margin_ratio(
        payload,
        Side.BUY,
        Decimal("0.5") * Decimal("4438.93"),
    )
    sell_ratio = hedge_swap_carry._initial_margin_ratio(
        payload,
        Side.SELL,
        Decimal("0.5") * Decimal("4423.35"),
    )

    assert f"{buy_ratio:.3%}" == "4.999%"
    assert f"{sell_ratio:.3%}" == "2.858%"
    assert buy_ratio != Decimal("0.05")
    assert sell_ratio != Decimal("0.05")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "margin_requirements"),
        (
            {"margin_requirements": {"initial_margin": "0.05"}},
            "ask_margin_delta",
        ),
        (
            {
                "margin_requirements": {
                    "ask_margin_delta": {"initial_margin": "非法值"}
                }
            },
            "不是有效十进制数",
        ),
        (
            {
                "margin_requirements": {
                    "ask_margin_delta": {"initial_margin": "101"}
                }
            },
            "不超过 1",
        ),
    ],
)
def test_initial_margin_ratio_rejects_missing_invalid_and_over_one(
    payload: dict[str, object],
    message: str,
) -> None:
    """开仓不得用旧顶层字段或默认值掩盖方向保证金字段异常。"""
    from tools import hedge_swap_carry

    with pytest.raises(ValueError, match=message):
        hedge_swap_carry._initial_margin_ratio(
            payload,
            Side.BUY,
            Decimal("100"),
        )


def test_leg_descriptors_are_fixed_to_xaus_long_then_xau_short() -> None:
    """合约类型、周期、kind 与方向不得被参数化或调换。"""
    from tools import hedge_swap_carry

    first, second = hedge_swap_carry._opening_plan(hedge_swap_carry.XAUS_XAU)
    assert (
        first.underlying,
        first.open_side,
        first.instrument_type,
        first.funding_interval_s,
        first.kind,
    ) == ("XAUS", Side.BUY, "swap", 0, "commodity")
    assert (
        second.underlying,
        second.open_side,
        second.instrument_type,
        second.funding_interval_s,
        second.kind,
    ) == ("XAU", Side.SELL, "perpetual_rwa_future", 3600, "commodity")


def test_close_does_not_require_opening_margin_fields(monkeypatch) -> None:
    """reduce-only 平仓报价缺初始保证金字段时仍必须优先完成平仓。"""
    from tools import hedge_swap_carry

    monkeypatch.setattr(hedge_swap_carry, "_POLL_DELAY_S", 0)
    client = StrictFakeVariational(
        metadata=_metadata(),
        positions={"XAUS": Decimal("0.0125"), "XAU": Decimal("-0.0125")},
        quote_enabled=True,
        accept_script=[{"ok": True}, {"ok": True}],
        include_quote_margin=False,
    )

    asyncio.run(
        hedge_swap_carry.cmd_close(
            client,
            structure=hedge_swap_carry.XAUS_XAU,
            yes=True,
            now=OPEN_NOW,
        )
    )

    assert _accepted_markets(client) == [
        ("XAUS", "sell", True),
        ("XAU", "buy", True),
    ]
    assert client.sizes == {"XAUS": Decimal("0"), "XAU": Decimal("0")}


def test_status_reports_liquidation_carry_schedule_and_actual_transfers(capsys) -> None:
    """状态快照必须含全部人工判断字段，结算额只取 /transfers。"""
    from tools import hedge_swap_carry

    class StatusClient(StrictFakeVariational):
        async def get_liquidation_info(
            self, underlying: str, *, exact: bool = False
        ) -> object:
            assert exact is True
            if underlying == "XAUS":
                return None
            if underlying == "XAU":
                return Decimal("4000"), Decimal("4500")
            raise AssertionError(f"未配置强平信息：{underlying}")

        async def get_swap_funding(self, underlying: str) -> object:
            assert underlying == "XAUS"
            return SimpleNamespace(
                upcoming=SimpleNamespace(
                    long_rate=SimpleNamespace(
                        normalized_annual_rate=Decimal("-0.05")
                    )
                )
            )

        async def get_funding_rate(
            self, underlying: str, instrument_type: str
        ) -> Decimal:
            assert (underlying, instrument_type) == (
                "XAU",
                "perpetual_rwa_future",
            )
            return Decimal("0.11")

        async def raw(self, path: str) -> object:
            assert path == "/transfers?limit=100&offset=0"
            rows = [
                {
                    "funding_rate": "-0.0001",
                    "qty": "-0.01",
                    "reference_instrument": {"underlying": "XAUS"},
                },
                {
                    "funding_rate": "0.0001",
                    "qty": "0.02",
                    "reference_instrument": {"underlying": "XAU"},
                },
                {
                    "funding_rate": None,
                    "qty": "999",
                    "reference_instrument": {"underlying": "XAU"},
                },
            ]
            return {
                "result": rows,
                "pagination": {"object_count": len(rows)},
            }

    client = StatusClient(
        metadata=_metadata(),
        positions={"XAUS": Decimal("0.0125"), "XAU": Decimal("-0.0125")},
    )

    asyncio.run(
        hedge_swap_carry.cmd_status(
            client,
            structure=hedge_swap_carry.XAUS_XAU,
            now=OPEN_NOW,
        )
    )

    output = capsys.readouterr().out
    assert "XAUS 数量=0.0125，权重=1，名义=$50.00" in output
    assert "XAU  数量=-0.0125，权重=1，名义=$50.00" in output
    assert "净 delta=0.0000" in output
    assert "XAUS 强平：无数据" in output
    assert "XAU 强平：强平价=4500" in output
    assert "净 carry=6.0000%" in output
    assert "XAUS 时段=可交易" in output
    assert "距下次休市=6小时30分钟" in output
    assert "XAUS=-0.01 USDC" in output
    assert "XAU=0.02 USDC" in output
    assert "合计=0.01 USDC" in output


def test_status_puts_incident_and_stale_guard_heartbeat_first(
    monkeypatch, tmp_path, capsys
) -> None:
    """守护事故与超过 15 分钟的心跳必须在普通状态之前醒目标红。"""
    from tools import hedge_swap_carry

    heartbeat_path = tmp_path / "heartbeat.json"
    state_path = tmp_path / "state.json"
    heartbeat_path.write_text(
        json.dumps(
            {
                "timestamp": (OPEN_NOW - timedelta(minutes=16)).isoformat(),
                "conclusion": "上一轮失败",
                "consecutive_failures": 2,
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps(
            {
                "status": "action_failed",
                "message": "地区封锁，无法平仓",
                "consecutive_failures": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        hedge_swap_carry, "SWAP_CARRY_GUARD_HEARTBEAT", heartbeat_path
    )
    monkeypatch.setattr(hedge_swap_carry, "SWAP_CARRY_GUARD_STATE", state_path)
    client = StrictFakeVariational(
        positions={"XAUS": Decimal("0"), "XAU": Decimal("0")}
    )

    asyncio.run(
        hedge_swap_carry.cmd_status(
            client,
            structure=hedge_swap_carry.XAUS_XAU,
            now=OPEN_NOW,
        )
    )

    output = capsys.readouterr().out
    assert output.index("地区封锁") < output.index("swap carry 状态")
    assert "守护进程上次运行于 16.0 分钟前" in output
    assert "\x1b[31m" in output
