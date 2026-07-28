"""硬止损纯判定测试：距强平百分比 + 多空方向。"""
from __future__ import annotations

from grid.risk import dist_to_liq_pct, hard_stop_triggered


def test_dist_long() -> None:
    # 多头：liq 在下方，(mark-liq)/mark
    assert abs(dist_to_liq_pct(mark=100.0, liq=90.0, signed_size=1.0) - 0.10) < 1e-9


def test_dist_short() -> None:
    # 空头：liq 在上方，(liq-mark)/mark
    assert abs(dist_to_liq_pct(mark=100.0, liq=110.0, signed_size=-1.0) - 0.10) < 1e-9


def test_trigger_when_close() -> None:
    # 距强平 8% < 阈值 12% → 触发
    assert hard_stop_triggered(mark=100.0, liq=92.0, signed_size=1.0, stop_dist=0.12) is True


def test_no_trigger_when_far() -> None:
    # 距强平 20% > 12% → 不触发
    assert hard_stop_triggered(mark=100.0, liq=80.0, signed_size=1.0, stop_dist=0.12) is False


def test_flat_never_triggers() -> None:
    assert hard_stop_triggered(mark=100.0, liq=0.0, signed_size=0.0, stop_dist=0.12) is False
