"""注册表测试。核心保证：单个 provider 炸了，其他卡片照常出。"""

from __future__ import annotations

import panel.registry as registry
from panel.types import SystemStatus


def test_collect_all_returns_one_status_per_provider(monkeypatch):
    monkeypatch.setattr(registry, "PROVIDERS", [
        ("A", lambda: SystemStatus(name="A", alive=True, summary="")),
        ("B", lambda: SystemStatus(name="B", alive=True, summary="")),
    ])
    got = registry.collect_all()
    assert [s.name for s in got] == ["A", "B"]


def test_failing_provider_does_not_break_others(monkeypatch):
    """这是本模块存在的理由：一个 provider 抛异常不能让整页白屏。"""
    def boom():
        raise RuntimeError("provider 炸了")

    monkeypatch.setattr(registry, "PROVIDERS", [
        ("坏的", boom),
        ("好的", lambda: SystemStatus(name="好的", alive=True, summary="")),
    ])
    got = registry.collect_all()
    assert len(got) == 2
    assert got[0].error is not None and "炸了" in got[0].error
    assert got[1].error is None


def test_total_equity_skips_none(monkeypatch):
    monkeypatch.setattr(registry, "PROVIDERS", [
        ("A", lambda: SystemStatus(name="A", alive=True, summary="", equity=100.0)),
        ("B", lambda: SystemStatus(name="B", alive=None, summary="", equity=None)),
    ])
    assert registry.total_equity(registry.collect_all()) == 100.0


def test_system_counts_excludes_none_alive():
    """只有存在进程概念的系统进入分母，且仅 True 算在线。"""
    systems = [
        SystemStatus(name="在线", alive=True, summary=""),
        SystemStatus(name="离线", alive=False, summary=""),
        SystemStatus(name="无存活概念", alive=None, summary=""),
    ]
    assert registry.system_counts(systems) == (2, 1)


def test_total_pnl_summary_skips_none_and_reports_missing_systems():
    systems = [
        SystemStatus(name="网格", alive=True, summary="", total_pnl=49.5),
        SystemStatus(name="对冲", alive=True, summary="", total_pnl=-1.75),
        SystemStatus(name="归因", alive=None, summary=""),
    ]
    assert registry.total_pnl_summary(systems) == (47.75, ["归因"])


def test_collect_panel_alerts_translates(monkeypatch):
    from tools.alert_check import Alert

    monkeypatch.setattr(registry, "_collect_alerts", lambda: [
        Alert("lighter_hedge_stale", "⛔ 心跳陈旧", "已 2 分钟")
    ])
    alerts = registry.collect_panel_alerts()
    assert alerts[0].action.strip()


def test_alert_collection_failure_is_surfaced(monkeypatch):
    """告警本身取不到时，必须造一条告警告诉用户，不能静默当作没事。"""
    def boom():
        raise RuntimeError("alert_check 炸了")

    monkeypatch.setattr(registry, "_collect_alerts", boom)
    alerts = registry.collect_panel_alerts()
    assert len(alerts) == 1
    assert alerts[0].level == "critical"
