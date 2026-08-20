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
        "allow_shared_account": False,
        "account": "X10_HEDGE",
        "lighter_address": "0x4A3D...3d82",
        "market": "BTC",
        "hedge_market": "BTC-USD",
        "max_primary_notional": Decimal("2000"),
        "interval": 30.0,
        "rebalance_threshold": Decimal("0.02"),
        "min_hedge_free_margin_ratio": Decimal("0.20"),
        "maker_first_timeout": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _SharedAccountHedge:
    """复刻真实适配器只读契约，并覆盖共用账户的成功与失败状态。"""

    def __init__(
        self,
        *,
        signed_size: Decimal = Decimal("0"),
        positions: list[Position] | None = None,
        open_orders: list[object] | None = None,
    ) -> None:
        self.signed_size = signed_size
        self.positions = (
            list(positions)
            if positions is not None
            else (
                []
                if signed_size == 0
                else [Position("BTC-USD", signed_size)]
            )
        )
        self.open_orders = list(open_orders or [])
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def get_position(self, market: str) -> Position:
        return next(
            (position for position in self.positions if position.market == market),
            Position(market, Decimal("0")),
        )

    async def get_all_positions(self) -> list[Position]:
        self.calls.append(("positions", "全部"))
        return self.positions

    async def get_open_orders(self, market: str) -> list[object]:
        return [
            order
            for order in self.open_orders
            if getattr(order, "market", market) == market
        ]

    async def get_all_open_orders(self) -> list[object]:
        self.calls.append(("open_orders", "全部"))
        return self.open_orders

    async def market_order(self, *_args, **_kwargs):
        return None

    async def get_liquidation_info(self, _market):
        return None

    async def get_free_margin_ratio(self):
        return Decimal("1")

    async def get_balance(self):
        return SimpleNamespace(equity=Decimal("1000"))

    async def close(self) -> None:
        self.closed = True


def _patch_shared_account_clients(monkeypatch, cli, hedge: _SharedAccountHedge) -> None:
    """隔离外部网络，同时保留入口真实的构造与关闭流程。"""

    class FakeLighter:
        def __init__(self, l1_address: str) -> None:
            self.l1_address = l1_address
            self.account_index = None
            self.closed = False

        async def connect(self) -> None:
            self.account_index = 5626

        async def get_position(self, market: str) -> Position:
            return Position(market, Decimal("0"))

        async def get_market_price(self, market: str):
            return SimpleNamespace(market=market, mid=Decimal("60000"))

        async def get_liquidation_info(self, _market):
            return None

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(cli, "LighterClient", FakeLighter)
    monkeypatch.setattr(
        cli,
        "ExtendedClient",
        SimpleNamespace(from_env=lambda prefix: hedge),
    )


def test_rejects_grid_account_before_constructing_clients(monkeypatch) -> None:
    """即使大小写或空白不同，也绝不能让该进程使用网格账户。"""
    cli = _cli_module()

    def fail_constructor(*_args, **_kwargs):
        pytest.fail("危险账户校验必须发生在客户端构造前")

    monkeypatch.setattr(cli, "LighterClient", fail_constructor)
    monkeypatch.setattr(cli.ExtendedClient, "from_env", fail_constructor)

    with pytest.raises(RuntimeError, match="X10_GRID"):
        asyncio.run(
            cli._main(_args(account=" x10_grid ", allow_shared_account=False))
        )


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
        asyncio.run(cli._main(_args(allow_shared_account=False)))


def test_rejects_selected_account_equal_to_grid_vault_without_declaration(
    monkeypatch,
) -> None:
    """未声明时，所选账户 vault 与网格相同仍必须在构造客户端前拒绝。"""
    cli = _cli_module()
    monkeypatch.setenv("X10_HEDGE_VAULT_ID", "9002")
    monkeypatch.setenv("X10_GRID_VAULT_ID", "9001")
    monkeypatch.setenv("X10_VAULT_ID", "9001")

    def fail_constructor(*_args, **_kwargs):
        pytest.fail("所选账户 vault 隔离校验必须发生在客户端构造前")

    monkeypatch.setattr(cli, "LighterClient", fail_constructor)
    monkeypatch.setattr(cli.ExtendedClient, "from_env", fail_constructor)

    with pytest.raises(RuntimeError, match="X10_VAULT_ID.*X10_GRID_VAULT_ID.*相同"):
        asyncio.run(
            cli._main(_args(account="X10", allow_shared_account=False))
        )


