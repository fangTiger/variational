"""把系统快照渲染成自包含的深色 HTML。纯函数，不读文件、不打网络。"""

from __future__ import annotations

from html import escape

from panel.registry import system_counts, total_pnl_summary
from panel.types import PanelAlert, SystemStatus

_TONE_COLOR = {
    "normal": "#d8dee9",
    "good": "#8fbf7f",
    "warn": "#e6c07b",
    "bad": "#e06c75",
}
_LEVEL_ICON = {"critical": "🔴", "warning": "🟡", "info": "⚪"}


def _dot(alive: bool | None) -> str:
    if alive is None:
        return '<span class="dot idle"></span>'
    return f'<span class="dot {"ok" if alive else "dead"}"></span>'


def _card(system: SystemStatus) -> str:
    if system.error:
        body = f'<p class="err">{escape(system.error)}</p>'
    else:
        rows = "".join(
            f'<tr><td>{escape(m.label)}</td>'
            f'<td style="color:{_TONE_COLOR.get(m.tone, _TONE_COLOR["normal"])}">'
            f"{escape(m.value)}</td></tr>"
            for m in system.metrics
        )
        body = f"<table>{rows}</table>"
    return (
        '<section class="card">'
        f"<h2>{_dot(system.alive)}{escape(system.name)}</h2>"
        f'<p class="sub">{escape(system.summary)}</p>'
        f"{body}</section>"
    )


def _alerts_block(alerts: list[PanelAlert]) -> str:
    if not alerts:
        return '<section class="card alerts"><h2>告警</h2><p class="ok">无异常</p></section>'
    items = "".join(
        f'<li class="{escape(a.level)}">'
        f'<div class="atitle">{_LEVEL_ICON.get(a.level, "⚪")} {escape(a.title)}</div>'
        f'<div class="aaction">→ {escape(a.action)}</div></li>'
        for a in alerts
    )
    return f'<section class="card alerts"><h2>告警</h2><ul>{items}</ul></section>'


def render_page(
    systems: list[SystemStatus], alerts: list[PanelAlert], *, total: float
) -> str:
    """渲染整页。systems 顺序即卡片顺序。"""
    critical = sum(1 for a in alerts if a.level == "critical")
    badge = "无告警" if not alerts else f"{len(alerts)} 条告警（{critical} 严重）"
    badge_cls = "ok" if not alerts else ("bad" if critical else "warn")
    total_systems, online_systems = system_counts(systems)
    pnl_total, pnl_missing = total_pnl_summary(systems)
    pnl_tone = "good" if pnl_total > 0 else ("bad" if pnl_total < 0 else "normal")
    pnl_marker = "*" if pnl_missing else ""
    pnl_title = (
        f' title="{escape("未计入：" + "、".join(pnl_missing))}"'
        if pnl_missing
        else ""
    )
    cards = "".join(_card(s) for s in systems)
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>实盘监控</title>
<style>
 body{{background:#20242b;color:#d8dee9;font:14px/1.6 -apple-system,sans-serif;margin:0;padding:20px}}
 .top{{display:flex;gap:24px;align-items:center;padding:14px 18px;background:#262b33;border-radius:10px;margin-bottom:16px}}
 .top b{{font-size:20px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}}
 .card{{background:#262b33;border-radius:10px;padding:14px 18px}}
 .card h2{{font-size:15px;margin:0 0 4px;display:flex;align-items:center;gap:8px}}
 .sub{{color:#8a92a0;margin:0 0 10px;font-size:12px}}
 table{{width:100%;border-collapse:collapse}}
 td{{padding:3px 0}} td:first-child{{color:#8a92a0}} td:last-child{{text-align:right}}
 .dot{{width:8px;height:8px;border-radius:50%;display:inline-block}}
 .dot.ok{{background:#8fbf7f}} .dot.dead{{background:#e06c75}} .dot.idle{{background:#6b7280}}
 .alerts{{margin-top:14px}} .alerts ul{{margin:0;padding:0;list-style:none}}
 .alerts li{{padding:8px 0;border-bottom:1px solid #30363f}}
 .alerts li:last-child{{border:none}}
 .atitle{{font-weight:600}} .aaction{{color:#8a92a0;font-size:13px;margin-top:2px}}
 .ok{{color:#8fbf7f}} .warn{{color:#e6c07b}} .bad{{color:#e06c75}} .err{{color:#e06c75}}
</style></head><body>
<div class="top"><span>总权益 <b>${total:,.2f}</b></span>
<span class="{pnl_tone}"{pnl_title}>总收益 <b>${pnl_total:+,.2f}{pnl_marker}</b></span>
<span>{total_systems} 个系统 / {online_systems} 个在线</span>
<span class="{badge_cls}">{escape(badge)}</span></div>
<div class="grid">{cards}</div>
{_alerts_block(alerts)}
</body></html>"""
