"""人工状态和平仓的结构来源回归测试；所有客户端调用均为离线替身。"""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from adapters.base import Position
from tools import hedge_swap_carry as carry


@pytest.fixture
def heartbeat(monkeypatch, tmp_path):
    """隔离真实守护文件，默认模拟心跳缺失。"""
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(carry, "SWAP_CARRY_GUARD_HEARTBEAT", path)
    monkeypatch.setattr(carry, "SWAP_CARRY_GUARD_STATE", tmp_path / "state.json")
    return path


def client_for(sizes):
    """仅允许读取指定持仓和费率，禁止构造真实客户端。"""
    client = AsyncMock(spec=["get_position", "get_positions", "get_funding_rate", "get_swap_funding", "close"])
    for method in ("get_position", "get_positions", "get_funding_rate", "get_swap_funding", "close"):
        setattr(client, method, AsyncMock())
    client.get_position.side_effect = lambda name, exact: Position(name, Decimal(sizes.get(name, "0")))
    client.get_positions.return_value = [
        {"position_info": {"instrument": {"underlying": name}, "qty": qty}}
        for name, qty in sizes.items()
    ]
    client.get_funding_rate.return_value = Decimal("0.1")
    client.get_swap_funding.side_effect = RuntimeError("离线费率不可用")
    return client


@pytest.mark.parametrize("name,explicit,sizes,missing", [
    ("XAUS_XAU", None, {"XAUS": "0.45224", "XAU": "-0.45224"}, False),
    ("XAU_XAUT", None, {"XAU": "0.45224", "XAUT": "-0.45224"}, False),
    ("XAU_XAUT", "XAUS_XAU", {"XAUS": "0.45224", "XAU": "-0.45224"}, False),
    ("XAUS_XAU", None, {"XAUS": "0.45224"}, True),
])
def test_status_structure(heartbeat, monkeypatch, capsys, name, explicit, sizes, missing):
    heartbeat.write_text(json.dumps({"structure": name}))
    client = client_for(sizes)
    monkeypatch.setattr(carry, "_load", AsyncMock(return_value=client))
    monkeypatch.setattr(carry, "_funding_rate_for_leg", AsyncMock(return_value=Decimal("0.1")))
    argv = ["status"] + (["--structure", explicit] if explicit else [])
    asyncio.run(carry._main(carry.build_parser().parse_args(argv)))
    output = capsys.readouterr().out
    assert f"结构={explicit or name}" in output
    assert ("缺腿裸仓告警" in output) is missing
    if not missing:
        assert "✅ 近似中性" in output
        for symbol, qty in sizes.items():
            direction = "多头" if Decimal(qty) > 0 else "空头"
            assert f"{symbol} {direction}" in output


@pytest.mark.parametrize("payload", [None, {}, {"structure": "INVALID"}, {"structure": []}, {"structure": ""}, "损坏 JSON"])
def test_unknown_status_and_close(heartbeat, capsys, payload):
    if payload is not None:
        heartbeat.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    sizes = {"XAUS": "0.45224", "XAU": "-0.45224", "BTC": "0.01", "ETH": "-2"}
    client = client_for(sizes)
    asyncio.run(carry.cmd_status(client))
    output = capsys.readouterr().out
    assert "结构未知（守护心跳不可用）" in output
    assert "检查守护进程是否在运行" in output
    assert "缺腿裸仓告警" not in output
    assert "净 delta" not in output
    for symbol, qty in sizes.items():
        assert symbol in output and qty in output
    client.get_positions.assert_awaited_once()
    client.get_position.assert_not_awaited()
    with pytest.raises(SystemExit, match="显式指定.*--structure"):
        asyncio.run(carry.cmd_close(object(), yes=True))


@pytest.mark.parametrize("name,explicit", [("XAUS_XAU", None), ("XAU_XAUT", None), ("XAU_XAUT", "XAUS_XAU"), (None, "XAUS_XAU")])
def test_close_structure(heartbeat, monkeypatch, capsys, name, explicit):
    if name:
        heartbeat.write_text(json.dumps({"structure": name}))
    client = client_for({})
    monkeypatch.setattr(carry, "_load", AsyncMock(return_value=client))
    argv = ["close", "--dry-run"] + (["--structure", explicit] if explicit else [])
    asyncio.run(carry._main(carry.build_parser().parse_args(argv)))
    assert f"结构={explicit or name}" in capsys.readouterr().out
    assert [call.args[0] for call in client.get_position.await_args_list] == [
        leg.underlying for leg in carry.resolve_structure(explicit or name).legs
    ]
