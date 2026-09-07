"""生产目录隔离与守护入口路径回归。"""
import asyncio

from tools import run_swap_carry_guard as guard


def test_guard_cli_forwards_history_path(tmp_path, monkeypatch):
    """入口必须把显式台账路径传到单轮执行器。"""
    captured = {}

    class Client:
        async def close(self):
            pass

    async def load():
        return Client()

    async def run_once(client, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(guard.execution, "_load", load)
    monkeypatch.setattr(guard, "run_once", run_once)
    target = tmp_path / "history.jsonl"
    args = guard.build_parser().parse_args(["--switch-history", str(target)])
    assert asyncio.run(guard._main(args)) == 0
    assert captured["switch_history_path"] == target


def test_legacy_writers_accept_paths(tmp_path):
    """历史工具的持久化出口同样支持逐次注入。"""
    from tools import alert_check, grid_monitor, pnl_attribution, trade_collector
    grid_monitor._record({"test": True}, path=tmp_path / "monitor.jsonl")
    pnl_attribution._write_result({"test": True}, path=tmp_path / "attribution.json")
    alert_check._save_cooldown({"test": True}, path=tmp_path / "cooldown.json")
    trade_collector._append([{"T": 0}], out_dir=tmp_path / "trades")
    trade_collector._append_gap(0, 1, out_dir=tmp_path / "trades")
    assert (tmp_path / "monitor.jsonl").exists()
    assert (tmp_path / "attribution.json").exists()
    assert (tmp_path / "cooldown.json").exists()
    assert len((tmp_path / "trades" / "1970-01-01.jsonl").read_text().splitlines()) == 2


def test_default_paths_are_isolated_per_test(tmp_path):
    """漏传参数的用例也只会落到当前 tmp_path。"""
    from tracking.direction_state import FundingDirectionStateStore
    from tracking.metrics import MetricsTracker
    assert guard.DEFAULT_SWITCH_HISTORY.parent == tmp_path
    assert guard.build_parser().parse_args([]).switch_history.parent == tmp_path
    assert FundingDirectionStateStore().path.parent == tmp_path
    assert MetricsTracker().path.parent == tmp_path


def test_multi_leg_helper_redirects_history(tmp_path):
    """直接复现产生三条污染预演的多腿测试路径遗漏。"""
    from tests.test_swap_carry_multi_leg import _guard_paths
    assert _guard_paths(tmp_path)["switch_history_path"].parent == tmp_path


def test_snapshot_guard_detects_all_mutations(tmp_path):
    """保护机制必须发现新增、删除、内容变化，以及同内容重写。"""
    import os
    import pytest
    from tests import conftest
    path = tmp_path / "state.json"
    path.write_text("original")
    before = conftest._data_snapshot(tmp_path)
    conftest._assert_data_unchanged(before, conftest._data_snapshot(tmp_path))
    path.write_text("changed")
    with pytest.raises(AssertionError, match="state.json"):
        conftest._assert_data_unchanged(before, conftest._data_snapshot(tmp_path))
    path.write_text("original")
    os.utime(path, ns=(path.stat().st_atime_ns, before["state.json"][0] + 1000000))
    with pytest.raises(AssertionError, match="state.json"):
        conftest._assert_data_unchanged(before, conftest._data_snapshot(tmp_path))
    path.unlink()
    with pytest.raises(AssertionError, match="state.json"):
        conftest._assert_data_unchanged(before, conftest._data_snapshot(tmp_path))
    with pytest.raises(AssertionError, match="new.json"):
        conftest._assert_data_unchanged({}, {"new.json": (0, "hash")})


def test_real_data_write_is_blocked_in_child_process(tmp_path):
    """在子进程验证拦截，避免预期违规污染主套件的违规清单。"""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = '''
import runpy
from pathlib import Path
ns = runpy.run_path("tests/conftest.py")
try:
    (ns["_REAL_DATA_DIR"] / "pollution-protection-probe.jsonl").write_text("must not be written")
except AssertionError as exc:
    print(exc)
else:
    raise SystemExit("保护失效")
'''
    env = dict(os.environ, PYTHONPATH=str(root))
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "测试禁止修改真实数据" in result.stdout
    assert not (root / "data" / "pollution-protection-probe.jsonl").exists()


def test_session_guard_runs_after_last_fixture_teardown(tmp_path):
    """即使用例全部通过，最后一个 fixture 留下的数据差异也必须使 pytest 失败。"""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    probe = tmp_path / "test_late_teardown.py"
    probe.write_text('''import pytest
from tests import conftest as guard

@pytest.fixture(scope="session", autouse=True)
def late_change():
    yield
    # 只构造差异证据，不碰真实目录。
    guard._DATA_BEFORE["__late_teardown_probe__.json"] = (0, "hash")

def test_passes():
    assert True
''', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", str(probe), "-q"],
        cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(root)),
        text=True, capture_output=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "真实 data/ 保护失败" in result.stdout
    assert "__late_teardown_probe__.json" in result.stdout
