## 1. Hyperliquid 主腿

- [x] 1.1 先写失败测试：Hyperliquid 主腿按独立前缀装配并读取账户标识
- [x] 1.2 增加 `--primary-venue hyperliquid` 与 `--primary-env-prefix`
- [x] 1.3 启动摘要显示主腿场馆与环境变量前缀
- [x] 1.4 测试同一 Hyperliquid 账户的 `io:SNDK` 与 `xyz:SNDK` 可并存

## 2. Hyperliquid builder dex

- [x] 2.1 先写失败测试：默认永续 dex 列表包含 `xyz`
- [x] 2.2 把 `xyz` 加入 `DEFAULT_PERP_DEXS` 并说明账户与保证金关系

## 3. 安全平仓退出

- [x] 3.1 先写失败测试：平仓成功后 `run_once` 只调用一次
- [x] 3.2 先写失败测试：初始已平时不调用 `run_once`
- [x] 3.3 先写失败测试：平仓失败会按既有机制重试且不打开新轮
- [x] 3.4 实现 `--close-and-exit`、立即到期、状态持久化与最终持仓摘要

## 4. 验证

- [x] 4.1 定向测试全绿
- [x] 4.2 `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q` 全绿
- [x] 4.3 OpenSpec 任务状态与实现、场景一致
