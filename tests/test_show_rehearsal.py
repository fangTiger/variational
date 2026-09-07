"""预演展示和告警必须与真实成交台账区分。"""
import json

from panel.providers import swap_carry as panel
from tools import show_switch_history


def test_recent_rehearsal_cli(tmp_path, capsys):
    from tools import show_rehearsal
    path = tmp_path / "history.jsonl"
    path.write_text("\n".join(map(json.dumps, [
        {"kind": "rehearsal", "conclusion": "ready", "timestamp": "旧记录"},
        {"kind": "switch", "status": "completed"},
        {"kind": "rehearsal", "conclusion": "blocked", "timestamp": "新记录",
         "blocking_reasons": ["可用保证金不足"], "open_legs": [{"market": "XAUT", "quantity": "0.5"}]},
    ])))
    assert show_rehearsal.main(["--path", str(path), "--last", "1"]) == 0
    output = capsys.readouterr().out
    assert "blocked" in output and "可用保证金不足" in output and "XAUT" in output
    assert "旧记录" not in output


def test_switch_display_excludes_rehearsals(tmp_path, capsys):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps({"kind": "rehearsal", "conclusion": "blocked"}))
    show_switch_history.main(["--path", str(path)])
    assert "尚无切换" in capsys.readouterr().out
    assert panel._last_switch_metric(path).value == "尚无切换"


def test_panel_rehearsal_critical(tmp_path):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"rehearsal_blocked": True, "last_rehearsal": {
        "timestamp": "2026-09-11T19:30:00+00:00", "conclusion": "blocked",
        "blocking_reasons": ["保证金不足"],
    }}))
    metric, alert = panel._rehearsal_metric(path)
    assert metric.label == "上次预演" and "blocked" in metric.value
    assert alert.level == "critical"
    assert "保证金不足" in alert.title
