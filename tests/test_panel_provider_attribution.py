"""归因 provider 测试。重点是缓存与异常降级——它是唯一会打网络的 provider。"""

from __future__ import annotations

import pytest

import panel.providers.attribution as attribution


_RESULT = {
    "loops_count": 25,
    "grid_pnl": 7.3776,
    "directional_pnl": -3.1866,
    "residual": -1.0215,
    "has_gap": False,
    "grid_annualised_pct": 412.13,
    "equity_change": 4.68,
    "funding_total": 0.0573,
    "max_drawdown_pct": 0.4536,
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


def _metric(status, label):
    return next(metric for metric in status.metrics if metric.label == label)


def test_detail_metrics_are_appended_in_order_and_formatted(monkeypatch):
    """四项归因详情保持约定顺序，短样本年化必须带警示。"""
    monkeypatch.setattr(
        attribution,
        "_run",
        lambda: {**_RESULT, "grid_annualised_pct": 403.2},
    )
    attribution.reset_cache()

    status = attribution.collect()
    details = status.metrics[5:]

    assert [metric.label for metric in details] == [
        "权益变化",
        "资金费",
        "闭环年化",
        "最大回撤",
    ]
    assert {metric.label: metric.value for metric in details} == {
        "权益变化": "$+4.68",
        "资金费": "$+0.06",
        "闭环年化": "403.2%（样本不足）",
        "最大回撤": "0.45%",
    }
    assert _metric(status, "资金费").tone == "normal"
    assert _metric(status, "闭环年化").tone == "normal"


@pytest.mark.parametrize(
    ("equity_change", "expected_tone"),
    [(0.01, "good"), (-0.01, "bad"), (0.0, "normal")],
)
def test_equity_change_tone(monkeypatch, equity_change, expected_tone):
    monkeypatch.setattr(
        attribution,
        "_run",
        lambda: {**_RESULT, "equity_change": equity_change},
    )
    attribution.reset_cache()
    assert _metric(attribution.collect(), "权益变化").tone == expected_tone


@pytest.mark.parametrize(
    ("days", "expected_suffix"),
    [(6.999, "（样本不足）"), (7.0, "")],
)
def test_annualised_sample_warning_boundary(monkeypatch, days, expected_suffix):
    monkeypatch.setattr(
        attribution,
        "_run",
        lambda: {**_RESULT, "days": days},
    )
    attribution.reset_cache()
    value = _metric(attribution.collect(), "闭环年化").value
    assert value.endswith(expected_suffix) if expected_suffix else "样本不足" not in value


@pytest.mark.parametrize(
    ("drawdown", "expected_tone"),
    [(5.0, "normal"), (5.001, "warn")],
)
def test_max_drawdown_tone_boundary(monkeypatch, drawdown, expected_tone):
    monkeypatch.setattr(
        attribution,
        "_run",
        lambda: {**_RESULT, "max_drawdown_pct": drawdown},
    )
    attribution.reset_cache()
    assert _metric(attribution.collect(), "最大回撤").tone == expected_tone


def test_missing_detail_fields_degrade_independently(monkeypatch):
    """旧归因结果缺字段时四项都显示破折号，卡片仍可渲染。"""
    result = dict(_RESULT)
    for key in (
        "equity_change",
        "funding_total",
        "grid_annualised_pct",
        "max_drawdown_pct",
    ):
        result.pop(key)
    monkeypatch.setattr(attribution, "_run", lambda: result)
    attribution.reset_cache()

    status = attribution.collect()

    assert status.error is None
    assert {metric.label: metric.value for metric in status.metrics[5:]} == {
        "权益变化": "—",
        "资金费": "—",
        "闭环年化": "—",
        "最大回撤": "—",
    }
