"""TPSL 幂等必须以交易所端实际状态为准，而不是只信引擎内存缓存。

背景：旧代码每 32s 无脑重挂，虽然浪费但顺带具备自愈能力——交易所端 TPSL 因
引擎以外的原因消失时下一轮会自动补回。改成幂等后这层意外保护被拆掉，
若不加交易所侧校验，仓位会无限期裸奔且无人察觉。

规则：
- 适配器未提供 get_position_tpsl → 沿用纯缓存路径（向后兼容旧桩）
- 提供了但查不到 / 触发价不符 / 查询抛异常 → 一律重挂（fail-closed）
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from tests.test_trend_aware_engine import StopExt, _eng


class VerifyExt(StopExt):
    """带交易所侧 TPSL 查询能力的桩；tpsl_on_book 模拟交易所真实状态。"""

    def __init__(self, positions, liq=None):
        super().__init__(positions=positions, liq=liq)
        self.tpsl_on_book = None       # None 表示交易所端没有 TPSL
        self.verify_calls = 0
        self.raise_on_verify = False

    async def get_position_tpsl(self, market):
        self.verify_calls += 1
        if self.raise_on_verify:
            raise RuntimeError("查询交易所 TPSL 失败")
        return self.tpsl_on_book

    async def place_position_stop_loss(self, market, signed_size, trigger_price):
        await super().place_position_stop_loss(market, signed_size, trigger_price)
        # 挂单成功后交易所端就有了这张单
        self.tpsl_on_book = SimpleNamespace(
            id="tpsl-1", trigger_price=trigger_price, status="UNTRIGGERED"
        )


def _short_engine(tmp_path):
    ext = VerifyExt(positions=[Decimal("-0.003")], liq=None)
    return ext, _eng(ext, tmp_path / "s.json")


def test_tpsl_skipped_when_exchange_side_matches(tmp_path) -> None:
    """缓存与交易所端一致时仍须幂等——不能因为加了校验就退回每轮重挂。"""
    ext, eng = _short_engine(tmp_path)

    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))
    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))

    assert len(ext.position_stop_losses) == 1
    assert ext.verify_calls >= 1, "幂等跳过前必须真的向交易所核对过"


def test_tpsl_replaced_when_exchange_side_missing(tmp_path) -> None:
    """交易所端 TPSL 被外部撤掉后，必须重新挂出，不能因内存缓存而跳过。"""
    ext, eng = _short_engine(tmp_path)
    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))

    ext.tpsl_on_book = None            # 模拟人工在网页端撤单/交易所侧清理

    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))

    assert len(ext.position_stop_losses) == 2, "交易所端已无 TPSL 却跳过重挂 → 裸仓"


def test_tpsl_replaced_when_exchange_trigger_differs(tmp_path) -> None:
    """交易所端触发价与引擎认知不符时必须以交易所为准重挂。"""
    ext, eng = _short_engine(tmp_path)
    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))

    ext.tpsl_on_book = SimpleNamespace(
        id="tpsl-x", trigger_price=Decimal("99999"), status="UNTRIGGERED"
    )

    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))

    assert len(ext.position_stop_losses) == 2, "触发价漂移必须重挂"


def test_tpsl_replaced_when_verification_fails(tmp_path) -> None:
    """校验查询本身抛异常时 fail-closed：宁可重挂，也不能凭缓存假设保护还在。"""
    ext, eng = _short_engine(tmp_path)
    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))

    ext.raise_on_verify = True

    asyncio.run(eng._maintain_tpsl(mark=64000.0, signed_size=Decimal("-0.003")))

    assert len(ext.position_stop_losses) == 2, "无法核实保护是否存在时必须重挂"


def test_adapter_picks_only_position_tpsl(tmp_path) -> None:
    """适配器只认整仓 TPSL：普通 LIMIT、非 POSITION 的 TPSL 都不算。"""
    from adapters.extended_client import filter_position_tpsl

    orders = [
        SimpleNamespace(id="lim", type="LIMIT", tp_sl_type=None),
        SimpleNamespace(id="ord-tpsl", type="TPSL", tp_sl_type="ORDER"),
        SimpleNamespace(id="pos-tpsl", type="TPSL", tp_sl_type="POSITION"),
    ]

    picked = filter_position_tpsl(orders)

    assert picked is not None and picked.id == "pos-tpsl"
    assert filter_position_tpsl(orders[:2]) is None
