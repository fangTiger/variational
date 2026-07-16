# 阶段一侦察报告：Variational Omni 私有 API

- **日期**：2026-07-15
- **来源**：逆向 `https://omni.variational.io/` 前端 SvelteKit 打包代码（62 个 chunk，约 10MB）
- **状态**：链路已打通，可进入适配器实现

---

## 1. 核心结论（一句话）

Variational Omni 网页端调用同源 `/api/*` 私有接口，**认证靠 SIWE 登录后种下的 httpOnly 会话 Cookie**，之后所有下单/撤单/查询请求**都是纯 JSON，无需每笔钱包签名**。交易采用 **RFQ 报价模式**（先询价拿 `quote_id`，再 `accept` 成交）。前端受 **Cloudflare Turnstile + WAF** 保护。

**对 bot 的意义**：最稳的路线 = 让用户在浏览器用钱包登录一次（自动处理 SIWE + Turnstile），**捕获会话 Cookie**，bot 用该 Cookie 直接打 `/api/*` 下单，无需在代码里重写 SIWE/Turnstile。Cookie 过期后重新捕获（或用私钥本地重签 SIWE，但 Turnstile 仍需过验证）。

---

## 2. 请求基础设施

| 项 | 值 |
|---|---|
| API 基址 (`m_`) | `/api`（同源，实际 `https://omni.variational.io/api/...`，反代到后端 `omni-client-api.prod.*`） |
| 默认超时 (`l_`) | 30000 ms |
| 固定请求头 | `content-type: application/json`、`vr-connected-address: <钱包地址>` |
| 认证 | 会话 Cookie **`vr-token`**（JWT，有效期 **7 天**）+ `vr-connected-address` Cookie/头 |
| 会话状态响应头 | `x-omni-auth: r` → 表示需刷新；连续 401 + 该头达阈值触发重新登录 |
| 防护 | Cloudflare：`cf-mitigated: challenge` 表示被挑战；403 "Just a moment..." 为托管挑战 |
| **Cloudflare 关键约束（实测）** | `cf_clearance` 通行证**绑定 IP+UA**：从异 IP 复用 Cookie 会被弹挑战 403。→ bot 必须与浏览器**同公网 IP** 运行，Cookie 须含 `cf_clearance` |
| 请求器 | `Gi(method, path, body, headers)`；`GET=Ki`、`POST=Lt`；底层 `fetch(\`${'/api'}${path}\`, {...})` |

---

## 3. 认证流程（SIWE / EIP-4361）

```
1. POST /auth/generate_signing_data
   body: { address, [transfer_init_code] }
   → 返回待签名的 SIWE 消息

2. 钱包对消息签名 → signed_message

3. POST /auth/login
   body: { address, signed_message, [code(邀请码,大写)], captchaToken }
   headers（可选）: captcha 相关头
   → 成功后 Set-Cookie 种下会话 Cookie

4. 后续请求自动带 Cookie + vr-connected-address 头
   登出：POST /auth/logout  body:{ address }
```

- `captchaToken` = Cloudflare Turnstile 令牌，**这是自动化的主要障碍**。
- 首次登录需**邀请码**（`code`），从 Discord/Twitter 获取。

---

## 4. 端点清单（已确认）

### 交易（RFQ 报价 → 成交）
| 端点 | 方法 | 用途 | 请求体关键字段 |
|---|---|---|---|
| `/quotes/simple` | POST | 询价（简单） | `asset_class, instrument_type, dex_token_details, qty, side` |
| `/quotes/indicative` | POST | 指示性报价（轮询） | 同上 |
| `/quotes/accept` | POST | **接受报价=成交** | `quote_id, rfq_id, side, max_slippage, is_reduce_only` |
| `/orders/new/limit` | POST | 挂限价单 | `order_type, instrument, qty, side, limit, trigger_price` |
| `/orders/tpsl` | POST | 止盈止损 | `instrument, side` |
| `/orders/cancel` | POST | 撤单 | `rfq_id` |
| `/orders/close_all` | POST | 全部平仓 | — |
| `/orders/v2` | GET | 当前挂单列表 | — |

> **平仓** = 对持仓反向 `accept` 一个 `is_reduce_only:true` 的报价。

### 账户与查询
| 端点 | 方法 | 用途 |
|---|---|---|
| `/positions` | GET | 当前持仓 |
| `/trades` | GET | 成交历史 |
| `/funding/v2` | GET | 资金费率 |
| `/portfolio/trade_volume` | GET | 成交量统计 |
| `/v1/profile/account` | GET | 账户信息 |
| `/metadata/config` | GET | 平台配置 |
| `/metadata/supported_assets` | GET | 支持的资产 |
| `/metadata/tiers` | GET | 等级信息 |

