"""告警动作映射测试。

最重要的是完整性测试：alert_check 里每一个 Alert key 都必须有动作指引。
新增告警时忘了写指引，会在这里就被拦下，而不是等用户半夜看到一条不知所云的红字。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from panel.actions import ACTIONS, level_for, to_panel_alert
from tools.alert_check import Alert


def _all_alert_keys() -> set[str]:
    """从 alert_check 源码里扫出全部 Alert key。"""
    src = Path("tools/alert_check.py").read_text(encoding="utf-8")
    return set(re.findall(r'Alert\(\s*\n?\s*"([a-z_]+)"', src))


def test_every_alert_key_has_action():
    missing = sorted(_all_alert_keys() - set(ACTIONS))
    assert not missing, f"这些告警缺少动作指引：{missing}"


def test_no_action_is_empty():
    empty = sorted(k for k, v in ACTIONS.items() if not v.strip())
    assert not empty, f"这些告警的动作指引是空的：{empty}"


def test_to_panel_alert_carries_action():
    a = Alert("lighter_hedge_stale", "⛔ 心跳陈旧", "已 2 分钟没有更新")
    p = to_panel_alert(a)
    assert p.key == "lighter_hedge_stale"
    assert p.level == "critical"
    assert "裸敞口" in p.action


def test_unknown_key_still_produces_usable_alert():
    """未知 key 不能让面板崩掉，也不能给出空动作。"""
    p = to_panel_alert(Alert("brand_new_key", "标题", "正文"))
    assert p.action.strip()
    assert "未登记" in p.action


def test_level_for_defaults_to_warning():
    assert level_for("brand_new_key") == "warning"
