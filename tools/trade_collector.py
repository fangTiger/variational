"""公开成交逐笔采集器（只读）。

订阅 Extended 公开成交 WebSocket 流，逐笔落盘为 JSONL，供 analyze_alpha 离线分析。
不碰账户、不下单、不 import grid_engine——它挂掉不影响实盘，实盘挂了它照采。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.trade_collector
"""
from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import ssl  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import certifi  # noqa: E402
import websockets  # noqa: E402

logger = logging.getLogger("trade_collector")

WS_URL = (
    "wss://api.starknet.extended.exchange/stream.extended.exchange"
    "/v1/publicTrades/BTC-USD"
)
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "trades"

# 相邻成交间隔超过此值即视为可能丢数据，写缺口标记。
# 实测正常成交间隔中位 0.196s、最大 23.8s，取 60s 留足余量。
DEFAULT_MAX_GAP_MS = 60_000

# 重连退避上限
MAX_BACKOFF_SECONDS = 60.0


def parse_trades(msg: dict) -> list[dict]:
    """从 WebSocket 消息里抽取成交行，字段缺失或无法解析的行跳过。

    单行坏数据不应让整条消息作废——丢一笔比丢一批好。
    """
    rows: list[dict] = []
    for item in (msg.get("data") or []):
        try:
            rows.append({
                "i": int(item["i"]),
                "T": int(item["T"]),
                "p": float(item["p"]),
                "q": float(item["q"]),
                "S": str(item["S"]),
            })
        except (KeyError, TypeError, ValueError):
            logger.warning("跳过结构异常的成交行：%s", item)
    return rows


class TradeBuffer:
    """按交易所 id 去重，并检测时间缺口。"""

    def __init__(self, max_gap_ms: int = DEFAULT_MAX_GAP_MS) -> None:
        self._seen: set[int] = set()
        self._last_ts: int | None = None
        self._max_gap_ms = max_gap_ms

    def add(self, rows: list[dict]) -> list[dict]:
        """返回其中未见过的行，并推进最新时间戳。"""
        fresh = []
        for row in rows:
            if row["i"] in self._seen:
                continue
            self._seen.add(row["i"])
            fresh.append(row)
            if self._last_ts is None or row["T"] > self._last_ts:
                self._last_ts = row["T"]
        return fresh

    def gap_since(self, ts_ms: int) -> tuple[int, int] | None:
        """若 ts_ms 距已见过的最大时间戳超过阈值，返回 (起, 止)，否则 None。

        基于最大时间戳而非最后一条，避免乱序到达时误判。
        """
        if self._last_ts is None:
            return None
        if ts_ms - self._last_ts > self._max_gap_ms:
            return (self._last_ts, ts_ms)
        return None


def _out_path(ts_ms: int) -> Path:
    """按交易所时间戳（UTC）切分日文件。"""
    day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return OUT_DIR / f"{day}.jsonl"


def _append(rows: list[dict]) -> None:
    """按天分组落盘。"""
    if not rows:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    by_day: dict[Path, list[dict]] = {}
    for row in rows:
        by_day.setdefault(_out_path(row["T"]), []).append(row)
    for path, chunk in by_day.items():
        with path.open("a", encoding="utf-8") as f:
            for row in chunk:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _append_gap(start_ms: int, end_ms: int) -> None:
    """写缺口标记。

    分析脚本遇到它必须截断该段而非跨越拼接——跨越缺口会凭空制造一次
    巨大的价格跳变，直接污染标度指数。宁可多标不可漏标。
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _out_path(end_ms)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"gap": True, "from": start_ms, "to": end_ms}) + "\n")
    logger.warning("检测到数据缺口 %d → %d（%.1f 秒）",
                   start_ms, end_ms, (end_ms - start_ms) / 1000)


async def run_forever() -> None:
    """长连接采集主循环，断线指数退避重连。"""
    buf = TradeBuffer()
    ctx = ssl.create_default_context(cafile=certifi.where())
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(
                WS_URL,
                additional_headers={"User-Agent": "variational-grid-research/1.0"},
                ssl=ctx,
                proxy=None,  # 本机有代理环境变量，须显式直连
            ) as ws:
                logger.info("已连接公开成交流")
                backoff = 1.0
                async for raw in ws:
                    rows = parse_trades(json.loads(raw))
                    if not rows:
                        continue
                    newest = max(r["T"] for r in rows)
                    gap = buf.gap_since(newest)
                    if gap is not None:
                        _append_gap(*gap)
                    _append(buf.add(rows))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("连接中断，%.0f 秒后重连：%s", backoff, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
