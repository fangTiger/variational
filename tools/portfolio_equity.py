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

EquityReader = Callable[[], Awaitable[Decimal]]
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


DEFAULT_VOLUME_INSTANCES = (
    VolumeInstanceConfig(
        key="lighter_entropy",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume.jsonl",
        symbol="BTC",
        primary_source="lighter",
        hedge_source="hyperliquid",
    ),
    VolumeInstanceConfig(
        key="variational_entropy",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_var.jsonl",
        symbol="BTC",
        primary_source="variational",
        hedge_source="hyperliquid_var",
    ),
    VolumeInstanceConfig(
        key="lighter_variational_eth",
        heartbeat_path=PROJECT_ROOT / "data" / "timed_volume_eth.jsonl",
        symbol="ETH",
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


def _symbol(value: object, *, label: str) -> str:
    """规范化成交币种，拒绝空值和带分隔符的异常名称。"""
    if not isinstance(value, str):
        raise ValueError(f"{label} 不是字符串")
    symbol = value.strip().upper()
    if not symbol or "|" in symbol:
        raise ValueError(f"{label} 无效")
    return symbol


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


async def read_lighter_equity(client: Any) -> Decimal:
    """读取 Lighter 的 ``total_asset_value`` 权益口径。"""
    balance = await client.get_balance()
    return _decimal(getattr(balance, "equity", None), label="Lighter 账户权益")


async def read_variational_equity(client: Any) -> Decimal:
    """读取 Variational 的 ``balance + upnl`` 权益口径。"""
    balance = await client.get_balance()
    cash = _decimal(getattr(balance, "balance", None), label="Variational 余额")
    upnl = _decimal(getattr(balance, "upnl", None), label="Variational 未实现盈亏")
    return cash + upnl


async def read_hyperliquid_equity(client: Any) -> Decimal:
    """读取 Hyperliquid 的 Spot USDC 加全部未实现盈亏。

    ``marginSummary.accountValue`` 是统一账户占用的诊断值，不能与 Spot
    USDC 相加；这里故意只使用余额对象中的两个明确分项。
    """
    balance = await client.get_balance()
    spot = _decimal(
        getattr(balance, "spot_usdc_total", None),
        label="Hyperliquid Spot USDC",
    )
    upnl = _decimal(
        getattr(balance, "unrealized_pnl_total", None),
        label="Hyperliquid 未实现盈亏",
    )
    return spot + upnl


async def _read_lighter_from_env() -> Decimal:
    """用只读 Lighter 客户端采集默认实盘账户。"""
    from adapters.lighter_client import LighterClient

    address = (os.getenv("LIGHTER_RH_L1_ADDRESS") or "").strip()
    if not address:
        raise RuntimeError("缺少 LIGHTER_RH_L1_ADDRESS 环境变量")
    client = LighterClient(l1_address=address)
    try:
        await client.connect()
        return await read_lighter_equity(client)
    finally:
        await client.close()


async def _read_variational_from_env() -> Decimal:
    """用只读 Variational 客户端采集默认实盘账户。"""
    from adapters.variational_client import Session, VariationalClient

    client = VariationalClient(Session.from_env())
    try:
        return await read_variational_equity(client)
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

    async def read() -> Decimal:
        from adapters.hyperliquid_client import HyperliquidClient

        client = HyperliquidClient.from_env(prefix=prefix, trading_enabled=False)
        try:
            await client.connect()
            return await read_hyperliquid_equity(client)
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
        "variational": _read_variational_from_env,
        "hyperliquid": _hyperliquid_reader(hyperliquid_prefix),
        "hyperliquid_var": _hyperliquid_reader(hyperliquid_var_prefix),
    }


def build_default_volume_readers(
    *,
    hyperliquid_prefix: str = "HYPERLIQUID",
    hyperliquid_var_prefix: str = "HYPERLIQUID_VAR",
) -> dict[str, VolumeReader]:
    """创建三个支持实例起点与币种过滤的只读成交采集函数。"""
    return {
        "hyperliquid": _hyperliquid_volume_reader(hyperliquid_prefix),
        "hyperliquid_var": _hyperliquid_volume_reader(hyperliquid_var_prefix),
        "variational": _read_variational_volume_from_env,
    }


async def collect_snapshot(
    readers: Mapping[str, EquityReader],
    *,
    timestamp: float | None = None,
) -> dict[str, object]:
    """并发采集账户；失败账户只写错误标注，不阻断其他账户。"""

    async def read_one(name: str, reader: EquityReader) -> Decimal:
        return _decimal(await reader(), label=f"{name} 账户权益")

    names = tuple(readers)
    results = await asyncio.gather(
        *(read_one(name, readers[name]) for name in names),
        return_exceptions=True,
    )
    accounts: dict[str, str] = {}
    errors: dict[str, str] = {}
    total = Decimal("0")
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors[name] = f"{type(result).__name__}: {result}"
            continue
        accounts[name] = _decimal_text(result)
        total += result

    snapshot: dict[str, object] = {
        "ts": time.time() if timestamp is None else timestamp,
        "accounts": accounts,
        "total_equity": _decimal_text(total),
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
        starts[config.key] = since
        symbols[config.key] = symbol
        decimal_instances[config.key] = {}
        for leg, source in (
            ("primary", config.primary_source),
            ("hedge", config.hedge_source),
        ):
            if source != "lighter":
                jobs.append((config.key, leg, source, since, symbol))

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

    estimated_legs: dict[str, list[str]] = {
        key: [] for key in decimal_instances
    }
    for config in instances:
        legs = decimal_instances.get(config.key)
        if legs is None:
            continue
        if config.primary_source == "lighter" and "hedge" in legs:
            legs["primary"] = legs["hedge"]
            estimated_legs[config.key].append("primary")
        if config.hedge_source == "lighter" and "primary" in legs:
            legs["hedge"] = legs["primary"]
            estimated_legs[config.key].append("hedge")

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
        serialized["estimated"] = estimated_legs[key]
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
    timestamp: float | None = None,
) -> dict[str, object]:
    """采集并落盘一次；账户失败由快照自身承载，不向外抛出。"""
    snapshot = await collect_snapshot(readers, timestamp=timestamp)
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
    print(
        f"组合权益已记录：成功 {count} 个账户，总权益 "
        f"{snapshot.get('total_equity', '—')}",
        flush=True,
    )
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
    """解析组合权益记录器命令行参数。"""
    parser = argparse.ArgumentParser(description="周期性记录组合级账户权益")
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
