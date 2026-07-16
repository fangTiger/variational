"""下单冒烟测试：最小量开仓 → 读持仓 → 立即平仓，验证下单链路。

⚠️ 会用真金下真单（极小额）。默认 Variational、BTC、最小量、开多后自动平仓。
安全措施：数量硬上限、try/finally 保证平仓、地区限制/错误清晰提示。

用法（Codex 在允许交易的 IP 上跑）：
    PYTHONPATH=. .venv/bin/python -m tools.smoke_order
    PYTHONPATH=. .venv/bin/python -m tools.smoke_order --venue variational --side sell --qty 0.000002
    PYTHONPATH=. .venv/bin/python -m tools.smoke_order --venue extended --qty 0.0001
    PYTHONPATH=. .venv/bin/python -m tools.smoke_order --keep   # 只开不自动平（谨慎）
"""

from __future__ import annotations

# 必须在导入 x10 前配好 CA
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
from decimal import Decimal  # noqa: E402

from adapters.base import Side  # noqa: E402

# 数量硬上限（防手滑）：BTC 约 $1.3 / $6.4
_MAX_QTY = {"variational": Decimal("0.00002"), "extended": Decimal("0.0002")}


async def _variational(side: Side, qty: Decimal, keep: bool) -> None:
    from adapters.variational_client import (
        Session,
        VariationalClient,
        VariationalJurisdictionError,
    )

    var = VariationalClient(Session.from_env())
    try:
        bal = await var.raw("/portfolio")
        pos0 = await var.get_position("BTC")
        print(f"开始：余额={bal} 持仓={pos0.signed_size}")

        print(f">>> 开仓 {side.value} {qty} BTC …")
        try:
            r = await var.market_order("BTC", side, qty)
        except VariationalJurisdictionError as exc:
            print(f"❌ 交易被地区封锁：{exc}\n   → 该 IP 不允许下单，需换到允许交易的地区。")
            return
        print(f"   成交返回：{json.dumps(r, ensure_ascii=False)[:250]}")

        await asyncio.sleep(1)
        pos1 = await var.get_position("BTC")
        print(f"   开仓后持仓：{pos1.signed_size}")

        if keep:
            print("   --keep：保留持仓，未平仓。请记得手动平仓！")
            return
        print(">>> 平仓 …")
        rc = await var.close_position("BTC")
        print(f"   平仓返回：{json.dumps(rc, ensure_ascii=False)[:200] if rc else '无'}")
        await asyncio.sleep(1)
        pos2 = await var.get_position("BTC")
        flat = pos2.signed_size == 0
        print(f"   平仓后持仓：{pos2.signed_size} {'✅ 已归零' if flat else '⚠️ 未归零，请手动检查！'}")
        bal2 = await var.raw("/portfolio")
        print(f"结束：余额={bal2}")
    finally:
        await var.close()


async def _extended(side: Side, qty: Decimal, keep: bool) -> None:
    from adapters.extended_client import ExtendedClient

    ext = ExtendedClient.from_env()
    await ext.connect()
    try:
        pos0 = await ext.get_position("BTC-USD")
        print(f"开始：Extended 持仓={pos0.signed_size}")
        print(f">>> 开仓 {side.value} {qty} BTC-USD …")
        r = await ext.market_order("BTC-USD", side, qty)
        print(f"   成交返回：{str(r)[:250]}")
        await asyncio.sleep(2)
        pos1 = await ext.get_position("BTC-USD")
        print(f"   开仓后持仓：{pos1.signed_size}")
        if keep:
            print("   --keep：保留持仓。请记得手动平仓！")
            return
        print(">>> 平仓 …")
        rc = await ext.close_position("BTC-USD")
        print(f"   平仓返回：{str(rc)[:200] if rc else '无'}")
        await asyncio.sleep(2)
        pos2 = await ext.get_position("BTC-USD")
        flat = pos2.signed_size == 0
        print(f"   平仓后持仓：{pos2.signed_size} {'✅ 已归零' if flat else '⚠️ 未归零，请手动检查！'}")
    finally:
        await ext.close()


async def _main(venue: str, side: Side, qty: Decimal, keep: bool) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    cap = _MAX_QTY[venue]
    if qty > cap:
        raise SystemExit(f"❌ 数量 {qty} 超过安全上限 {cap}，拒绝执行。")

    if venue == "variational":
        await _variational(side, qty, keep)
    else:
        await _extended(side, qty, keep)


def main() -> None:
    p = argparse.ArgumentParser(description="下单冒烟测试（真金极小额）")
    p.add_argument("--venue", choices=["variational", "extended"], default="variational")
    p.add_argument("--side", choices=["buy", "sell"], default="buy")
    p.add_argument("--qty", default="0.000002", help="合约数量（BTC 个数）")
    p.add_argument("--keep", action="store_true", help="只开不自动平仓（谨慎）")
    args = p.parse_args()

    side = Side.BUY if args.side == "buy" else Side.SELL
    asyncio.run(_main(args.venue, side, Decimal(args.qty), args.keep))


if __name__ == "__main__":
    main()
