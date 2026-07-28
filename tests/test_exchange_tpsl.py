"""适配器扩展契约测试：reduce_only 透传、只撤网格单保留TPSL。

真实 SDK 下单/撤单的交易所行为在 testnet 单独验证（见计划 Task 9 说明）。
本测试只锁定"过滤逻辑"这一可纯测的部分。
"""
from __future__ import annotations

from types import SimpleNamespace

from adapters.extended_client import filter_grid_orders  # 纯函数：从开放单里挑出该撤的网格单


def _o(oid, reduce_only=False, otype="LIMIT"):
    return SimpleNamespace(id=oid, reduce_only=reduce_only, type=otype)


def test_filter_keeps_tpsl_and_reduce_only() -> None:
    orders = [_o("g1"), _o("g2"), _o("tp", otype="TPSL"),
              _o("ro", reduce_only=True)]
    to_cancel = filter_grid_orders(orders)
    ids = {getattr(o, "id") for o in to_cancel}
    assert ids == {"g1", "g2"}  # 只撤普通网格单，保留 TPSL 与 reduce_only
