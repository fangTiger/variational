## Why

Extended 对冲腿当前只使用 IOC 吃单。高频调仓下，taker 手续费会持续侵蚀本金；同时，直接把旧的 maker 方案接入实盘会面临部分成交误判、超额补单和孤儿挂单风险。

## What Changes

- 新增独立的 maker 优先对冲函数：挂本方最优价，按订单状态轮询，超时撤单后按最终剩余量吃单。
- 撤单成功与否都重新读取一次订单，覆盖撤单竞态和部分成交。
- 在对冲引擎中增加默认值为 `0` 的等待配置；默认继续走既有 IOC 路径，只有显式设置正数才启用 maker 优先。
- CLI 增加显式参数并在启动摘要展示当前下单模式。
- 不修改再平衡触发条件、风控、名义上限、清算保护或快照写入。

## Capabilities

### New Capabilities

- `maker-first-hedge`: 为 Extended 对冲腿提供可选、默认关闭的 maker 优先执行方式。

### Modified Capabilities

无。

## Impact

- 修改 `engine/hedge_engine.py` 与 `tools/run_lighter_hedge.py`。
- 新增 `tests/test_maker_first_hedge.py`，并更新既有 CLI 测试以锁定默认值、装配和启动摘要。
- 不新增依赖，不发送真实网络请求，不修改运行中进程或实盘配置。
- 本变更只覆盖实施计划 Task 1 和 Task 2；Task 3 实盘启用由人工执行。
