"""归因数据的 SQLite 存储。

选 SQLite 而非 PostgreSQL：4 周数据量约 2000 行、单机单写者。
无服务、文件即数据库、可随时从 JSONL 全量重建。
启用 WAL，让面板和分析脚本能在导入期间并发读。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from infra.logger import get_logger

logger = get_logger("attribution")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    fill_id       TEXT PRIMARY KEY,
    ts            REAL NOT NULL,
    level         INTEGER NOT NULL,
    side          TEXT NOT NULL,
    price         REAL NOT NULL,
    qty           REAL NOT NULL,
    engine_run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_level_ts ON fills(level, ts);

CREATE TABLE IF NOT EXISTS closed_loops (
    loop_id     TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    level       INTEGER NOT NULL,
    buy_price   REAL NOT NULL,
    sell_price  REAL NOT NULL,
    qty         REAL NOT NULL,
    gross_pnl   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loops_ts ON closed_loops(ts);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts          REAL PRIMARY KEY,
    equity      REAL NOT NULL,
    inv_usd     REAL,
    price       REAL,
    mode        TEXT,
    cash_flow   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS funding (
    funding_id  TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    fee         REAL NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _iter_json_lines(path: Path):
    """逐行读 JSONL，跳过损坏行——单行坏了不该让整次导入失败。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:  # noqa: BLE001
            logger.warning("跳过损坏行：%.80s", line)


def import_fills(conn: sqlite3.Connection, src: Path) -> int:
    """导入成交明细，返回新增行数。fill_id 为主键，重复导入自动忽略。"""
    inserted = 0
    for record in _iter_json_lines(src):
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO fills"
                " (fill_id, ts, level, side, price, qty, engine_run_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record["fill_id"]),
                    float(record["ts"]),
                    int(record["level"]),
                    str(record["side"]),
                    float(record["price"]),
                    float(record["qty"]),
                    record.get("engine_run_id"),
                ),
            )
            inserted += cursor.rowcount
        except Exception as exc:  # noqa: BLE001
            logger.warning("跳过无法入库的成交记录：%s", exc)
    conn.commit()
    return inserted


def load_fills(conn: sqlite3.Connection) -> list[dict]:
    """按时间升序取出全部成交，供离线配对使用。"""
    rows = conn.execute(
        "SELECT fill_id, ts, level, side, price, qty, engine_run_id"
        " FROM fills ORDER BY ts, fill_id"
    ).fetchall()
    return [
        {
            "fill_id": row[0],
            "ts": row[1],
            "level": row[2],
            "side": row[3],
            "price": row[4],
            "qty": row[5],
            "engine_run_id": row[6],
        }
        for row in rows
    ]


def save_loops(conn: sqlite3.Connection, loops: list[dict]) -> int:
    """写入闭环，返回新增行数。loop_id 确定性生成，故可反复重算。"""
    inserted = 0
    for loop in loops:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO closed_loops"
            " (loop_id, ts, level, buy_price, sell_price, qty, gross_pnl)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                loop["loop_id"],
                loop["ts"],
                loop["level"],
                loop["buy_price"],
                loop["sell_price"],
                loop["qty"],
                loop["gross_pnl"],
            ),
        )
        inserted += cursor.rowcount
    conn.commit()
    return inserted


def import_equity_snapshots(conn: sqlite3.Connection, src: Path) -> int:
    """导入权益快照（来自 data/grid_monitor.jsonl），返回新增行数。

    权益为 0 的记录是接口超时的产物，必须剔除：它们会把回撤统计
    从真实的 $27 虚增到 $972（2026-08-10 实测）。
    """
    inserted = 0
    for record in _iter_json_lines(src):
        equity = record.get("equity") or 0
        if float(equity) <= 0:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO equity_snapshots"
            " (ts, equity, inv_usd, price, mode) VALUES (?, ?, ?, ?, ?)",
            (
                float(record["ts"]),
                float(equity),
                record.get("inv_usd"),
                record.get("price"),
                record.get("mode"),
            ),
        )
        inserted += cursor.rowcount
    conn.commit()
    return inserted


def import_funding(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """导入资金费流水，返回新增行数。"""
    inserted = 0
    for record in rows:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO funding (funding_id, ts, fee) VALUES (?, ?, ?)",
            (
                str(record["funding_id"]),
                float(record["ts"]),
                float(record["fee"]),
            ),
        )
        inserted += cursor.rowcount
    conn.commit()
    return inserted
