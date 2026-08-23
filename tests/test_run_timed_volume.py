"""定时定量对冲入口测试；所有测试均离线运行。"""

from __future__ import annotations

import importlib
import asyncio
import json
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest

from timed_volume.strategy import (
    RoundDirection,
    TimedVolumeResult,
    TimedVolumeState,
)


def _cli():
    """延迟导入入口，保留 CLI 功能缺失时的明确 RED。"""
    return importlib.import_module("tools.run_timed_volume")


def test_parser_defaults_match_two_hour_randomized_notional_plan() -> None:
    """入口默认值就是已批准的 2 小时、2000~2300 美元方案。"""
    cli = _cli()

    args = cli.build_parser().parse_args([])
    config = cli.build_config(args)

    assert args.cycle_hours == 2.0
    assert args.notional_min == 2000
    assert args.notional_max == 2300
    assert args.initial_direction == "long"
    assert args.maker_timeout == 300.0
    assert args.primary_venue == "lighter"
    assert args.hedge_env_prefix == "HYPERLIQUID"
    assert config.cycle_seconds == 7200.0
    assert config.notional_min_usd == 2000
    assert config.notional_max_usd == 2300
    assert config.initial_direction is RoundDirection.LONG
    assert config.maker_timeout_s == 300.0


def test_build_primary_client_supports_variational(monkeypatch) -> None:
    """4.1：显式选择 Variational 时用环境会话构造主腿。"""
    cli = _cli()
    session = object()
    captured = {}

    class FakeSession:
        @classmethod
        def from_env(cls):
            return session

    class FakeVariational:
        def __init__(self, received_session):
            captured["session"] = received_session

    monkeypatch.setattr(cli, "Session", FakeSession)
    monkeypatch.setattr(cli, "VariationalClient", FakeVariational)
    args = cli.build_parser().parse_args(["--primary-venue", "variational"])

    client = cli._build_primary_client(args)

    assert isinstance(client, FakeVariational)
    assert captured["session"] is session


def test_build_hyperliquid_hedge_uses_selected_env_prefix(monkeypatch) -> None:
    """4.2：Hyperliquid 对冲腿必须读取显式的第二账户前缀。"""
    cli = _cli()
    captured = {}

    class FakeHyperliquid:
        @classmethod
        def from_env(cls, prefix, *, trading_enabled):
            captured["prefix"] = prefix
            captured["trading_enabled"] = trading_enabled
            return object()

    monkeypatch.setattr(cli, "HyperliquidClient", FakeHyperliquid)
    args = cli.build_parser().parse_args(
        [
            "--hedge-venue",
            "hyperliquid",
            "--hedge-env-prefix",
            "HYPERLIQUID_VAR",
        ]
    )

    cli._build_hedge_client(args)

    assert captured == {
        "prefix": "HYPERLIQUID_VAR",
        "trading_enabled": True,
    }


def test_same_venue_account_and_market_are_rejected() -> None:
    """4.3：同平台、同账户、同市场的双腿配置必须失败关闭。"""
    cli = _cli()

    with pytest.raises(RuntimeError, match="同一交易所账户.*同一市场"):
        cli._validate_account_pair(
            primary_venue="hyperliquid",
            primary_account="0xABC",
            primary_market="BTC",
            hedge_venue="hyperliquid",
            hedge_account="0xabc",
            hedge_market="btc",
        )


def test_cross_venue_same_account_text_is_allowed() -> None:
    """4.3：跨平台账户文本相同不代表共用交易账户。"""
    cli = _cli()

    cli._validate_account_pair(
        primary_venue="variational",
        primary_account="0xabc",
        primary_market="BTC",
        hedge_venue="hyperliquid",
        hedge_account="0xabc",
        hedge_market="BTC",
    )


def test_running_process_state_lock_rejects_second_instance(tmp_path) -> None:
    """4.4：同一状态文件被存活进程占用时，第二实例必须拒绝启动。"""
    cli = _cli()
    state_path = tmp_path / "state.json"
    first = cli._acquire_state_path_lease(state_path)
    try:
        with pytest.raises(RuntimeError, match="状态文件.*正在被 PID"):
            cli._acquire_state_path_lease(state_path)
    finally:
        first.release()

    second = cli._acquire_state_path_lease(state_path)
    second.release()


