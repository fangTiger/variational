## 1. 契约与真实边界

- [x] 1.1 核对 `hyperliquid-python-sdk==0.24.0` 的 `Exchange.order` 与
  `Exchange.bulk_orders` builder 参数签名和传递路径
- [x] 1.2 严格校验本 change 的 proposal、spec delta 与 tasks

## 2. 权益公式（严格 TDD）

- [x] 2.1 先补失败测试并确认 RED：空仓、实测持仓、浮亏、多个持仓、排除非 USDC
- [x] 2.2 先补失败测试并确认 RED：任一必需查询失败、结构/字段无效、合计非正
- [x] 2.3 最小实现 Spot USDC + 全部持仓未实现盈亏，并返回三个诊断字段
- [x] 2.4 运行权益定向测试并确认 GREEN

## 3. Builder Code（严格 TDD）

- [x] 3.1 先补失败测试并确认 RED：默认不传、限价/市价均传、费率 0 合法
- [x] 3.2 先补失败测试并确认 RED：地址非法、费率为负、超永续上限
- [x] 3.3 最小实现构造/环境变量配置校验与两条下单路径的条件透传
- [x] 3.4 用真实 SDK 签名约束测试桩，并运行 builder 定向测试确认 GREEN

## 4. 验证

- [x] 4.1 回放空仓 457.31 与持仓 498.20 + 5.47 = 503.67
- [x] 4.2 运行 Hyperliquid 定向测试与完整 `tests/`，确认无回归
- [x] 4.3 运行语法检查及 `openspec validate fix-hyperliquid-equity-builder-code --strict`
- [x] 4.4 核对 tasks 与实际完成情况，并确认未越界修改运行时或其他适配器
