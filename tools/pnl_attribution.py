"""归因主流程：导入 → 配对 → 计算 → 落盘。launchd 每小时调用。

全部只读交易所、只写本地 SQLite 与 JSON 报告，不碰账户、不下单。
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from grid.attribution.pairing import pair_fills  # noqa: E402
from grid.attribution.report import compute_attribution, verdict  # noqa: E402
from grid.attribution.store import (  # noqa: E402
    connect,
    import_equity_snapshots,
    import_fills,
    import_funding,
    init_schema,
    load_fills,
    save_loops,
)
from tools.fetch_funding import fetch_funding  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "data"
_DB = _DATA / "grid.db"
_RESULT = _DATA / "attribution.json"
_START = _DATA / "attribution_start.json"
_MARKET = "BTC-USD"
_FUNDING_LIMIT = 500
_RUN_ID_RE = re.compile(r"^run-(\d+(?:\.\d+)?)$")


def _seconds(timestamp: object) -> float:
    """把交易所可能返回的毫秒时间戳归一化成秒。"""
    value = float(timestamp)
    return value / 1000.0 if value > 100_000_000_000 else value


async def _fetch_grid_account_snapshot() -> dict | None:
    """从网格账户读取同一份余额快照中的权益与未实现盈亏。"""
    client = None
    try:
        client = ExtendedClient.from_env(prefix="X10_GRID")
        balance = await client.get_balance()
        equity = float(balance.equity)
        unrealised_pnl = float(balance.unrealised_pnl)
        updated_time = getattr(balance, "updated_time", None)
        timestamp = _seconds(updated_time) if updated_time else time.time()
        if not all(math.isfinite(value) for value in (timestamp, equity, unrealised_pnl)):
            return None
        if equity <= 0:
            return None
        return {
            "ts": timestamp,
            "equity": equity,
            "unrealised_pnl": unrealised_pnl,
        }
    except Exception:  # noqa: BLE001  旁路读取失败由报告显式标记，不影响实盘
        return None
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001  关闭失败不覆盖主要读取结果
                pass


def fetch_grid_account_snapshot() -> dict | None:
    """同步读取网格账户余额；仅供归因旁路调用。"""
    return asyncio.run(_fetch_grid_account_snapshot())


def _observation_start_ts(fills: list[dict], start_path: Path) -> float | None:
    """确定观察起点：显式起点优先，其次最早引擎启动时间。"""
    try:
        payload = json.loads(start_path.read_text(encoding="utf-8"))
        explicit = float(payload["start_ts"])
        if math.isfinite(explicit) and explicit > 0:
            return explicit
    except Exception:  # noqa: BLE001  缺失或损坏时按成交流回退
        pass

    run_starts = []
    for fill in fills:
        match = _RUN_ID_RE.fullmatch(str(fill.get("engine_run_id") or ""))
        if match is not None:
            run_starts.append(float(match.group(1)))
    if run_starts:
        return min(run_starts)

    timestamps = [float(fill["ts"]) for fill in fills if fill.get("ts") is not None]
    return min(timestamps) if timestamps else None


def _write_result(result: dict) -> None:
    """原子替换报告，避免告警器读到只写了一半的 JSON。"""
    _RESULT.parent.mkdir(parents=True, exist_ok=True)
    temporary = _RESULT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(_RESULT)


def _undecided(reason: str, *, loops_count: int) -> dict:
    """生成不会伪装成零残差的不可判定报告。"""
    return {
        "decided": False,
        "should_stop": False,
        "identity_checked": False,
        "residual": None,
        "has_gap": False,
        "loops_count": loops_count,
        "reason": reason,
    }


def run() -> dict:
    """执行一次只读归因并返回与落盘内容一致的结果。"""
    load_dotenv(str(_ROOT / ".env"))
    connection = connect(_DB)
    try:
        init_schema(connection)
        import_fills(connection, _DATA / "fills.jsonl")
        import_equity_snapshots(connection, _DATA / "grid_monitor.jsonl")
        import_funding(
            connection,
            fetch_funding(market=_MARKET, limit=_FUNDING_LIMIT),
        )

        fills = load_fills(connection)
        all_loops = pair_fills(fills)
        save_loops(connection, all_loops)

        account = fetch_grid_account_snapshot()
        if account is None:
            result = _undecided(
                "网格账户快照不足，无法执行恒等式校验",
                loops_count=len(all_loops),
            )
            _write_result(result)
            return result

        requested_start = _observation_start_ts(fills, _START)
        if requested_start is None:
            first = connection.execute(
                "SELECT MIN(ts) FROM equity_snapshots"
            ).fetchone()[0]
            requested_start = float(first) if first is not None else None
        if requested_start is None:
            result = _undecided("权益快照不足，无法归因", loops_count=len(all_loops))
            _write_result(result)
            return result

        end_ts = float(account["ts"])
        snapshots = connection.execute(
            "SELECT ts, equity FROM equity_snapshots"
            " WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (requested_start, end_ts),
        ).fetchall()
        if not snapshots:
            result = _undecided("权益快照不足，无法归因", loops_count=len(all_loops))
            _write_result(result)
            return result

        start_ts = float(snapshots[0][0])
        equity_start = float(snapshots[0][1])
        equity_end = float(account["equity"])
        window_loops = connection.execute(
            "SELECT gross_pnl FROM closed_loops"
            " WHERE ts >= ? AND ts <= ? ORDER BY ts, loop_id",
            (start_ts, end_ts),
        ).fetchall()
        funding_total = float(
            connection.execute(
                "SELECT COALESCE(SUM(fee), 0) FROM funding"
                " WHERE ts >= ? AND ts <= ?",
                (start_ts, end_ts),
            ).fetchone()[0]
        )
        cash_flow = float(
            connection.execute(
                "SELECT COALESCE(SUM(cash_flow), 0) FROM equity_snapshots"
                " WHERE ts > ? AND ts <= ?",
                (start_ts, end_ts),
            ).fetchone()[0]
        )

        # 现有历史快照没有保存起点未实现盈亏。首轮只能明确按 0 假设，
        # 把由此产生的偏差留在 residual 中，不能为了闭账去改配对或阈值。
        unrealised_start = 0.0
        unrealised_end = float(account["unrealised_pnl"])
        unrealised_change = unrealised_end - unrealised_start
        days = (end_ts - start_ts) / 86400.0
        attribution = compute_attribution(
            equity_start=equity_start,
            equity_end=equity_end,
            loops=[{"gross_pnl": float(row[0])} for row in window_loops],
            funding_total=funding_total,
            cash_flow=cash_flow,
            unrealised_change=unrealised_change,
            days=days,
        )

        equities = [float(row[1]) for row in snapshots] + [equity_end]
        peak = equities[0]
        max_drawdown_pct = 0.0
        for equity in equities:
            peak = max(peak, equity)
            max_drawdown_pct = max(
                max_drawdown_pct,
                (peak - equity) / peak * 100.0,
            )
        equity_annualised_pct = (
            attribution["equity_change"]
            / max(days, 1e-9)
            * 365.0
            / equity_start
            * 100.0
        )
        decision = verdict(
            grid_annualised_pct=attribution["grid_annualised_pct"],
            equity_annualised_pct=equity_annualised_pct,
            max_drawdown_pct=max_drawdown_pct,
            has_gap=attribution["has_gap"],
            days=days,
        )
        result = {
            **attribution,
            "cash_flow": cash_flow,
            "equity_start": equity_start,
            "equity_end": equity_end,
            "equity_annualised_pct": equity_annualised_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "loops_count": len(window_loops),
            "observation_start_ts": start_ts,
            "observation_end_ts": end_ts,
            "unrealised_start": unrealised_start,
            "unrealised_end": unrealised_end,
            "unrealised_change": unrealised_change,
            "unrealised_start_assumed_zero": True,
            "identity_checked": True,
            **decision,
        }
        _write_result(result)
        return result
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="网格收益归因")
    parser.add_argument("--report", action="store_true", help="打印结果（默认已打印）")
    parser.parse_args()
    result = run()
    residual = result.get("residual")
    residual_text = "不可校验" if residual is None else f"${residual:+.2f}"
    print(
        f"闭环 {result.get('loops_count', 0)} 个 | "
        f"网格 ${result.get('grid_pnl', 0):+.2f} | "
        f"方向性 ${result.get('directional_pnl', 0):+.2f} | "
        f"残差 {residual_text} | "
        f"闭环年化 {result.get('grid_annualised_pct', 0):.1f}% | "
        f"{result.get('reason', '')}"
    )


if __name__ == "__main__":
    main()
