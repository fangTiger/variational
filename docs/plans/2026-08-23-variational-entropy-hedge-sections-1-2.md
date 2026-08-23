# Variational RFQ 执行分流实施计划

> **执行要求：** 使用 `executing-plans` 与 `test-driven-development`，逐项完成 RED、GREEN 和回归验证。

**目标：** 完成 OpenSpec 变更 `variational-entropy-hedge` 的第 1、2 节，使 Variational 以 RFQ 模型安全参与定时定量双腿执行，同时保持现有订单簿适配器行为不变。

**架构：** 在适配器基类增加默认 `orderbook` 能力声明，Variational 覆盖为 `rfq`。数量下限与步长只从 Variational API 元数据读取；执行器在任何额外询价前识别 RFQ 并直接调用市价路径。策略的可用性检查按执行模型选择能力清单，双腿提交仍复用既有 `asyncio.gather`。

**技术栈：** Python、asyncio、Decimal、pytest、OpenSpec。

---

### 任务 1：锁定适配器执行模型契约

**文件：**

- 修改：`adapters/base.py`
- 修改：`adapters/variational_client.py`
- 测试：`tests/test_variational_execution.py`

1. 写测试断言基类默认 `execution_model == "orderbook"`，Variational 声明为 `"rfq"`。
2. 在只叠加测试的 HEAD 快照中运行测试，确认因属性缺失失败。
3. 用最小类属性与中文文档说明实现契约。
4. 运行目标测试确认通过。

### 任务 2：从 API 元数据取得数量约束

**文件：**

- 修改：`adapters/variational_client.py`
- 测试：`tests/test_variational_execution.py`
- 测试：`tests/test_timed_hedged_volume.py`

1. 写测试覆盖报价元数据中的最小量、数量步长、元数据缺失时明确抛错、步长缺失时保持原值。
2. 写策略级测试，证明最小量未知时进入互锁且执行器没有任何下单调用。
3. 在只叠加测试的 HEAD 快照中运行测试，确认基类 `NotImplementedError`、缺少对齐行为和策略互锁断言构成红灯。
4. 实现报价优先、支持资产后备的元数据解析与按市场缓存；禁止构造默认下限或步长。
5. 运行数量约束与策略互锁测试确认通过。

### 任务 3：RFQ 执行路径分流

**文件：**

- 修改：`engine/hedge_engine.py`
- 测试：`tests/test_maker_first_hedge.py`

1. 用不实现 `place_limit_order` 的 RFQ 假适配器复现旧实现的 `AttributeError`。
2. 写最终行为测试，要求只调用一次 `market_order`，不调用 `get_market_price` 或持仓读取，并返回 `used_taker=True`。
3. 写默认执行模型测试，要求未声明属性的旧适配器仍走 maker 路径。
4. 在 `maker_first_hedge` 的零目标判断之后、询价之前增加 RFQ 早期分支。
5. 运行目标测试确认 RFQ 与 orderbook 两条路径均通过。

### 任务 4：按模型检查能力并验证并发

**文件：**

- 修改：`timed_volume/strategy.py`
- 测试：`tests/test_timed_hedged_volume.py`

1. 写测试证明 RFQ 不需要限价下单、订单查询和撤单能力，但仍需要市价、行情和最小量能力。
2. 写事件栅栏测试：RFQ 腿与订单簿腿互相等待对方开始，只有 `asyncio.gather` 并发提交才能在超时前完成。
3. 在只叠加测试的 HEAD 快照中运行测试，确认旧能力清单拒绝 RFQ。
4. 仅按 `execution_model` 调整 `_check_hedge_available` 的 required 清单，不改 `_execute_pair`。
5. 运行目标策略测试确认通过。

### 任务 5：OpenSpec 状态与全量验证

**文件：**

- 修改：`openspec/changes/variational-entropy-hedge/tasks.md`

1. 逐条核对 1.1～2.4 与实现、测试一致后标记 `[x]`。
2. 运行新增/相关测试并检查失败路径桩确实没有固定返回成功。
3. 运行 `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q`。
4. 检查 `git diff --check`、相关文件差异及未触碰第 3、4 节和 `tests/test_lighter_mm_deploy.py`。
