"""周期性记录全部实盘账户的组合级权益与累计成交量。

记录器只做账户与成交查询和 JSONL 追加，不参与任何策略、下单或风控流程。
单个来源读取失败会从当次合计中跳过，并在同一条记录的 ``errors`` 字段标注。
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "portfolio_equity.jsonl"
DEFAULT_VOLUME_OUTPUT = PROJECT_ROOT / "data" / "portfolio_volume.jsonl"
DEFAULT_INTERVAL_SECONDS = 900.0
PORTFOLIO_SCHEMA = 5
ACCOUNT_SOURCES = {
    "lighter": "platform",
    "hyperliquid": "platform",
    "hyperliquid_var": "platform",
    "variational": "platform",
}

EquityReader = Callable[[tuple[str, ...]], Awaitable[Decimal]]
VolumeReader = Callable[[Decimal, str], Awaitable[Decimal]]
HyperliquidPageReader = Callable[[int], Awaitable[object]]
VariationalPageReader = Callable[[int | None, int | None], Awaitable[object]]


@dataclass(frozen=True)
class VolumeInstanceConfig:
    """累计成交量所需的实例级只读配置。"""

    key: str
    heartbeat_path: Path
    symbol: str
    primary_source: str
    hedge_source: str
    #: 对冲腿在其所属场馆的真实标的名。HL 的 HIP-3 dex 会给成交记录里的
    #: ``coin`` 加 dex 前缀（如 ``io:SNDK``），与主腿的 ``SNDK`` 不是同一个
    #: 字符串；两者混用会让该腿累计成交量恒为 0，且不报错——2026-08-26
    #: 就是这样漏掉了赚 Entropy 积分的整条 io:SNDK 腿。留空表示与主腿同名。
    hedge_symbol: str | None = None


#: ⚠️ 必须与实际启动参数一致（标的、心跳路径、两腿来源）。
#: 配对变更后忘了同步这里，成交量会静默统计错标的。
DEFAULT_VOLUME_INSTANCES = (
    VolumeInstanceConfig(
        key="variational_entropy_sndk",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_sndk.jsonl",
        symbol="SNDK",
        primary_source="variational",
        hedge_source="hyperliquid_var",
        hedge_symbol="io:SNDK",
    ),
    VolumeInstanceConfig(
        key="lighter_variational_btc",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_btc.jsonl",
        symbol="BTC",
        primary_source="lighter",
        hedge_source="variational",
    ),
)


def _decimal(value: object, *, label: str) -> Decimal:
    """把账户字段转换为有限 Decimal，拒绝布尔值和无效数字。"""
    if isinstance(value, bool):
        raise ValueError(f"{label} 不能是布尔值")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效十进制数") from exc
    if not result.is_finite():
        raise ValueError(f"{label} 必须是有限数")
    return result


def _decimal_text(value: Decimal) -> str:
    """把 Decimal 保存为不经过浮点数的十进制字符串。"""
    return format(value, "f")


def _lighter_auth_headers(client: Any) -> dict[str, str]:
    """即时生成 Lighter 短期 token，并规范成接口要求的小写请求头。"""
    raw_headers = client._auth_headers()
    if not isinstance(raw_headers, Mapping):
        raise ValueError("Lighter 鉴权头不是对象")
    for name, value in raw_headers.items():
        if isinstance(name, str) and name.lower() == "authorization" and value:
            return {"authorization": str(value)}
    raise ValueError("Lighter 鉴权头缺少 authorization token")


async def _lighter_get_json(
    client: Any,
    path: str,
    *,
    params: dict[str, str],
) -> Mapping[str, object]:
    """在每次 Lighter 取数前重新生成 token，再执行只读请求。"""
    reader = getattr(client, "_get_json_decimal", client._get_json)
    data = await reader(
        path,
        params=params,
        headers=_lighter_auth_headers(client),
    )
    if not isinstance(data, Mapping):
        raise ValueError(f"Lighter {path} 响应不是对象")
    return data


def _symbol(value: object, *, label: str) -> str:
    """规范化成交币种，拒绝空值和带分隔符的异常名称。"""
    if not isinstance(value, str):
        raise ValueError(f"{label} 不是字符串")
    symbol = value.strip().upper()
    if not symbol or "|" in symbol:
        raise ValueError(f"{label} 无效")
    return symbol


def strategy_symbols_by_source(
    instances: Sequence[VolumeInstanceConfig] = DEFAULT_VOLUME_INSTANCES,
) -> dict[str, tuple[str, ...]]:
    """从成交量实例配置推导每个账户应纳入权益统计的币种。"""
    collected: dict[str, set[str]] = {}
    for config in instances:
        symbol = _symbol(config.symbol, label=f"实例 {config.key} 币种")
        for source in (config.primary_source, config.hedge_source):
            normalized_source = source.strip()
            if not normalized_source:
                raise ValueError(f"实例 {config.key} 的账户来源为空")
            collected.setdefault(normalized_source, set()).add(symbol)
    return {
        source: tuple(sorted(symbols))
        for source, symbols in sorted(collected.items())
    }


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    """解析分页整数，拒绝布尔值、小数和低于下限的值。"""
    if isinstance(value, bool):
        raise ValueError(f"{label} 不能是布尔值")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} 不是有效整数") from exc
    if str(parsed) != str(value).strip() or parsed < minimum:
        raise ValueError(f"{label} 不是不小于 {minimum} 的整数")
    return parsed


def _timestamp(value: object, *, label: str) -> Decimal:
    """把 Unix 秒或 ISO 8601 时间转换为 Decimal 秒。"""
    try:
        result = _decimal(value, label=label)
    except ValueError:
        if not isinstance(value, str):
            raise
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} 不是有效时间") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        utc = parsed.astimezone(timezone.utc)
        result = Decimal(calendar.timegm(utc.utctimetuple())) + (
            Decimal(utc.microsecond) / Decimal("1000000")
        )
    if result < 0:
        raise ValueError(f"{label} 不能为负数")
    return result


#: 心跳落盘晚于成交时，向前回退的安全余量（秒）。
#: 实测首笔成交比首条心跳早约 6~21 秒——策略先下单、等成交、再写心跳。
#: 直接拿心跳时间当起点会整轮漏掉，实例 C 就因此统计出 0。
#: 余量取 5 分钟：足以覆盖开仓延迟，又远小于与上一次人工/验证交易的间隔。
HEARTBEAT_START_MARGIN_SECONDS = Decimal("300")

#: Lighter /api/v1/pnl 在 resolution=1h 下的最大可查窗口（毫秒）。
#: 实测 start_timestamp=0 与 30 天前均返回 400，7 天正常。取 6 天留余量。
LIGHTER_PNL_WINDOW_MS = 6 * 24 * 60 * 60 * 1000


def read_heartbeat_start(path: Path | str) -> Decimal:
    """只读心跳文件首行，返回该实例的成交统计起点。

    优先用 ``due_at - 周期`` 反推真实开仓时刻（精确）；
    首条心跳若不是 ``opened``（例如 ``execution_uncertain``）拿不到 due_at，
    则退回「心跳时间减去安全余量」。两者都保证不会漏掉首轮成交。
    """
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            first_line = stream.readline()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取心跳首行：{exc}") from exc
    if not first_line:
        raise ValueError("心跳文件为空")
    try:
        payload = json.loads(
            first_line,
            parse_float=Decimal,
            parse_int=Decimal,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("心跳首行不是有效 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("心跳首行不是对象")

    heartbeat_ts = _timestamp(payload.get("ts"), label="心跳首行 ts")

    # 首条是 opened 时，due_at 减去整周期即为真实开仓时刻。
    due_at = payload.get("due_at")
    if due_at is not None:
        try:
            due = _timestamp(due_at, label="心跳首行 due_at")
        except ValueError:
            due = None
        if due is not None:
            for cycle_hours in (Decimal(1), Decimal(2), Decimal(4)):
                opened_at = due - cycle_hours * Decimal(3600)
                gap = heartbeat_ts - opened_at
                # 开仓到落盘之间只应相隔秒级；命中即认为反推成立。
                if Decimal(0) < gap < Decimal(600):
                    return opened_at

    return heartbeat_ts - HEARTBEAT_START_MARGIN_SECONDS


async def read_lighter_volume(
    client: Any,
    *,
    start_time_ms: int,
    market_id: int,
) -> Decimal:
    """翻页读取 Lighter 账户成交，并直接累计官方 ``usd_amount``。"""
    first_timestamp = _integer(start_time_ms, label="Lighter 起始时间")
    target_market = _integer(market_id, label="Lighter 市场编号")
    account_index = _integer(
        client._require_account_index(),
        label="Lighter 账户索引",
    )
    cursor: str | None = None
    seen_cursors: set[str] = set()
    total = Decimal("0")

    while True:
        params = {
            "sort_by": "timestamp",
            "sort_dir": "desc",
            "limit": "100",
            "account_index": str(account_index),
        }
        if cursor is not None:
            params["cursor"] = cursor

        data = await _lighter_get_json(
            client,
            "/api/v1/trades",
            params=params,
        )
        client._raise_api_error(data, context="成交查询")
        trades = data.get("trades")
        if not isinstance(trades, list):
            raise ValueError("Lighter 成交响应缺少 trades 数组")
        if not trades:
            return total

        oldest_timestamp: int | None = None
        for index, trade in enumerate(trades):
            if not isinstance(trade, Mapping):
                raise ValueError(f"Lighter 第 {index + 1} 条成交不是对象")
            trade_timestamp = _integer(
                trade.get("timestamp"),
                label="Lighter 成交时间",
            )
            oldest_timestamp = (
                trade_timestamp
                if oldest_timestamp is None
                else min(oldest_timestamp, trade_timestamp)
            )
            if trade_timestamp < first_timestamp:
                continue
            trade_market = _integer(
                trade.get("market_id"),
                label="Lighter 成交市场编号",
            )
            if trade_market != target_market:
                continue
            amount = _decimal(
                trade.get("usd_amount"),
                label="Lighter 成交额",
            )
            if amount < 0:
                raise ValueError("Lighter 成交额不能为负数")
            total += amount

        # 接口按时间倒序返回；一旦本页跨过起点，后续页只会更早。
        if oldest_timestamp is not None and oldest_timestamp < first_timestamp:
            return total

        raw_cursor = data.get("next_cursor")
        if raw_cursor in (None, ""):
            return total
        if not isinstance(raw_cursor, str):
            raise ValueError("Lighter 下一页 cursor 不是字符串")
        cursor = raw_cursor.strip()
        if not cursor:
            return total
        if cursor in seen_cursors:
            raise ValueError("Lighter 分页 cursor 重复，无法继续推进")
        seen_cursors.add(cursor)


async def read_hyperliquid_volume(
    fetch_page: HyperliquidPageReader,
    *,
    start_time_ms: int,
    symbol: str,
) -> Decimal:
    """按时间游标取全 Hyperliquid 成交，只累计实例指定币种。"""
    first_cursor = _integer(start_time_ms, label="Hyperliquid 起始时间")
    cursor = first_cursor
    target_symbol = _symbol(symbol, label="Hyperliquid 实例币种")
    total = Decimal("0")

    while True:
        payload = await fetch_page(cursor)
        if not isinstance(payload, list):
            raise ValueError("Hyperliquid 成交响应不是数组")
        if not payload:
            return total

        latest_time: int | None = None
        for index, fill in enumerate(payload):
            if not isinstance(fill, Mapping):
                raise ValueError(f"Hyperliquid 第 {index + 1} 条成交不是对象")
            fill_time = _integer(
                fill.get("time"),
                label="Hyperliquid 成交时间",
            )
            latest_time = (
                fill_time if latest_time is None else max(latest_time, fill_time)
            )
            if fill_time < first_cursor:
                continue
            raw_symbol = fill.get("coin")
            if not isinstance(raw_symbol, str):
                continue
            fill_symbol = raw_symbol.strip().upper()
            if fill_symbol != target_symbol:
                continue
            size = _decimal(
                fill.get("sz"),
                label=f"Hyperliquid {target_symbol} 成交数量",
            )
            price = _decimal(
                fill.get("px"),
                label=f"Hyperliquid {target_symbol} 成交价格",
            )
            amount = size * price
            if amount < 0:
                raise ValueError(f"Hyperliquid {target_symbol} 成交额不能为负数")
            total += amount

        if latest_time is None or latest_time < cursor:
            raise ValueError("Hyperliquid 成交时间游标无法继续推进")
        cursor = latest_time + 1


async def read_variational_volume(
    fetch_page: VariationalPageReader,
    *,
    start_time: Decimal,
    symbol: str,
) -> Decimal:
    """翻页取全 Variational 成交，再按实例起点和币种累计。"""
    limit: int | None = None
    offset: int | None = None
    fetched_count = 0
    object_count: int | None = None
    requested_pages: set[tuple[int | None, int | None]] = set()
    since = _timestamp(start_time, label="Variational 起始时间")
    target_symbol = _symbol(symbol, label="Variational 实例币种")
    total = Decimal("0")

    while True:
        page_key = (limit, offset)
        if page_key in requested_pages:
            raise ValueError("Variational 分页游标重复，无法继续推进")
        requested_pages.add(page_key)

        payload = await fetch_page(limit, offset)
        if not isinstance(payload, Mapping):
            raise ValueError("Variational 成交响应不是对象")
        result = payload.get("result")
        pagination = payload.get("pagination")
        if not isinstance(result, list):
            raise ValueError("Variational 成交响应缺少 result 数组")
        if not isinstance(pagination, Mapping):
            raise ValueError("Variational 成交响应缺少 pagination 对象")

        current_object_count = _integer(
            pagination.get("object_count"),
            label="Variational 成交总数",
        )
        if object_count is None:
            object_count = current_object_count
        elif current_object_count != object_count:
            object_count = max(object_count, current_object_count)

        for index, trade in enumerate(result):
            if not isinstance(trade, Mapping):
                raise ValueError(f"Variational 第 {index + 1} 条成交不是对象")
            created_at = _timestamp(
                trade.get("created_at"),
                label="Variational 成交时间",
            )
            if created_at < since:
                continue
            instrument = trade.get("instrument")
            if not isinstance(instrument, Mapping):
                raise ValueError("Variational 成交缺少 instrument 对象")
            trade_symbol = _symbol(
                instrument.get("underlying"),
                label="Variational 成交币种",
            )
            if trade_symbol != target_symbol:
                continue
            quantity = _decimal(
                trade.get("qty"),
                label=f"Variational {target_symbol} 成交数量",
            )
            price = _decimal(
                trade.get("price"),
                label=f"Variational {target_symbol} 成交价格",
            )
            amount = abs(quantity) * price
            if amount < 0:
                raise ValueError(f"Variational {target_symbol} 成交额不能为负数")
            total += amount

        fetched_count += len(result)
        if object_count is not None and fetched_count >= object_count:
            return total

        next_page = pagination.get("next_page")
        if not isinstance(next_page, Mapping):
            return total
        limit = _integer(
            next_page.get("limit"),
            label="Variational 下一页 limit",
            minimum=1,
        )
        offset = _integer(
            next_page.get("offset"),
            label="Variational 下一页 offset",
        )


async def read_variational_pnl(fetch_page: VariationalPageReader) -> Decimal:
    """翻页汇总 Variational 平台流水盈亏，并排除充值与提现。"""
    pnl_types = {"realized_pnl", "funding", "fee", "referral_reward"}
    cash_types = {"deposit", "withdrawal"}
    known_types = pnl_types | cash_types
    limit = 100
    offset = 0
    fetched_count = 0
    object_count: int | None = None
    total = Decimal("0")

    while True:
        payload = await fetch_page(limit, offset)
        if not isinstance(payload, Mapping):
            raise ValueError("Variational 流水响应不是对象")
        result = payload.get("result")
        pagination = payload.get("pagination")
        if not isinstance(result, list):
            raise ValueError("Variational 流水响应缺少 result 数组")
        if not isinstance(pagination, Mapping):
            raise ValueError("Variational 流水响应缺少 pagination 对象")

        current_object_count = _integer(
            pagination.get("object_count"),
            label="Variational 流水总数",
        )
        if object_count is None:
            object_count = current_object_count
        elif current_object_count != object_count:
            object_count = max(object_count, current_object_count)

        for index, transfer in enumerate(result):
            if not isinstance(transfer, Mapping):
                raise ValueError(f"Variational 第 {index + 1} 条流水不是对象")
            transfer_type = transfer.get("transfer_type")
            if not isinstance(transfer_type, str) or transfer_type not in known_types:
                raise ValueError(f"Variational 未知流水类型：{transfer_type}")
            asset = transfer.get("asset")
            if asset != "USDC":
                raise ValueError(f"Variational 流水资产仅支持 USDC，实际为：{asset}")
            quantity = _decimal(
                transfer.get("qty"),
                label=f"Variational {transfer_type} 流水数量",
            )
            if transfer_type in pnl_types:
                total += quantity

        fetched_count += len(result)
        if object_count is not None and fetched_count >= object_count:
            return total
        if not result:
            raise ValueError("Variational 流水未取满总数，分页无法继续推进")
        offset += limit


async def read_lighter_equity(
    client: Any,
    symbols: Sequence[str],
) -> Decimal:
    """读取 Lighter 官方累计盈亏 ``trade_pnl`` 的最新值。"""
    for symbol in symbols:
        _symbol(symbol, label="Lighter 策略币种")
    account_index = client._require_account_index()
    data = await _lighter_get_json(
        client,
        "/api/v1/pnl",
        params={
            "by": "index",
            "value": str(account_index),
            "resolution": "1h",
            # ⚠️ resolution=1h 时窗口不能过长：实测 start=0 与 start=30 天前
            # 都返回 400，7 天可用。这里只取最新一点的累计值，
            # 窗口够覆盖到「有数据的最近一小时」即可。
            "start_timestamp": str(
                time.time_ns() // 1_000_000 - LIGHTER_PNL_WINDOW_MS
            ),
            "end_timestamp": str(time.time_ns() // 1_000_000),
            "count_back": "200",
        },
    )
    client._raise_api_error(data, context="累计盈亏查询")
    pnl_history = data.get("pnl")
    if not isinstance(pnl_history, list) or not pnl_history:
        raise ValueError("Lighter 累计盈亏响应缺少 pnl 数组")
    latest = pnl_history[-1]
    if not isinstance(latest, Mapping):
        raise ValueError("Lighter 最新累计盈亏不是对象")
    _integer(latest.get("timestamp"), label="Lighter 累计盈亏时间")
    return _decimal(
        latest.get("trade_pnl"),
        label="Lighter 平台累计盈亏",
    )


async def read_variational_equity(
    client: Any,
    symbols: Sequence[str],
) -> Decimal:
    """读取 Variational 权益：``balance`` **已包含**未实现盈亏，故不再叠加。

    实测（2026-08-25，8 秒间隔三次采样）：``Δbalance`` 与 ``Δupnl``
    逐次完全相等，证明 balance 实时反映未实现盈亏。
    早期实现写成 ``balance + upnl``，把未实现算了两遍。

    非策略币种（用户手工开的仓位）的未实现已含在 balance 里，需要**减掉**。
    """
    targets = {_symbol(symbol, label="Variational 策略币种") for symbol in symbols}
    portfolio = await client.raw("/portfolio")
    if not isinstance(portfolio, Mapping):
        raise ValueError("Variational portfolio 响应不是对象")
    cash = _decimal(portfolio.get("balance"), label="Variational 余额")

    payload = await client.get_positions()
    positions = payload if isinstance(payload, list) else (
        payload.get("positions") if isinstance(payload, Mapping) else None
    )
    if not isinstance(positions, list):
        raise ValueError("Variational positions 响应缺少持仓数组")
    # balance 已含全部币种的未实现，这里累计**非策略币种**的部分，稍后减掉。
    foreign_unrealized_pnl = Decimal("0")
    for index, position in enumerate(positions):
        if not isinstance(position, Mapping):
            raise ValueError(f"Variational 第 {index + 1} 条持仓不是对象")
        position_info = position.get("position_info")
        if not isinstance(position_info, Mapping):
            raise ValueError(f"Variational 第 {index + 1} 条持仓缺少 position_info")
        instrument = position_info.get("instrument")
        if not isinstance(instrument, Mapping):
            raise ValueError(f"Variational 第 {index + 1} 条持仓缺少 instrument")
        symbol = _symbol(
            instrument.get("underlying"),
            label=f"Variational 第 {index + 1} 条持仓币种",
        )
        if symbol in targets:
            continue
        foreign_unrealized_pnl += _decimal(
            position.get("upnl"),
            label=f"Variational {symbol} 未实现盈亏",
        )
    return cash - foreign_unrealized_pnl


async def read_hyperliquid_equity(
    client: Any,
    symbols: Sequence[str],
) -> Decimal:
    """读取 Hyperliquid ``allTime`` 的官方累计盈亏末值。"""
    for symbol in symbols:
        _symbol(symbol, label="Hyperliquid 策略币种")
    info = client._require_info()
    address = client._require_account_address()
    payload = await asyncio.to_thread(info.portfolio, address)
    if not isinstance(payload, list):
        raise ValueError("Hyperliquid portfolio 响应不是数组")

    all_time: Mapping[str, object] | None = None
    for index, period_entry in enumerate(payload):
        if (
            not isinstance(period_entry, (list, tuple))
            or len(period_entry) != 2
        ):
            raise ValueError(f"Hyperliquid 第 {index + 1} 个周期结构无效")
        period, data = period_entry
        if period == "allTime":
            if not isinstance(data, Mapping):
                raise ValueError("Hyperliquid allTime 数据不是对象")
            all_time = data
            break
    if all_time is None:
        raise ValueError("Hyperliquid portfolio 缺少 allTime 周期")

    history = all_time.get("pnlHistory")
    if not isinstance(history, list) or not history:
        raise ValueError("Hyperliquid allTime 缺少 pnlHistory")
    latest = history[-1]
    if not isinstance(latest, (list, tuple)) or len(latest) != 2:
        raise ValueError("Hyperliquid 最新累计盈亏结构无效")
    _integer(latest[0], label="Hyperliquid 累计盈亏时间")
    return _decimal(latest[1], label="Hyperliquid 平台累计盈亏")


async def _read_lighter_from_env(symbols: tuple[str, ...]) -> Decimal:
    """用只读 Lighter 客户端采集默认实盘账户。"""
    from adapters.lighter_client import LighterClient

    address = (os.getenv("LIGHTER_RH_L1_ADDRESS") or "").strip()
    if not address:
        raise RuntimeError("缺少 LIGHTER_RH_L1_ADDRESS 环境变量")
    private_key = (os.getenv("LIGHTER_API_PRIVATE_KEY") or "").strip()
    if not private_key:
        raise RuntimeError("缺少 LIGHTER_API_PRIVATE_KEY 环境变量")
    client = LighterClient(
        l1_address=address,
        api_private_key=private_key,
        api_key_index=_integer(
            os.getenv("LIGHTER_API_KEY_INDEX", "255"),
            label="Lighter API 密钥索引",
        ),
    )
    try:
        await client.connect()
        return await read_lighter_equity(client, symbols)
    finally:
        await client.close()


async def _read_lighter_volume_from_env(
    start_time: Decimal,
    symbol: str,
) -> Decimal:
    """用鉴权只读客户端采集 Lighter 官方成交额。"""
    from adapters.lighter_client import LighterClient

    address = (os.getenv("LIGHTER_RH_L1_ADDRESS") or "").strip()
    if not address:
        raise RuntimeError("缺少 LIGHTER_RH_L1_ADDRESS 环境变量")
    private_key = (os.getenv("LIGHTER_API_PRIVATE_KEY") or "").strip()
    if not private_key:
        raise RuntimeError("缺少 LIGHTER_API_PRIVATE_KEY 环境变量")
    client = LighterClient(
        l1_address=address,
        api_private_key=private_key,
        api_key_index=_integer(
            os.getenv("LIGHTER_API_KEY_INDEX", "255"),
            label="Lighter API 密钥索引",
        ),
    )
    try:
        await client.connect()
        market = await client._load_market_meta(
            _symbol(symbol, label="Lighter 实例币种")
        )
        return await read_lighter_volume(
            client,
            start_time_ms=int(start_time * Decimal("1000")),
            market_id=_integer(
                market.get("market_id"),
                label="Lighter 市场编号",
            ),
        )
    finally:
        await client.close()


async def _read_variational_pnl_from_env(
    _symbols: tuple[str, ...],
) -> Decimal:
    """用只读 Variational 会话采集平台累计盈亏流水。"""
    from adapters.variational_client import Session, VariationalClient

    client = VariationalClient(Session.from_env())

    async def fetch_page(limit: int | None, offset: int | None) -> object:
        return await client.raw(f"/transfers?limit={limit}&offset={offset}")

    try:
        return await read_variational_pnl(fetch_page)
    finally:
        await client.close()


async def _read_variational_volume_from_env(
    start_time: Decimal,
    symbol: str,
) -> Decimal:
    """用只读 Variational 会话采集指定实例范围内的成交。"""
    from adapters.variational_client import Session, VariationalClient

    client = VariationalClient(Session.from_env())

    async def fetch_page(limit: int | None, offset: int | None) -> object:
        path = (
            "/trades"
            if limit is None or offset is None
            else f"/trades?limit={limit}&offset={offset}"
        )
        return await client.raw(path)

    try:
        return await read_variational_volume(
            fetch_page,
            start_time=start_time,
            symbol=symbol,
        )
    finally:
        await client.close()


def _hyperliquid_reader(prefix: str) -> EquityReader:
    """为指定环境变量前缀创建只读 Hyperliquid 采集函数。"""

    async def read(symbols: tuple[str, ...]) -> Decimal:
        from adapters.hyperliquid_client import HyperliquidClient

        client = HyperliquidClient.from_env(prefix=prefix, trading_enabled=False)
        try:
            await client.connect()
            return await read_hyperliquid_equity(client, symbols)
        finally:
            await client.close()

    return read


def _hyperliquid_volume_reader(prefix: str) -> VolumeReader:
    """为指定公开账户创建实例级 Hyperliquid 成交采集函数。"""

    async def read(start_time: Decimal, symbol: str) -> Decimal:
        from adapters.hyperliquid_client import HyperliquidClient

        client = HyperliquidClient.from_env(prefix=prefix, trading_enabled=False)
        try:
            await client.connect()
            info = client._require_info()
            address = client._require_account_address()

            async def fetch_page(start_time: int) -> object:
                return await asyncio.to_thread(
                    info.user_fills_by_time,
                    address,
                    start_time,
                )

            start_time_ms = int(start_time * Decimal("1000"))
            return await read_hyperliquid_volume(
                fetch_page,
                start_time_ms=start_time_ms,
                symbol=symbol,
            )
        finally:
            await client.close()

    return read


def build_default_readers(
    *,
    hyperliquid_prefix: str = "HYPERLIQUID",
    hyperliquid_var_prefix: str = "HYPERLIQUID_VAR",
) -> dict[str, EquityReader]:
    """创建四个账户互相隔离的默认只读采集函数。"""
    return {
        "lighter": _read_lighter_from_env,
        "variational": _read_variational_pnl_from_env,
        "hyperliquid": _hyperliquid_reader(hyperliquid_prefix),
        "hyperliquid_var": _hyperliquid_reader(hyperliquid_var_prefix),
    }


def build_default_volume_readers(
    *,
    hyperliquid_prefix: str = "HYPERLIQUID",
    hyperliquid_var_prefix: str = "HYPERLIQUID_VAR",
) -> dict[str, VolumeReader]:
    """创建四个支持实例起点与币种过滤的只读成交采集函数。"""
    return {
        "lighter": _read_lighter_volume_from_env,
        "hyperliquid": _hyperliquid_volume_reader(hyperliquid_prefix),
        "hyperliquid_var": _hyperliquid_volume_reader(hyperliquid_var_prefix),
        "variational": _read_variational_volume_from_env,
    }


async def collect_snapshot(
    readers: Mapping[str, EquityReader],
    *,
    instances: Sequence[VolumeInstanceConfig] = DEFAULT_VOLUME_INSTANCES,
    timestamp: float | None = None,
) -> dict[str, object]:
    """并发采集账户；失败账户只写错误标注，不阻断其他账户。"""

    symbols_by_source = strategy_symbols_by_source(instances)

    async def read_one(name: str, reader: EquityReader) -> Decimal:
        symbols = symbols_by_source.get(name)
        if not symbols:
            raise ValueError(f"{name} 没有对应的策略币种配置")
        return _decimal(await reader(symbols), label=f"{name} 账户权益")

    names = tuple(readers)
    results = await asyncio.gather(
        *(read_one(name, readers[name]) for name in names),
        return_exceptions=True,
    )
    accounts: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors[name] = f"{type(result).__name__}: {result}"
            continue
        accounts[name] = _decimal_text(result)

    snapshot: dict[str, object] = {
        # schema 5：Variational 从账户权益切换为剔除出入金的平台累计盈亏。
        # 面板按 schema 独立建立首条基准，避免与旧权益口径混算。
        "schema": PORTFOLIO_SCHEMA,
        "ts": time.time() if timestamp is None else timestamp,
        "accounts": accounts,
        "sources": {
            name: ACCOUNT_SOURCES.get(name, "computed")
            for name in names
        },
        "symbols": {
            name: list(symbols_by_source.get(name, ()))
            for name in names
        },
    }
    if errors:
        snapshot["errors"] = errors
    return snapshot


async def collect_volume_snapshot(
    readers: Mapping[str, VolumeReader],
    *,
    instances: Sequence[VolumeInstanceConfig] = DEFAULT_VOLUME_INSTANCES,
    timestamp: float | None = None,
) -> dict[str, object]:
    """按实例采集成交量；单腿失败时保留其他实例和可用腿。"""

    async def read_leg(source: str, since: Decimal, symbol: str) -> Decimal:
        reader = readers.get(source)
        if reader is None:
            raise ValueError(f"未配置成交来源 {source}")
        amount = _decimal(
            await reader(since, symbol),
            label=f"{source} {symbol} 累计成交额",
        )
        if amount < 0:
            raise ValueError(f"{source} {symbol} 累计成交额不能为负数")
        return amount

    errors: dict[str, str] = {}
    starts: dict[str, Decimal] = {}
    symbols: dict[str, str] = {}
    decimal_instances: dict[str, dict[str, Decimal]] = {}
    jobs: list[tuple[str, str, str, Decimal, str]] = []

    for config in instances:
        try:
            if config.key in starts:
                raise ValueError(f"实例键重复：{config.key}")
            since = read_heartbeat_start(config.heartbeat_path)
            symbol = _symbol(config.symbol, label=f"实例 {config.key} 币种")
        except (OSError, ValueError) as exc:
            errors[f"{config.key}|heartbeat"] = f"{type(exc).__name__}: {exc}"
            continue
        try:
            hedge_symbol = (
                _symbol(config.hedge_symbol, label=f"实例 {config.key} 对冲腿币种")
                if config.hedge_symbol is not None
                else symbol
            )
        except ValueError as exc:
            errors[f"{config.key}|heartbeat"] = f"{type(exc).__name__}: {exc}"
            continue
        starts[config.key] = since
        symbols[config.key] = symbol
        decimal_instances[config.key] = {}
        for leg, source, leg_symbol in (
            ("primary", config.primary_source, symbol),
            ("hedge", config.hedge_source, hedge_symbol),
        ):
            jobs.append((config.key, leg, source, since, leg_symbol))

    results = await asyncio.gather(
        *(read_leg(source, since, symbol) for _, _, source, since, symbol in jobs),
        return_exceptions=True,
    )
    for (key, leg, _source, _since, _symbol_name), result in zip(
        jobs,
        results,
        strict=True,
    ):
        if isinstance(result, BaseException):
            errors[f"{key}|{leg}"] = f"{type(result).__name__}: {result}"
            continue
        decimal_instances[key][leg] = result

    totals_by_symbol: dict[str, Decimal] = {}
    serialized_instances: dict[str, dict[str, object]] = {}
    for config in instances:
        key = config.key
        if key not in starts:
            continue
        symbol = symbols[key]
        legs = decimal_instances[key]
        serialized: dict[str, object] = {
            "symbol": symbol,
            "since": float(starts[key]),
        }
        for leg in ("primary", "hedge"):
            amount = legs.get(leg)
            if amount is None:
                continue
            serialized[leg] = _decimal_text(amount)
            totals_by_symbol[symbol] = (
                totals_by_symbol.get(symbol, Decimal("0")) + amount
            )
        serialized_instances[key] = serialized

    snapshot: dict[str, object] = {
        "ts": time.time() if timestamp is None else timestamp,
        "instances": serialized_instances,
        "totals_by_symbol": {
            symbol: _decimal_text(value)
            for symbol, value in totals_by_symbol.items()
        },
    }
    if errors:
        snapshot["errors"] = errors
    return snapshot


def append_snapshot(path: Path | str, snapshot: Mapping[str, object]) -> None:
    """把一条组合统计快照追加为单行 JSON。"""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    with output.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


async def record_once(
    readers: Mapping[str, EquityReader],
    output: Path | str,
    *,
    instances: Sequence[VolumeInstanceConfig] = DEFAULT_VOLUME_INSTANCES,
    timestamp: float | None = None,
) -> dict[str, object]:
    """采集并落盘一次；账户失败由快照自身承载，不向外抛出。"""
    snapshot = await collect_snapshot(
        readers,
        instances=instances,
        timestamp=timestamp,
    )
    append_snapshot(output, snapshot)
    return snapshot


async def record_volume_once(
    readers: Mapping[str, VolumeReader],
    output: Path | str,
    *,
    instances: Sequence[VolumeInstanceConfig] = DEFAULT_VOLUME_INSTANCES,
    timestamp: float | None = None,
) -> dict[str, object]:
    """采集并落盘一次累计成交量；来源失败不会向外抛出。"""
    snapshot = await collect_volume_snapshot(
        readers,
        instances=instances,
        timestamp=timestamp,
    )
    append_snapshot(output, snapshot)
    return snapshot


def _print_result(snapshot: Mapping[str, object]) -> None:
    """输出一条不含凭据的中文采集摘要。"""
    accounts = snapshot.get("accounts")
    count = len(accounts) if isinstance(accounts, dict) else 0
    print(f"组合盈亏基准已记录：成功 {count} 个账户", flush=True)
    errors = snapshot.get("errors")
    if isinstance(errors, dict):
        for name, message in errors.items():
            print(f"账户 {name} 读取失败，已跳过：{message}", flush=True)


def _print_volume_result(snapshot: Mapping[str, object]) -> None:
    """输出一条不含凭据的中文累计成交量采集摘要。"""
    instances = snapshot.get("instances")
    count = len(instances) if isinstance(instances, dict) else 0
    print(f"累计成交量已记录：成功 {count} 个实例", flush=True)
    errors = snapshot.get("errors")
    if isinstance(errors, dict):
        for name, message in errors.items():
            print(f"成交来源 {name} 读取失败，已跳过：{message}", flush=True)


async def run_recorder(
    readers: Mapping[str, EquityReader],
    output: Path | str,
    *,
    interval: float,
    once: bool,
    volume_readers: Mapping[str, VolumeReader] | None = None,
    volume_output: Path | str = DEFAULT_VOLUME_OUTPUT,
) -> None:
    """按固定间隔持续记录权益和成交量；单来源故障不会退出循环。"""
    while True:
        snapshot = await record_once(readers, output)
        _print_result(snapshot)
        if volume_readers is not None:
            volume_snapshot = await record_volume_once(volume_readers, volume_output)
            _print_volume_result(volume_snapshot)
        if once:
            return
        await asyncio.sleep(interval)


def _positive_interval(value: str) -> float:
    """校验命令行周期必须为正数。"""
    try:
        interval = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("记录周期必须是数字") from exc
    if interval <= 0:
        raise argparse.ArgumentTypeError("记录周期必须大于零")
    return interval


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析组合盈亏基准记录器命令行参数。"""
    parser = argparse.ArgumentParser(description="周期性记录组合级盈亏基准")
    parser.add_argument(
        "--interval",
        type=_positive_interval,
        default=DEFAULT_INTERVAL_SECONDS,
        help="记录周期秒数（默认 900）",
    )
    parser.add_argument("--once", action="store_true", help="只记录一次后退出")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSONL 输出路径（默认 {DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--volume-output",
        type=Path,
        default=DEFAULT_VOLUME_OUTPUT,
        help=f"累计成交量 JSONL 输出路径（默认 {DEFAULT_VOLUME_OUTPUT}）",
    )
    parser.add_argument(
        "--hyperliquid-prefix",
        default="HYPERLIQUID",
        help="第一个 Hyperliquid 账户环境变量前缀",
    )
    parser.add_argument(
        "--hyperliquid-var-prefix",
        default="HYPERLIQUID_VAR",
        help="第二个 Hyperliquid 账户环境变量前缀",
    )
    return parser.parse_args(argv)


def main() -> None:
    """加载本地环境并启动组合级权益记录循环。"""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    args = parse_args()
    readers = build_default_readers(
        hyperliquid_prefix=args.hyperliquid_prefix,
        hyperliquid_var_prefix=args.hyperliquid_var_prefix,
    )
    volume_readers = build_default_volume_readers(
        hyperliquid_prefix=args.hyperliquid_prefix,
        hyperliquid_var_prefix=args.hyperliquid_var_prefix,
    )
    asyncio.run(
        run_recorder(
            readers,
            args.output,
            interval=args.interval,
            once=args.once,
            volume_readers=volume_readers,
            volume_output=args.volume_output,
        )
    )


if __name__ == "__main__":
    main()
