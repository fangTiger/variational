"""网格状态持久化测试：原子读写 + 缺失/损坏时的安全默认。"""
from __future__ import annotations

from grid.grid_state import GridState, load_state, save_state


def test_roundtrip(tmp_path) -> None:
    p = tmp_path / "grid_state.json"
    st = GridState(band_low=63000.0, band_high=68000.0, frozen=True,
                   blocked_side="BUY", halted=False)
    save_state(p, st)
    got = load_state(p)
    assert got == st


def test_missing_file_returns_none(tmp_path) -> None:
    assert load_state(tmp_path / "nope.json") is None


def test_corrupt_file_returns_none(tmp_path) -> None:
    p = tmp_path / "grid_state.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_state(p) is None


def test_atomic_write_no_partial(tmp_path) -> None:
    # 覆盖写不应留下半截文件：写两次，第二次能完整读回
    p = tmp_path / "grid_state.json"
    save_state(p, GridState(1.0, 2.0, False, None, False))
    save_state(p, GridState(3.0, 4.0, True, "SELL", True))
    got = load_state(p)
    assert got == GridState(3.0, 4.0, True, "SELL", True)
