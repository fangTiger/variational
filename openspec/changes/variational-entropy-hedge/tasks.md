## 1. 适配器能力声明
- [x] 1.1 `adapters/base.py` 增加 `execution_model` 属性,默认 `"orderbook"`
- [x] 1.2 `VariationalClient` 声明 `execution_model = "rfq"`
- [x] 1.3 实现 `VariationalClient.get_min_order_size`(先写失败测试:未实现时策略应拒绝启动而非静默下 0 单)
- [x] 1.4 实现 `VariationalClient.round_amount`,按 Variational 的 qty 精度对齐

## 2. 执行路径分流
- [x] 2.1 先写失败测试:RFQ 适配器(无 `place_limit_order`)走 `maker_first_hedge` 应抛 AttributeError —— 复现当前 bug
- [x] 2.2 `engine/hedge_engine.py` 增加 RFQ 分支,直接 `market_order` 并返回 `used_taker=True`
- [x] 2.3 `_check_hedge_available` 的能力清单按 `execution_model` 区分
- [x] 2.4 验证两腿模型不同时仍并发提交(不得退化为串行)

## 3. 认证自愈
- [ ] 3.1 先写失败测试:主腿抛认证异常时,当前实现会让整轮崩溃
- [ ] 3.2 `timed_volume` 增加 `auth_error_types` 与 `on_auth_error` 钩子
- [ ] 3.3 重载失败时置互锁 + 告警,不得继续开仓
- [ ] 3.4 测试:一腿成交后另一腿认证失效 → 必须回滚,不留单边仓

## 4. 账户隔离
- [ ] 4.1 `tools/run_timed_volume.py` 增加 `--primary-venue {lighter,variational}`
- [ ] 4.2 增加 `--hedge-env-prefix`,默认 `HYPERLIQUID`
- [ ] 4.3 启动校验:两腿账户地址不得相同(跨平台时天然不同,同平台必须拦)
- [ ] 4.4 状态文件与日志按实例名隔离,避免两套策略互相覆写 `state.json`

## 5. 验证
- [ ] 5.1 全量单测通过,且现有 Lighter×Entropy 路径行为零变化
- [ ] 5.2 dry-run 打印两腿配置摘要,人工核对账户不同
- [ ] 5.3 小额实盘:单轮开仓 → 确认两侧净敞口 ≈ 0 → 平仓
- [ ] 5.4 确认 Variational 成交计入积分、Entropy 成交带 `builderFee`
