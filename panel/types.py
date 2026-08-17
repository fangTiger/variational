"""面板数据契约。

provider 只负责产出这些结构，render 只负责消费它们。
两边都不知道对方的存在，这样加新系统时渲染器一个字都不用改。
"""

from __future__ import annotations

from dataclasses import dataclass, field

_TONES = ("normal", "good", "warn", "bad")
_LEVELS = ("critical", "warning", "info")


@dataclass(frozen=True)
class Metric:
    """卡片里的一行指标。tone 只影响配色，不影响语义。"""

    label: str
    value: str
    tone: str = "normal"

    def __post_init__(self) -> None:
        if self.tone not in _TONES:
            object.__setattr__(self, "tone", "normal")


@dataclass(frozen=True)
class PanelAlert:
    """一条告警。action 是给人看的动作指引，不可为空。"""

    key: str
    level: str
    title: str
    action: str

    def __post_init__(self) -> None:
        if self.level not in _LEVELS:
            raise ValueError(f"level 必须是 {_LEVELS} 之一，收到 {self.level!r}")
        if not self.action.strip():
            raise ValueError(f"告警 {self.key} 缺少 action 动作指引")


@dataclass(frozen=True)
class SystemStatus:
    """一个系统的快照，对应面板上的一张卡片。"""

    name: str
    alive: bool | None
    summary: str
    metrics: list[Metric] = field(default_factory=list)
    equity: float | None = None
    error: str | None = None
