"""【已废弃 · 判据本身推导错误 · 请勿重新启用】

2026-08-09 结论：下面这个"N_osc/2 是闭环上界"的判据**推导是错的**——
一次价格反转在走过多格行程后会产生**一批**闭环，而不是一个。据此得出的
"模型被证伪"是误报，相关结论全部作废。调用已从 tools/run_anchor_check.sh
移除，本文件仅作为历史记录保留。

留下它的原因是那次教训本身有价值：判据必须有交叉验证手段，否则算错了
自己不知道。新的归因判据（见 grid/attribution/report.py）改用恒等式
自证：权益变动 ≡ 闭环利润 + 未实现盈亏 + 资金费 + 出入金，两边对不上
就报"归因存在缺口"而非静默出数。

--- 以下为当时的原始说明，仅供追溯 ---

实盘证伪锚点：用采集到的逐笔数据算闭环率上界，与实盘实测对照。

这是设计文档 5.5 的第三层验证（前两层是合成路径闸门）。它拿实盘真实的
闭环率去**证伪**这套模型——注意是证伪，不是校准。用实盘去校准模型会把
错误糊过去，用实盘去证伪则错了藏不住。

判据：N_osc(s)/2 被当作闭环次数的上界（**此处即错误所在**）——每个闭环
需要一上一下两次转折，而且只有该格位恰好有活单时才成交，引擎又只在
band 内 30 档挂单。所以实测必然低于上界；一旦超过，说明振荡计数或数据源有误。

只读：不碰账户、不下单，只读 data/trades/ 与硬编码的实盘基准。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.verify_anchor
"""
from __future__ import annotations

from grid.scaling import count_oscillations
from tools.analyze_alpha import OPERATING_SPACING, load_trades, segment_by_gap

# 实盘基准：引擎计数器的纯净窗口（该段配置未变更，unit=166）
LIVE_LOOPS = 67
LIVE_HOURS = 17.01

# 覆盖时长低于此值只提示不阻断——样本太短判定没有意义
MIN_HOURS_FOR_VERDICT = 24.0

# 实测/上界 低于此比例视为「成交效率很低」，需要记录
LOW_EFFICIENCY_RATIO = 0.2


def main() -> None:
    rows = load_trades(days=None)
    segments = segment_by_gap(rows)
    if not segments:
        raise SystemExit("没有可用数据段，先把采集器跑起来")

    total_turns = sum(
        count_oscillations([r["p"] for r in seg], OPERATING_SPACING)
        for seg in segments
    )
    all_ts = [r["T"] for seg in segments for r in seg]
    hours = (max(all_ts) - min(all_ts)) / 3_600_000
    if hours <= 0:
        raise SystemExit("数据时间跨度为零，无法判定")

    upper_bound = total_turns / 2 / hours
    live_rate = LIVE_LOOPS / LIVE_HOURS

    print(f"格距 {OPERATING_SPACING * 100:.4f}%，覆盖 {hours:.2f} 小时，"
          f"{len(segments)} 段")
    print(f"振荡转折数 {total_turns}，闭环上界 {upper_bound:.2f} 次/小时")
    print(f"实盘实测 {live_rate:.2f} 次/小时"
          f"（{LIVE_LOOPS} 闭环 / {LIVE_HOURS} 小时）")

    if upper_bound <= 0:
        print("\n⚠ 上界为零，样本内没有任何振荡，无法判定。继续采集。")
        return

    ratio = live_rate / upper_bound
    print(f"实测/上界 = {ratio:.1%}")

    # 样本不足时只报告不阻断：实盘基准取自含白天时段的 17 小时，若采集样本
    # 只覆盖清淡夜盘，上界天然偏低，此时超过 100% 是行情差异而非模型错误。
    # 说了「不作数」就不该同时 exit(1)，否则等于把参考值当判决。
    provisional = hours < MIN_HOURS_FOR_VERDICT
    if provisional:
        print(f"\n⚠ 仅覆盖 {hours:.2f} 小时（需 {MIN_HOURS_FOR_VERDICT:.0f} 小时），"
              f"以下仅供参考、不作判定，也不会以失败退出。")

    print()
    if ratio > 1.0:
        print("❌ 实测闭环率超过理论上界。")
        if provisional:
            print("   但样本时长不足，且实盘基准与采集样本覆盖的行情不同，"
                  "尚不能据此证伪。继续采集到 24 小时后重跑。")
            return
        print("   模型被证伪：振荡计数漏计或数据源有误，结论全部作废，不得继续。")
        raise SystemExit(1)
    if ratio >= LOW_EFFICIENCY_RATIO:
        print("✅ 通过：实测落在上界的 20%–100% 区间，"
              "差额来自挂单覆盖率与队列效应。")
    else:
        print("⚠ 通过但存疑：成交效率低于 20%，队列效应可能是主要矛盾。")
        print("   记录此值，后续实盘调参若与 α 预测系统性偏离，第一嫌疑在此。")


if __name__ == "__main__":
    main()
