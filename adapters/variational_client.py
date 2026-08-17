"""Variational Omni 私有 API 客户端（异步，实现统一适配器接口）。

认证方式：复用从浏览器捕获的会话 Cookie（SIWE 登录 + Cloudflare Turnstile 由浏览器完成），
本客户端只负责用该 Cookie 调用同源 /api/* 接口。详见 tools/recon/FINDINGS.md。

⚠️ 状态：只读接口（持仓/积分/资金费）可用；**报价/下单的完整请求体字段**
需在拿到真实会话 Cookie 后抓一次真实请求确认（FINDINGS.md 第 6 节），
标 TODO 的方法在确认前会抛 NotImplementedError，禁止实盘下单。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

# curl_cffi 提供 Chrome TLS 指纹伪装，用于绕过 Cloudflare 的 TLS 指纹检测。
from curl_cffi.requests import AsyncSession

from adapters.base import ExchangeAdapter, MarketPrice, Position, Side

BASE_URL = "https://omni.variational.io/api"
DEFAULT_TIMEOUT = 30.0
# TLS 指纹伪装目标（curl_cffi）。"chrome" 取最新 Chrome 指纹；可用 VARIATIONAL_IMPERSONATE 覆盖。
DEFAULT_IMPERSONATE = "chrome"
# 默认 UA 对齐常见 Chrome。⚠️ Cloudflare 的 cf_clearance 绑定「获取它时的 UA」，
# 必须与你导出 Cookie 的那个浏览器 UA 一致，否则会被弹挑战。
# 如与你的浏览器不同，用 VARIATIONAL_USER_AGENT 环境变量覆盖。
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class VariationalAuthError(Exception):
    """会话失效（401 / Cloudflare 挑战），需重新捕获 Cookie。"""


class VariationalJurisdictionError(Exception):
    """当前 IP 所在地区被禁止执行该操作（下单等）。读取不受影响。"""


class VariationalRequestError(Exception):
    """接口返回非 2xx。"""

    def __init__(self, status: int, message: str, data: Any = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.data = data


@dataclass
class Session:
    """一次登录会话所需的凭证。

    cookies: 浏览器登录后种下的会话 Cookie（name -> value）。
    wallet_address: 钱包地址，作为 vr-connected-address 头发送。
    """

    cookies: dict[str, str]
    wallet_address: str
    user_agent: str | None = None  # 须与导出 Cookie 的浏览器 UA 一致

    @classmethod
    def from_env(cls) -> "Session":
        """从环境变量加载。

        VARIATIONAL_COOKIE（"k=v; k2=v2"，须含 cf_clearance 与 vr-token）
        VARIATIONAL_WALLET_ADDRESS
        VARIATIONAL_USER_AGENT（可选，默认对齐 Chrome/149）
        """
        cookie_str = os.getenv("VARIATIONAL_COOKIE", "")
        addr = os.getenv("VARIATIONAL_WALLET_ADDRESS", "")
        if not cookie_str or not addr:
            raise VariationalAuthError(
                "缺少 VARIATIONAL_COOKIE / VARIATIONAL_WALLET_ADDRESS 环境变量"
            )
        return cls(
            cookies=_parse_cookie_header(cookie_str),
            wallet_address=addr,
            user_agent=os.getenv("VARIATIONAL_USER_AGENT") or None,
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "Session":
        """从 JSON 文件加载：{"cookies": {...}, "wallet_address": "0x...", "user_agent": "..."}。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cookies = data["cookies"]
        # 兼容 Cookie-Editor 导出的数组格式
        if isinstance(cookies, list):
            cookies = {c["name"]: c["value"] for c in cookies}
        return cls(
            cookies=cookies,
            wallet_address=data["wallet_address"],
            user_agent=data.get("user_agent"),
        )


