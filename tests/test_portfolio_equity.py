"""组合级权益记录器测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal

from tools import portfolio_equity


class FakeHyperliquidClient:
    """返回固定清算状态和 Spot 状态的只读客户端。"""

    async def _user_state(self) -> dict:
        """返回同时含策略仓位和人工仓位的清算状态。"""
        return {
            "marginSummary": {"accountValue": "62.00"},
            "assetPositions": [
                {"position": {"coin": "BTC", "unrealizedPnl": "-5.00"}},
                {"position": {"coin": "SOL", "unrealizedPnl": "17.25"}},
            ],
        }

    async def _spot_user_state(self) -> dict:
        """返回 Spot USDC 余额。"""
        return {"balances": [{"coin": "USDC", "total": "557.34"}]}


class FakeLighterClient:
    """返回固定账户详情的只读 Lighter 客户端。"""

    def _require_account_index(self) -> int:
        """返回测试账户索引。"""
        return 7

    async def _get_json(self, path: str, *, params: dict) -> dict:
        """返回抵押品及全部持仓。"""
        assert path == "/api/v1/account"
        assert params == {"by": "index", "value": "7"}
        return {
            "accounts": [
                {
                    "collateral": "579.037583",
                    "total_asset_value": "546.757606",
                    "positions": [
                        {"symbol": "BTC", "unrealized_pnl": "3.285513"},
                        {"symbol": "SOL", "unrealized_pnl": "-35.565490"},
                    ],
                }
            ]
        }

    def _raise_api_error(self, data: dict, *, context: str) -> None:
        """测试响应固定成功。"""
        assert context == "账户详情查询"


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


def test_hyperliquid_equity_filters_manual_symbol_and_ignores_account_value() -> None:
    """Hyperliquid 权益 = Spot 减去非策略币种浮盈亏。

    Spot USDC **本身已含**全部未实现（实测 Δspot == Δupnl），
    所以策略币种的浮盈亏不能再加一遍，人工币种的则要减掉。
    """
    equity = asyncio.run(
        portfolio_equity.read_hyperliquid_equity(
            FakeHyperliquidClient(),
            ("BTC",),
        )
    )

    assert isinstance(equity, Decimal)
    # spot 557.34 已含 BTC(-5) 与 SOL(+17.25)；只需减掉人工的 SOL。
    assert equity == Decimal("557.34") - Decimal("17.25")
    assert equity != Decimal("557.34") + Decimal("-5.00"), "不得再叠加策略币种浮盈亏"
    assert equity != Decimal("557.34"), "不得忽略人工币种"
    assert equity != Decimal("557.34") + Decimal("62.00"), "不得使用 accountValue"


def test_lighter_equity_uses_collateral_plus_filtered_unrealized_pnl() -> None:
    """Lighter 排除人工 SOL，结果必须不同于抵押品加全量浮盈亏。"""
    equity = asyncio.run(
        portfolio_equity.read_lighter_equity(FakeLighterClient(), ("BTC",))
    )

    assert isinstance(equity, Decimal)
    assert equity == Decimal("579.037583") + Decimal("3.285513")
    assert equity != (
        Decimal("579.037583")
        + Decimal("3.285513")
        + Decimal("-35.565490")
    )


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

    async def lighter_reader(symbols: tuple[str, ...]) -> Decimal:
        assert symbols == ("BTC", "ETH")
        return Decimal("561.49")

    async def variational_reader(symbols: tuple[str, ...]) -> Decimal:
        assert symbols == ("BTC", "ETH")
        raise RuntimeError("临时不可用")

    async def hyperliquid_reader(symbols: tuple[str, ...]) -> Decimal:
        assert symbols == ("BTC",)
        return Decimal("572.20")

    output = tmp_path / "portfolio_equity.jsonl"
    snapshot = asyncio.run(
        portfolio_equity.record_once(
            {
                "lighter": lighter_reader,
                "variational": variational_reader,
                "hyperliquid": hyperliquid_reader,
            },
            output,
            timestamp=1787590000.0,
        )
    )

    written = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot == written
    assert written["schema"] == 3
    assert written["symbols"] == {
        "lighter": ["BTC", "ETH"],
        "variational": ["BTC", "ETH"],
        "hyperliquid": ["BTC"],
    }
    assert written["accounts"] == {
        "lighter": "561.49",
        "hyperliquid": "572.20",
    }
    assert written["total_equity"] == "1133.69"
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


def test_volume_snapshot_uses_heartbeat_start_and_same_instance_estimates(
    tmp_path,
) -> None:
    """每实例从心跳首行起算，Lighter 只复制本实例对手腿并显式标记。"""
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
        ("hyperliquid", expected_since["lighter_entropy"], "BTC"),
        ("variational", expected_since["variational_entropy"], "BTC"),
        ("hyperliquid_var", expected_since["variational_entropy"], "BTC"),
        ("variational", expected_since["lighter_variational_eth"], "ETH"),
    ]
    assert snapshot["instances"] == {
        "lighter_entropy": {
            "symbol": "BTC",
            "since": float(expected_since["lighter_entropy"]),
            "primary": "100",
            "hedge": "100",
            "estimated": ["primary"],
        },
        "variational_entropy": {
            "symbol": "BTC",
            "since": float(expected_since["variational_entropy"]),
            "primary": "300",
            "hedge": "200",
            "estimated": [],
        },
        "lighter_variational_eth": {
            "symbol": "ETH",
            "since": float(expected_since["lighter_variational_eth"]),
            "primary": "40",
            "hedge": "40",
            "estimated": ["primary"],
        },
    }
    assert snapshot["totals_by_symbol"] == {"BTC": "700", "ETH": "80"}


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

    async def variational_reader(since: Decimal, symbol: str) -> Decimal:
        return Decimal("300")

    async def hyperliquid_var_reader(since: Decimal, symbol: str) -> Decimal:
        raise RuntimeError("成交查询暂不可用")

    output = tmp_path / "portfolio_volume.jsonl"
    snapshot = asyncio.run(
        portfolio_equity.record_volume_once(
            {
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
            "primary": "100",
            "hedge": "100",
            "estimated": ["primary"],
        },
        "variational_entropy": {
            "symbol": "BTC",
            "since": 2000.0 - margin,
            "primary": "300",
            "estimated": [],
        },
    }
    assert written["totals_by_symbol"] == {"BTC": "500"}
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
