## Why

Lighter 做市机器人已经每轮写入 `data/lighter_mm.jsonl`，但主动告警完全不读取
这份心跳。机器人静默退出或连续失败时不会通知，重复了此前网格无声停摆 35.5
小时所暴露的监控盲区；同时现有心跳缺少动态轮询间隔和库存名义额，告警侧无法
可靠判断陈旧程度与库存风险。

## What Changes

- 做市心跳新增快轮询 `interval` 与基于本轮真实标记价计算的 `inventory_usd`；
  价格不可用时保留 `null`。
- 主动告警读取 `data/lighter_mm.jsonl`，覆盖心跳陈旧、连续三轮失败和库存超过
  上限 90% 三类风险。
- 做市心跳文件不存在时视为尚未部署并保持沉默；文件存在但无有效记录时告警。
- 做市告警使用独立的 `lighter_mm_` key 前缀，并拒绝把远未来时间戳视为新鲜。

## Capabilities

### New Capabilities

- `lighter-mm-alerting`: 定义做市心跳的告警字段、部署感知缺失规则、陈旧、连续
  失败和库存风险判据。

### Modified Capabilities

无。

## Impact

- 修改 `tools/run_lighter_mm.py`、`tools/alert_check.py`、`panel/actions.py` 及对应测试。
- 不修改网格、适配器、对冲或部署代码，不读写既有 `data/` 文件。
- 验证仅使用本地替身与临时目录，不连接交易所、不下单。
