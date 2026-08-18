"""Lighter 无对冲做市入口测试。"""

from __future__ import annotations

import asyncio
import importlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


def _cli_module():
    """延迟导入入口，使 RED 阶段明确暴露缺失的生产模块。"""
    return importlib.import_module("tools.run_lighter_mm")


def _args(**overrides) -> SimpleNamespace:
    """构造启动自检与引擎装配所需的完整参数。"""
    values = {
        "live": False,
        "market": "BTC",
        "unit": 50.0,
        "levels": 4,
        "max_inv": 500.0,
        "spacing": 0.000986,
        "interval": 2.5,
        "slow_interval": 30.0,
        "lighter_address": "0xabc",
        "candle_account": "X10_HEDGE",
        "state_path": "data/lighter_mm/state.json",
        "trend_aware": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parser_uses_safe_defaults(monkeypatch) -> None:
    """防止默认参数漂移，使首次启动意外扩大档位或跳过 dry-run。"""
    monkeypatch.setenv("LIGHTER_RH_L1_ADDRESS", "0xwallet")
    cli = _cli_module()

    args = cli.build_parser().parse_args([])

    assert args.live is False
    assert args.market == "BTC"
    assert args.unit == 50.0
    assert args.levels == 4
    assert args.max_inv == 500.0
    assert args.spacing == 0.000986
    assert args.interval == 2.5
    assert args.slow_interval == 30.0
    assert args.lighter_address == "0xwallet"
    assert args.candle_account == "X10_HEDGE"
    assert args.state_path == "data/lighter_mm/state.json"
    assert args.trend_aware is False
    assert Path(args.state_path).parent.resolve() != Path("data").resolve()
    assert cli._grid_config(args).state_path == args.state_path
    assert cli._grid_config(args).trend_aware is False


def test_default_state_path_isolates_all_grid_derived_files(monkeypatch) -> None:
    """防止「只隔离了主状态文件、派生文件仍撞车」这个具体故障。"""
    monkeypatch.setenv("LIGHTER_RH_L1_ADDRESS", "0xwallet")
    cli = _cli_module()

    args = cli.build_parser().parse_args([])
    state_dir = Path(args.state_path).parent
    extended_dir = Path("data").resolve()

    assert (state_dir / "equity_peak.json").resolve().parent != extended_dir
    assert (state_dir / "fills.jsonl").resolve().parent != extended_dir
    assert (state_dir / "grid_live.json").resolve().parent != extended_dir


def test_grid_config_enables_explicit_trend_aware_flag(monkeypatch) -> None:
    """显式开关必须让 Lighter 做市使用趋势感知路径。"""
    monkeypatch.setenv("LIGHTER_RH_L1_ADDRESS", "0xwallet")
    cli = _cli_module()

    args = cli.build_parser().parse_args(["--trend-aware"])

    assert args.trend_aware is True
    assert cli._grid_config(args).trend_aware is True


def test_validate_args_rejects_extended_grid_relative_state_path(capsys) -> None:
    """相对路径也不能复用 Extended 实盘网格的状态文件。"""
    cli = _cli_module()

    with pytest.raises(SystemExit):
        cli.validate_args(_args(state_path="data/grid_state.json"))

    output = capsys.readouterr().err
    assert "Extended 网格" in output
    assert "撞车" in output


def test_validate_args_rejects_any_state_file_in_extended_directory(capsys) -> None:
    """主文件名虽不同，只要仍放在 data 下就必须拒绝启动。"""
    cli = _cli_module()

    with pytest.raises(SystemExit):
        cli.validate_args(_args(state_path="data/lighter_mm_state.json"))

    output = capsys.readouterr().err
    assert "equity_peak.json" in output
    assert "fills.jsonl" in output
    assert "grid_live.json" in output
    assert "撞车" in output


def test_validate_args_rejects_extended_grid_absolute_state_path(capsys) -> None:
    """绝对路径指向 data 下任意文件也不得绕过状态目录隔离校验。"""
    cli = _cli_module()
    extended_state = str(Path("data/arbitrary_state.json").resolve())

    with pytest.raises(SystemExit):
        cli.validate_args(_args(state_path=extended_state))

    output = capsys.readouterr().err
    assert "Extended 网格" in output
    assert "撞车" in output


def test_validate_args_rejects_non_positive_levels(capsys) -> None:
    """防止零档或负档配置进入循环后形成无报价的假运行状态。"""
    cli = _cli_module()

    with pytest.raises(SystemExit):
        cli.validate_args(_args(levels=0))

    assert "档位" in capsys.readouterr().err


def test_validate_args_rejects_inventory_above_hard_cap(capsys) -> None:
    """防止手滑多输入一个零，把真实库存风险放大到 500 美元硬顶以上。"""
    cli = _cli_module()

    with pytest.raises(SystemExit):
        cli.validate_args(_args(max_inv=500.01))

    output = capsys.readouterr().err
    assert "库存" in output
    assert "500" in output


def test_validate_args_rejects_unit_below_lighter_minimum(capsys) -> None:
    """防止低名义订单被 Lighter 以 21706 全部拒绝而机器人仍像在正常运行。"""
    cli = _cli_module()

    with pytest.raises(SystemExit):
        cli.validate_args(_args(unit=14.99))

    output = capsys.readouterr().err
    assert "每格" in output
    assert "15" in output


def test_validate_args_rejects_full_side_above_inventory(capsys) -> None:
    """防止单边挂满后已承诺名义额超过配置的库存上限。"""
    cli = _cli_module()

    with pytest.raises(SystemExit):
        cli.validate_args(_args(unit=51.0, levels=4, max_inv=200.0))

    assert "单边" in capsys.readouterr().err


def test_validate_args_rejects_live_without_private_key(monkeypatch, capsys) -> None:
    """防止实盘启动到客户端构造阶段才因缺签名密钥失败，留下含糊启动记录。"""
    monkeypatch.delenv("LIGHTER_API_PRIVATE_KEY", raising=False)
    cli = _cli_module()

    with pytest.raises(SystemExit):
        cli.validate_args(_args(live=True))

    assert "LIGHTER_API_PRIVATE_KEY" in capsys.readouterr().err


def test_validate_args_rejects_missing_lighter_address(capsys) -> None:
    """地址缺失时必须在客户端构造前给出可定位的中文错误。"""
    cli = _cli_module()

    with pytest.raises(SystemExit):
        cli.validate_args(_args(lighter_address=None))

    assert "缺少 Lighter L1 地址" in capsys.readouterr().err


def test_validate_args_allows_inventory_boundary() -> None:
    """防止把单边名义刚好等于库存上限的安全边界误判为超限。"""
    cli = _cli_module()

    cli.validate_args(_args(unit=50.0, levels=4, max_inv=200.0))


def test_validate_args_allows_legal_live_parameters(monkeypatch) -> None:
    """防止安全校验写得过宽，连具备私钥且所有额度合法的实盘参数也无法启动。"""
    monkeypatch.setenv("LIGHTER_API_PRIVATE_KEY", "secret")
    cli = _cli_module()

    cli.validate_args(_args(live=True, unit=25.0, levels=4, max_inv=100.0))


def test_dry_run_assembles_read_only_lighter_and_extended_candles(
    monkeypatch,
    tmp_path,
) -> None:
    """防止用户以为处于 dry-run，入口却开启真实下单或误用 Lighter 的 403 K 线。"""
    monkeypatch.delenv("LIGHTER_API_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("LIGHTER_API_KEY_INDEX", raising=False)
    cli = _cli_module()
    captured: dict = {}

    class FakeLighter:
        def __init__(self, **kwargs) -> None:
            captured["lighter_kwargs"] = kwargs

        async def close(self) -> None:
            captured["lighter_closed"] = True

    class FakeExtended:
        @classmethod
        def from_env(cls, prefix: str):
            captured["candle_account"] = prefix
            return cls()

        async def close(self) -> None:
            captured["extended_closed"] = True

    class FakeCandleSource:
        def __init__(self, ext, market_override=None) -> None:
            captured["candle_client"] = ext
            captured["market_override"] = market_override

    class FakeEngine:
        def __init__(self, ext, config, candle_source=None) -> None:
            captured["engine_ext"] = ext
            captured["config"] = config
            captured["candle_source"] = candle_source

        async def run_forever(self) -> None:
            captured["engine_ran"] = True

    class FakeLogger:
        def info(self, message: str, *values) -> None:
            captured["summary"] = message % values

        def error(self, _message: str, *_values) -> None:
            pytest.fail("正常关闭不应写错误日志")

    monkeypatch.setattr(cli, "LighterClient", FakeLighter)
    monkeypatch.setattr(cli, "ExtendedClient", FakeExtended)
    monkeypatch.setattr(cli, "ExtendedCandleSource", FakeCandleSource)
    monkeypatch.setattr(cli, "GridEngine", FakeEngine)
    monkeypatch.setattr(cli, "logger", FakeLogger())

    asyncio.run(cli._main(_args(state_path=str(tmp_path / "state" / "state.json"))))

    assert captured["lighter_kwargs"] == {
        "l1_address": "0xabc",
        "api_private_key": None,
        "api_key_index": 255,
        "trading_enabled": False,
    }
    config = captured["config"]
    assert config.market == "BTC"
    assert config.unit_usd == 50.0
    assert config.levels_per_side == 4
    assert config.max_inventory_usd == 500.0
    assert config.spacing_pct == 0.000986
    assert config.poll_interval == 2.5
    assert config.slow_interval == 30.0
    assert config.dry_run is True
    assert captured["candle_account"] == "X10_HEDGE"
    assert captured["market_override"] == "BTC-USD"
    assert captured["engine_ext"] is not captured["candle_client"]
    assert captured["engine_ran"] is True
    assert captured["lighter_closed"] is True
    assert captured["extended_closed"] is True
    assert "标的=BTC" in captured["summary"]
    assert "dry_run=True" in captured["summary"]
    assert "\n" not in captured["summary"]


def test_startup_creates_missing_state_directory(monkeypatch, tmp_path) -> None:
    """首次启动必须先创建状态目录，避免网格引擎第一次持久化失败。"""
    cli = _cli_module()
    state_path = tmp_path / "new" / "nested" / "state.json"

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        @classmethod
        def from_env(cls, _prefix: str):
            return cls()

        async def close(self) -> None:
            pass

    class FakeCandleSource:
        def __init__(self, _client, market_override=None) -> None:
            pass

    class FakeEngine:
        def __init__(self, _client, _config, candle_source=None) -> None:
            pass

        async def run_forever(self) -> None:
            pass

    monkeypatch.setattr(cli, "LighterClient", FakeClient)
    monkeypatch.setattr(cli, "ExtendedClient", FakeClient)
    monkeypatch.setattr(cli, "ExtendedCandleSource", FakeCandleSource)
    monkeypatch.setattr(cli, "GridEngine", FakeEngine)

    assert not state_path.parent.exists()

    asyncio.run(cli._main(_args(state_path=str(state_path))))

    assert state_path.parent.is_dir()


def test_heartbeat_appends_complete_successful_round(monkeypatch, tmp_path) -> None:
    """成功轮次必须在本地追加包含配置、持仓与挂单事实的完整心跳。"""
    cli = _cli_module()
    heartbeat_path = tmp_path / "lighter_mm.jsonl"
    monkeypatch.setattr(cli, "_MM_HEARTBEAT", heartbeat_path, raising=False)

    class FakeEngine:
        def __init__(self) -> None:
            self._last_inv = None
            self._orders = {1: object(), 2: object()}

        async def _inv(self):
            return Decimal("0.0123"), 60_000.0

        async def run_once(self):
            await self._inv()
            return "本轮完成"

    engine = FakeEngine()
    cli._install_heartbeat(engine, _args(interval=1.75))

    assert asyncio.run(engine.run_once()) == "本轮完成"

    rows = heartbeat_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    payload = json.loads(rows[0])
    assert isinstance(payload["ts"], float)
    assert payload == {
        "ts": payload["ts"],
        "market": "BTC",
        "dry_run": True,
        "levels": 4,
        "unit": 50.0,
        "max_inv": 500.0,
        "interval": 1.75,
        "position_size": "0.0123",
        "inventory_usd": 738.0,
        "open_orders": 2,
        "success": True,
    }


def test_heartbeat_keeps_inventory_unknown_without_mark_price() -> None:
    """防止价格缺失时把未知库存名义额编造成 0，掩盖真实库存风险。"""
    cli = _cli_module()
    engine = SimpleNamespace(
        _last_inv=Decimal("0.0123"),
        _last_mark=None,
        _orders={},
    )

    payload = cli._heartbeat_payload(engine, _args(), success=True)

    assert payload["inventory_usd"] is None


def test_failed_price_read_does_not_reuse_previous_round_inventory(
    monkeypatch,
    tmp_path,
) -> None:
    """防止取价失败的轮次沿用上一轮标记价，把陈旧库存估值伪装成当前值。"""
    cli = _cli_module()
    heartbeat_path = tmp_path / "lighter_mm.jsonl"
    monkeypatch.setattr(cli, "_MM_HEARTBEAT", heartbeat_path, raising=False)

    class FakeEngine:
        def __init__(self) -> None:
            self._last_inv = None
            self._last_mark = None
            self._orders = {}
            self.round = 0

        async def _inv(self):
            if self.round == 1:
                return Decimal("0.01"), 60_000.0
            raise RuntimeError("本轮标记价不可用")

        async def run_once(self):
            self.round += 1
            await self._inv()

    engine = FakeEngine()
    cli._install_heartbeat(engine, _args())

    asyncio.run(engine.run_once())
    with pytest.raises(RuntimeError, match="本轮标记价不可用"):
        asyncio.run(engine.run_once())

    payloads = [
        json.loads(line)
        for line in heartbeat_path.read_text(encoding="utf-8").splitlines()
    ]
    assert payloads[0]["inventory_usd"] == 600.0
    assert payloads[1]["inventory_usd"] is None


def test_heartbeat_records_failed_round_without_hiding_error(
    monkeypatch,
    tmp_path,
) -> None:
    """失败轮次也要留下心跳，同时原始异常必须继续交给引擎循环处理。"""
    cli = _cli_module()
    heartbeat_path = tmp_path / "lighter_mm.jsonl"
    monkeypatch.setattr(cli, "_MM_HEARTBEAT", heartbeat_path, raising=False)

    class FakeEngine:
        _last_inv = None
        _orders: dict = {}

        async def run_once(self):
            raise RuntimeError("模拟轮次失败")

    engine = FakeEngine()
    cli._install_heartbeat(engine, _args())

    with pytest.raises(RuntimeError, match="模拟轮次失败"):
        asyncio.run(engine.run_once())

    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert payload["position_size"] is None
    assert payload["open_orders"] == 0
    assert payload["success"] is False
