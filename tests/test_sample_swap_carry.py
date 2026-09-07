"""只读 swap carry 采样器测试。"""

from __future__ import annotations

import asyncio
import inspect
import json
import plistlib
import sys
from types import SimpleNamespace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from adapters.variational_client import SwapFundingPeriod, SwapFundingRate, SwapFundingSnapshot
from tools import sample_swap_carry


NOW = datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc)
SESSIONS = [
    {"open": "2026-09-02T22:00:00Z", "close": "2026-09-03T21:00:00Z"},
    {"open": "2026-09-03T22:00:00Z", "close": "2026-09-04T21:00:00Z"},
    {"open": "2026-09-06T22:00:00Z", "close": "2026-09-07T18:30:00Z"},
]
METADATA = {
    "XAU": [
        {
            "asset": "XAU",
            "asset_class": "commodity",
            "funding_interval_s": 14400,
            "index_price": "4436.03",
            "instrument_type": "perpetual_rwa_future",
            "open_interest": {
                "long_open_interest": "25600000.123",
                "short_open_interest": "29200000.456",
            },
            "price": "4438.64",
        }
    ],
    "XAUS": [
        {
            "asset": "XAUS",
            "asset_class": "commodity",
            "funding_interval_s": 0,
            "index_price": "4435.04",
            "instrument_type": "swap",
            "market_status": "open",
            "open_interest": {
                "long_open_interest": "27700000.123",
                "short_open_interest": "9100000.456",
            },
            "price": "4435.04",
            "trading_schedule": {
                "next_open_at": "2026-09-06T22:00:00Z",
                "next_close_at": "2026-09-04T21:00:00Z",
            },
            "trading_sessions": SESSIONS,
        }
    ],
}


def _rate(raw: str, apply_time: datetime, coverage_days: int | None) -> SwapFundingRate:
    return SwapFundingRate(
        raw_rate=Decimal(raw),
        coverage_days=coverage_days,
        day_count_basis=365,
        normalized_annual_rate=Decimal(raw),
        apply_time=apply_time,
        observed_at=NOW,
    )


SWAP_FUNDING = SwapFundingSnapshot(
    upcoming=SwapFundingPeriod(
        trade_date="2026-09-04",
        basis="rates",
        long_rate=_rate("-0.057214", datetime(2026, 9, 4, 21, 5, tzinfo=timezone.utc), 1),
        short_rate=_rate("0.025031", datetime(2026, 9, 4, 21, 5, tzinfo=timezone.utc), 1),
    ),
    latest_applied=SwapFundingPeriod(
        trade_date="2026-09-03",
        basis="rates",
        long_rate=_rate("-0.056914", datetime(2026, 9, 3, 21, 5, tzinfo=timezone.utc), None),
        short_rate=_rate("0.024731", datetime(2026, 9, 3, 21, 5, tzinfo=timezone.utc), None),
    ),
    observed_at=NOW,
    warnings=(),
)


