"""网格风控完整性自检测试。"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from grid import grid_engine
from grid.grid_engine import GridConfig, GridEngine
from tools import run_grid, run_lighter_mm


_EXPLICIT_RISK_FLAGS = (
    "--trend-aware",
    "--max-drawdown",
    "--hard-stop-dist",
)


class _ReadyAdapter:
    """提供全部风控能力的最小适配器，不执行任何外部请求。"""

    async def get_balance(self):
        return SimpleNamespace(equity=Decimal("1000"))

    async def get_liquidation_info(self, _market: str):
        return Decimal("60000"), Decimal("90000")


class _MissingBalanceAdapter:
    """只缺权益能力，用于验证一处缺失影响两层风控。"""

    async def get_liquidation_info(self, _market: str):
        return Decimal("60000"), Decimal("90000")


class _ReadyCandleSource:
    """声明趋势 band 所需的 K 线能力。"""

    async def get_hourly_candles(self, _market: str, _limit: int):
        return []


def _protected_config(**overrides) -> GridConfig:
    """构造四层风控都开启且三项 CLI 配置均显式提供的配置。"""
    values = {
        "dry_run": False,
        "trend_aware": True,
        "max_drawdown_pct": 0.12,
        "hard_stop_dist": 0.12,
        "explicit_risk_flags": _EXPLICIT_RISK_FLAGS,
    }
    values.update(overrides)
    return GridConfig(**values)


def test_risk_mapping_covers_all_declared_layers() -> None:
    """显式映射至少覆盖规范列出的四层及其真实依赖。"""
    requirements = grid_engine.RISK_LAYER_REQUIREMENTS

    assert {
        "wallet_exposure_cap",
        "equity_drawdown",
        "position_tpsl_equity",
        "liquidation_constraints",
        "trend_band",
    } <= requirements.keys()
    assert requirements["wallet_exposure_cap"].capabilities == ("get_balance",)
    assert requirements["equity_drawdown"].capabilities == ("get_balance",)
    assert requirements["position_tpsl_equity"].capabilities == ("get_balance",)
    assert requirements["liquidation_constraints"].capabilities == (
        "get_liquidation_info",
    )
    assert requirements["trend_band"].capabilities == ("get_hourly_candles",)


def test_wallet_exposure_cap_requires_balance_only_when_configured() -> None:
    """比例层默认可不配置；一旦启用，缺失权益能力必须拒绝启动。"""
    without_ratio = GridEngine(
        _ReadyAdapter(),
        _protected_config(),
        candle_source=_ReadyCandleSource(),
    )

    statuses = without_ratio.validate_risk_controls()

    assert statuses["wallet_exposure_cap"] == "未配置"

    with_ratio = GridEngine(
        _MissingBalanceAdapter(),
        _protected_config(wallet_exposure_ratio=3.0),
        candle_source=_ReadyCandleSource(),
    )
    with pytest.raises(RuntimeError) as raised:
        with_ratio.validate_risk_controls()

    message = str(raised.value)
    assert "权益比例库存上限" in message
    assert "get_balance" in message


def test_enabled_layers_missing_balance_reject_with_capability_and_layers() -> None:
    """一个缺失能力同时影响两层时，拒绝原因必须完整列出。"""
    engine = GridEngine(
        _MissingBalanceAdapter(),
        _protected_config(),
        candle_source=_ReadyCandleSource(),
    )

    with pytest.raises(RuntimeError) as raised:
        engine.validate_risk_controls()

    message = str(raised.value)
    assert "get_balance" in message
    assert "净值回撤熔断" in message
    assert "整仓 TPSL 权益约束" in message


def test_default_has_no_waiver_and_closed_layer_rejects() -> None:
    """什么都不声明不等于默认放弃，关闭关键层仍应拒绝启动。"""
    config = GridConfig(trend_aware=False)
    engine = GridEngine(
        _ReadyAdapter(),
        config,
        candle_source=_ReadyCandleSource(),
    )

    assert config.risk_waivers == ()
    with pytest.raises(RuntimeError, match="趋势 band 保护"):
        engine.validate_risk_controls()


def test_explicit_waiver_logs_warning_and_allows_start(caplog) -> None:
    """依赖缺失时，只有明确放弃受影响层后才以 WARNING 放行。"""
    engine = GridEngine(
        _MissingBalanceAdapter(),
        GridConfig(
            trend_aware=True,
            risk_waivers=(
                "equity_drawdown",
                "position_tpsl_equity",
            ),
        ),
        candle_source=_ReadyCandleSource(),
    )

    with caplog.at_level(logging.WARNING, logger="grid_engine"):
        engine.validate_risk_controls()

    assert "知情放弃" in caplog.text
    assert "get_balance" in caplog.text
    assert "净值回撤熔断" in caplog.text
    assert "整仓 TPSL 权益约束" in caplog.text


def test_dry_run_still_rejects_missing_capability() -> None:
    """dry-run 也必须暴露能力缺失，不能形成可安全运行的错觉。"""
    engine = GridEngine(
        _MissingBalanceAdapter(),
        _protected_config(dry_run=True),
        candle_source=_ReadyCandleSource(),
    )

    with pytest.raises(RuntimeError, match="get_balance"):
        engine.validate_risk_controls()


@pytest.mark.parametrize("hard_stop_dist", [0, -0.01])
def test_nonpositive_hard_stop_requires_explicit_waiver(
    hard_stop_dist: float,
) -> None:
    """TPSL 清算约束仍在时，也不能掩盖硬止损已被非正参数关闭。"""
    engine = GridEngine(
        _ReadyAdapter(),
        _protected_config(hard_stop_dist=hard_stop_dist),
        candle_source=_ReadyCandleSource(),
    )

    with pytest.raises(RuntimeError, match="硬止损配置已关闭"):
        engine.validate_risk_controls()


def test_ready_dependencies_enter_existing_loop_and_log_summary(
    monkeypatch,
    caplog,
) -> None:
    """依赖齐备时仍按原顺序连接并执行交易循环。"""
    engine = GridEngine(
        _ReadyAdapter(),
        _protected_config(poll_interval=0),
        candle_source=_ReadyCandleSource(),
    )
    calls: list[str] = []

    async def fake_connect() -> None:
        calls.append("connect")

    async def fake_run_once(*_args, **_kwargs) -> str:
        calls.append("run_once")
        engine.stop()
        return "完成"

    monkeypatch.setattr(engine, "connect", fake_connect)
    monkeypatch.setattr(engine, "run_once", fake_run_once)

    with caplog.at_level(logging.INFO, logger="grid_engine"):
        asyncio.run(engine.run_forever())

    assert calls == ["connect", "run_once"]
    assert "风控状态摘要" in caplog.text
    assert "净值回撤熔断=启用" in caplog.text


def test_summary_distinguishes_closed_from_unavailable(caplog) -> None:
    """参数关闭与能力缺失是不同原因，摘要不得混成同一状态。"""
    engine = GridEngine(
        _MissingBalanceAdapter(),
        GridConfig(trend_aware=False),
        candle_source=_ReadyCandleSource(),
    )

    with caplog.at_level(logging.INFO, logger="grid_engine"):
        with pytest.raises(RuntimeError):
            engine.validate_risk_controls()

    assert "趋势 band 保护=关闭" in caplog.text
    assert "净值回撤熔断=不可用" in caplog.text


def test_runtime_missing_warning_is_rate_limited(caplog) -> None:
    """连续同因跳过只在首次输出 WARNING，其余重复事件降为 DEBUG。"""
    engine = GridEngine(
        _ReadyAdapter(),
        _protected_config(),
        candle_source=_ReadyCandleSource(),
    )

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        for _ in range(3):
            engine._log_risk_capability_missing(
                "equity_drawdown",
                "get_balance",
            )

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "运行期风控跳过" in record.message
    ]
    assert len(warnings) == 1
    assert "净值回撤熔断" in warnings[0].message
    assert "get_balance" in warnings[0].message


def test_runtime_drawdown_check_uses_limited_missing_warning(caplog) -> None:
    """真实回撤检查入口遇到缺失能力时，也必须接入同一限频告警。"""
    engine = GridEngine(
        _MissingBalanceAdapter(),
        _protected_config(),
        candle_source=_ReadyCandleSource(),
    )

    with caplog.at_level(logging.DEBUG, logger="grid_engine"):
        for _ in range(3):
            assert asyncio.run(engine._check_equity_drawdown()) is False

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "运行期风控跳过" in record.message
    ]
    assert len(warnings) == 1
    assert "净值回撤熔断" in warnings[0].message
    assert "get_balance" in warnings[0].message


def _lighter_incident_config() -> GridConfig:
    """把 8 月 19 日 Lighter plist 的 ProgramArguments 转成配置。"""
    args = run_lighter_mm.build_parser().parse_args(
        [
            "--live",
            "--unit",
            "300",
            "--levels",
            "10",
            "--max-inv",
            "3750",
            "--spacing",
            "0.000986",
            "--interval",
            "2.5",
        ]
    )
    return run_lighter_mm._grid_config(args)


def test_lighter_incident_config_without_balance_rejects() -> None:
    """回放事故时的旧适配器：缺失权益能力必须暴露全部受影响层。"""
    engine = GridEngine(
        _MissingBalanceAdapter(),
        _lighter_incident_config(),
        candle_source=_ReadyCandleSource(),
    )

    with pytest.raises(RuntimeError) as raised:
        engine.validate_risk_controls()

    message = str(raised.value)
    assert "get_balance" in message
    assert "整仓 TPSL 权益约束" in message
    assert "趋势 band 保护" in message and "--trend-aware" in message
    assert "净值回撤熔断" in message and "--max-drawdown" in message
    assert "清算相关约束" in message and "--hard-stop-dist" in message


def test_lighter_current_arguments_reject_three_unconfirmed_layers() -> None:
    """适配器补齐权益后，现行 plist 仍因三个 flag 全无而拒绝。"""
    engine = GridEngine(
        _ReadyAdapter(),
        _lighter_incident_config(),
        candle_source=_ReadyCandleSource(),
    )

    with pytest.raises(RuntimeError) as raised:
        engine.validate_risk_controls()

    message = str(raised.value)
    assert "（3 层）" in message
    assert "趋势 band 保护" in message and "--trend-aware" in message
    assert "净值回撤熔断" in message and "--max-drawdown" in message
    assert "清算相关约束" in message and "--hard-stop-dist" in message


def test_extended_selfcheck_only_validates_without_entering_loop(
    monkeypatch,
) -> None:
    """Extended 入口的只检模式必须执行自检、关闭客户端且不启动循环。"""
    args = run_grid._build_parser().parse_args(
        [
            "--trend-aware",
            "--max-drawdown",
            "0.12",
            "--hard-stop-dist",
            "0.12",
            "--risk-selfcheck-only",
        ]
    )
    captured: dict[str, object] = {}

    class FakeExtended:
        @classmethod
        def from_env(cls, prefix: str):
            captured["prefix"] = prefix
            return cls()

        async def close(self) -> None:
            captured["closed"] = True

    class FakeEngine:
        def __init__(self, _ext, config, fng_provider=None) -> None:
            captured["config"] = config

        def validate_risk_controls(self) -> None:
            captured["validated"] = True

        async def run_forever(self) -> None:
            pytest.fail("只检模式不得进入交易循环")

    monkeypatch.setattr(run_grid, "ExtendedClient", FakeExtended)
    monkeypatch.setattr(run_grid, "GridEngine", FakeEngine)

    asyncio.run(run_grid._main(args))

    assert captured["validated"] is True
    assert captured["closed"] is True


def test_lighter_selfcheck_only_needs_no_signer_and_does_not_run(
    monkeypatch,
    tmp_path,
) -> None:
    """Lighter 只检模式不得要求交易私钥、启用签名器或启动循环。"""
    args = run_lighter_mm.build_parser().parse_args(
        [
            "--live",
            "--lighter-address",
            "0xabc",
            "--state-path",
            str(tmp_path / "lighter" / "state.json"),
            "--trend-aware",
            "--max-drawdown",
            "0.12",
            "--hard-stop-dist",
            "0.12",
            "--risk-selfcheck-only",
        ]
    )
    monkeypatch.delenv("LIGHTER_API_PRIVATE_KEY", raising=False)
    captured: dict[str, object] = {}

    class FakeLighter:
        def __init__(self, **kwargs) -> None:
            captured["lighter_kwargs"] = kwargs

        async def close(self) -> None:
            captured["lighter_closed"] = True

    class FakeExtended:
        @classmethod
        def from_env(cls, _prefix: str):
            return cls()

        async def close(self) -> None:
            captured["extended_closed"] = True

    class FakeCandleSource:
        def __init__(self, _ext, market_override=None) -> None:
            pass

    class FakeEngine:
        def __init__(self, _ext, _config, candle_source=None) -> None:
            pass

        def validate_risk_controls(self) -> None:
            captured["validated"] = True

        async def run_forever(self) -> None:
            pytest.fail("只检模式不得进入交易循环")

    monkeypatch.setattr(run_lighter_mm, "LighterClient", FakeLighter)
    monkeypatch.setattr(run_lighter_mm, "ExtendedClient", FakeExtended)
    monkeypatch.setattr(run_lighter_mm, "ExtendedCandleSource", FakeCandleSource)
    monkeypatch.setattr(run_lighter_mm, "GridEngine", FakeEngine)

    asyncio.run(run_lighter_mm._main(args))

    assert captured["validated"] is True
    assert captured["lighter_kwargs"]["trading_enabled"] is False
    assert captured["lighter_closed"] is True
    assert captured["extended_closed"] is True
