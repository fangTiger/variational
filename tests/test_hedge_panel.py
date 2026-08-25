"""定时定量对冲面板测试。"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

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


def _write_portfolio_equity(tmp_path, rows: list[dict]) -> Path:
    """写入组合级权益测试数据并返回路径。"""
    path = tmp_path / "portfolio_equity.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _write_portfolio_volume(tmp_path, rows: list[dict]) -> Path:
    """写入组合累计成交量测试数据并返回路径。"""
    path = tmp_path / "portfolio_volume.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


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


def test_net_exposure_is_compact_normally_and_prominent_when_any_instance_is_bad(
    tmp_path,
) -> None:
    """正常净敞口只占灰色小行，任一实例超限时总览与实例行都放大标红。"""
    normal = _write_instance(
        tmp_path,
        name="normal_exposure",
        heartbeat=_heartbeat(net_exposure="0.000005"),
    )
    bad = _write_instance(
        tmp_path,
        name="bad_exposure",
        heartbeat=_heartbeat(net_exposure="0.002"),
    )

    normal_html = hedge_panel.build_page(instances=(normal,), now=NOW)
    bad_html = hedge_panel.build_page(instances=(normal, bad), now=NOW)

    assert 'class="exposure-summary exposure-summary-normal"' in normal_html
    assert 'class="net-status"' in normal_html
    assert 'class="exposure-summary exposure-summary-danger"' not in normal_html
    assert 'class="exposure-summary exposure-summary-danger"' in bad_html
    assert 'class="net-status net-status-danger"' in bad_html
    assert ".exposure-summary-normal {" in normal_html
    assert ".exposure-summary-danger {" in normal_html
    assert ".net-status-danger {" in normal_html


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
    assert "WebSocket" not in html
    assert "fetch(" not in html


def test_panel_source_keeps_zero_credential_boundary() -> None:
    """面板只能读本地心跳，不得引入交易所连接或凭据加载能力。"""
    source = Path(hedge_panel.__file__).read_text(encoding="utf-8").casefold()

    for forbidden in ("getenv", "load_dotenv", "httpx", "requests", "私钥"):
        assert forbidden not in source


def test_portfolio_cumulative_pnl_sums_each_account_delta_with_sources(tmp_path) -> None:
    """累计盈亏按账户分别做首末差，并提示本地计算口径。"""
    instance = _write_instance(
        tmp_path,
        name="cumulative",
        heartbeat=_heartbeat(pair_pnl="-1.24"),
    )
    portfolio_path = _write_portfolio_equity(
        tmp_path,
        [
            {
                "schema": 4,
                "ts": "1787481600.1",
                "accounts": {
                    "lighter": "100.000000000000000001",
                    "variational": "500",
                    "hyperliquid": "-10",
                },
                "sources": {
                    "lighter": "platform",
                    "variational": "computed",
                    "hyperliquid": "platform",
                },
            },
            {
                "schema": 4,
                "ts": "1787485200.2",
                "accounts": {"variational": "502", "hyperliquid": "-12"},
                "sources": {
                    "variational": "computed",
                    "hyperliquid": "platform",
                },
            },
            {
                "schema": 4,
                "ts": "1787488800.3",
                "accounts": {
                    "lighter": "104.000000000000000003",
                    "hyperliquid": "-11",
                },
                "sources": {
                    "lighter": "platform",
                    "hyperliquid": "platform",
                },
            },
        ],
    )

    summary = hedge_panel.read_portfolio_equity_summary(portfolio_path)
    html = hedge_panel.build_page(
        instances=(instance,),
        portfolio_equity_path=portfolio_path,
        now=NOW,
    )

    assert summary.cumulative_pnl == Decimal("5.000000000000000002")
    assert summary.first_equity == Decimal("590.000000000000000001")
    assert summary.latest_equity == Decimal("595.000000000000000003")
    assert summary.computed_accounts == frozenset({"variational"})
    assert "本对盈亏" in html
    assert "累计盈亏" in html
    assert "+$5.00" in html
    assert "自 08-23 18:40 起" in html
    assert "本地计算口径：variational" in html
    assert "平台官方口径" in html
    assert 'class="portfolio-pnl-value mono pnl-positive">+$5.00</' in html
    assert "全部实例累计盈亏" not in html
    assert "cumulative-pnl-block" not in html


def test_portfolio_cumulative_pnl_uses_only_schema_four_snapshots(tmp_path) -> None:
    """schema 4 上线后不得混入旧权益公式的历史快照。"""
    portfolio_path = _write_portfolio_equity(
        tmp_path,
        [
            {"schema": 3, "ts": "1000.1", "total_equity": "900.00"},
            {"schema": 3, "ts": "1001.1", "total_equity": "950.00"},
            {
                "schema": 4,
                "ts": "1002.1",
                "accounts": {
                    "lighter": "10.000000000000000001",
                    "variational": "100",
                },
                "sources": {"lighter": "platform", "variational": "computed"},
            },
            {
                "schema": 4,
                "ts": "1003.1",
                "accounts": {
                    "lighter": "12.000000000000000003",
                    "variational": "101",
                },
                "sources": {"lighter": "platform", "variational": "computed"},
            },
        ],
    )

    summary = hedge_panel.read_portfolio_equity_summary(portfolio_path)

    assert summary.snapshot_count == 2
    assert summary.started_at == Decimal("1002.1")
    assert summary.first_equity == Decimal("110.000000000000000001")
    assert summary.latest_equity == Decimal("113.000000000000000003")
    assert summary.cumulative_pnl == Decimal("3.000000000000000002")


def test_fewer_than_two_portfolio_snapshots_show_accumulating(tmp_path) -> None:
    """组合权益快照不足两条时不得伪装成零收益。"""
    instance = _write_instance(tmp_path, name="accumulating", heartbeat=_heartbeat())
    portfolio_path = _write_portfolio_equity(
        tmp_path,
        [
            {
                "schema": 4,
                "ts": "1787481600.1",
                "accounts": {"lighter": "561.49"},
                "sources": {"lighter": "platform"},
            }
        ],
    )

    html = hedge_panel.build_page(
        instances=(instance,),
        portfolio_equity_path=portfolio_path,
        now=NOW,
    )

    assert html.count("累计中…") == 1
    assert "+$0.00" not in html
    assert "portfolio-pnl-value mono pnl-missing" in html


def test_portfolio_volume_does_not_show_approximation_for_lighter(tmp_path) -> None:
    """Lighter 已用官方成交额，面板不得再显示约等号或推算提示。"""
    instance = _write_instance(tmp_path, name="volume", heartbeat=_heartbeat())
    volume_path = _write_portfolio_volume(
        tmp_path,
        [
            {
                "ts": 1787590000.0,
                "instances": {
                    "lighter_entropy": {
                        "symbol": "BTC",
                        "since": 1787383380.0,
                        "primary": "155866.12",
                        "hedge": "155866.12",
                        "estimated": ["primary"],
                    },
                    "variational_entropy": {
                        "symbol": "BTC",
                        "since": 1787475540.0,
                        "primary": "24480.00",
                        "hedge": "22683.40",
                        "estimated": [],
                    },
                    "lighter_variational_eth": {
                        "symbol": "ETH",
                        "since": 1787578800.0,
                        "primary": "1936.00",
                        "hedge": "1936.00",
                        "estimated": ["primary"],
                    },
                },
                "totals_by_symbol": {
                    "BTC": "358895.64",
                    "ETH": "3872.00",
                },
            }
        ],
    )

    summary = hedge_panel.read_portfolio_volume_summary(volume_path)
    html = hedge_panel.build_page(
        instances=(instance,),
        portfolio_volume_path=volume_path,
        now=NOW,
    )

    assert summary.totals_by_symbol == {
        "BTC": Decimal("358895.64"),
        "ETH": Decimal("3872.00"),
    }
    assert summary.estimated_symbols == frozenset()
    assert "累计成交量" in html
    assert "BTC $358,896 · ETH $3,872" in html
    assert "合计 $362,768" in html
    assert "BTC ≈$" not in html
    assert "ETH ≈$" not in html
    assert "合计 ≈$" not in html
    assert "≈ 表示含 Lighter 对手腿推算值" not in html
    assert "全部金额均来自交易所成交记录" in html


def test_missing_portfolio_volume_shows_collecting_instead_of_zero(tmp_path) -> None:
    """累计成交量数据缺失时显示统计中，不能伪装为零。"""
    instance = _write_instance(
        tmp_path,
        name="volume_missing",
        heartbeat=_heartbeat(),
    )

    html = hedge_panel.build_page(
        instances=(instance,),
        portfolio_volume_path=tmp_path / "missing_volume.jsonl",
        now=NOW,
    )

    assert "累计成交量" in html
    assert "统计中…" in html
    assert "合计 $0" not in html


def test_each_leg_and_pair_render_signed_pnl_with_entries(tmp_path) -> None:
    """单腿盈亏需分色，卡片内本对合计必须更醒目并解释正确口径。"""
    instance = _write_instance(
        tmp_path,
        name="pnl",
        heartbeat=_heartbeat(
            primary_pnl="4.33",
            hedge_pnl="-5.57",
            primary_entry="77299.3",
            hedge_entry="77301.1",
            pair_pnl="-1.24",
        ),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert 'class="leg-pnl mono pnl-positive">+$4.33</' in html
    assert 'class="leg-pnl mono pnl-negative">-$5.57</' in html
    assert "入场 77299.3" in html
    assert "入场 77301.1" in html
    assert "本对盈亏" in html
    assert 'class="pair-pnl-value mono pnl-negative">-$1.24</' in html
    assert "两腿盈亏相互抵消，本对合计才是真实损益" in html


def test_missing_pnl_renders_dashes_without_crashing(tmp_path) -> None:
    """旧心跳没有盈亏字段时，单腿、卡片和总览均安全显示破折号。"""
    instance = _write_instance(tmp_path, name="old", heartbeat=_heartbeat())

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert html.count('class="leg-pnl mono pnl-missing">—</') == 2
    assert 'class="pair-pnl-value mono pnl-missing">—</' in html
    assert "1 对合计盈亏" in html


def test_overview_sums_two_pair_pnls(tmp_path) -> None:
    """顶部总览使用两张卡片的本对盈亏之和，不能汇总单腿数字。"""
    first = _write_instance(
        tmp_path,
        name="first_pnl",
        heartbeat=_heartbeat(pair_pnl="1.25"),
    )
    second = _write_instance(
        tmp_path,
        name="second_pnl",
        heartbeat=_heartbeat(pair_pnl="-0.50"),
    )

    html = hedge_panel.build_page(instances=(first, second), now=NOW)

    assert "2 对合计盈亏" in html
    assert 'class="mono pnl-positive">+$0.75</strong>' in html



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


def test_fresh_position_read_failure_is_not_rendered_as_empty_position(tmp_path) -> None:
    """新鲜心跳明确读取失败时，两条腿必须显示读取失败而不是空仓。"""
    instance = _write_instance(
        tmp_path,
        name="read_failed",
        heartbeat=_heartbeat(
            action="position_read_failed",
            primary_size=None,
            hedge_size=None,
            net_exposure=None,
        ),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert html.count('class="mono leg-read-failed">⚠ 读取失败</strong>') == 2
    assert 'class="mono leg-flat">空仓</strong>' not in html


def test_zero_position_is_rendered_as_empty_position_not_read_failure(tmp_path) -> None:
    """数值零是已确认的空仓，不得误报为读取失败。"""
    instance = _write_instance(
        tmp_path,
        name="empty",
        heartbeat=_heartbeat(
            primary_size="0",
            hedge_size=0,
            net_exposure="0",
        ),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert html.count('class="mono leg-flat">空仓</strong>') == 2
    assert 'class="mono leg-read-failed">' not in html


def test_read_failure_shows_previous_valid_readings_and_their_ages(tmp_path) -> None:
    """读取失败时回溯每个字段最近的有效读数，并显示距当前的秒数。"""
    instance = _write_instance(
        tmp_path,
        name="history",
        heartbeat=_heartbeat(
            action="position_read_failed",
            primary_size=None,
            hedge_size=None,
            net_exposure=None,
        ),
    )
    heartbeats = (
        _heartbeat(
            ts=str(NOW - Decimal("95")),
            primary_size="-0.02630",
            hedge_size="0.02625",
            net_exposure="-0.00005",
        ),
        _heartbeat(
            ts=str(NOW - Decimal("30")),
            action="position_read_failed",
            primary_size=None,
            hedge_size=None,
            net_exposure=None,
        ),
    )
    instance.heartbeat_path.write_text(
        "".join(json.dumps(item) + "\n" for item in heartbeats),
        encoding="utf-8",
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert "上次读数 -0.02630（95 秒前）" in html
    assert "上次读数 0.02625（95 秒前）" in html
    assert "上次读数 -0.00005（95 秒前）" in html


def test_previous_reading_scan_is_limited_to_recent_lines(tmp_path, monkeypatch) -> None:
    """回溯只读取末尾有限行，不能随心跳文件增长而扫描整个文件。"""
    heartbeat_path = tmp_path / "long.jsonl"
    old_valid = _heartbeat(
        ts=str(NOW - Decimal("5000")),
        primary_size="9.99",
    )
    failures = [
        _heartbeat(
            ts=str(NOW - Decimal(index)),
            action="position_read_failed",
            primary_size=None,
            hedge_size=None,
            net_exposure=None,
        )
        for index in range(5000, 0, -1)
    ]
    heartbeat_path.write_text(
        "".join(json.dumps(item) + "\n" for item in (old_valid, *failures)),
        encoding="utf-8",
    )
    file_size = heartbeat_path.stat().st_size
    bytes_read = 0
    original_open = Path.open

    class CountingReader:
        """统计目标二进制文件的实际读取字节数。"""

        def __init__(self, stream) -> None:
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            nonlocal bytes_read
            chunk = self.stream.read(size)
            bytes_read += len(chunk)
            return chunk

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def counted_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == heartbeat_path and args and args[0] == "rb":
            return CountingReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", counted_open)

    readings = hedge_panel.read_previous_readings(heartbeat_path, max_lines=200)

    assert readings == {}
    assert bytes_read < file_size


def test_missing_net_exposure_makes_overview_total_unavailable(tmp_path) -> None:
    """任一实例净敞口读取失败时，总览不得展示不完整的部分合计。"""
    failed = _write_instance(
        tmp_path,
        name="failed_exposure",
        heartbeat=_heartbeat(
            action="position_read_failed",
            primary_size=None,
            hedge_size=None,
            net_exposure=None,
        ),
    )
    valid = _write_instance(
        tmp_path,
        name="valid_exposure",
        heartbeat=_heartbeat(net_exposure="0.000005"),
    )

    html = hedge_panel.build_page(instances=(failed, valid), now=NOW)

    assert (
        "2 个实例净敞口合计</span>\n"
        '        <strong class="mono exposure-missing">—</strong>'
    ) in html
    assert 'class="net-value mono exposure-read-failed">⚠ 读取失败</strong>' in html


def test_read_failure_uses_yellow_hint_without_interlock_red_outline(tmp_path) -> None:
    """常态读取抖动使用黄色提示，不得冒充互锁的整卡红色警报。"""
    instance = _write_instance(
        tmp_path,
        name="yellow_failure",
        heartbeat=_heartbeat(
            action="position_read_failed",
            primary_size=None,
            hedge_size=None,
            net_exposure=None,
            hedge_interlock_active=False,
        ),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert 'class="instance-card read-failed"' in html
    assert 'class="instance-card interlocked"' not in html
    assert 'class="read-status">⚠ 持仓读取失败</span>' in html
    assert ".instance-card.read-failed:not(.interlocked)" in html


def test_overview_counts_follow_actual_instance_count(tmp_path) -> None:
    """总览标题里的数量必须跟随实际实例数，不能写死。

    原先硬编码成「两个实例」「两对合计」，加第三对时页面就开始说谎。
    这条测试用 1 / 2 / 3 个实例各渲染一次，确保数字随之变化。
    """
    instances = [
        _write_instance(tmp_path, name=f"n{i}", heartbeat=_heartbeat())
        for i in range(3)
    ]

    for count in (1, 2, 3):
        html = hedge_panel.build_page(instances=tuple(instances[:count]), now=NOW)

        assert f"{count} 个实例净敞口合计" in html
        assert f"{count} 对合计盈亏" in html


def test_default_instances_have_unique_paths() -> None:
    """默认实例清单的各路径必须互不相同。

    路径写重了会让两张卡片显示同一份数据，而页面上看不出异常。
    这里刻意**不锁死实例数量**——配对会随策略调整增减
    （2026-08-25 就从三对变成两对），锁数量只会让正确的变更被判成错误。
    """
    configs = hedge_panel.DEFAULT_INSTANCES

    assert configs, "至少要有一个实例"
    for attr in ("key", "heartbeat_path", "state_path", "lock_path"):
        values = [getattr(c, attr) for c in configs]
        assert len(set(values)) == len(values), f"{attr} 存在重复"
    assert all(not hasattr(config, "equity_path") for config in configs)


def test_cards_layout_adapts_to_instance_count(tmp_path) -> None:
    """卡片列数必须自适应宽度，不能写死列数。

    写死 `repeat(2, ...)` 时，加到第三对就会折行，用户得下拉才能看全。
    用 auto-fit 让够宽时所有实例排在同一行，加第四对也不会突然折行。
    """
    instance = _write_instance(tmp_path, heartbeat=_heartbeat())

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert "grid-template-columns: repeat(auto-fit, minmax(" in html
    assert "repeat(2, minmax(0, 1fr));\n        gap: 14px" not in html


def test_run_days_come_from_first_heartbeat_not_process_start(tmp_path) -> None:
    """已运行天数取首条心跳时间，重启进程不应把计数清零。

    锁文件里的 started_at 每次重启都会刷新，用它算天数会一直显示很小的值，
    看不出策略实际跑了多久。
    """
    instance = _write_instance(tmp_path, name="days", heartbeat=_heartbeat())
    # 在最新心跳之前补一条三天前的首条心跳
    old = _heartbeat(ts=str(NOW - Decimal("259200")))
    latest = instance.heartbeat_path.read_text(encoding="utf-8")
    instance.heartbeat_path.write_text(
        json.dumps(old, ensure_ascii=False) + "\n" + latest, encoding="utf-8"
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert "已跑 3.0 天" in html
    assert "3.0 实例·天" in html


def test_run_days_show_hours_when_under_one_day(tmp_path) -> None:
    """不足一天时用小时表示，避免显示 0.0 天让人以为没在跑。"""
    instance = _write_instance(
        tmp_path,
        name="hours",
        heartbeat=_heartbeat(ts=str(NOW - Decimal("7200"))),
    )

    html = hedge_panel.build_page(instances=(instance,), now=NOW)

    assert "已跑 2.0 小时" in html


def test_total_run_days_sum_across_instances(tmp_path) -> None:
    """累计运行按实例·天求和，反映总刷量时长。"""
    first = _write_instance(
        tmp_path, name="a", heartbeat=_heartbeat(ts=str(NOW - Decimal("172800")))
    )
    second = _write_instance(
        tmp_path, name="b", heartbeat=_heartbeat(ts=str(NOW - Decimal("86400")))
    )

    html = hedge_panel.build_page(instances=(first, second), now=NOW)

    assert "3.0 实例·天" in html


def test_cumulative_sums_deltas_across_mixed_sources(tmp_path) -> None:
    """平台口径与自算口径可以混用，因为累计取的是各账户的**差值**。

    平台账户记录的是「累计盈亏」，自算账户记录的是「权益」——绝对值含义不同，
    但两者的差值都等于该窗口内的盈亏，所以求和有意义。
    这条断言锁住这个前提；若改成先求和再作差，结果将毫无意义。
    """
    path = tmp_path / "portfolio_equity.jsonl"
    rows = [
        {
            "schema": 4,
            "ts": 1000,
            "accounts": {"a": "-436.00", "b": "561.00", "c": "7.00"},
            "sources": {"a": "platform", "b": "computed", "c": "platform"},
        },
        {
            "schema": 4,
            "ts": 2000,
            "accounts": {"a": "-440.00", "b": "558.00", "c": "9.00"},
            "sources": {"a": "platform", "b": "computed", "c": "platform"},
        },
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    summary = hedge_panel.read_portfolio_equity_summary(path)

    # -4（平台）+ -3（自算）+ +2（平台）
    assert summary.cumulative_pnl == Decimal("-5.00")
    assert summary.computed_accounts == frozenset({"b"})
