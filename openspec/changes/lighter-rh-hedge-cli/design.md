## Context

Task 1、2 已提供只读 `LighterClient`、通用 `HedgeEngine`、可配置名义上限和认证异常类型。Task 3 只负责把这些能力装配成独立进程，并确保它不能接触 `X10_GRID` 实盘网格账户。测试必须完全使用假客户端和假引擎，不访问真实 API 或下单。

## Goals / Non-Goals

**Goals:**

- 提供默认 dry-run、参数明确的 CLI。
- 在构造任何客户端前验证账户前缀、Lighter 地址和 vault 隔离。
- 打印并记录足以人工核对两腿身份和风险参数的启动摘要。
- 使用 `auth_error_types=()` 和 `on_auth_error=None` 关闭 Lighter 不需要的会话自愈。
- 无论引擎正常返回或抛错，都关闭已构造的客户端。

**Non-Goals:**

- 不实现 Task 4 告警、Task 5 灰度上线或任何 Lighter 自动交易。
- 不修改网格引擎、网格入口、运行时配置或已运行进程。

## Decisions

1. 账户前缀先去空白并转大写，再拒绝 `X10_GRID`。相比只做区分大小写的字符串判断，这能堵住配置书写差异造成的绕过。
2. 同时比较固定的 `X10_HEDGE_VAULT_ID` 与 `X10_GRID_VAULT_ID`，并比较所选前缀的 vault 与网格 vault。只有两值都已配置时比较；所选 Extended 配置缺失仍由 `ExtendedClient.from_env` 给出完整缺失项错误。
3. `main()` 在构建 parser 前加载 dotenv，使 `--lighter-address` 的默认值可来自 `.env`；异步 `_main()` 不重复加载环境，便于测试且避免隐藏状态变化。
4. CLI 先连接只读 Lighter 腿取得 `account_index`，再打印启动摘要并进入现有 `run_forever()`。这会让标准引擎连接阶段再次执行一次幂等的公开账户反查，但不接触交易接口，换取不修改通用引擎 API。
5. 复用 `get_logger("lighter_hedge")` 生成 `log/lighter_hedge_YYYYMMDD.log`。引擎内部仍保留自身日志，CLI 专用日志记录本进程的启动身份和入口级错误。
6. 使用 `asyncio.gather(..., return_exceptions=True)` 回收所有已构造客户端，避免一个关闭失败阻止另一腿释放资源。

## Risks / Trade-offs

- [网格 vault 未配置时无法比较隔离性] → 所选账户仍不能使用 `X10_GRID` 前缀；生产环境应同时配置两套 vault，缺失的对冲凭据由客户端拒绝。
- [Lighter 启动时发生两次公开账户反查] → 请求幂等且不带交易权限；避免为单一入口扩展通用引擎生命周期 API。
- [命令行显示公开钱包地址] → 地址本身是公开账户标识且是人工核对所必需，不输出任何私钥或 API key。

## Migration Plan

1. 先运行无网络 CLI 测试，确认失败路径和装配参数。
2. 仅以默认 dry-run 启动；`--live` 留待 Task 5 人工灰度。
3. 回滚只需停止新 CLI，不影响独立运行的网格进程。

## Open Questions

无。参数和账户隔离规则以本次用户提供的 Task 3 要求为准。
