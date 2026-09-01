"""io:SNDK × Lighter-RH:SNDK 可成交边际采样器（只读，不下单）。

与 ``tools/sample_basis.py`` 的关键区别：**只记录并计算可成交价**。

背景（2026-08-31 的失败教训）：上一版信号策略用 5 分钟 K 线收盘价算基差，
而收盘价是最后成交价，在薄盘上会在买卖价之间跳动，机械地制造出均值回归。
回测因此出现 100% 胜率，实盘首轮却方向相反——信号说基差有利变动 +0.0634%，
按成交价算实际是 -0.0133%。

所以这一版从采集到判据全程只用 bid/ask，一次都不碰中价和收盘价。

记录的两个方向化边际（已扣双边价差，尚未扣手续费）：

    卖边际 = (io买价 - Lighter卖价) / Lighter卖价    # 卖 io、买 Lighter
    买边际 = (io卖价 - Lighter买价) / Lighter买价    # 买 io、卖 Lighter

真实可得边际还需再扣手续费，见 ``IO_TAKER_FEE_PCT``。
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402

logger = logging.getLogger("sample_edge")

#: 实测费率（2026-08-30 用 $200 实盘成交验证）。Lighter 挂单吃单均为 0。
IO_TAKER_FEE_PCT = Decimal("0.0100")
IO_MAKER_FEE_PCT = Decimal("0.0040")
LIGHTER_FEE_PCT = Decimal("0")

#: 一个完整来回（开+平）两腿全吃单的成本，用于判断边际是否够本。
ROUND_TRIP_COST_PCT = (IO_TAKER_FEE_PCT + LIGHTER_FEE_PCT) * 2

DEFAULT_INTERVAL_SECONDS = 10.0


def _hl_book(coin: str, timeout: float = 10.0) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """取 Hyperliquid 某标的的买一卖一及对应挂单量。"""
    request = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps({"type": "l2Book", "coin": coin}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        book = json.loads(response.read())
    levels = book["levels"]
    if not levels[0] or not levels[1]:
        raise ValueError(f"{coin} 盘口为空")
    bid = levels[0][0]
    ask = levels[1][0]
    return (
        Decimal(bid["px"]),
        Decimal(ask["px"]),
        Decimal(bid["sz"]),
        Decimal(ask["sz"]),
    )


async def sample_once(lighter_client, market: str, hl_coin: str) -> dict[str, object]:
    """采集一次两侧可成交价并算出方向化边际。"""
    io_bid, io_ask, io_bid_sz, io_ask_sz = await asyncio.to_thread(_hl_book, hl_coin)
    price = await lighter_client.get_market_price(market)
    if price is None:
        raise ValueError("Lighter 盘口不可用")
    lt_bid, lt_ask = price.bid, price.ask

    # 方向化边际：分子分母都用真实可成交价，不用中价
    sell_edge = (io_bid - lt_ask) / lt_ask * Decimal("100")
    buy_edge = (io_ask - lt_bid) / lt_bid * Decimal("100")

    return {
        "ts": time.time(),
        "io_bid": str(io_bid),
        "io_ask": str(io_ask),
        "io_bid_sz": str(io_bid_sz),
        "io_ask_sz": str(io_ask_sz),
        "lighter_bid": str(lt_bid),
        "lighter_ask": str(lt_ask),
        # 卖 io / 买 Lighter 这个方向的边际
        "sell_edge_pct": str(sell_edge),
        # 买 io / 卖 Lighter 这个方向的边际
        "buy_edge_pct": str(buy_edge),
        "io_spread_pct": str((io_ask - io_bid) / io_bid * Decimal("100")),
        "lighter_spread_pct": str((lt_ask - lt_bid) / lt_bid * Decimal("100")),
    }


async def run(args: argparse.Namespace) -> None:
    """循环采样直到被中断或达到时长上限。"""
    from adapters.lighter_client import LighterClient

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    address = os.environ.get("LIGHTER_RH_L1_ADDRESS")
    if not address:
        raise RuntimeError("缺少 LIGHTER_RH_L1_ADDRESS")
    client = LighterClient(l1_address=address)
    if hasattr(client, "connect"):
        await client.connect()

    deadline = time.time() + args.hours * 3600 if args.hours > 0 else None
    failures = 0
    written = 0
    try:
        while deadline is None or time.time() < deadline:
            try:
                record = await sample_once(client, args.market, args.hl_coin)
            except Exception as exc:  # noqa: BLE001 长跑采样不因单次失败中断
                failures += 1
                logger.warning("采样失败第 %d 次：%s", failures, exc)
                await asyncio.sleep(args.interval)
                continue
            with output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
            if written % 30 == 0:
                logger.info(
                    "已记录 %d 条；最新 卖边际=%s%% 买边际=%s%%（往返成本 %s%%）",
                    written,
                    record["sell_edge_pct"][:8],
                    record["buy_edge_pct"][:8],
                    ROUND_TRIP_COST_PCT,
                )
            await asyncio.sleep(args.interval)
    finally:
        logger.info("采样结束：共 %d 条，失败 %d 次", written, failures)
        try:
            await client.close()
        except Exception:  # noqa: BLE001 关闭失败不影响已落盘数据
            pass


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数。"""
    parser = argparse.ArgumentParser(
        description="io:SNDK × Lighter-RH:SNDK 可成交边际采样（只读）"
    )
    parser.add_argument("--market", default="SNDK", help="Lighter 侧标的")
    parser.add_argument("--hl-coin", default="io:SNDK", help="Hyperliquid 侧标的")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"采样间隔秒数（默认 {DEFAULT_INTERVAL_SECONDS:g}）",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=72.0,
        help="采样时长小时数；0 表示一直跑（默认 72）",
    )
    parser.add_argument(
        "--output",
        default="data/edge_samples_io_lighter.jsonl",
        help="输出文件路径",
    )
    return parser


def main() -> None:
    """加载环境并进入采样循环。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
