"""Variational Omni 私有 API 客户端（阶段一骨架）。

认证方式：复用从浏览器捕获的会话 Cookie（SIWE 登录 + Cloudflare Turnstile 由浏览器完成），
本客户端只负责用该 Cookie 调用同源 /api/* 接口。详见 tools/recon/FINDINGS.md。

⚠️ 阶段一状态：端点与请求器已按逆向结论搭好，但报价/下单的**完整请求体字段**
需在拿到真实会话 Cookie 后抓一次真实请求确认（见 FINDINGS.md 第 6 节），
标注 TODO 的字段目前是占位，禁止直接实盘下单。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://omni.variational.io/api"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class VariationalAuthError(Exception):
    """会话失效（401 / x-omni-auth 提示刷新），需重新捕获 Cookie。"""


class VariationalRequestError(Exception):
    """接口返回非 2xx。"""

    def __init__(self, status: int, message: str, data: Any = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.data = data


@dataclass
class Session:
    """一次登录会话所需的凭证。

    cookies: 从浏览器导出的会话 Cookie（登录后 Set-Cookie 种下的 httpOnly Cookie）。
    wallet_address: 钱包地址，作为 vr-connected-address 头发送。
    """

    cookies: dict[str, str]
    wallet_address: str


class VariationalClient:
    """基于捕获会话 Cookie 的 Omni 私有 API 客户端。"""

    def __init__(self, session: Session, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._session = session
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            cookies=session.cookies,
            headers={
                "user-agent": USER_AGENT,
                "content-type": "application/json",
                "vr-connected-address": session.wallet_address,
            },
        )

    # ---- 底层请求器（对应前端的 Gi/Ki/Lt）----

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        """发起请求并处理认证/错误。"""
        resp = self._http.request(method, path, json=body)
        # 前端约定：响应头 x-omni-auth == "r" 表示会话需刷新
        if resp.headers.get("x-omni-auth") == "r" or resp.status_code == 401:
            raise VariationalAuthError("会话已失效，请重新捕获 Cookie")
        if resp.headers.get("cf-mitigated") == "challenge":
            raise VariationalAuthError("触发 Cloudflare 挑战，需重新过验证")
        if not resp.is_success:
            data = _safe_json(resp)
            msg = (data or {}).get("message") or resp.text[:200]
            raise VariationalRequestError(resp.status_code, msg, data)
        return _safe_json(resp)

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    def _post(self, path: str, body: dict | None = None) -> Any:
        return self._request("POST", path, body)

    # ---- 只读查询 ----

    def get_positions(self) -> Any:
        """当前持仓。"""
        return self._get("/positions")

    def get_funding(self) -> Any:
        """资金费率。"""
        return self._get("/funding/v2")

    def get_open_orders(self) -> Any:
        """当前挂单。"""
        return self._get("/orders/v2")

    def get_points_summary(self) -> Any:
        """积分汇总。"""
        return self._get("/points/summary")

    def get_config(self) -> Any:
        """平台配置（含标的元数据）。"""
        return self._get("/metadata/config")

    def get_supported_assets(self) -> Any:
        """支持的资产。"""
        return self._get("/metadata/supported_assets")

    # ---- 交易（RFQ 报价 → 成交）----
    # ⚠️ 以下请求体字段为占位，需用真实会话抓包确认后再启用。

    def request_quote(self, instrument: str, side: str, qty: str) -> Any:
        """询价，返回含 quote_id / rfq_id 的报价。

        TODO(阶段二): 用真实抓包确认 /quotes/simple 的完整字段
        （asset_class、instrument_type、dex_token_details 等）。
        """
        raise NotImplementedError("待抓包确认 /quotes/simple 请求体后实现")

    def accept_quote(
        self,
        *,
        quote_id: str,
        rfq_id: str,
        side: str,
        max_slippage: str,
        is_reduce_only: bool = False,
    ) -> Any:
        """接受报价成交（开/平仓）。字段依据逆向结论，需抓包最终确认。"""
        body = {
            "quote_id": quote_id,
            "rfq_id": rfq_id,
            "side": side,
            "max_slippage": max_slippage,
            "is_reduce_only": is_reduce_only,
        }
        return self._post("/quotes/accept", body)

    def cancel_order(self, rfq_id: str) -> Any:
        """撤单。"""
        return self._post("/orders/cancel", {"rfq_id": rfq_id})

    def close_all(self) -> Any:
        """全部平仓。"""
        return self._post("/orders/close_all")

    def close(self) -> None:
        self._http.close()


def _safe_json(resp: httpx.Response) -> Any:
    """尽力解析 JSON，失败返回 None。"""
    if "application/json" in resp.headers.get("content-type", ""):
        try:
            return resp.json()
        except ValueError:
            return None
    return None
