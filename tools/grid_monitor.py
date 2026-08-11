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
from grid.grid_state import load_state  # noqa: E402
from grid.regime import adx, decide_mode, donchian_prev  # noqa: E402
from grid.risk import dist_to_liq_pct  # noqa: E402

_FILE = Path(__file__).resolve().parent.parent / "data" / "grid_monitor.jsonl"
_BASELINE = Path(__file__).resolve().parent.parent / "data" / "grid_baseline.json"
_STATE = Path(__file__).resolve().parent.parent / "data" / "grid_state.json"
_LIVE = Path(__file__).resolve().parent.parent / "data" / "grid_live.json"
MARKET = "BTC-USD"


class SnapshotInvalid(RuntimeError):
    """快照读数不可信（接口超时/返回空），不应写入历史。"""


def _process_alive() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "tools.run_grid"], capture_output=True, text=True)
        return bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _read_grid_state(path: str | Path = _STATE) -> dict:
    """读取引擎持久化状态；缺失或损坏时返回空监控字段。"""
    state = load_state(path)
    if state is None:
        return {
            "frozen": None,
            "blocked_side": None,
            "halted": None,
            "band_low": None,
            "band_high": None,
        }
    return {
        "frozen": state.frozen,
        "blocked_side": state.blocked_side,
        "halted": state.halted,
        "band_low": state.band_low,
        "band_high": state.band_high,
    }


def _read_engine_live(path: str | Path = _LIVE, max_age_s: float = 300.0) -> dict | None:
    """读引擎自己写的 live 快照；缺失/损坏/过期返回 None。

    引擎是 mode 与封锁状态的唯一真相源。本模块曾用硬编码 adx_off=999 自行
    重算 mode，与 launchd 实参（一度是 35/28）不同步，导致引擎已 OFF 停摆
    19.5 小时而监控始终报 neutral（2026-08-11）。故改为优先采信引擎快照。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  损坏当作没有
        return None
    ts = d.get("ts")
    if not isinstance(ts, (int, float)) or time.time() - ts > max_age_s:
        return None  # 引擎卡死/停了，快照不可信
    return d


def _liquidation_distance_pct(
    liquidation_info,
    signed_size: float,
) -> float | None:
    """计算距强平价比例；空仓或交易所未返回数据时记为 None。"""
    if liquidation_info is None or signed_size == 0:
        return None
    mark, liq = liquidation_info
    return dist_to_liq_pct(float(mark), float(liq), signed_size)


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
        raw_equity = getattr(bal, "equity", None)
        # 接口超时/返回空时 equity 会是 None，旧代码把它当 0 记进历史，
        # 会污染回撤统计并伪造"爆仓"假象；这里直接判为无效快照。
        if raw_equity is None or float(raw_equity) <= 0:
            raise SnapshotInvalid(f"权益读数无效：{raw_equity!r}")
        equity = float(raw_equity)
        pos = await ext.get_position(MARKET)
        r = await ext._client.info.get_candles_history(
            market_name=MARKET, candle_type="trades", interval="PT1H", limit=200)
        candles = sorted(r.data, key=lambda k: int(k.timestamp))
        if not candles:
            raise SnapshotInvalid("K 线返回为空，无法计算现价与 regime")
        highs = [float(k.high) for k in candles]
        lows = [float(k.low) for k in candles]
        closes = [float(k.close) for k in candles]
        a = adx(highs, lows, closes)
        up, lo = donchian_prev(highs, lows, 96)
        price = closes[-1]
        inv = float(pos.signed_size)
        liquidation_info = await ext.get_liquidation_info(MARKET)
        liquidation_distance = _liquidation_distance_pct(liquidation_info, inv)
    finally:
        await ext.close()

    # 基线权益（首次记录）
    if _BASELINE.exists():
        base = json.loads(_BASELINE.read_text())["equity"]
    else:
        base = equity
        _BASELINE.parent.mkdir(exist_ok=True)
        _BASELINE.write_text(json.dumps({"equity": equity, "ts": time.time()}))

    grid_state = _read_grid_state()
    # mode/封锁状态一律以引擎快照为准；引擎停了或快照过期才回退到本地重算，
    # 并用 mode_source 标出来，避免回退值被当成引擎真实状态。
    live = _read_engine_live()
    if live is not None:
        mode_value = live.get("mode")
        mode_source = "engine"
        grid_state["blocked_side"] = live.get(
            "effective_blocked_side", grid_state.get("blocked_side")
        )
    else:
        mode_value = decide_mode(
            adx_val=a[-1],
            close=price,
            donchian_up=up[-1],
            donchian_lo=lo[-1],
            adx_off=999.0,
        ).value
        mode_source = "fallback"

    return {
        "ts": time.time(),
        "alive": _process_alive(),
        "equity": equity,
        "pnl_since_start": equity - base,
        "inv_btc": inv,
        "inv_usd": inv * price,
        "price": price,
        "adx": round(a[-1], 1) if a[-1] else None,
        "mode": mode_value,
        "mode_source": mode_source,
        **grid_state,
        "dist_to_liq_pct": liquidation_distance,
    }


def _record(snap: dict) -> None:
    _FILE.parent.mkdir(exist_ok=True)
    with _FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")


def _load() -> list[dict]:
    """读取历史快照，并剔除权益为 0 的坏记录（修复前遗留，会虚增回撤）。"""
    if not _FILE.exists():
        return []
    snaps = [json.loads(x) for x in _FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [s for s in snaps if (s.get("equity") or 0) > 0]


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
    band_low = cur.get("band_low")
    band_high = cur.get("band_high")
    band = (
        f"[{band_low:.0f}, {band_high:.0f}]"
        if band_low is not None and band_high is not None
        else "None"
    )
    liquidation_distance = cur.get("dist_to_liq_pct")
    liquidation_text = (
        f"{liquidation_distance:.1%}" if liquidation_distance is not None else "None"
    )
    print(
        f"  趋势状态 frozen={cur.get('frozen')} "
        f"blocked_side={cur.get('blocked_side')} halted={cur.get('halted')} "
        f"band={band} 距强平 {liquidation_text}"
    )
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
    try:
        snap = asyncio.run(_snapshot())
    except SnapshotInvalid as exc:
        # 宁可漏记一次，也不能把假读数写进历史；下个周期 launchd 会自动重试
        print(f"[跳过] 本次快照不可信，未记录：{exc}", flush=True)
        raise SystemExit(1)
    _record(snap)
    status = "存活" if snap["alive"] else "⚠️未存活"
    blocked = snap.get("blocked_side")
    # 封锁状态必须显式打出来：OFF 停摆时 frozen=False，只看模式容易漏
    blocked_text = f" ⛔封锁{blocked}" if blocked else ""
    stale_text = "" if snap.get("mode_source") == "engine" else "(引擎快照过期，本地估算)"
    print(f"[{status}] 权益${snap['equity']:.2f} PnL${snap['pnl_since_start']:+.2f} "
          f"库存{snap['inv_btc']:+.5f}BTC 模式{snap['mode']}{stale_text}"
          f"{blocked_text} ADX{snap['adx']}")


if __name__ == "__main__":
    main()
