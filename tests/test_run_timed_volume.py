"""定时定量对冲入口测试；所有测试均离线运行。"""

from __future__ import annotations

import importlib
import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

from timed_volume.strategy import (
    RoundDirection,
    TimedVolumeResult,
    TimedVolumeState,
)


def _cli():
    """延迟导入入口，保留 CLI 功能缺失时的明确 RED。"""
    return importlib.import_module("tools.run_timed_volume")


def test_parser_defaults_match_two_hour_two_thousand_plan() -> None:
    """入口默认值就是已批准的 2 小时、2000 美元、首轮做多方案。"""
    cli = _cli()

    args = cli.build_parser().parse_args([])
    config = cli.build_config(args)

    assert args.cycle_hours == 2.0
    assert args.notional == Decimal("2000")
    assert args.initial_direction == "long"
    assert args.maker_timeout == 300.0
    assert config.cycle_seconds == 7200.0
    assert config.notional_usd == Decimal("2000")
    assert config.initial_direction is RoundDirection.LONG
    assert config.maker_timeout_s == 300.0


def test_startup_summary_contains_parameters_and_current_round() -> None:
    """启动摘要必须同时暴露配置与恢复出的当前轮次。"""
    cli = _cli()
    args = cli.build_parser().parse_args(
        [
            "--cycle-hours",
            "2",
            "--notional",
            "2000",
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
        opened_at=1000.0,
        due_at=8200.0,
    )

    summary = cli.startup_summary(args, state)

    assert "周期：2 小时" in summary
    assert "单边名义额：2000 USD" in summary
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
        warnings=("测试告警",),
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
        "hedge_available": False,
        "hedge_interlock_active": True,
        "hedge_interlock_reason": "Extended 侧不可用",
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
        def __init__(self, primary, hedge, config):
            captured["primary"] = primary
            captured["hedge"] = hedge
            captured["config"] = config
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
