# 定时对冲数量容差修复实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**目标：** 让定时定量策略按交易所真实最小下单量推导跨所对冲容差，并让不可交易补单与平仓残差确定性收敛。

**架构：** 策略启动每个状态机节拍前通过两侧适配器的 `get_min_order_size` 读取并缓存有效最小下单量，以两者较大值和既有数值误差容差的较大值作为对冲容差。每侧是否可补单或平仓仍使用该侧自己的最小下单量；持仓始终在交易后重读，元数据未知时互锁且按普通轮询告警，不进入紧急重试。

**技术栈：** Python 3.11、`asyncio`、`Decimal`、pytest、OpenSpec。

---

### 任务 1：锁定跨所精度事故与容差行为

**文件：**
- 修改：`tests/test_timed_hedged_volume.py`
- 修改：`tests/test_run_timed_volume.py`

**步骤：**

1. 扩展现有内存适配器，使测试可分别配置最小下单量与舍入结果，同时继续由签名测试核对 `LighterClient`、`ExtendedClient` 的真实 `get_min_order_size` 方法。
2. 新增事故回放：Lighter 舍入并持有 `0.00129`、Extended 舍入并持有 `-0.00128`，断言动作不是 `execution_uncertain`、轮次已推进且净敞口在动态容差内。
3. 新增超容差实仓测试，断言只按重读实仓的净差补齐较小腿。
4. 运行新增测试，确认因当前固定 `0.000001` 容差而按预期失败。

### 任务 2：锁定不可交易残差与查询失败行为

**文件：**
- 修改：`tests/test_timed_hedged_volume.py`
- 修改：`tests/test_run_timed_volume.py`

**步骤：**

1. 新增小于该侧最小下单量的补齐差测试，断言不发送补单、记录中文告警并按容差完成开仓。
2. 新增到期平仓残余低于每侧最小下单量测试，断言轮次关闭、状态清空且不会重复发残差平仓单。
3. 新增任一侧最小下单量查询失败测试，断言失败关闭为互锁、无交易、无 `execution_uncertain`。
4. 新增运行循环测试，断言元数据互锁即使携带非零净敞口也按普通间隔检查，不形成每秒死循环。
5. 运行新增测试并确认 RED 原因准确。

### 任务 3：最小实现动态容差与确定性收敛

**文件：**
- 修改：`timed_volume/strategy.py`
- 修改：`tools/run_timed_volume.py`

**步骤：**

1. 增加私有订单限制值对象与缓存查询，验证两侧返回有限正数；失败时中文告警并启用互锁。
2. 将中性判定改为反向持仓且净敞口绝对值小于动态容差；目标数量保留两侧各自舍入结果。
3. 在补齐和 `_flatten_all` 下单前按对应侧最小下单量过滤不可交易数量，并在过滤时明确告警。
4. 将两侧均为零或各自仅剩不可交易残差视为平仓完成，同时在结果中保留实际残差。
5. 让运行循环只在已知动态容差下判定净敞口紧急，元数据互锁保持普通轮询。
6. 运行定向测试至全绿，再运行相关适配器与粉尘仓位测试防止契约回归。

### 任务 4：更新规范与完整验证

**文件：**
- 修改：`openspec/changes/timed-hedged-volume/specs/timed-hedged-volume/spec.md`
- 修改：`openspec/changes/timed-hedged-volume/proposal.md`
- 修改：`openspec/changes/timed-hedged-volume/tasks.md`

**步骤：**

1. 把“等量/归零”语义修订为“交易所精度与最小下单量推导的容差内对冲/不可交易残差正常退出”，补充查询失败和事故场景。
2. 在提案记录本次教训：跨所精确数量相等不是可实现的不变量，补齐与平仓必须先验证可交易性。
3. 运行 `openspec validate timed-hedged-volume --strict`。
4. 运行定向测试、完整 `.venv/bin/python -m pytest -q tests/`、Python 编译检查和 diff 审阅。
5. 按用户约束不执行 `git commit`、`git push`、真实网络访问、plist 或 `launchctl` 操作。
