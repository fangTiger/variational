## Context

现有 `ExchangeAdapter` 已统一行情、持仓和交易接口，`HedgeEngine` 也已具备两腿读取失败时保持不动、清算保护、保证金降险和再平衡能力。Lighter 的 Robinhood Chain 实例提供无需鉴权的公开 HTTP API，但 primary 腿必须保持人工操作；同时，现有引擎用异常类名识别 Variational 会话失效，妨碍适配器泛化。

## Goals / Non-Goals

**Goals:**

- 将 Lighter 的公开 API 数据严格转换为统一模型，任何传输或解析失败都向上抛出。
- 仅阻止超额 primary 仓位触发再平衡，同时保留更早执行的清算保护和保证金降险。
- 让引擎识别人工只读腿，清算与保证金动作只告警、不做可能单腿成功的非原子改仓。
- 通过配置异常类型保留 Variational 的旧会话自愈逻辑。

**Non-Goals:**

- 不实现 Lighter 自动下单、签名或凭据管理。
- 不实现 CLI、告警、灰度上线或真实 API 探测。
- 不改变网格系统及其账户和运行时配置。

## Decisions

1. `LighterClient` 直接实现 `ExchangeAdapter`，持有带 10 秒超时的 `httpx.AsyncClient`。相比在引擎中加入 Lighter 分支，这能保持引擎只依赖统一抽象。
2. `connect()` 先用 L1 地址解析并缓存 `account_index`。相比每次查仓都重新反查，缓存减少请求且让未开户在启动阶段明确失败。
3. HTTP 状态、超时、JSON 解码错误和持仓字段结构错误全部传播，不转换为空仓。Robinhood Chain 实例的业务成功码为 `200`；同时兼容旧成功码 `0` 以及缺失或为 `None` 的 `code`。只有接口成功、列表结构有效且市场条目不存在或数量为零时，才返回零仓位。
4. 行情端点只提供标记价，因此统一模型的 `bid` 与 `ask` 都使用 `mark_price`；其 `mid` 精确等于标记价，满足名义换算用途。
5. `market_order()` 与 `close_position()` 都在子类中明确抛出 `NotImplementedError`，并用 `supports_trading=False` 声明人工只读能力。引擎遇到需要双腿改仓的清算或保证金风险时只返回人工告警，避免 hedge 单腿成功后制造裸仓。
6. `max_primary_notional` 默认为 `None`，只在配置时启用。清算保护和保证金降险先执行；检查位置紧邻再平衡之前，仅拒绝跟随变大的 primary 仓位，不阻止降低风险的动作。
7. 自愈异常类型存放在 `HedgeConfig` 的元组中，默认空元组，并且仅按显式配置的类型判断。空配置表示没有异常需要自愈，不再按 `*AuthError` 类名后缀猜测。旧 Variational 入口显式注入真实异常类，因此既有自愈行为不变，引擎也无需导入具体适配器。

## Risks / Trade-offs

- [行情响应中的市场标识字段可能与持仓字段不同] → 仅按已实测的 `symbol` 匹配，并用假响应固定契约。
- [只读 primary 逼近清算或 hedge 保证金不足时无法自动双腿降险] → 引擎明确告警需人工处理并保持两腿不动，优先避免非原子操作制造裸仓。
- [名义超限可能同时发生清算或保证金风险] → 清算和保证金保护优先执行，避免超限门禁吞掉唯一风险告警；只读 primary 则告警并保持两腿不动。
- [旧 Variational 自愈需要入口配置异常类型] → 同步修改现有入口构造配置，保持既有行为与测试基线。

## Migration Plan

1. 先新增只读适配器测试并验证失败，再实现适配器。
2. 再新增名义上限测试并验证失败，最后修改引擎与旧 Variational 入口配置。
3. 运行两个新增测试文件和完整 `tests/`；不提交、不推送。

## Open Questions

无。本次 API 契约与范围以 `docs/superpowers/plans/2026-08-15-Lighter-RH积分对冲.md` 为准。
