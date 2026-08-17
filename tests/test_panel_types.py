"""面板数据契约测试。"""

from __future__ import annotations

import pytest

from panel.types import Metric, PanelAlert, SystemStatus


def test_metric_defaults_to_normal_tone():
    m = Metric(label="权益", value="$997.87")
    assert m.tone == "normal"


def test_panel_alert_requires_non_empty_action():
    """动作指引不可为空——这是设计文档里的刚性约束。"""
    with pytest.raises(ValueError, match="action"):
        PanelAlert(key="k", level="critical", title="t", action="")


def test_panel_alert_rejects_unknown_level():
    with pytest.raises(ValueError, match="level"):
        PanelAlert(key="k", level="fatal", title="t", action="做点什么")


def test_system_status_allows_none_alive_and_equity():
    """归因卡片没有进程概念，alive/equity 都是 None。"""
    s = SystemStatus(name="收益归因", alive=None, summary="—", metrics=[], equity=None)
    assert s.alive is None
    assert s.equity is None
