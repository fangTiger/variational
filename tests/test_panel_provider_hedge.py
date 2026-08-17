"""对冲 provider 测试。净敞口是这张卡最要紧的数，必须一眼看出对不对。"""

from __future__ import annotations

import json
import time

import pytest

from panel.providers.hedge import collect


def _snap(**kw) -> dict:
    row = {
        "ts": time.time(),
        "interval": 30,
        "primary_size": "0.02362",
        "hedge_size": "-0.02362",
        "net_delta": "0.00000",
        "action": "无需再平衡",
        "primary_read_ok": True,
        "hedge_read_ok": True,
        "primary_notional_exceeded": False,
        "rebalance_threshold_ratio": "0.02",
        "hedge_free_margin_ratio": "0.78",
        "min_hedge_free_margin_ratio": "0.20",
        "hedge_margin_error": None,
        "primary_notional": "1499",
        "hedge_notional": "1499.052990",
        "primary_unrealized": "-1.25",
        "hedge_unrealized": "0.30",
        "max_primary_notional": "2000",
        "primary_collateral": "489.27",
        "hedge_equity": "465.23",
    }
    row.update(kw)
    return row


def _write(tmp_path, rows):
    p = tmp_path / "lighter_hedge.jsonl"
    if rows is not None:
        p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _collect_with_baseline(tmp_path, *, baseline=None, **snapshot):
    """显式写入基线，避免测试读取真实 data 目录。"""
    baseline = {"total": 950.0, "ts": 1786930000.0} if baseline is None else baseline
    baseline_path = tmp_path / "hedge_baseline.json"
    if isinstance(baseline, str):
        baseline_path.write_text(baseline, encoding="utf-8")
    elif baseline is not False:
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    return collect(
        snapshot_path=_write(tmp_path, [_snap(**snapshot)]),
        baseline_path=baseline_path,
    )


def _metrics(status):
    return {metric.label: metric for metric in status.metrics}


def test_missing_file_does_not_raise(tmp_path):
    s = collect(snapshot_path=_write(tmp_path, None))
    assert s.alive is False
    assert s.error is not None


def test_neutral_position_is_good(tmp_path):
    s = collect(snapshot_path=_write(tmp_path, [_snap()]))
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["净敞口"] == "good"


def test_non_zero_net_delta_is_bad(tmp_path):
    """净敞口不为零意味着用户在裸奔，必须红色。"""
    s = collect(snapshot_path=_write(tmp_path, [_snap(hedge_size="0", net_delta="0.02362")]))
    tones = {m.label: m.tone for m in s.metrics}
    assert tones["净敞口"] == "bad"


def test_stale_heartbeat_marks_dead(tmp_path):
    """心跳超过 3×interval 视为失联。"""
    s = collect(snapshot_path=_write(tmp_path, [_snap(ts=time.time() - 300)]))
    assert s.alive is False


def test_equity_sums_both_legs(tmp_path):
    """两条腿是一个整体，权益取 Lighter 抵押品 + Extended 权益之和。"""
    s = collect(snapshot_path=_write(tmp_path, [
        _snap(primary_collateral="489.27", hedge_equity="465.23")
    ]))
    assert s.equity == 954.5


def test_equity_is_none_when_fields_missing(tmp_path):
    """老版本心跳没有权益字段时不参与汇总，也不能报错。"""
    s = collect(snapshot_path=_write(tmp_path, [
        _snap(primary_collateral=None, hedge_equity=None)
    ]))
    assert s.equity is None


def test_equity_is_none_when_only_one_leg_available(tmp_path):
    """只有一条腿的数不能算总权益，那会少算一半。"""
    s = _collect_with_baseline(
        tmp_path,
        primary_collateral=None,
        hedge_equity="465.23",
    )
    assert s.equity is None
    assert s.total_pnl is None
    assert _metrics(s)["总收益"].value == "—"


def test_detail_metrics_are_appended_in_order_and_formatted(tmp_path):
    """总收益跟随两腿权益，其余详情保持顺序且不访问交易所。"""
    status = _collect_with_baseline(tmp_path)
    details = status.metrics[6:]

    assert [metric.label for metric in details] == [
        "名义金额",
        "上限占用",
        "Lighter 权益",
        "Extended 权益",
        "总收益",
        "当前持仓浮盈",
        "再平衡阈值",
    ]
    assert {metric.label: metric.value for metric in details} == {
        "名义金额": "$1,499",
        "上限占用": "$1,499 / $2,000 (75%)",
        "Lighter 权益": "$489.27",
        "Extended 权益": "$465.23",
        "总收益": "$+4.50（自 08-17）",
        "当前持仓浮盈": "$-0.95",
        "再平衡阈值": "2.0%",
    }
    assert _metrics(status)["总收益"].tone == "good"
    assert _metrics(status)["当前持仓浮盈"].tone == "good"
    assert status.total_pnl == 4.5


def test_leg_metrics_include_signed_btc_and_notional(tmp_path):
    status = _collect_with_baseline(tmp_path)

    assert _metrics(status)["Lighter 腿"].value == "+0.02362 BTC / $1,499"
    assert _metrics(status)["Extended 腿"].value == "-0.02362 BTC / $1,499"


