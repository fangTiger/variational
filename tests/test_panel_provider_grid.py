"""网格 provider 测试。失败路径优先——文件缺失、损坏是常态，不能让面板白屏。"""

from __future__ import annotations

import json

import pytest

from panel.providers.grid import collect


def _write(tmp_path, live: dict | None, monitor: list[dict] | None):
    live_path = tmp_path / "grid_live.json"
    monitor_path = tmp_path / "grid_monitor.jsonl"
    if live is not None:
        live_path.write_text(json.dumps(live), encoding="utf-8")
    if monitor is not None:
        monitor_path.write_text(
            "\n".join(json.dumps(r) for r in monitor), encoding="utf-8"
        )
    return live_path, monitor_path


_LIVE = {
    "ts": 1786946078.3,
    "mark": 63422.9,
    "mode": "neutral",
    "inv_btc": -0.0254,
    "inv_usd": -1610.9,
    "band_low": 61669.4,
    "band_high": 65484.0,
    "frozen": False,
    "halted": False,
    "dist_to_liq_pct": 0.6038,
    "adx": 25.215,
    "replacement_placed": 58,
    "replacement_failed": 0,
    "fill_detected": 61,
    "max_abs_inv_usd": 2516.03,
    "cfg": {"unit": 300.0, "levels": 30, "max_inv": 3750.0, "spacing": 0.000986},
}
_MONITOR = [{"ts": 1786946078.3, "equity": 997.87, "pnl_since_start": 49.32}]
_PEAK = {"peak": 1003.417376}
_BASELINE = {"equity": 948.27, "ts": 1785945600.0}
_ATTRIBUTION = {"unrealised_end": -1.026024}


def _collect_details(
    tmp_path,
    *,
    live=None,
    monitor=None,
    peak=None,
    baseline=None,
    attribution=None,
):
    """写入各类快照并采集，默认使用完整健康样本。"""
    live = dict(_LIVE) if live is None else live
    monitor = list(_MONITOR) if monitor is None else monitor
    peak = dict(_PEAK) if peak is None else peak
    baseline = dict(_BASELINE) if baseline is None else baseline
    attribution = dict(_ATTRIBUTION) if attribution is None else attribution
    live_path, monitor_path = _write(tmp_path, live, monitor)
    peak_path = tmp_path / "equity_peak.json"
    baseline_path = tmp_path / "grid_baseline.json"
    attribution_path = tmp_path / "attribution.json"
    if isinstance(peak, str):
        peak_path.write_text(peak, encoding="utf-8")
    elif peak is not False:
        peak_path.write_text(json.dumps(peak), encoding="utf-8")
    if isinstance(baseline, str):
        baseline_path.write_text(baseline, encoding="utf-8")
    elif baseline is not False:
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    if isinstance(attribution, str):
        attribution_path.write_text(attribution, encoding="utf-8")
    elif attribution is not False:
        attribution_path.write_text(json.dumps(attribution), encoding="utf-8")
    return collect(
        live_path=live_path,
        monitor_path=monitor_path,
        equity_peak_path=peak_path,
        baseline_path=baseline_path,
        attribution_path=attribution_path,
    )


def _metrics(status):
    return {metric.label: metric for metric in status.metrics}


def test_missing_files_do_not_raise(tmp_path):
    """文件都不存在时返回一张「无数据」卡片，而不是抛异常。"""
    lp, mp = _write(tmp_path, None, None)
    s = collect(live_path=lp, monitor_path=mp)
    assert s.name
    assert s.error is not None


def test_corrupt_live_json_is_tolerated(tmp_path):
    lp, mp = _write(tmp_path, None, _MONITOR)
    lp.write_text("{ 这不是 json", encoding="utf-8")
    s = collect(live_path=lp, monitor_path=mp)
    assert s.error is not None


def test_healthy_snapshot_produces_metrics(tmp_path):
    lp, mp = _write(tmp_path, _LIVE, _MONITOR)
    s = collect(live_path=lp, monitor_path=mp)
    assert s.error is None
    assert s.equity == 997.87
    labels = [m.label for m in s.metrics]
    assert "权益" in labels
    assert "持仓" in labels
    assert "库存上限" in labels


