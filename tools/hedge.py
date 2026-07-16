"""双腿 delta 中性对冲：开仓 / 查看 / 平仓。

策略（阶段 A）：Variational 做空 + Extended 做多，等额名义，收 Variational 资金费。
方向依据监控：Variational 资金费 > Extended → 空 Variational 收费。

安全：
- 先开 Variational 腿，成功后再开 Extended 腿；Extended 失败则立即平掉 Variational（不留裸仓）。
- 名义/数量硬上限；开仓前检查无既有持仓；开仓后校验净 delta ≈ 0。

用法（Codex 在允许交易的 IP 上跑）：
    # 查看两腿与资金费/积分
    PYTHONPATH=. .venv/bin/python -m tools.hedge status
    # 开仓：每腿约 50 美元名义（首次建议小额验证）
    PYTHONPATH=. .venv/bin/python -m tools.hedge open --notional 50
    # 平掉两腿
    PYTHONPATH=. .venv/bin/python -m tools.hedge close
"""

from __future__ import annotations

# 必须在导入 x10 前配好 CA
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
from decimal import ROUND_DOWN, Decimal  # noqa: E402

from adapters.base import Side  # noqa: E402
from adapters.extended_client import ExtendedClient  # noqa: E402
from adapters.variational_client import (  # noqa: E402
    Session,
    VariationalClient,
    VariationalJurisdictionError,
)
from tracking.monitor import compute_funding_view  # noqa: E402

UNDERLYING = "BTC"
EXT_MARKET = "BTC-USD"
QTY_STEP = Decimal("0.0001")          # 对齐 Extended 最小下单粒度
MAX_NOTIONAL = Decimal("2000")        # 单腿名义硬上限（防手滑）


def _round_qty(qty: Decimal) -> Decimal:
    return qty.quantize(QTY_STEP, rounding=ROUND_DOWN)


async def _load():
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    var = VariationalClient(Session.from_env())
    ext = ExtendedClient.from_env()
    await ext.connect()
    return var, ext


async def _net_delta(var: VariationalClient, ext: ExtendedClient):
    vp = await var.get_position(UNDERLYING)
    ep = await ext.get_position(EXT_MARKET)
    return vp.signed_size, ep.signed_size, vp.signed_size + ep.signed_size


async def cmd_status(var: VariationalClient, ext: ExtendedClient) -> None:
    vs, es, net = await _net_delta(var, ext)
    stats = await ext._client.info.get_market_statistics(market_name=EXT_MARKET)
    mark = Decimal(str(stats.data.mark_price))
    var_rate = await var.get_funding_rate(UNDERLYING)
    fv = compute_funding_view(var_rate, Decimal(str(stats.data.funding_rate)))
    pts = await var.get_points_summary()
    print(f"BTC 价格≈{mark}")
    print(f"Variational 持仓={vs}（名义≈${abs(vs)*mark:.2f}）")
    print(f"Extended    持仓={es}（名义≈${abs(es)*mark:.2f}）")
    print(f"净 delta={net}  {'✅ 中性' if abs(net) < QTY_STEP else '⚠️ 有敞口'}")
    print(fv.pretty())
    print(f"积分：{pts['total_points']}（排名 {pts.get('rank')}）")


async def cmd_open(var: VariationalClient, ext: ExtendedClient, notional: Decimal, leverage: Decimal) -> None:
    if notional > MAX_NOTIONAL:
        raise SystemExit(f"❌ 名义 {notional} 超过上限 {MAX_NOTIONAL}")

    vs, es, _ = await _net_delta(var, ext)
    if vs != 0 or es != 0:
        raise SystemExit(f"❌ 已有持仓（Variational={vs} Extended={es}），请先 close 或人工检查")

    stats = await ext._client.info.get_market_statistics(market_name=EXT_MARKET)
    mark = Decimal(str(stats.data.mark_price))
    qty = _round_qty(notional / mark)
    if qty < QTY_STEP:
        raise SystemExit(f"❌ 名义太小，qty={qty} 低于最小 {QTY_STEP}")
    print(f"目标：每腿 qty={qty} BTC（名义≈${qty*mark:.2f}，价格≈{mark}）")

    # 设 Extended 杠杆（Variational 为隐式保证金，无需设置）
    try:
        await ext.set_leverage(EXT_MARKET, leverage)
        print(f"已设 Extended 杠杆 {leverage}x")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 设杠杆失败（用账户默认）：{exc}")

    # 1) 先开 Variational 空头
    print(f">>> [1/2] Variational 开空 {qty} …")
    try:
        r1 = await var.market_order(UNDERLYING, Side.SELL, qty)
    except VariationalJurisdictionError as exc:
        raise SystemExit(f"❌ Variational 交易被地区封锁：{exc}\n   需在允许交易的 IP 上运行。")
    print(f"   成交：{json.dumps(r1, ensure_ascii=False)[:150]}")

    # 2) 再开 Extended 多头；失败则回滚 Variational
    print(f">>> [2/2] Extended 开多 {qty} …")
    try:
        r2 = await ext.market_order(EXT_MARKET, Side.BUY, qty)
        print(f"   成交：{str(r2)[:150]}")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Extended 开仓失败：{exc}\n>>> 回滚：平掉 Variational 空头以避免裸仓 …")
        rc = await var.close_position(UNDERLYING)
        print(f"   Variational 已回滚：{json.dumps(rc, ensure_ascii=False)[:120] if rc else '无'}")
        raise SystemExit("已回滚，未留裸仓。")

    await asyncio.sleep(2)
    vs, es, net = await _net_delta(var, ext)
    print(f"\n开仓完成：Variational={vs} Extended={es} 净delta={net} "
          f"{'✅ 中性' if abs(net) < QTY_STEP else '⚠️ 有敞口，请检查/再平衡'}")


async def cmd_close(var: VariationalClient, ext: ExtendedClient) -> None:
    print(">>> 平 Extended …")
    try:
        r = await ext.close_position(EXT_MARKET)
        print(f"   {str(r)[:150] if r else '无持仓'}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ Extended 平仓异常：{exc}")
    print(">>> 平 Variational …")
    try:
        r = await var.close_position(UNDERLYING)
        print(f"   {json.dumps(r, ensure_ascii=False)[:150] if r else '无持仓'}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ Variational 平仓异常：{exc}")
    await asyncio.sleep(2)
    vs, es, net = await _net_delta(var, ext)
    flat = vs == 0 and es == 0
    print(f"\n平仓后：Variational={vs} Extended={es} {'✅ 均已归零' if flat else '⚠️ 仍有持仓，请人工检查！'}")


async def _main(args) -> None:
    var, ext = await _load()
    try:
        if args.cmd == "status":
            await cmd_status(var, ext)
        elif args.cmd == "open":
            await cmd_open(var, ext, Decimal(str(args.notional)), Decimal(str(args.leverage)))
        elif args.cmd == "close":
            await cmd_close(var, ext)
    finally:
        await var.close()
        await ext.close()


def main() -> None:
    p = argparse.ArgumentParser(description="双腿 delta 中性对冲")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看两腿/资金费/积分")
    po = sub.add_parser("open", help="开仓（Variational空 + Extended多）")
    po.add_argument("--notional", type=float, required=True, help="每腿名义（美元）")
    po.add_argument("--leverage", type=float, default=3, help="Extended 杠杆（默认3）")
    sub.add_parser("close", help="平掉两腿")
    args = p.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
