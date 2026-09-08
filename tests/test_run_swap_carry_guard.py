"""Swap carry 无人值守风控守护进程测试；全部离线且假客户端默认抛错。"""

from __future__ import annotations

import asyncio
import base64
import json
import plistlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import run_swap_carry_guard

from adapters.base import Position
from adapters.variational_client import (
    VariationalAuthError,
    VariationalJurisdictionError,
    VariationalRequestError,
)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
_UNCONFIGURED = object()


@pytest.fixture(autouse=True)
def _isolate_variational_cookie_environment(monkeypatch) -> None:
    """普通守护测试不继承开发机上的真实会话。"""
    from tools import run_swap_carry_guard
    monkeypatch.setattr(run_swap_carry_guard, "notify", lambda *_args: True)
    monkeypatch.delenv("VARIATIONAL_COOKIE", raising=False)
    monkeypatch.delenv("VARIATIONAL_WALLET_ADDRESS", raising=False)


def _jwt(expires_at: datetime) -> str:
    """构造只供到期测试解码的无签名 JWT。"""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at.timestamp()}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.secret-test-signature"


def _metadata(
    *,
    closure_duration: timedelta = timedelta(hours=1),
    time_until_close: timedelta = timedelta(hours=9),
    market_status: str = "open",
    stale: bool = False,
) -> dict[str, object]:
    """构造可精确控制休市长度与新鲜度的 XAUS 元数据。"""
    close_at = NOW + time_until_close
    next_open_at = close_at + closure_duration
    if stale:
        sessions = [
            {
                "open": (NOW - timedelta(hours=3)).isoformat(),
                "close": (NOW - timedelta(hours=2)).isoformat(),
            }
        ]
    elif market_status == "open":
        sessions = [
            {
                "open": (NOW - timedelta(hours=1)).isoformat(),
                "close": close_at.isoformat(),
            },
            {
                "open": next_open_at.isoformat(),
                "close": (next_open_at + timedelta(hours=23)).isoformat(),
            },
        ]
    else:
        previous_close = NOW - timedelta(minutes=30)
        next_open_at = previous_close + closure_duration
        close_at = previous_close
        sessions = [
            {
                "open": (previous_close - timedelta(hours=23)).isoformat(),
                "close": previous_close.isoformat(),
            },
            {
                "open": next_open_at.isoformat(),
                "close": (next_open_at + timedelta(hours=23)).isoformat(),
            },
        ]
    return {
        "XAUS": [
            {
                "asset": "XAUS",
                "asset_class": "commodity",
                "instrument_type": "swap",
                "funding_interval_s": 0,
                "isolated_only": True,
                "market_status": market_status,
                "price": "4000",
                "trading_schedule": {
                    "next_open_at": next_open_at.isoformat(),
                    "next_close_at": close_at.isoformat(),
                },
                "trading_sessions": sessions,
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


def _maintenance_rate(underlying: str) -> Decimal:
    """返回实测标的值，并为结构外 BTC 提供真实路径下的测试值。"""
    return {
        "XAUS": Decimal("0.05"),
        "XAU": Decimal("0.025"),
        "XAUT": Decimal("0.0142855"),
        "BTC": Decimal("0.01"),
    }[underlying]


def _position_payload(
    underlying: str,
    qty: Decimal,
    *,
    mark_price: Decimal,
) -> dict[str, object]:
    """按真实 `/positions` schema 构造单条持仓。"""
    instrument_types = {
        "XAUS": ("swap", 0, "commodity"),
        "XAU": ("perpetual_rwa_future", 3600, "commodity"),
        "XAUT": ("perpetual_future", 3600, None),
        "BTC": ("perpetual_future", 3600, None),
    }
    instrument_type, funding_interval_s, kind = instrument_types[underlying]
    instrument: dict[str, object] = {
        "underlying": underlying,
        "instrument_type": instrument_type,
        "funding_interval_s": funding_interval_s,
        "settlement_asset": "USDC",
    }
    if kind is not None:
        instrument["kind"] = kind
    return {
        "position_info": {
            "instrument": instrument,
            "qty": str(qty),
            "avg_entry_price": str(mark_price),
        },
        "price_info": {"underlying_price": str(mark_price)},
        "upnl": "0",
        "initial_margin": str(abs(qty) * mark_price * Decimal("0.1")),
    }


class StrictGuardClient:
    """只允许测试显式配置的调用；遗漏编排时立即失败。"""

    def __init__(
        self,
        *,
        positions: dict[str, Decimal],
        metadata: object,
        liquidation_info: object = _UNCONFIGURED,
        accept_script: list[object] | None = None,
        quote_enabled: bool = True,
        apply_accept: bool = True,
        swap_rate: object = None,
        perp_rate: object = None,
        equity: Decimal | None = None,
        account_positions: list[dict[str, object]] | None = None,
    ) -> None:
        self.sizes = dict(positions)
        self.metadata = metadata
        self.liquidation_info = liquidation_info
        self.accept_script = (
            list(accept_script) if accept_script is not None else None
        )
        self.quote_enabled = quote_enabled
        self.apply_accept = apply_accept
        self.swap_rate = swap_rate
        self.perp_rate = perp_rate
        self.equity = equity
        self.account_positions = account_positions
        self.quote_calls: list[tuple[str, str, Decimal]] = []
        self.accept_calls: list[tuple[str, str, bool]] = []
        self.position_calls: list[tuple[str, bool]] = []
        self.liquidation_calls: list[tuple[str, bool]] = []
        self.funding_calls: list[tuple[str, str | None]] = []
        self.all_positions_calls = 0
        self.balance_calls = 0
        self._quotes: dict[str, tuple[str, str, Decimal]] = {}
        self._quote_index = 0
        self._max_slippage = 0.01

    async def get_position(self, underlying: str, *, exact: bool = False) -> Position:
        self.position_calls.append((underlying, exact))
        assert exact is True
        if underlying not in self.sizes:
            raise AssertionError(f"未配置仓位：{underlying}")
        return Position(underlying, self.sizes[underlying])

    async def get_positions(self) -> list[dict[str, object]]:
        """返回真实 schema 的账户全部持仓，而非仅返回结构腿。"""
        self.all_positions_calls += 1
        if self.account_positions is not None:
            return self.account_positions
        return [
            _position_payload(
                underlying,
                qty,
                mark_price=Decimal("4000"),
            )
            for underlying, qty in self.sizes.items()
            if qty != 0
        ]

    async def get_supported_assets(self) -> object:
        if self.metadata is None:
            raise AssertionError("未配置调用：get_supported_assets")
        return self.metadata

    async def get_liquidation_info(
        self, underlying: str, *, exact: bool = False
    ) -> object:
        self.liquidation_calls.append((underlying, exact))
        assert exact is True
        if self.liquidation_info is _UNCONFIGURED:
            raise AssertionError(f"未配置调用：get_liquidation_info({underlying})")
        result = self.liquidation_info
        if isinstance(result, dict):
            if underlying not in result:
                raise AssertionError(
                    f"未配置调用：get_liquidation_info({underlying})"
                )
            result = result[underlying]
        if isinstance(result, BaseException):
            raise result
        return result

    async def request_quote(
        self,
        underlying: str,
        side: str,
        qty: Decimal,
        **_kwargs: object,
    ) -> dict[str, object]:
        if not self.quote_enabled:
            raise AssertionError("未配置调用：request_quote")
        self.quote_calls.append((underlying, side, qty))
        self._quote_index += 1
        quote_id = f"q-{self._quote_index}"
        self._quotes[quote_id] = (underlying, side, qty)
        return {
            "quote_id": quote_id,
            "bid": "3999",
            "ask": "4001",
            "mark_price": "4000",
            "margin_requirements": _margin_requirements(
                qty=qty,
                isolated=underlying == "XAUS",
            ),
            "margin_params": {
                "params": {
                    "asset_params": {
                        underlying: {
                            "futures_initial_margin": "0.05",
                            "futures_maintenance_margin": str(
                                _maintenance_rate(underlying)
                            )
                        }
                    },
                    "default_asset_param": {
                        "futures_maintenance_margin": "0.1"
                    },
                    "use_default_asset_param": False,
                }
            },
        }

    async def get_swap_funding(self, underlying: str) -> object:
        self.funding_calls.append((underlying, None))
        if self.swap_rate is None:
            raise AssertionError("未配置调用：get_swap_funding")
        if isinstance(self.swap_rate, BaseException):
            raise self.swap_rate
        return SimpleNamespace(
            upcoming=SimpleNamespace(
                long_rate=SimpleNamespace(normalized_annual_rate=self.swap_rate)
            )
        )

    async def get_funding_rate(
        self, underlying: str, instrument_type: str
    ) -> object:
        self.funding_calls.append((underlying, instrument_type))
        if self.perp_rate is None:
            raise AssertionError("未配置调用：get_funding_rate")
        result = self.perp_rate
        if isinstance(result, dict):
            if underlying not in result:
                raise AssertionError(
                    f"未配置调用：get_funding_rate({underlying})"
                )
            result = result[underlying]
        if isinstance(result, BaseException):
            raise result
        return result

    async def raw(self, path: str) -> object:
        """真实 /portfolio 只给 balance 和 upnl，不虚构可用余额。"""
        assert path == "/portfolio"
        if self.equity is None:
            raise AssertionError("未配置调用：raw(/portfolio)")
        return {"balance": str(self.equity), "upnl": "0"}

    async def get_balance(self) -> object:
        self.balance_calls += 1
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
        result = self.accept_script.pop(0)
        if isinstance(result, BaseException):
            raise result
        underlying, quoted_side, qty = self._quotes[quote_id]
        assert quoted_side == side
        current = self.sizes[underlying]
        if self.apply_accept:
            if is_reduce_only:
                if current > 0 and side == "sell":
                    self.sizes[underlying] = max(Decimal("0"), current - qty)
                elif current < 0 and side == "buy":
                    self.sizes[underlying] = min(Decimal("0"), current + qty)
            else:
                delta = qty if side == "buy" else -qty
                self.sizes[underlying] = current + delta
        return {"rfq_id": f"rfq-{len(self.accept_calls)}"}


def _paths(tmp_path: Path) -> dict[str, Path]:
    """返回单个测试隔离使用的全部本地状态路径。"""
    return {
        "kill_switch_path": tmp_path / "swap_carry.kill",
        "heartbeat_path": tmp_path / "heartbeat.json",
        "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl",
        "switch_history_path": tmp_path / "switch_history.jsonl",
    }


def _run(client: StrictGuardClient, tmp_path: Path, **kwargs: object) -> int:
    """执行一轮并固定观察时点。"""
    from tools import run_swap_carry_guard

    return asyncio.run(
        run_swap_carry_guard.run_once(
            client,
            now=kwargs.pop("now", NOW),
            **_paths(tmp_path),
            **kwargs,
        )
    )


def _accepted_markets(client: StrictGuardClient) -> list[tuple[str, str, bool]]:
    """把报价编号转换为可读的标的、方向、reduce_only。"""
    return [
        (client._quotes[quote_id][0], side, reduce_only)
        for quote_id, side, reduce_only in client.accept_calls
    ]


def _healthy_client(
    *,
    positions: dict[str, Decimal] | None = None,
    metadata: object | None = None,
    liquidation_info: object = _UNCONFIGURED,
    accept_script: list[object] | None = None,
    swap_rate: object = None,
    perp_rate: object = None,
    equity: Decimal = Decimal("1000"),
    account_positions: list[dict[str, object]] | None = None,
) -> StrictGuardClient:
    """构造默认安全且配平的守护客户端。"""
    if liquidation_info is _UNCONFIGURED:
        liquidation_info = {
            "XAUS": (Decimal("4000"), Decimal("3900")),
            "XAU": (Decimal("4000"), Decimal("4100")),
        }
    return StrictGuardClient(
        positions=positions
        or {"XAUS": Decimal("0.01"), "XAU": Decimal("-0.01")},
        metadata=metadata if metadata is not None else _metadata(),
        liquidation_info=liquidation_info,
        accept_script=accept_script,
        swap_rate=swap_rate,
        perp_rate=perp_rate,
        equity=equity,
        account_positions=account_positions,
    )


def _flat_open_client(
    *,
    metadata: object | None = None,
    swap_rate: object = Decimal("-0.04"),
    perp_rate: object = Decimal("0.10"),
    equity: Decimal = Decimal("1000"),
    accept_script: list[object] | None = None,
) -> StrictGuardClient:
    """构造完整的空仓自动开仓场景。"""
    return StrictGuardClient(
        positions={"XAUS": Decimal("0"), "XAU": Decimal("0")},
        metadata=metadata if metadata is not None else _metadata(),
        swap_rate=swap_rate,
        perp_rate=perp_rate,
        equity=equity,
        accept_script=accept_script,
    )


def _switch_client(
    *,
    positions: dict[str, Decimal],
    metadata: object | None = None,
    accept_script: list[object] | None = None,
    swap_rate: object = Decimal("-0.04"),
    perp_rate: object = None,
) -> StrictGuardClient:
    """构造三标的切换场景，所有可能调用均显式配置。"""
    if perp_rate is None:
        perp_rate = {
            "XAU": Decimal("0.10"),
            "XAUT": Decimal("0.1095"),
        }
    configured_positions = {
        "XAUS": Decimal("0"),
        "XAU": Decimal("0"),
        "XAUT": Decimal("0"),
        **positions,
    }
    return StrictGuardClient(
        positions=configured_positions,
        metadata=metadata if metadata is not None else _metadata(),
        liquidation_info={
            "XAUS": (Decimal("4000"), Decimal("3900")),
            "XAU": (Decimal("4000"), Decimal("4100")),
            "XAUT": (Decimal("4000"), Decimal("4200")),
        },
        accept_script=accept_script,
        swap_rate=swap_rate,
        perp_rate=perp_rate,
        equity=Decimal("1000"),
    )


def test_kill_switch_flattens_both_legs_reduce_only(tmp_path: Path) -> None:
    """kill switch 命中后必须平两腿，且不得进入最低优先级开仓分支。"""
    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client = _healthy_client(accept_script=[{}, {}])

    result = asyncio.run(
        __import__("tools.run_swap_carry_guard", fromlist=["run_once"]).run_once(
            client, now=NOW, **paths
        )
    )

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAUS", "sell", True),
        ("XAU", "buy", True),
    ]
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["status"] == "kill_switch_active"
    assert client.sizes == {"XAUS": Decimal("0"), "XAU": Decimal("0")}


@pytest.mark.parametrize(
    ("positions", "expected"),
    [
        ({"XAUS": Decimal("0.01"), "XAU": Decimal("0")}, ("XAUS", "sell", True)),
        ({"XAUS": Decimal("0"), "XAU": Decimal("-0.01")}, ("XAU", "buy", True)),
    ],
)
def test_single_remaining_leg_is_flattened(
    tmp_path: Path,
    positions: dict[str, Decimal],
    expected: tuple[str, str, bool],
) -> None:
    """只剩 XAUS 或 XAU 时都必须立即平掉剩余腿。"""
    client = _healthy_client(positions=positions, accept_script=[{}])

    result = _run(client, tmp_path)

    assert result == 0
    assert _accepted_markets(client) == [expected]


def test_notional_difference_over_five_percent_flattens(tmp_path: Path) -> None:
    """两腿名义差超过 5% 时必须退出，不能把显著失衡当成正常波动。"""
    client = _healthy_client(
        positions={"XAUS": Decimal("0.01"), "XAU": Decimal("-0.008")},
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert len(client.accept_calls) == 2


def test_closed_xaus_remaining_leg_records_pending_instead_of_fake_success(
    tmp_path: Path,
) -> None:
    """休市中的裸 XAUS 无法平时必须记录待处理，不能假装成功。"""
    client = _healthy_client(
        positions={"XAUS": Decimal("0.01"), "XAU": Decimal("0")},
        metadata=_metadata(market_status="closed"),
        accept_script=[],
    )

    result = _run(client, tmp_path)

    assert result != 0
    assert client.accept_calls == []
    state = json.loads(_paths(tmp_path)["state_path"].read_text(encoding="utf-8"))
    assert state["status"] == "pending_xaus_close"
    assert state["consecutive_failures"] == 1


def test_low_liquidation_distance_flattens_both_legs(tmp_path: Path) -> None:
    """XAUS 距权威强平价不足阈值时必须退出双腿。"""
    client = _healthy_client(
        liquidation_info=(Decimal("4000"), Decimal("3960")),
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert len(client.accept_calls) == 2
    assert client.sizes == {"XAUS": Decimal("0"), "XAU": Decimal("0")}


def test_missing_liquidation_price_is_unsafe_and_flattens(tmp_path: Path) -> None:
    """isolated 腿强平价拿不到时必须 fail-closed。"""
    client = _healthy_client(
        liquidation_info={
            "XAUS": None,
            "XAU": (Decimal("4000"), Decimal("4100")),
        },
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert len(client.accept_calls) == 2
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "强平" in heartbeat["conclusion"]


def test_cross_leg_missing_liquidation_keeps_running_with_account_check(
    tmp_path: Path,
) -> None:
    """全仓腿没有 per-leg 强平价时不得平仓，仍须完成账户级检查。"""
    from tools import hedge_swap_carry

    client = _healthy_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        liquidation_info={
            "XAU": (Decimal("4000"), Decimal("3900")),
            "XAUT": None,
        },
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path, structure=hedge_swap_carry.XAU_XAUT, auto_switch=False)

    assert result == 0
    assert client.accept_calls == []
    assert client.all_positions_calls == 1
    assert client.balance_calls == 1
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    xaut_monitor = heartbeat["legs"]["XAUT"]["per_leg_liquidation"]
    assert xaut_monitor["status"] == "无数据"
    assert xaut_monitor["enforced"] is False
    assert xaut_monitor["fallback"] == "账户级保证金率"
    assert Decimal(heartbeat["account_margin"]["ratio"]) > Decimal("2.0")


def test_low_account_margin_ratio_flattens_even_with_normal_leg_prices(
    tmp_path: Path,
) -> None:
    """账户级保证金率低于阈值时，即使各腿强平价正常也必须平仓。"""
    client = _healthy_client(
        liquidation_info={
            "XAUS": (Decimal("4000"), Decimal("3900")),
            "XAU": (Decimal("4000"), Decimal("4100")),
        },
        equity=Decimal("5"),
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert len(client.accept_calls) == 2
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert Decimal(heartbeat["account_margin"]["ratio"]) < Decimal("2.0")
    assert "账户保证金率" in heartbeat["conclusion"]


def test_account_margin_includes_btc_outside_selected_structure(
    tmp_path: Path,
) -> None:
    """账户级维持保证金必须覆盖 `/positions` 中结构外的 BTC 腿。"""
    from tools import hedge_swap_carry

    positions = {"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")}
    account_positions = [
        _position_payload("XAU", Decimal("0.01"), mark_price=Decimal("4000")),
        _position_payload("XAUT", Decimal("-0.01"), mark_price=Decimal("4000")),
        _position_payload("BTC", Decimal("0.01"), mark_price=Decimal("100000")),
    ]
    client = _healthy_client(
        positions=positions,
        liquidation_info={
            "XAU": (Decimal("4000"), Decimal("3900")),
            "XAUT": (Decimal("4000"), Decimal("4100")),
        },
        equity=Decimal("20"),
        account_positions=account_positions,
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path, structure=hedge_swap_carry.XAU_XAUT)

    assert result == 0
    assert len(client.accept_calls) == 2
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    margins = heartbeat["account_margin"]["positions"]
    assert {item["underlying"] for item in margins} == {"XAU", "XAUT", "BTC"}
    assert next(
        item for item in margins if item["underlying"] == "BTC"
    )["maintenance_margin"] == "10.0000"
    assert Decimal(heartbeat["account_margin"]["ratio"]) < Decimal("2.0")


def test_missing_isolated_only_and_margin_mode_defaults_to_isolated(
    tmp_path: Path,
) -> None:
    """两级保证金模式证据都缺失时须保守按 isolated 严格监控。"""
    from tools import hedge_swap_carry

    metadata = _metadata()
    metadata["XAU"][0].pop("isolated_only")  # type: ignore[index,union-attr]
    client = _healthy_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        metadata=metadata,
        liquidation_info={
            "XAU": None,
            "XAUT": (Decimal("4000"), Decimal("4100")),
        },
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path, structure=hedge_swap_carry.XAU_XAUT)

    assert result == 0
    assert len(client.accept_calls) == 2
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert heartbeat["legs"]["XAU"]["margin_mode"] == "isolated"
    assert heartbeat["legs"]["XAU"]["margin_mode_source"] == "保守默认"


def test_incident_regression_xau_cross_missing_liquidation_stays_open(
    tmp_path: Path,
) -> None:
    """复现事故：健康的 XAU/XAUT 不得因 XAU 强平价缺失而随机平仓。"""
    from tools import hedge_swap_carry

    client = _healthy_client(
        positions={"XAU": Decimal("0.1"), "XAUT": Decimal("-0.1")},
        liquidation_info={
            "XAU": None,
            "XAUT": (Decimal("4000"), Decimal("4200")),
        },
        equity=Decimal("1000"),
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path, structure=hedge_swap_carry.XAU_XAUT)

    assert result == 0
    assert client.accept_calls == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert heartbeat["legs"]["XAU"]["margin_mode"] == "cross"
    assert heartbeat["legs"]["XAU"]["per_leg_liquidation"]["status"] == "无数据"
    assert Decimal(heartbeat["account_margin"]["ratio"]) > Decimal("2.0")


def test_long_closure_within_preclose_window_flattens(tmp_path: Path) -> None:
    """49 小时长休市前 25 分钟必须平掉两腿。"""
    client = _healthy_client(
        metadata=_metadata(
            closure_duration=timedelta(hours=49),
            time_until_close=timedelta(minutes=25),
        ),
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert len(client.accept_calls) == 2


def test_daily_one_hour_closure_is_held_through(tmp_path: Path) -> None:
    """每日一小时休市不得触发平仓，否则交易成本会摧毁 carry。"""
    client = _healthy_client(
        metadata=_metadata(
            closure_duration=timedelta(hours=1),
            time_until_close=timedelta(minutes=25),
        ),
        accept_script=None,
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert client.quote_calls == []
    assert client.accept_calls == []


def test_stale_schedule_metadata_flattens(tmp_path: Path) -> None:
    """交易时段元数据陈旧时按不确定处理并平仓。"""
    client = _healthy_client(
        metadata=_metadata(stale=True),
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert len(client.accept_calls) == 2


def test_missing_market_status_metadata_flattens(tmp_path: Path) -> None:
    """market_status 缺失不是权威休市，必须按不确定尝试平两腿。"""
    metadata = _metadata()
    metadata["XAUS"][0].pop("market_status")  # type: ignore[index,union-attr]
    client = _healthy_client(metadata=metadata, accept_script=[{}, {}])

    result = _run(client, tmp_path)

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAUS", "sell", True),
        ("XAU", "buy", True),
    ]


def test_missing_notional_price_is_uncertain_and_flattens(tmp_path: Path) -> None:
    """拿不到双腿名义时无法验证 5% 阈值，必须按不确定平仓。"""
    metadata = _metadata()
    metadata["XAU"][0].pop("price")  # type: ignore[index,union-attr]
    client = _healthy_client(metadata=metadata, accept_script=[{}, {}])

    result = _run(client, tmp_path)

    assert result == 0
    assert len(client.accept_calls) == 2
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "名义" in heartbeat["conclusion"]


@pytest.mark.parametrize(
    ("failure", "expected_text"),
    [
        (VariationalJurisdictionError("restricted jurisdiction"), "地区"),
        (VariationalAuthError("Cookie 已失效"), "会话失效"),
    ],
)
def test_execution_block_notifies_persists_and_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
    expected_text: str,
) -> None:
    """地区封锁或会话失效不能静默重试，必须通知、写状态并计数。"""
    from tools import run_swap_carry_guard

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client = _healthy_client(accept_script=[failure])

    result = asyncio.run(run_swap_carry_guard.run_once(client, now=NOW, **paths))

    assert result != 0
    assert len(client.accept_calls) == 1
    assert notifications and expected_text in notifications[0][1]
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["status"] == "action_failed"
    assert state["consecutive_failures"] == 1
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert heartbeat["consecutive_failures"] == 1


def test_close_failure_retries_are_finite_then_become_loud_incident(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """普通平仓失败只重试有限次，耗尽后通知并非零退出。"""
    from tools import run_swap_carry_guard

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(run_swap_carry_guard, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client = _healthy_client(
        accept_script=[RuntimeError("拒绝 1"), RuntimeError("拒绝 2"), RuntimeError("拒绝 3")]
    )

    result = asyncio.run(run_swap_carry_guard.run_once(client, now=NOW, **paths))

    assert result != 0
    assert len(client.accept_calls) == run_swap_carry_guard.CLOSE_RETRIES
    assert notifications
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["status"] == "action_failed"
    assert "连续失败 3 次" in state["message"]


def test_accept_success_without_flat_confirmation_is_not_reported_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """accept 返回成功但实仓未归零时必须报事故，不能误报已平仓。"""
    from tools import hedge_swap_carry, run_swap_carry_guard

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(hedge_swap_carry, "_POLL_DELAY_S", 0)
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client = _healthy_client(accept_script=[{}, {}])
    client.apply_accept = False

    result = asyncio.run(run_swap_carry_guard.run_once(client, now=NOW, **paths))

    assert result != 0
    assert notifications
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["status"] == "action_failed"
    assert "未归零" in state["message"]


def test_heartbeat_and_audit_are_written_even_without_action(tmp_path: Path) -> None:
    """安全轮次也必须留下完整心跳与审计记录。"""
    client = _healthy_client(accept_script=None)

    result = _run(client, tmp_path)

    assert result == 0
    paths = _paths(tmp_path)
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert heartbeat["timestamp"] == NOW.isoformat()
    assert heartbeat["conclusion"]
    assert heartbeat["xaus_notional"] == "40.00"
    assert heartbeat["xau_notional"] == "40.00"
    assert heartbeat["net_delta"] == "0.00"
    assert heartbeat["xaus_schedule"]["closure_duration_seconds"] == 3600
    assert heartbeat["consecutive_failures"] == 0
    assert paths["audit_path"].read_text(encoding="utf-8").strip()


def test_session_under_24_hours_notifies_once_during_two_hour_cooldown(
    tmp_path: Path,
    monkeypatch,
    capsys,
    caplog,
) -> None:
    """24 小时预警要写心跳，但两小时内不能重复弹窗。"""
    from tools import run_swap_carry_guard

    token = _jwt(NOW + timedelta(hours=23))
    monkeypatch.setenv("VARIATIONAL_COOKIE", f"other=value; vr-token={token}")
    monkeypatch.setenv("VARIATIONAL_WALLET_ADDRESS", "0xabc")
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    paths = _paths(tmp_path)

    first = asyncio.run(
        run_swap_carry_guard.run_once(
            _healthy_client(),
            now=NOW,
            **paths,
        )
    )
    second = asyncio.run(
        run_swap_carry_guard.run_once(
            _healthy_client(),
            now=NOW + timedelta(minutes=5),
            **paths,
        )
    )

    assert first == second == 0
    assert len(notifications) == 1
    heartbeat_text = paths["heartbeat_path"].read_text(encoding="utf-8")
    audit_text = paths["audit_path"].read_text(encoding="utf-8")
    heartbeat = json.loads(heartbeat_text)
    assert heartbeat["session_expires_at"] == (NOW + timedelta(hours=23)).isoformat()
    assert heartbeat["session_hours_left"] == pytest.approx(23 - 5 / 60)
    assert token not in heartbeat_text
    assert token not in audit_text
    assert all(token not in " ".join(item) for item in notifications)
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert token not in caplog.text


def test_session_under_six_hours_notifies_every_round(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """临近失效的 critical 通知不受普通冷却限制。"""
    from tools import run_swap_carry_guard

    token = _jwt(NOW + timedelta(hours=5))
    monkeypatch.setenv("VARIATIONAL_COOKIE", f"vr-token={token}")
    monkeypatch.setenv("VARIATIONAL_WALLET_ADDRESS", "0xabc")
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    paths = _paths(tmp_path)

    for minutes in (0, 5):
        result = asyncio.run(
            run_swap_carry_guard.run_once(
                _healthy_client(),
                now=NOW + timedelta(minutes=minutes),
                **paths,
            )
        )
        assert result == 0

    assert len(notifications) == 2
    critical_records = [
        json.loads(line)
        for line in paths["audit_path"].read_text(encoding="utf-8").splitlines()
        if '"event": "session_expiry_warning"' in line
    ]
    assert len(critical_records) == 2
    assert all(record["level"] == "critical" for record in critical_records)


def test_expired_session_blocks_open_and_switch_without_flattening(
    tmp_path: Path,
    monkeypatch,
    capsys,
    caplog,
) -> None:
    """过期前置门禁不得把结构切换误执行成先平旧仓。"""
    from tools import run_swap_carry_guard

    token = _jwt(NOW - timedelta(hours=1))
    monkeypatch.setenv("VARIATIONAL_COOKIE", f"vr-token={token}")
    monkeypatch.setenv("VARIATIONAL_WALLET_ADDRESS", "0xabc")
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[],
    )
    paths = _paths(tmp_path)

    result = asyncio.run(
        run_swap_carry_guard.run_once(
            client,
            now=NOW,
            auto_switch=True,
            **paths,
        )
    )

    assert result != 0
    assert client.all_positions_calls == 0
    assert client.quote_calls == []
    assert client.accept_calls == []
    heartbeat_text = paths["heartbeat_path"].read_text(encoding="utf-8")
    state_text = paths["state_path"].read_text(encoding="utf-8")
    audit_text = paths["audit_path"].read_text(encoding="utf-8")
    assert "会话已过期，需人工刷新 Cookie" in heartbeat_text
    assert "会话已过期，需人工刷新 Cookie" in state_text
    assert '"level": "critical"' in audit_text
    assert token not in heartbeat_text
    assert token not in state_text
    assert token not in audit_text
    assert all(token not in " ".join(item) for item in notifications)
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert token not in caplog.text


def test_dry_run_performs_decision_but_never_accepts(tmp_path: Path) -> None:
    """dry-run 必须给出将执行的动作，但绝不能调用 accept。"""
    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client = _healthy_client(accept_script=None)

    result = asyncio.run(
        __import__("tools.run_swap_carry_guard", fromlist=["run_once"]).run_once(
            client,
            now=NOW,
            dry_run=True,
            **paths,
        )
    )

    assert result == 0
    assert {call[0] for call in client.quote_calls} == {"XAUS", "XAU"}
    assert client.accept_calls == []
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["status"] == "dry_run"
    assert state["last_closed_at"] is None


def test_launchd_plist_runs_guard_once_every_minute() -> None:
    """部署文件只声明每分钟单轮执行，不在测试中加载 launchd。"""
    path = Path("deploy/com.variational.swap-carry-guard.plist")
    payload = plistlib.loads(path.read_bytes())

    assert payload["StartInterval"] == 60
    assert "tools.run_swap_carry_guard" in payload["ProgramArguments"]
    assert "--once" in payload["ProgramArguments"]


def test_auto_open_skips_when_net_carry_is_below_threshold(tmp_path: Path) -> None:
    """净 carry 低于 2% 时不得询价或开仓。"""
    client = _flat_open_client(
        swap_rate=Decimal("-0.04"),
        perp_rate=Decimal("0.0599"),
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert client.quote_calls == []
    assert client.accept_calls == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert heartbeat["auto_open_attempted"] is False
    assert "低于" in heartbeat["auto_open_conclusion"]


def test_auto_open_skips_when_carry_cannot_be_read(tmp_path: Path) -> None:
    """carry 任一腿不可读时按不确定处理，不得开仓。"""
    client = _flat_open_client(swap_rate=RuntimeError("资金费暂不可用"))

    result = _run(client, tmp_path)

    assert result == 0
    assert client.quote_calls == []
    assert client.accept_calls == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "读取失败" in heartbeat["candidate_structures"]["XAUS_XAU"]["reason"]
    assert heartbeat["best_structure"] == "XAU_XAUT"


def test_auto_open_skips_when_xaus_is_not_tradable(tmp_path: Path) -> None:
    """XAUS 不可交易时排除相关结构，其余 carry 仍须评估。"""
    client = _flat_open_client(metadata=_metadata(market_status="closed"))

    result = _run(client, tmp_path)

    assert result == 0
    assert {call[0] for call in client.funding_calls} == {"XAUS", "XAU", "XAUT"}
    assert client.quote_calls == []
    assert client.accept_calls == []


def test_auto_open_skips_with_two_hours_or_less_to_close(tmp_path: Path) -> None:
    """距休市不超过两小时时排除 XAUS，剩余结构低于阈值时不入场。"""
    client = _flat_open_client(
        metadata=_metadata(time_until_close=timedelta(hours=2))
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert {call[0] for call in client.funding_calls} == {"XAUS", "XAU", "XAUT"}
    assert client.quote_calls == []
    assert client.accept_calls == []


def test_auto_open_never_runs_while_either_leg_exists(tmp_path: Path) -> None:
    """已有任一目标腿时只执行既有风控，不进入自动开仓判定。"""
    client = _healthy_client(accept_script=None)

    result = _run(client, tmp_path)

    assert result == 0
    assert client.accept_calls == []


@pytest.mark.parametrize("carry,hours,closes", [
    ("-0.03", 2, False), ("-0.12", 23, False),
    ("-0.12", 24, True), ("-0.10", 24, True),
])
def test_exit_carry_duration_survives_restart(tmp_path, monkeypatch, carry, hours, closes):
    """每轮重建客户端并重读磁盘，按经过时间而非轮数决定平仓。"""
    start = NOW
    for elapsed in (0, hours / 2, hours):
        current = start + timedelta(hours=elapsed)
        monkeypatch.setattr(__import__(__name__, fromlist=["NOW"]), "NOW", current)
        client = _healthy_client(swap_rate=Decimal(carry), perp_rate=Decimal("0"),
                                 accept_script=[{}, {}])
        assert _run(client, tmp_path, auto_switch=False) == 0
        assert bool(client.accept_calls) is (closes and elapsed == hours)
        state = json.loads(_paths(tmp_path)["state_path"].read_text())
        expected = start.isoformat() if Decimal(carry) <= Decimal("-0.10") else None
        assert state["exit_carry_since"] == (None if closes and elapsed == hours else expected)
        if closes and elapsed == hours:
            assert all(reduce_only for _, _, reduce_only in client.accept_calls)
            assert state["last_closed_at"] == current.isoformat()


@pytest.mark.parametrize("recovered", ["0.03", "-0.03"])
def test_recovery_resets_exit_timer(tmp_path, monkeypatch, recovered):
    """回到阈值以上即清零，下一次恶化从当前时间重新开始。"""
    start = NOW
    for hours, carry in [(0, "-0.12"), (23, recovered), (24, "-0.12"), (47, "-0.12")]:
        monkeypatch.setattr(__import__(__name__, fromlist=["NOW"]), "NOW", start + timedelta(hours=hours))
        client = _healthy_client(swap_rate=Decimal(carry), perp_rate=Decimal("0"))
        assert _run(client, tmp_path, auto_switch=False) == 0
        assert client.accept_calls == []
        state = json.loads(_paths(tmp_path)["state_path"].read_text())
        assert state["exit_carry_since"] == (None if hours == 23 else
            (start + timedelta(hours=24 if hours >= 24 else 0)).isoformat())


def test_unreadable_carry_preserves_timer_without_closing(tmp_path):
    """读取失败保留起点，但不能仅凭旧值触发平仓。"""
    since = (NOW - timedelta(hours=25)).isoformat()
    _paths(tmp_path)["state_path"].write_text(json.dumps({"exit_carry_since": since}))
    client = _healthy_client(swap_rate=RuntimeError("费率不可读"))
    assert _run(client, tmp_path, auto_switch=False) == 0
    assert client.accept_calls == []
    assert json.loads(_paths(tmp_path)["state_path"].read_text())["exit_carry_since"] == since


def test_three_percent_carry_opens_flat_position(tmp_path):
    """3% 已满足新的 2% 入场阈值。"""
    client = _flat_open_client(swap_rate=Decimal("-0.04"), perp_rate=Decimal("0.07"), accept_script=[{}, {}])
    assert _run(client, tmp_path) == 0
    assert len(client.accept_calls) == 2
    assert all(not reduce_only for _, _, reduce_only in client.accept_calls)


def test_close_cooldown_survives_rounds_and_does_not_slide(tmp_path, monkeypatch):
    """确认平仓后一小时禁止重开，两小时半允许，空仓轮次不延长冷却。"""
    start = NOW
    paths = _paths(tmp_path)
    paths["kill_switch_path"].touch()
    closing = _healthy_client(accept_script=[{}, {}])
    assert _run(closing, tmp_path) == 0
    assert len(closing.accept_calls) == 2
    paths["kill_switch_path"].unlink()
    for hours, opens in [(1, False), (2.5, True)]:
        monkeypatch.setattr(__import__(__name__, fromlist=["NOW"]), "NOW", start + timedelta(hours=hours))
        client = _flat_open_client(accept_script=[{}, {}])
        assert _run(client, tmp_path) == 0
        assert bool(client.accept_calls) is opens
        assert json.loads(paths["state_path"].read_text())["last_closed_at"] == start.isoformat()


def test_auto_open_skips_when_available_margin_is_insufficient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """报价所需保证金高于可用权益时不得 accept。"""
    from tools import run_swap_carry_guard

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    client = _flat_open_client(equity=Decimal("1"), accept_script=[])

    result = _run(client, tmp_path)

    assert result == 0
    assert client.quote_calls
    assert client.accept_calls == []
    assert notifications == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "保证金不足" in heartbeat["auto_open_conclusion"]


def test_flat_kill_switch_takes_priority_over_auto_open(tmp_path: Path) -> None:
    """空仓时 kill switch 仍属于最高优先级平仓类检查。"""
    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client = _flat_open_client(accept_script=[])

    result = asyncio.run(
        __import__("tools.run_swap_carry_guard", fromlist=["run_once"]).run_once(
            client,
            now=NOW,
            **paths,
        )
    )

    assert result == 0
    assert client.funding_calls == []
    assert client.quote_calls == []
    assert client.accept_calls == []
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert heartbeat["auto_open_attempted"] is False
    assert "kill switch" in heartbeat["conclusion"]


def test_skew_422_is_audited_without_notification_and_retries_next_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """偏斜拒绝是可退避的业务结果，不是故障，下一轮可再次尝试。"""
    from tools import run_swap_carry_guard

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    skew = VariationalRequestError(
        422,
        "long OI skew is too large to accommodate your trade",
    )
    client = _flat_open_client(accept_script=[skew, skew])
    paths = _paths(tmp_path)

    first = asyncio.run(run_swap_carry_guard.run_once(client, now=NOW, **paths))
    second = asyncio.run(run_swap_carry_guard.run_once(client, now=NOW, **paths))

    assert (first, second) == (0, 0)
    assert len(client.accept_calls) == 2
    assert all(not call[2] for call in client.accept_calls)
    assert notifications == []
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert heartbeat["auto_open_attempted"] is True
    assert heartbeat["daily_open_attempts"] == 2
    assert "偏斜" in heartbeat["auto_open_conclusion"]
    assert "auto_open_skew_rejected" in paths["audit_path"].read_text(
        encoding="utf-8"
    )


def test_non_skew_422_remains_a_notifiable_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """不含 skew 的 422 不得混入预期业务拒绝，必须按故障处理。"""
    from tools import run_swap_carry_guard

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    client = _flat_open_client(
        accept_script=[VariationalRequestError(422, "invalid margin mode")]
    )

    result = _run(client, tmp_path)

    assert result != 0
    assert notifications
    audit = _paths(tmp_path)["audit_path"].read_text(encoding="utf-8")
    assert "auto_open_failed" in audit
    assert "auto_open_skew_rejected" not in audit


def test_daily_open_attempt_limit_stops_further_attempts(tmp_path: Path) -> None:
    """同一 UTC 日达到尝试上限后仍评估 carry，但不得询价或开仓。"""
    from tools import run_swap_carry_guard

    paths = _paths(tmp_path)
    paths["state_path"].write_text(
        json.dumps(
            {
                "consecutive_failures": 0,
                "open_attempt_date": NOW.date().isoformat(),
                "daily_open_attempts": run_swap_carry_guard.MAX_DAILY_OPEN_ATTEMPTS,
                "auto_open_incident": False,
            }
        ),
        encoding="utf-8",
    )
    client = _flat_open_client(accept_script=[])

    result = asyncio.run(run_swap_carry_guard.run_once(client, now=NOW, **paths))

    assert result == 0
    assert {call[0] for call in client.funding_calls} == {"XAUS", "XAU", "XAUT"}
    assert client.quote_calls == []
    assert client.accept_calls == []
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert heartbeat["daily_open_attempts"] == run_swap_carry_guard.MAX_DAILY_OPEN_ATTEMPTS
    assert "上限" in heartbeat["auto_open_conclusion"]


def test_auto_open_success_uses_fixed_two_leg_execution(tmp_path: Path) -> None:
    """全部条件满足时应按多 XAUS、空 XAU 顺序自动建立等数量双腿。"""
    client = _flat_open_client(accept_script=[{}, {}])

    result = _run(client, tmp_path)

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAUS", "buy", False),
        ("XAU", "sell", False),
    ]
    assert client.sizes["XAUS"] == -client.sizes["XAU"]
    assert client.sizes["XAUS"] > 0
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert heartbeat["auto_open_attempted"] is True
    assert heartbeat["daily_open_attempts"] == 1


def test_second_leg_failure_rolls_back_first_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """自动开仓第二腿失败时必须复用 reduce_only 回滚原语。"""
    from tools import run_swap_carry_guard

    monkeypatch.setattr(run_swap_carry_guard, "notify", lambda *_args: True)
    client = _flat_open_client(
        accept_script=[{}, RuntimeError("第二腿拒绝"), {}]
    )

    result = _run(client, tmp_path)

    assert result != 0
    assert _accepted_markets(client) == [
        ("XAUS", "buy", False),
        ("XAU", "sell", False),
        ("XAUS", "sell", True),
    ]
    assert client.sizes == {"XAUS": Decimal("0"), "XAU": Decimal("0")}
    state = json.loads(_paths(tmp_path)["state_path"].read_text(encoding="utf-8"))
    assert state["auto_open_incident"] is False
    assert state["last_closed_at"] == NOW.isoformat()
    reopened = _flat_open_client(accept_script=[{}, {}])
    assert _run(reopened, tmp_path, now=NOW + timedelta(hours=1)) == 0
    assert reopened.accept_calls == []


def test_rollback_failure_enters_incident_and_blocks_later_auto_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """回滚失败后 INCIDENT 必须跨轮次阻断全部新开仓。"""
    from tools import run_swap_carry_guard

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, body: notifications.append((title, body)) or True,
    )
    client = _flat_open_client(
        accept_script=[{}, RuntimeError("第二腿拒绝"), RuntimeError("回滚拒绝")]
    )
    paths = _paths(tmp_path)

    first = asyncio.run(run_swap_carry_guard.run_once(client, now=NOW, **paths))

    assert first != 0
    assert notifications
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["status"] == "incident"
    assert state["auto_open_incident"] is True
    first_accept_count = len(client.accept_calls)

    # 模拟人工已把裸腿归零但尚未解除 INCIDENT；守护仍不得重开。
    client.sizes = {"XAUS": Decimal("0"), "XAU": Decimal("0")}
    funding_count = len(client.funding_calls)
    second = asyncio.run(run_swap_carry_guard.run_once(client, now=NOW, **paths))

    assert second != 0
    assert len(client.accept_calls) == first_accept_count
    assert len(client.funding_calls) == funding_count + 3
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert "INCIDENT" in heartbeat["auto_open_conclusion"]


def test_auto_open_rejects_notional_over_execution_hard_cap(tmp_path: Path) -> None:
    """自动名义超过人工执行器硬上限时仍评估 carry，但不得询价或 accept。"""
    from tools import hedge_swap_carry

    client = _flat_open_client(accept_script=[])

    result = _run(
        client,
        tmp_path,
        auto_open_notional=hedge_swap_carry.MAX_NOTIONAL_USD + Decimal("0.01"),
    )

    assert result == 0
    assert {call[0] for call in client.funding_calls} == {"XAUS", "XAU", "XAUT"}
    assert client.quote_calls == []
    assert client.accept_calls == []


def test_no_auto_open_disables_only_entry(tmp_path: Path) -> None:
    """--no-auto-open 必须关闭入场，但仍保留 kill switch 自动平仓。"""
    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client = _healthy_client(accept_script=[{}, {}])

    result = asyncio.run(
        __import__("tools.run_swap_carry_guard", fromlist=["run_once"]).run_once(
            client,
            now=NOW,
            auto_open=False,
            **paths,
        )
    )

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAUS", "sell", True),
        ("XAU", "buy", True),
    ]
    assert client.funding_calls == []


def test_auto_open_dry_run_checks_and_quotes_but_never_accepts(
    tmp_path: Path,
) -> None:
    """空仓 dry-run 必须走完入场、报价和保证金判定，但绝不 accept。"""
    client = _flat_open_client(accept_script=None)

    result = _run(client, tmp_path, dry_run=True)

    assert result == 0
    assert {call[0] for call in client.quote_calls} == {"XAUS", "XAU"}
    assert client.accept_calls == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert heartbeat["auto_open_attempted"] is True
    assert heartbeat["daily_open_attempts"] == 0


def test_auto_open_cli_and_environment_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动开仓可独立关闭，名义可由环境变量或命令行覆盖。"""
    from tools import run_swap_carry_guard

    env_notional = run_swap_carry_guard.AUTO_OPEN_NOTIONAL_USD / Decimal("2")
    monkeypatch.setenv("AUTO_OPEN_NOTIONAL_USD", str(env_notional))
    parser = run_swap_carry_guard.build_parser()

    env_args = parser.parse_args(["--once", "--no-auto-open"])
    cli_args = parser.parse_args(["--once", "--auto-open-notional", "750"])

    assert env_args.auto_open is False
    assert env_args.auto_open_notional == env_notional
    assert cli_args.auto_open is True
    assert cli_args.auto_open_notional == Decimal("750")


def test_open_market_switches_xau_xaut_to_xaus_xau(tmp_path: Path) -> None:
    """开市中应先平周末结构，再建立多 XAUS、空 XAU。"""
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[{}, {}, {}, {}],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAU", "sell", True),
        ("XAUT", "buy", True),
        ("XAUS", "buy", False),
        ("XAU", "sell", False),
    ]
    assert client.sizes["XAUT"] == 0
    assert client.sizes["XAUS"] == -client.sizes["XAU"] > 0


def test_weekend_target_xau_xaut_accepts_zero_xau_rate(
    tmp_path: Path,
) -> None:
    """周末目标结构应把合法的 XAU 零费率计入 carry 并允许开仓。"""
    client = _switch_client(
        positions={},
        metadata=_metadata(
            closure_duration=timedelta(hours=49),
            market_status="closed",
        ),
        perp_rate={
            "XAU": Decimal("0"),
            "XAUT": Decimal("0.1095"),
        },
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert client.funding_calls == [
        ("XAUS", None),
        ("XAU", "perpetual_rwa_future"),
        ("XAUT", "perpetual_future"),
        ("XAU", "perpetual_rwa_future"),
        ("XAUT", "perpetual_future"),
    ]
    assert _accepted_markets(client) == [
        ("XAU", "buy", False),
        ("XAUT", "sell", False),
    ]
    audit_records = [
        json.loads(line)
        for line in _paths(tmp_path)["audit_path"].read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    attempt = next(
        record
        for record in audit_records
        if record["event"] == "auto_open_attempt"
    )
    assert Decimal(attempt["net_carry_annual"]) == Decimal("0.1095")


def test_weekend_rejects_non_target_xaus_xau_entry(tmp_path: Path) -> None:
    """周末不得用当期费率打开并非时段目标的 XAUS_XAU。"""
    client = _flat_open_client(
        metadata=_metadata(
            closure_duration=timedelta(hours=49),
            market_status="closed",
        ),
        accept_script=[],
    )

    result = _run(
        client,
        tmp_path,
        structure="XAUS_XAU",
        auto_switch=False,
    )

    assert result == 0
    assert client.funding_calls == []
    assert client.quote_calls == []
    assert client.accept_calls == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "不是当前时段目标结构 XAU_XAUT" in heartbeat[
        "auto_open_conclusion"
    ]


def test_open_market_target_xaus_xau_allows_entry(tmp_path: Path) -> None:
    """开市时 XAUS_XAU 等于时段目标，正常读取费率并允许开仓。"""
    client = _switch_client(
        positions={},
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAUS", "buy", False),
        ("XAU", "sell", False),
    ]
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert heartbeat["target_structure"] == "XAUS_XAU"
    assert heartbeat["auto_open_attempted"] is True


def test_weekend_rate_api_failure_is_not_treated_as_zero(tmp_path: Path) -> None:
    """目标结构正确也不能把费率读取异常降级成合法零值。"""
    client = _switch_client(
        positions={},
        metadata=_metadata(
            closure_duration=timedelta(hours=49),
            market_status="closed",
        ),
        perp_rate={
            "XAU": RuntimeError("XAU 费率接口暂不可用"),
            "XAUT": Decimal("0.1095"),
        },
        accept_script=[],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert client.funding_calls == [("XAUS", None), ("XAU", "perpetual_rwa_future"), ("XAUT", "perpetual_future")]
    assert client.quote_calls == []
    assert client.accept_calls == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "读取失败" in heartbeat["auto_open_conclusion"]


def test_weekend_positive_xau_xaut_carry_resets_exit_timer(
    tmp_path: Path,
) -> None:
    """周末目标结构的正 carry 应按正常值评估并清零退出计时。"""
    paths = _paths(tmp_path)
    paths["state_path"].write_text(
        json.dumps({"exit_carry_since": (NOW - timedelta(hours=24)).isoformat()}),
        encoding="utf-8",
    )
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        metadata=_metadata(
            closure_duration=timedelta(hours=49),
            market_status="closed",
        ),
        perp_rate={
            "XAU": Decimal("0"),
            "XAUT": Decimal("0.1095"),
        },
        accept_script=None,
    )

    result = asyncio.run(
        __import__("tools.run_swap_carry_guard", fromlist=["run_once"]).run_once(
            client,
            now=NOW,
            auto_open=False,
            auto_switch=True,
            **paths,
        )
    )

    assert result == 0
    assert client.funding_calls == [
        ("XAU", "perpetual_rwa_future"),
        ("XAUT", "perpetual_future"),
        ("XAUS", None),
        ("XAU", "perpetual_rwa_future"),
        ("XAUT", "perpetual_future"),
    ]
    state = json.loads(paths["state_path"].read_text(encoding="utf-8"))
    assert state["exit_carry_since"] is None
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert heartbeat["net_carry_annual"] == "0.1095"


def test_unknown_schedule_target_rejects_entry_conservatively(
    tmp_path: Path,
) -> None:
    """时段元数据不完整时无法确定目标结构，必须保守拒绝开仓。"""
    metadata = _metadata()
    metadata["XAUS"][0].pop("trading_schedule")  # type: ignore[index,union-attr]
    client = _switch_client(
        positions={},
        metadata=metadata,
        accept_script=[],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert client.funding_calls == []
    assert client.quote_calls == []
    assert client.accept_calls == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert heartbeat["target_structure"] is None
    assert "时段元数据" in heartbeat["auto_open_conclusion"]


def test_45_minutes_before_long_close_switches_to_xau_xaut(
    tmp_path: Path,
) -> None:
    """距长休市 45 分钟时应在 XAUS 关市前完成周末结构切换。"""
    _paths(tmp_path)["state_path"].write_text(json.dumps({"last_closed_at": NOW.isoformat()}))
    client = _switch_client(
        positions={"XAUS": Decimal("0.01"), "XAU": Decimal("-0.01")},
        metadata=_metadata(
            closure_duration=timedelta(hours=49),
            time_until_close=timedelta(minutes=45),
        ),
        accept_script=[{}, {}, {}, {}],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAUS", "sell", True),
        ("XAU", "buy", True),
        ("XAU", "buy", False),
        ("XAUT", "sell", False),
    ]
    assert client.sizes["XAUS"] == 0
    assert client.sizes["XAU"] == -client.sizes["XAUT"] > 0


def test_daily_one_hour_closure_does_not_switch_structure(tmp_path: Path) -> None:
    """每日一小时休市边界必须继续持有 XAUS_XAU，不做结构切换。"""
    client = _switch_client(
        positions={"XAUS": Decimal("0.01"), "XAU": Decimal("-0.01")},
        metadata=_metadata(
            closure_duration=timedelta(hours=1),
            time_until_close=timedelta(minutes=45),
        ),
        accept_script=None,
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert client.accept_calls == []


def test_long_closure_keeps_existing_xau_xaut_unchanged(tmp_path: Path) -> None:
    """长休市中已经处于 XAU_XAUT 时不得重复切换或成交。"""
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        metadata=_metadata(
            closure_duration=timedelta(hours=49),
            market_status="closed",
        ),
        accept_script=None,
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert client.accept_calls == []


def test_open_market_with_unavailable_rates_defers_switch(tmp_path: Path) -> None:
    """周日开市后费率尚未刷新时保留旧结构，等待下一轮重试。"""
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        swap_rate=RuntimeError("XAUS 费率尚未刷新"),
        accept_script=None,
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert client.accept_calls == []
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "费率" in heartbeat["candidate_structures"]["XAUS_XAU"]["reason"]
    assert heartbeat["selection_decision"]["entry_threshold_blocked"]


def test_switch_close_failure_never_opens_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧结构任一平仓失败时不得发送目标结构的非 reduce-only 委托。"""
    from tools import run_swap_carry_guard

    monkeypatch.setattr(run_swap_carry_guard, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(run_swap_carry_guard, "notify", lambda *_args: True)
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[
            RuntimeError("平仓拒绝 1"),
            RuntimeError("平仓拒绝 2"),
            RuntimeError("平仓拒绝 3"),
        ],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result != 0
    assert client.accept_calls
    assert all(call[2] is True for call in client.accept_calls)
    assert client.sizes == {
        "XAUS": Decimal("0"),
        "XAU": Decimal("0.01"),
        "XAUT": Decimal("-0.01"),
    }


def test_switch_open_failure_stays_fully_flat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧结构全平后若目标第二腿失败并回滚，本轮必须安全停在全空仓。"""
    from tools import run_swap_carry_guard

    monkeypatch.setattr(run_swap_carry_guard, "notify", lambda *_args: True)
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[
            {},
            {},
            {},
            RuntimeError("目标第二腿拒绝"),
            {},
        ],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result != 0
    assert _accepted_markets(client) == [
        ("XAU", "sell", True),
        ("XAUT", "buy", True),
        ("XAUS", "buy", False),
        ("XAU", "sell", False),
        ("XAUS", "sell", True),
    ]
    assert all(size == 0 for size in client.sizes.values())

    switch_record = [json.loads(line) for line in
                     _paths(tmp_path)["switch_history_path"].read_text(encoding="utf-8").splitlines()
                     if json.loads(line).get("kind") != "rehearsal"][-1]
    assert switch_record["status"] == "failed"
    assert switch_record["failure"]["stage"] == "open"
    assert switch_record["failure"]["residual_state"] == "空仓"
    assert switch_record["failure"]["has_residual_position"] is False
    assert switch_record["self_check"]["status"] == "开仓失败，账户为空仓"
    assert "completed_at" not in switch_record

    client.accept_script = [{}, {}]
    retry = _run(client, tmp_path, auto_switch=True)

    assert retry == 0
    # 切换失败前确实平旧仓并回滚，立即重试应冷却；到期后仍可恢复开仓。
    assert len(client.accept_calls) == 5
    assert json.loads(_paths(tmp_path)["state_path"].read_text())["last_closed_at"] == NOW.isoformat()
    assert _run(client, tmp_path, auto_switch=True, now=NOW + timedelta(hours=2.5)) == 0
    assert _accepted_markets(client)[-2:] == [
        ("XAUS", "buy", False),
        ("XAU", "sell", False),
    ]
    assert client.sizes["XAUS"] == -client.sizes["XAU"] > 0


def test_simultaneous_structures_are_flattened_without_switching(
    tmp_path: Path,
) -> None:
    """发现两结构同时存在时必须先清空全部受管腿，绝不继续开仓。"""
    client = _switch_client(
        positions={"XAUS": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[{}, {}],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAUS", "sell", True),
        ("XAUT", "buy", True),
    ]
    assert all(size == 0 for size in client.sizes.values())


def test_switch_kill_switch_priority_flattens_without_reopening(
    tmp_path: Path,
) -> None:
    """kill switch 优先于切换，命中后只平旧结构并结束本轮。"""
    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[{}, {}],
    )

    result = asyncio.run(
        __import__("tools.run_swap_carry_guard", fromlist=["run_once"]).run_once(
            client,
            now=NOW,
            auto_switch=True,
            **paths,
        )
    )

    assert result == 0
    assert _accepted_markets(client) == [
        ("XAU", "sell", True),
        ("XAUT", "buy", True),
    ]


def test_switch_imbalance_priority_flattens_without_reopening(
    tmp_path: Path,
) -> None:
    """缺腿失衡优先于切换，命中后只平剩余腿并结束本轮。"""
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("0")},
        accept_script=[{}],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert _accepted_markets(client) == [("XAU", "sell", True)]


def test_no_auto_switch_keeps_structure_but_preserves_risk(
    tmp_path: Path,
) -> None:
    """关闭切换时健康仓位不动，但 kill switch 仍照常平仓。"""
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=None,
    )

    first = _run(
        client,
        tmp_path,
        structure="XAU_XAUT",
        auto_switch=False,
    )

    assert first == 0
    assert client.accept_calls == []

    paths = _paths(tmp_path)
    paths["kill_switch_path"].write_text("停止\n", encoding="utf-8")
    client.accept_script = [{}, {}]
    second = asyncio.run(
        __import__("tools.run_swap_carry_guard", fromlist=["run_once"]).run_once(
            client,
            now=NOW,
            structure="XAU_XAUT",
            auto_switch=False,
            **paths,
        )
    )

    assert second == 0
    assert _accepted_markets(client) == [
        ("XAU", "sell", True),
        ("XAUT", "buy", True),
    ]


def test_xau_xaut_switch_open_ignores_xaus_preclose_freeze(
    tmp_path: Path,
) -> None:
    """目标结构不含 XAUS 时，即使进入 30 分钟冻结窗也必须能开仓。"""
    client = _switch_client(
        positions={"XAUS": Decimal("0.01"), "XAU": Decimal("-0.01")},
        metadata=_metadata(
            closure_duration=timedelta(hours=49),
            time_until_close=timedelta(minutes=25),
        ),
        accept_script=[{}, {}, {}, {}],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    assert _accepted_markets(client)[-2:] == [
        ("XAU", "buy", False),
        ("XAUT", "sell", False),
    ]


def test_switch_respects_daily_open_attempt_limit(tmp_path: Path) -> None:
    """达到每日开仓上限时不得先平旧结构再陷入无法开仓的空仓。"""
    from tools import run_swap_carry_guard

    paths = _paths(tmp_path)
    paths["state_path"].write_text(
        json.dumps(
            {
                "open_attempt_date": NOW.date().isoformat(),
                "daily_open_attempts": run_swap_carry_guard.MAX_DAILY_OPEN_ATTEMPTS,
            }
        ),
        encoding="utf-8",
    )
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=None,
    )

    result = asyncio.run(
        run_swap_carry_guard.run_once(
            client,
            now=NOW,
            auto_switch=True,
            **paths,
        )
    )

    assert result == 0
    assert client.accept_calls == []
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert "上限" in heartbeat["auto_switch_conclusion"]


def test_auto_switch_cli_and_default_notional_configuration() -> None:
    """切换默认开启且可关闭，自动开仓默认名义跟随配置常量。"""
    from tools import run_swap_carry_guard

    parser = run_swap_carry_guard.build_parser()

    defaults = parser.parse_args(["--once"])
    disabled = parser.parse_args(["--once", "--no-auto-switch"])

    assert defaults.auto_switch is True
    assert disabled.auto_switch is False
    assert defaults.auto_open_notional == run_swap_carry_guard.AUTO_OPEN_NOTIONAL_USD
    assert run_swap_carry_guard.SWITCH_LEAD_TIME == timedelta(minutes=60)


def test_successful_switch_writes_complete_ledger_and_passes_self_check(
    tmp_path: Path,
) -> None:
    """正常切换必须追加单条完整台账，并在新结构上通过立即自检。"""
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[{}, {}, {}, {}],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result == 0
    records = [
        json.loads(line)
        for line in _paths(tmp_path)["switch_history_path"].read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert len([r for r in records if r.get("kind") == "rehearsal"]) == 1
    records = [r for r in records if r.get("kind") != "rehearsal"]
    assert len(records) == 1
    record = records[0]
    assert {
        "schema_version",
        "started_at",
        "completed_at",
        "status",
        "direction",
        "trigger",
        "before",
        "close_phase",
        "flat_confirmation",
        "open_phase",
        "after",
        "measured_wear_usd",
        "total_duration_ms",
        "self_check",
        "failure",
    } <= set(record)
    assert record["status"] == "completed"
    assert record["direction"] == {
        "from": "XAU_XAUT",
        "to": "XAUS_XAU",
    }
    assert record["trigger"]["types"] == ["时段边界", "目标结构变化"]
    assert record["before"]["legs"]["XAU"]["quantity"] == "0.01"
    assert record["before"]["legs"]["XAUT"]["notional_usd"] == "40.00"
    assert record["before"]["net_delta"] == "0.00"
    assert record["before"]["equity"] == "1000"
    assert record["flat_confirmation"]["all_flat"] is True
    assert record["flat_confirmation"]["poll_count"] >= 1
    assert record["flat_confirmation"]["confirmed_at"]
    # 目标数量由默认名义和客户端价格推导，保留台账字符串的精确比较。
    expected_quantity = (
        run_swap_carry_guard.AUTO_OPEN_NOTIONAL_USD
        / Decimal(client.metadata["XAUS"][0]["price"])
    )
    assert record["after"]["legs"]["XAUS"]["quantity"] == str(expected_quantity)
    assert record["after"]["legs"]["XAU"]["quantity"] == str(-expected_quantity)
    assert record["after"]["net_delta"] == str(expected_quantity - expected_quantity)
    assert record["after"]["equity"] == "1000"
    assert record["measured_wear_usd"] == "0"
    assert record["self_check"]["performed"] is True
    assert record["self_check"]["passed"] is True
    assert all(
        check["passed"] for check in record["self_check"]["checks"].values()
    )
    assert record["failure"] is None
    for phase_name in ("close_phase", "open_phase"):
        phase = record[phase_name]
        assert phase["status"] == "completed"
        assert phase["duration_ms"] >= 0
        assert len(phase["legs"]) == 2
        for leg in phase["legs"]:
            assert {
                "market",
                "side",
                "execution_price",
                "quote_mid",
                "rfq_id",
                "filled_quantity",
                "duration_ms",
                "slippage_bp",
            } <= set(leg)
            assert leg["rfq_id"].startswith("rfq-")


def test_switch_close_failure_records_stage_without_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """平仓失败必须形成失败台账，且绝不能出现切换完成时间。"""
    from tools import run_swap_carry_guard

    monkeypatch.setattr(run_swap_carry_guard, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(run_swap_carry_guard, "notify", lambda *_args: True)
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[
            RuntimeError("平仓拒绝 1"),
            RuntimeError("平仓拒绝 2"),
            RuntimeError("平仓拒绝 3"),
        ],
    )

    result = _run(client, tmp_path, auto_switch=True)

    assert result != 0
    record = [json.loads(line) for line in
              _paths(tmp_path)["switch_history_path"].read_text(encoding="utf-8").splitlines()
              if json.loads(line).get("kind") != "rehearsal"][-1]
    assert record["status"] == "failed"
    assert record["failure"]["stage"] == "close"
    assert record["failure"]["error_type"] == "CloseActionError"
    assert "平仓连续失败" in record["failure"]["message"]
    assert record["failure"]["has_residual_position"] is True
    assert record["failure"]["residual_state"] == "残仓"
    assert "completed_at" not in record


def test_post_switch_delta_failure_sets_incident_and_emits_critical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """开仓后净 delta 超容差必须升级事故并持久化锁死标记。"""
    from tools import run_swap_carry_guard

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_swap_carry_guard,
        "notify",
        lambda title, message: notifications.append((title, message)) or True,
    )
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=[{}, {}, {}, {}],
    )
    original_accept = client.accept_quote

    async def accept_with_delta_drift(**kwargs: object) -> dict[str, str]:
        response = await original_accept(**kwargs)
        quote_id = str(kwargs["quote_id"])
        underlying, _side, _qty = client._quotes[quote_id]
        if underlying == "XAU" and kwargs["is_reduce_only"] is False:
            client.sizes["XAU"] -= Decimal("0.001")
        return response

    monkeypatch.setattr(client, "accept_quote", accept_with_delta_drift)

    result = _run(client, tmp_path, auto_switch=True)

    assert result != 0
    state = json.loads(_paths(tmp_path)["state_path"].read_text(encoding="utf-8"))
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    record = [json.loads(line) for line in
              _paths(tmp_path)["switch_history_path"].read_text(encoding="utf-8").splitlines()
              if json.loads(line).get("kind") != "rehearsal"][-1]
    assert state["switch_incident"] is True
    assert heartbeat["switch_incident"] is True
    assert record["status"] == "failed"
    assert record["failure"]["stage"] == "self_check"
    assert record["self_check"]["passed"] is False
    assert record["self_check"]["checks"]["net_delta_within_tolerance"][
        "passed"
    ] is False
    audit = [
        json.loads(line)
        for line in _paths(tmp_path)["audit_path"].read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    critical = next(
        item for item in audit if item["event"] == "auto_switch_self_check_failed"
    )
    assert critical["level"] == "critical"
    assert notifications and "自检失败" in notifications[-1][0]


def test_self_check_rejects_position_outside_target_structure() -> None:
    """目标结构以外的任意非豁免持仓必须令切换自检失败。"""
    from tools import hedge_swap_carry, run_swap_carry_guard

    payload = [
        _position_payload("XAUS", Decimal("0.5"), mark_price=Decimal("4000")),
        _position_payload("XAU", Decimal("-0.5"), mark_price=Decimal("4000")),
        {
            "position_info": {
                "instrument": {
                    "underlying": "ETH",
                    "instrument_type": "perpetual_future",
                    "funding_interval_s": 3600,
                    "settlement_asset": "USDC",
                },
                "qty": "0.25",
                "avg_entry_price": "2500",
            },
            "price_info": {"underlying_price": "2500"},
            "upnl": "0",
        },
    ]

    result = run_swap_carry_guard._switch_self_check(
        hedge_swap_carry.XAUS_XAU,
        account_positions_payload=payload,
        old_structure_was_flat=True,
    )

    assert result["passed"] is False
    residual = result["checks"]["no_external_residual_legs"]
    assert residual["passed"] is False
    assert residual["residual_legs"] == ["ETH"]


def test_switch_incident_blocks_later_auto_open_and_switch(tmp_path: Path) -> None:
    """人工清除前，事故标记必须同时阻止后续自动开仓和结构切换。"""
    from tools import run_swap_carry_guard

    paths = _paths(tmp_path)
    paths["state_path"].write_text(
        json.dumps(
            {
                "open_attempt_date": NOW.date().isoformat(),
                "daily_open_attempts": 0,
                "switch_incident": True,
            }
        ),
        encoding="utf-8",
    )
    switching = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        accept_script=None,
    )

    switch_result = asyncio.run(
        run_swap_carry_guard.run_once(
            switching,
            now=NOW,
            auto_switch=True,
            **paths,
        )
    )

    assert switch_result == 0
    assert switching.accept_calls == []
    assert "switch_incident" in json.loads(
        paths["heartbeat_path"].read_text(encoding="utf-8")
    )["auto_switch_conclusion"]

    flat_dir = tmp_path / "flat"
    flat_dir.mkdir()
    flat_paths = _paths(flat_dir)
    flat_paths["state_path"].write_text(
        json.dumps(
            {
                "open_attempt_date": NOW.date().isoformat(),
                "daily_open_attempts": 0,
                "switch_incident": True,
            }
        ),
        encoding="utf-8",
    )
    flat = _flat_open_client(accept_script=None)

    open_result = asyncio.run(
        run_swap_carry_guard.run_once(flat, now=NOW, **flat_paths)
    )

    assert open_result == 0
    assert flat.accept_calls == []
    assert "switch_incident" in json.loads(
        flat_paths["heartbeat_path"].read_text(encoding="utf-8")
    )["auto_open_conclusion"]


@pytest.mark.parametrize("unreadable", [False, True])
def test_non_target_holding_exit_uses_readability_only(tmp_path, unreadable):
    """复现开市后负 carry 旧结构：读取失败保留计时，不能伪造恢复。"""
    paths = _paths(tmp_path)
    paths["state_path"].write_text(json.dumps({"exit_carry_since": (NOW - timedelta(hours=1)).isoformat()}))
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        perp_rate={"XAU": RuntimeError("读取失败") if unreadable else Decimal("0.1120"),
                   "XAUT": Decimal("0.0036")},
        swap_rate=RuntimeError("目标费率暂不可用"),
    )
    assert _run(client, tmp_path, auto_switch=True) == 0
    state = json.loads(paths["state_path"].read_text())
    assert state["exit_carry_since"] == (NOW - timedelta(hours=1)).isoformat()
    records = [json.loads(line) for line in paths["audit_path"].read_text().splitlines()]
    observed = next(row for row in records if row["event"] == "exit_carry_observed")
    assert ("保留原计时" in observed["message"]) is unreadable
    if unreadable:
        assert "读取失败" in observed["message"]
    else:
        assert Decimal(observed["net_carry_annual"]) == Decimal("-0.1084")


def test_switch_accepts_latest_applied_fallback(tmp_path):
    """真实适配器解析降级响应后，守护应完成先平后开的切换。"""
    from adapters.variational_client import VariationalClient

    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        perp_rate={"XAU": Decimal("0.1120"), "XAUT": Decimal("0.0036")},
        accept_script=[{}, {}, {}, {}],
    )
    adapter = object.__new__(VariationalClient)

    async def offline_get(path):
        assert path == "/funding/swap?underlying=XAUS"
        return {"upcoming": None, "latest_applied": {
            "trade_date": "2026-09-04", "basis": "rates",
            "apply_time": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "long_rate": "-0.057214", "short_rate": "0.025031"}}

    adapter._get = offline_get
    client.get_swap_funding = adapter.get_swap_funding
    assert _run(client, tmp_path, auto_switch=True) == 0
    assert _accepted_markets(client) == [
        ("XAU", "sell", True), ("XAUT", "buy", True),
        ("XAUS", "buy", False), ("XAU", "sell", False)]


def test_non_target_negative_carry_closes_after_duration(tmp_path):
    """非目标结构持续恶化满一天，也必须先熔断且不重开。"""
    _paths(tmp_path)["state_path"].write_text(json.dumps({
        "exit_carry_since": (NOW - timedelta(hours=24)).isoformat()}))
    client = _switch_client(
        positions={"XAU": Decimal("0.01"), "XAUT": Decimal("-0.01")},
        perp_rate={"XAU": Decimal("0.1120"), "XAUT": Decimal("0.0036")},
        swap_rate=RuntimeError("目标费率不可用"), accept_script=[{}, {}])
    assert _run(client, tmp_path, auto_switch=True) == 0
    assert _accepted_markets(client) == [("XAU", "sell", True), ("XAUT", "buy", True)]


@pytest.mark.parametrize('trigger', ['liquidation', 'kill', 'carry', 'none'])
def test_allocation_runs_only_after_all_close_checks(tmp_path, monkeypatch, trigger):
    """用真实单轮编排证明平仓优先，以及成功补仓进入审计和心跳。"""
    from unittest.mock import AsyncMock
    from tools import run_swap_carry_guard as guard
    client = _healthy_client(accept_script=[{},{}])
    client.get_isolated_allocation = AsyncMock(side_effect=[
        {'initial_margin':Decimal('199.44'),'maintenance_margin':Decimal('99.72'),
         'notional':Decimal('1994.36'),'distance':Decimal('.05')},
        {'initial_margin':Decimal('199.44'),'maintenance_margin':Decimal('99.72'),
         'notional':Decimal('1994.36'),'distance':Decimal('.081')},
    ])
    client.set_isolated_allocation = AsyncMock(return_value="3a63f279-test")
    client.wait_allocation_conversion = AsyncMock(return_value="confirmed")
    client.raw = AsyncMock(return_value={'balance':'1000', 'upnl':'0'})
    assert 'available_margin' not in client.raw.return_value
    monkeypatch.setattr(guard,'notify',lambda *_args: True)
    if trigger == 'liquidation':
        client.liquidation_info = {'XAUS':(Decimal('4000'),Decimal('3960'))}
    elif trigger == 'kill':
        _paths(tmp_path)['kill_switch_path'].touch()
    elif trigger == 'carry':
        client.swap_rate = Decimal('-1')
        client.perp_rate = Decimal('0')
        _paths(tmp_path)['state_path'].write_text(json.dumps({'exit_carry_since': (NOW - timedelta(hours=24)).isoformat()}))
    assert _run(client,tmp_path,auto_switch=False,auto_open=False) == 0
    if trigger != 'none':
        client.get_isolated_allocation.assert_not_awaited()
        client.set_isolated_allocation.assert_not_awaited()
        assert len(client.accept_calls) == 2
    else:
        client.set_isolated_allocation.assert_awaited_once_with('XAUS',Decimal('251.30'))
        heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
        assert heartbeat['isolated_allocation']['XAUS']['daily_attempts'] == 1
        assert heartbeat['isolated_allocation']['XAUS']['distance'] == '0.081'
        allocation = heartbeat['isolated_allocation']['XAUS']
        assert allocation['before_allocation'] == '194.4520'
        assert allocation['before_distance'] == '0.05'
        assert allocation['after_allocation'] == '253.18584'
        assert allocation['after_distance'] == '0.081'


def test_funding_recon_runs_in_guard_dry_run(tmp_path, monkeypatch):
    """真实单轮编排每轮只读流水，对账结论进入心跳和审计，dry-run 不 POST。"""
    from tests.test_swap_carry_funding_recon import transfer, sample
    client = _healthy_client()
    original_raw = client.raw
    reads = []
    async def raw(path):
        reads.append(path)
        if path == '/transfers?limit=100&offset=0':
            return {'result':[transfer(8)], 'pagination':{'object_count':1}}
        return await original_raw(path)
    client.raw = raw
    samples = tmp_path/'samples.jsonl'
    samples.write_text(json.dumps(sample(8))+'\n')
    for _ in range(2):
        assert _run(client,tmp_path,dry_run=True, auto_open=False,auto_switch=False,
                    funding_samples_path=samples,funding_recon_path=tmp_path/'recon.jsonl') == 0
    heartbeat = json.loads(_paths(tmp_path)['heartbeat_path'].read_text())
    assert heartbeat['funding_reconciliation']['conclusion'] == '日常计提正常'
    assert heartbeat['funding_reconciliation']['new_count'] == 0
    audit = [json.loads(line) for line in _paths(tmp_path)['audit_path'].read_text().splitlines()]
    records = [row for row in audit if row['event'] == 'funding_reconciliation']
    assert len(records) == 1
    assert records[0]['conclusion'] == '日常计提正常'
    assert reads.count('/transfers?limit=100&offset=0') == 2
    assert client.accept_calls == []


@pytest.mark.parametrize("trigger", ["kill", "single_leg", "liquidation", "account_margin"])
def test_risk_closes_immediately_during_carry_timer_and_cooldown(tmp_path, trigger):
    """最高优先级风控不等待 carry 满一天，也不受重开冷却约束。"""
    paths = _paths(tmp_path)
    paths["state_path"].write_text(json.dumps({
        "exit_carry_since": (NOW - timedelta(minutes=5)).isoformat(),
        "last_closed_at": (NOW - timedelta(minutes=30)).isoformat(),
    }))
    client = _healthy_client(swap_rate=Decimal("-0.12"), perp_rate=Decimal("0"),
                             accept_script=[{}, {}])
    if trigger == "kill":
        paths["kill_switch_path"].touch()
    elif trigger == "single_leg":
        client.sizes["XAU"] = Decimal("0")
    elif trigger == "liquidation":
        client.liquidation_info = (Decimal("4000"), Decimal("3960"))
    else:
        client.equity = Decimal("5")
    assert _run(client, tmp_path) == 0
    assert len(client.accept_calls) == (1 if trigger == "single_leg" else 2)
    assert all(reduce_only for _, _, reduce_only in client.accept_calls)
    assert client.funding_calls == []
    assert json.loads(paths["state_path"].read_text())["last_closed_at"] == NOW.isoformat()


def test_reopen_cooldown_exact_boundary(tmp_path):
    """冷却满两小时即允许开仓。"""
    _paths(tmp_path)["state_path"].write_text(json.dumps({
        "last_closed_at": (NOW - timedelta(hours=2)).isoformat()}))
    client = _flat_open_client(accept_script=[{}, {}])
    assert _run(client, tmp_path) == 0
    assert len(client.accept_calls) == 2


def test_legacy_round_counter_does_not_trigger_duration_exit(tmp_path):
    """旧版高轮数没有时间语义，升级后从首次低 carry 观察重新计时。"""
    _paths(tmp_path)["state_path"].write_text(json.dumps({"exit_carry_consecutive_rounds": 999}))
    client = _healthy_client(swap_rate=Decimal("-0.12"), perp_rate=Decimal("0"))
    assert _run(client, tmp_path) == 0
    assert client.accept_calls == []
    assert json.loads(_paths(tmp_path)["state_path"].read_text())["exit_carry_since"] == NOW.isoformat()


def test_second_leg_skew_rollback_also_starts_cooldown(tmp_path):
    """首腿成交后第二腿偏斜拒单已产生回滚成本，不能下一轮立即重试。"""
    client = _flat_open_client(accept_script=[{}, VariationalRequestError(422, "skew"), {}])
    assert _run(client, tmp_path) == 0
    assert len(client.accept_calls) == 3
    assert json.loads(_paths(tmp_path)["state_path"].read_text())["last_closed_at"] == NOW.isoformat()
    retry = _flat_open_client(accept_script=[{}, {}])
    assert _run(retry, tmp_path, now=NOW + timedelta(hours=1)) == 0
    assert retry.accept_calls == []


@pytest.mark.parametrize("previous_close", [None, NOW - timedelta(hours=3)])
def test_flat_rejection_with_rollback_text_does_not_restart_cooldown(tmp_path, previous_close):
    """错误文本提到已回滚且账户为空，不代表本轮真的发送过平仓。"""
    saved = previous_close.isoformat() if previous_close else None
    state_path = _paths(tmp_path)["state_path"]
    state_path.write_text(json.dumps({"last_closed_at": saved}))
    client = _flat_open_client(accept_script=[RuntimeError("上游提示已回滚，本次首腿拒绝")])
    assert _run(client, tmp_path) == 1
    assert len(client.accept_calls) == 1
    assert not client.accept_calls[0][2]
    assert json.loads(state_path.read_text())["last_closed_at"] == saved


def test_ten_flat_rounds_preserve_actual_close_and_allow_reopen(tmp_path):
    """真实平仓后的十轮空仓不刷新起点，一小时仍冷却，两小时半可开仓。"""
    paths = _paths(tmp_path)
    paths["kill_switch_path"].touch()
    closing = _healthy_client(accept_script=[{}, {}])
    assert _run(closing, tmp_path) == 0
    assert len(closing.accept_calls) == 2
    assert all(reduce_only for _, _, reduce_only in closing.accept_calls)
    assert json.loads(paths["state_path"].read_text())["last_closed_at"] == NOW.isoformat()
    paths["kill_switch_path"].unlink()
    for minute in range(6, 61, 6):
        client = _flat_open_client(accept_script=[])
        assert _run(client, tmp_path, now=NOW + timedelta(minutes=minute)) == 0
        assert client.accept_calls == []
        assert json.loads(paths["heartbeat_path"].read_text())["status"] == "reopen_cooldown"
        assert json.loads(paths["state_path"].read_text())["last_closed_at"] == NOW.isoformat()
    reopening = _flat_open_client(accept_script=[{}, {}])
    assert _run(reopening, tmp_path, now=NOW + timedelta(hours=2.5)) == 0
    assert len(reopening.accept_calls) == 2
    assert json.loads(paths["state_path"].read_text())["last_closed_at"] == NOW.isoformat()


@pytest.mark.parametrize("dry_run", [False, True])
def test_reset_reopen_cooldown_is_local_and_preserves_other_state(tmp_path, monkeypatch, dry_run):
    """重置只清理冷却字段，保留事故和其它状态，且绝不创建交易客户端。"""
    state_path = tmp_path / "state.json"
    original = {"last_closed_at": NOW.isoformat(), "switch_incident": True,
                "auto_open_incident": True, "daily_open_attempts": 4, "custom": {"keep": 1}}
    state_path.write_text(json.dumps(original))
    def forbidden_load():
        raise AssertionError("本地重置不得创建交易客户端")
    monkeypatch.setattr(run_swap_carry_guard.execution, "_load", forbidden_load)
    args = run_swap_carry_guard.build_parser().parse_args([
        "--reset-reopen-cooldown", "--state", str(state_path),
        *(["--dry-run"] if dry_run else []),
    ])
    assert asyncio.run(run_swap_carry_guard._main(args)) == 0
    updated = json.loads(state_path.read_text())
    if dry_run:
        assert updated == original
    else:
        record = updated.pop("reopen_cooldown_reset")
        assert record["previous_last_closed_at"] == NOW.isoformat()
        assert record["timestamp"]
        assert updated == {**original, "last_closed_at": None}


@pytest.mark.parametrize("contents", [None, "{", "[]"])
def test_reset_reopen_cooldown_refuses_missing_or_invalid_state(tmp_path, contents):
    """坏状态不得被重置开关静默覆盖，也不能隐式进入交易轮次。"""
    state_path = tmp_path / "state.json"
    if contents is not None:
        state_path.write_text(contents)
    args = run_swap_carry_guard.build_parser().parse_args([
        "--reset-reopen-cooldown", "--state", str(state_path)])
    with pytest.raises((OSError, ValueError)):
        asyncio.run(run_swap_carry_guard._main(args))
    assert state_path.read_text() == contents if contents is not None else not state_path.exists()


def test_forced_switch_records_real_close_despite_active_cooldown(tmp_path):
    """长休市强制切换绕过冷却，但真实平旧仓仍应更新平仓时间。"""
    paths = _paths(tmp_path)
    paths["state_path"].write_text(json.dumps({
        "last_closed_at": (NOW - timedelta(hours=1)).isoformat()}))
    client = _switch_client(
        positions={"XAUS": Decimal("0.01"), "XAU": Decimal("-0.01")},
        metadata=_metadata(closure_duration=timedelta(hours=49),
                           time_until_close=timedelta(minutes=45)),
        accept_script=[{}, {}, {}, {}],
    )
    assert _run(client, tmp_path, auto_switch=True) == 0
    assert [reduce_only for _, _, reduce_only in client.accept_calls] == [True, True, False, False]
    assert json.loads(paths["state_path"].read_text())["last_closed_at"] == NOW.isoformat()
