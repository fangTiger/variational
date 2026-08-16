"""拉取资金费流水。

Extended 按小时收付资金费，量级很小（实测日均 +$0.019，约占闭环利润
0.24%），但归因恒等式里有这一项，不采集就永远闭不上、残差校验会一直误报。

端点已验证可用：GET /api/v1/user/funding/history?market=BTC-USD&limit=N
返回字段：id / fundingFee / paidTime
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import json  # noqa: E402
import os  # noqa: E402
import ssl  # noqa: E402
import urllib.request  # noqa: E402

import certifi  # noqa: E402

_BASE = "https://api.starknet.extended.exchange"


def fetch_funding(market: str = "BTC-USD", limit: int = 500) -> list[dict]:
    """返回归一化的资金费记录；失败返回空列表，不抛错。"""
    api_key = os.getenv("X10_GRID_API_KEY")
    if not api_key:
        return []
    url = f"{_BASE}/api/v1/user/funding/history?market={market}&limit={limit}"
    request = urllib.request.Request(
        url,
        headers={
            "X-Api-Key": api_key,
            "User-Agent": "grid-bot/1.0",
            "Accept": "application/json",
        },
    )
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=25, context=context) as response:
            payload = json.loads(response.read())
    except Exception:  # noqa: BLE001
        return []
    records = []
    for row in payload.get("data") or []:
        try:
            records.append(
                {
                    "funding_id": str(row["id"]),
                    "ts": float(row["paidTime"]) / 1000.0,
                    "fee": float(row["fundingFee"]),
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return records
