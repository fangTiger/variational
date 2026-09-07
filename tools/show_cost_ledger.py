"""只读展示 swap carry 成本与对账；不创建客户端，不联网，不修复台账。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import swap_carry_cost as cost

REPORT_LABELS = {
    'start_equity': '期初权益', 'end_equity': '期末权益',
    'equity_change': '账户权益变化', 'realized_price_pnl': '已实现价格盈亏（见来源口径）',
    'unrealized_change': '未实现浮动变化', 'external_cashflow': '外部存取款',
    'other_strategies': '其它策略盈亏（含 BTC 已知流水）',
    'referral_reward': '推荐奖励', 'unclassified': '未分类流水',
    'spread_embedded_adjustment': '平台盈亏已含滑点的重复计入抵销',
    'explained': '已解释合计', 'residual': '未解释残差',
}


def main(argv=None):
    """文件缺失或损坏以中文降级；路径全部可以注入。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=cost.DEFAULT_PATH, help='成本台账路径')
    parser.add_argument('--reconciliation', type=Path, default=None, help='对账证据路径')
    parser.add_argument('--equity', type=Path, help='组合权益快照路径，默认与台账同目录')
    parser.add_argument('--scope', choices=['account', 'swap_carry'], default='account')
    parser.add_argument('--threshold', default='1', help='残差绝对值告警阈值（USD）')
    parser.add_argument('--summary', action='store_true', help='按类型汇总及对账')
    parser.add_argument('--since', help='带时区的起始时间（ISO 8601）')
    parser.add_argument('-n', '--limit', type=int, default=20, help='最近记录条数')
    args = parser.parse_args(argv)
    try:
        if cost.number(args.threshold) < 0:
            raise ValueError('残差阈值不得为负')
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit < 1:
        parser.error('条数必须大于零')
    rows, error = cost.load(args.path)
    if error:
        print(error)
        return 0
    try:
        selected = cost.select(rows, args.since)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.summary:
        print('时间 | 类型 | 市场 | 金额 USD | 来源')
        for row in sorted(selected, key=lambda r: cost.timestamp(r['ts']))[-args.limit:]:
            print(f"{row['ts']} | {cost.LABELS.get(row['event_type'], row['event_type'])} | "
                  f"{row['market']} | {cost.number(row['amount_usd']):+.6f} | {row['source']}")
        return 0
    print(f'成本明细（{args.scope}；切换父项不重复计入）')
    for kind, amount in cost.summarize(selected, args.scope).items():
        print(f"{cost.LABELS.get(kind, '合计')}：{amount:+.2f} USD")
    try:
        start, end = cost.reconciliation_period(rows, args.since)
        print(f'请求对账区间：({start.isoformat()}, {end.isoformat()}]；范围：{args.scope}')
    except ValueError as exc:
        print(f'对账不可用：{exc}；未解释残差：不可用')
        return 0
    if args.reconciliation:
        report, error = cost.read_report(rows, args.reconciliation, start.isoformat())
        if report and (cost.timestamp(report['end_ts']) != end or report['scope'] != args.scope):
            report, error = None, '对账不可用：显式证据的终点或 scope 与请求不符'
    else:
        report, error = cost.snapshot_report(
            rows, args.equity or args.path.with_name('portfolio_equity.jsonl'), args.since,
            scope=args.scope, threshold=args.threshold)
    if error:
        print(error + '；未解释残差：不可用')
    else:
        print(f"对账区间：({report['start_ts']}, {report['end_ts']}]；来源：{report['source']}")
        if 'snapshot_start_ts' in report:
            offsets = report['snapshot_offsets_seconds']
            print(f"期初权益快照：{report['snapshot_start_ts']}（偏差 {offsets[0]:+g} 秒）")
            print(f"期末权益快照：{report['snapshot_end_ts']}（偏差 {offsets[1]:+g} 秒）；容差 900 秒")
            if any(offsets):
                print('注意：权益快照与请求端点不完全重合，未改动流水区间，时间偏差可能贡献残差。')
        print('口径：' + report['basis_note'])
        for kind, label in cost.LABELS.items():
            print(f'{label}：{report[kind]:+.6f} USD')
        print('对账区间成本合计：' + f"{report['total']:+.6f} USD")
        for key, label in REPORT_LABELS.items():
            amount = report[key]
            line = f'{label}：' + ('不可用（缺少证据）' if amount is None else f'{amount:+.6f} USD')
            print(f'\033[31m{line}\033[0m' if key == 'residual' and report['warning'] else line)
        ratio = report['residual_ratio']
        print('残差占权益变化比例（绝对值）：' + (f'{ratio:.2f}%' if ratio is not None else '不可用（权益变化为零）'))
        if not report['evidence_complete']:
            print('证据不完整：以上合计仅含已知科目，未实现浮动变化未填零，不能确认完整闭合。')
        if report['warning']:
            print(f"\033[31m告警：未解释残差超过阈值 ${report['threshold']}；可能原因：漏记科目、数据源缺口、口径不一致。\033[0m")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
