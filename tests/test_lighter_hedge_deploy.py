"""Lighter 对冲 launchd 常驻配置与运维文档测试。"""

from __future__ import annotations

import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLIST = ROOT / "deploy" / "com.variational.lighter-hedge.plist"


def test_lighter_hedge_plist_is_safe_keepalive_service() -> None:
    """仓库内 plist 必须满足常驻、隔离日志和项目路径约束。"""
    assert PLIST.exists()
    with PLIST.open("rb") as stream:
        config = plistlib.load(stream)

    assert config["Label"] == "com.variational.lighter-hedge"
    assert config["KeepAlive"] is True
    assert config["RunAtLoad"] is True
    assert config["ThrottleInterval"] == 30
    assert config["WorkingDirectory"] == str(ROOT)
    assert config["EnvironmentVariables"]["PYTHONPATH"] == str(ROOT)
    assert config["StandardOutPath"] == str(ROOT / "logs/lighter-hedge.out.log")
    assert config["StandardErrorPath"] == str(ROOT / "logs/lighter-hedge.err.log")
    assert config["ProgramArguments"][:4] == [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "tools.run_lighter_hedge",
        "--live",
    ]


def test_lighter_hedge_plist_enables_15_second_maker_first_execution() -> None:
    """实盘对冲必须先被动挂单 15 秒，再按剩余量吃单补齐。"""
    with PLIST.open("rb") as stream:
        config = plistlib.load(stream)

    arguments = config["ProgramArguments"]
    option_index = arguments.index("--maker-first-timeout")
    assert arguments[option_index + 1] == "15"


def test_readme_documents_lighter_hedge_service_and_install_source() -> None:
    """README 必须说明服务入口及从仓库 plist 人工安装。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "com.variational.lighter-hedge" in readme
    assert "deploy/com.variational.lighter-hedge.plist" in readme
    assert "tools.run_lighter_hedge --live" in readme
