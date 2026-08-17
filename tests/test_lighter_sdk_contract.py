"""Lighter SDK 契约冒烟测试。

单测全部用假签名器，因此真实 SDK 的签名变化不会被任何测试发现——
除了这个文件。它只做一件事：断言我们依赖的方法签名和常量没变。
"""

from __future__ import annotations

import inspect

import pytest

lighter = pytest.importorskip("lighter", reason="未安装 lighter_sdk")


def test_create_order_signature():
    params = list(inspect.signature(lighter.SignerClient.create_order).parameters)
    for name in (
        "market_index", "client_order_index", "base_amount", "price",
        "is_ask", "order_type", "time_in_force", "reduce_only",
    ):
        assert name in params, f"SDK 的 create_order 缺少参数 {name}"


def test_cancel_order_signature():
    params = list(inspect.signature(lighter.SignerClient.cancel_order).parameters)
    assert "market_index" in params and "order_index" in params


def test_market_order_uses_avg_execution_price():
    params = list(inspect.signature(lighter.SignerClient.create_market_order).parameters)
    assert "avg_execution_price" in params


def test_order_constants_unchanged():
    sc = lighter.SignerClient
    assert sc.ORDER_TYPE_LIMIT == 0
    assert sc.ORDER_TIME_IN_FORCE_POST_ONLY == 2
    assert sc.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME == 1


def test_send_tx_response_has_no_order_index():
    """本项目订单号映射层存在的全部理由。

    若某天 SDK 开始返回 order_index，这条测试会失败——那是好消息，
    说明可以删掉映射层。请勿直接改断言。
    """
    from lighter.models.resp_send_tx import RespSendTx

    assert "order_index" not in RespSendTx.model_fields


def test_order_model_has_fields_we_depend_on():
    from lighter.models.order import Order

    for name in (
        "order_index", "client_order_index",
        "filled_base_amount", "remaining_base_amount", "status",
    ):
        assert name in Order.model_fields
