"""从成交流离线配对出闭环。纯函数，无 IO，便于测试与重算。

为什么不用引擎内的配对结果：那份逻辑依赖内存态 self._loop_fills，
进程重启即清空，重启前挂着的半个闭环再也配不上，利润永久漏计
（2026-08-13 一天重启 5 次）。离线重算不受此影响，且配对规则
将来要修正时不必动交易进程。
"""

from __future__ import annotations


def pair_fills(fills: list[dict]) -> list[dict]:
    """按格号配对反向成交，返回闭环列表（按时间升序）。

    规则与引擎一致：同格反向即配对，数量取小值，剩余留待下次。
    loop_id 由两个 fill_id 拼成，因此对同一批输入是确定的——
    这保证重复入库不会重复计算利润。
    """
    pending: dict[int, list[dict]] = {}
    loops: list[dict] = []

    for fill in sorted(fills, key=lambda item: (float(item["ts"]), str(item["fill_id"]))):
        level = int(fill["level"])
        side = str(fill["side"]).upper()
        remaining = float(fill["qty"])
        queue = pending.setdefault(level, [])

        # 先和同格反向的挂账逐个配对
        while remaining > 0 and queue and str(queue[0]["side"]).upper() != side:
            head = queue[0]
            matched = min(remaining, float(head["qty"]))
            buy_price = float(head["price"]) if side == "SELL" else float(fill["price"])
            sell_price = float(fill["price"]) if side == "SELL" else float(head["price"])
            loops.append(
                {
                    "loop_id": f"{head['fill_id']}+{fill['fill_id']}",
                    "ts": float(fill["ts"]),
                    "level": level,
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "qty": matched,
                    "gross_pnl": (sell_price - buy_price) * matched,
                }
            )
            remaining -= matched
            head["qty"] = float(head["qty"]) - matched
            if head["qty"] <= 0:
                queue.pop(0)

        if remaining > 0:
            queue.append({**fill, "qty": remaining})

    return loops
