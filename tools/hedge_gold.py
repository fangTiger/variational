"""Variational 同所黄金基差对冲：status / open / close。

策略：多 XAU + 空 XAUT，等盎司数量。XAUT 流动性更薄，开仓先开 XAUT 空，
再按读回成交 qty 开 XAU 多；第二腿失败时立即 reduce_only 回滚 XAUT。

用法（必须在允许交易的 IP 上执行实盘命令）：
    PYTHONPATH=. .venv/bin/python -m tools.hedge_gold status
    PYTHONPATH=. .venv/bin/python -m tools.hedge_gold open --notional 300
    PYTHONPATH=. .venv/bin/python -m tools.hedge_gold close
"""

from __future__ import annotations

# 必须在导入交易相关依赖前配好 CA。
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from decimal import ROUND_DOWN, Decimal  # noqa: E402
from typing import Any  # noqa: E402

from adapters.base import Position, Side  # noqa: E402
from adapters.variational_client import (  # noqa: E402
    Session,
    VariationalClient,
    VariationalJurisdictionError,
)

MAX_NOTIONAL = Decimal("2000")  # 单腿名义硬上限，防手滑。
FALLBACK_QTY_STEP = Decimal("0.001")


@dataclass(frozen=True)
class GoldLeg:
    """黄金对冲腿的 Variational 合约参数。"""

    underlying: str
    open_side: Side
    instrument_type: str
    funding_interval_s: int


XAU_LEG = GoldLeg(
    underlying="XAU",
    open_side=Side.BUY,
    instrument_type="perpetual_rwa_future",
    funding_interval_s=14400,
)
XAUT_LEG = GoldLeg(
    underlying="XAUT",
    open_side=Side.SELL,
    instrument_type="perpetual_future",
    funding_interval_s=14400,
)


async def _load() -> VariationalClient:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    return VariationalClient(Session.from_env())


def _opening_plan() -> tuple[GoldLeg, GoldLeg]:
    """返回固定开仓顺序；同时守住唯一允许方向。"""
    plan = (XAUT_LEG, XAU_LEG)
    if XAU_LEG.open_side is not Side.BUY or XAUT_LEG.open_side is not Side.SELL:
        raise RuntimeError("黄金对冲方向配置错误：只允许多 XAU + 空 XAUT")
    return plan


def _round_qty(qty: Decimal, step: Decimal) -> Decimal:
    """按步长向下取整，避免超过目标名义。"""
    if step <= 0:
        raise ValueError("qty step 必须为正数")
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def _quote_decimal(quote: Any, *keys: str) -> Decimal | None:
    """从 RFQ 报价中按候选字段读取 Decimal。"""
    if not isinstance(quote, dict):
        return None
    for key in keys:
        value = quote.get(key)
        if value not in (None, ""):
            return Decimal(str(value))
    return None


