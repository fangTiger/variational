"""收益实测统计模块测试：验证派生指标计算与持久化。"""

from __future__ import annotations

from decimal import Decimal

from tracking.metrics import MetricsTracker, Snapshot

_WEEK = 7 * 24 * 3600


def test_summary_needs_two_snapshots(tmp_path) -> None:
    t = MetricsTracker(tmp_path / "m.jsonl")
    assert t.summary() is None
    t.record(Snapshot(ts=0, notional_usd=900, points_total=0, net_funding_usd=0, wear_usd=0))
    assert t.summary() is None  # 仍只有 1 条


def test_derived_metrics_over_one_week(tmp_path) -> None:
    """一周内名义 900U、积分 +0.9、净资金费 +1.8U、磨损 0.6U 的算术校验。"""
    t = MetricsTracker(tmp_path / "m.jsonl")
    t.record(Snapshot(ts=0, notional_usd=900, points_total=10.0, net_funding_usd=5.0, wear_usd=2.0))
    t.record(
        Snapshot(
            ts=_WEEK, notional_usd=900, points_total=10.9,
            net_funding_usd=6.8, wear_usd=2.6,
        )
    )
    s = t.summary()
    assert s is not None
    # 积分：+0.9；每千U每周 = 0.9 / (900/1000) = 1.0
    assert s.points_gained == Decimal("0.9")
    assert abs(s.points_per_1k_per_week - Decimal("1.0")) < Decimal("1e-9")
    # 净资金费 +1.8；年化 = 1.8/900 * 52.14 * 100 ≈ 10.43%
    assert s.net_funding_usd == Decimal("1.8")
    assert Decimal("10") < s.annualized_funding_pct < Decimal("11")
    # 磨损 0.6；周净盈亏 = 1.8 - 0.6 = 1.2
    assert s.wear_usd == Decimal("0.6")
    assert s.net_pnl_usd == Decimal("1.2")


def test_persistence_across_reload(tmp_path) -> None:
    path = tmp_path / "m.jsonl"
    t1 = MetricsTracker(path)
    t1.record(Snapshot(ts=0, notional_usd=900, points_total=0, net_funding_usd=0, wear_usd=0))
    t1.record(Snapshot(ts=_WEEK, notional_usd=900, points_total=1, net_funding_usd=1, wear_usd=0))
    # 重新加载应看到 2 条
    t2 = MetricsTracker(path)
    assert len(t2.snapshots) == 2
    assert t2.summary() is not None


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    # 每个测试用独立子目录，避免共用 jsonl 串数据
    for fn in (
        test_summary_needs_two_snapshots,
        test_derived_metrics_over_one_week,
        test_persistence_across_reload,
    ):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
    print("✅ metrics 测试通过")
