"""资金费推荐方向的轻量持久化状态。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from infra.logger import get_logger

logger = get_logger("funding_direction_state")

DEFAULT_DIRECTION_STATE_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "funding_direction_state.json"
)
_VALID_DIRECTIONS = {"short_variational", "long_variational"}


class FundingDirectionStateStore:
    """按交易场所与市场隔离保存最近一次推荐方向。"""

    def __init__(self, path: str | Path = DEFAULT_DIRECTION_STATE_FILE) -> None:
        self.path = Path(path)

    @staticmethod
    def _keys(venue: str, market: str) -> tuple[str, str]:
        """统一状态键格式，避免大小写造成重复分区。"""
        return venue.strip().lower(), market.strip().upper()

    def _read(self) -> dict[str, Any]:
        """读取完整状态；缺失视为空，损坏时告警并降级。"""
        if not self.path.exists():
            return {"directions": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("顶层不是 JSON 对象")
            directions = payload.get("directions")
            if directions is None:
                payload["directions"] = {}
            elif not isinstance(directions, dict):
                raise ValueError("directions 不是 JSON 对象")
            return payload
        except Exception as exc:  # noqa: BLE001 状态损坏不能阻断监控主流程
            logger.warning("读取资金方向状态失败，按无历史方向降级：%s", exc)
            return {"directions": {}}

    def load(self, venue: str, market: str) -> str | None:
        """读取指定 ``(venue, market)`` 的最近方向。"""
        venue_key, market_key = self._keys(venue, market)
        payload = self._read()
        venue_state = payload["directions"].get(venue_key)
        if not isinstance(venue_state, dict):
            return None
        direction = venue_state.get(market_key)
        if direction not in _VALID_DIRECTIONS:
            if direction is not None:
                logger.warning(
                    "资金方向状态值无效，按无历史方向降级：venue=%s market=%s value=%r",
                    venue_key,
                    market_key,
                    direction,
                )
            return None
        return direction

    def save(self, venue: str, market: str, direction: str) -> None:
        """原子写入方向；任何写入失败都只告警，不向调用方抛错。"""
        venue_key, market_key = self._keys(venue, market)
        temporary_path: Path | None = None
        try:
            if direction not in _VALID_DIRECTIONS:
                raise ValueError(f"未知方向：{direction!r}")
            payload = self._read()
            directions = payload["directions"]
            venue_state = directions.setdefault(venue_key, {})
            if not isinstance(venue_state, dict):
                venue_state = {}
                directions[venue_key] = venue_state
            venue_state[market_key] = direction

            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
            os.replace(temporary_path, self.path)
            temporary_path = None
        except Exception as exc:  # noqa: BLE001 状态写失败不能阻断监控主流程
            logger.warning("写入资金方向状态失败，已跳过持久化：%s", exc)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
