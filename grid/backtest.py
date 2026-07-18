"""网格回测：用 Extended 历史K线 + 恐惧贪婪历史，验证"中性网格 + 急停"是否值得做。

模型（mark-to-market，精确）：
- 几何网格，格距 spacing_pct。价格每跌一格买一份、每涨一格卖一份（中性，库存可正可负）。
- 权益 = 现金 + 库存×现价。震荡时格差利润推高权益；趋势时库存浮亏拖累权益。
- 急停：regime=OFF 时平掉库存、暂停交易，封住趋势亏损。
- 硬约束：|库存名义| ≤ max_inventory_usd。

对比三条曲线：网格+急停 / 网格无急停 / 单纯持有BTC，看急停是否有价值。
数据不入库（data/ 已 gitignore）。
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import asyncio  # noqa: E402
import math  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import urllib.request  # noqa: E402
import json  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from grid.regime import GridMode, adx, decide_mode, donchian_prev  # noqa: E402


@dataclass
class Candle:
    ts: int  # ms
    o: float
    h: float
    l: float
    c: float


async def fetch_candles(market: str, hours: int) -> list[Candle]:
    """分页拉取 hours 根小时K线（Extended 单次 limit 有上限，用 end_time 往前翻）。"""
    ensure_ssl_cert()
    from dotenv import load_dotenv

    load_dotenv()
    ext = ExtendedClient.from_env()
    await ext.connect()
    out: dict[int, Candle] = {}
    end = datetime.now(timezone.utc)
    try:
        while len(out) < hours:
            r = await ext._client.info.get_candles_history(
                market_name=market, candle_type="trades", interval="PT1H",
                limit=2000, end_time=end,
            )
            batch = r.data or []
            if not batch:
                break
            for k in batch:
                out[int(k.timestamp)] = Candle(int(k.timestamp), float(k.open), float(k.high), float(k.low), float(k.close))
            oldest = min(int(k.timestamp) for k in batch)
            new_end = datetime.fromtimestamp(oldest / 1000, timezone.utc) - timedelta(hours=1)
            if new_end >= end:
                break
            end = new_end
    finally:
        await ext.close()
    return [out[t] for t in sorted(out)][-hours:]


def fetch_fng() -> dict[int, int]:
    """恐惧贪婪指数历史：返回 {日期(UTC零点epoch秒): value}。"""
    url = "https://api.alternative.me/fng/?limit=0"
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read())
    out = {}
    for row in data.get("data", []):
        out[int(row["timestamp"])] = int(row["value"])
    return out


def _fng_for(ts_ms: int, fng: dict[int, int]) -> int | None:
    """取该时刻当天的恐惧贪婪值。"""
    day = int(ts_ms // 1000 // 86400 * 86400)
    return fng.get(day)


@dataclass
class Result:
    label: str
    pnl: float
    max_drawdown: float
    trades: int
    off_ratio: float
    max_inventory_usd: float
    equity_curve: list[float] = field(default_factory=list)


def simulate(
    candles: list[Candle],
    fng: dict[int, int],
    *,
    spacing_pct: float = 0.006,
    unit_usd: float = 50.0,
    max_inventory_usd: float = 400.0,
    fee_bps: float = 2.0,
    use_killswitch: bool = True,
    adx_period: int = 14,
    donchian_period: int = 48,
    adx_off: float = 30.0,
) -> Result:
    """跑一遍网格模拟，返回结果。"""
    highs = [c.h for c in candles]
    lows = [c.l for c in candles]
    closes = [c.c for c in candles]
    adx_series = adx(highs, lows, closes, adx_period)
    dc_up, dc_lo = donchian_prev(highs, lows, donchian_period)

    log_step = math.log(1 + spacing_pct)
    cash = 0.0
    inv = 0.0  # BTC
    last_level = None
    trades = 0
    off_count = 0
    fee = fee_bps / 1e4
    equity_curve = []
    peak = 0.0
    max_dd = 0.0
    max_inv_usd = 0.0

    for i, c in enumerate(candles):
        price = c.c
        level = round(math.log(price) / log_step)
        if last_level is None:
            last_level = level

        mode = GridMode.NEUTRAL
        if use_killswitch:
            mode = decide_mode(
                adx_val=adx_series[i], close=price,
                donchian_up=dc_up[i], donchian_lo=dc_lo[i],
                fng=_fng_for(c.ts, fng), adx_off=adx_off,
            )

        if mode is GridMode.OFF:
            off_count += 1
            if inv != 0:  # 平库存
                cash += inv * price - abs(inv * price) * fee
                trades += 1
                inv = 0.0
            last_level = level
        else:
            # 逐格成交（几何网格）
            if level < last_level:  # 跌：买
                for lv in range(last_level - 1, level - 1, -1):
                    if abs(inv * price) >= max_inventory_usd and inv > 0:
                        break
                    lp = math.exp(lv * log_step)
                    q = unit_usd / lp
                    cash -= lp * q + lp * q * fee
                    inv += q
                    trades += 1
            elif level > last_level:  # 涨：卖
                for lv in range(last_level + 1, level + 1):
                    if abs(inv * price) >= max_inventory_usd and inv < 0:
                        break
                    lp = math.exp(lv * log_step)
                    q = unit_usd / lp
                    cash += lp * q - lp * q * fee
                    inv -= q
                    trades += 1
            last_level = level

        equity = cash + inv * price
        equity_curve.append(equity)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        max_inv_usd = max(max_inv_usd, abs(inv * price))

    return Result(
        label="网格+急停" if use_killswitch else "网格无急停",
        pnl=equity_curve[-1] if equity_curve else 0.0,
        max_drawdown=max_dd,
        trades=trades,
        off_ratio=off_count / len(candles) if candles else 0.0,
        max_inventory_usd=max_inv_usd,
        equity_curve=equity_curve,
    )


def buy_hold(candles: list[Candle], unit_usd: float, max_inventory_usd: float) -> float:
    """参照：期初用 max_inventory_usd 买 BTC 持有到期末的盈亏。"""
    if not candles:
        return 0.0
    qty = max_inventory_usd / candles[0].c
    return qty * (candles[-1].c - candles[0].c)


async def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="网格回测")
    p.add_argument("--hours", type=int, default=6000, help="回测小时K线数(默认约8个月)")
    p.add_argument("--spacing", type=float, default=0.006, help="格距(默认0.6%)")
    p.add_argument("--unit", type=float, default=50.0, help="每格名义USD")
    p.add_argument("--max-inv", type=float, default=400.0, help="最大库存名义USD")
    args = p.parse_args()

    print(f"拉取 {args.hours} 根小时K线 …")
    candles = await fetch_candles("BTC-USD", args.hours)
    print(f"拿到 {len(candles)} 根，区间 "
          f"{datetime.fromtimestamp(candles[0].ts/1000, timezone.utc):%Y-%m-%d} ~ "
          f"{datetime.fromtimestamp(candles[-1].ts/1000, timezone.utc):%Y-%m-%d}")
    print("拉取恐惧贪婪历史 …")
    fng = fetch_fng()
    print(f"恐惧贪婪 {len(fng)} 天\n")

    kw = simulate(candles, fng, spacing_pct=args.spacing, unit_usd=args.unit,
                  max_inventory_usd=args.max_inv, use_killswitch=True)
    no_kw = simulate(candles, fng, spacing_pct=args.spacing, unit_usd=args.unit,
                     max_inventory_usd=args.max_inv, use_killswitch=False)
    bh = buy_hold(candles, args.unit, args.max_inv)

    for r in (kw, no_kw):
        print(f"【{r.label}】盈亏 ${r.pnl:+.1f} | 最大回撤 ${r.max_drawdown:.1f} | "
              f"成交 {r.trades} | OFF占比 {r.off_ratio:.0%} | 峰值库存 ${r.max_inventory_usd:.0f}")
    print(f"【单纯持有BTC同资金】盈亏 ${bh:+.1f}")
    span_days = (candles[-1].ts - candles[0].ts) / 1000 / 86400
    if kw.max_inventory_usd > 0:
        ann = kw.pnl / args.max_inv * (365 / span_days) * 100
        print(f"\n网格+急停：约 {span_days:.0f} 天，相对最大库存${args.max_inv}的年化 ≈ {ann:+.0f}%")


if __name__ == "__main__":
    asyncio.run(main())