class VariationalClient(ExchangeAdapter):
    """基于捕获会话 Cookie 的 Omni 私有 API 客户端（异步）。"""

    name = "variational"

    def __init__(
        self,
        session: Session,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        impersonate: str | None = None,
    ) -> None:
        self._session = session
        self._impersonate = (
            impersonate or os.getenv("VARIATIONAL_IMPERSONATE") or DEFAULT_IMPERSONATE
        )
        # 市价单最大滑点（f64 数字，分数：0.01=1%）。accept 按 quote_id 锁价成交，实际滑点≈0。
        self._max_slippage = float(os.getenv("VARIATIONAL_MAX_SLIPPAGE") or "0.01")
        self._timeout = timeout
        self._headers = {
            "user-agent": session.user_agent or USER_AGENT,
            "content-type": "application/json",
            "accept": "*/*",
            "referer": "https://omni.variational.io/portfolio?tab=positions",
            "vr-connected-address": session.wallet_address,
        }
        self._http = AsyncSession(
            timeout=timeout,
            cookies=session.cookies,
            headers=self._headers,
            impersonate=self._impersonate,
        )

    async def connect(self) -> None:
        """无需握手；调只读接口自检会话是否有效。"""
        await self.get_positions()

    # ---- 底层请求器（对应前端 Gi/Ki/Lt，走 curl_cffi Chrome 指纹）----

    async def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        resp = await self._http.request(method, BASE_URL + path, json=body)
        headers = resp.headers
        # 注意：x-omni-auth: r 是正常已认证响应上的头（前端用它重置重试计数），不是失效信号。
        if headers.get("cf-mitigated") == "challenge":
            raise VariationalAuthError("触发 Cloudflare 挑战，需重新过验证")
        if resp.status_code == 401:
            raise VariationalAuthError("会话被拒绝 (HTTP 401)，请重新捕获 Cookie")
        if resp.status_code == 403:
            data = _safe_json(resp)
            emsg = (data or {}).get("error_message", "") if isinstance(data, dict) else ""
            if "jurisdiction" in emsg.lower() or "restricted" in emsg.lower():
                raise VariationalJurisdictionError(
                    f"当前 IP 地区被禁止该操作：{emsg or '(restricted jurisdiction)'}"
                )
            raise VariationalAuthError("被拒 (HTTP 403)，可能是 Cloudflare 拦截")
        if not (200 <= resp.status_code < 300):
            data = _safe_json(resp)
            msg = ""
            if isinstance(data, dict):
                # Variational 错误体用 error_message 字段（不是 message）
                msg = data.get("error_message") or data.get("message") or ""
            if not msg:
                msg = (resp.text or "")[:300]
            raise VariationalRequestError(resp.status_code, msg, data)
        return _safe_json(resp)

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def raw(self, path: str) -> Any:
        """对任意只读端点发 GET，返回原始 JSON（侦察/监控用）。"""
        return await self._get(path)

    async def _post(self, path: str, body: dict | None = None) -> Any:
        return await self._request("POST", path, body)

    # ---- 只读查询（可用）----

    async def get_positions(self) -> Any:
        """全部持仓（原始结构）。"""
        return await self._get("/positions")

    async def get_funding(
        self, underlying: str = "BTC", instrument_type: str = "perpetual_future"
    ) -> Any:
        """资金费率。/funding/v2 需要 underlying + instrument_type 两个查询参数。

        返回形如 {predicted_funding_rate, next_funding_time, funding_interval_s}。
        费率为每 funding_interval_s（BTC=28800s=8h）的百分比；正=多头付空头。
        """
        return await self._get(
            f"/funding/v2?underlying={underlying}&instrument_type={instrument_type}"
        )

    async def get_funding_rate(
        self, underlying: str = "BTC", instrument_type: str = "perpetual_future"
    ) -> Decimal:
        """便捷方法：直接返回预测资金费率（Decimal）。"""
        data = await self.get_funding(underlying, instrument_type)
        return Decimal(str(data["predicted_funding_rate"]))

    async def get_open_orders(self) -> Any:
        """当前挂单。"""
        return await self._get("/orders/v2")

    async def get_points_summary(self) -> Any:
        """积分汇总：{total_points, self_points, referral_points, rank}。"""
        return await self._get("/points/summary")

    async def get_total_points(self) -> Decimal:
        """便捷方法：账户累计总积分。"""
        data = await self.get_points_summary()
        return Decimal(str(data["total_points"]))

    async def get_config(self) -> Any:
        """平台配置（费用/保证金/精度）。"""
        return await self._get("/metadata/config")

    async def get_supported_assets(self) -> Any:
        """全部支持标的（按符号索引的 dict，含 price/index_price/funding 等）。"""
        return await self._get("/metadata/supported_assets")

    # ---- 统一接口实现 ----

    @staticmethod
    def _position_matches_underlying(
        info: dict[str, Any], underlying: str, *, exact: bool = False
    ) -> bool:
        """判断持仓是否属于目标 underlying；默认保留旧的子串匹配行为。"""
        target = underlying.upper()
        instrument = info.get("instrument", info.get("underlying", ""))
        if not exact:
            return target in str(instrument).upper()

        candidates: list[str] = []
        if isinstance(instrument, dict):
            for key in ("underlying", "asset", "base_asset"):
                value = instrument.get(key)
                if value:
                    candidates.append(str(value))
        else:
            text = str(instrument).upper()
            if text == target:
                return True
            candidates.extend(
                token for token in re.split(r"[^A-Z0-9]+", text) if token
            )

        direct_underlying = info.get("underlying")
        if direct_underlying and not isinstance(direct_underlying, dict):
            candidates.append(str(direct_underlying))

        return any(candidate.upper() == target for candidate in candidates)

    async def get_position(
        self, underlying: str = "BTC", *, exact: bool = False
    ) -> Position:
        """获取某标的持仓并归一化为有符号数量（qty 本身带符号：>0 多，<0 空）。

        market 参数传 underlying（如 "BTC"）。当前账户无持仓时返回 signed_size=0。
        exact=True 时按 instrument.underlying 精确匹配，避免 XAU 误命中 XAUT。
        TODO(有持仓后确认): /positions 填充后核对 instrument/qty 的确切字段与嵌套。
        前端侦察显示结构含 position_info.instrument 与 qty。
        """
        data = await self.get_positions()
        items = data if isinstance(data, list) else (data or {}).get("positions", [])
        for p in items:
            info = p.get("position_info", p)
            if self._position_matches_underlying(info, underlying, exact=exact):
                qty = Decimal(str(info.get("qty", info.get("size", "0"))))
                return Position(market=underlying, signed_size=qty, raw=p)
        return Position(market=underlying, signed_size=Decimal(0))

    async def get_market_price(self, market: str) -> MarketPrice:
        """获取买一/卖一价（用一次极小询价拿 bid/ask）。"""
        q = await self.request_quote(market, "buy", Decimal("0.0001"))
        return MarketPrice(market, Decimal(str(q["bid"])), Decimal(str(q["ask"])))

    async def get_liquidation_info(
        self, market: str, maint: Decimal = Decimal("0.1"), *, exact: bool = False
    ) -> tuple[Decimal, Decimal] | None:
        """计算 (mark, liquidation_price)。Variational 不直接给清仓价，用维持保证金(10%)推算。

        清仓条件：equity + s·(P−M) = maint·|s|·P  → P = (s·M − E) / (s − maint·|s|)
        （s 为有符号数量，空头 s<0；E=balance+upnl；M=标记价）
        """
        data = await self.get_positions()
        items = data if isinstance(data, list) else (data or {}).get("positions", [])
        pos = None
        for p in items:
            info = p.get("position_info", p)
            if self._position_matches_underlying(info, market, exact=exact):
                pos = p
                break
        if not pos:
            return None
        info = pos["position_info"]
        s = Decimal(str(info["qty"]))
        if s == 0:
            return None
        price_info = pos.get("price_info", {})
        mark = Decimal(str(price_info.get("underlying_price") or info.get("avg_entry_price")))
        port = await self.raw("/portfolio")
        equity = Decimal(str(port["balance"])) + Decimal(str(port.get("upnl", "0")))
        denom = s - maint * abs(s)
        if denom == 0:
            return None
        liq = (s * mark - equity) / denom
        if liq <= 0:
            return None
        return mark, liq

    @staticmethod
    def _instrument(
        underlying: str,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ) -> dict:
        """构造永续 instrument 描述符；默认值保持原 BTC 流程不变。

        RWA 永续（perpetual_rwa_future，如 XAU）后端 schema 额外要求 kind 字段，
        取值 = 该资产的 asset_class（黄金=commodity），与前端构造一致；
        缺失会被后端拒绝：HTTP 400 missing field `kind`。非 RWA 传 None 即不带该字段。
        """
        instrument = {
            "funding_interval_s": funding_interval_s,
            "instrument_type": instrument_type,
            "settlement_asset": "USDC",
            "underlying": underlying,
        }
        if kind is not None:
            instrument["kind"] = kind
        return instrument

    async def request_quote(
        self,
        underlying: str,
        side: str,
        qty: Decimal,
        *,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ) -> Any:
        """询价。side ∈ {buy, sell}。返回含 quote_id/bid/ask/mark_price/margin_requirements。

        用 /quotes/indicative：它按用户注册可执行报价（含保证金计算），其 quote_id 可用于
        /quotes/accept 成交。/quotes/simple 是无状态价格预览，quote_id 不可成交。
        kind：RWA 永续需传（如 XAU=commodity），其余传 None。
        """
        body = {
            "instrument": self._instrument(
                underlying,
                instrument_type=instrument_type,
                funding_interval_s=funding_interval_s,
                kind=kind,
            ),
            "qty": str(qty),
            "side": side,
        }
        return await self._post("/quotes/indicative", body)

    async def accept_quote(
        self, *, quote_id: str, side: str, max_slippage: float, is_reduce_only: bool
    ) -> Any:
        """接受报价成交（真正下单）。max_slippage 必须是 JSON 数字(f64)。返回含 rfq_id。"""
        body = {
            "quote_id": quote_id,
            "side": side,
            "max_slippage": float(max_slippage),
            "is_reduce_only": is_reduce_only,
        }
        return await self._post("/quotes/accept", body)

    async def market_order(
        self,
        market: str,
        side: Side,
        amount: Decimal,
        *,
        reduce_only: bool = False,
        instrument_type: str = "perpetual_future",
        funding_interval_s: int = 3600,
        kind: str | None = None,
    ):
        """RFQ 市价成交：/quotes/simple 询价 → /quotes/accept 成交。

        market 传 underlying（如 "BTC"）。amount 为合约数量（BTC 个数）。
        kind：RWA 永续需传（如 XAU=commodity），其余传 None。
        """
        s = "buy" if side is Side.BUY else "sell"
        quote = await self.request_quote(
            market,
            s,
            amount,
            instrument_type=instrument_type,
            funding_interval_s=funding_interval_s,
            kind=kind,
        )
        return await self.accept_quote(
            quote_id=quote["quote_id"],
            side=s,
            max_slippage=self._max_slippage,
            is_reduce_only=reduce_only,
        )

    async def cancel_order(self, market: str, order_id) -> Any:
        """撤单。Variational 的 RFQ 编号全局唯一，market 仅为满足统一契约。"""
        del market
        return await self._post("/orders/cancel", {"rfq_id": order_id})

    async def close(self) -> None:
        await self._http.close()


def _parse_cookie_header(cookie_str: str) -> dict[str, str]:
    """解析 "k1=v1; k2=v2" 形式的 Cookie 头为字典。"""
    out: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _safe_json(resp: Any) -> Any:
    """尽力解析 JSON，失败返回 None（兼容 curl_cffi Response）。"""
    if "application/json" in (resp.headers.get("content-type") or ""):
        try:
            return resp.json()
        except Exception:  # noqa: BLE001 curl_cffi 解析失败
            return None
    return None
