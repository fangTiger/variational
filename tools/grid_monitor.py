"""网格自动监控（一次性快照，供 launchd 定时跑）。

每次记录：进程是否存活、账户权益、库存、现价、ADX/regime、自起始的 PnL。
写入 data/grid_monitor.jsonl。--report 打印历史汇总（PnL/回撤/OFF占比/存活率）。

用法：
    # 记一条快照（launchd 每小时调）
    PYTHONPATH=. .venv/bin/python -m tools.grid_monitor
    # 看汇总
    PYTHONPATH=. .venv/bin/python -m tools.grid_monitor --report
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from grid.regime import GridMode, adx, decide_mode, donchian_prev  # noqa: E402

_FILE = Path(__file__).resolve().parent.parent / "data" / "grid_monitor.jsonl"
_BASELINE = Path(__file__).resolve().parent.parent / "data" / "grid_baseline.json"
MARKET = "BTC-USD"


def _process_alive() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "tools.run_grid"], capture_output=True, text=True)
        return bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


async def _snapshot() -> dict:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    ext = ExtendedClient.from_env(prefix="X10_GRID")
    await ext.connect()
    try:
        bal = await ext.get_balance()
        equity = float(getattr(bal, "equity", 0) or 0)
        pos = await ext.get_position(MARKET)
        r = await ext._client.info.get_candles_history(
            market_name=MARKET, candle_type="trades", interval="PT1H", limit=200)
        candles = sorted(r.data, key=lambda k: int(k.timestamp))
        highs = [float(k.high) for k in candles]
        lows = [float(k.low) for k in candles]
        closes = [float(k.close) for k in candles]
        a = adx(highs, lows, closes)
        up, lo = donchian_prev(highs, lows, 48)
        price = closes[-1]
        mode = decide_mode(adx_val=a[-1], close=price, donchian_up=up[-1], donchian_lo=lo[-1])
        inv = float(pos.signed_size)
    finally:
        await ext.close()

    # 基线权益（首次记录）
    if _BASELINE.exists():
        base = json.loads(_BASELINE.read_text())["equity"]
    else:
        base = equity
        _BASELINE.parent.mkdir(exist_ok=True)
        _BASELINE.write_text(json.dumps({"equity": equity, "ts": time.time()}))

    return {
        "ts": time.time(),
        "alive": _process_alive(),
        "equity": equity,
        "pnl_since_start": equity - base,
        "inv_btc": inv,
        "inv_usd": inv * price,
        "price": price,
        "adx": round(a[-1], 1) if a[-1] else None,
        "mode": mode.value,
    }


def _record(snap: dict) -> None:
    _FILE.parent.mkdir(exist_ok=True)
    with _FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")


def _load() -> list[dict]:
    if not _FILE.exists():
        return []
    return [json.loads(x) for x in _FILE.read_text(encoding="utf-8").splitlines() if x.strip()]


def _report() -> None:
    snaps = _load()
    if not snaps:
        print("暂无监控记录")
        return
    cur = snaps[-1]
    equities = [s["equity"] for s in snaps]
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)
    off_ratio = sum(1 for s in snaps if s["mode"] == "off") / len(snaps)
    alive_ratio = sum(1 for s in snaps if s["alive"]) / len(snaps)
    span_h = (cur["ts"] - snaps[0]["ts"]) / 3600
    print(f"网格监控汇总（{len(snaps)} 条，跨 {span_h:.1f}h）")
    print(f"  当前权益 ${cur['equity']:.2f}  自起始 PnL ${cur['pnl_since_start']:+.2f}")
    print(f"  当前库存 {cur['inv_btc']:+.5f} BTC(≈${cur['inv_usd']:+.0f})  现价 ${cur['price']:.0f}")
    print(f"  当前 ADX {cur['adx']} / 模式 {cur['mode']}")
    print(f"  权益最大回撤 ${max_dd:.2f}  OFF占比 {off_ratio:.0%}  进程存活率 {alive_ratio:.0%}")
    if not cur["alive"]:
        print("  ⚠️ 最新一次检测进程未存活，请检查 launchd！")


def main() -> None:
    p = argparse.ArgumentParser(description="网格自动监控")
    p.add_argument("--report", action="store_true", help="打印历史汇总")
    args = p.parse_args()
    if args.report:
        _report()
        return
    snap = asyncio.run(_snapshot())
    _record(snap)
    status = "存活" if snap["alive"] else "⚠️未存活"
    print(f"[{status}] 权益${snap['equity']:.2f} PnL${snap['pnl_since_start']:+.2f} "
          f"库存{snap['inv_btc']:+.5f}BTC 模式{snap['mode']} ADX{snap['adx']}")


if __name__ == "__main__":
    main()
