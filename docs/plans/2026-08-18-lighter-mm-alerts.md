# Lighter 做市告警覆盖 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 Lighter 做市心跳补齐动态间隔和真实库存名义额，并让主动告警覆盖未更新、连续失败与库存风险，同时在尚未部署时保持沉默。

**Architecture:** 心跳生产端复用 `GridEngine._inv()` 本轮返回的持仓和标记价，不增加网络请求；告警端复用现有 JSONL 尾部读取、有限数解析与未来时间容差。做市告警拥有独立文件常量、收集函数和 `lighter_mm_` key 空间。

**Tech Stack:** Python 3、asyncio、pytest、OpenSpec。

---

### Task 1: 扩充做市心跳

**Files:**
- Modify: `tests/test_run_lighter_mm.py`
- Modify: `tools/run_lighter_mm.py`

1. 修改成功轮次测试，要求心跳包含传入的 `interval`，并断言 `0.0123 × 60000 = 738.0`。
2. 新增价格未取得时 `inventory_usd is None` 的测试；每个测试 docstring 写明防止把未知库存伪装成零值。
3. 运行 `.venv/bin/python -m pytest tests/test_run_lighter_mm.py -q`，确认新增断言因字段缺失失败。
4. 在 `_inv` 包装器中同时缓存本轮标记价；构造心跳时仅在持仓和价格都存在时计算带符号浮点名义额，否则写 `None`。
5. 重跑定向测试确认通过。

### Task 2: 新增做市告警收集器

**Files:**
- Modify: `tests/test_alert_check.py`
- Modify: `tools/alert_check.py`
- Modify: `panel/actions.py`

1. 给临时环境增加独立 MM 路径和健康心跳辅助函数。
2. 分别测试：文件不存在返回空做市告警；空/损坏文件产生缺失告警；动态三倍间隔与 5 秒回退；远未来时间产生陈旧告警；连续三轮失败；库存绝对值超过 90%；库存为 `None` 跳过；`collect_alerts()` 能汇总做市告警。每个测试 docstring 指明具体静默或误报故障。
3. 运行 `.venv/bin/python -m pytest tests/test_alert_check.py -q`，确认因常量/函数/接线缺失失败。
4. 新增 `_MM_MONITOR` 和默认 5 秒常量；实现 `_collect_lighter_mm_alerts(now)`：先用 `Path.exists()` 区分未部署，再读取有效对象，按最新记录判断陈旧和库存，按尾部明确 `False` 统计连续失败。
5. 在 `collect_alerts()` 中扩展收集结果，并给每个新增 key 登记面板动作指引和严重级别，重跑两个定向测试文件。

### Task 3: 规范与全量验证

**Files:**
- Modify: `openspec/changes/add-lighter-mm-alerts/tasks.md`

1. 运行 `openspec validate add-lighter-mm-alerts --strict` 并核对 spec 场景。
2. 运行 `.venv/bin/python -m pytest tests/ -q`，确认原基线和新增测试全部通过。
3. 运行语法检查并检查 `git diff/status`，确认没有修改禁止文件或 `data/`。
4. 按实际结果勾选 tasks；不执行 commit、push、网络或交易操作。
