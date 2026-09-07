"""只读展示 XAUS 资金费对账历史。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.data_paths import data_dir
from tools.swap_carry_funding_recon import read_history


def main(argv=None):
    """输出中文表格，费率为小数；不连接交易所，也不创建文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=data_dir()/'swap_carry_funding_recon.jsonl')
    parser.add_argument('--last', type=int, default=20, help='最近 N 条')
    args = parser.parse_args(argv)
    if args.last <= 0:
        parser.error('--last 必须为正整数')
    history = sorted(read_history(args.path), key=lambda r: r['created_at'])[-args.last:]
    print('时间 | 预测年化费率 | 实际单次费率 | 比值 / 偏差 bp | 覆盖天数 | 结论')
    for row in history:
        print(f"{row['created_at']} | {row['predicted_annual_rate']} | {row['actual_rate']} | "
              f"{row['ratio']} / {row['deviation_bp']} | {row['coverage_days']} | {row['conclusion']}"
              f" | {row['notional_source'] or '无独立名义证据'}")
    if not history:
        print('暂无对账记录')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