### 积分与推荐（本项目重点）
| 端点 | 方法 | 用途 |
|---|---|---|
| `/points/summary` | GET | 积分汇总 |
| `/points/history` | GET | 积分历史 |
| `/points/multiplier_info` | GET | 加成信息 |
| `/points/next_drop_ts` | GET | 下次结算时间戳 |
| `/referrals/summary` | GET | 推荐汇总 |
| `/referrals/rewards_summary` | GET | 推荐奖励 |

### 存取款（bot 不需要，由用户手动完成）
`/auth/issue_transfer_init_code`、`/auth/issue_transfer_token`、`/auth/redeem_transfer_token`、`/v1/convert/*`（这些涉及链上交易，需钱包签名/合约交互）。

---

## 5. 关键技术障碍与对策

| 障碍 | 说明 | 对策 |
|---|---|---|
| Cloudflare Turnstile | 登录需 `captchaToken`，纯脚本难生成 | 浏览器登录一次捕获 Cookie；或用 Playwright 半自动过验证 |
| httpOnly 会话 Cookie | 无法用 JS 读取，只能靠浏览器/带 Cookie 的 HTTP 客户端 | 从浏览器导出 Cookie 注入 `requests`/`httpx` 的 cookie jar |
| Cloudflare WAF 封禁 | 高频/异常请求可能触发 403 封禁 | 控制频率、用真实 UA、模拟正常轮询节奏、保留 `vr-connected-address` 头 |
| Cookie 过期 | `x-omni-auth: r` 提示刷新，401 触发重登 | 检测该信号，触发重新捕获/重登流程 |
| 前端改版 | 端点/字段可能变化 | 侦察脚本可复跑；适配器与字段解析解耦 |

---

## 5.5 实测确认的字段（2026-07-16，curl_cffi 过 Cloudflare 后）

**架构验证成功**：curl_cffi（Chrome TLS 指纹伪装）+ 捕获的会话 Cookie（同 IP + 同 UA）
可稳定读取 `/api/*`，无需 Playwright。`x-omni-auth: r` 是正常已认证响应头（非失效信号）。

已确认的真实字段/参数：
- **BTC 永续标识**：`underlying=BTC` + `instrument_type=perpetual_future`。
- `GET /funding/v2?underlying=BTC&instrument_type=perpetual_future`
  → `{predicted_funding_rate, next_funding_time, funding_interval_s}`；BTC 周期 28800s(8h)。
- `GET /points/summary` → `{total_points, self_points, referral_points, rank}`。
- `GET /points/next_drop_ts` → `{next_drop_ts}`（每周五 0:00 UTC）。
- `GET /portfolio/trade_volume` → `{last_30d, all_time, own, referred, total, current_tier{id,name,points_rate}, boosted_tier}`。
- `GET /referrals/summary` → `{status, trade_volume{current,goal}, referred_by{address,code,points_boost}}`。
- `GET /points/history` → 每周窗口数组 `{total_points, self_points, referral_points, start_window, end_window}`。
- `GET /metadata/supported_assets` → 按符号索引的 dict（509 标的），每项数组含
  `{asset, price, index_price, funding_rate, funding_interval_s, open_interest{long,short}, ...}`。
- `GET /metadata/config` → `{min_order_notional:0.1, futures_taker_fee:0.0, transfer_fee:0.1,
  default_margin_params{futures_initial_margin:0.2, futures_maintenance_margin:0.1}, ...}`
  → **默认最大杠杆 5x（初始保证金 0.2）**，维持保证金 0.1。
- 仍 404/403：`/points/multiplier_info`（路径/方法待定，非关键）、`/v1/profile/account`（403，可能需额外头）。

## 6. 尚待验证的点（需一个真实持仓 / 一次真实下单）

以下需在**有真实会话 Cookie**时抓一次真实请求/响应确认（阶段二开头做）：
1. `/auth/login` 的 Set-Cookie 具体名称与属性、有效期。
2. `/quotes/simple` 与 `/quotes/accept` 的**完整请求体与响应体字段**（尤其 instrument 标识、qty 精度、`max_slippage` 单位）。
3. `/positions` 响应结构（方向、名义价值、保证金字段名）。
4. 频率限制的真实阈值（避免触发 WAF）。

---

## 7. 复现方式

```bash
# 重新抓取前端并刷新本报告依据
python3 tools/recon/fetch_bundle.py     # 下载最新 chunk 到 captured/
python3 tools/recon/scan_endpoints.py   # 扫描端点/认证/签名
```

> `captured/` 下的打包代码体积大且会随前端更新变化，已在 `.gitignore` 忽略，不入库。