def test_declaration_skips_all_static_account_rejections(monkeypatch) -> None:
    """声明共用后，账户前缀和两类 vault 相同都只进入运行时校验。"""
    cli = _cli_module()
    monkeypatch.setenv("X10_HEDGE_VAULT_ID", "9001")
    monkeypatch.setenv("X10_GRID_VAULT_ID", "9001")

    result = cli._validate_startup(
        _args(account=" x10_grid ", allow_shared_account=True)
    )

    assert result == ("X10_GRID", "0x4A3D...3d82")


def test_shared_account_with_position_is_rejected(monkeypatch) -> None:
    """即使其他标的仍有仓位，共用账户也必须拒绝启动。"""
    cli = _cli_module()
    hedge = _SharedAccountHedge(
        positions=[Position("ETH-USD", Decimal("0.01"))]
    )
    _patch_shared_account_clients(monkeypatch, cli, hedge)

    class FailEngine:
        def __init__(self, *_args, **_kwargs) -> None:
            pytest.fail("账户级仓位门禁必须发生在引擎构造前")

    monkeypatch.setattr(cli, "HedgeEngine", FailEngine)

    with pytest.raises(cli.StartupError, match="仍有仓位"):
        asyncio.run(
            cli._main(
                _args(account="X10_GRID", allow_shared_account=True)
            )
        )

    assert hedge.calls == [("positions", "全部")]
    assert hedge.closed is True


def test_shared_account_with_grid_orders_is_rejected(monkeypatch) -> None:
    """已声明但目标账户仍有普通网格单时，入口必须拒绝启动。"""
    cli = _cli_module()
    hedge = _SharedAccountHedge(
        open_orders=[
            SimpleNamespace(
                id="grid-order-1",
                market="ETH-USD",
                reduce_only=False,
                type="LIMIT",
                tp_sl_type=None,
            )
        ]
    )
    _patch_shared_account_clients(monkeypatch, cli, hedge)

    class FailEngine:
        def __init__(self, *_args, **_kwargs) -> None:
            pytest.fail("账户级挂单门禁必须发生在引擎构造前")

    monkeypatch.setattr(cli, "HedgeEngine", FailEngine)

    with pytest.raises(cli.StartupError, match="仍有网格活动"):
        asyncio.run(
            cli._main(
                _args(account="X10_GRID", allow_shared_account=True)
            )
        )

    assert hedge.calls == [
        ("positions", "全部"),
        ("open_orders", "全部"),
    ]
    assert hedge.closed is True


def test_clean_shared_account_passes_runtime_validation() -> None:
    """零持仓且仅有保护单时应视为干净，不得误判成网格活动。"""
    cli = _cli_module()
    hedge = _SharedAccountHedge(
        open_orders=[
            SimpleNamespace(
                id="reduce-only-1",
                reduce_only=True,
                type="LIMIT",
                tp_sl_type=None,
            ),
            SimpleNamespace(
                id="conditional-1",
                reduce_only=False,
                type="CONDITIONAL",
                tp_sl_type="POSITION",
            ),
        ]
    )

    asyncio.run(cli._validate_shared_account_state(hedge))

    assert hedge.calls == [
        ("positions", "全部"),
        ("open_orders", "全部"),
    ]


def test_clean_shared_account_main_reaches_engine(monkeypatch) -> None:
    """账户级校验通过后，入口必须继续完成风控自检并启动引擎。"""
    cli = _cli_module()
    hedge = _SharedAccountHedge()
    _patch_shared_account_clients(monkeypatch, cli, hedge)
    captured: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, *_args, **_kwargs) -> None:
            captured["engine_created"] = True

        async def run_forever(self) -> None:
            captured["ran_forever"] = True

    monkeypatch.setattr(cli, "HedgeEngine", FakeEngine)

    asyncio.run(
        cli._main(_args(account="X10_GRID", allow_shared_account=True))
    )

    assert hedge.calls == [
        ("positions", "全部"),
        ("open_orders", "全部"),
    ]
    assert captured == {"engine_created": True, "ran_forever": True}
    assert hedge.closed is True