def test_stale_state_lock_is_reclaimed(tmp_path) -> None:
    """4.4：已退出进程遗留的锁不得永久阻塞状态文件。"""
    cli = _cli()
    state_path = tmp_path / "state.json"
    lock_path = cli._state_lock_path(state_path)
    lock_path.write_text(
        json.dumps({"pid": 99999999, "started_at": 1.0}),
        encoding="utf-8",
    )

    lease = cli._acquire_state_path_lease(state_path)

    assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] != 99999999
    lease.release()


def test_dry_run_summary_masks_accounts_and_prints_instance_boundaries(
    monkeypatch,
    tmp_path,
) -> None:
    """4.5：离线摘要打印两腿、脱敏账户、状态路径、金额与周期。"""
    cli = _cli()
    monkeypatch.setenv("VARIATIONAL_WALLET_ADDRESS", "0x1234567890abcdef")
    monkeypatch.setenv(
        "HYPERLIQUID_VAR_ACCOUNT_ADDRESS",
        "0xfedcba0987654321",
    )
    state_path = tmp_path / "var-state.json"
    args = cli.build_parser().parse_args(
        [
            "--primary-venue",
            "variational",
            "--hedge-venue",
            "hyperliquid",
            "--hedge-env-prefix",
            "HYPERLIQUID_VAR",
            "--state-path",
            str(state_path),
            "--notional-min",
            "2100",
            "--notional-max",
            "2200",
            "--cycle-hours",
            "3",
        ]
    )

    summary = cli.startup_summary(args, TimedVolumeState())

    assert "主腿：variational，账户：0x12…cdef" in summary
    assert "对冲腿：hyperliquid，账户：0xfe…4321" in summary
    assert f"轮次状态：{state_path}" in summary
    assert "单边名义额区间：2100~2200 USD" in summary
    assert "周期：3 小时" in summary


def test_startup_summary_contains_parameters_and_current_round() -> None:
    """启动摘要必须同时暴露配置与恢复出的当前轮次。"""
    cli = _cli()
    args = cli.build_parser().parse_args(
        [
            "--cycle-hours",
            "2",
            "--notional-min",
            "2050",
            "--notional-max",
            "2250",
            "--initial-direction",
            "short",
            "--maker-timeout",
            "420",
        ]
    )
    state = TimedVolumeState(
        round_index=5,
        last_direction=RoundDirection.LONG,
        current_direction=RoundDirection.SHORT,
        current_notional_usd=2179,
        opened_at=1000.0,
        due_at=8200.0,
    )

    summary = cli.startup_summary(args, state)

    assert "周期：2 小时" in summary
    assert "单边名义额区间：2050~2250 USD" in summary
    assert "当前轮名义额：2179 USD" in summary
    assert "初始方向：short" in summary
    assert "maker 优先等待：420 秒" in summary
    assert "当前轮次：5" in summary
    assert "当前方向：short" in summary
    assert "到期时刻：8200.0" in summary


def test_heartbeat_contains_round_direction_due_net_and_interlock() -> None:
    """独立心跳必须完整覆盖规范要求的调度与安全状态。"""
    cli = _cli()
    result = TimedVolumeResult(
        action="wait",
        round_index=3,
        direction=RoundDirection.LONG,
        due_at=9000.0,
        primary_size=Decimal("0.5"),
        hedge_size=Decimal("-0.49"),
        net_exposure=Decimal("0.01"),
        hedge_available=False,
        interlock_reason="Extended 侧不可用",
        notional_usd=2179,
        warnings=("测试告警",),
        primary_pnl=Decimal("4.330000000000000001"),
        hedge_pnl=Decimal("-5.570000000000000002"),
        primary_entry=Decimal("77299.300000000000000001"),
        hedge_entry=Decimal("77301.1"),
        pair_pnl=Decimal("-1.240000000000000001"),
    )

    payload = cli.heartbeat_payload(result, now=1234.5)

    assert payload == {
        "ts": 1234.5,
        "action": "wait",
        "round_index": 3,
        "direction": "long",
        "due_at": 9000.0,
        "primary_size": "0.5",
        "hedge_size": "-0.49",
        "net_exposure": "0.01",
        "primary_pnl": "4.330000000000000001",
        "hedge_pnl": "-5.570000000000000002",
        "primary_entry": "77299.300000000000000001",
        "hedge_entry": "77301.1",
        "pair_pnl": "-1.240000000000000001",
        "hedge_available": False,
        "hedge_interlock_active": True,
        "hedge_interlock_reason": "Extended 侧不可用",
        "notional_usd": 2179,
        "warnings": ["测试告警"],
    }


