"""权益快照工具：采集两账户总权益/积分/资金费并追加到 JSONL。

被 tools/track_equity.py 与 tools/run_hedge_bot.py 共用。适配器为鸭子类型，
模块本身不导入 x10/curl_cffi，可安全被纯逻辑代码引用。
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from tracking.monitor import compute_funding_view

UNDERLYING = "BTC"
EXT_MARKET = "BTC-USD"
EQUITY_FILE = Path(__file__).resolve().parent.parent / "data" / "equity_track.jsonl"


async def build_snapshot(var: Any, ext: Any) -> dict:
    """采集一次两账户权益/持仓/积分/资金费快照。"""
    port = await var.raw("/portfolio")
    var_equity = Decimal(str(port["balance"])) + Decimal(str(port.get("upnl", "0")))

    bal = await ext.get_balance()
    ext_equity = Decimal(str(getattr(bal, "equity", 0) or 0))

    stats = await ext._client.info.get_market_statistics(market_name=EXT_MARKET)
    mark = Decimal(str(stats.data.mark_price))
    var_rate = await var.get_funding_rate(UNDERLYING)
    fv = compute_funding_view(var_rate, Decimal(str(stats.data.funding_rate)))

    vp = await var.get_position(UNDERLYING)
    ep = await ext.get_position(EXT_MARKET)
    pts = await var.get_points_summary()

    return {
        "ts": time.time(),
        "btc": float(mark),
        "notional": float(abs(vp.signed_size) * mark),
        "net_delta": float(vp.signed_size + ep.signed_size),
        "var_equity": float(var_equity),
        "ext_equity": float(ext_equity),
        "total_equity": float(var_equity + ext_equity),
        "points_total": float(pts["total_points"]),
        "carry_pct_8h": float(fv.carry_short_var_pct_8h),
        "annualized_pct_est": float(fv.annualized_pct),
    }


async def snapshot_and_append(var: Any, ext: Any) -> dict:
    """采集快照并追加到 EQUITY_FILE，返回该快照。"""
    snap = await build_snapshot(var, ext)
    EQUITY_FILE.parent.mkdir(exist_ok=True)
    with EQUITY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    return snap


def load_snapshots() -> list[dict]:
    """读取全部历史快照。"""
    if not EQUITY_FILE.exists():
        return []
    return [json.loads(l) for l in EQUITY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
