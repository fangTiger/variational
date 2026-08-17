"""渲染器测试。纯函数，不碰文件系统。"""

from __future__ import annotations

from panel.render import render_page
from panel.types import Metric, PanelAlert, SystemStatus


_SYSTEMS = [
    SystemStatus(
        name="BTC 网格",
        alive=True,
        summary="标记价 $63,422",
        metrics=[Metric("权益", "$997.87"), Metric("熔断", "正常", "good")],
        equity=997.87,
    ),
    SystemStatus(name="RH 积分对冲", alive=True, summary="无需再平衡", metrics=[]),
    SystemStatus(name="收益归因", alive=None, summary="观察期未满", metrics=[]),
]


def test_page_contains_every_system_name():
    html = render_page(_SYSTEMS, [], total=997.87)
    assert "BTC 网格" in html
    assert "收益归因" in html


def test_alert_action_is_rendered():
    """动作指引必须出现在页面上，否则这套设计就白做了。"""
    alerts = [PanelAlert("k", "critical", "⛔ 心跳陈旧", "告诉 Claude 重启")]
    html = render_page(_SYSTEMS, alerts, total=0.0)
    assert "告诉 Claude 重启" in html


def test_no_alerts_shows_healthy_text():
    html = render_page(_SYSTEMS, [], total=0.0)
    assert "无异常" in html


def test_error_card_shows_reason_and_page_still_renders():
    bad = SystemStatus(name="坏的", alive=None, summary="采集失败", error="文件不见了")
    html = render_page([*_SYSTEMS, bad], [], total=0.0)
    assert "文件不见了" in html
    assert "BTC 网格" in html


def test_html_escapes_injected_text():
    """数据来自本地文件，但仍然不该让它注入标签。"""
    evil = SystemStatus(name="<script>x</script>", alive=True, summary="")
    html = render_page([evil], [], total=0.0)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_page_has_autorefresh():
    html = render_page(_SYSTEMS, [], total=0.0)
    assert "refresh" in html.lower()


def test_overview_shows_online_count_between_equity_and_alert_badge():
    html = render_page(_SYSTEMS, [], total=997.87)
    online_text = "2 个系统 / 2 个在线"

    assert online_text in html
    assert html.index("总权益") < html.index(online_text) < html.index("无告警")


def test_overview_shows_total_pnl_with_missing_marker_and_title():
    systems = [
        SystemStatus(name="BTC 网格", alive=True, summary="", total_pnl=49.5),
        SystemStatus(name="RH 积分对冲", alive=True, summary="", total_pnl=-1.75),
        SystemStatus(name="收益归因", alive=None, summary=""),
    ]
    html = render_page(systems, [], total=1_000.0)

    assert 'class="good"' in html
    assert 'title="未计入：收益归因"' in html
    assert "总收益 <b>$+47.75*</b>" in html


def test_overview_negative_total_pnl_is_bad_without_missing_marker():
    systems = [
        SystemStatus(name="BTC 网格", alive=True, summary="", total_pnl=-2.5),
        SystemStatus(name="RH 积分对冲", alive=True, summary="", total_pnl=-1.25),
    ]
    html = render_page(systems, [], total=1_000.0)

    assert '<span class="bad">总收益 <b>$-3.75</b></span>' in html
    assert "$-3.75*" not in html
