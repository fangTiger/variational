"""测试全局配置：在测试模块导入前隔离文件日志。

测试假订单曾污染真实 ``grid_engine`` 日志，使事故统计从真实 15 笔偏成
79 笔（约 5 倍），并直接导致整版修复计划作废。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import infra.logger as logger_config


# pytest 此时尚未导入测试模块；必须在 collection 触发 grid_engine 的模块级
# get_logger() 之前改目录，否则 FileHandler 已打开真实日志，函数级 fixture 来不及。
# 顶层拿不到 tmp_path，因此用 mkdtemp 为本次 pytest 进程保留独立日志证据。
_TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="variational-test-logs-"))
logger_config._LOG_DIR = _TEST_LOG_DIR
