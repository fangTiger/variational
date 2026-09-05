"""切换台账只读查询工具测试。"""

from __future__ import annotations

import json
from pathlib import Path


def _record(started_at: str, *, wear: str, passed: bool) -> dict[str, object]:
    """构造查询工具消费的稳定台账 schema。"""
    return {
        "schema_version": 1,
        "started_at": started_at,
        "status": "completed" if passed else "failed",
        "direction": {"from": "XAU_XAUT", "to": "XAUS_XAU"},
        "close_phase": {
            "legs": [
                {"market": "XAU", "execution_price": "3999"},
                {"market": "XAUT", "execution_price": "4001"},
            ]
        },
        "open_phase": {
            "legs": [
                {"market": "XAUS", "execution_price": "4001"},
                {"market": "XAU", "execution_price": "3999"},
            ]
        },
        "measured_wear_usd": wear,
        "total_duration_ms": 1234,
        "self_check": {"performed": True, "passed": passed},
    }


def test_show_history_prints_last_n_records_in_chinese(
    tmp_path: Path,
    capsys,
) -> None:
    """--last N 只打印最近 N 条，并包含方向、价格、磨损、耗时和自检。"""
    from tools import show_switch_history

    path = tmp_path / "switch_history.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in (
                _record("2026-09-05T22:00:00+00:00", wear="-0.10", passed=True),
                _record("2026-09-06T22:00:00+00:00", wear="-0.20", passed=True),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = show_switch_history.main(["--last", "1", "--path", str(path)])

    output = capsys.readouterr().out
    assert result == 0
    assert "切换时间" in output
    assert "方向" in output
    assert "XAU_XAUT→XAUS_XAU" in output
    assert "XAUS@4001" in output
    assert "实测磨损 -0.20 USDC" in output
    assert "耗时 1.234 秒" in output
    assert "自检通过" in output
    assert "09-05" not in output


def test_show_history_missing_or_corrupt_file_degrades(
    tmp_path: Path,
    capsys,
) -> None:
    """文件不存在或损坏时工具给中文提示并正常返回。"""
    from tools import show_switch_history

    missing = tmp_path / "missing.jsonl"
    assert show_switch_history.main(["--path", str(missing)]) == 0
    assert "尚无切换记录" in capsys.readouterr().out

    damaged = tmp_path / "damaged.jsonl"
    damaged.write_text("{损坏\n", encoding="utf-8")
    assert show_switch_history.main(["--path", str(damaged)]) == 0
    assert "台账不可用（文件损坏）" in capsys.readouterr().out