def test_halted_is_marked_bad(tmp_path):
    """熔断是最要紧的状态，必须以 bad 色显示。"""
    lp, mp = _write(tmp_path, {**_LIVE, "halted": True}, _MONITOR)
    s = collect(live_path=lp, monitor_path=mp)
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["熔断"] == "bad"


def test_frozen_is_marked_warn(tmp_path):
    lp, mp = _write(tmp_path, {**_LIVE, "frozen": True}, _MONITOR)
    s = collect(live_path=lp, monitor_path=mp)
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["熔断"] == "warn"


def test_detail_metrics_are_appended_in_order_and_formatted(tmp_path):
    """总收益替换旧指标，当前持仓浮盈紧随其后，其余详情保持顺序。"""
    status = _collect_details(tmp_path)
    details = status.metrics[5:]

    assert [metric.label for metric in details] == [
        "总收益",
        "当前持仓浮盈",
        "距熔断线",
        "距强平",
        "band 区间",
        "价格位置",
        "ADX",
        "格距 / 每格",
        "下单成功",
        "历史最大库存",
    ]
    assert {metric.label: metric.value for metric in details} == {
        "总收益": "$+49.60（自 08-06）",
        "当前持仓浮盈": "$-1.03",
        "距熔断线": "$115 (11.5%)",
        "距强平": "60.4%",
        "band 区间": "$61,669 ~ $65,484",
        "价格位置": "46%",
        "ADX": "25.2",
        "格距 / 每格": "0.0986% / $300",
        "下单成功": "58 / 61",
        "历史最大库存": "$2,516",
    }
    assert _metrics(status)["band 区间"].tone == "normal"
    assert _metrics(status)["ADX"].tone == "normal"
    assert _metrics(status)["格距 / 每格"].tone == "normal"
    assert "自起始 PnL" not in _metrics(status)
    assert status.total_pnl == pytest.approx(49.60)


@pytest.mark.parametrize(
    ("pnl", "expected_value", "expected_tone"),
    [
        (0.01, "$+0.01（基线未知）", "good"),
        (-0.01, "$-0.01（基线未知）", "bad"),
        (0.0, "$+0.00（基线未知）", "normal"),
        (None, "—（基线未知）", "normal"),
    ],
)
def test_missing_baseline_falls_back_to_pnl_since_start(
    tmp_path, pnl, expected_value, expected_tone
):
    status = _collect_details(
        tmp_path,
        monitor=[{**_MONITOR[-1], "pnl_since_start": pnl}],
        baseline=False,
    )
    metric = _metrics(status)["总收益"]
    assert (metric.value, metric.tone) == (expected_value, expected_tone)
    assert status.total_pnl == pnl


def test_corrupt_baseline_falls_back_and_marks_date_unknown(tmp_path):
    status = _collect_details(tmp_path, baseline="{损坏的 json")
    metric = _metrics(status)["总收益"]
    assert metric.value == "$+49.32（基线未知）"
    assert status.total_pnl == 49.32


@pytest.mark.parametrize(
    ("unrealised", "expected_value", "expected_tone"),
    [
        (1.234, "$+1.23", "good"),
        (-1.234, "$-1.23", "bad"),
        (0.0, "$+0.00", "normal"),
    ],
)
def test_current_pnl_uses_attribution_unrealised_end(
    tmp_path, unrealised, expected_value, expected_tone
):
    status = _collect_details(
        tmp_path,
        attribution={"unrealised_end": unrealised},
    )
    metric = _metrics(status)["当前持仓浮盈"]
    assert (metric.value, metric.tone) == (expected_value, expected_tone)


def test_current_pnl_is_dash_when_attribution_file_is_missing(tmp_path):
    status = _collect_details(tmp_path, attribution=False)
    metric = _metrics(status)["当前持仓浮盈"]
    assert (metric.value, metric.tone) == ("—", "normal")


def test_position_includes_signed_usd_and_inventory_uses_absolute_usage(tmp_path):
    """持仓保留方向，库存上限只表达绝对占用比例。"""
    status = _collect_details(
        tmp_path,
        live={
            **_LIVE,
            "inv_btc": -0.03012,
            "inv_usd": -1912,
            "cfg": {**_LIVE["cfg"], "max_inv": 3750},
        },
    )

    # 浮盈并进持仓行——用户要看的是"这个仓位现在赚亏多少"
    position = _metrics(status)["持仓"].value
    assert position.startswith("-0.03012 BTC / $-1,912")
    assert "浮盈" in position
    assert _metrics(status)["库存上限"].value == "$1,912 / $3,750 (51%)"


