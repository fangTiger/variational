## 1. 失败路径测试（先写，必须确认 RED）

- [x] 1.1 Lighter 真实适配器从 `min_base_units=20`、`size_decimals=5` 返回 `0.00020`
- [x] 1.2 `0.00003` 微仓低于 `0.00020` → 不挂 TPSL、继续铺网格、不进硬止损 fail-safe
- [x] 1.3 持仓等于最小下单量 → 保留现有 TPSL 下单与失败阻塞行为
- [x] 1.4 持仓超过最小下单量 → 保留现有 TPSL 行为
- [x] 1.5 最小下单量查询失败 → 仍尝试 TPSL，且硬止损继续既有 fail-safe
- [x] 1.6 硬止损与 TPSL 同轮读取最小量时只调用一次适配器；后续轮次复用成功缓存
- [x] 1.7 微仓日志为中文且相同微仓状态限频
- [x] 1.8 运行定向测试，确认因最小量能力和微仓判断缺失而预期失败

## 2. 最小实现

- [x] 2.1 在统一适配器契约增加 `get_min_order_size`
- [x] 2.2 核对 Extended 真实适配器方法读取 `trading_config.min_order_size` 并复用市场缓存
- [x] 2.3 Lighter 真实适配器从 `_load_market_meta` 的 `min_base_units` 与 `size_decimals` 换算基础资产数量
- [x] 2.4 网格引擎增加有效最小量缓存和查询失败短时缓存；无效结果按失败处理
- [x] 2.5 `_maintain_tpsl` 对已确认微仓跳过 TPSL、返回成功并限频记录中文日志
- [x] 2.6 `_check_hard_stop` 对已确认微仓按零返回，不读取清算信息
- [x] 2.7 运行定向测试并保持全绿

## 3. 验证

- [x] 3.1 `openspec validate fix-dust-position-tpsl-deadlock --strict`
- [x] 3.2 定向测试覆盖全部新增与回归场景
- [x] 3.3 回放 `0.00003 BTC` + `min_base_units=20`，断言网格照常挂单
- [x] 3.4 完整 `PYTHONPATH=. .venv/bin/python -m pytest tests/` 无回归（基线 794，完成后 802 passed）
- [x] 3.5 核对 tasks 与实现、spec 场景一致
- [x] 3.6 确认本 change 未触碰已有 plist 用户改动、未改风控阈值/网格策略/对冲算法、未访问网络、未提交推送
