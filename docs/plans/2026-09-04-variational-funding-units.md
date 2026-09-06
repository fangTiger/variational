# Variational 资金费单位修正实施计划

> **执行要求：** 按测试驱动开发逐项实施，并在完成前运行完整离线测试。

**目标：** 将 Variational `predicted_funding_rate` 统一解释为年化小数，并提供一个只读、零下单的单位证据验证工具。

**方案：** 交易监控继续以 `%/8h` 作为两腿可比展示口径，只修正 Variational 原始值到该口径的换算；Extended 算法保持不变并明确标注尚未由真实结算校准。验证工具把分页、记录解析、365/360 年化和报告生成写成纯函数，网络入口只负责加载凭证、清除代理变量和调用只读端点。

**技术栈：** Python、`Decimal`、异步 Variational 客户端、pytest。

---

### 任务 1：锁定正确单位口径

**文件：**
- 修改：`tests/test_monitor.py`
- 修改：`tracking/monitor.py`

1. 用 BTC 真实预测值与结算值编写失败测试，覆盖年化小数到 `%/8h` 的换算、推荐方向及输出文案。
2. 用 8h/4h 真实结算费率断言 365 天还原值，并断言旧“百分比/周期”解释产生错误年化。
3. 运行定向测试确认按预期失败。
4. 最小修改 `compute_funding_view`、文件头说明和 `FundingView` 文档/展示。
5. 重跑定向测试确认通过。

### 任务 2：澄清适配器与调用点语义

**文件：**
- 修改：`adapters/variational_client.py`
- 审计：`tools/hedge.py`
- 审计：`tracking/track_equity_util.py`

1. 更新 `get_funding` 与 `get_funding_rate` 文档，声明返回原始年化小数及单期换算方法，不改变返回值。
2. 确认两个调用点只通过 `compute_funding_view` 展示，无二次旧口径换算；除非发现误导文案，否则不改调用点。

### 任务 3：新增只读验证工具

**文件：**
- 新建：`tests/test_verify_funding_units.py`
- 新建：`tools/verify_funding_units.py`

1. 先写失败测试，覆盖 `limit=100` 全量分页、资金费筛选、代理变量清除、真实 BTC 隐含名义/价格、365/360 六位小数对照、8h/4h 同年化上限、当前预测两种解释与历史区间比较。
2. 运行定向测试确认因工具不存在而失败。
3. 实现纯计算与中文报告；网络入口仅调用 `/transfers` 和 `/funding/v2`，不包含任何下单方法。
4. 对缺少历史价格的记录明确报告为反推证据；有历史价格时才计入独立扣款误差统计。
5. 重跑定向测试确认通过。

### 任务 4：完整验证

**文件：**
- 检查：上述全部变更

1. 检查 `git diff`，确认未修改 `engine/`、`grid/`、`timed_volume/`、`lighter` 相关文件和用户已有 OpenSpec 文件。
2. 运行 `.venv/bin/python -m pytest tests/ -q`。
3. 运行 Python 语法编译检查，并核对工具 `--help` 不触发网络。
4. 汇报命令、通过数量和剩余风险；不运行真实网络验证，不操作进程，不下单。
