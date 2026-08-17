"""HTTP 层测试。只验证路由与装配，页面内容由 render 的测试覆盖。"""

from __future__ import annotations

from tools.grid_panel import build_unified_page


def test_build_unified_page_returns_html(monkeypatch):
    import panel.registry as registry
    from panel.types import SystemStatus

    monkeypatch.setattr(registry, "PROVIDERS", [
        ("A", lambda: SystemStatus(name="A", alive=True, summary="", equity=10.0)),
    ])
    monkeypatch.setattr(registry, "_collect_alerts", lambda: [])
    html = build_unified_page()
    assert html.startswith("<!doctype html>")
    assert "A" in html


def test_build_unified_page_survives_total_failure(monkeypatch):
    """连注册表都炸了也要出一个页面，白屏比错误信息更危险。"""
    import panel.registry as registry

    def boom():
        raise RuntimeError("全炸了")

    monkeypatch.setattr(registry, "collect_all", boom)
    html = build_unified_page()
    assert "<!doctype html>" in html
    assert "全炸了" in html
