"""周期性记录全部实盘账户的组合级权益。

记录器只做账户查询和 JSONL 追加，不参与任何策略、下单或风控流程。单个
账户读取失败会从当次合计中跳过，并在同一条记录的 ``errors`` 字段标注。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "portfolio_equity.jsonl"
DEFAULT_INTERVAL_SECONDS = 900.0

EquityReader = Callable[[], Awaitable[Decimal]]


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


def append_snapshot(path: Path | str, snapshot: Mapping[str, object]) -> None:
    """把一条组合权益快照追加为单行 JSON。"""
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


async def run_recorder(
    readers: Mapping[str, EquityReader],
    output: Path | str,
    *,
    interval: float,
    once: bool,
) -> None:
    """按固定间隔持续记账；单账户故障不会退出循环。"""
    while True:
        snapshot = await record_once(readers, output)
        _print_result(snapshot)
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
    asyncio.run(
        run_recorder(
            readers,
            args.output,
            interval=args.interval,
            once=args.once,
        )
    )


if __name__ == "__main__":
    main()
