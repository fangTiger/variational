## Context

统一面板已经采用 `SystemStatus.metrics` 扩展点，三类 provider 各自负责格式化与 tone，渲染器无需理解指标语义。本次只扩展 provider 输出，并在总览增加一个由 `alive` 三态派生的统计。

## Decisions

### 网格熔断余量只依赖本地快照

`grid.collect()` 增加可注入的 `equity_peak_path`。有效权益和峰值同时存在且当前权益非零时，先算 `breaker_equity = peak * 0.88`，再算 `distance = equity - breaker_equity` 和 `distance_ratio = distance / equity`。峰值文件或字段无效时仅该指标降级为 `—`。

持仓行复用 `grid_live.json` 已有的 `inv_btc` 与 `inv_usd`，保留美元正负号；库存上限行对占用和上限取绝对值并补百分比，避免把方向语义混入容量占用。

### 对冲名义金额只读当轮状态

快照回调从 `state.primary.raw` 字典读取 `position_value` 与 `unrealized_pnl`，从 `state.hedge.raw` pydantic 对象读取 `value` 与 `unrealised_pnl`，不调用客户端方法。每个字段独立容错，任何读取或字符串转换失败只让该字段成为 `None`。CLI 的 `max_primary_notional` 作为回调构造参数透传，快照只序列化字符串或 `None`。

### provider 统一容错新增字段

数值先安全解析，再格式化。新增字段缺失或无效只影响对应 `Metric`，不会抛出到注册表。tone 阈值严格采用需求中的小于或大于规则，边界值不向更严重等级偏移。

### 在线统计保留 alive 三态语义

注册表提供纯函数返回“有存活概念的系统数、在线数”；渲染器使用当前 systems 列表生成文案。`alive is None` 不进入分母，`alive is True` 才计在线。

## Safety

- 不修改任何下单、再平衡、名义上限判断、风控或清算保护函数。
- 测试全部使用临时文件与构造对象，不请求真实网络。
- 不重启或探测运行中进程，不执行 `launchctl`、kill、commit 或 push。