def test_missing_inventory_usd_keeps_btc_amount(tmp_path):
    """缺美元库存时不猜金额，但已有的 BTC 数量必须保留。

    整行降级为「—」会把本来就有的持仓数量弄丢，那是信息倒退。
    「库存上限」没有 BTC 口径可退，才显示「—」。
    """
    live = dict(_LIVE)
    live.pop("inv_usd")

    status = _collect_details(tmp_path, live=live)

    assert status.error is None
    position = _metrics(status)["持仓"].value
    assert position != "—"
    assert "BTC" in position
    assert _metrics(status)["库存上限"].value == "—"


def test_position_is_dash_only_when_btc_amount_missing(tmp_path):
    live = dict(_LIVE)
    live.pop("inv_btc")

    status = _collect_details(tmp_path, live=live)

    assert _metrics(status)["持仓"].value == "—"


@pytest.mark.parametrize(
    ("distance_ratio", "expected_tone"),
    [
        (0.049, "bad"),
        (0.05, "warn"),
        (0.099, "warn"),
        (0.10, "good"),
    ],
)
def test_breaker_distance_tone_boundaries(tmp_path, distance_ratio, expected_tone):
    """熔断余量严格按小于 5% / 小于 10% 分级。"""
    equity = 1000.0
    peak = equity * (1 - distance_ratio) / 0.88
    status = _collect_details(
        tmp_path,
        monitor=[{**_MONITOR[-1], "equity": equity}],
        peak={"peak": peak},
    )
    assert _metrics(status)["距熔断线"].tone == expected_tone


@pytest.mark.parametrize("peak", [False, "{损坏的 json"])
def test_breaker_distance_degrades_when_peak_is_unavailable(tmp_path, peak):
    """峰值文件缺失或损坏只隐藏这一项，不能让整张卡失败。"""
    status = _collect_details(tmp_path, peak=peak)
    metric = _metrics(status)["距熔断线"]
    assert status.error is None
    assert (metric.value, metric.tone) == ("—", "normal")


@pytest.mark.parametrize(
    ("distance", "expected_tone"),
    [
        (0.149, "bad"),
        (0.15, "warn"),
        (0.249, "warn"),
        (0.25, "good"),
    ],
)
def test_liquidation_distance_tone_boundaries(tmp_path, distance, expected_tone):
    status = _collect_details(
        tmp_path,
        live={**_LIVE, "dist_to_liq_pct": distance},
    )
    assert _metrics(status)["距强平"].tone == expected_tone


@pytest.mark.parametrize(
    ("mark", "expected_tone"),
    [
        (109.0, "warn"),
        (110.0, "normal"),
        (150.0, "normal"),
        (190.0, "normal"),
        (191.0, "warn"),
    ],
)
def test_band_position_tone_boundaries(tmp_path, mark, expected_tone):
    status = _collect_details(
        tmp_path,
        live={**_LIVE, "mark": mark, "band_low": 100.0, "band_high": 200.0},
    )
    assert _metrics(status)["价格位置"].tone == expected_tone


@pytest.mark.parametrize(
    ("failed", "expected_tone"),
    [(0, "normal"), (1, "warn")],
)
def test_replacement_failure_controls_order_success_tone(
    tmp_path, failed, expected_tone
):
    status = _collect_details(
        tmp_path,
        live={**_LIVE, "replacement_failed": failed},
    )
    assert _metrics(status)["下单成功"].tone == expected_tone


@pytest.mark.parametrize(
    ("maximum", "expected_tone"),
    [(3750.0, "normal"), (3750.01, "warn")],
)
def test_historical_inventory_tone_boundary(tmp_path, maximum, expected_tone):
    status = _collect_details(
        tmp_path,
        live={**_LIVE, "max_abs_inv_usd": maximum},
    )
    assert _metrics(status)["历史最大库存"].tone == expected_tone
