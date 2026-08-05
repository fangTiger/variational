"""被拒档位的指数退避冷却测试。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from adapters.base import Side
from grid import grid_engine
from grid.grid_engine import GridConfig, GridEngine


class RejectExt:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = iter(statuses)
        self.requests: list[int] = []

    async def place_limit_order(self, market, side, qty, price, **kwargs):
        self.requests.append(1)
        status = next(self.statuses)
        return SimpleNamespace(
            data=SimpleNamespace(
                id=f"order-{len(self.requests)}",
                status=status,
                status_reason="模拟 post-only 拒单",
            )
        )


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(grid_engine.time, "monotonic", lambda: now[0])
    return now


def _engine(ext: RejectExt) -> GridEngine:
    return GridEngine(
        ext,
        GridConfig(dry_run=False, unit_usd=20.0, spacing_pct=0.02),
    )


def _place(eng: GridEngine, level: int = 558, *, retrying: bool = False) -> None:
    asyncio.run(
        eng._place(level, Side.BUY, 0.0, why="补买格", retrying=retrying)
    )


def test_rejected_level_enters_cooldown_and_skips_request(clock) -> None:
    ext = RejectExt(["REJECTED"])
    eng = _engine(ext)

    _place(eng)
    eng._retry.pop(558)
    _place(eng)

    assert eng._reject_cooldown[558] == {"until": 130.0, "count": 1}
    assert len(ext.requests) == 1
    assert eng._counters["reject_cooldown_skipped"] == 1


def test_level_can_be_placed_after_cooldown_expires(clock) -> None:
    ext = RejectExt(["REJECTED", "OPEN"])
    eng = _engine(ext)

    _place(eng)
    eng._retry.pop(558)
    clock[0] = 129.0
    _place(eng)
    assert len(ext.requests) == 1

    clock[0] = 130.0
    _place(eng)

    assert len(ext.requests) == 2
    assert eng._orders[558]["id"] == "order-2"


def test_consecutive_rejections_back_off_to_300_seconds(clock) -> None:
    ext = RejectExt(["REJECTED"] * 6)
    eng = _engine(ext)

    for count, delay in enumerate((30, 60, 120, 240, 300, 300), start=1):
        _place(eng, retrying=True)
        cooldown = eng._reject_cooldown[558]
        assert cooldown == {"until": clock[0] + delay, "count": count}
        clock[0] = cooldown["until"]


def test_success_clears_cooldown_and_resets_count(clock) -> None:
    ext = RejectExt(["REJECTED", "OPEN", "REJECTED"])
    eng = _engine(ext)

    _place(eng, retrying=True)
    clock[0] = 130.0
    _place(eng, retrying=True)
    assert 558 not in eng._reject_cooldown

    eng._orders.pop(558)
    _place(eng, retrying=True)
    assert eng._reject_cooldown[558]["count"] == 1


def test_cooldown_only_blocks_rejected_level(clock) -> None:
    ext = RejectExt(["REJECTED", "OPEN"])
    eng = _engine(ext)

    _place(eng, level=558)
    eng._retry.pop(558)
    _place(eng, level=558)
    _place(eng, level=559)

    assert len(ext.requests) == 2
    assert eng._orders[559]["id"] == "order-2"


def test_retrying_cannot_bypass_reject_cooldown(clock) -> None:
    ext = RejectExt(["REJECTED"])
    eng = _engine(ext)

    _place(eng)
    assert 558 in eng._retry
    _place(eng, retrying=True)

    assert len(ext.requests) == 1
    assert eng._counters["reject_cooldown_skipped"] == 1
