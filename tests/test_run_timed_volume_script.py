"""定时对冲统一启动脚本的离线行为测试。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def _install_script_fixture(tmp_path: Path) -> Path:
    """复制真实脚本，并用只记录参数的项目虚拟环境解释器替身运行。"""
    script_dir = tmp_path / "scripts"
    python_path = tmp_path / ".venv" / "bin" / "python"
    script_dir.mkdir(parents=True)
    python_path.parent.mkdir(parents=True)
    script_path = script_dir / "run_timed_volume.sh"
    shutil.copy2(ROOT / "scripts" / "run_timed_volume.sh", script_path)
    python_path.write_text(
        """#!/bin/bash
for proxy_name in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy; do
    if [ "${!proxy_name+x}" = "x" ]; then
        printf 'PROXY=%s\n' "$proxy_name"
    fi
done
if IFS= read -r unexpected_input; then
    printf 'STDIN=%s\n' "$unexpected_input"
else
    printf 'STDIN=EOF\n'
fi
printf 'PYTHONPATH=%s\n' "${PYTHONPATH:-}"
printf 'ARG=%s\n' "$@"
printf 'STDERR=已重定向\n' >&2
""",
        encoding="utf-8",
    )
    python_path.chmod(0o755)
    return script_path


@pytest.mark.parametrize(
    ("instance", "log_name", "expected_args"),
    [
        (
            "sndk",
            "timed_volume_sndk.log",
            [
                "-m",
                "tools.run_timed_volume",
                "--live",
                "--primary-venue",
                "variational",
                "--market",
                "SNDK",
                "--hedge-venue",
                "hyperliquid",
                "--hedge-env-prefix",
                "HYPERLIQUID_VAR",
                "--hedge-market",
                "io:SNDK",
                "--notional-min",
                "1500",
                "--notional-max",
                "2000",
                "--cycle-hours",
                "8",
                "--initial-direction",
                "long",
                "--maker-timeout",
                "30",
                "--poll-interval",
                "10",
                "--basis-gate-sigma",
                "1.5",
                "--basis-gate-max-wait",
                "1800",
                "--state-path",
                "data/timed_volume_sndk/state.json",
                "--heartbeat-path",
                "data/timed_volume_sndk.jsonl",
                "--equity-path",
                "data/timed_volume_sndk_equity.jsonl",
                "--ledger-path",
                "data/timed_volume_sndk_ledger.jsonl",
            ],
        ),
        (
            "btc",
            "timed_volume_btc.log",
            [
                "-m",
                "tools.run_timed_volume",
                "--live",
                "--primary-venue",
                "lighter",
                "--market",
                "BTC",
                "--hedge-venue",
                "variational",
                "--hedge-market",
                "BTC",
                "--notional-min",
                "3500",
                "--notional-max",
                "4000",
                "--cycle-hours",
                "8",
                "--initial-direction",
                "long",
                "--maker-timeout",
                "300",
                "--poll-interval",
                "30",
                "--basis-gate-sigma",
                "0",
                "--state-path",
                "data/timed_volume_btc/state.json",
                "--heartbeat-path",
                "data/timed_volume_btc.jsonl",
                "--equity-path",
                "data/timed_volume_btc_equity.jsonl",
                "--ledger-path",
                "data/timed_volume_btc_ledger.jsonl",
            ],
        ),
    ],
)
def test_script_strips_proxies_and_uses_exact_instance_arguments(
    tmp_path,
    instance,
    log_name,
    expected_args,
) -> None:
    """脚本必须用项目解释器、原始参数、空 stdin 和 log/ 重定向启动。"""
    script_path = _install_script_fixture(tmp_path)
    environment = os.environ.copy()
    environment.update(
        {name: "http://127.0.0.1:10808" for name in PROXY_ENV_NAMES}
    )

    completed = subprocess.run(
        ["/bin/bash", str(script_path), instance],
        cwd=tmp_path,
        env=environment,
        input="不应传入子进程\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    output = (tmp_path / "log" / log_name).read_text(encoding="utf-8")
    assert "PROXY=" not in output
    assert "STDIN=EOF" in output
    assert "PYTHONPATH=." in output
    assert "STDERR=已重定向" in output
    assert [
        line.removeprefix("ARG=")
        for line in output.splitlines()
        if line.startswith("ARG=")
    ] == expected_args


def test_script_self_check_stops_when_env_fails_to_remove_proxy(tmp_path) -> None:
    """模拟剥离后仍有代理时，自检必须在下单进程前报错退出。"""
    script_path = _install_script_fixture(tmp_path)
    environment = os.environ.copy()
    environment["HTTP_PROXY"] = "http://127.0.0.1:10808"

    completed = subprocess.run(
        ["/bin/bash", str(script_path), "--proxy-env-clean", "sndk"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode != 0
    assert "代理变量剥离自检失败" in completed.stderr
    assert not (tmp_path / "log" / "timed_volume_sndk.log").exists()


def test_path_wrappers_cannot_bypass_proxy_stripping_or_self_check(tmp_path) -> None:
    """PATH 同时伪造 env 与 printenv 时，实盘解释器仍不得继承代理。"""
    script_path = _install_script_fixture(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_env = fake_bin / "env"
    fake_env.write_text(
        """#!/bin/bash
while [ "${1:-}" = "-u" ]; do
    shift 2
done
exec "$@"
""",
        encoding="utf-8",
    )
    fake_env.chmod(0o755)
    fake_printenv = fake_bin / "printenv"
    fake_printenv.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    fake_printenv.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["HTTP_PROXY"] = "http://127.0.0.1:10808"

    completed = subprocess.run(
        ["/bin/bash", str(script_path), "sndk"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    output = (tmp_path / "log" / "timed_volume_sndk.log").read_text(
        encoding="utf-8"
    )
    assert "PROXY=" not in output