class StrictFakeClient:
    """只有显式配置的调用才成功，避免测试桩把遗漏路径放过。"""

    def __init__(self, configured: dict[str, object]) -> None:
        self.configured = configured
        self.calls: list[tuple[str, object]] = []

    async def _result(self, key: str, detail: object = None) -> object:
        self.calls.append((key, detail))
        if key not in self.configured:
            raise AssertionError(f"未配置的假客户端调用：{key}")
        value = self.configured[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def get_supported_assets(self) -> object:
        return await self._result("metadata")

    async def get_funding(self, underlying: str, instrument_type: str) -> object:
        return await self._result("xau_funding", (underlying, instrument_type))

    async def get_swap_funding(self, underlying: str) -> object:
        return await self._result("xaus_funding", underlying)

    async def request_quote(
        self,
        underlying: str,
        side: str,
        qty: Decimal,
        **instrument: object,
    ) -> object:
        return await self._result(
            f"quote_{underlying.lower()}",
            (underlying, side, qty, instrument),
        )

    async def close(self) -> None:
        await self._result("close")


def _configured_client() -> StrictFakeClient:
    return StrictFakeClient(
        {
            "metadata": METADATA,
            "xau_funding": {
                "predicted_funding_rate": "0.1028881234567890123456789",
                "funding_interval_s": 14400,
                "next_funding_time": "2026-09-05T00:00:00Z",
            },
            "xaus_funding": SWAP_FUNDING,
            "quote_xau": {"bid": "4437.955", "ask": "4439.325"},
            "quote_xaus": {"bid": "4434.740", "ask": "4435.340"},
            "close": None,
        }
    )


def test_sample_preserves_full_rate_precision_and_records_all_evidence() -> None:
    """一轮采样应完整保存费率、OI、基差、RFQ、时段与两种收益。"""
    client = _configured_client()

    record = asyncio.run(
        sample_swap_carry.sample_once(
            client,
            observed_at=NOW,
            qty=Decimal("0.224"),
            hold_ratio=Decimal("0.705"),
            swap_ratio=Decimal(4) / Decimal(7),
        )
    )

    assert record["observed_at"] == "2026-09-04T20:00:00Z"
    assert record["xau"]["funding"]["raw_rate"] == "0.1028881234567890123456789"
    assert record["xau"]["funding"]["annual_percent"] == "10.2888123456789012345678900"
    assert record["xau"]["funding"]["funding_interval_s"] == 14400
    assert record["xaus"]["funding"]["long_rate"]["raw_rate"] == "-0.057214"
    assert record["xaus"]["funding"]["long_rate"]["coverage_days"] == 1
    assert record["xau"]["open_interest"]["short"] == "29200000.456"
    assert record["xaus"]["open_interest"]["long"] == "27700000.123"
    assert record["basis"]["mark"]["absolute"] == "3.60"
    assert record["basis"]["index"]["absolute"] == "0.99"
    assert record["rfq"]["qty"] == "0.224"
    assert record["rfq"]["xau"]["bid"] == "4437.955"
    assert record["rfq"]["xaus"]["ask"] == "4435.340"
    assert record["xaus"]["market"]["market_status"] == "open"
    assert record["xaus"]["market"]["schedule"]["is_tradable"] is True
    assert record["xaus"]["market"]["metadata_freshness"]["is_fresh"] is True
    assert set(record["carry"]) >= {"weekly_flat", "hold_through"}
    assert record["errors"] == []


def test_one_side_failure_is_recorded_without_dropping_other_evidence() -> None:
    """XAU 读取失败时，XAUS 费率和整轮时间戳仍须落盘。"""
    client = StrictFakeClient(
        {
            "metadata": METADATA,
            "xau_funding": RuntimeError("XAU funding 暂不可用"),
            "xaus_funding": SWAP_FUNDING,
            "quote_xau": RuntimeError("XAU RFQ 暂不可用"),
            "quote_xaus": {"bid": "4434.740", "ask": "4435.340"},
        }
    )

    record = asyncio.run(
        sample_swap_carry.sample_once(
            client,
            observed_at=NOW,
            qty=Decimal("0.224"),
            hold_ratio=Decimal("0.705"),
            swap_ratio=Decimal(4) / Decimal(7),
        )
    )

    assert record["observed_at"] == "2026-09-04T20:00:00Z"
    assert record["xau"]["funding"] is None
    assert record["xaus"]["funding"]["long_rate"]["raw_rate"] == "-0.057214"
    assert record["rfq"]["xau"] is None
    assert record["rfq"]["xaus"]["bid"] == "4434.740"
    assert {error["source"] for error in record["errors"]} == {
        "xau_funding",
        "xau_rfq",
    }


def test_append_jsonl_appends_complete_lines(tmp_path: Path) -> None:
    """采样结果使用追加模式，每轮独占一行。"""
    path = tmp_path / "swap.jsonl"
    sample_swap_carry.append_jsonl(path, {"round": 1})
    sample_swap_carry.append_jsonl(path, {"round": 2})

    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [
        {"round": 1},
        {"round": 2},
    ]


def test_sampler_source_contains_no_execution_call() -> None:
    """取证采样器不得出现成交接口调用。"""
    source = inspect.getsource(sample_swap_carry)
    assert ".market_order(" not in source
    assert ".accept_quote(" not in source
    assert "/quotes/accept" not in source


def test_parser_defaults_to_once_and_hourly_interval() -> None:
    """命令行默认单次执行，也允许显式切换循环间隔。"""
    parser = sample_swap_carry.build_parser()
    defaults = parser.parse_args([])
    loop = parser.parse_args(["--interval-seconds", "15"])

    assert defaults.once is True
    assert defaults.interval_seconds == 3600
    assert loop.once is False
    assert loop.interval_seconds == 15


def test_environment_loader_removes_proxies_before_and_after_dotenv(monkeypatch) -> None:
    """代理变量必须在 dotenv 前先清一次，并清掉 dotenv 重新载入的值。"""
    environment = {
        "HTTP_PROXY": "http://127.0.0.1:1080",
        "VARIATIONAL_COOKIE": "保留",
    }

    def load_dotenv() -> None:
        assert "HTTP_PROXY" not in environment
        environment["all_proxy"] = "socks5://127.0.0.1:1080"

    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=load_dotenv))

    removed = sample_swap_carry._load_environment_without_proxy(environment)

    assert removed == ("HTTP_PROXY", "all_proxy")
    assert environment == {"VARIATIONAL_COOKIE": "保留"}


def test_launchd_template_runs_readonly_sampler_hourly() -> None:
    """launchd 模板只调 --once，由 StartInterval 每小时拉起。"""
    path = Path("deploy/com.variational.swap-carry-sampler.plist")
    payload = plistlib.loads(path.read_bytes())

    assert payload["StartInterval"] == 3600
    assert "tools.sample_swap_carry" in payload["ProgramArguments"]
    assert "--once" in payload["ProgramArguments"]
    assert "KeepAlive" not in payload


def test_sample_keeps_metadata_price_when_rfq_closed():
    """休市 RFQ 失败时仍保存真实 supported_assets.price，供结算名义估计。"""
    client = _configured_client()
    client.configured['quote_xaus'] = RuntimeError('休市无 RFQ')
    record = asyncio.run(sample_swap_carry.sample_once(client, observed_at=NOW))
    assert record['xaus']['mark_price'] == METADATA['XAUS'][0]['price']
    assert record['rfq']['xaus'] is None
