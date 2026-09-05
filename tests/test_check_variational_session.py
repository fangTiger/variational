"""Variational 人工会话自检输出测试。"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

from adapters.variational_client import Session
from tools import check_variational_session


NOW = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)


def _jwt(expires_at: datetime) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at.timestamp()}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.test-signature"


def test_selfcheck_prints_expiry_without_printing_token(monkeypatch, capsys) -> None:
    token = _jwt(NOW + timedelta(hours=25, minutes=30))

    class FakeClient:
        def __init__(self, _session: Session) -> None:
            pass

        async def get_positions(self) -> list[object]:
            return []

        async def get_points_summary(self) -> dict[str, int]:
            return {"points": 1}

        async def close(self) -> None:
            pass

    monkeypatch.setattr(check_variational_session, "VariationalClient", FakeClient)

    result = asyncio.run(
        check_variational_session._run(
            Session(cookies={"vr-token": token}, wallet_address="0xabc"),
            now=NOW,
        )
    )
    output = capsys.readouterr().out

    assert result == 0
    assert "会话到期" in output
    assert "剩余 25.5 小时" in output
    assert token not in output

