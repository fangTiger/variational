"""归因 provider 测试。重点是缓存与异常降级——它是唯一会打网络的 provider。"""

from __future__ import annotations

import panel.providers.attribution as attribution


_RESULT = {
    "loops_count": 25,
    "grid_pnl": 7.3776,
    "directional_pnl": -3.1866,
    "residual": -1.0215,
    "has_gap": False,
    "grid_annualised_pct": 412.13,
    "days": 0.657,
    "decided": False,
    "reason": "观察期未满（0.7/28 天）",
}


def test_result_renders_metrics(monkeypatch):
    monkeypatch.setattr(attribution, "_run", lambda: dict(_RESULT))
    attribution.reset_cache()
    s = attribution.collect()
    labels = [m.label for m in s.metrics]
    assert "闭环利润" in labels
    assert "残差" in labels
    assert s.alive is None      # 归因没有进程概念
    assert s.equity is None     # 不计入总权益


def test_gap_marks_residual_bad(monkeypatch):
    monkeypatch.setattr(attribution, "_run", lambda: {**_RESULT, "has_gap": True})
    attribution.reset_cache()
    s = attribution.collect()
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["残差"] == "bad"


def test_exception_degrades_to_error_card(monkeypatch):
    """归因算失败不能让整页白屏。"""
    def boom():
        raise RuntimeError("资金费接口超时")

    monkeypatch.setattr(attribution, "_run", boom)
    attribution.reset_cache()
    s = attribution.collect()
    assert s.error is not None
    assert "超时" in s.error


def test_second_call_uses_cache(monkeypatch):
    calls = []

    def counting():
        calls.append(1)
        return dict(_RESULT)

    monkeypatch.setattr(attribution, "_run", counting)
    attribution.reset_cache()
    attribution.collect()
    attribution.collect()
    assert len(calls) == 1, "5 分钟内第二次调用应命中缓存"


def test_cache_expires(monkeypatch):
    calls = []

    def counting():
        calls.append(1)
        return dict(_RESULT)

    monkeypatch.setattr(attribution, "_run", counting)
    attribution.reset_cache()
    attribution.collect(now=1000.0)
    attribution.collect(now=1000.0 + attribution.CACHE_TTL_S + 1)
    assert len(calls) == 2
