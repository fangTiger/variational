# Change: Variational × Entropy 跨平台定时定量对冲

## Why

当前定时定量对冲跑的是 Lighter × Hyperliquid(Entropy)。用户希望把 Variational
也纳入刷量,主腿换成 Variational、对冲腿仍是 Entropy。

复用 `timed_volume` 策略需要解决三个已核实的阻塞点:

1. **Variational 是纯 RFQ 模型,没有限价单**。而 `maker_first_hedge`
   (`engine/hedge_engine.py:463`)无条件调用 `adapter.place_limit_order`,
   `VariationalClient` 没有这个方法 → `AttributeError` 不属于 post-only 拒绝,
   会在 `:473` 原样抛出,主腿必然失败。
2. **`get_min_order_size` 未实现**,基类抛 `NotImplementedError`
   (`adapters/base.py:132`),策略 `_get_order_limits` 取不到下单下限。
3. **Entropy 侧必须换独立账户**。现有 Lighter×Entropy 系统正在实盘运行,
   两套策略若共用同一个 Hyperliquid 账户,会各自 `get_position("BTC")`
   读到对方的仓位并互相冲销 —— 双方都会误判为"已对冲"或"超额对冲"。

另有一个运行期约束:Variational 用浏览器 Cookie 授权,会话会过期
(本次实测已 HTTP 401)。`HedgeEngine` 有 `_on_auth_error` 重载钩子,
但 `timed_volume` 策略是独立实现,**没有等价机制**。

## What Changes

- **BREAKING(内部契约)**:`TimedVolumeStrategy._execute` 由"两腿都走
  maker-first"改为**按腿选择执行器**。RFQ 类适配器走纯市价路径,
  订单簿类适配器保持 maker-first 不变。
- `VariationalClient` 补齐 `get_min_order_size`、`round_amount`,
  并声明自身为 RFQ 执行模型(新增能力标记)。
- `timed_volume` 增加认证失效重载钩子,复用 `run_hedge_bot` 的
  `_reload_primary` 模式,401 时重读 `.env` 重建客户端而非整体崩溃。
- `tools/run_timed_volume.py` 新增 `--primary-venue {lighter,variational}`
  与 `--hedge-env-prefix`,后者用于指向第二套 Hyperliquid 凭据。
- `_check_hedge_available` 的能力清单按执行模型区分,不再硬性要求
  `place_limit_order`。

## Impact

- Affected specs: `timed-hedged-volume`(修改)、`variational-adapter`(新增)
- Affected code: `timed_volume/strategy.py`、`engine/hedge_engine.py`、
  `adapters/variational_client.py`、`adapters/base.py`、
  `tools/run_timed_volume.py`
- **不影响正在运行的 Lighter × Entropy 系统**:默认参数保持现状,
  新路径必须显式指定 `--primary-venue variational` 才启用。
