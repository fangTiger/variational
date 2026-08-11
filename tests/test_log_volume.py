"""日志降噪行为测试。

2026-08-11：每轮一行「本轮完成」占了全天日志 90%（21095/23354 行、约 30MB/天）。
降噪的前提是**不丢排查所需的信息**，所以这里重点验证两件事：
慢轮必须逐条保留，trend 状态变化必须立即可见。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from grid.grid_engine import (
    _ROUND_SUMMARY_EVERY,
    _SLOW_ROUND_LOG_S,
    _TREND_LOG_MIN_INTERVAL_S,
    GridEngine,
)


def _fake_engine():
    return SimpleNamespace(
        _round_stats={"n": 0, "sum": 0.0, "max": 0.0},
        _last_trend_log=None,
    )


def test_slow_round_logged_individually(caplog):
    """慢轮是排查 TLS 握手超时的关键线索，必须逐条保留。"""
    eng = _fake_engine()
    with caplog.at_level(logging.INFO, logger="grid_engine"):
        GridEngine._log_round_complete(eng, _SLOW_ROUND_LOG_S + 0.1)
    assert "慢" in caplog.text
    assert eng._round_stats["n"] == 0  # 慢轮不进汇总统计


def test_normal_rounds_are_summarised(caplog):
    eng = _fake_engine()
    with caplog.at_level(logging.INFO, logger="grid_engine"):
        for _ in range(_ROUND_SUMMARY_EVERY - 1):
            GridEngine._log_round_complete(eng, 1.0)
        assert caplog.text == ""  # 未达汇总阈值前一行不打
        GridEngine._log_round_complete(eng, 3.0)

    assert f"近 {_ROUND_SUMMARY_EVERY} 轮" in caplog.text
    assert "最慢 3.00 秒" in caplog.text
    assert eng._round_stats["n"] == 0  # 汇总后重新计数


def test_summary_reduces_volume_by_orders_of_magnitude():
    """1000 个正常轮次应只产生个位数日志行。"""
    eng = _fake_engine()
    lines = 0

    class _Counter(logging.Handler):
        def emit(self, record):
            nonlocal lines
            lines += 1

    logger = logging.getLogger("grid_engine")
    handler = _Counter()
    logger.addHandler(handler)
    try:
        for _ in range(1000):
            GridEngine._log_round_complete(eng, 1.5)
    finally:
        logger.removeHandler(handler)

    assert lines == 1000 // _ROUND_SUMMARY_EVERY


def test_trend_state_change_logs_immediately():
    """状态一变必须立即可见，否则又会漏掉 OFF 停摆。"""
    eng = _fake_engine()
    now = 1000.0
    assert GridEngine._should_log_trend(eng, ("neutral", False, None), now)
    # 同一状态、间隔很短 → 抑制
    assert not GridEngine._should_log_trend(eng, ("neutral", False, None), now + 1)
    # 状态变了 → 立刻放行，不受间隔限制
    assert GridEngine._should_log_trend(eng, ("off", False, "BOTH"), now + 2)


def test_trend_same_state_throttled_then_released():
    eng = _fake_engine()
    now = 1000.0
    sig = ("neutral", False, None)
    assert GridEngine._should_log_trend(eng, sig, now)
    assert not GridEngine._should_log_trend(eng, sig, now + _TREND_LOG_MIN_INTERVAL_S - 1)
    # 超过最小间隔后重新放行，保证稳态也有心跳行
    assert GridEngine._should_log_trend(eng, sig, now + _TREND_LOG_MIN_INTERVAL_S + 1)
