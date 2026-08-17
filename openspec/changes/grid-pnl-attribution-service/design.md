## Context

Task 1–5 已把成交、闭环和归因纯函数拆到 `grid.attribution`，Task 6 只负责旁路编排、定时配置与告警接入。真实成交从 2026-08-16 22:12 的引擎运行开始，但权益历史没有保存起点未实现盈亏；因此首份恒等式报告必须保留这个已知基线缺口，不能用配对或阈值调整把残差抹平。

## Goals / Non-Goals

**Goals**

- 每小时幂等导入本地 JSONL 和网格账户资金费，离线配对并落盘报告。
- 用同一份网格账户余额响应取得结束权益和未实现盈亏，真实调用 Task 5 恒等式。
- 让残差超阈值与满 4 周停止结论进入既有告警冷却链路。
- 提供可审查、由人安装的仓库 plist 和查看说明。

**Non-Goals**

- 不修改 Task 1–5 的表结构、配对算法、残差阈值或判据。
- 不实现 Task 7–9，不自动识别出入金，不创建正式 4 周起点。
- 不安装服务、不操作账户、不控制任何实盘进程。

## Decisions

### 观察窗口

若 `data/attribution_start.json` 存在则使用其 `start_ts`；否则从最早 `engine_run_id=run-<timestamp>` 推导，最后才回退到最早成交。权益起点取该时间后的第一条有效快照，闭环、资金费和现金流都按这条权益快照到结束账户快照的同一窗口过滤。

### 结束账户快照

使用 `ExtendedClient.from_env(prefix="X10_GRID")` 读取余额模型中的 `equity`、`unrealised_pnl` 与更新时间。资金费显式调用 `fetch_funding(market="BTC-USD", limit=500)`；该函数自身只读取 `X10_GRID_API_KEY`。测试全部替换这两个边界，不访问网络。

### 起点未实现盈亏

现有 `grid_monitor.jsonl` 只有权益、库存与价格，没有历史未实现盈亏或持仓均价。首轮报告把起点未实现盈亏按 0 传入 Task 5，同时写出 `unrealised_start_assumed_zero=true`。这会把未知起点基线如实留在 residual 中；后续正式起点应由未来任务保存完整基线，而不是在本任务推测并回填。

### 失败与落盘

账户快照不可用时输出 `identity_checked=false`、`residual=null` 的不可判定报告，禁止伪装为零残差。报告先写临时文件再原子替换，避免 `alert_check` 读到半份 JSON。SQLite 继续使用 Task 1–5 的主键幂等与 WAL。

### 告警优先级

同一报告同时含停止结论与缺口时先发 `verdict_stop`；否则在 `has_gap=true` 时发 `attribution_gap`。归因 JSON 缺失、损坏或顶层不是对象时忽略该分支，既有网格与 Lighter 告警继续工作。

## Risks / Trade-offs

- 首轮 residual 混有未知起点未实现盈亏；通过显式字段和完成报告说明，不宣称它是成交漏记。
- 余额、资金费和本地成交流不是事务快照；短时点差可能带来小额波动。报告记录起止时间，且每小时重算会自然收敛。
- `StartInterval=3600` 不是常驻服务；若单次执行超过一小时，launchd 的具体重叠行为由系统管理。当前数据量和实测耗时远低于一小时。

## Verification

- TDD 覆盖窗口过滤、非零残差、账户前缀、快照失败、两类告警、损坏 JSON、plist 和 README。
- 使用真实 `data/fills.jsonl` 执行正式 CLI，核对闭环、资金费、未实现盈亏与残差。
- 完成前运行全量 pytest、Python 编译、plist lint、OpenSpec strict validate 和 diff whitespace 检查。
