"""下单 accept 诊断（Codex 在允许交易的 IP 上跑）。

目的：确定 /quotes/accept 的正确 max_slippage 格式。对多个候选值各试一次
（每次全新询价→accept）：一旦成交(200) 立即平仓并停止；失败则打印真实 error_message。

安全：最小量、成功即平、try/finally、遇地区限制立即停。最多只会成交 1 次。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.diag_order
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from adapters.variational_client import (
    BASE_URL,
    Session,
    VariationalClient,
    VariationalJurisdictionError,
)

QTY = Decimal("0.000002")  # ~$0.13
# 覆盖 分数/百分比/基点 等不同单位假设
CANDIDATES = ["0.1", "0.01", "0.005", "1", "10", "100", "0.001"]


async def _raw_accept(var: VariationalClient, quote_id: str, side: str, slippage: str, reduce_only: bool):
    body = {
        "quote_id": quote_id,
        "side": side,
        "max_slippage": float(slippage),  # 服务端要求 f64 数字
        "is_reduce_only": reduce_only,
    }
    r = await var._http.request("POST", BASE_URL + "/quotes/accept", json=body)
    return r.status_code, (r.text or "")


async def _close(var: VariationalClient, slippage: str) -> None:
    """用同样的 slippage 反向平掉 BTC 持仓。"""
    pos = await var.get_position("BTC")
    if pos.signed_size == 0:
        print("   平仓：已无持仓")
        return
    side = "sell" if pos.signed_size > 0 else "buy"
    q = await var.request_quote("BTC", side, abs(pos.signed_size))
    st, txt = await _raw_accept(var, q["quote_id"], side, slippage, True)
    print(f"   平仓 accept [{st}] {txt[:160]}")
    await asyncio.sleep(1)
    pos2 = await var.get_position("BTC")
    print(f"   平仓后持仓：{pos2.signed_size} {'✅ 已归零' if pos2.signed_size==0 else '⚠️ 未归零，请手动平仓！'}")


async def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    var = VariationalClient(Session.from_env())
    try:
        pos0 = await var.get_position("BTC")
        print(f"开始持仓：{pos0.signed_size}\n")
        for slp in CANDIDATES:
            try:
                q = await var.request_quote("BTC", "buy", QTY)
                st, txt = await _raw_accept(var, q["quote_id"], "buy", slp, False)
                print(f"max_slippage={slp!r} → [{st}] {txt[:200]}")
                if st == 200:
                    print(f"\n✅ 成交成功！正确 max_slippage 格式 = {slp!r}")
                    await asyncio.sleep(1)
                    print(">>> 立即平仓 …")
                    await _close(var, slp)
                    return
            except VariationalJurisdictionError as exc:
                print(f"❌ 地区限制，停止：{exc}")
                return
            except Exception as exc:  # noqa: BLE001
                print(f"max_slippage={slp!r} → 异常 {type(exc).__name__}: {str(exc)[:160]}")
            await asyncio.sleep(0.5)
        print("\n所有候选都失败——请把上面每行的 error_message 发回，据此定位真正问题。")
    finally:
        await var.close()


if __name__ == "__main__":
    asyncio.run(main())
