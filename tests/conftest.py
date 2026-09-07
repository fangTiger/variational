"""测试全局配置：在测试模块导入前隔离文件日志。

测试假订单曾污染真实 ``grid_engine`` 日志，使事故统计从真实 15 笔偏成
79 笔（约 5 倍），并直接导致整版修复计划作废。
"""

from __future__ import annotations

import socket
import tempfile

import pytest
from pathlib import Path

import infra.logger as logger_config


# pytest 此时尚未导入测试模块；必须在 collection 触发 grid_engine 的模块级
# get_logger() 之前改目录，否则 FileHandler 已打开真实日志，函数级 fixture 来不及。
# 顶层拿不到 tmp_path，因此用 mkdtemp 为本次 pytest 进程保留独立日志证据。
_TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="variational-test-logs-"))
logger_config._LOG_DIR = _TEST_LOG_DIR


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
