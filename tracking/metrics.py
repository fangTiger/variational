"""阶段 A 收益实测统计。

定期记录快照（名义仓位、累计积分、累计净资金费、累计磨损），
据此算出阶段 B 决策需要的三项核心指标：
  1. 每千美元每周积分（points_per_1k_per_week）
  2. 年化资金费率（annualized_funding_pct）
  3. 周净盈亏（net_pnl_usd = 净资金费 - 磨损）

快照持久化为 JSONL，便于跨重启累计。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_FILE = _DATA_DIR / "metrics.jsonl"

# 一年的秒数，用于年化
_SECONDS_PER_YEAR = Decimal(365 * 24 * 3600)
_SECONDS_PER_WEEK = Decimal(7 * 24 * 3600)


@dataclass
class Snapshot:
    """某时刻的累计状态快照。

    ts: 时间戳（epoch 秒）
    notional_usd: 当前 primary 腿名义仓位（积分计价基准）
    points_total: 账户累计积分（来自 /points/summary）
    net_funding_usd: 累计净资金费（>0 净收，<0 净付）
    wear_usd: 累计磨损（点差+手续费，>0）
    """

    ts: float
    notional_usd: float
    points_total: float
    net_funding_usd: float
    wear_usd: float


@dataclass
class MetricsSummary:
    """窗口内派生指标。"""

    span_seconds: float
    avg_notional_usd: Decimal
    points_gained: Decimal
    points_per_1k_per_week: Decimal
    net_funding_usd: Decimal
    annualized_funding_pct: Decimal
    wear_usd: Decimal
    net_pnl_usd: Decimal

    def pretty(self) -> str:
        days = self.span_seconds / 86400
        return (
            f"观测 {days:.1f} 天 | 均名义 ${self.avg_notional_usd:.0f}\n"
            f"  积分：+{self.points_gained:.4f}（{self.points_per_1k_per_week:.4f} 点/千U/周）\n"
            f"  资金费：净 ${self.net_funding_usd:+.2f}（年化 {self.annualized_funding_pct:+.2f}%）\n"
            f"  磨损：${self.wear_usd:.2f} | 周净盈亏：${self.net_pnl_usd:+.2f}"
        )


class MetricsTracker:
    """快照记录 + 派生指标计算。"""

    def __init__(self, path: str | Path = _DEFAULT_FILE) -> None:
        self.path = Path(path)
        self._snapshots: list[Snapshot] = []
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                self._snapshots.append(Snapshot(**json.loads(line)))

    def record(self, snap: Snapshot) -> None:
        """追加一条快照并持久化。"""
        self._snapshots.append(snap)
        self.path.parent.mkdir(exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(snap), ensure_ascii=False) + "\n")

    @property
    def snapshots(self) -> list[Snapshot]:
        return list(self._snapshots)

    def summary(self, *, window_seconds: float | None = None) -> MetricsSummary | None:
        """计算窗口内派生指标；快照不足 2 条返回 None。

        window_seconds=None 表示用全部历史；否则只取最近该时长内的快照。
        """
        if len(self._snapshots) < 2:
            return None

        snaps = self._snapshots
        if window_seconds is not None:
            cutoff = snaps[-1].ts - window_seconds
            snaps = [s for s in snaps if s.ts >= cutoff]
            if len(snaps) < 2:
                snaps = self._snapshots[-2:]

        first, last = snaps[0], snaps[-1]
        span = Decimal(str(last.ts - first.ts))
        if span <= 0:
            return None

        # 均名义仓位（简单按端点平均，够阶段 A 用）
        avg_notional = (
            Decimal(str(first.notional_usd)) + Decimal(str(last.notional_usd))
        ) / 2

        points_gained = Decimal(str(last.points_total)) - Decimal(str(first.points_total))
        net_funding = Decimal(str(last.net_funding_usd)) - Decimal(str(first.net_funding_usd))
        wear = Decimal(str(last.wear_usd)) - Decimal(str(first.wear_usd))

        # 每千美元每周积分
        if avg_notional > 0:
            points_per_1k_week = (
                points_gained / (avg_notional / 1000) * (_SECONDS_PER_WEEK / span)
            )
            annualized_funding = (
                net_funding / avg_notional * (_SECONDS_PER_YEAR / span) * 100
            )
        else:
            points_per_1k_week = Decimal(0)
            annualized_funding = Decimal(0)

        return MetricsSummary(
            span_seconds=float(span),
            avg_notional_usd=avg_notional,
            points_gained=points_gained,
            points_per_1k_per_week=points_per_1k_week,
            net_funding_usd=net_funding,
            annualized_funding_pct=annualized_funding,
            wear_usd=wear,
            net_pnl_usd=net_funding - wear,
        )
