## ADDED Requirements

### Requirement: 只读归因主流程
系统 MUST 提供可重复执行的归因 CLI，读取本地成交与权益快照、使用 `X10_GRID` 账户只读接口取得资金费和当前账户快照，完成导入、离线配对、计算并原子写入 `data/attribution.json`。系统 MUST NOT 下单、撤单或修改账户状态。

#### Scenario: 重复运行归因
- **WHEN** 对同一批 JSONL 和交易所流水连续运行两次
- **THEN** SQLite 中的成交、资金费和闭环保持幂等，报告金额不重复累计

#### Scenario: 测试不访问网络
- **WHEN** 运行归因单元测试
- **THEN** 交易所读取由构造数据替代且不会发出真实网络请求

### Requirement: 一致的观察窗口
系统 MUST 从 `data/attribution_start.json` 的显式起点开始观察；文件不存在时 MUST 回退到最早成交的 `engine_run_id` 启动时间，再回退到最早成交时间。权益起点、闭环与资金费 MUST 使用同一观察窗口。

#### Scenario: 当前真实运行没有显式起点
- **WHEN** 成交携带 `engine_run_id=run-<秒级时间戳>` 且归因起点文件不存在
- **THEN** 系统使用该运行时间后的首条有效权益快照作为权益起点

### Requirement: 真实执行恒等式校验
系统 MUST 把同一次网格账户余额快照中的结束权益和未实现盈亏传入 Task 5 的归因函数，并在报告中写出残差、阈值判断与输入口径。若起点未实现盈亏没有历史记录，系统 MUST 显式标注按零假设，不能把未执行校验伪装成残差为零。

#### Scenario: 存在非零残差
- **WHEN** 权益变化不等于闭环利润、未实现盈亏变化与资金费之和
- **THEN** 报告保留实际非零残差，且不修改配对逻辑或归因阈值来让它归零

#### Scenario: 账户快照不可用
- **WHEN** 无法取得结束权益或未实现盈亏
- **THEN** 报告明确归因不可判定，而不是输出看似通过的零残差

### Requirement: 归因主动告警
告警系统 MUST 在归因报告表明残差超过阈值时产生 `attribution_gap` 告警，并在满 4 周且判据不通过时产生 `verdict_stop` 告警。新增逻辑 MUST NOT 改变既有网格和 Lighter 告警。

#### Scenario: 该报残差告警
- **WHEN** `attribution.json` 的 `has_gap` 为真且没有更高优先级的停止结论
- **THEN** 告警集合包含 `attribution_gap`，正文包含带符号残差金额

#### Scenario: 该报停止告警
- **WHEN** `attribution.json` 的 `should_stop` 为真
- **THEN** 告警集合包含 `verdict_stop`，正文包含判据原因

### Requirement: 仓库内每小时调度配置
仓库 MUST 包含 Label 为 `com.variational.pnl-attribution` 的 plist，使用项目虚拟环境和 `tools.pnl_attribution`，设置项目 WorkingDirectory/PYTHONPATH、每小时调度、RunAtLoad 和独立 stdout/stderr 日志。系统 MUST NOT 自动安装该 plist 或执行 `launchctl`。

#### Scenario: 人工安装前校验
- **WHEN** 对仓库内 plist 执行语法与结构检查
- **THEN** 配置合法、StartInterval 为 3600，且用户 LaunchAgents 目录未被修改

### Requirement: 运维文档可发现
README MUST 列出归因服务、仓库 plist 位置、手工运行命令以及 `data/attribution.json` 的查看方式。

#### Scenario: 人工查看归因
- **WHEN** 运维人员查阅 README
- **THEN** 能找到归因 CLI、每小时服务和结果文件路径
