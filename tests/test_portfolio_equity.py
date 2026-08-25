"""组合级权益记录器测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal

from tools import portfolio_equity


class FakeHyperliquidClient:
    """返回固定平台组合历史的只读客户端。"""

    class Info:
        """模拟 Hyperliquid 官方 Info 客户端。"""

        def portfolio(self, user: str) -> list:
            """返回 allTime 平台累计盈亏。"""
            assert user == "0xaccount"
            return [
                ["day", {"pnlHistory": [[1000, "1.25"]], "vlm": "10"}],
                [
                    "allTime",
                    {
                        "pnlHistory": [
                            [1000, "-10.000000000000000001"],
                            [2000, "-12.345678901234567891"],
                        ],
                        "accountValueHistory": [
                            [1000, "557.34"],
                            [2000, "552.34"],
                        ],
                        "vlm": "123456.78",
                    },
                ],
            ]

    def _require_info(self) -> "FakeHyperliquidClient.Info":
        """返回模拟官方读取客户端。"""
        return self.Info()

    def _require_account_address(self) -> str:
        """返回公开账户地址。"""
        return "0xaccount"

    async def _user_state(self) -> dict:
        """旧的自算权益分支不得再被调用。"""
        raise AssertionError("不得读取 spot 或未实现盈亏拼装累计值")

    async def _spot_user_state(self) -> dict:
        """旧的 Spot 权益分支不得再被调用。"""
        raise AssertionError("不得读取 spot 或未实现盈亏拼装累计值")


class FakeLighterClient:
    """返回固定平台累计盈亏的只读 Lighter 客户端。"""

    def __init__(self) -> None:
        """记录每次请求使用的短期 token。"""
        self.auth_count = 0
        self.headers: list[dict[str, str]] = []

    def _require_account_index(self) -> int:
        """返回测试账户索引。"""
        return 7

    def _auth_headers(self) -> dict[str, str]:
        """每次调用生成不同 token，模拟十分钟有效期。"""
        self.auth_count += 1
        return {"Authorization": f"token-{self.auth_count}"}

    async def _get_json(
        self,
        path: str,
        *,
        params: dict,
        headers: dict[str, str],
    ) -> dict:
        """返回平台 PnL 历史并记录鉴权头。"""
        assert path == "/api/v1/pnl"
        assert params["by"] == "index"
        assert params["value"] == "7"
        assert params["resolution"] == "1h"
        # 窗口必须有界：start=0 会被接口以 400 拒绝（实测）。
        start = int(params["start_timestamp"])
        end = int(params["end_timestamp"])
        assert start > 0, "start_timestamp 不能为 0"
        assert 0 < end - start <= 7 * 24 * 60 * 60 * 1000, "窗口须落在 7 天内"
        assert params["count_back"] == "200"
        self.headers.append(headers)
        return {
            "code": 200,
            "resolution": "1h",
            "pnl": [
                {
                    "timestamp": 1000,
                    "trade_pnl": "-400.000000",
                    "volume": "1000",
                    "inflow": "967.789164",
                    "outflow": "0",
                    "total_asset_value": "567.789164",
                },
                {
                    "timestamp": 2000,
                    "trade_pnl": "-436.500513",
                    "volume": "2000",
                    "inflow": "967.789164",
                    "outflow": "0",
                    "total_asset_value": "531.288651",
                },
            ],
        }

    def _raise_api_error(self, data: dict, *, context: str) -> None:
        """测试响应固定成功。"""
        assert context == "累计盈亏查询"


class FakeVariationalClient:
    """返回固定余额及持仓的只读 Variational 客户端。"""

    async def raw(self, path: str) -> dict:
        """返回不含筛选逻辑的账户余额。"""
        assert path == "/portfolio"
        return {"balance": "552.340000000000000001", "upnl": "95"}  # balance 已含 upnl

    async def get_positions(self) -> list[dict]:
        """返回策略 BTC 仓位和人工 SOL 仓位。"""
        return [
            {
                "position_info": {"instrument": {"underlying": "BTC"}},
                "upnl": "-5.000000000000000002",
            },
            {
                "position_info": {"instrument": {"underlying": "SOL"}},
                "upnl": "100",
            },
        ]


def test_hyperliquid_uses_latest_platform_pnl_instead_of_spot_formula() -> None:
    """Hyperliquid 必须取 allTime pnlHistory 末值，不得拼装 Spot 权益。"""
    cumulative_pnl = asyncio.run(
        portfolio_equity.read_hyperliquid_equity(
            FakeHyperliquidClient(),
            ("BTC",),
        )
    )

    assert isinstance(cumulative_pnl, Decimal)
    assert cumulative_pnl == Decimal("-12.345678901234567891")
    assert cumulative_pnl != Decimal("552.34"), "不得把 Spot 余额当累计盈亏"
    assert cumulative_pnl != Decimal("552.34") + Decimal("-5.00"), (
        "不得用 Spot 加未实现盈亏拼装累计值"
    )


def test_lighter_uses_trade_pnl_and_platform_equity_identity() -> None:
    """Lighter 必须取 trade_pnl，并钉住平台已验证的权益恒等式。"""
    client = FakeLighterClient()
    cumulative_pnl = asyncio.run(
        portfolio_equity.read_lighter_equity(client, ("BTC",))
    )
    latest = asyncio.run(
        client._get_json(
            "/api/v1/pnl",
            params={
                "by": "index",
                "value": "7",
                "resolution": "1h",
                "start_timestamp": "1",
                "end_timestamp": "2",
                "count_back": "200",
            },
            headers={"authorization": "test-only"},
        )
    )["pnl"][-1]

    assert isinstance(cumulative_pnl, Decimal)
    assert cumulative_pnl == Decimal("-436.500513")
    assert (
        Decimal(latest["inflow"])
        - Decimal(latest["outflow"])
        + Decimal(latest["trade_pnl"])
        == Decimal(latest["total_asset_value"])
    )


def test_lighter_generates_fresh_auth_token_before_every_fetch() -> None:
    """连续取数必须生成新 token，不能复用已经可能过期的请求头。"""
    client = FakeLighterClient()

    asyncio.run(portfolio_equity.read_lighter_equity(client, ("BTC",)))
    asyncio.run(portfolio_equity.read_lighter_equity(client, ("BTC",)))

    assert client.headers == [
        {"authorization": "token-1"},
        {"authorization": "token-2"},
    ]


def test_variational_equity_excludes_manual_symbol_without_float_conversion() -> None:
    """Variational 权益 = balance 减去非策略币种浮盈亏，且保持 Decimal 精度。

    balance **本身已含**未实现（实测 Δbalance == Δupnl），
    早期实现写成 balance + upnl，把未实现算了两遍，
    误差随行情摆动，在面板上表现为「越刷亏损越大」的假象。
    """
    equity = asyncio.run(
        portfolio_equity.read_variational_equity(
            FakeVariationalClient(),
            ("BTC",),
        )
    )

    assert isinstance(equity, Decimal)
    # balance 552.34 已含 BTC(-5) 与 SOL(+100)；只需减掉人工的 SOL。
    assert equity == Decimal("552.340000000000000001") - Decimal("100")
    assert equity != Decimal("552.340000000000000001") + Decimal(
        "-5.000000000000000002"
    ), "不得再叠加策略币种浮盈亏"
    assert equity != Decimal("552.340000000000000001"), "不得忽略人工币种"


def test_strategy_symbols_are_derived_from_volume_instance_configs() -> None:
    """账户币种集合必须随成交量实例配置变化，不维护第二份固定表。"""
    instances = (
        portfolio_equity.VolumeInstanceConfig(
            key="first",
            heartbeat_path=portfolio_equity.PROJECT_ROOT / "first.jsonl",
            symbol="doge",
            primary_source="lighter",
            hedge_source="hyperliquid",
        ),
        portfolio_equity.VolumeInstanceConfig(
            key="second",
            heartbeat_path=portfolio_equity.PROJECT_ROOT / "second.jsonl",
            symbol="eth",
            primary_source="variational",
            hedge_source="lighter",
        ),
    )

    assert portfolio_equity.strategy_symbols_by_source(instances) == {
        "hyperliquid": ("DOGE",),
        "lighter": ("DOGE", "ETH"),
        "variational": ("ETH",),
    }


def test_failed_account_is_skipped_annotated_and_record_is_still_written(
    tmp_path,
) -> None:
    """单个账户读取失败时仍写部分记录，且单次进程正常返回。"""

    # 币种集合由 DEFAULT_VOLUME_INSTANCES 推导，会随配对调整变化，
    # 因此断言「与推导结果一致」而不是写死某组币种。
    expected = portfolio_equity.strategy_symbols_by_source()

    async def lighter_reader(symbols: tuple[str, ...]) -> Decimal:
        assert symbols == expected["lighter"]
        return Decimal("561.49")

    async def variational_reader(symbols: tuple[str, ...]) -> Decimal:
        assert symbols == expected["variational"]
        raise RuntimeError("临时不可用")

    async def hyperliquid_reader(symbols: tuple[str, ...]) -> Decimal:
        return Decimal("572.20")

    output = tmp_path / "portfolio_equity.jsonl"
    snapshot = asyncio.run(
        portfolio_equity.record_once(
            {
                "lighter": lighter_reader,
                "variational": variational_reader,
                "hyperliquid_var": hyperliquid_reader,
            },
            output,
            timestamp=1787590000.0,
        )
    )

    written = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot == written
    assert written["schema"] == 4
    assert written["symbols"]["lighter"] == list(expected["lighter"])
    assert written["accounts"] == {
        "lighter": "561.49",
        "hyperliquid_var": "572.20",
    }
    assert written["sources"] == {
        "lighter": "platform",
        "variational": "computed",
        "hyperliquid_var": "platform",
    }
    assert "total_equity" not in written, "不同口径的绝对值不得直接相加"
    assert "variational" not in written["accounts"]
    assert written["errors"]["variational"].endswith("临时不可用")


def test_cli_defaults_to_fifteen_minutes_and_two_hyperliquid_prefixes() -> None:
    """命令行默认值对应长期记录路径和两个实盘账户前缀。"""
    args = portfolio_equity.parse_args([])

    assert args.interval == 900
    assert args.once is False
    assert args.output == portfolio_equity.DEFAULT_OUTPUT
    assert args.volume_output == portfolio_equity.DEFAULT_VOLUME_OUTPUT
    assert args.hyperliquid_prefix == "HYPERLIQUID"
    assert args.hyperliquid_var_prefix == "HYPERLIQUID_VAR"
    assert set(portfolio_equity.build_default_volume_readers()) == {
        "lighter",
        "hyperliquid",
        "hyperliquid_var",
        "variational",
    }


def test_hyperliquid_volume_filters_start_and_instance_symbol() -> None:
    """Hyperliquid 只累计起点之后且属于本实例币种的成交。"""
    calls: list[int] = []
    pages = {
        1000: [
            {"coin": "BTC", "sz": "9", "px": "100", "time": 999},
            {"coin": "@166", "sz": "8", "px": "100", "time": 1000},
            {"coin": "XYZ:CXMT", "sz": "7", "px": "100", "time": 1001},
            {"coin": "XYZ:SP500", "sz": "6", "px": "100", "time": 1002},
            {"coin": "SOL", "sz": "5", "px": "100", "time": 1003},
            {"coin": "BTC", "sz": "0.1", "px": "100", "time": 1004},
        ],
        1005: [],
    }

    async def fetch_page(start_time: int) -> object:
        calls.append(start_time)
        return pages[start_time]

    amount = asyncio.run(
        portfolio_equity.read_hyperliquid_volume(
            fetch_page,
            start_time_ms=1000,
            symbol="BTC",
        )
    )

    assert calls == [1000, 1005]
    assert amount == Decimal("10.0")


def test_lighter_volume_uses_usd_amount_and_fresh_auth_per_page() -> None:
    """Lighter 成交额直接取 usd_amount，翻页时也不得复用 token。"""

    class Client:
        """模拟带游标的 Lighter 成交接口。"""

        def __init__(self) -> None:
            self.auth_count = 0
            self.requests: list[tuple[dict, dict]] = []

        def _require_account_index(self) -> int:
            """返回账户索引。"""
            return 7

        def _auth_headers(self) -> dict[str, str]:
            """为每页生成不同 token。"""
            self.auth_count += 1
            return {"Authorization": f"token-{self.auth_count}"}

        def _raise_api_error(self, data: dict, *, context: str) -> None:
            """测试响应固定成功。"""
            assert data["code"] == 200
            assert context == "成交查询"

        async def _get_json(
            self,
            path: str,
            *,
            params: dict,
            headers: dict,
        ) -> dict:
            """返回两页成交，其中 size×price 故意不等于 usd_amount。"""
            assert path == "/api/v1/trades"
            self.requests.append((params, headers))
            if "cursor" not in params:
                return {
                    "code": 200,
                    "next_cursor": "page-2",
                    "trades": [
                        {
                            "timestamp": 2000,
                            "market_id": 1,
                            "size": "999",
                            "price": "999",
                            "usd_amount": "10.000000000000000001",
                            "ask_account_id": 70,
                            "bid_account_id": 80,
                        },
                        {
                            "timestamp": 1999,
                            "market_id": 2,
                            "size": "1",
                            "price": "500",
                            "usd_amount": "500",
                            "ask_account_id": 70,
                            "bid_account_id": 80,
                        },
                    ],
                }
            return {
                "code": 200,
                "next_cursor": "",
                "trades": [
                    {
                        "timestamp": 1500,
                        "market_id": 1,
                        "size": "888",
                        "price": "888",
                        "usd_amount": "20.000000000000000002",
                        "ask_account_id": 70,
                        "bid_account_id": 80,
                    },
                    {
                        "timestamp": 999,
                        "market_id": 1,
                        "size": "777",
                        "price": "777",
                        "usd_amount": "30",
                        "ask_account_id": 70,
                        "bid_account_id": 80,
                    },
                ],
            }

    client = Client()
    amount = asyncio.run(
        portfolio_equity.read_lighter_volume(
            client,
            start_time_ms=1000,
            market_id=1,
        )
    )

    assert amount == Decimal("30.000000000000000003")
    assert isinstance(amount, Decimal)
    assert [request[1] for request in client.requests] == [
        {"authorization": "token-1"},
        {"authorization": "token-2"},
    ]
    assert client.requests[0][0] == {
        "sort_by": "timestamp",
        "sort_dir": "desc",
        "limit": "100",
        "account_index": "7",
    }
    assert client.requests[1][0]["cursor"] == "page-2"


def test_hyperliquid_volume_follows_time_cursor_until_all_pages_are_read() -> None:
    """Hyperliquid 成交超过单页上限时必须推进时间游标取全。"""
    calls: list[int] = []
    pages = {
        2000: [
            {"coin": "BTC", "sz": "0.1", "px": "100", "time": 2000},
            {"coin": "BTC", "sz": "0.2", "px": "100", "time": 2001},
        ],
        2002: [
            {"coin": "BTC", "sz": "0.3", "px": "100", "time": 2002},
        ],
        2003: [],
    }

    async def fetch_page(start_time: int) -> object:
        calls.append(start_time)
        return pages[start_time]

    amount = asyncio.run(
        portfolio_equity.read_hyperliquid_volume(
            fetch_page,
            start_time_ms=2000,
            symbol="BTC",
        )
    )

    assert calls == [2000, 2002, 2003]
    assert amount == Decimal("60.0")


def test_variational_volume_paginates_and_filters_start_and_symbol() -> None:
    """Variational 必须翻页取全，再按起点和本实例 underlying 过滤。"""
    calls: list[tuple[int | None, int | None]] = []
    pages = {
        (None, None): {
            "pagination": {
                "object_count": 3,
                "next_page": {"limit": 2, "offset": 2},
            },
            "result": [
                {
                    "instrument": {"underlying": "BTC"},
                    "qty": "-9",
                    "price": "100",
                    "created_at": "2026-08-24T00:00:00Z",
                },
                {
                    "instrument": {"underlying": "ETH"},
                    "qty": "8",
                    "price": "100",
                    "created_at": "2026-08-24T00:01:00Z",
                },
            ],
        },
        (2, 2): {
            "pagination": {"object_count": 3, "next_page": None},
            "result": [
                {
                    "instrument": {"underlying": "BTC"},
                    "qty": "0.2",
                    "price": "100",
                    "created_at": "2026-08-24T00:02:00Z",
                }
            ],
        },
    }

    async def fetch_page(limit: int | None, offset: int | None) -> object:
        calls.append((limit, offset))
        return pages[(limit, offset)]

    amount = asyncio.run(
        portfolio_equity.read_variational_volume(
            fetch_page,
            start_time=Decimal("1787529660"),
            symbol="BTC",
        )
    )

    assert calls == [(None, None), (2, 2)]
    assert amount == Decimal("20.0")


def test_volume_amounts_keep_decimal_precision_without_float_rounding() -> None:
    """成交额乘加必须全程保持 Decimal 精度。"""

    async def fetch_page(start_time: int) -> object:
        if start_time == 0:
            return [
                {
                    "coin": "BTC",
                    "sz": "0.1000000000000000001",
                    "px": "3",
                    "time": 1000,
                }
            ]
        return []

    amount = asyncio.run(
        portfolio_equity.read_hyperliquid_volume(
            fetch_page,
            start_time_ms=0,
            symbol="BTC",
        )
    )

    assert amount == Decimal("0.3000000000000000003")
    assert isinstance(amount, Decimal)


def test_volume_snapshot_uses_each_platform_reader_without_estimates(
    tmp_path,
) -> None:
    """每实例从心跳首行起算，Lighter 必须走自己的实测成交接口。"""
    starts = {
        "lighter_entropy": Decimal("1787383380.25"),
        "variational_entropy": Decimal("1787475540.5"),
        "lighter_variational_eth": Decimal("1787578800.75"),
    }
    configs = []
    for key, symbol, primary, hedge in (
        ("lighter_entropy", "BTC", "lighter", "hyperliquid"),
        ("variational_entropy", "BTC", "variational", "hyperliquid_var"),
        ("lighter_variational_eth", "ETH", "lighter", "variational"),
    ):
        heartbeat_path = tmp_path / f"{key}.jsonl"
        heartbeat_path.write_text(
            json.dumps({"ts": str(starts[key])})
            + "\n"
            + "{后续行故意不是 JSON，绝不能读取}\n",
            encoding="utf-8",
        )
        configs.append(
            portfolio_equity.VolumeInstanceConfig(
                key=key,
                heartbeat_path=heartbeat_path,
                symbol=symbol,
                primary_source=primary,
                hedge_source=hedge,
            )
        )

    calls: list[tuple[str, Decimal, str]] = []

    async def lighter_reader(since: Decimal, symbol: str) -> Decimal:
        calls.append(("lighter", since, symbol))
        return Decimal("111") if symbol == "BTC" else Decimal("50")

    async def hyperliquid_reader(since: Decimal, symbol: str) -> Decimal:
        calls.append(("hyperliquid", since, symbol))
        return Decimal("100")

    async def hyperliquid_var_reader(since: Decimal, symbol: str) -> Decimal:
        calls.append(("hyperliquid_var", since, symbol))
        return Decimal("200")

    async def variational_reader(since: Decimal, symbol: str) -> Decimal:
        calls.append(("variational", since, symbol))
        return Decimal("300") if symbol == "BTC" else Decimal("40")

    snapshot = asyncio.run(
        portfolio_equity.collect_volume_snapshot(
            {
                "lighter": lighter_reader,
                "hyperliquid": hyperliquid_reader,
                "hyperliquid_var": hyperliquid_var_reader,
                "variational": variational_reader,
            },
            instances=tuple(configs),
            timestamp=1787590000.0,
        )
    )

    # 首条心跳没有 due_at 时，起点回退一个安全余量——心跳落盘晚于成交。
    margin = portfolio_equity.HEARTBEAT_START_MARGIN_SECONDS
    expected_since = {key: value - margin for key, value in starts.items()}

    assert calls == [
        ("lighter", expected_since["lighter_entropy"], "BTC"),
        ("hyperliquid", expected_since["lighter_entropy"], "BTC"),
        ("variational", expected_since["variational_entropy"], "BTC"),
        ("hyperliquid_var", expected_since["variational_entropy"], "BTC"),
        ("lighter", expected_since["lighter_variational_eth"], "ETH"),
        ("variational", expected_since["lighter_variational_eth"], "ETH"),
    ]
    assert snapshot["instances"] == {
        "lighter_entropy": {
            "symbol": "BTC",
            "since": float(expected_since["lighter_entropy"]),
            "primary": "111",
            "hedge": "100",
        },
        "variational_entropy": {
            "symbol": "BTC",
            "since": float(expected_since["variational_entropy"]),
            "primary": "300",
            "hedge": "200",
        },
        "lighter_variational_eth": {
            "symbol": "ETH",
            "since": float(expected_since["lighter_variational_eth"]),
            "primary": "50",
            "hedge": "40",
        },
    }
    assert snapshot["totals_by_symbol"] == {"BTC": "711", "ETH": "90"}


def test_failed_volume_source_is_skipped_and_partial_record_is_written(
    tmp_path,
) -> None:
    """单个成交来源失败时仍写入其他来源，单次进程正常返回。"""
    heartbeat_a = tmp_path / "a.jsonl"
    heartbeat_b = tmp_path / "b.jsonl"
    heartbeat_a.write_text('{"ts":"1000"}\n', encoding="utf-8")
    heartbeat_b.write_text('{"ts":"2000"}\n', encoding="utf-8")
    configs = (
        portfolio_equity.VolumeInstanceConfig(
            key="lighter_entropy",
            heartbeat_path=heartbeat_a,
            symbol="BTC",
            primary_source="lighter",
            hedge_source="hyperliquid",
        ),
        portfolio_equity.VolumeInstanceConfig(
            key="variational_entropy",
            heartbeat_path=heartbeat_b,
            symbol="BTC",
            primary_source="variational",
            hedge_source="hyperliquid_var",
        ),
    )

    async def hyperliquid_reader(since: Decimal, symbol: str) -> Decimal:
        return Decimal("100")

    async def lighter_reader(since: Decimal, symbol: str) -> Decimal:
        return Decimal("120")

    async def variational_reader(since: Decimal, symbol: str) -> Decimal:
        return Decimal("300")

    async def hyperliquid_var_reader(since: Decimal, symbol: str) -> Decimal:
        raise RuntimeError("成交查询暂不可用")

    output = tmp_path / "portfolio_volume.jsonl"
    snapshot = asyncio.run(
        portfolio_equity.record_volume_once(
            {
                "lighter": lighter_reader,
                "hyperliquid": hyperliquid_reader,
                "hyperliquid_var": hyperliquid_var_reader,
                "variational": variational_reader,
            },
            output,
            instances=configs,
            timestamp=1787590000.0,
        )
    )

    written = json.loads(output.read_text(encoding="utf-8"))
    margin = float(portfolio_equity.HEARTBEAT_START_MARGIN_SECONDS)
    assert snapshot == written
    assert written["instances"] == {
        "lighter_entropy": {
            "symbol": "BTC",
            "since": 1000.0 - margin,
            "primary": "120",
            "hedge": "100",
        },
        "variational_entropy": {
            "symbol": "BTC",
            "since": 2000.0 - margin,
            "primary": "300",
        },
    }
    assert written["totals_by_symbol"] == {"BTC": "520"}
    assert written["errors"]["variational_entropy|hedge"].endswith(
        "成交查询暂不可用"
    )


def test_start_never_later_than_first_fill(tmp_path) -> None:
    """统计起点必须早于首笔成交，否则整轮成交会被漏掉。

    实测：策略先下单、等成交、再写心跳，首笔成交比首条心跳早 6~21 秒。
    早期实现直接拿心跳时间当起点，实例 C 因此把唯一一轮统计成了 0。
    """
    # 首条心跳带 due_at（action=opened）：应能反推出真实开仓时刻
    opened_at = Decimal("1787578796.25")
    heartbeat_ts = opened_at + Decimal("8")            # 心跳晚 8 秒落盘
    due_at = opened_at + Decimal(4 * 3600)             # 4 小时周期
    path = tmp_path / "with_due.jsonl"
    path.write_text(
        json.dumps({"ts": str(heartbeat_ts), "action": "opened", "due_at": str(due_at)})
        + "\n",
        encoding="utf-8",
    )

    start = portfolio_equity.read_heartbeat_start(path)

    assert start == opened_at, "带 due_at 时应精确反推开仓时刻"
    assert start < heartbeat_ts

    # 首条心跳没有 due_at（例如 execution_uncertain）：退回安全余量
    bare = tmp_path / "no_due.jsonl"
    bare.write_text(json.dumps({"ts": str(heartbeat_ts)}) + "\n", encoding="utf-8")

    fallback = portfolio_equity.read_heartbeat_start(bare)

    assert fallback == heartbeat_ts - portfolio_equity.HEARTBEAT_START_MARGIN_SECONDS
    assert fallback < heartbeat_ts - Decimal("21"), "余量要覆盖实测最大的落盘延迟"


def test_lighter_pnl_window_is_bounded() -> None:
    """Lighter 盈亏查询窗口必须有界。

    实测 resolution=1h 时 start_timestamp=0 与 30 天前都返回 400，7 天可用。
    早期实现写死 start_timestamp="0"，导致该账户每次取数都失败、
    在快照里只留一条 errors，而累计盈亏静默少算一个账户。
    """
    window_days = portfolio_equity.LIGHTER_PNL_WINDOW_MS / (24 * 60 * 60 * 1000)

    assert 0 < window_days <= 7, "窗口需落在接口允许的范围内"
