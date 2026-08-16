"""Lighter RH 积分对冲 CLI 测试。"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import Position
from engine.hedge_engine import HedgeState


def _cli_module():
    """延迟导入待实现入口，让缺失模块表现为明确的测试失败。"""
    return importlib.import_module("tools.run_lighter_hedge")


def _args(**overrides) -> SimpleNamespace:
    """构造 _main 所需的最小参数集合。"""
    values = {
        "live": False,
        "account": "X10_HEDGE",
        "lighter_address": "0x4A3D...3d82",
        "market": "BTC",
        "hedge_market": "BTC-USD",
        "max_primary_notional": Decimal("2000"),
        "interval": 30.0,
        "rebalance_threshold": Decimal("0.02"),
        "min_hedge_free_margin_ratio": Decimal("0.20"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_rejects_grid_account_before_constructing_clients(monkeypatch) -> None:
    """即使大小写或空白不同，也绝不能让该进程使用网格账户。"""
    cli = _cli_module()

    def fail_constructor(*_args, **_kwargs):
        pytest.fail("危险账户校验必须发生在客户端构造前")

    monkeypatch.setattr(cli, "LighterClient", fail_constructor)
    monkeypatch.setattr(cli.ExtendedClient, "from_env", fail_constructor)

    with pytest.raises(RuntimeError, match="X10_GRID"):
        asyncio.run(cli._main(_args(account=" x10_grid ")))


def test_rejects_equal_hedge_and_grid_vault_ids(monkeypatch) -> None:
    """对冲与网格 vault 相同时必须在构造客户端前拒绝启动。"""
    cli = _cli_module()
    monkeypatch.setenv("X10_HEDGE_VAULT_ID", "9001")
    monkeypatch.setenv("X10_GRID_VAULT_ID", "9001")

    def fail_constructor(*_args, **_kwargs):
        pytest.fail("vault 隔离校验必须发生在客户端构造前")

    monkeypatch.setattr(cli, "LighterClient", fail_constructor)
    monkeypatch.setattr(cli.ExtendedClient, "from_env", fail_constructor)

    with pytest.raises(RuntimeError, match="VAULT_ID.*相同"):
        asyncio.run(cli._main(_args()))


def test_rejects_missing_lighter_address_before_constructing_clients(monkeypatch) -> None:
    """CLI 参数和环境变量都未提供地址时必须明确失败。"""
    cli = _cli_module()

    def fail_constructor(*_args, **_kwargs):
        pytest.fail("地址校验必须发生在客户端构造前")

    monkeypatch.setattr(cli, "LighterClient", fail_constructor)
    monkeypatch.setattr(cli.ExtendedClient, "from_env", fail_constructor)

    with pytest.raises(RuntimeError, match="LIGHTER_RH_L1_ADDRESS"):
        asyncio.run(cli._main(_args(lighter_address=None)))


def test_parser_uses_safe_defaults_and_environment_address(monkeypatch) -> None:
    """默认 dry_run，并从环境变量读取 Lighter 地址。"""
    cli = _cli_module()
    monkeypatch.setenv("LIGHTER_RH_L1_ADDRESS", "0xwallet")

    args = cli._build_parser().parse_args([])

    assert args.live is False
    assert args.account == "X10_HEDGE"
    assert args.lighter_address == "0xwallet"
    assert args.market == "BTC"
    assert args.hedge_market == "BTC-USD"
    assert args.max_primary_notional == Decimal("2000")
    assert args.interval == 30.0
    assert args.rebalance_threshold == Decimal("0.02")
    assert args.min_hedge_free_margin_ratio == Decimal("0.20")


def test_main_assembles_engine_prints_identity_and_closes_clients(
    monkeypatch,
    capsys,
) -> None:
    """启动时打印完整标识，并按只读 primary 配置引擎与回收资源。"""
    cli = _cli_module()
    monkeypatch.setenv("X10_HEDGE_VAULT_ID", "9002")
    monkeypatch.setenv("X10_GRID_VAULT_ID", "9001")
    captured: dict = {}

    class FakeLighter:
        name = "lighter-rh"

        def __init__(self, l1_address: str) -> None:
            captured["lighter_address"] = l1_address
            self.account_index = None
            self.closed = False

        async def connect(self) -> None:
            captured["lighter_connects"] = captured.get("lighter_connects", 0) + 1
            self.account_index = 5626

        async def close(self) -> None:
            self.closed = True

    class FakeExtended:
        name = "extended"

        def __init__(self) -> None:
            self.closed = False

        @classmethod
        def from_env(cls, prefix: str):
            captured["account_prefix"] = prefix
            return cls()

        async def close(self) -> None:
            self.closed = True

    class FakeEngine:
        def __init__(
            self,
            primary,
            hedge,
            config,
            *,
            on_snapshot,
            on_auth_error,
        ) -> None:
            captured["primary"] = primary
            captured["hedge"] = hedge
            captured["config"] = config
            captured["on_snapshot"] = on_snapshot
            captured["on_auth_error"] = on_auth_error

        async def run_forever(self) -> None:
            captured["ran_forever"] = True

    monkeypatch.setattr(cli, "LighterClient", FakeLighter)
    monkeypatch.setattr(cli, "ExtendedClient", FakeExtended)
    monkeypatch.setattr(cli, "HedgeEngine", FakeEngine)

    asyncio.run(
        cli._main(
            _args(
                live=True,
                account="x10_hedge",
                max_primary_notional=Decimal("2500"),
                interval=45.0,
                rebalance_threshold=Decimal("0.03"),
            )
        )
    )

    config = captured["config"]
    assert captured["lighter_address"] == "0x4A3D...3d82"
    assert captured["account_prefix"] == "X10_HEDGE"
    assert captured["lighter_connects"] == 1
    assert captured["ran_forever"] is True
    assert callable(captured["on_snapshot"])
    assert captured["on_auth_error"] is None
    assert config.primary_market == "BTC"
    assert config.market == "BTC-USD"
    assert config.max_primary_notional == Decimal("2500")
    assert config.poll_interval == 45.0
    assert config.rebalance_threshold_ratio == Decimal("0.03")
    assert config.dry_run is False
    assert config.auth_error_types == ()
    assert captured["primary"].closed is True
    assert captured["hedge"].closed is True

    output = capsys.readouterr().out
    assert "Lighter 地址：0x4A3D...3d82" in output
    assert "Lighter account_index：5626" in output
    assert "Extended 账户前缀：X10_HEDGE" in output
    assert "标的：BTC → BTC-USD" in output
    assert "名义上限：2500 USD" in output
    assert "dry_run：False" in output


def test_snapshot_callback_appends_complete_round(monkeypatch, tmp_path) -> None:
    """正常一轮必须写出两腿、净敞口、动作和保证金事实。"""
    cli = _cli_module()
    snapshot_file = tmp_path / "lighter_hedge.jsonl"
    monkeypatch.setattr(cli, "_HEDGE_SNAPSHOT", snapshot_file)
    assert hasattr(cli, "_build_snapshot_callback")
    monkeypatch.setattr(cli.time, "time", lambda: 1_786_000_000.0)

    class FakeHedge:
        async def get_free_margin_ratio(self):
            return Decimal("0.35")

    callback = cli._build_snapshot_callback(
        FakeHedge(),
        interval=30.0,
        rebalance_threshold_ratio=Decimal("0.02"),
        min_hedge_free_margin_ratio=Decimal("0.20"),
    )
    state = HedgeState(
        primary=Position("BTC", Decimal("1")),
        hedge=Position("BTC-USD", Decimal("-0.9")),
        net_delta=Decimal("0.1"),
        action_taken="再平衡 hedge → -1",
        primary_notional_exceeded=True,
    )

    asyncio.run(callback(state))

    row = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert row == {
        "ts": 1_786_000_000.0,
        "interval": 30.0,
        "primary_size": "1",
        "hedge_size": "-0.9",
        "net_delta": "0.1",
        "action": "再平衡 hedge → -1",
        "primary_read_ok": True,
        "hedge_read_ok": True,
        "primary_notional_exceeded": True,
        "rebalance_threshold_ratio": "0.02",
        "hedge_free_margin_ratio": "0.35",
        "min_hedge_free_margin_ratio": "0.20",
        "hedge_margin_error": None,
    }


def test_snapshot_callback_keeps_heartbeat_when_margin_read_fails(
    monkeypatch,
    tmp_path,
) -> None:
    """primary 与保证金读取都失败时仍要写心跳，不能制造监控盲区。"""
    cli = _cli_module()
    snapshot_file = tmp_path / "lighter_hedge.jsonl"
    monkeypatch.setattr(cli, "_HEDGE_SNAPSHOT", snapshot_file)
    assert hasattr(cli, "_build_snapshot_callback")
    monkeypatch.setattr(cli.time, "time", lambda: 1_786_000_030.0)

    class BrokenMarginHedge:
        async def get_free_margin_ratio(self):
            raise RuntimeError("Extended balance timeout")

    callback = cli._build_snapshot_callback(
        BrokenMarginHedge(),
        interval=30.0,
        rebalance_threshold_ratio=Decimal("0.02"),
        min_hedge_free_margin_ratio=Decimal("0.20"),
    )
    state = HedgeState(
        primary=None,
        hedge=Position("BTC-USD", Decimal("-1")),
        action_taken="跳过本轮（primary 读取失败）",
    )

    asyncio.run(callback(state))

    row = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert row["ts"] == 1_786_000_030.0
    assert row["primary_read_ok"] is False
    assert row["hedge_read_ok"] is True
    assert row["primary_size"] is None
    assert row["net_delta"] is None
    assert row["hedge_free_margin_ratio"] is None
    assert "Extended balance timeout" in row["hedge_margin_error"]


def test_cli_uses_dedicated_dated_log_file() -> None:
    """入口日志必须写入 lighter_hedge 专用日期文件。"""
    cli = _cli_module()
    file_handlers = [
        handler
        for handler in cli.logger.handlers
        if isinstance(handler, logging.FileHandler)
    ]

    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename).name == (
        f"lighter_hedge_{date.today():%Y%m%d}.log"
    )