def test_current_position_pnl_sums_negative_two_leg_values(tmp_path):
    status = _collect_with_baseline(
        tmp_path,
        primary_unrealized="-1.25",
        hedge_unrealized="0.30",
    )

    metric = _metrics(status)["当前持仓浮盈"]
    assert (metric.value, metric.tone) == ("$-0.95", "good")


@pytest.mark.parametrize("missing_key", ["primary_unrealized", "hedge_unrealized"])
def test_current_position_pnl_is_dash_when_only_one_leg_exists(
    tmp_path,
    missing_key,
):
    """只有一条腿的浮盈会严重误导，必须拒绝计算。"""
    row = _snap()
    row.pop(missing_key)

    status = collect(snapshot_path=_write(tmp_path, [row]))

    metric = _metrics(status)["当前持仓浮盈"]
    assert (metric.value, metric.tone) == ("—", "normal")


@pytest.mark.parametrize(
    ("total", "expected_tone"),
    [
        ("4.99", "good"),
        ("-4.99", "good"),
        ("5", "warn"),
        ("19.99", "warn"),
        ("20", "bad"),
        ("-20", "bad"),
    ],
)
def test_current_position_pnl_tone_boundaries(tmp_path, total, expected_tone):
    status = _collect_with_baseline(
        tmp_path,
        primary_unrealized=total,
        hedge_unrealized="0",
    )

    assert _metrics(status)["当前持仓浮盈"].tone == expected_tone


@pytest.mark.parametrize(
    ("missing_key", "label", "size_key"),
    [
        ("primary_notional", "Lighter 腿", "primary_size"),
        ("hedge_notional", "Extended 腿", "hedge_size"),
    ],
)
def test_leg_keeps_btc_size_when_notional_is_missing(
    tmp_path, missing_key, label, size_key
):
    """缺名义金额时仍要显示持仓数量。

    整行降级为「—」会把本来就有的数量弄丢，那是信息倒退。
    这与「总收益」「两腿浮盈」不同——那两个是合计值，
    半边数据算出来是错的，必须整项降级。
    """
    row = _snap()
    row.pop(missing_key)

    status = collect(snapshot_path=_write(tmp_path, [row]))

    assert status.error is None
    value = _metrics(status)[label].value
    assert value != "—"
    assert "BTC" in value
    assert "$" not in value


def test_leg_is_dash_only_when_size_itself_is_missing(tmp_path):
    row = _snap()
    row.pop("primary_size")

    status = collect(snapshot_path=_write(tmp_path, [row]))

    assert _metrics(status)["Lighter 腿"].value == "—"


@pytest.mark.parametrize(
    ("notional", "maximum", "expected_tone"),
    [
        ("1800", "2000", "normal"),
        ("1800.01", "2000", "warn"),
        ("2000", "2000", "warn"),
        ("2000.01", "2000", "bad"),
    ],
)
def test_notional_usage_tone_boundaries(
    tmp_path, notional, maximum, expected_tone
):
    status = collect(snapshot_path=_write(tmp_path, [
        _snap(primary_notional=notional, max_primary_notional=maximum)
    ]))
    tones = {metric.label: metric.tone for metric in status.metrics}
    assert tones["上限占用"] == expected_tone


def test_old_heartbeat_missing_detail_fields_degrades_each_metric(tmp_path):
    """老版本心跳缺字段时只显示破折号，整张卡仍可用。"""
    row = _snap()
    for key in (
        "primary_notional",
        "hedge_notional",
        "primary_unrealized",
        "hedge_unrealized",
        "max_primary_notional",
        "primary_collateral",
        "hedge_equity",
        "rebalance_threshold_ratio",
    ):
        row.pop(key)

    status = collect(snapshot_path=_write(tmp_path, [row]))

    values = {metric.label: metric.value for metric in status.metrics[6:]}
    assert status.error is None
    assert values == {
        "名义金额": "—",
        "上限占用": "—",
        "Lighter 权益": "—",
        "Extended 权益": "—",
        "总收益": "—",
        "当前持仓浮盈": "—",
        "再平衡阈值": "—",
    }
    # 两条腿的数量字段没被删，所以仍要显示出来——这正是机器人重启前的真实情况
    assert _metrics(status)["Lighter 腿"].value == "+0.02362 BTC"
    assert _metrics(status)["Extended 腿"].value == "-0.02362 BTC"
    assert all(metric.tone == "normal" for metric in status.metrics[6:])


def test_total_pnl_is_dash_when_baseline_is_missing(tmp_path):
    status = _collect_with_baseline(tmp_path, baseline=False)
    metric = _metrics(status)["总收益"]
    assert (metric.value, metric.tone) == ("—", "normal")
    assert status.total_pnl is None


def test_negative_total_pnl_is_bad(tmp_path):
    status = _collect_with_baseline(
        tmp_path,
        baseline={"total": 960.0, "ts": 1786930000.0},
    )
    metric = _metrics(status)["总收益"]
    assert (metric.value, metric.tone) == ("$-5.50（自 08-17）", "bad")
    assert status.total_pnl == -5.5