def test_heartbeat_appends_independent_jsonl_file(tmp_path) -> None:
    """每个节拍追加一行，不覆盖上一轮心跳。"""
    cli = _cli()
    path = tmp_path / "timed_volume.jsonl"

    cli.append_heartbeat({"ts": 1.0, "round_index": 1}, path)
    cli.append_heartbeat({"ts": 2.0, "round_index": 2}, path)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {"ts": 1.0, "round_index": 1},
        {"ts": 2.0, "round_index": 2},
    ]


def test_run_loop_starts_next_round_without_sleep_after_close(
    monkeypatch,
    tmp_path,
) -> None:
    """到期平仓后的下一节拍不得等待 poll interval，须立即开始下一轮。"""
    cli = _cli()
    results = [
        TimedVolumeResult(
            action="closed",
            round_index=1,
            direction=None,
            due_at=None,
            primary_size=Decimal("0"),
            hedge_size=Decimal("0"),
            net_exposure=Decimal("0"),
            hedge_available=True,
            interlock_reason="Extended 侧可用",
        ),
        TimedVolumeResult(
            action="opened",
            round_index=2,
            direction=RoundDirection.SHORT,
            due_at=8200.0,
            primary_size=Decimal("-20"),
            hedge_size=Decimal("20"),
            net_exposure=Decimal("0"),
            hedge_available=True,
            interlock_reason="Extended 侧可用",
        ),
    ]

    class FakeStrategy:
        config = SimpleNamespace(position_tolerance=Decimal("0.000001"))

        async def run_once(self):
            return results.pop(0)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    iterations = 0

    def stop_requested():
        nonlocal iterations
        iterations += 1
        return iterations > 2

    heartbeat_path = tmp_path / "heartbeat.jsonl"
    asyncio.run(
        cli.run_loop(
            FakeStrategy(),
            poll_interval=30.0,
            heartbeat_path=heartbeat_path,
            stop_requested=stop_requested,
        )
    )

    rows = heartbeat_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
    assert [json.loads(row)["action"] for row in rows] == ["closed", "opened"]
    assert sleeps == [30.0]


def test_non_live_run_prints_summary_without_constructing_clients(
    monkeypatch,
    capsys,
) -> None:
    """默认运行仅输出离线摘要，不能连接或构造交易客户端。"""
    cli = _cli()

    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("离线摘要不得构造客户端")

    monkeypatch.setattr(cli, "LighterClient", ForbiddenClient)
    monkeypatch.setattr(cli, "ExtendedClient", ForbiddenClient)
    args = cli.build_parser().parse_args([])

    asyncio.run(cli.run(args))

    output = capsys.readouterr().out
    assert "定时定量对冲配置" in output
    assert "dry_run：True" in output


def test_live_run_connects_both_clients_and_closes_them(monkeypatch, tmp_path) -> None:
    """live 装配必须让 Lighter 可交易、连接两腿，并在退出时关闭两腿。"""
    cli = _cli()
    captured = {}

    class FakeLighter:
        def __init__(self, **kwargs):
            captured["lighter_kwargs"] = kwargs
            self.closed = False
            self.connected = False

        async def connect(self):
            self.connected = True

        async def close(self):
            self.closed = True

    class FakeExtended:
        def __init__(self):
            self.closed = False
            self.connected = False

        @classmethod
        def from_env(cls, prefix):
            captured["account"] = prefix
            instance = cls()
            captured["extended"] = instance
            return instance

        async def connect(self):
            self.connected = True

        async def close(self):
            self.closed = True

    class FakeStrategy:
        def __init__(self, primary, hedge, config, **kwargs):
            captured["primary"] = primary
            captured["hedge"] = hedge
            captured["config"] = config
            captured["strategy_kwargs"] = kwargs
            self.state = TimedVolumeState()

    async def fake_run_loop(strategy, **kwargs):
        captured["loop_strategy"] = strategy
        captured["loop_kwargs"] = kwargs

    monkeypatch.setattr(cli, "LighterClient", FakeLighter)
    monkeypatch.setattr(cli, "ExtendedClient", FakeExtended)
    monkeypatch.setattr(cli, "TimedHedgedVolumeStrategy", FakeStrategy)
    monkeypatch.setattr(cli, "run_loop", fake_run_loop)
    monkeypatch.setenv("LIGHTER_API_PRIVATE_KEY", "test-key")
    args = cli.build_parser().parse_args(
        [
            "--live",
            "--lighter-address",
            "0xtest",
            "--state-path",
            str(tmp_path / "state.json"),
            "--heartbeat-path",
            str(tmp_path / "heartbeat.jsonl"),
        ]
    )

    asyncio.run(cli.run(args))

    assert captured["lighter_kwargs"]["trading_enabled"] is True
    assert captured["lighter_kwargs"]["api_private_key"] == "test-key"
    assert captured["primary"].connected is True
    assert captured["extended"].connected is True
    assert captured["primary"].closed is True
    assert captured["extended"].closed is True
    assert captured["loop_kwargs"]["poll_interval"] == 30.0
    assert captured["strategy_kwargs"] == {}