def test_shared_account_rejects_offline_selfcheck_before_constructing_clients(
    monkeypatch,
) -> None:
    """共用模式依赖账户查询，不能伪装成纯离线风控自检。"""
    cli = _cli_module()
    monkeypatch.setenv("X10_HEDGE_VAULT_ID", "9002")
    monkeypatch.setenv("X10_GRID_VAULT_ID", "9001")

    def fail_constructor(*_args, **_kwargs):
        pytest.fail("互斥参数必须在客户端构造前拒绝")

    monkeypatch.setattr(cli, "LighterClient", fail_constructor)
    monkeypatch.setattr(
        cli,
        "ExtendedClient",
        SimpleNamespace(from_env=fail_constructor),
    )

    with pytest.raises(
        cli.StartupError,
        match="--allow-shared-account.*--risk-selfcheck-only.*不能同时使用",
    ):
        asyncio.run(
            cli._main(
                _args(
                    allow_shared_account=True,
                    risk_selfcheck_only=True,
                )
            )
        )


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
    assert args.allow_shared_account is False
    assert args.account == "X10_HEDGE"
    assert args.lighter_address == "0xwallet"
    assert args.market == "BTC"
    assert args.hedge_market == "BTC-USD"
    assert args.max_primary_notional == Decimal("2000")
    assert args.interval == 30.0
    assert args.rebalance_threshold == Decimal("0.02")
    assert args.min_hedge_free_margin_ratio == Decimal("0.20")
    assert args.maker_first_timeout == 0.0
    assert args.waive_risk == []
    assert args.risk_selfcheck_only is False

    declared = cli._build_parser().parse_args(["--allow-shared-account"])
    assert declared.allow_shared_account is True


def test_risk_selfcheck_rejects_missing_real_adapter_capability() -> None:
    """桩刻意缺 get_position，验证失败路径而不是只伪造成功能力。"""
    cli = _cli_module()

    class ReadablePrimary:
        async def get_position(self, _market):
            return Position("BTC", Decimal("0"))

        async def get_market_price(self, _market):
            return SimpleNamespace(mid=Decimal("60000"))

        async def get_liquidation_info(self, _market):
            return None

    class HedgeWithoutPositionRead:
        async def get_liquidation_info(self, _market):
            return None

        async def market_order(self, *_args, **_kwargs):
            return None

        async def get_free_margin_ratio(self):
            return Decimal("1")

        async def get_balance(self):
            return SimpleNamespace(equity=Decimal("100"))

    hedge = HedgeWithoutPositionRead()
    assert not hasattr(hedge, "get_position")

    with pytest.raises(cli.StartupError, match="两腿持仓读取.*get_position"):
        cli._validate_risk_controls(
            ReadablePrimary(),
            hedge,
            _args(waive_risk=[]),
        )


def test_startup_summary_contains_account_and_each_risk_layer() -> None:
    """启动摘要必须同时暴露所选账户与每层风控自检结果。"""
    cli = _cli_module()

    summary = cli._startup_summary(
        account_prefix="X10_FUNDED_HEDGE",
        lighter_address="0xwallet",
        account_index=5626,
        args=_args(account="X10_FUNDED_HEDGE"),
        risk_statuses={
            "position_visibility": "启用",
            "hedge_execution": "启用",
            "liquidation_constraints": "启用",
            "primary_notional_cap": "启用",
            "margin_monitor": "启用",
        },
    )

    assert "Extended 账户前缀：X10_FUNDED_HEDGE" in summary
    assert "风控状态：" in summary
    assert "两腿持仓读取=启用" in summary
    assert "对冲执行=启用" in summary
    assert "清算约束=启用" in summary
    assert "primary 名义上限=启用" in summary
    assert "对冲保证金监控=启用" in summary


