"""只读展示 swap carry 结构切换台账。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SWITCH_HISTORY = PROJECT_ROOT / "data" / "swap_carry_switch_history.jsonl"


def load_switch_history(
    path: Path = DEFAULT_SWITCH_HISTORY,
) -> tuple[list[Mapping[str, Any]], str | None]:
    """读取完整 JSONL；缺失视为空，任一损坏行令结果安全降级。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f"文件读取失败：{exc}"

    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return [], f"第 {line_number} 行不是合法 JSON"
        if not isinstance(payload, Mapping):
            return [], f"第 {line_number} 行不是 JSON 对象"
        records.append(payload)
    return records, None


def _format_timestamp(value: object) -> str:
    """将 ISO 时间转换为简洁 UTC；坏值原样降级。"""
    if not isinstance(value, str) or not value.strip():
        return "无数据"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "无数据"
    if parsed.tzinfo is None:
        return "无数据"
    return parsed.astimezone(timezone.utc).strftime("%m-%d %H:%M:%S UTC")


def _direction(record: Mapping[str, Any]) -> str:
    """格式化结构切换方向。"""
    direction = record.get("direction")
    if not isinstance(direction, Mapping):
        return "未知方向"
    source = str(direction.get("from") or "?")
    target = str(direction.get("to") or "?")
    return f"{source}→{target}"


def _phase_prices(record: Mapping[str, Any], phase_name: str) -> str:
    """按成交顺序格式化某阶段的实际接受报价。"""
    phase = record.get(phase_name)
    legs = phase.get("legs") if isinstance(phase, Mapping) else None
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
        return "无成交"
    values: list[str] = []
    for leg in legs:
        if not isinstance(leg, Mapping) or leg.get("status", "succeeded") != "succeeded":
            continue
        market = str(leg.get("market") or "?")
        price = str(leg.get("execution_price") or "无数据")
        values.append(f"{market}@{price}")
    return "、".join(values) if values else "无成交"


def format_switch_record(record: Mapping[str, Any]) -> str:
    """把单条台账格式化为一行中文摘要。"""
    wear = record.get("measured_wear_usd")
    wear_text = str(wear) if wear is not None else "无数据"
    duration = record.get("total_duration_ms")
    try:
        duration_text = f"{float(duration) / 1000:.3f} 秒"
    except (TypeError, ValueError):
        duration_text = "无数据"
    self_check = record.get("self_check")
    passed = self_check.get("passed") if isinstance(self_check, Mapping) else None
    if passed is True:
        check_text = "自检通过"
    elif passed is False:
        check_text = "自检失败"
    else:
        check_text = "自检未执行"
    return (
        f"{_format_timestamp(record.get('started_at'))} | {_direction(record)} | "
        f"平仓 {_phase_prices(record, 'close_phase')} | "
        f"开仓 {_phase_prices(record, 'open_phase')} | "
        f"实测磨损 {wear_text} USDC | 耗时 {duration_text} | {check_text}"
    )


def build_parser() -> argparse.ArgumentParser:
    """构造只读查询参数。"""
    parser = argparse.ArgumentParser(description="查看 swap carry 结构切换台账")
    parser.add_argument("--last", type=int, default=None, help="仅显示最近 N 条")
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_SWITCH_HISTORY,
        help="切换台账路径",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """读取并打印台账；文件缺失或损坏均正常返回。"""
    args = build_parser().parse_args(argv)
    if args.last is not None and args.last <= 0:
        print("--last 必须是正整数")
        return 2
    records, error = load_switch_history(args.path)
    if error is not None:
        print(f"切换台账不可用（文件损坏）：{error}")
        return 0
    if not records:
        print("尚无切换记录。")
        return 0
    selected = records[-args.last :] if args.last is not None else records
    print("切换时间 | 方向 | 各腿成交价 | 实测磨损 | 耗时 | 自检")
    for record in selected:
        print(format_switch_record(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