def test_variational_live_run_wires_auth_reload_and_avoids_lighter_credentials(
    monkeypatch,
    tmp_path,
) -> None:
    """3.5/4.1：Variational 实盘装配认证自愈，且不依赖 Lighter 密钥。"""
    cli = _cli()
    captured = {"dotenv_overrides": []}

    class FakeSession:
        calls = 0

        @classmethod
        def from_env(cls):
            cls.calls += 1
            return SimpleNamespace(wallet_address="0xvariational")

    class FakeVariational:
        def __init__(self, session):
            self.session = session
            self.connected = False
            self.closed = False

        async def connect(self):
            self.connected = True

        async def close(self):
            self.closed = True

    class FakeHyperliquid:
        def __init__(self):
            self.connected = False
            self.closed = False

        @classmethod
        def from_env(cls, prefix, *, trading_enabled):
            captured["hedge_prefix"] = prefix
            captured["hedge_trading_enabled"] = trading_enabled
            return cls()

        async def connect(self):
            self.connected = True

        async def close(self):
            self.closed = True

    class FakeStrategy:
        def __init__(self, primary, hedge, config, **kwargs):
            captured["strategy_primary"] = primary
            captured["strategy_hedge"] = hedge
            captured["strategy_kwargs"] = kwargs
            self.state = TimedVolumeState()

    async def fake_run_loop(strategy, **kwargs):
        captured["loop_strategy"] = strategy
        captured["loop_kwargs"] = kwargs

    fake_dotenv = SimpleNamespace(
        load_dotenv=lambda *, override=False: captured["dotenv_overrides"].append(
            override
        )
    )
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setattr(cli, "Session", FakeSession)
    monkeypatch.setattr(cli, "VariationalClient", FakeVariational)
    monkeypatch.setattr(cli, "HyperliquidClient", FakeHyperliquid)
    monkeypatch.setattr(cli, "TimedHedgedVolumeStrategy", FakeStrategy)
    monkeypatch.setattr(cli, "run_loop", fake_run_loop)
    monkeypatch.delenv("LIGHTER_API_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("VARIATIONAL_WALLET_ADDRESS", "0xvariational")
    monkeypatch.setenv("HYPERLIQUID_VAR_ACCOUNT_ADDRESS", "0xhyperliquid")
    args = cli.build_parser().parse_args(
        [
            "--live",
            "--primary-venue",
            "variational",
            "--hedge-venue",
            "hyperliquid",
            "--hedge-env-prefix",
            "HYPERLIQUID_VAR",
            "--state-path",
            str(tmp_path / "state.json"),
            "--heartbeat-path",
            str(tmp_path / "heartbeat.jsonl"),
        ]
    )

    asyncio.run(cli.run(args))

    kwargs = captured["strategy_kwargs"]
    assert kwargs["auth_error_types"] == (cli.VariationalAuthError,)
    replacement = kwargs["on_auth_error"]()
    assert isinstance(replacement, FakeVariational)
    assert captured["dotenv_overrides"] == [True]
    assert FakeSession.calls == 2
    assert captured["hedge_prefix"] == "HYPERLIQUID_VAR"
    assert captured["strategy_primary"].connected is True
    assert captured["strategy_hedge"].connected is True


def test_run_loop_uses_emergency_retry_for_unknown_or_naked_state(
    monkeypatch,
    tmp_path,
) -> None:
    """未知成交状态与非零净敞口不得进入普通 30 秒休眠。"""
    cli = _cli()
    results = [
        TimedVolumeResult(
            action="execution_uncertain",
            round_index=0,
            direction=None,
            due_at=None,
            primary_size=None,
            hedge_size=None,
            net_exposure=None,
            hedge_available=False,
            interlock_reason="下单后持仓暂不可读",
        ),
        TimedVolumeResult(
            action="convergence_failed",
            round_index=0,
            direction=None,
            due_at=None,
            primary_size=Decimal("1"),
            hedge_size=Decimal("0"),
            net_exposure=Decimal("1"),
            hedge_available=False,
            interlock_reason="正在紧急收敛",
        ),
    ]

    class FakeStrategy:
        config = SimpleNamespace(position_tolerance=Decimal("0.000001"))

        async def run_once(self):
            return results.pop(0)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    iterations = 0

    def stop_requested():
        nonlocal iterations
        iterations += 1
        return iterations > 2

    asyncio.run(
        cli.run_loop(
            FakeStrategy(),
            poll_interval=30.0,
            heartbeat_path=tmp_path / "heartbeat.jsonl",
            stop_requested=stop_requested,
        )
    )

    assert sleeps == [1.0, 1.0]


def test_heartbeat_write_failure_does_not_stop_safety_convergence(
    monkeypatch,
    tmp_path,
) -> None:
    """心跳落盘失败只能告警，不能中断未知成交状态的后续收敛。"""
    cli = _cli()
    results = [
        TimedVolumeResult(
            action="execution_uncertain",
            round_index=0,
            direction=None,
            due_at=None,
            primary_size=None,
            hedge_size=None,
            net_exposure=None,
            hedge_available=False,
            interlock_reason="成交状态未知",
        ),
        TimedVolumeResult(
            action="reconciled",
            round_index=0,
            direction=None,
            due_at=None,
            primary_size=Decimal("0"),
            hedge_size=Decimal("0"),
            net_exposure=Decimal("0"),
            hedge_available=True,
            interlock_reason="已恢复",
        ),
    ]

    class FakeStrategy:
        config = SimpleNamespace(position_tolerance=Decimal("0.000001"))
        calls = 0

        async def run_once(self):
            self.calls += 1
            return results.pop(0)

    writes = 0

    def flaky_append(payload, path):
        nonlocal writes
        del payload, path
        writes += 1
        if writes == 1:
            raise OSError("磁盘暂不可写")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(cli, "append_heartbeat", flaky_append)
    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    iterations = 0

    def stop_requested():
        nonlocal iterations
        iterations += 1
        return iterations > 2

    strategy = FakeStrategy()
    asyncio.run(
        cli.run_loop(
            strategy,
            poll_interval=30.0,
            heartbeat_path=tmp_path / "heartbeat.jsonl",
            stop_requested=stop_requested,
        )
    )

    assert strategy.calls == 2
    assert writes == 2


def test_tolerance_metadata_interlock_uses_normal_poll_interval(
    monkeypatch,
    tmp_path,
) -> None:
    """容差查询失败已明确互锁时，不得因固定旧容差退化成每秒死循环。"""
    cli = _cli()

    class FakeStrategy:
        config = SimpleNamespace(position_tolerance=Decimal("0.000001"))
        hedge_tolerance = None

        async def run_once(self):
            return TimedVolumeResult(
                action="interlocked",
                round_index=0,
                direction=None,
                due_at=None,
                primary_size=Decimal("0.00129"),
                hedge_size=Decimal("-0.00128"),
                net_exposure=Decimal("0.00001"),
                hedge_available=False,
                interlock_reason="对冲容差查询失败",
            )

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    iterations = 0

    def stop_requested():
        nonlocal iterations
        iterations += 1
        return iterations > 1

    asyncio.run(
        cli.run_loop(
            FakeStrategy(),
            poll_interval=30.0,
            heartbeat_path=tmp_path / "heartbeat.jsonl",
            stop_requested=stop_requested,
        )
    )

    assert sleeps == [30.0]
