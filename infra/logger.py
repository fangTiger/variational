"""统一日志工具：中文日志，输出到 log/ 目录。"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "log"


def get_logger(name: str) -> logging.Logger:
    """获取带文件与控制台输出的 logger。

    日志文件命名：log/{name}_{日期}.log
    """
    logger = logging.getLogger(name)
    if logger.handlers:  # 避免重复添加 handler
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    _LOG_DIR.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(
        _LOG_DIR / f"{name}_{date.today():%Y%m%d}.log", encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger
