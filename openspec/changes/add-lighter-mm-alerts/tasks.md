## 1. 心跳字段

- [x] 1.1 先补 `interval`、真实库存名义额与价格缺失降级的失败测试
- [x] 1.2 最小实现心跳字段并运行 `tests/test_run_lighter_mm.py`

## 2. 做市告警

- [x] 2.1 先补文件不存在静默、存在但无有效记录告警的失败测试
- [x] 2.2 先补动态陈旧门槛、默认间隔和未来时间戳的失败测试
- [x] 2.3 先补三轮连续失败及库存九成阈值、空库存的失败测试
- [x] 2.4 最小实现 `_collect_lighter_mm_alerts` 并接入 `collect_alerts`
- [x] 2.5 运行 `tests/test_alert_check.py`，保持原有 41 个测试通过
- [x] 2.6 为四个新增告警 key 补齐面板动作指引与严重级别

## 3. 验证

- [x] 3.1 校验 OpenSpec，并逐项核对实现与场景
- [x] 3.2 运行 `.venv/bin/python -m pytest tests/ -q`
- [x] 3.3 核对未修改禁止范围、未访问交易所、未提交 Git