def _mark_from_quote(quote: Any) -> Decimal | None:
    mark = _quote_decimal(quote, "mark_price", "mark", "price", "index_price")
    if mark is not None:
        return mark
    bid = _quote_decimal(quote, "bid")
    ask = _quote_decimal(quote, "ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return ask or bid


def _qty_step_from_quote(quote: Any) -> Decimal:
    step = _quote_decimal(
        quote,
        "qty_step",
        "quantity_step",
        "size_increment",
        "min_size_increment",
        "step_size",
    )
    if step is None or step <= 0:
        print(f"⚠️ 未从报价取到下单步长，使用保守步长 {FALLBACK_QTY_STEP}")
        return FALLBACK_QTY_STEP
    return step


async def _quote_xau_basis(var: VariationalClient) -> tuple[Decimal, Decimal]:
    """用 XAU 报价拿基准价格和数量步长；只询价，不成交。"""
    quote = await var.request_quote(
        XAU_LEG.underlying,
        "buy",
        FALLBACK_QTY_STEP,
        instrument_type=XAU_LEG.instrument_type,
        funding_interval_s=XAU_LEG.funding_interval_s,
    )
    mark = _mark_from_quote(quote)
    if mark is None or mark <= 0:
        raise SystemExit(f"❌ 无法从 XAU 报价读取有效价格：{quote}")
    return mark, _qty_step_from_quote(quote)


async def _get_position(var: VariationalClient, leg: GoldLeg) -> Position:
    return await var.get_position(leg.underlying, exact=True)


async def _net_delta(var: VariationalClient) -> tuple[Decimal, Decimal, Decimal]:
    """返回 (XAU 有符号仓位, XAUT 有符号仓位, 净 delta)。"""
    xau = await _get_position(var, XAU_LEG)
    xaut = await _get_position(var, XAUT_LEG)
    return xau.signed_size, xaut.signed_size, xau.signed_size + xaut.signed_size


async def _market_order(
    var: VariationalClient,
    leg: GoldLeg,
    side: Side,
    qty: Decimal,
    *,
    reduce_only: bool = False,
):
    return await var.market_order(
        leg.underlying,
        side,
        qty,
        reduce_only=reduce_only,
        instrument_type=leg.instrument_type,
        funding_interval_s=leg.funding_interval_s,
    )


def _format_result(result: Any) -> str:
    try:
        return json.dumps(result, ensure_ascii=False)[:150]
    except TypeError:
        return str(result)[:150]


async def _rollback_xaut(var: VariationalClient, qty: Decimal) -> None:
    print(">>> 回滚：reduce_only 平掉 XAUT 空头，避免裸仓 …")
    result = await _market_order(var, XAUT_LEG, Side.BUY, qty, reduce_only=True)
    print(f"   XAUT 已回滚：{_format_result(result) if result else '无'}")


async def cmd_status(var: VariationalClient) -> None:
    xau_size, xaut_size, net = await _net_delta(var)
    try:
        mark, step = await _quote_xau_basis(var)
    except Exception as exc:  # noqa: BLE001
        mark, step = None, FALLBACK_QTY_STEP
        print(f"⚠️ XAU 报价读取失败，仅展示仓位：{exc}")

    neutral = abs(net) <= step
    print("黄金同所对冲（目标：多 XAU + 空 XAUT）")
    if mark is not None:
        print(f"XAU 价格≈{mark}")
        print(f"XAU  持仓={xau_size}（名义≈${abs(xau_size) * mark:.2f}）")
        print(f"XAUT 持仓={xaut_size}（名义≈${abs(xaut_size) * mark:.2f}）")
    else:
        print(f"XAU  持仓={xau_size}")
        print(f"XAUT 持仓={xaut_size}")
    print(f"净 delta={net}  {'✅ 中性' if neutral else '⚠️ 有敞口'}")

    try:
        xau_rate = await var.get_funding_rate(XAU_LEG.underlying, XAU_LEG.instrument_type)
        xaut_rate = await var.get_funding_rate(
            XAUT_LEG.underlying, XAUT_LEG.instrument_type
        )
        print(f"资金费：XAU={xau_rate} / XAUT={xaut_rate}（正数为空头收）")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 资金费读取失败：{exc}")

    try:
        pts = await var.get_points_summary()
        print(f"积分：{pts['total_points']}（排名 {pts.get('rank')}）")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 积分读取失败：{exc}")


async def cmd_open(var: VariationalClient, notional: Decimal) -> None:
    if notional > MAX_NOTIONAL:
        raise SystemExit(f"❌ 名义 {notional} 超过上限 {MAX_NOTIONAL}")

    xau_size, xaut_size, _ = await _net_delta(var)
    if xau_size != 0 or xaut_size != 0:
        raise SystemExit(
            f"❌ 已有黄金持仓（XAU={xau_size} XAUT={xaut_size}），请先 close 或人工检查"
        )

    mark, step = await _quote_xau_basis(var)
    qty = _round_qty(notional / mark, step)
    if qty < step:
        raise SystemExit(f"❌ 名义太小，qty={qty} 低于最小步长 {step}")
    print(f"目标：每腿 qty={qty} 盎司（名义≈${qty * mark:.2f}，XAU 价格≈{mark}）")

    first_leg, second_leg = _opening_plan()
    print(f">>> [1/2] Variational 开空 {first_leg.underlying} {qty} …")
    try:
        r1 = await _market_order(var, first_leg, first_leg.open_side, qty)
    except VariationalJurisdictionError as exc:
        raise SystemExit(
            f"❌ Variational 交易被地区封锁：{exc}\n   需在放行 IP（Codex）执行。"
        ) from exc
    print(f"   成交：{_format_result(r1)}")

    filled_qty = abs((await _get_position(var, first_leg)).signed_size)
    if filled_qty <= 0:
        raise SystemExit("❌ XAUT 开仓后未读到成交仓位，请人工检查")
    print(f"   读回 XAUT 成交 qty={filled_qty}")

    print(f">>> [2/2] Variational 开多 {second_leg.underlying} {filled_qty} …")
    try:
        r2 = await _market_order(var, second_leg, second_leg.open_side, filled_qty)
        print(f"   成交：{_format_result(r2)}")
    except VariationalJurisdictionError as exc:
        print(f"❌ Variational 交易被地区封锁：{exc}\n   需在放行 IP（Codex）执行。")
        try:
            await _rollback_xaut(var, filled_qty)
        except Exception as rollback_exc:  # noqa: BLE001
            raise SystemExit(
                f"❌ 第二腿失败且 XAUT 回滚失败：{rollback_exc}，请立即人工检查"
            ) from rollback_exc
        raise SystemExit("已回滚，未留裸仓；后续需在放行 IP（Codex）执行。") from exc
    except Exception as exc:  # noqa: BLE001
        print(f"❌ XAU 开仓失败：{exc}")
        try:
            await _rollback_xaut(var, filled_qty)
        except Exception as rollback_exc:  # noqa: BLE001
            raise SystemExit(
                f"❌ 第二腿失败且 XAUT 回滚失败：{rollback_exc}，请立即人工检查"
            ) from rollback_exc
        raise SystemExit("已回滚，未留裸仓。") from exc

    xau_size, xaut_size, net = await _net_delta(var)
    neutral = abs(net) <= step
    print(
        f"\n开仓完成：XAU={xau_size} XAUT={xaut_size} 净delta={net} "
        f"{'✅ 中性' if neutral else '⚠️ 有敞口，请检查'}"
    )


async def _close_leg(var: VariationalClient, leg: GoldLeg):
    pos = await _get_position(var, leg)
    if pos.is_flat:
        return None
    side = Side.SELL if pos.signed_size > 0 else Side.BUY
    return await _market_order(var, leg, side, abs(pos.signed_size), reduce_only=True)


async def cmd_close(var: VariationalClient) -> None:
    print(">>> 平 XAUT …")
    try:
        r = await _close_leg(var, XAUT_LEG)
        print(f"   {_format_result(r) if r else '无持仓'}")
    except VariationalJurisdictionError as exc:
        raise SystemExit(
            f"❌ XAUT 平仓被地区封锁：{exc}\n   需在放行 IP（Codex）执行，已停止平 XAU。"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"❌ XAUT 平仓异常：{exc}，已停止平 XAU。") from exc

    print(">>> 平 XAU …")
    try:
        r = await _close_leg(var, XAU_LEG)
        print(f"   {_format_result(r) if r else '无持仓'}")
    except VariationalJurisdictionError as exc:
        raise SystemExit(
            f"❌ XAU 平仓被地区封锁：{exc}\n   需在放行 IP（Codex）执行。"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ XAU 平仓异常：{exc}")

    xau_size, xaut_size, net = await _net_delta(var)
    flat = xau_size == 0 and xaut_size == 0
    print(
        f"\n平仓后：XAU={xau_size} XAUT={xaut_size} 净delta={net} "
        f"{'✅ 均已归零' if flat else '⚠️ 仍有持仓，请人工检查！'}"
    )


async def _main(args) -> None:
    var = await _load()
    try:
        if args.cmd == "status":
            await cmd_status(var)
        elif args.cmd == "open":
            await cmd_open(var, Decimal(str(args.notional)))
        elif args.cmd == "close":
            await cmd_close(var)
    finally:
        await var.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Variational 黄金同所基差对冲")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看两腿/资金费/积分")
    po = sub.add_parser("open", help="开仓（多 XAU + 空 XAUT）")
    po.add_argument("--notional", type=float, required=True, help="每腿名义（美元）")
    sub.add_parser("close", help="平掉两腿")
    args = p.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
