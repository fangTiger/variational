"""网格趋势感知状态的持久化（原子读写）。

launchd 会定期重启进程，band 边界与冻结/急停状态必须落盘，
否则重启后按现价重开网、绕过突破保护（见设计 v3 Component 2）。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class GridState:
    band_low: float
    band_high: float
    frozen: bool
    blocked_side: str | None  # "BUY" / "SELL" / None
    halted: bool


def load_state(path: str | Path) -> GridState | None:
    """读回状态；文件缺失或损坏返回 None（调用方据此走 fail-closed）。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return GridState(**d)
    except Exception:  # noqa: BLE001  损坏当作无状态
        return None


def save_state(path: str | Path, state: GridState) -> None:
    """原子写：先写临时文件再 rename，避免半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
