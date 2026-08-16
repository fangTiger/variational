"""归因存储层测试。重点是幂等：同一份 JSONL 导入两次不得产生重复行。"""

from __future__ import annotations

import json

from grid.attribution.store import connect, import_fills, init_schema


def _write_fills(path, records):
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _fill(fill_id, ts=1.0, level=100, side="BUY", price=64000.0, qty=0.0026):
    return {
        "fill_id": fill_id,
        "ts": ts,
        "level": level,
        "side": side,
        "price": price,
        "qty": qty,
        "engine_run_id": "run-1",
    }


def test_import_inserts_rows(tmp_path):
    src = tmp_path / "fills.jsonl"
    _write_fills(src, [_fill("a"), _fill("b")])
    conn = connect(tmp_path / "grid.db")
    init_schema(conn)

    assert import_fills(conn, src) == 2
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_import_is_idempotent(tmp_path):
    """重复导入不得产生重复行——否则闭环利润会被重复计算。"""
    src = tmp_path / "fills.jsonl"
    _write_fills(src, [_fill("a"), _fill("b")])
    conn = connect(tmp_path / "grid.db")
    init_schema(conn)

    import_fills(conn, src)
    assert import_fills(conn, src) == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_import_skips_corrupt_lines(tmp_path):
    """单行损坏不能让整次导入失败。"""
    src = tmp_path / "fills.jsonl"
    src.write_text(
        json.dumps(_fill("a")) + "\n{坏行\n" + json.dumps(_fill("b")) + "\n",
        encoding="utf-8",
    )
    conn = connect(tmp_path / "grid.db")
    init_schema(conn)

    assert import_fills(conn, src) == 2


def test_missing_source_returns_zero(tmp_path):
    conn = connect(tmp_path / "grid.db")
    init_schema(conn)
    assert import_fills(conn, tmp_path / "nope.jsonl") == 0


def test_schema_is_reentrant(tmp_path):
    conn = connect(tmp_path / "grid.db")
    init_schema(conn)
    init_schema(conn)


def test_save_loops_is_idempotent(tmp_path):
    from grid.attribution.store import save_loops

    conn = connect(tmp_path / "grid.db")
    init_schema(conn)
    loops = [
        {
            "loop_id": "1+2",
            "ts": 1.0,
            "level": 100,
            "buy_price": 100.0,
            "sell_price": 101.0,
            "qty": 1.0,
            "gross_pnl": 1.0,
        }
    ]
    assert save_loops(conn, loops) == 1
    assert save_loops(conn, loops) == 0


def test_load_fills_returns_time_order(tmp_path):
    from grid.attribution.store import load_fills

    src = tmp_path / "fills.jsonl"
    _write_fills(src, [_fill("b", ts=2.0), _fill("a", ts=1.0)])
    conn = connect(tmp_path / "grid.db")
    init_schema(conn)
    import_fills(conn, src)

    assert [fill["fill_id"] for fill in load_fills(conn)] == ["a", "b"]


def test_import_equity_snapshots_skips_invalid(tmp_path):
    """权益为 0 的坏记录（接口超时的产物）必须剔除，否则回撤统计失真。"""
    from grid.attribution.store import import_equity_snapshots

    src = tmp_path / "grid_monitor.jsonl"
    src.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "ts": 1.0,
                    "equity": 990.0,
                    "inv_usd": -100.0,
                    "price": 63000.0,
                    "mode": "neutral",
                },
                {
                    "ts": 2.0,
                    "equity": 0.0,
                    "inv_usd": 0.0,
                    "price": 63000.0,
                    "mode": "neutral",
                },
                {
                    "ts": 3.0,
                    "equity": 991.0,
                    "inv_usd": 100.0,
                    "price": 63100.0,
                    "mode": "neutral",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    conn = connect(tmp_path / "grid.db")
    init_schema(conn)

    assert import_equity_snapshots(conn, src) == 2


def test_import_funding_is_idempotent(tmp_path):
    from grid.attribution.store import import_funding

    conn = connect(tmp_path / "grid.db")
    init_schema(conn)
    rows = [{"funding_id": "f1", "ts": 1.0, "fee": -0.0112}]
    assert import_funding(conn, rows) == 1
    assert import_funding(conn, rows) == 0
