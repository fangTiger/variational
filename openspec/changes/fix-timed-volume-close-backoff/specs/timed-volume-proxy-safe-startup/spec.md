# timed-volume-proxy-safe-startup

## ADDED Requirements

### Requirement: 双实例必须通过无代理统一脚本启动

项目 SHALL 提供参数化脚本启动 SNDK 或 BTC 定时对冲实例。脚本 SHALL 使用项目
`.venv/bin/python`，并通过 `/usr/bin/env -u` 显式剥离 `HTTP_PROXY`、`HTTPS_PROXY`、
`http_proxy`、`https_proxy`、`ALL_PROXY`、`all_proxy`。

#### Scenario: 剥离代理后执行自检
- **WHEN** 启动脚本完成代理变量剥离
- **THEN** 脚本以 Bash 内建变量检查确认六个变量均不存在；任一仍存在时打印中文错误并非零退出，不得依赖可被 PATH 替换的外部自检命令

#### Scenario: SNDK 参数保持事故后确认值
- **WHEN** 使用 `sndk` 实例参数调用脚本
- **THEN** 脚本以规定的 Variational 主腿、Hyperliquid 对冲腿和全部原始数值、路径参数启动

#### Scenario: BTC 参数保持运行中实例值
- **WHEN** 使用 `btc` 实例参数调用脚本
- **THEN** 脚本以 PID 29931 只读取得的 Lighter 主腿、Variational 对冲腿和全部原始数值、路径参数启动

#### Scenario: 进程输入输出隔离
- **WHEN** 任一实例启动
- **THEN** stdin 来自 `/dev/null`，stdout 与 stderr 追加到 `log/` 下对应实例日志

### Requirement: CLI 必须警告代理污染

定时对冲 CLI SHALL 在启动时检测六个大小写代理环境变量。发现任一变量时 SHALL
打印醒目中文警告，说明代理出口可能位于受限地区、写接口可能拒单而读接口仍正常，
但 SHALL 不阻断启动。

#### Scenario: 检测到任一代理变量
- **WHEN** 六个受检变量至少一个存在且非空
- **THEN** CLI 打印醒目中文警告并继续后续启动流程

#### Scenario: 无代理变量
- **WHEN** 六个受检变量均不存在或为空
- **THEN** CLI 不打印代理警告