def test_startup_summary_marks_shared_account_and_warns_against_grid_process() -> None:
    """共用模式必须在摘要中醒目标注，并提示不得同时运行网格进程。"""
    cli = _cli_module()

    summary = cli._startup_summary(
        account_prefix="X10_GRID",
        lighter_address="0xwallet",
        account_index=5626,
        args=_args(account="X10_GRID", allow_shared_account=True),
        risk_statuses={
            "position_visibility": "启用",
            "hedge_execution": "启用",
            "liquidation_constraints": "启用",
            "primary_notional_cap": "启用",
            "margin_monitor": "启用",
        },
    )

    assert "Extended 账户模式：与网格共用" in summary
    assert "网格进程不得同时运行" in summary


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

        async def get_position(self, market):
            return Position(market, Decimal("0"))

        async def get_market_price(self, market):
            return SimpleNamespace(market=market, mid=Decimal("60000"))

        async def get_liquidation_info(self, _market):
            return None

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

        async def get_position(self, market):
            return Position(market, Decimal("0"))

        async def get_market_price(self, market):
            return SimpleNamespace(
                market=market,
                bid=Decimal("59999"),
                ask=Decimal("60001"),
            )

        async def market_order(self, *_args, **_kwargs):
            return None

        async def place_limit_order(self, *_args, **_kwargs):
            return SimpleNamespace(id="order-1")

        async def cancel_order(self, *_args, **_kwargs):
            return None

        async def get_liquidation_info(self, _market):
            return None

        async def get_free_margin_ratio(self):
            return Decimal("1")

        async def get_balance(self):
            return SimpleNamespace(equity=Decimal("1000"))

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

    def capture_snapshot_options(primary, hedge, **options):
        captured["snapshot_primary"] = primary
        captured["snapshot_hedge"] = hedge
        captured["snapshot_options"] = options

        async def snapshot(_state):
            return None

        return snapshot

    monkeypatch.setattr(cli, "_build_snapshot_callback", capture_snapshot_options)

    asyncio.run(
        cli._main(
            _args(
                live=True,
                account="x10_hedge",
                max_primary_notional=Decimal("2500"),
                interval=45.0,
                rebalance_threshold=Decimal("0.03"),
                maker_first_timeout=12.0,
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
    assert config.maker_first_timeout_s == 12.0
    assert config.dry_run is False
    assert config.auth_error_types == ()
    assert captured["snapshot_options"]["max_primary_notional"] == Decimal("2500")
    assert captured["primary"].closed is True
    assert captured["hedge"].closed is True

    output = capsys.readouterr().out
    assert "Lighter 地址：0x4A3D...3d82" in output
    assert "Lighter account_index：5626" in output
    assert "Extended 账户前缀：X10_HEDGE" in output
    assert "标的：BTC → BTC-USD" in output
    assert "名义上限：2500 USD" in output
    assert "maker 优先等待：12 秒（0=关闭）" in output
    assert "风控状态：两腿持仓读取=启用" in output
    assert "dry_run：False" in output


def test_snapshot_callback_appends_complete_round(monkeypatch, tmp_path) -> None:
    """正常一轮必须写出两腿、净敞口、动作和保证金事实。"""
    cli = _cli_module()
    snapshot_file = tmp_path / "lighter_hedge.jsonl"
    monkeypatch.setattr(cli, "_HEDGE_SNAPSHOT", snapshot_file)
    assert hasattr(cli, "_build_snapshot_callback")
    monkeypatch.setattr(cli.time, "time", lambda: 1_786_000_000.0)

    class FakePrimary:
        async def get_collateral(self):
            return Decimal("489.265906")

    class FakeHedge:
        async def get_free_margin_ratio(self):
            return Decimal("0.35")

        async def get_balance(self):
            return SimpleNamespace(equity=Decimal("465.23"))

    callback = cli._build_snapshot_callback(
        FakePrimary(),
        FakeHedge(),
        interval=30.0,
        rebalance_threshold_ratio=Decimal("0.02"),
        min_hedge_free_margin_ratio=Decimal("0.20"),
        max_primary_notional=Decimal("2000"),
    )
    state = HedgeState(
        primary=Position(
            "BTC",
            Decimal("1"),
            raw={
                "position_value": "1499.25",
                "unrealized_pnl": "-1.25",
            },
        ),
        hedge=Position(
            "BTC-USD",
            Decimal("-0.9"),
            raw=SimpleNamespace(
                value="1499.052990",
                unrealised_pnl="0.30",
            ),
        ),
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
        "primary_collateral": "489.265906",
        "hedge_equity": "465.23",
        "primary_notional": "1499.25",
        "max_primary_notional": "2000",
        "hedge_notional": "1499.052990",
        "primary_unrealized": "-1.25",
        "hedge_unrealized": "0.30",
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

    class BrokenPrimary:
        async def get_collateral(self):
            raise RuntimeError("Lighter account timeout")

    class BrokenMarginHedge:
        async def get_free_margin_ratio(self):
            raise RuntimeError("Extended balance timeout")

        async def get_balance(self):
            raise RuntimeError("Extended equity timeout")

    callback = cli._build_snapshot_callback(
        BrokenPrimary(),
        BrokenMarginHedge(),
        interval=30.0,
        rebalance_threshold_ratio=Decimal("0.02"),
        min_hedge_free_margin_ratio=Decimal("0.20"),
        max_primary_notional=Decimal("2000"),
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
    assert row["primary_collateral"] is None
    assert row["hedge_equity"] is None
    assert row["primary_notional"] is None
    assert row["max_primary_notional"] == "2000"
    assert row["hedge_notional"] is None
    assert row["primary_unrealized"] is None
    assert row["hedge_unrealized"] is None


def test_snapshot_callback_keeps_heartbeat_when_notional_conversion_fails(
    monkeypatch,
    tmp_path,
) -> None:
    """原始名义金额异常时必须写 null，不能中断交易循环的心跳。"""
    cli = _cli_module()
    snapshot_file = tmp_path / "lighter_hedge.jsonl"
    monkeypatch.setattr(cli, "_HEDGE_SNAPSHOT", snapshot_file)

    class BrokenValue:
        def __str__(self):
            raise RuntimeError("position_value 无法转换")

    class FakePrimary:
        async def get_collateral(self):
            return Decimal("489.27")

    class FakeHedge:
        async def get_free_margin_ratio(self):
            return Decimal("0.35")

        async def get_balance(self):
            return SimpleNamespace(equity=Decimal("465.23"))

    callback = cli._build_snapshot_callback(
        FakePrimary(),
        FakeHedge(),
        interval=30.0,
        rebalance_threshold_ratio=Decimal("0.02"),
        min_hedge_free_margin_ratio=Decimal("0.20"),
        max_primary_notional=Decimal("2000"),
    )
    state = HedgeState(
        primary=Position(
            "BTC",
            Decimal("1"),
            raw={"position_value": BrokenValue()},
        ),
        hedge=Position("BTC-USD", Decimal("-1")),
        action_taken="无需再平衡",
    )

    asyncio.run(callback(state))

    row = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert row["primary_notional"] is None
    assert row["max_primary_notional"] == "2000"


def test_snapshot_callback_degrades_each_new_raw_field_independently(
    monkeypatch,
    tmp_path,
) -> None:
    """任一新字段读取失败时，其他已有原始字段仍要进入心跳。"""
    cli = _cli_module()
    snapshot_file = tmp_path / "lighter_hedge.jsonl"
    monkeypatch.setattr(cli, "_HEDGE_SNAPSHOT", snapshot_file)

    class FakePrimary:
        async def get_collateral(self):
            return Decimal("489.27")

    class FakeHedge:
        async def get_free_margin_ratio(self):
            return Decimal("0.35")

        async def get_balance(self):
            return SimpleNamespace(equity=Decimal("465.23"))

    class BrokenPrimaryRaw(dict):
        def get(self, key, default=None):
            if key == "unrealized_pnl":
                raise RuntimeError("unrealized_pnl 读取失败")
            return super().get(key, default)

    class BrokenValue:
        def __str__(self):
            raise RuntimeError("value 无法转换")

    class BrokenHedgeUnrealized:
        value = "1499.052990"

        @property
        def unrealised_pnl(self):
            raise RuntimeError("unrealised_pnl 读取失败")

    callback = cli._build_snapshot_callback(
        FakePrimary(),
        FakeHedge(),
        interval=30.0,
        rebalance_threshold_ratio=Decimal("0.02"),
        min_hedge_free_margin_ratio=Decimal("0.20"),
        max_primary_notional=Decimal("2000"),
    )
    states = [
        HedgeState(
            primary=Position(
                "BTC",
                Decimal("1"),
                raw=BrokenPrimaryRaw(position_value="1499.25"),
            ),
            hedge=Position(
                "BTC-USD",
                Decimal("-1"),
                raw=SimpleNamespace(value="1499.052990", unrealised_pnl="0.30"),
            ),
        ),
        HedgeState(
            primary=Position(
                "BTC",
                Decimal("1"),
                raw={"position_value": "1499.25", "unrealized_pnl": "-1.25"},
            ),
            hedge=Position(
                "BTC-USD",
                Decimal("-1"),
                raw=SimpleNamespace(value=BrokenValue(), unrealised_pnl="0.30"),
            ),
        ),
        HedgeState(
            primary=Position(
                "BTC",
                Decimal("1"),
                raw={"position_value": "1499.25", "unrealized_pnl": "-1.25"},
            ),
            hedge=Position(
                "BTC-USD",
                Decimal("-1"),
                raw=BrokenHedgeUnrealized(),
            ),
        ),
    ]

    for state in states:
        asyncio.run(callback(state))

    rows = [
        json.loads(line)
        for line in snapshot_file.read_text(encoding="utf-8").splitlines()
    ]
    assert (
        rows[0]["primary_unrealized"],
        rows[0]["hedge_notional"],
        rows[0]["hedge_unrealized"],
    ) == (None, "1499.052990", "0.30")
    assert (
        rows[1]["primary_unrealized"],
        rows[1]["hedge_notional"],
        rows[1]["hedge_unrealized"],
    ) == ("-1.25", None, "0.30")
    assert (
        rows[2]["primary_unrealized"],
        rows[2]["hedge_notional"],
        rows[2]["hedge_unrealized"],
    ) == ("-1.25", "1499.052990", None)


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
