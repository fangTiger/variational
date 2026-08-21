# maker post-only 拒绝降级实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**目标：** 让 maker 优先对冲在 post-only 立即成交拒绝时按实际仓位差安全转为市价补单，不再阻断平仓和反向开仓。

**架构：** 在 `maker_first_hedge` 的 post-only 下单边界增加窄错误识别。命中后使用已读取的挂单前仓位与新读取的当前仓位计算实际变化，再沿目标方向只补剩余差值；未命中的错误和正常 maker 状态机完全保持原样。

**技术栈：** Python 3.11、`asyncio`、`Decimal`、pytest、Hyperliquid Python SDK 0.24.0、OpenSpec。

---

### 任务 1：锁定真实适配器与事故行为

**文件：**
- 修改：`tests/test_hyperliquid_client.py`
- 修改：`tests/test_maker_first_hedge.py`

**步骤：**

1. 用 FakeExchange 返回 SDK `statuses[0].error` 的真实结构，断言 `HyperliquidClient.place_limit_order` 产生实盘同形态 `RuntimeError`。
2. 扩展 maker 测试桩以模拟挂单拒绝、拒绝期间仓位变化和市价成交后的仓位更新，方法签名继续与真实适配器契约核对。
3. 新增大小写/措辞识别、按实际仓位差补单、无关错误透传和最终敞口收敛测试。
4. 运行新增测试，确认适配器契约测试通过而 maker 降级测试按预期 RED。

### 任务 2：实现窄错误分流与安全补差

**文件：**
- 修改：`engine/hedge_engine.py`

**步骤：**

1. 增加私有错误分类函数，规范化大小写、连接符和空白，并组合识别 post-only/maker-only 与立即撮合、穿价、吃流动性语义。
2. 捕获 `place_limit_order` 异常；未命中分类时原样抛出。
3. 命中时重读当前持仓，按挂单前后实际变化计算剩余量，仅按目标同方向差值发送市价单。
4. 返回清晰中文诊断说明，仓位重读失败时保守向上失败。
5. 运行 maker 定向测试至全绿，并确认正常 maker、部分成交、撤单竞态测试无变化。

### 任务 3：OpenSpec 与完整验证

**文件：**
- 修改：`openspec/changes/fix-maker-post-only-rejection/tasks.md`

**步骤：**

1. 运行 maker 与 Hyperliquid 定向测试、事故回放断言和 Python 编译检查。
2. 运行完整 `.venv/bin/python -m pytest -q tests/`，确认相对 897 基线无回归。
3. 更新 OpenSpec 任务勾选并运行 `openspec validate fix-maker-post-only-rejection --strict`。
4. 检查最终 diff，确认只涉及修复规范、计划、引擎与测试；保留 `tools/run_timed_volume.py` 既有改动。
5. 按用户约束不执行提交、推送、真实网络访问、plist 或 `launchctl` 操作。
