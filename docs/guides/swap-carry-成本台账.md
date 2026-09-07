# Swap carry 成本与对账

成本台账默认为 `data/swap_carry_cost_ledger.jsonl`，对账证据默认为同目录的
`swap_carry_cost_reconciliation.json`。`VARIATIONAL_DATA_DIR` 可统一改数据根目录。
守护入口支持 `--cost-ledger` 和 `--cost-reconciliation`；人工 open/close 支持
`--cost-ledger`。provider 的两个对应参数也可注入。只读工具不联网、不创建文件。

```bash
.venv/bin/python tools/show_cost_ledger.py -n 30
.venv/bin/python tools/show_cost_ledger.py --summary
.venv/bin/python tools/show_cost_ledger.py --since 2026-09-07T00:00:00+08:00
.venv/bin/python tools/show_cost_ledger.py --path /tmp/carry.jsonl --summary --reconciliation /tmp/recon.json
```

## 逐笔口径

金额正数为收入，负数为支出。所有金额以十进制字符串保存，未知数量、方向或名义为
`null`，不伪造为零。每行另外有 `event_id`，切换子项有 `parent_id`。

- 成交后立即写入每腿 spread 和零 fee，覆盖人工及守护开仓、平仓、回滚、切换。
  spread = 数量 ×（报价中点 − 接受价）× 买卖方向。正滑点改善可为收入。
  接受价采用 RFQ 已接受的 bid/ask，来源明确标为报价对比，未冒充 `/trades` 回读。
- fee 的零行标明当前规则假设；实际非零手续费另从 `/transfers` 记账。
- funding 只读取 `/transfers.qty`；`funding_rate` 仅识别流水类型，绝不计算扣款。
  所有 XAUS、XAU、XAUT 资金费都采集，不局限当前结构或某一资金费周期。
- switch 汇总已知各腿成交成本，父项永不再进入总额。旧切换台账的
  `measured_wear_usd` 是全账户权益差，只保留为对照证据。失败切换的成功腿和回滚腿仍计账。
- allocation 在转换 confirmed 后记零，明确说明内部划转本金不属于成本。
  非零费用以实际费用流水为依据，不能把追加保证金金额当成磨损。
- `liquidation_penalty` 接受实际流水的同名类型。无法识别的流水保留为
  `unclassified`，不猜测映射、不偷偷归为手续费或其它策略。

RFQ、转换用各自唯一编号去重；资金流水有 id 时用 id，无 id 时用时间、金额、标的、
参考仓位等稳定字段指纹。无 id 且全部业务字段完全相同的两笔独立事件无法可靠区分，
需要上游提供编号才可消除这一限制。文件锁保护并发去重与追加；损坏台账拒绝继续写，
避免把损坏文件当空历史导致重复记账。记账失败会记录中文警告，不改变已确认成交结果。

## 对账

守护正常轮次分页同步实际流水，并读取 `/portfolio.balance + upnl` 与
`/positions.upnl`。首次成功快照建立基线；后续快照形成 `(期初, 期末]` 区间。
首次快照本身没有可比较区间，明确显示对账不可用。历史上未采集的权益不能倒推。
持仓浮动合计与账户浮动偏差超过 0.01 USD 时拒绝保存不同步快照。

账户权益变化 = carry 资金费 + 滑点 + 手续费 + 罚金 + 划转成本
+ carry 已实现价格盈亏 + carry 未实现变化 + 外部存取款
+ 其它策略 + 未解释残差。

平台价格盈亏本身含滑点。自动归因使用已记录成交建立加权滑点成本：开仓滑点放在持仓中，
平仓时按关闭数量从浮动移入已实现。然后从对应平台价格项剔除这些已知滑点，单列 spread。
部分平仓及跨期平仓都沿用该规则，不把残差回填价格项。旧持仓缺失的历史报价无法精确拆分，
未记录滑点仍留在平台价格项中，界面明确说明；因此残差为零不等于逐笔滑点历史完整。

carry 成本科目只接受 XAUS/XAU/XAUT。BTC 及其它具名标的的实际现金盈亏和浮动变化单列
“其它策略”；无标的未知流水保持未分类，不借其它策略消除残差。

阈值默认 1 USD，可在对账证据 `threshold` 中配置，后续快照保留该值。
残差绝对值严格超过阈值时，守护日志和面板发出 warning。
成本汇总默认覆盖全部台账；对账表另外标出准确区间与区间成本合计。
`--since` 过滤逐笔与汇总。如果与对账期初不一致，就显示缺少对应权益证据，避免错配区间。

也可为只读工具提供独立对账证据 JSON，以下是**内部证据格式，不是交易所 API schema**：

```json
{
  "start_ts": "2026-09-07T00:00:00Z",
  "end_ts": "2026-09-08T00:00:00Z",
  "start_equity": "100",
  "end_equity": "99",
  "realized_price_pnl": "0",
  "unrealized_change": "0",
  "external_cashflow": "0",
  "other_strategies": "0",
  "scope": "account",
  "source": "填写权益快照及各项独立证据来源",
  "threshold": "1"
}
```

上述价格项默认已经剔除已知滑点。若导入的是平台原始成交价口径，应显式设置
`pnl_basis: "platform_execution_price"`，工具会单列确定性的滑点重复计入抵销，
不把它隐藏为价格盈利。`scope: "swap_carry"` 则要求期初期末都是独立策略权益，
且其它策略为零。证据字段缺失、非法或文件损坏时，对账降级为不可用，不补零。

既有资金费及切换历史在守护下一正常轮次同步；没有历史报价的普通成交不能补算中点滑点。
本变更不重启进程，不迁移或重写现有真实数据。
