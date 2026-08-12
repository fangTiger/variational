"""净值回撤熔断测试。

这是唯一的**跨腿**保护：TPSL 的 max_equity_loss_pct 每腿 10%，但单向棘轮在
仓位翻向时重置，而网格库存频繁穿零，连续阴跌里每腿各亏 10% 会 0.9ⁿ 复利。

熔断写错了比没有更糟——它会在错误的时机平掉真仓位。所以两个方向都要测：
该触发时必须触发，不该触发时绝不能误伤。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from grid.grid_engine import GridConfig, GridEngine


class _FakeExt:
    """最小交易所桩：只提供熔断路径用到的两个查询。"""

    def __init__(self, equity, signed_size=Decimal("0.01"), fail_balance=False):
        self._equity = equity
        self._signed_size = signed_size
        self._fail_balance = fail_balance
        self.balance_calls = 0

    async def get_balance(self):
        self.balance_calls += 1
        if self._fail_balance:
            raise RuntimeError("网络超时")
        return SimpleNamespace(equity=self._equity)

    async def get_position(self, market):
        return SimpleNamespace(signed_size=self._signed_size)


def _engine(tmp_path, equity, limit=0.12, **kw):
    cfg = GridConfig(
        state_path=str(tmp_path / "grid_state.json"),
        max_drawdown_pct=limit,
    )
    eng = GridEngine(_FakeExt(equity, **kw), cfg)
    eng._go_off_confirmed = _record_halt(eng)
    return eng


def _record_halt(eng):
    async def _fake(signed_size):
        eng.halted_with = signed_size
        return True

    eng.halted_with = None
    return _fake


# ---------- 纯判定 ----------

@pytest.mark.parametrize(
    "equity,peak,limit,expected",
    [
        (880.0, 1000.0, 0.12, True),    # 正好 12%
        (879.0, 1000.0, 0.12, True),    # 超过
        (881.0, 1000.0, 0.12, False),   # 差一点
        (1200.0, 1000.0, 0.12, False),  # 新高
        (500.0, 1000.0, 0.0, False),    # limit=0 视为关闭
        (500.0, 1000.0, -1.0, False),   # 负数也是关闭
        (500.0, 0.0, 0.12, False),      # 无有效峰值
    ],
)
def test_drawdown_breached(equity, peak, limit, expected):
    assert GridEngine.drawdown_breached(equity, peak, limit) is expected


# ---------- 触发路径 ----------

def test_breach_triggers_flatten_and_halt(tmp_path):
    eng = _engine(tmp_path, equity=870.0)
    eng._save_equity_peak(1000.0)
    assert asyncio.run(eng._check_equity_drawdown()) is True
    assert eng.halted_with == Decimal("0.01")  # 带着实际持仓去平仓


def test_no_breach_does_not_halt(tmp_path):
    eng = _engine(tmp_path, equity=950.0)
    eng._save_equity_peak(1000.0)
    assert asyncio.run(eng._check_equity_drawdown()) is False
    assert eng.halted_with is None


def test_new_high_updates_peak(tmp_path):
    eng = _engine(tmp_path, equity=1100.0)
    eng._save_equity_peak(1000.0)
    assert asyncio.run(eng._check_equity_drawdown()) is False
    assert eng._load_equity_peak() == 1100.0


def test_first_run_seeds_peak_without_firing(tmp_path):
    """首次运行没有历史峰值，应当只播种、不触发。"""
    eng = _engine(tmp_path, equity=1000.0)
    assert asyncio.run(eng._check_equity_drawdown()) is False
    assert eng._load_equity_peak() == 1000.0


# ---------- 不该误伤的情况 ----------

def test_balance_failure_does_not_halt(tmp_path):
    """取不到权益时宁可漏防，绝不能凭陈旧数据平掉真仓位。"""
    eng = _engine(tmp_path, equity=800.0, fail_balance=True)
    eng._save_equity_peak(1000.0)
    assert asyncio.run(eng._check_equity_drawdown()) is False
    assert eng.halted_with is None


def test_rate_limited_between_checks(tmp_path):
    """限频生效：短时间内重复调用不应反复查 balance。"""
    eng = _engine(tmp_path, equity=950.0)
    eng._save_equity_peak(1000.0)
    asyncio.run(eng._check_equity_drawdown())
    calls = eng.ext.balance_calls
    asyncio.run(eng._check_equity_drawdown())
    assert eng.ext.balance_calls == calls  # 第二次被限频挡住


def test_disabled_by_config(tmp_path):
    eng = _engine(tmp_path, equity=100.0, limit=0.0)
    eng._save_equity_peak(1000.0)
    assert asyncio.run(eng._check_equity_drawdown()) is False
    assert eng.ext.balance_calls == 0  # 关闭时连查询都不该发


# ---------- 持久化 ----------

def test_peak_survives_restart(tmp_path):
    """峰值只存内存的话，重启就重置、保护形同虚设——而实盘会因网络问题反复重启。"""
    eng1 = _engine(tmp_path, equity=1000.0)
    eng1._save_equity_peak(1000.0)

    eng2 = _engine(tmp_path, equity=1000.0)  # 模拟重启：全新实例
    assert eng2._load_equity_peak() == 1000.0


def test_corrupt_peak_file_is_ignored(tmp_path):
    eng = _engine(tmp_path, equity=1000.0)
    eng._peak_path().parent.mkdir(parents=True, exist_ok=True)
    eng._peak_path().write_text("{坏文件", encoding="utf-8")
    assert eng._load_equity_peak() is None


def test_peak_file_is_valid_json(tmp_path):
    eng = _engine(tmp_path, equity=1000.0)
    eng._save_equity_peak(1234.5)
    data = json.loads(eng._peak_path().read_text(encoding="utf-8"))
    assert data["peak"] == 1234.5
    assert "ts" in data


# ---------- 复利场景（这个熔断存在的理由） ----------

def test_compounding_leg_losses_eventually_breach():
    """每腿 10% 连亏：第 2 腿之后累计就超过 12% 熔断线。

    这正是 TPSL 单腿保护盖不住、需要跨腿熔断的场景。
    """
    peak = 1000.0
    equity = peak
    breached_at = None
    for leg in range(1, 5):
        equity *= 0.90  # 每腿触发一次 10% 止损
        if GridEngine.drawdown_breached(equity, peak, 0.12):
            breached_at = leg
            break
    assert breached_at == 2, f"应在第 2 腿触发，实际 {breached_at}"
