"""Swap carry 无人值守风控守护进程测试；全部离线且假客户端默认抛错。"""

from __future__ import annotations

import asyncio
import json
import plistlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import Position
from adapters.variational_client import (
    VariationalAuthError,
    VariationalJurisdictionError,
    VariationalRequestError,
)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


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
                "instrument_type": "swap",
                "funding_interval_s": 0,
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
                "instrument_type": "perpetual_rwa_future",
                "funding_interval_s": 3600,
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


class StrictGuardClient:
    """只允许测试显式配置的调用；遗漏编排时立即失败。"""

    def __init__(
        self,
        *,
        positions: dict[str, Decimal],
        metadata: object,
        liquidation_info: object = None,
        accept_script: list[object] | None = None,
        quote_enabled: bool = True,
        apply_accept: bool = True,
        swap_rate: object = None,
        perp_rate: object = None,
        equity: Decimal | None = None,
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
        self.quote_calls: list[tuple[str, str, Decimal]] = []
        self.accept_calls: list[tuple[str, str, bool]] = []
        self.position_calls: list[tuple[str, bool]] = []
        self.funding_calls: list[tuple[str, str | None]] = []
        self._quotes: dict[str, tuple[str, str, Decimal]] = {}
        self._quote_index = 0
        self._max_slippage = 0.01

    async def get_position(self, underlying: str, *, exact: bool = False) -> Position:
        self.position_calls.append((underlying, exact))
        assert exact is True
        if underlying not in self.sizes:
            raise AssertionError(f"未配置仓位：{underlying}")
        return Position(underlying, self.sizes[underlying])

    async def get_supported_assets(self) -> object:
        if self.metadata is None:
            raise AssertionError("未配置调用：get_supported_assets")
        return self.metadata

    async def get_liquidation_info(
        self, underlying: str, *, exact: bool = False
    ) -> object:
        assert (underlying, exact) == ("XAUS", True)
        if isinstance(self.liquidation_info, BaseException):
            raise self.liquidation_info
        if self.liquidation_info is None:
            return None
        return self.liquidation_info

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
            "margin_requirements": _margin_requirements(
                qty=qty,
                isolated=underlying == "XAUS",
            ),
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
        if isinstance(self.perp_rate, BaseException):
            raise self.perp_rate
        return self.perp_rate

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
    }


def _run(client: StrictGuardClient, tmp_path: Path, **kwargs: object) -> int:
    """执行一轮并固定观察时点。"""
    from tools import run_swap_carry_guard

    return asyncio.run(
        run_swap_carry_guard.run_once(
            client,
            now=NOW,
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
    liquidation_info: object = (Decimal("4000"), Decimal("3900")),
    accept_script: list[object] | None = None,
) -> StrictGuardClient:
    """构造默认安全且配平的守护客户端。"""
    return StrictGuardClient(
        positions=positions
        or {"XAUS": Decimal("0.01"), "XAU": Decimal("-0.01")},
        metadata=metadata if metadata is not None else _metadata(),
        liquidation_info=liquidation_info,
        accept_script=accept_script,
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
    """强平价拿不到时必须 fail-closed，不得假设安全。"""
    client = _healthy_client(liquidation_info=None, accept_script=[{}, {}])

    result = _run(client, tmp_path)

    assert result == 0
    assert len(client.accept_calls) == 2
    heartbeat = json.loads(
        _paths(tmp_path)["heartbeat_path"].read_text(encoding="utf-8")
    )
    assert "强平" in heartbeat["conclusion"]


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


def test_launchd_plist_runs_guard_once_every_five_minutes() -> None:
    """部署文件只声明每五分钟单轮执行，不在测试中加载 launchd。"""
    path = Path("deploy/com.variational.swap-carry-guard.plist")
    payload = plistlib.loads(path.read_bytes())

    assert payload["StartInterval"] == 300
    assert "tools.run_swap_carry_guard" in payload["ProgramArguments"]
    assert "--once" in payload["ProgramArguments"]


def test_auto_open_skips_when_net_carry_is_below_threshold(tmp_path: Path) -> None:
    """净 carry 低于 5% 时不得询价或开仓。"""
    client = _flat_open_client(
        swap_rate=Decimal("-0.04"),
        perp_rate=Decimal("0.0899"),
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
    assert "读取失败" in heartbeat["auto_open_conclusion"]


def test_auto_open_skips_when_xaus_is_not_tradable(tmp_path: Path) -> None:
    """XAUS 不可交易时不得读取 carry、询价或开仓。"""
    client = _flat_open_client(metadata=_metadata(market_status="closed"))

    result = _run(client, tmp_path)

    assert result == 0
    assert client.funding_calls == []
    assert client.quote_calls == []
    assert client.accept_calls == []


def test_auto_open_skips_with_two_hours_or_less_to_close(tmp_path: Path) -> None:
    """距休市不超过两小时时不得开仓。"""
    client = _flat_open_client(
        metadata=_metadata(time_until_close=timedelta(hours=2))
    )

    result = _run(client, tmp_path)

    assert result == 0
    assert client.funding_calls == []
    assert client.quote_calls == []
    assert client.accept_calls == []


def test_auto_open_never_runs_while_either_leg_exists(tmp_path: Path) -> None:
    """已有任一目标腿时只执行既有风控，不进入自动开仓判定。"""
    client = _healthy_client(accept_script=None)

    result = _run(client, tmp_path)

    assert result == 0
    assert client.funding_calls == []
    assert client.accept_calls == []


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
    """同一 UTC 日达到尝试上限后不得继续读取 carry 或询价。"""
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
    assert client.funding_calls == []
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
    assert len(client.funding_calls) == funding_count
    heartbeat = json.loads(paths["heartbeat_path"].read_text(encoding="utf-8"))
    assert "INCIDENT" in heartbeat["auto_open_conclusion"]


def test_auto_open_rejects_notional_over_execution_hard_cap(tmp_path: Path) -> None:
    """自动名义超过人工执行器硬上限时不得读取 carry、询价或 accept。"""
    from tools import hedge_swap_carry

    client = _flat_open_client(accept_script=[])

    result = _run(
        client,
        tmp_path,
        auto_open_notional=hedge_swap_carry.MAX_NOTIONAL_USD + Decimal("0.01"),
    )

    assert result == 0
    assert client.funding_calls == []
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

    monkeypatch.setenv("AUTO_OPEN_NOTIONAL_USD", "2000")
    parser = run_swap_carry_guard.build_parser()

    env_args = parser.parse_args(["--once", "--no-auto-open"])
    cli_args = parser.parse_args(["--once", "--auto-open-notional", "750"])

    assert env_args.auto_open is False
    assert env_args.auto_open_notional == Decimal("2000")
    assert cli_args.auto_open is True
    assert cli_args.auto_open_notional == Decimal("750")
