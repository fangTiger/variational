"""Swap carry 面板 provider 测试；全部离线，未配置调用默认抛错。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from adapters.base import Position


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _metadata() -> dict[str, object]:
    """构造包含两腿价格和真实交易时段的元数据。"""
    return {
        "XAUS": [
            {
                "instrument_type": "swap",
                "funding_interval_s": 0,
                "market_status": "open",
                "price": "4000",
                "trading_schedule": {
                    "next_open_at": "2026-09-08T22:00:00Z",
                    "next_close_at": "2026-09-08T18:30:00Z",
                },
                "trading_sessions": [
                    {
                        "open": "2026-09-08T00:00:00Z",
                        "close": "2026-09-08T18:30:00Z",
                    },
                    {
                        "open": "2026-09-08T22:00:00Z",
                        "close": "2026-09-09T18:30:00Z",
                    },
                ],
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


def _transfers() -> list[dict[str, object]]:
    """本周两笔真实扣款外加一笔上周记录。"""
    return [
        {
            "created_at": "2026-09-08T08:00:00Z",
            "funding_rate": "-0.0001",
            "qty": "-1.25",
            "reference_instrument": {"underlying": "XAUS"},
        },
        {
            "created_at": "2026-09-08T09:00:00Z",
            "funding_rate": "0.0001",
            "qty": "1.75",
            "reference_instrument": {"underlying": "XAU"},
        },
        {
            "created_at": "2026-09-06T23:59:59Z",
            "funding_rate": "0.0001",
            "qty": "100",
            "reference_instrument": {"underlying": "XAU"},
        },
    ]


class StrictClient:
    """仅允许显式配置的只读调用，漏配立即失败。"""

    def __init__(
        self,
        *,
        positions: dict[str, Decimal] | BaseException | None = None,
        metadata: object | BaseException | None = None,
        liquidations: dict[str, object] | BaseException | None = None,
        xaus_rate: Decimal | BaseException | None = None,
        xau_rate: Decimal | BaseException | None = None,
        transfers: list[dict[str, object]] | BaseException | None = None,
    ) -> None:
        self.positions = positions
        self.metadata = metadata
        self.liquidations = liquidations
        self.xaus_rate = xaus_rate
        self.xau_rate = xau_rate
        self.transfers = transfers
        self.calls: list[str] = []

    @staticmethod
    def _configured(value: object, name: str) -> object:
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise AssertionError(f"未配置调用：{name}")
        return value

    async def get_position(self, underlying: str, *, exact: bool = False) -> Position:
        self.calls.append(f"position:{underlying}")
        assert exact is True
        positions = self._configured(self.positions, "get_position")
        assert isinstance(positions, dict)
        if underlying not in positions:
            raise AssertionError(f"未配置仓位：{underlying}")
        return Position(underlying, positions[underlying])

    async def get_supported_assets(self) -> object:
        self.calls.append("supported_assets")
        return self._configured(self.metadata, "get_supported_assets")

    async def get_liquidation_info(
        self, underlying: str, *, exact: bool = False
    ) -> object:
        self.calls.append(f"liquidation:{underlying}")
        assert exact is True
        values = self._configured(self.liquidations, "get_liquidation_info")
        assert isinstance(values, dict)
        return self._configured(values.get(underlying), f"liquidation:{underlying}")

    async def get_swap_funding(self, underlying: str) -> object:
        self.calls.append("swap_funding")
        assert underlying == "XAUS"
        rate = self._configured(self.xaus_rate, "get_swap_funding")
        return SimpleNamespace(
            upcoming=SimpleNamespace(
                long_rate=SimpleNamespace(normalized_annual_rate=rate)
            )
        )

    async def get_funding_rate(
        self, underlying: str, instrument_type: str
    ) -> Decimal:
        self.calls.append("funding_rate")
        assert (underlying, instrument_type) == ("XAU", "perpetual_rwa_future")
        rate = self._configured(self.xau_rate, "get_funding_rate")
        assert isinstance(rate, Decimal)
        return rate

    async def raw(self, path: str) -> object:
        self.calls.append("transfers")
        assert path == "/transfers?limit=100&offset=0"
        rows = self._configured(self.transfers, "raw")
        assert isinstance(rows, list)
        return {"result": rows, "pagination": {"object_count": len(rows)}}


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _paths(
    tmp_path: Path,
    *,
    heartbeat_age: timedelta | None = timedelta(minutes=5),
    state: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    heartbeat_path = tmp_path / "heartbeat.json"
    state_path = tmp_path / "state.json"
    if heartbeat_age is not None:
        _write_json(
            heartbeat_path,
            {"timestamp": (NOW - heartbeat_age).isoformat()},
        )
    if state is not None:
        _write_json(state_path, state)
    return heartbeat_path, state_path


def _client(**overrides: object) -> StrictClient:
    values: dict[str, object] = {
        "positions": {"XAUS": Decimal("0.0125"), "XAU": Decimal("-0.0125")},
        "metadata": _metadata(),
        "liquidations": {
            "XAUS": (Decimal("4000"), Decimal("3000")),
            "XAU": (Decimal("4000"), Decimal("4500")),
        },
        "xaus_rate": Decimal("-0.05"),
        "xau_rate": Decimal("0.132"),
        "transfers": _transfers(),
    }
    values.update(overrides)
    return StrictClient(**values)


def _collect(tmp_path: Path, client: StrictClient, **path_overrides: object):
    from panel.providers.swap_carry import collect

    heartbeat_path, state_path = _paths(tmp_path, **path_overrides)
    return collect(
        client=client,
        heartbeat_path=heartbeat_path,
        state_path=state_path,
        now=NOW,
    )


def _metrics(status) -> dict[str, object]:
    return {metric.label: metric for metric in status.metrics}


def _alert_keys(status) -> set[str]:
    return {alert.key for alert in status.alerts}


def test_normal_position_reports_all_metrics_and_weekly_actual_funding(tmp_path) -> None:
    """正常双腿必须完整显示，且本周资金费排除上周流水。"""
    status = _collect(tmp_path, _client())
    metrics = _metrics(status)

    assert status.name == "Swap Carry（XAUS/XAU）"
    assert status.alive is True
    assert status.error is None
    assert status.summary == "持仓中，净 carry +8.2%/年"
    assert (metrics["XAUS 多腿"].value, metrics["XAUS 多腿"].tone) == (
        "+0.01250 / $50.00",
        "normal",
    )
    assert metrics["XAU 空腿"].value == "-0.01250 / $50.00"
    assert (metrics["净 delta"].value, metrics["净 delta"].tone) == (
        "+0.00000",
        "good",
    )
    assert (metrics["净 carry 年化"].value, metrics["净 carry 年化"].tone) == (
        "+8.2%",
        "good",
    )
    assert "强平价=3000" in metrics["XAUS 强平"].value
    assert "距离=25.00%" in metrics["XAUS 强平"].value
    assert "强平价=4500" in metrics["XAU 强平"].value
    assert "可交易" in metrics["XAUS 时段"].value
    assert "距下次休市=6小时30分钟" in metrics["XAUS 时段"].value
    assert "下次开市=09-08 22:00 UTC" in metrics["XAUS 时段"].value
    assert metrics["守护进程心跳"].value == "上次运行于 5.0 分钟前"
    assert metrics["本周已结算资金费"].value == (
        "XAUS -1.25 / XAU +1.75 / 合计 +0.50 USDC"
    )
    assert status.alerts == []


def test_flat_position_displays_normally_without_liquidation_calls(tmp_path) -> None:
    client = _client(
        positions={"XAUS": Decimal("0"), "XAU": Decimal("0")},
        liquidations=None,
    )

    status = _collect(tmp_path, client)

    assert status.error is None
    assert status.summary == "空仓，可交易"
    assert _metrics(status)["XAUS 强平"].value == "无持仓"
    assert _metrics(status)["XAU 强平"].value == "无持仓"
    assert not any(call.startswith("liquidation:") for call in client.calls)


def test_stale_heartbeat_marks_dead_and_emits_critical_alert(tmp_path) -> None:
    status = _collect(
        tmp_path,
        _client(),
        heartbeat_age=timedelta(minutes=16),
    )

    assert status.alive is False
    alert = next(a for a in status.alerts if a.key == "swap_carry_heartbeat_stale")
    assert alert.level == "critical"
    assert alert.action.strip()
    assert _metrics(status)["守护进程心跳"].tone == "bad"


def test_missing_heartbeat_degrades_without_raising(tmp_path) -> None:
    status = _collect(tmp_path, _client(), heartbeat_age=None)

    assert status.alive is None
    assert status.error is None
    assert _metrics(status)["守护进程心跳"].value == "无数据"


def test_single_leg_emits_critical_alert(tmp_path) -> None:
    status = _collect(
        tmp_path,
        _client(positions={"XAUS": Decimal("0.0125"), "XAU": Decimal("0")}),
    )

    alert = next(a for a in status.alerts if a.key == "swap_carry_single_leg")
    assert alert.level == "critical"
    assert "只剩 XAUS" in alert.title
    assert alert.action.strip()


def test_liquidation_failure_only_degrades_that_metric(tmp_path) -> None:
    client = _client(
        liquidations={
            "XAUS": RuntimeError("强平接口暂不可用"),
            "XAU": (Decimal("4000"), Decimal("4500")),
        }
    )

    status = _collect(tmp_path, client)

    assert status.error is None
    assert _metrics(status)["XAUS 强平"].value == "无数据"
    assert "强平价=4500" in _metrics(status)["XAU 强平"].value
    assert _metrics(status)["净 carry 年化"].value == "+8.2%"


def test_negative_carry_emits_warning_alert(tmp_path) -> None:
    status = _collect(
        tmp_path,
        _client(xaus_rate=Decimal("-0.08"), xau_rate=Decimal("0.03")),
    )

    assert (_metrics(status)["净 carry 年化"].value, _metrics(status)["净 carry 年化"].tone) == (
        "-5.0%",
        "bad",
    )
    alert = next(a for a in status.alerts if a.key == "swap_carry_negative_carry")
    assert alert.level == "warning"
    assert alert.action.strip()


def test_core_account_read_failure_returns_error_card(tmp_path) -> None:
    status = _collect(
        tmp_path,
        _client(positions=RuntimeError("账户读取失败")),
    )

    assert status.alive is None
    assert status.summary == "采集失败"
    assert status.error is not None and "账户读取失败" in status.error


def test_near_xaus_liquidation_emits_critical_alert(tmp_path) -> None:
    status = _collect(
        tmp_path,
        _client(
            liquidations={
                "XAUS": (Decimal("4000"), Decimal("3960")),
                "XAU": (Decimal("4000"), Decimal("4500")),
            }
        ),
    )

    alert = next(a for a in status.alerts if a.key == "swap_carry_xaus_liquidation")
    assert alert.level == "critical"
    assert alert.action.strip()
    assert _metrics(status)["XAUS 强平"].tone == "bad"


def test_guard_region_or_session_failure_emits_critical_alert(tmp_path) -> None:
    for message in ("地区封锁，无法平仓", "会话失效，Cookie 已过期"):
        status = _collect(
            tmp_path,
            _client(),
            state={
                "status": "action_failed",
                "message": message,
                "consecutive_failures": 2,
            },
        )
        alert = next(a for a in status.alerts if a.key == "swap_carry_guard_blocked")
        assert alert.level == "critical"
        assert message in alert.title
        assert alert.action.strip()


def test_proxy_environment_is_removed_before_first_network_call(
    tmp_path, monkeypatch
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1080")

    class ProxyCheckingClient(StrictClient):
        async def get_position(
            self, underlying: str, *, exact: bool = False
        ) -> Position:
            assert not any(
                name in os.environ
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
            )
            return await super().get_position(underlying, exact=exact)

    defaults = _client()
    client = ProxyCheckingClient(
        positions=defaults.positions,
        metadata=defaults.metadata,
        liquidations=defaults.liquidations,
        xaus_rate=defaults.xaus_rate,
        xau_rate=defaults.xau_rate,
        transfers=defaults.transfers,
    )

    status = _collect(tmp_path, client)

    assert status.error is None


def test_provider_source_contains_no_order_calls() -> None:
    source = (
        Path(__file__).parents[1] / "panel" / "providers" / "swap_carry.py"
    ).read_text(encoding="utf-8")

    for forbidden in ("request_quote", "accept_quote", "cmd_open", "cmd_close"):
        assert forbidden not in source
