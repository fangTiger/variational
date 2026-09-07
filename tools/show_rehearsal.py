"""只读打印结构切换预演台账，不加载交易客户端。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.show_switch_history import DEFAULT_SWITCH_HISTORY, load_switch_history


LABELS = {
    "timestamp": "预演时间", "window_id": "切换窗口", "planned_at": "计划切换时间",
    "direction": "切换方向", "trigger": "触发原因", "blocking_reasons": "阻断原因",
    "warnings": "注意事项", "before": "当前各腿数量、名义与净 delta",
    "close_legs": "平仓侧预计成交价与滑点（bp）", "open_legs": "开仓侧数量、名义与保证金",
    "margin": "可用保证金检查", "markets": "可交易状态与距关市秒数",
    "funding_rates": "各腿年化费率", "net_carry_annual": "预计新结构净 carry 年化",
    "estimated_duration_ms": "预计总耗时（毫秒）", "duration_basis": "耗时估计依据",
}


def main(argv=None):
    """按追加顺序显示最近 N 次预演和全部检查明细。"""
    parser = argparse.ArgumentParser(description="查看结构切换预演结论与检查明细")
    parser.add_argument("--last", type=int, default=5, help="显示最近 N 次，默认 5")
    parser.add_argument("--path", type=Path, default=DEFAULT_SWITCH_HISTORY, help="切换台账路径")
    args = parser.parse_args(argv)
    if args.last <= 0:
        print("--last 必须是正整数")
        return 2
    records, error = load_switch_history(args.path)
    if error:
        print(f"预演台账不可用：{error}")
        return 1
    records = [r for r in records if r.get("kind") == "rehearsal"]
    if not records:
        print("尚无预演记录。")
    for record in records[-args.last:]:
        print(f"预演结论：{record.get('conclusion', '未知')}")
        for key, label in LABELS.items():
            if key in record:
                value = record[key]
                print(f"  {label}：{json.dumps(value, ensure_ascii=False) if value is not None else '无估计 / 无数据'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
