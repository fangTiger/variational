"""证伪锚点的判据测试。不读真实数据。"""
from __future__ import annotations

from tools.verify_anchor import (
    LIVE_HOURS,
    LIVE_LOOPS,
    LOW_EFFICIENCY_RATIO,
    MIN_HOURS_FOR_VERDICT,
)


def test_live_baseline_matches_engine_counter() -> None:
    """实盘基准锁值：67 闭环 / 17.01 小时的纯净窗口。

    这两个数来自引擎计数器配置未变更的那一段（unit=166 期间），
    改动它们会让证伪判据失去锚点。
    """
    assert LIVE_LOOPS == 67
    assert LIVE_HOURS == 17.01
    assert abs(LIVE_LOOPS / LIVE_HOURS - 3.94) < 0.01


def test_verdict_thresholds() -> None:
    """判定阈值锁值——放宽它们等于削弱这道证伪防线。"""
    assert MIN_HOURS_FOR_VERDICT == 24.0
    assert LOW_EFFICIENCY_RATIO == 0.2
