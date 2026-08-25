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
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

# curl_cffi 提供 Chrome TLS 指纹伪装，用于绕过 Cloudflare 的 TLS 指纹检测。
from curl_cffi.requests import AsyncSession

from adapters.base import ExchangeAdapter, MarketPrice, Position, PositionPnl, Side

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


@dataclass(frozen=True)
class _QuantityLimits:
    """某标的某方向的下单数量约束。

    对应报价响应里的 ``qty_limits.bid`` / ``qty_limits.ask``。
    三项都可能缺失：minimum 缺失必须拒绝交易，另外两项缺失只是降级。
    """

    minimum: Decimal | None
    maximum: Decimal | None
    step: Decimal | None


class VariationalRequestError(Exception):
    """接口返回非 2xx。"""

    def __init__(self, status: int, message: str, data: Any = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.data = data


@dataclass(frozen=True)
class VariationalBalance:
    """供通用引擎读取的 Variational 权益及其组成。"""

    equity: Decimal
    balance: Decimal
    upnl: Decimal


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
    execution_model = "rfq"

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
        # 按 (market, side) 缓存数量约束：读取一次要打一发真实 RFQ 询价。
        self._quantity_limits: dict[tuple[str, str | None], "_QuantityLimits"] = {}
        # 标的分类元数据与股票判定均按进程缓存，避免每次 RFQ 前重复读取。
        self._supported_assets_loaded = False
        self._supported_assets_metadata: Any = None
        self._equity_market_cache: dict[str, bool] = {}
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

    async def get_balance(self) -> VariationalBalance:
        """从 portfolio 返回余额、未实现盈亏与两者之和。"""
        data = await self._get("/portfolio")
        if not isinstance(data, dict):
            raise ValueError("Variational portfolio 响应不是对象")
        if data.get("balance") is None:
            raise ValueError("Variational portfolio 缺少 balance")
        if data.get("upnl") is None:
            raise ValueError("Variational portfolio 缺少 upnl")
        try:
            balance = Decimal(str(data["balance"]))
            upnl = Decimal(str(data["upnl"]))
            equity = balance + upnl
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("Variational portfolio 权益字段不是有效十进制数") from exc
        if not balance.is_finite() or not upnl.is_finite() or not equity.is_finite():
            raise ValueError("Variational portfolio 权益字段必须为有限数")
        return VariationalBalance(equity=equity, balance=balance, upnl=upnl)

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
    def _decimal_or_none(value: Any) -> Decimal | None:
        """把元数据字段解析成正数 Decimal；缺失、非法或非正数一律返回 None。"""
        if value in (None, ""):
            return None
        try:
            parsed = Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None
        if not parsed.is_finite() or parsed <= 0:
            return None
        return parsed

    async def _get_quantity_limits(
        self,
        market: str,
        side: Side | None = None,
    ) -> _QuantityLimits:
        """读取并缓存某标的某方向的数量约束。

        结构取自前端实现：``qty_limits.bid`` 与 ``qty_limits.ask`` 各自带
        ``min_qty`` / ``max_qty`` / ``min_qty_tick``。前端按方向取值——
        买单读 bid 侧、卖单读 ask 侧，本实现与之保持一致。

        side 为 None 时（基类单参数契约的调用方）取两侧中**更严格**的组合：
        最小量取较大者、步长取较大者、上限取较小者。宁可少下也不要超限。
        """
        cache_key = (market, side.value if side is not None else None)
        if cache_key in self._quantity_limits:
            return self._quantity_limits[cache_key]

        # 询价本身是一次真实 RFQ 调用，因此按 (market, side) 缓存，进程内只打一次。
        quote = await self.request_quote(market, "buy", Decimal("0.0001"))
        quote_data = quote if isinstance(quote, dict) else {}
        qty_limits = quote_data.get("qty_limits")
        if not isinstance(qty_limits, dict):
            limits = _QuantityLimits(None, None, None)
            self._quantity_limits[cache_key] = limits
            return limits

        if side is Side.BUY:
            wanted = ("bid",)
        elif side is Side.SELL:
            wanted = ("ask",)
        else:
            wanted = ("bid", "ask")

        minimums: list[Decimal] = []
        maximums: list[Decimal] = []
        steps: list[Decimal] = []
        for key in wanted:
            entry = qty_limits.get(key)
            if not isinstance(entry, dict):
                continue
            if (value := self._decimal_or_none(entry.get("min_qty"))) is not None:
                minimums.append(value)
            if (value := self._decimal_or_none(entry.get("max_qty"))) is not None:
                maximums.append(value)
            if (value := self._decimal_or_none(entry.get("min_qty_tick"))) is not None:
                steps.append(value)

        limits = _QuantityLimits(
            minimum=max(minimums) if minimums else None,
            maximum=min(maximums) if maximums else None,
            step=max(steps) if steps else None,
        )
        self._quantity_limits[cache_key] = limits
        return limits

    async def get_min_order_size(
        self, market: str, side: Side | None = None
    ) -> Decimal:
        """返回 RFQ 报价声明的最小下单量；未知时明确拒绝交易。

        下限缺失必须失败关闭：不知道下限就可能提交低于门槛的单，
        被拒后策略会误判成「已下单」，进而留下单边裸仓。绝不编造默认值。
        """
        limits = await self._get_quantity_limits(market, side)
        if limits.minimum is None:
            raise RuntimeError(
                "Variational 报价未提供 qty_limits.min_qty，已拒绝按未知下限交易"
            )
        return limits.minimum

    async def get_max_order_size(
        self, market: str, side: Side | None = None
    ) -> Decimal | None:
        """返回单笔数量上限；未声明时返回 None 表示无上限。

        与下限相反，上限缺失不影响安全，因此不抛异常。但调用方必须处理
        非 None 的情况——本策略单边名义额较大，撞上上限会直接下不进单。
        """
        limits = await self._get_quantity_limits(market, side)
        return limits.maximum

    async def round_amount(
        self, market: str, amount: Decimal, side: Side | None = None
    ) -> Decimal:
        """按 min_qty_tick 向下对齐；步长未知时保持基类行为原样返回。

        向下取整而非四舍五入：宁可少下一个步长，也不要超过调用方算出的目标量。
        """
        normalized = Decimal(str(amount))
        limits = await self._get_quantity_limits(market, side)
        if limits.step is None:
            # API 未提供数量精度时保持基类兼容行为，避免猜测步长导致错误数量。
            return await super().round_amount(market, normalized)
        aligned = (normalized / limits.step).to_integral_value(
            rounding=ROUND_DOWN
        ) * limits.step
        return aligned

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

    async def get_position_pnl(self, market: str) -> PositionPnl | None:
        """读取指定标的盈亏，并用绝对数量与标记价计算仓位价值。"""
        data = await self.get_positions()
        items = data if isinstance(data, list) else (data or {}).get("positions", [])
        for position in items:
            info = position.get("position_info", position)
            if not self._position_matches_underlying(info, market):
                continue

            def optional_decimal(value: object) -> Decimal | None:
                return Decimal(str(value)) if value not in (None, "") else None

            quantity = optional_decimal(info.get("qty", info.get("size")))
            price_info = position.get("price_info")
            mark_price = optional_decimal(
                price_info.get("underlying_price")
                if isinstance(price_info, dict)
                else None
            )
            position_value = (
                abs(quantity) * mark_price
                if quantity is not None and mark_price is not None
                else None
            )
            return PositionPnl(
                unrealized_pnl=optional_decimal(position.get("upnl")),
                entry_price=optional_decimal(info.get("avg_entry_price")),
                position_value=position_value,
            )
        return None

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

    async def _instrument_kind_from_metadata(
        self, underlying: str
    ) -> tuple[str, str | None] | None:
        """从 supported_assets 元数据读取平台标注的合约类型与 kind。

        元数据里有权威字段，不必从图标 URL 之类的展示字段推断：

            SNDK       instrument_type=perpetual_rwa_future  asset_class=equity
            ANTHROPIC  instrument_type=perpetual_rwa_future  asset_class=equity
            XAU        instrument_type=perpetual_rwa_future  asset_class=commodity
            BTC        instrument_type=perpetual_future      （无 asset_class）

        早期实现按 ``token_uri`` 是否含 ``/equities/`` 判断，对 SNDK 失效——
        它是真实上市公司，图标来自第三方金融数据商
        （images.financialmodelingprep.com），而 OPENAI/ANTHROPIC 未上市、
        只能用自家图标。**用展示字段做分类判断本就不可靠。**

        取不到元数据时返回 None，调用方保持原有普通永续行为。
        """
        cache = getattr(self, "_instrument_kind_cache", None)
        if cache is None:
            cache = {}
            self._instrument_kind_cache = cache
        key = str(underlying).upper()
        if key in cache:
            return cache[key]

        if not getattr(self, "_supported_assets_loaded", False):
            try:
                self._supported_assets_metadata = await self.get_supported_assets()
            except Exception:  # noqa: BLE001 元数据不可用时保持原普通永续行为
                self._supported_assets_metadata = None
            self._supported_assets_loaded = True

        metadata = self._supported_assets_metadata
        records: Any = None
        if isinstance(metadata, dict):
            records = next(
                (
                    value
                    for asset, value in metadata.items()
                    if str(asset).upper() == key
                ),
                None,
            )
        if isinstance(records, dict):
            records = [records]

        resolved: tuple[str, str | None] | None = None
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                declared = str(record.get("instrument_type") or "").strip()
                if not declared:
                    continue
                asset_class = record.get("asset_class")
                # kind 只在 RWA 类合约上要求；普通永续传 None。
                kind = (
                    str(asset_class).strip()
                    if declared == "perpetual_rwa_future" and asset_class
                    else None
                )
                resolved = (declared, kind)
                break

        cache[key] = resolved
        return resolved

    async def _resolve_instrument_params(
        self,
        underlying: str,
        instrument_type: str,
        kind: str | None,
    ) -> tuple[str, str | None]:
        """仅为默认普通永续参数自动补全股票 RWA 类型，显式参数保持不变。"""
        if instrument_type != "perpetual_future" or kind is not None:
            return instrument_type, kind
        resolved = await self._instrument_kind_from_metadata(underlying)
        if resolved is not None:
            return resolved
        return instrument_type, kind

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
        默认参数会根据标的元数据自动识别股票 RWA；显式黄金参数继续原样透传。
        """
        instrument_type, kind = await self._resolve_instrument_params(
            underlying,
            instrument_type,
            kind,
        )
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
        默认参数会根据标的元数据自动识别股票 RWA；显式黄金参数继续原样透传。
        """
        s = "buy" if side is Side.BUY else "sell"
        instrument_type, kind = await self._resolve_instrument_params(
            market,
            instrument_type,
            kind,
        )
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
