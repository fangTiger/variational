# 如何安全导出 Variational Omni 会话 Cookie

> 背景：Variational Omni 的官方交易 API 未开放，bot 通过复用你在浏览器登录后的**会话 Cookie**
> 调用私有接口 `/api/*`。你只需在浏览器用钱包登录一次（自动完成 SIWE 签名 + Cloudflare 验证码），
> 然后把会话 Cookie 导出给 bot。详见 `tools/recon/FINDINGS.md`。

---

## 一、安全须知（先读）

- **会话 Cookie 等同于你账户的临时登录凭证**，能下单/平仓。像对待私钥一样保管。
- 只存在本地 `.env` 或 `secrets/`（已在 `.gitignore` 忽略），**绝不提交 git、不发聊天、不截图**。
- Cookie 有有效期，过期后需重新导出。bot 检测到 `x-omni-auth: r` 或 401 会提示你刷新。
- 本操作只导出**你自己账户**的凭证用于**你自己的** bot，属正常使用；但自动化交易本身可能触及平台 ToS，风险自担。

---

## 二、准备：登录

1. 用 Chrome（推荐配 Rabby 钱包）打开 <https://omni.variational.io/>。
2. 点 **Connect Wallet**，用钱包签名完成登录（首次需邀请码）。
3. 确认已进入交易界面（能看到自己的持仓/余额），说明会话已建立。

---

## 三、导出 Cookie（三选一）

### 方法 A：从 DevTools 手动复制（最直接）

1. 在 Omni 页面按 `F12` 打开开发者工具 → **Application（应用）** 标签。
2. 左侧 **Storage → Cookies → https://omni.variational.io**。
3. 找到会话相关 Cookie（通常是 httpOnly 的那几条，名字可能含 `omni`/`auth`/`session`）。
   - 稳妥起见，把该域下**所有** Cookie 都记下来（name → value）。
4. 同时记下你的**钱包地址**（bot 需要它作为 `vr-connected-address` 头）。

### 方法 B：从 Network 请求头抓取（最可靠）

1. `F12` → **Network（网络）** 标签，勾选保留日志。
2. 在页面点一下刷新持仓，找到任意一条发往 `/api/...` 的请求。
3. 右键该请求 → **Copy → Copy as cURL**。
4. 把复制的 cURL 整段发给 bot/我，里面包含完整的 `Cookie:` 头和 `vr-connected-address` 头，
   我可以直接解析出会话（这是最省事、最不易出错的方式）。

### 方法 C：用浏览器扩展导出（批量）

- 安装受信任的 Cookie 导出扩展（如 "Cookie-Editor"），在 Omni 页面导出为 JSON。
- 只导出 `omni.variational.io` 域，保存为 `cookies.json`（已 gitignore）。

---

## 四、交给 bot 使用

把导出的内容放进项目根的 `.env`（不入库）：

```dotenv
# Variational 会话（从上面任一方法获得）
VARIATIONAL_COOKIE="name1=value1; name2=value2; ..."
VARIATIONAL_WALLET_ADDRESS="0x你的钱包地址"
```

或保存 `secrets/variational_cookies.json`：

```json
{
  "cookies": { "name1": "value1", "name2": "value2" },
  "wallet_address": "0x..."
}
```

bot 的 `adapters/variational_client.py` 会读取它，注入 httpx 客户端调用 `/api/*`。

---

## 五、验证是否有效

导出后，bot 会先打只读接口自检（不下任何单）：

```bash
python -m tools.check_variational_session   # 阶段二会提供
```

看到能正常返回你的 `/positions` 与 `/points/summary`，即表示会话有效。
若报 `VariationalAuthError`，说明 Cookie 过期或被 Cloudflare 拦截，回到第二步重新登录导出。

---

## 六、什么时候要重新导出

- bot 日志出现"会话已失效，请重新捕获 Cookie"。
- 距上次导出较久（保守估计几天到一两周，具体有效期待实测）。
- 你在别处主动登出了 Omni，或更换了钱包/设备。
