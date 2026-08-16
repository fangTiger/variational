"""成交明细追写。

引擎侧只负责 append 一行纯文本，这是能想到的最低风险写入方式：
不引入数据库依赖、不占锁、失败也只丢一条统计数据。
闭环配对由旁路进程离线做（见 grid/attribution/pairing.py），
因为引擎内的配对依赖内存态 _loop_fills，跨重启必然断裂。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from infra.logger import get_logger

logger = get_logger("fill_log")


def build_fill_record(
    *,
    fill_id: str,
    ts: float,
    level: int,
    side: str,
    price: Decimal,
    qty: Decimal,
    engine_run_id: str,
) -> dict:
    """构造一条成交记录。Decimal 转 float 便于 JSON 序列化。"""
    return {
        "fill_id": str(fill_id),
        "ts": float(ts),
        "level": int(level),
        "side": str(side),
        "price": float(price),
        "qty": float(qty),
        "engine_run_id": str(engine_run_id),
    }


def append_fill(path: Path, record: dict) -> None:
    """追加一行。任何异常只记日志，绝不向上抛——交易循环优先。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("成交追写失败（不影响交易）：%s", exc)
