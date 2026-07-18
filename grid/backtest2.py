"""严谨版网格回测：成交区间(保守~乐观) + 资金费 + 样本外验证 + 完整指标。

改进 grid/backtest.py 的乐观假设：
- 成交模型两套给区间：
  · conservative(close)：收盘对收盘，低估成交（下界）
  · optimistic(path)：用K线内 高低点路径 判定成交，高估成交（上界）
  真实介于两者之间。
- 计入资金费成本（库存×小时费率）。
- 样本外：前 60% 选格距、后 40% 检验。
- 指标：盈亏、年化、最大回撤、Sharpe、成交数、OFF占比。
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import asyncio  # noqa: E402
import math  # noqa: E402
import statistics  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from grid.backtest import Candle, fetch_candles, fetch_fng, _fng_for  # noqa: E402
from grid.regime import GridMode, adx, decide_mode, donchian_prev  # noqa: E402

_HOURS_PER_YEAR = 24 * 365


async def fetch_funding(market: str, days: int) -> dict[int, float]:
    """拉资金费历史，返回 {小时epoch秒: rate}。"""
    from dotenv import load_dotenv

    load_dotenv()
    ext = ExtendedClient.from_env()
    await ext.connect()
    out: dict[int, float] = {}
    end = datetime.now(timezone.utc)
    try:
        for _ in range(days // 25 + 2):
            start = end - timedelta(days=25)
            r = await ext._client.info.get_funding_rates_history(
                market_name=market, start_time=start, end_time=end)
            batch = r.data or []
            if not batch:
                break
            for k in batch:
                hour = int(int(k.timestamp) // 1000 // 3600 * 3600)
                out[hour] = float(k.funding_rate)
            end = start - timedelta(hours=1)
    finally:
        await ext.close()
    return out


@dataclass
class Metrics:
    label: str
    pnl: float
    annualized_pct: float
    max_dd: float
    sharpe: float
    trades: int
    off_ratio: float
    funding_pnl: float


def _levels_between(p0, p1, step, last_level):
    """价格从 p0 到 p1 跨过的格子(方向)，返回 [(level_price, +1买/-1卖), ...]。"""
    lv0 = round(math.log(p0) / step)
    lv1 = round(math.log(p1) / step)
    trades = []
    if lv1 < lv0:      # 下跌 → 买
        for lv in range(lv0 - 1, lv1 - 1, -1):
            trades.append((math.exp(lv * step), 1))
    elif lv1 > lv0:    # 上涨 → 卖
        for lv in range(lv0 + 1, lv1 + 1):
            trades.append((math.exp(lv * step), -1))
    return trades


def simulate(
    candles: list[Candle], fng, funding: dict[int, float],
    *, spacing_pct=0.02, unit_usd=50.0, max_inventory_usd=400.0, fee_bps=1.0,
    fill="path", adx_off=30.0, adx_period=14, donchian_period=48,
) -> Metrics:
    highs = [c.h for c in candles]; lows = [c.l for c in candles]; closes = [c.c for c in candles]
    a = adx(highs, lows, closes, adx_period)
    up, lo = donchian_prev(highs, lows, donchian_period)
    step = math.log(1 + spacing_pct)
    fee = fee_bps / 1e4
    cash = 0.0; inv = 0.0; trades = 0; off = 0; funding_pnl = 0.0
    last_level = None
    equity = []; peak = 0.0; max_dd = 0.0
    prev_eq = 0.0; rets = []

    for i, c in enumerate(candles):
        mode = decide_mode(adx_val=a[i], close=c.c, donchian_up=up[i], donchian_lo=lo[i],
                           fng=_fng_for(c.ts, fng), adx_off=adx_off)
        if last_level is None:
            last_level = round(math.log(c.c) / step)

        if mode is GridMode.OFF:
            off += 1
            if inv != 0:
                cash += inv * c.c - abs(inv * c.c) * fee; trades += 1; inv = 0.0
            last_level = round(math.log(c.c) / step)
        else:
            # 成交路径
            if fill == "path":
                path = [c.o, c.l, c.h, c.c] if c.c >= c.o else [c.o, c.h, c.l, c.c]
            else:  # conservative：只看收盘
                path = [closes[i - 1] if i else c.o, c.c]
            for j in range(len(path) - 1):
                for lp, direction in _levels_between(path[j], path[j + 1], step, last_level):
                    if direction == 1 and inv * c.c >= max_inventory_usd:
                        continue
                    if direction == -1 and -inv * c.c >= max_inventory_usd:
                        continue
                    q = unit_usd / lp
                    cash -= direction * lp * q + lp * q * fee
                    inv += direction * q
                    trades += 1
            last_level = round(math.log(c.c) / step)

        # 资金费：库存×费率（正费率多头付）
        f = funding.get(int(c.ts // 1000 // 3600 * 3600))
        if f and inv != 0:
            cost = inv * c.c * f
            cash -= cost; funding_pnl -= cost

        eq = cash + inv * c.c
        equity.append(eq)
        peak = max(peak, eq); max_dd = max(max_dd, peak - eq)
        rets.append(eq - prev_eq); prev_eq = eq

    days = (candles[-1].ts - candles[0].ts) / 1000 / 86400 if len(candles) > 1 else 1
    pnl = equity[-1] if equity else 0.0
    ann = pnl / max_inventory_usd * (365 / days) * 100 if days else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (statistics.mean(rets) / sd * math.sqrt(_HOURS_PER_YEAR)) if sd else 0.0
    return Metrics(
        label=f"{fill}/格距{spacing_pct*100:.1f}%",
        pnl=pnl, annualized_pct=ann, max_dd=max_dd, sharpe=sharpe,
        trades=trades, off_ratio=off / len(candles) if candles else 0, funding_pnl=funding_pnl,
    )


async def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=12000)
    p.add_argument("--max-inv", type=float, default=400.0)
    args = p.parse_args()

    print(f"拉取 {args.hours} 根小时K线 + 资金费 + 恐惧贪婪 …")
    candles = await fetch_candles("BTC-USD", args.hours)
    fng = fetch_fng()
    days = int((candles[-1].ts - candles[0].ts) / 1000 / 86400)
    funding = await fetch_funding("BTC-USD", days + 5)
    print(f"K线 {len(candles)} 根 / {days} 天，资金费 {len(funding)} 小时\n")

    def show(cs, tag):
        print(f"===== {tag}（{len(cs)}根）=====")
        for sp in [0.015, 0.02, 0.025]:
            cons = simulate(cs, fng, funding, spacing_pct=sp, max_inventory_usd=args.max_inv, fill="close")
            opt = simulate(cs, fng, funding, spacing_pct=sp, max_inventory_usd=args.max_inv, fill="path")
            print(f"  格距{sp*100:.1f}%  盈亏 ${cons.pnl:+.0f}~${opt.pnl:+.0f}  "
                  f"年化 {cons.annualized_pct:+.0f}%~{opt.annualized_pct:+.0f}%  "
                  f"回撤 ${cons.max_dd:.0f}~${opt.max_dd:.0f}  "
                  f"Sharpe {cons.sharpe:.1f}~{opt.sharpe:.1f}  资金费${opt.funding_pnl:+.0f}")

    show(candles, "全周期")
    n = len(candles)
    show(candles[:int(n * 0.6)], "样本内(前60%)")
    show(candles[int(n * 0.6):], "样本外(后40%)")


if __name__ == "__main__":
    asyncio.run(main())
