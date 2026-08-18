"""告警 key → 动作指引。

用户原话：「即使报警了，我也不知道需要做什么」。
所以每条告警都必须回答「现在该干什么」，否则它只是噪音，
还会稀释真正要紧的那几条。
"""

from __future__ import annotations

from panel.types import PanelAlert
from tools.alert_check import Alert

#: 告警 key → 动作指引。新增告警必须同步加一条，否则完整性测试会失败。
ACTIONS: dict[str, str] = {
    "bot_down": "网格引擎不在运行。告诉 Claude 重启，或查看 logs/grid-bot.err.log",
    "halted": "净值回撤熔断已触发，已全平停机。需人工复位——告诉 Claude 处理",
    "grid_blocked": "网格单边被封（价格触及 band 边界），属正常保护，回到区间内会自动恢复",
    "live_missing": "引擎快照文件缺失，可能从未启动过。告诉 Claude 检查",
    "live_stale": "引擎快照长时间未更新，可能卡死。告诉 Claude 检查",
    "monitor_missing": "权益快照文件缺失，不影响交易，但监控数据会断档",
    "monitor_stale": "权益快照服务异常，不影响交易，但监控数据会断档",
    "no_success": "长时间没有成功下单，可能是 API 凭据或余额问题。告诉 Claude 排查",
    "leverage": "杠杆偏离预期，检查是否被手工改过",
    "lighter_hedge_missing": "对冲机器人没有心跳记录，可能从未启动。你现在可能裸敞口——告诉 Claude 启动",
    "lighter_hedge_stale": "对冲机器人失联，你现在可能裸敞口。告诉 Claude 重启，或自己先平掉 Lighter 仓位",
    "lighter_hedge_net_delta": "净敞口持续未收敛，对冲正在失效。告诉 Claude 检查",
    "lighter_hedge_notional_cap": "Lighter 仓位超过上限，机器人拒绝跟单。去 RH Wallet 减仓，或告诉 Claude 调高上限",
    "lighter_hedge_primary_read": "读不到 Lighter 账户，引擎已停止动仓以避免裸腿。检查网络或 API 状态",
    "lighter_hedge_margin": "Extended 对冲腿可用保证金不足。给该账户加钱，或减小两腿仓位",
    "lighter_hedge_invalid": "对冲心跳数据异常（字段缺失或时间戳错乱），机器人可能有 bug。告诉 Claude 排查",
    "lighter_mm_missing": "做市心跳文件存在但没有有效记录，机器人可能启动异常。告诉 Claude 检查日志和进程",
    "lighter_mm_stale": "做市机器人已经失联，现有挂单和库存可能无人管理。告诉 Claude 检查并重启",
    "lighter_mm_failures": "做市循环连续失败，可能无法维护挂单。检查网络、API 凭据和错误日志",
    "lighter_mm_inventory": "做市库存已接近配置上限。检查持仓与挂单，必要时减仓或暂停机器人",
    "attribution_gap": "归因账目对不上，这批收益数字不可信，不要用它做去留判断。告诉 Claude 复核",
    "verdict_stop": "4 周判据结论为停止。告诉 Claude 复核数据后再决定是否真的停",
}

#: 需要立刻处理的告警。其余默认 warning。
_CRITICAL = {
    "bot_down",
    "halted",
    "lighter_hedge_missing",
    "lighter_hedge_stale",
    "lighter_hedge_net_delta",
    "lighter_hedge_notional_cap",
    "lighter_hedge_margin",
    "lighter_mm_missing",
    "lighter_mm_stale",
    "lighter_mm_failures",
}

_UNKNOWN = "未登记的告警类型，告诉 Claude 补充动作指引"


def level_for(key: str) -> str:
    """告警级别。未知 key 一律按 warning，不假装它不重要，也不谎报紧急。"""
    return "critical" if key in _CRITICAL else "warning"


def to_panel_alert(alert: Alert) -> PanelAlert:
    """把 alert_check 的 Alert 翻译成面板用的 PanelAlert。"""
    return PanelAlert(
        key=alert.key,
        level=level_for(alert.key),
        title=alert.title,
        action=ACTIONS.get(alert.key, _UNKNOWN),
    )
