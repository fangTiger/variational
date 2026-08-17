"""下单返回值与订单号映射。

Lighter 的下单响应（RespSendTx）只有 code/message/tx_hash，不含订单号，
而 cancel_order 必须传 order_index。因此下单时自己指定 client_order_index，
事后通过 account_active_orders 建立 client_order_index → order_index 的映射。

client_order_index 必须跨重启单调递增：进程内自增会在重启后从 1 开始，
与历史订单撞号，导致映射查到别人的单、撤错单。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OrderRef:
    """统一的下单返回值。

    字段名 id 是为了兼容引擎的取值方式（grid_engine.py:1747 用
    getattr(res, "id")），不要改名。
    """

    id: int | None
    client_order_index: int
    status: str = ""


class ClientOrderIndexAllocator:
    """跨重启单调递增的客户端订单号分配器。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._current: int | None = None

    def _load(self) -> int:
        if not self._path.exists():
            return 0
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            last = data["last"]
            if type(last) is not int or last < 0:
                raise ValueError("last 必须是非负整数")
            return last
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"客户端订单号文件 {self._path} 已损坏（{exc}）。"
                "不能从 1 重新开始——会与历史订单撞号。"
                "请人工确认历史最大编号后修复"
            ) from exc

    def next(self) -> int:
        if self._current is None:
            self._current = self._load()
        self._current += 1
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"last": self._current}), encoding="utf-8"
        )
        return self._current


def resolve_order_index(orders, client_order_index: int) -> int | None:
    """在活动订单列表里按 client_order_index 找 order_index。

    查不到返回 None：可能尚未上链，也可能已成交或已撤销。
    由调用方决定重试还是放弃——这里不猜。
    """
    for order in orders:
        got = (
            order.get("client_order_index")
            if isinstance(order, dict)
            else getattr(order, "client_order_index", None)
        )
        if got == client_order_index:
            return (
                order.get("order_index")
                if isinstance(order, dict)
                else getattr(order, "order_index", None)
            )
    return None
