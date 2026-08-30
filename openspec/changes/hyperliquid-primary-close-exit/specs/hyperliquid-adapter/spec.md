## ADDED Requirements

### Requirement: 默认加载 XYZ builder dex
Hyperliquid 适配器 SHALL 默认加载主永续、`io` 与 `xyz` 永续 dex。

#### Scenario: 未显式覆盖永续 dex 列表
- **WHEN** 客户端未收到参数或环境变量形式的永续 dex 覆盖
- **THEN** 默认列表 SHALL 为 `("", "io", "xyz")`
- **AND** `xyz:` 前缀标的 SHALL 可进入元数据解析流程
