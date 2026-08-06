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
from collections import deque  # noqa: E402
from collections.abc import Iterable  # noqa: E402
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

# 连接正常但长时间无数据时视为可能静默卡死，写缺口标记。
# 实测正常成交间隔中位 0.196s、最大 23.8s，取 60s 留足余量。
DEFAULT_MAX_GAP_MS = 60_000

# 重连退避上限
MAX_BACKOFF_SECONDS = 60.0

# 去重窗口上限。交易所不会重推很久以前的成交，5 万条约覆盖 11 小时，
# 远超实际需要；无界 set 跑 30 天会累积到 100MB+。
MAX_SEEN_IDS = 50_000

# 重启后从磁盘回灌的 id 数量。实测流在连上时会补发约 50 笔 / 57 秒的
# backlog，取 1000 留 20 倍余量。
RECOVER_SEEN_LIMIT = 1000


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
    """按交易所 id 去重，并检测数据缺口。

    缺口有三个来源，前两个是无歧义信号，第三个是启发式：
      1. 断线重连——连接断过，无法证明期间没丢数据
      2. 进程重启——我们根本不在运行
      3. 连接正常但长时间无数据——可能是静默卡死，也可能只是市场清淡

    前两者由调用方显式 mark_discontinuity() 告知，第三者用时间阈值兜底。
    """

    def __init__(self, max_gap_ms: int = DEFAULT_MAX_GAP_MS,
                 last_ts: int | None = None,
                 seen_ids: Iterable[int] = ()) -> None:
        self._seen: set[int] = set(seen_ids)
        self._order: deque[int] = deque(self._seen)
        self._last_ts = last_ts
        self._max_gap_ms = max_gap_ms
        self._pending_break = False

    def mark_discontinuity(self) -> None:
        """标记此后第一批数据之前存在不可证明的中断（重连/重启）。"""
        self._pending_break = True

    def clear_discontinuity(self) -> None:
        """已为该中断写过标记，复位。"""
        self._pending_break = False

    def add(self, rows: list[dict]) -> list[dict]:
        """返回其中未见过的行，并推进最新时间戳。去重窗口有界。"""
        fresh = []
        for row in rows:
            if row["i"] in self._seen:
                continue
            self._seen.add(row["i"])
            self._order.append(row["i"])
            if len(self._order) > MAX_SEEN_IDS:
                self._seen.discard(self._order.popleft())
            fresh.append(row)
            if self._last_ts is None or row["T"] > self._last_ts:
                self._last_ts = row["T"]
        return fresh

    def gap_since(self, ts_ms: int) -> tuple[int, int] | None:
        """判断 ts_ms 之前是否存在数据缺口。

        没有前序数据时返回 None——无从谈起缺口，不是漏判。
        """
        if self._last_ts is None:
            return None
        if self._pending_break or ts_ms - self._last_ts > self._max_gap_ms:
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


def recover_last_ts(out_dir: Path = OUT_DIR) -> int | None:
    """从已落盘文件末尾恢复最后一笔成交的时间戳。

    进程重启后必须接上停机前的时间线，否则停机期无法被标记成缺口。
    跳过 gap 标记行，只取真实成交。
    """
    if not out_dir.exists():
        return None
    for path in sorted(out_dir.glob("*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not row.get("gap") and "T" in row:
                return int(row["T"])
    return None


def recover_recent_ids(out_dir: Path = OUT_DIR,
                        limit: int = RECOVER_SEEN_LIMIT) -> list[int]:
    """从已落盘文件末尾恢复最近若干成交 id，用于重启后对 backlog 去重。

    公开成交流在连上时会补发一小段历史（实测约 50 笔 / 57 秒）。若不把
    这些 id 回灌进去重集合，重启后这批已落盘的成交会被重复写入，抬高
    下游的振荡计数。

    Returns:
        按时间正序排列的 id 列表；无历史数据时返回空列表
    """
    if not out_dir.exists():
        return []
    ids: list[int] = []
    for path in sorted(out_dir.glob("*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("gap") or "i" not in row:
                continue
            ids.append(int(row["i"]))
            if len(ids) >= limit:
                return list(reversed(ids))
    return list(reversed(ids))


async def run_forever() -> None:
    """长连接采集主循环，断线指数退避重连。"""
    last_ts = recover_last_ts()
    buf = TradeBuffer(last_ts=last_ts, seen_ids=recover_recent_ids())
    if last_ts is not None:
        # 进程此前不在运行，这段空窗无法证明没丢数据
        buf.mark_discontinuity()
        logger.info("从磁盘恢复 last_ts=%d、%d 个历史 id，已标记启动断点",
                    last_ts, len(buf._seen))

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
                got_message = False
                async for raw in ws:
                    if not got_message:
                        # 收到首条消息才认为连接真的可用。若在建连瞬间就重置，
                        # 服务端"握手后立刻断开"会导致 1 秒一次的重连风暴。
                        got_message = True
                        backoff = 1.0
                    rows = parse_trades(json.loads(raw))
                    if not rows:
                        continue
                    newest = max(r["T"] for r in rows)
                    gap = buf.gap_since(newest)
                    if gap is not None:
                        _append_gap(*gap)
                    buf.clear_discontinuity()
                    _append(buf.add(rows))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("连接中断，%.0f 秒后重连：%s", backoff, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        finally:
            # 无论正常关闭还是异常断开，连接都已中断，
            # 下一批数据之前存在不可证明的空窗。
            buf.mark_discontinuity()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
