"""断网前收摊 / 恢复后开工。

断网期间的核心风险不是"引擎停了"，而是**挂单留在交易所继续成交**：
价格跌穿全部买单会让持仓从空翻多，而交易所侧那张止损是 BUY 方向
（用于平空），对多头完全失效——引擎正常时会在仓位翻向后重挂，断网时
不会。结果是裸奔的多头。

收摊做两件事：停引擎 + 撤掉全部网格挂单（保留持仓与整仓止损）。
没有挂单，持仓就不会翻向，那张止损就永远是对的方向。
代价是断网期间不赚钱，换来完全受保护。

用法：
    # 下班前
    PYTHONPATH=. .venv/bin/python -m tools.go_dark
    # 回来后
    PYTHONPATH=. .venv/bin/python -m tools.go_dark --resume
"""
from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402

from adapters.extended_client import ExtendedClient, filter_grid_orders  # noqa: E402

MARKET = "BTC-USD"
LABEL = "com.variational.grid-bot"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _launchctl(*args: str) -> tuple[int, str]:
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _engine_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "tools.run_grid"], capture_output=True, text=True)
    return bool(r.stdout.strip())


async def _state(client: ExtendedClient) -> dict:
    pos = await client.get_position(MARKET)
    bal = await client.get_balance()
    price = await client.get_market_price(MARKET)
    orders = await client.get_open_orders(MARKET)
    tpsl = await client.get_position_tpsl(MARKET)
    return {
        "size": float(pos.signed_size),
        "equity": float(bal.equity),
        "price": float(price.mid),
        "grid_orders": len(filter_grid_orders(orders)),
        "tpsl": tpsl,
    }


def _report_protection(st: dict) -> bool:
    """打印断网期间的保护状态。返回是否受保护。"""
    size, price, tpsl = st["size"], st["price"], st["tpsl"]
    if size == 0:
        print("  持仓为零 → 无敞口，完全安全")
        return True
    if tpsl is None or getattr(tpsl, "stop_loss", None) is None:
        print("  ⚠⚠ 没有整仓止损！持仓在断网期间无保护")
        return False

    trigger = float(tpsl.stop_loss.trigger_price)
    side = str(tpsl.side).upper()
    # 空头需要 BUY 止损（价格涨上去平掉），多头需要 SELL 止损
    correct_side = ("BUY" in side) if size < 0 else ("SELL" in side)
    loss = abs(trigger - price) * abs(size)
    print(f"  持仓 {size:+.5f} BTC  现价 {price:.0f}")
    print(f"  整仓止损 {side} @ {trigger:.0f}"
          f"（距现价 {100 * abs(trigger - price) / price:.2f}%）")
    print(f"  触发时亏损约 ${loss:.0f} = 权益的 {100 * loss / st['equity']:.1f}%")
    if not correct_side:
        print("  ⚠⚠ 止损方向与持仓不匹配，该止损无效！")
        return False
    print("  ✅ 止损方向正确，且没有挂单可导致持仓翻向")
    return True


async def go_dark() -> int:
    client = ExtendedClient.from_env(prefix="X10_GRID")
    await client.connect()
    try:
        before = await _state(client)
        print(f"收摊前：持仓 {before['size']:+.5f} BTC，"
              f"网格挂单 {before['grid_orders']} 张，权益 ${before['equity']:.2f}")

        print("\n1) 停引擎")
        code, out = _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        print(f"   {'已停止' if code == 0 else f'launchctl 返回 {code}: {out}'}")
        await asyncio.sleep(3)
        if _engine_running():
            print("   ⚠ 引擎进程仍在，撤单可能被它重新挂回来，请人工确认")
            return 1

        print("\n2) 撤掉全部网格挂单（保留持仓与整仓止损）")
        n = await client.cancel_grid_orders(MARKET)
        print(f"   已撤 {n} 张")

        await asyncio.sleep(2)
        after = await _state(client)
        print(f"\n收摊后：持仓 {after['size']:+.5f} BTC，"
              f"网格挂单 {after['grid_orders']} 张")
        if after["grid_orders"] > 0:
            print(f"   ⚠ 仍有 {after['grid_orders']} 张挂单未撤干净，请重跑或人工处理")
            return 1

        print("\n断网期间的保护状态：")
        ok = _report_protection(after)
        print("\n恢复联网后运行：PYTHONPATH=. .venv/bin/python -m tools.go_dark --resume")
        return 0 if ok else 1
    finally:
        await client.close()


async def go_live() -> int:
    if _engine_running():
        print("引擎已在运行，无需恢复")
        return 0
    print("1) 启动引擎")
    code, out = _launchctl("bootstrap", f"gui/{os.getuid()}", PLIST)
    print(f"   {'已启动' if code == 0 else f'launchctl 返回 {code}: {out}'}")
    if code != 0:
        return 1

    print("   等待接管持仓与重建阶梯 …")
    await asyncio.sleep(30)

    client = ExtendedClient.from_env(prefix="X10_GRID")
    await client.connect()
    try:
        st = await _state(client)
        print(f"\n恢复后：持仓 {st['size']:+.5f} BTC，"
              f"网格挂单 {st['grid_orders']} 张，权益 ${st['equity']:.2f}")
        if st["grid_orders"] == 0:
            print("   ⚠ 阶梯尚未重建，再等一分钟复查；若仍为 0 请看 logs/grid-bot.err.log")
            return 1
        print("   ✅ 阶梯已重建，恢复正常运行")
        return 0
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="断网前收摊 / 恢复后开工")
    parser.add_argument("--resume", action="store_true", help="恢复运行（默认是收摊）")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    raise SystemExit(asyncio.run(go_live() if args.resume else go_dark()))


if __name__ == "__main__":
    main()
