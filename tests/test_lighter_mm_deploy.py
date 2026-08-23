"""Lighter 做市 launchd 常驻配置测试。"""

from __future__ import annotations

import plistlib
from pathlib import Path

from tools import run_lighter_mm


ROOT = Path(__file__).resolve().parent.parent
PLIST = ROOT / "deploy" / "com.variational.lighter-mm.plist"


def _load_config() -> dict:
    with PLIST.open("rb") as stream:
        return plistlib.load(stream)


def test_lighter_mm_plist_can_be_parsed() -> None:
    """防止 plist XML 损坏，导致 launchd 无法加载做市服务。"""
    assert isinstance(_load_config(), dict)


def test_lighter_mm_plist_restarts_crashed_process() -> None:
    """防止进程崩溃后无人重启，造成做市网格静默停摆。"""
    config = _load_config()

    assert config["KeepAlive"] is True
    assert config["RunAtLoad"] is True
    assert config["ThrottleInterval"] == 30


def test_lighter_mm_plist_runs_live_within_inventory_hard_cap() -> None:
    """防止服务误跑 dry-run，或库存参数越过入口模块的资金硬顶。"""
    arguments = _load_config()["ProgramArguments"]

    assert arguments[:3] == [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "tools.run_lighter_mm",
    ]
    assert "--live" in arguments
    max_inventory = float(arguments[arguments.index("--max-inv") + 1])
    assert max_inventory <= run_lighter_mm.MAX_INVENTORY_USD


def test_lighter_mm_plist_exposes_existing_ca_bundle() -> None:
    """防止 launchd 缺少 CA 时出现“读正常、写全挂”的隐蔽 SSL 故障。"""
    environment = _load_config()["EnvironmentVariables"]

    assert environment["PYTHONPATH"] == str(ROOT)
    ssl_cert_file = Path(environment["SSL_CERT_FILE"])
    assert ssl_cert_file.is_absolute()
    assert ssl_cert_file.is_file()


def test_lighter_mm_plist_has_expected_identity_paths_and_logs() -> None:
    """防止服务标签或目录串线，导致加载错实例或日志落到未知位置。"""
    config = _load_config()

    assert config["Label"] == "com.variational.lighter-mm"
    assert config["WorkingDirectory"] == str(ROOT)
    assert config["StandardOutPath"] == str(ROOT / "logs/lighter-mm.out.log")
    assert config["StandardErrorPath"] == str(ROOT / "logs/lighter-mm.err.log")


def test_lighter_mm_plist_arguments_pass_startup_validation(monkeypatch) -> None:
    """防止 plist 参数被入口自检拒绝，造成 launchd 反复崩溃重启。"""
    monkeypatch.setenv("LIGHTER_API_PRIVATE_KEY", "test-only-private-key")
    monkeypatch.setenv("LIGHTER_RH_L1_ADDRESS", "test-only-l1-address")
    arguments = _load_config()["ProgramArguments"]
    parsed = run_lighter_mm.build_parser().parse_args(arguments[3:])

    run_lighter_mm.validate_args(parsed)


def test_readme_lists_lighter_mm_with_status() -> None:
    """做市服务必须出现在系统清单里，且状态标注与真实运行状态一致。

    做市已于 2026-08-23 停用（被定时定量对冲取代，launchd 中已无常驻任务）。
    这条测试原先硬断言「实盘运行中」，停用后反而会把正确的 README 判成错的，
    因此改为断言标注了明确状态——防的是「漏出清单」和「状态没人维护」，
    而不是锁死某一个具体状态。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    system_row = next(
        (line for line in readme.splitlines() if "tools/run_lighter_mm.py" in line),
        "",
    )

    assert "Lighter 做市" in system_row
    assert "Lighter" in system_row
    assert "实盘运行中" in system_row or "已停用" in system_row
