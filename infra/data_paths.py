"""数据目录配置；测试在模块导入前设置独立目录，生产默认保持不变。"""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """读取可注入的数据根目录，供各入口构造默认路径。"""
    configured = os.environ.get("VARIATIONAL_DATA_DIR")
    return Path(configured) if configured else Path(__file__).resolve().parents[1] / "data"
