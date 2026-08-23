"""定时定量对冲面板测试。"""

from __future__ import annotations

import json
import os
from decimal import Decimal

from tools import hedge_panel


NOW = Decimal("1787481600")


def _write_instance(
    tmp_path,
    *,
    name: str = "timed_volume",
    heartbeat: dict | None = None,
    running: bool = True,
):
    """写入一套面板测试数据并返回实例配置。"""
    state_dir = tmp_path / name
    state_dir.mkdir(parents=True)
    heartbeat_path = tmp_path / f"{name}.jsonl"
    if heartbeat is not None:
        heartbeat_path.write_text(
            json.dumps(heartbeat, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    state_path = state_dir / "state.json"
    state_path.write_text("{}\n", encoding="utf-8")
    lock_path = state_dir / "state.json.lock"
    if running:
        lock_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    return hedge_panel.InstanceConfig(
        key=name,
        name=f"测试实例 {name}",
        primary_exchange="主交易所",
        hedge_exchange="对冲交易所",
        heartbeat_path=heartbeat_path,
        state_path=state_path,
        lock_path=lock_path,
    )


def _heartbeat(**overrides) -> dict:
    """生成一条完整心跳。"""
    data = {
        "ts": str(NOW - Decimal("30")),
        "action": "opened",
        "round_index": 1,
        "direction": "long",
        "due_at": str(NOW + Decimal("3600")),
        "primary_size": "0.000325",
        "hedge_size": "-0.00032",
        "net_exposure": "0.000005",
        "hedge_available": True,
        "hedge_interlock_active": False,
        "notional_usd": "25",
        "warnings": [],
    }
    data.update(overrides)
    return data


def test_missing_files_do_not_crash_and_show_not_running(tmp_path) -> None:
    """全部数据文件缺失时仍渲染未运行状态。"""
    missing = hedge_panel.InstanceConfig(
        key="missing",
        name="缺失实例",
        primary_exchange="Lighter",
        hedge_exchange="Entropy",
        heartbeat_path=tmp_path / "missing.jsonl",
        state_path=tmp_path / "missing" / "state.json",
        lock_path=tmp_path / "missing" / "state.json.lock",
    )

    html = hedge_panel.build_page(instances=(missing,), now=NOW)

    assert "缺失实例" in html
    assert "未运行" in html
    assert "暂无心跳数据" in html


def test_stale_data_is_red_and_warned(tmp_path) -> None:
    """超过 120 秒的心跳必须以红色过期提示展示。"""
    instance = _write_instance(
        tmp_path,
        heartbeat=_heartbeat(ts=str(NOW - Decimal("121"))),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert 'class="data-time stale"' in html
    assert "121 秒前" in html
    assert "⚠ 数据可能已过期" in html


def test_interlock_outlines_whole_card(tmp_path) -> None:
    """互锁激活时整张实例卡描红并显示原因。"""
    instance = _write_instance(
        tmp_path,
        heartbeat=_heartbeat(
            hedge_interlock_active=True,
            hedge_interlock_reason="对冲账户不可用",
        ),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert 'class="instance-card interlocked"' in html
    assert "互锁已激活" in html
    assert "对冲账户不可用" in html


def test_net_exposure_color_levels(tmp_path) -> None:
    """净敞口按绝对值使用绿、黄、红三级配色。"""
    cases = (
        ("0.000099", "exposure-good"),
        ("-0.0001", "exposure-warn"),
        ("0.000999", "exposure-warn"),
        ("-0.001", "exposure-bad"),
    )
    for index, (exposure, expected_class) in enumerate(cases):
        instance = _write_instance(
            tmp_path,
            name=f"case_{index}",
            heartbeat=_heartbeat(net_exposure=exposure),
        )

        html = hedge_panel.build_page(instances=(instance,), now=NOW)

        assert f'class="net-value mono {expected_class}"' in html


def test_every_exposure_class_has_css_rule(tmp_path) -> None:
    """每个可能返回的配色 class 都必须有对应 CSS 规则。

    只断言 class 名字出现在 HTML 里是不够的：类名拼对、但样式表里没有
    对应规则时，最危险的「净敞口失控」会渲染成普通文字色，
    和正常状态在视觉上完全无法区分——而测试依然是绿的。
    """
    instance = _write_instance(tmp_path, name="css", heartbeat=_heartbeat())
    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    produced = {
        hedge_panel.exposure_class(value)
        for value in ("0.00001", "0.0005", "0.005", "-0.005", None, "")
    }
    assert produced, "至少应产生一个配色 class"

    for name in produced:
        assert f".{name} {{" in html, f"配色 class {name} 没有对应的 CSS 规则"


def test_page_contains_no_automatic_refresh_code(tmp_path) -> None:
    """页面只能由用户手动刷新。"""
    instance = _write_instance(tmp_path, heartbeat=_heartbeat())

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert 'meta http-equiv="refresh"' not in html
    assert "setInterval" not in html



def test_same_direction_legs_are_flagged(tmp_path) -> None:
    """两腿方向相同意味着对冲已失效，必须显式警示。

    这是最危险的状态：净敞口会翻倍而不是抵消。若页面只列出两个带符号
    小数、由人去心算符号，值班时极容易看漏。
    """
    instance = _write_instance(
        tmp_path,
        name="same",
        heartbeat=_heartbeat(primary_size="0.02", hedge_size="0.02"),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert "未形成对冲" in html
    assert "offset-bad" in html


def test_opposite_direction_legs_are_confirmed(tmp_path) -> None:
    """方向相反时给出对冲成立的明确结论。"""
    instance = _write_instance(
        tmp_path,
        name="hedged",
        heartbeat=_heartbeat(primary_size="0.02", hedge_size="-0.02"),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert "对冲成立" in html
    assert "offset-ok" in html


def test_each_leg_shows_side_and_usd_value(tmp_path) -> None:
    """每条腿都要标出多空方向与折合美元，而不只是一个带符号小数。"""
    instance = _write_instance(
        tmp_path,
        name="legs",
        heartbeat=_heartbeat(
            primary_size="-0.021761",
            hedge_size="0.02176",
            notional_usd=1680,
        ),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert "leg-short" in html and "leg-long" in html
    assert "-$1,680" in html
    assert "$1,680" in html
