"""Swap carry 无人值守风控守护进程测试；全部离线且假客户端默认抛错。"""

from __future__ import annotations

import asyncio
import json
import plistlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from adapters.base import Position
from adapters.variational_client import (
    VariationalAuthError,
    VariationalJurisdictionError,
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
    ) -> None:
        self.sizes = dict(positions)
        self.metadata = metadata
        self.liquidation_info = liquidation_info
        self.accept_script = (
            list(accept_script) if accept_script is not None else None
        )
        self.quote_enabled = quote_enabled
        self.apply_accept = apply_accept
        self.quote_calls: list[tuple[str, str, Decimal]] = []
        self.accept_calls: list[tuple[str, str, bool]] = []
        self.position_calls: list[tuple[str, bool]] = []
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
        }

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
        assert is_reduce_only is True
        current = self.sizes[underlying]
        if self.apply_accept:
            if current > 0 and side == "sell":
                self.sizes[underlying] = max(Decimal("0"), current - qty)
            elif current < 0 and side == "buy":
                self.sizes[underlying] = min(Decimal("0"), current + qty)
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


def test_kill_switch_flattens_both_legs_reduce_only(tmp_path: Path) -> None:
    """kill switch 命中后必须平两腿，且守护进程没有任何开仓入口。"""
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
