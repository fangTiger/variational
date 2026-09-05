"""Variational 会话 JWT 到期解析测试；全部只做本地 base64 解码。"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from adapters.variational_client import get_session_expiry


NOW = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)


def _jwt(payload: object, *, marker: str = "signature") -> str:
    """构造无需签名的测试 JWT。"""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{header}.{body}.{marker}"


def test_normal_jwt_returns_utc_expiry_and_remaining_duration() -> None:
    expires_at = NOW + timedelta(hours=49, minutes=30)

    expiry = get_session_expiry(
        {"vr-token": _jwt({"exp": expires_at.timestamp()})},
        wallet_address="0xabc",
        now=NOW,
    )

    assert expiry is not None
    assert expiry.expires_at == expires_at
    assert expiry.expires_at.tzinfo is timezone.utc
    assert expiry.remaining == timedelta(hours=49, minutes=30)
    assert expiry.hours_left == pytest.approx(49.5)


@pytest.mark.parametrize(
    "cookies",
    [
        {"vr-token": "不是-JWT"},
        {"vr-token": "a.@@@.c"},
        {"vr-token": _jwt({"sub": "wallet"})},
        {"vr-token": _jwt({"exp": "not-a-timestamp"})},
        {},
    ],
)
def test_malformed_non_jwt_or_missing_exp_degrades_to_none(
    cookies: dict[str, str],
) -> None:
    assert get_session_expiry(cookies, wallet_address="0xabc", now=NOW) is None


def test_wallet_suffixed_token_uses_matching_wallet_only() -> None:
    expected = NOW + timedelta(hours=36)
    cookies = {
        "vr-token-0xwrong": _jwt({"exp": (NOW + timedelta(hours=1)).timestamp()}),
        "vr-token-0xAbC": _jwt({"exp": expected.timestamp()}),
    }

    expiry = get_session_expiry(cookies, wallet_address="0xabc", now=NOW)

    assert expiry is not None
    assert expiry.expires_at == expected


def test_plain_token_takes_priority_over_wallet_suffixed_token() -> None:
    expected = NOW + timedelta(hours=72)
    cookies = {
        "vr-token": _jwt({"exp": expected.timestamp()}),
        "vr-token-0xabc": _jwt({"exp": (NOW + timedelta(hours=1)).timestamp()}),
    }

    expiry = get_session_expiry(cookies, wallet_address="0xabc", now=NOW)

    assert expiry is not None
    assert expiry.expires_at == expected

