# 测试数据隔离与污染清理

## 已定位的问题

`tests/test_swap_carry_multi_leg.py` 的 `_guard_paths` 缺少 `switch_history_path`，预演走到 `run_once` 的生产默认路径。守护 CLI 同样缺少台账参数及传递。另有三个网格启动测试遗漏 `GridConfig.state_path`，会尝试写 `grid_live.json.tmp`，且异常被生产代码捕获，普通测试断言无法发现。

这些用例现在显式使用 `tmp_path`，守护 CLI 支持 `--switch-history`。没有修改 `grid/`、`timed_volume/`、Lighter 实现、`engine/hedge_engine.py` 或 `engine/risk.py`。

## 写入出口核对

| 数据 | 写入出口与注入点 |
| --- | --- |
| 切换历史、预演记录 | `run_swap_carry_guard.run_once(switch_history_path=...)`；CLI `--switch-history`，两种记录共用台账 |
| 守护心跳、审计 | `heartbeat_path`、`audit_path`；CLI `--heartbeat`、`--audit-log` |
| 补仓状态、每日尝试次数、退出计数、事故、预演窗口去重 | `state_path`；CLI `--state`，全部保存在同一状态文件 |
| 隔离保证金分配台账、锁、原子写入临时文件 | 从 `state_path` 派生 `.allocation.json`、`.lock`；底层写入器显式接收路径 |
| kill switch | `kill_switch_path`；守护只读，测试标记文件使用临时路径 |
| 资金方向 | `FundingDirectionStateStore(path=...)`；权益采集提供 `direction_state_file` |
| 权益、收益指标 | `snapshot_and_append(equity_file=...)`、`MetricsTracker(path=...)` |
| 组合权益、成交量 | `portfolio_equity` 的 `output` 和 CLI 输出路径 |
| carry、基差、价差采样 | `sample_swap_carry`、`sample_basis`、`sample_edge` 的输出路径参数 |
| 监控记录、首次权益基线 | `grid_monitor._record(path=...)`、`_snapshot(baseline_path=...)` |
| 告警冷却、归因报告 | `alert_check._save_cooldown(path=...)`、`pnl_attribution._write_result(path=...)` |
| 成交和缺口流 | `trade_collector._append(out_dir=...)`、`_append_gap(out_dir=...)` |
| 原始响应导出 | `dump_variational._run(output=...)`，CLI `--output` |
| 网格状态、实时快照、权益峰值、成交日志 | 现有 `GridConfig.state_path` 派生路径、`append_fill(path, ...)`；仅修复测试配置 |
| 归因 SQLite | 现有 `grid.attribution.store.connect(db_path)`；测试传临时数据库 |
| 定时交易状态、权益、轮次台账、心跳及锁 | 现有 `state_path`、`equity_path`、`ledger_path`、`heartbeat_path`、`scan_root`；未改实现 |
| Lighter 心跳、状态、订单序号 | 现有心跳 `path`、状态路径及 `client_order_index_path`；未改实现 |

新增 `infra.data_paths.data_dir()` 读取 `VARIATIONAL_DATA_DIR`，未配置时仍使用项目 `data/`。本次涉及的默认工具与 tracking 路径采用此目录；禁改模块继续使用已有显式注入点。

## 保护机制

1. `conftest.py` 在测试模块导入前设置临时数据根目录，避免默认参数在定义时绑定生产路径；自动 fixture 将已导入模块的路径常量重定向到当前 `tmp_path`。守护与 tracking 构造器在调用时解析默认路径。
2. Python 审计钩子在 collection 前安装，对真实 `data/` 的写入、替换、删除、SQLite 连接等直接报错，并保留违规调用位置。即使业务代码捕获异常，套件退出状态仍失败。
3. `pytest_sessionfinish` 在测试及 fixture 拆卸完毕后比较文件集合、mtime 和 SHA-256。保护自身的回归用子 pytest 证明：最后一个 fixture 留下差异时，即使所有普通断言通过，套件也退出失败。
4. 当前环境的两个生产心跳 `timed_volume_btc.jsonl`、`timed_volume_sndk_xyz.jsonl` 和 UTC 当日 `trades/YYYY-MM-DD.jsonl` 有独立进程持续写入，因此只对这三个文件豁免静态快照比较，并单独报告变化。**测试对这些文件的写入仍被拦截**；其余状态、权益、台账及历史文件不豁免。没有停止生产进程。

该机制保护 Python 测试进程；子进程默认继承临时数据目录。不要使用绕过 Python 审计的原生代码或子进程向真实数据文件写入。

## 一次性清理

默认只读扫描，逐条打印文件、行号、完整记录与理由：

```bash
.venv/bin/python tools/clean_test_pollution.py
```

确认输出后，显式执行：

```bash
.venv/bin/python tools/clean_test_pollution.py --apply
```

也可用 `--data-dir` 指定其他目录。命中条件为 `未配置调用`、`AssertionError`，或同一记录包含价格字段恰为 4000/4001 且 `equity` 恰为 1000。脚本保留未命中行的原始字节及非法 JSON，重写前独占创建 `原文件.jsonl.bak`，不覆盖已有备份。无命中记录时不重写。检测扫描后文件变化时中止；实际应用应在目标文件没有并发写入时进行。

本次只读扫描识别了 3 条污染记录，未执行 `--apply`。
