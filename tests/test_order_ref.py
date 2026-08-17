"""订单号映射测试。

Lighter 下单不返回订单号，撤单又必须要它，所以这层是撤单能否工作的前提。
撤不掉单意味着挂单在交易所端不断堆积——设计文档明确指出这种形态可能
被判定为 automation exploitation 并撤销积分。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.order_ref import ClientOrderIndexAllocator, OrderRef, resolve_order_index


def test_order_ref_exposes_id_attribute():
    """引擎用 getattr(res, "id") 取订单号（grid_engine.py:1747），
    所以返回值必须有 .id，否则 oid=None、后续撤单必然失败。"""
    ref = OrderRef(id=12345, client_order_index=7)
    assert ref.id == 12345


def test_allocator_persists_across_restart(tmp_path):
    """进程重启后不得从 1 重新开始——会与历史订单撞号。"""
    path = tmp_path / "coi.json"
    a1 = ClientOrderIndexAllocator(path)
    first = a1.next()
    second = a1.next()
    assert second == first + 1

    a2 = ClientOrderIndexAllocator(path)  # 模拟重启
    assert a2.next() == second + 1


def test_allocator_starts_from_one_when_file_missing(tmp_path):
    assert ClientOrderIndexAllocator(tmp_path / "missing.json").next() == 1


def test_allocator_recovers_from_corrupt_file(tmp_path):
    """文件损坏时不能从 1 重来（会撞号），应报错让人处理。"""
    path = tmp_path / "coi.json"
    path.write_text("{ 坏文件", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        ClientOrderIndexAllocator(path).next()


@pytest.mark.parametrize(
    "contents",
    [
        '{"last": true}',
        '{"last": 1.9}',
        '{"last": "7"}',
        '{"last": -1}',
    ],
)
def test_allocator_rejects_semantically_corrupt_file(tmp_path, contents):
    """语法合法但类型或范围错误的状态也不得被强制转换后继续使用。"""
    path = tmp_path / "coi.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        ClientOrderIndexAllocator(path).next()


def test_resolve_finds_order_index_by_client_index():
    orders = [
        {"client_order_index": 5, "order_index": 900},
        {"client_order_index": 7, "order_index": 901},
    ]
    assert resolve_order_index(orders, client_order_index=7) == 901


def test_resolve_supports_sdk_order_objects():
    orders = [SimpleNamespace(client_order_index=7, order_index=901)]
    assert resolve_order_index(orders, client_order_index=7) == 901


def test_resolve_returns_none_when_not_found():
    """查不到可能是尚未上链、也可能是已成交，调用方决定重试还是放弃，
    这里不猜、不抛。"""
    assert resolve_order_index([], client_order_index=7) is None
