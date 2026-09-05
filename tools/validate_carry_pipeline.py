"""端到端链路验证：多 XAU + 空 PAXG 的最小实盘往返。

用途仅限**验证下单链路是否通**，不是策略工具：
证明 quote → accept → 读仓（含最终一致延迟）→ reduce_only 平仓 全链路可用，
并实测真实成交滑点与读仓延迟。

⚠️ 与 tools/run_swap_carry_guard.py 不兼容：守护进程只认 XAUS/XAU 两条腿，
运行本脚本前必须先 unload 守护进程，否则它会把 XAU 腿当单腿失衡平掉。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from decimal import Decimal

# 硬上限：本脚本只做链路验证，绝不允许放大名义。
MAX_NOTIONAL_USD = Decimal("60")
# accept 按 quote_id 锁价成交，实际滑点≈0；该值只是保护性上限（分数，0.01=1%）。
MAX_SLIPPAGE = 0.01
POSITION_POLL_ATTEMPTS = 12
POSITION_POLL_INTERVAL_S = 2.0

LEGS = (
    # (underlying, instrument_type, kind, 开仓方向)
    ("XAU", "perpetual_rwa_future", "commodity", "buy"),
    ("PAXG", "perpetual_future", None, "sell"),
)


def _strip_proxy() -> None:
    """本机代理出口在美国，会被 Variational 地区限制误伤。"""
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
        "https_proxy", "ALL_PROXY", "all_proxy",
    ):
        os.environ.pop(name, None)


async def _poll_position(var, underlying: str, *, expect_nonzero: bool):
    """轮询持仓，容忍 /positions 最终一致延迟。

    历史教训：accept 返回 rfq_id 后立即读仓可能仍为 0，
    据即时读仓判定失败会误判并留下裸仓。
    """
    for attempt in range(1, POSITION_POLL_ATTEMPTS + 1):
        pos = await var.get_position(underlying, exact=True)
        if (pos.signed_size != 0) == expect_nonzero:
            return pos, attempt
        await asyncio.sleep(POSITION_POLL_INTERVAL_S)
    return await var.get_position(underlying, exact=True), POSITION_POLL_ATTEMPTS


async def _quote(var, leg, side: str, qty: Decimal):
    """询价并返回 (payload, 成交侧价格)。"""
    underlying, itype, kind, _ = leg
    payload = await var.request_quote(
        underlying, side, qty, instrument_type=itype, kind=kind
    )
    price = Decimal(str(payload["ask" if side == "buy" else "bid"]))
    return payload, price


async def run(notional: Decimal, *, dry_run: bool, hold_seconds: int) -> int:
    from adapters.variational_client import (
        VariationalClient,
        Session,
        VariationalJurisdictionError,
    )

    if notional > MAX_NOTIONAL_USD:
        print(f"❌ 名义 ${notional} 超过硬上限 ${MAX_NOTIONAL_USD}，拒绝执行")
        return 2

    var = VariationalClient(Session.from_env())
    await var.connect()
    opened: list[tuple] = []
    try:
        # ---- 1. 两腿询价，按同名义换算数量 ----
        plans = []
        for leg in LEGS:
            underlying, itype, kind, side = leg
            probe, price = await _quote(var, leg, side, Decimal("0.001"))
            qty = (notional / price).quantize(Decimal("0.00001"))
            payload, exec_price = await _quote(var, leg, side, qty)
            plans.append((leg, qty, payload, exec_price))
            print(
                f"  {underlying:5s} {side:4s} qty={qty} 价={exec_price} "
                f"名义=${qty * exec_price:.2f} quote_id={payload['quote_id']}"
            )

        if dry_run:
            print("\n✅ dry-run：链路检查完成，未 accept 任何报价")
            return 0

        # ---- 2. 按顺序开两腿；第二腿失败则回滚第一腿 ----
        for leg, qty, payload, exec_price in plans:
            underlying, itype, kind, side = leg
            print(f"\n→ accept {underlying} {side} qty={qty} ...")
            t0 = time.monotonic()
            try:
                rfq = await var.accept_quote(
                    quote_id=payload["quote_id"],
                    side=side,
                    max_slippage=MAX_SLIPPAGE,
                    is_reduce_only=False,
                )
            except VariationalJurisdictionError as exc:
                print(f"🚫 地区封锁，需在放行 IP 上执行：{exc}")
                raise
            print(f"   rfq={rfq}  耗时={time.monotonic() - t0:.2f}s")
            pos, attempts = await _poll_position(var, underlying, expect_nonzero=True)
            print(
                f"   读仓确认 qty={pos.signed_size}（轮询 {attempts} 次，"
                f"约 {(attempts - 1) * POSITION_POLL_INTERVAL_S:.0f}s 后可见）"
            )
            if pos.signed_size == 0:
                raise RuntimeError(f"{underlying} accept 后读仓仍为 0，链路异常")
            opened.append((leg, pos.signed_size, exec_price))
        return await _report_and_close(var, opened, hold_seconds)
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 执行出错：{type(exc).__name__}: {exc}")
        if opened:
            print("→ 尝试回滚已开腿 ...")
            await _close_all(var, opened)
        return 1
    finally:
        await var.close()


async def _close_all(var, opened) -> bool:
    """对已开腿逐一 reduce_only 平仓；返回是否全部归零。"""
    all_flat = True
    for leg, signed_size, _entry in opened:
        underlying, itype, kind, _side = leg
        if signed_size == 0:
            continue
        close_side = "sell" if signed_size > 0 else "buy"
        qty = abs(signed_size)
        try:
            payload = await var.request_quote(
                underlying, close_side, qty, instrument_type=itype, kind=kind
            )
            await var.accept_quote(
                quote_id=payload["quote_id"],
                side=close_side,
                max_slippage=MAX_SLIPPAGE,
                is_reduce_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"   ❌ {underlying} 平仓失败：{type(exc).__name__}: {exc}")
            all_flat = False
            continue
        pos, _ = await _poll_position(var, underlying, expect_nonzero=False)
        status = "✅ 已归零" if pos.signed_size == 0 else f"⚠️ 仍有 {pos.signed_size}"
        print(f"   {underlying} 平仓 {status}")
        if pos.signed_size != 0:
            all_flat = False
    return all_flat


async def _report_and_close(var, opened, hold_seconds: int) -> int:
    """打印净敞口，持有指定秒数后平掉两腿。"""
    print("\n=== 两腿已建立 ===")
    total_signed_notional = Decimal(0)
    for leg, signed_size, entry in opened:
        underlying = leg[0]
        notional = signed_size * entry
        total_signed_notional += notional
        print(f"  {underlying:5s} qty={signed_size:+} 入场价={entry} 名义=${notional:+.2f}")
    print(f"  净敞口 = ${total_signed_notional:+.2f}（越接近 0 越中性）")

    balance = await var.get_balance()
    print(f"  账户权益=${balance.equity:.2f}")

    if hold_seconds > 0:
        print(f"\n持有 {hold_seconds}s 后平仓 ...")
        await asyncio.sleep(hold_seconds)

    print("\n=== 平仓 ===")
    all_flat = await _close_all(var, opened)
    after = await var.get_balance()
    print(f"\n账户权益：平仓前 ${balance.equity:.2f} → 平仓后 ${after.equity:.2f}"
          f"  差额=${after.equity - balance.equity:+.4f}（即本次往返实测磨损）")
    if not all_flat:
        print("🚨 存在未归零的腿，需人工处理！")
        return 1
    print("✅ 全链路验证通过：报价 → accept → 读仓 → reduce_only 平仓 → 归零")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="carry 下单链路端到端验证（XAU/PAXG 最小往返）")
    parser.add_argument("--notional", type=Decimal, default=Decimal("50"), help="每腿名义美元")
    parser.add_argument("--hold-seconds", type=int, default=30, help="两腿建立后持有秒数")
    parser.add_argument("--yes", action="store_true", help="真正下单；缺省为 dry-run")
    args = parser.parse_args()

    _strip_proxy()
    from dotenv import load_dotenv

    load_dotenv()
    mode = "实盘下单" if args.yes else "dry-run（不 accept）"
    print(f"=== carry 链路验证：多 XAU + 空 PAXG，每腿 ${args.notional}，{mode} ===\n")
    return asyncio.run(
        run(args.notional, dry_run=not args.yes, hold_seconds=args.hold_seconds)
    )


if __name__ == "__main__":
    raise SystemExit(main())
