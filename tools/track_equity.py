"""权益追踪器（阶段 A 实测核心）：记录两账户总权益 + 积分 + 资金费。

对冲为 delta 中性 → 两腿价格盈亏基本抵消 → 两账户总权益随时间的变化
≈ 实际净资金费 − 磨损。据此校准监控估算的年化 carry。

每次运行记录一条快照到 data/equity_track.jsonl；若已有历史，打印自首条以来的
权益变化、折算年化实际 carry、积分变化。

用法（在能读到两边账户的机器上跑；只读，不下单）：
    PYTHONPATH=. .venv/bin/python -m tools.track_equity
    # 循环每小时记录
    PYTHONPATH=. .venv/bin/python -m tools.track_equity --interval 3600
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from adapters.variational_client import Session, VariationalClient  # noqa: E402
from tracking.monitor import compute_funding_view  # noqa: E402

UNDERLYING = "BTC"
EXT_MARKET = "BTC-USD"
_FILE = Path(__file__).resolve().parent.parent / "data" / "equity_track.jsonl"
_SECONDS_PER_YEAR = Decimal(365 * 24 * 3600)


async def _snapshot(var: VariationalClient, ext: ExtendedClient) -> dict:
    port = await var.raw("/portfolio")
    var_bal = Decimal(str(port["balance"]))
    var_upnl = Decimal(str(port.get("upnl", "0")))
    var_equity = var_bal + var_upnl

    bal = await ext.get_balance()
    ext_equity = Decimal(str(getattr(bal, "equity", 0)))

    stats = await ext._client.info.get_market_statistics(market_name=EXT_MARKET)
    var_rate = await var.get_funding_rate(UNDERLYING)
    fv = compute_funding_view(var_rate, Decimal(str(stats.data.funding_rate)))

    vp = await var.get_position(UNDERLYING)
    ep = await ext.get_position(EXT_MARKET)
    mark = Decimal(str(stats.data.mark_price))
    notional = abs(vp.signed_size) * mark

    pts = await var.get_points_summary()

    return {
        "ts": time.time(),
        "btc": float(mark),
        "notional": float(notional),
        "net_delta": float(vp.signed_size + ep.signed_size),
        "var_equity": float(var_equity),
        "ext_equity": float(ext_equity),
        "total_equity": float(var_equity + ext_equity),
        "points_total": float(pts["total_points"]),
        "carry_pct_8h": float(fv.carry_short_var_pct_8h),
        "annualized_pct_est": float(fv.annualized_pct),
    }


def _report(snaps: list[dict]) -> None:
    cur = snaps[-1]
    print(
        f"总权益=${cur['total_equity']:.4f}"
        f"（Var ${cur['var_equity']:.4f} + Ext ${cur['ext_equity']:.4f}）"
    )
    print(f"净 delta={cur['net_delta']:.5f}  名义≈${cur['notional']:.0f}  积分={cur['points_total']}")
    print(f"资金费估算 carry {cur['carry_pct_8h']:+.4f}%/8h（年化估 {cur['annualized_pct_est']:+.1f}%）")

    if len(snaps) < 2:
        print("（首条快照，持有一段时间后再跑即可对比出真实 carry）")
        return

    first = snaps[0]
    span = Decimal(str(cur["ts"] - first["ts"]))
    if span <= 0:
        return
    hours = span / 3600
    eq_change = Decimal(str(cur["total_equity"])) - Decimal(str(first["total_equity"]))
    notional = Decimal(str(cur["notional"])) or Decimal(1)
    # 实际年化 carry ≈ (总权益变化 / 名义) / 持有年数
    annualized_real = eq_change / notional * (_SECONDS_PER_YEAR / span) * 100
    pts_change = Decimal(str(cur["points_total"])) - Decimal(str(first["points_total"]))
    print(
        f"\n自首条快照起：持有 {hours:.1f}h\n"
        f"  总权益变化 ${eq_change:+.4f} → **实测年化 carry {annualized_real:+.1f}%**\n"
        f"  积分变化 {pts_change:+.4f}\n"
        f"  （与估算 {cur['annualized_pct_est']:+.1f}% 对比，校准资金费单位）"
    )


async def _run_once(var: VariationalClient, ext: ExtendedClient) -> None:
    snap = await _snapshot(var, ext)
    _FILE.parent.mkdir(exist_ok=True)
    with _FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    snaps = [json.loads(l) for l in _FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    _report(snaps)


async def _main(interval: float | None) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    var = VariationalClient(Session.from_env())
    ext = ExtendedClient.from_env()
    await ext.connect()
    try:
        while True:
            try:
                await _run_once(var, ext)
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️ 采集异常：{type(exc).__name__}: {exc}")
            if interval is None:
                break
            print(f"—— 等待 {interval:.0f}s ——\n")
            await asyncio.sleep(interval)
    finally:
        await var.close()
        await ext.close()


def main() -> None:
    p = argparse.ArgumentParser(description="权益追踪（阶段A实测）")
    p.add_argument("--interval", type=float, default=None, help="循环间隔秒（省略=单次）")
    args = p.parse_args()
    asyncio.run(_main(args.interval))


if __name__ == "__main__":
    main()
