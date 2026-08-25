"""基差高频采样器（只读，不下单）。

用途：测定两侧基差的均值回归时间尺度，为「持仓时长」与「基差择向」提供依据。

背景：2026-08-25 的台账分析显示，单轮成本精确等于
``-方向 × 基差变动 + 手续费``，且入场基差与后续基差变动在 10 分钟
horizon 上相关系数 -0.783（t=-3.78, df=9）。但该相关性只在 10 分钟
尺度上验证过，换成更长持仓是否仍成立未知——这正是本采样器要回答的。

本工具**不下任何单**，只读盘口与报价。
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402

from adapters.hyperliquid_client import HyperliquidClient  # noqa: E402
from adapters.variational_client import Session, VariationalClient  # noqa: E402

logger = logging.getLogger("sample_basis")

#: 采样间隔。Variational 报价走的是真实 RFQ 调用，间隔过密会给对手方
#: 造成不必要的询价压力，20 秒足以刻画分钟级的回归过程。
DEFAULT_INTERVAL_SECONDS = 20.0


def _mid(bid: Decimal, ask: Decimal) -> Decimal:
    """取中价。"""
    return (bid + ask) / 2


async def sample_once(
    variational: VariationalClient,
    hyperliquid: HyperliquidClient,
    market: str,
    hedge_market: str,
) -> dict[str, object]:
    """采集一次两侧盘口并计算基差。"""
    primary, hedge = await asyncio.gather(
        variational.get_market_price(market),
        hyperliquid.get_market_price(hedge_market),
    )
    primary_mid = _mid(primary.bid, primary.ask)
    hedge_mid = _mid(hedge.bid, hedge.ask)
    basis_pct = (primary_mid - hedge_mid) / hedge_mid * 100
    return {
        "ts": time.time(),
        "market": market,
        "primary_bid": str(primary.bid),
        "primary_ask": str(primary.ask),
        "primary_mid": str(primary_mid),
        "hedge_bid": str(hedge.bid),
        "hedge_ask": str(hedge.ask),
        "hedge_mid": str(hedge_mid),
        "basis_pct": str(basis_pct),
        "primary_spread_pct": str((primary.ask - primary.bid) / primary_mid * 100),
        "hedge_spread_pct": str((hedge.ask - hedge.bid) / hedge_mid * 100),
    }


async def run(args: argparse.Namespace) -> None:
    """循环采样直到被中断。"""
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    variational = VariationalClient(Session.from_env())
    hyperliquid = HyperliquidClient(perp_dexs=["", "io"])
    await hyperliquid.connect()
    consecutive_failures = 0
    try:
        while True:
            try:
                record = await sample_once(
                    variational,
                    hyperliquid,
                    args.market,
                    args.hedge_market,
                )
            except Exception as exc:  # noqa: BLE001 - 采样失败不应中断长跑
                consecutive_failures += 1
                logger.warning("采样失败第 %d 次：%s", consecutive_failures, exc)
                await asyncio.sleep(args.interval)
                continue
            consecutive_failures = 0
            with output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info(
                "基差=%s%%  主=%s  对冲=%s",
                record["basis_pct"][:8],
                record["primary_mid"],
                record["hedge_mid"],
            )
            await asyncio.sleep(args.interval)
    finally:
        for client in (variational, hyperliquid):
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响采样结果
                pass


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数。"""
    parser = argparse.ArgumentParser(description="基差高频采样（只读）")
    parser.add_argument("--market", default="SNDK", help="Variational 侧标的")
    parser.add_argument(
        "--hedge-market",
        default="io:SNDK",
        help="Hyperliquid 侧标的",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"采样间隔秒数（默认 {DEFAULT_INTERVAL_SECONDS:g}）",
    )
    parser.add_argument(
        "--output",
        default="data/basis_samples_sndk.jsonl",
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
