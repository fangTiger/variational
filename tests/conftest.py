"""测试全局配置：在测试模块导入前隔离文件日志。

测试假订单曾污染真实 ``grid_engine`` 日志，使事故统计从真实 15 笔偏成
79 笔（约 5 倍），并直接导致整版修复计划作废。
"""

from __future__ import annotations

import socket
import hashlib
from datetime import datetime, timezone
import os
import sys
import tempfile

import pytest
from pathlib import Path

import infra.logger as logger_config


# pytest 此时尚未导入测试模块；必须在 collection 触发 grid_engine 的模块级
# get_logger() 之前改目录，否则 FileHandler 已打开真实日志，函数级 fixture 来不及。
# 顶层拿不到 tmp_path，因此用 mkdtemp 为本次 pytest 进程保留独立日志证据。
_TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="variational-test-logs-"))
logger_config._LOG_DIR = _TEST_LOG_DIR


# 导入测试模块之前设置，连函数定义时绑定的默认路径也只能落到临时目录。
_REAL_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="variational-test-data-"))
os.environ["VARIATIONAL_DATA_DIR"] = str(_TEST_DATA_DIR)


def _data_snapshot(root):
    """同时比较文件集合、纳秒修改时间与内容，捕获新增、删除和同内容重写。"""
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns,
                                      hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*") if path.is_file()
    }


_DATA_BEFORE = _data_snapshot(_REAL_DATA_DIR)
_DATA_WRITE_ATTEMPTS = []
# 这些流由正在运行的独立生产进程持续追加，不属于本次状态快照断言范围。
# 测试进程对它们的写入仍受下面的审计钩子严格禁止。
_LIVE_OUTPUT_PATHS = {
    "timed_volume_btc.jsonl",
    "timed_volume_sndk_xyz.jsonl",
    f"trades/{datetime.now(timezone.utc):%Y-%m-%d}.jsonl",
}


def _assert_data_unchanged(before, after):
    """纯比较函数也用于保护机制自身的回归测试。"""
    changed = sorted(key for key in before.keys() | after.keys()
                     if before.get(key) != after.get(key))
    assert not changed, f"真实数据文件发生变化：{changed}"


def _protect_real_data(event, args):
    """审计钩子覆盖内置 open、Path、os.open 及原子替换，误写在发生前失败。"""
    paths = ()
    if event == "open":
        path, mode, flags = args
        if ((isinstance(mode, str) and any(char in mode for char in "wax+"))
                or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            paths = ((path, None),)
    elif event == "sqlite3.connect":
        paths = ((args[0], None),)
    elif event in {"os.rename", "os.link"}:
        paths = ((args[0], args[2]), (args[1], args[3]))
    elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
        index = {"os.remove": 1, "os.rmdir": 1, "os.mkdir": 2, "os.chmod": 2, "os.utime": 3}.get(event)
        paths = ((args[0], args[index] if index is not None else None),)
    elif event == "os.symlink":
        paths = ((args[1], args[2]),)
    for raw, directory_fd in paths:
        if not isinstance(raw, (str, bytes, os.PathLike)):
            continue
        path = Path(os.fsdecode(raw))
        if not path.is_absolute() and directory_fd is not None and directory_fd >= 0:
            # shutil.rmtree 使用 dir_fd，相对名称不能按当前工作目录解析。
            if sys.platform == "darwin":
                import fcntl
                directory = os.fsdecode(fcntl.fcntl(directory_fd, 50, bytes(1024)).split(b"\0", 1)[0])
            else:
                directory = os.readlink(f"/proc/self/fd/{directory_fd}")
            path = Path(directory) / path
        path = path.resolve()
        if event == "os.mkdir" and path.is_dir():
            continue
        if path == _REAL_DATA_DIR or _REAL_DATA_DIR in path.parents:
            import traceback
            callers = [f"{frame.filename}:{frame.lineno} {frame.name}"
                       for frame in traceback.extract_stack()
                       if "/tests/" in frame.filename and frame.name != "_protect_real_data"]
            message = f"测试禁止修改真实数据：{event} {path}；调用：{callers}"
            _DATA_WRITE_ATTEMPTS.append(message)
            raise AssertionError(message)


sys.addaudithook(_protect_real_data)


@pytest.fixture(autouse=True)
def _isolate_default_data_paths(tmp_path, monkeypatch):
    """已导入模块的默认路径按用例隔离；漏传参数也不能读取生产状态。"""
    monkeypatch.setenv("VARIATIONAL_DATA_DIR", str(tmp_path))
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith(("tools.", "tracking.", "panel.", "infra.")):
            continue
        for key, value in list(vars(module).items()):
            if not isinstance(value, Path):
                continue
            for root in (_REAL_DATA_DIR, _TEST_DATA_DIR):
                if value == root or root in value.parents:
                    monkeypatch.setattr(module, key, tmp_path / value.relative_to(root))
                    break


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """所有测试及 fixture 拆卸完毕后检查，避免普通测试执行顺序留下盲区。"""
    after = _data_snapshot(_REAL_DATA_DIR)
    changed = sorted(key for key in _DATA_BEFORE.keys() | after.keys()
                     if _DATA_BEFORE.get(key) != after.get(key))
    protected_before = {key: value for key, value in _DATA_BEFORE.items()
                        if key not in _LIVE_OUTPUT_PATHS}
    protected_after = {key: value for key, value in after.items()
                       if key not in _LIVE_OUTPUT_PATHS}
    try:
        _assert_data_unchanged(protected_before, protected_after)
    except AssertionError:
        snapshot_failed = True
    else:
        snapshot_failed = False
    if snapshot_failed or _DATA_WRITE_ATTEMPTS:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter:
            reporter.write_sep("!", "真实 data/ 保护失败")
            reporter.write_line(f"变化文件：{changed}；被阻止的写入：{_DATA_WRITE_ATTEMPTS}")
    else:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter:
            reporter.write_line("真实 data/ 保护通过：受检文件集合、mtime、SHA-256 均未改变，测试未尝试写入真实目录。")
            if changed:
                reporter.write_line(f"独立生产流发生更新（不纳入静态快照断言）：{changed}")


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """所有测试禁止真实网络；保留显式假传输及本地事件循环套接字。"""
    def blocked(*args, **kwargs):
        raise AssertionError("离线测试禁止真实网络请求")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    # libcurl 绕过 Python socket，必须单独封锁其真实传输入口。
    from curl_cffi import Curl, AsyncCurl
    monkeypatch.setattr(Curl, "perform", blocked)
    monkeypatch.setattr(AsyncCurl, "add_handle", blocked)
