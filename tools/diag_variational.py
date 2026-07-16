"""Variational 原始诊断：直接打印服务器真实返回，不做任何成功/失败判定。

用于看清一次 /positions 请求的 HTTP 状态码与关键响应头（x-omni-auth、cf-mitigated、
cf-ray、content-type）和响应体片段，判断到底是 200 / 401 / 403 / 挑战。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.diag_variational
"""

from __future__ import annotations

import asyncio

from adapters.variational_client import BASE_URL, USER_AGENT, Session


async def _run(session: Session) -> int:
    from curl_cffi.requests import AsyncSession

    impersonate = "chrome"
    import os

    impersonate = os.getenv("VARIATIONAL_IMPERSONATE") or impersonate

    http = AsyncSession(
        timeout=30.0,
        cookies=session.cookies,
        headers={
            "user-agent": session.user_agent or USER_AGENT,
            "content-type": "application/json",
            "accept": "*/*",
            "referer": "https://omni.variational.io/portfolio?tab=positions",
            "vr-connected-address": session.wallet_address,
        },
        impersonate=impersonate,
    )
    try:
        print(f"impersonate={impersonate}  cookies={len(session.cookies)} 个")
        resp = await http.request("GET", BASE_URL + "/positions")
        print(f"\n=== HTTP {resp.status_code} ===")
        interesting = [
            "x-omni-auth", "cf-mitigated", "cf-ray", "server",
            "content-type", "content-length", "set-cookie",
        ]
        print("--- 关键响应头 ---")
        for h in interesting:
            v = resp.headers.get(h)
            if v is not None:
                # set-cookie 可能含敏感值，只显示是否存在
                if h == "set-cookie":
                    print(f"  {h}: <存在，已隐藏>")
                else:
                    print(f"  {h}: {v}")
        body = resp.text or ""
        print(f"\n--- 响应体前 600 字 ---\n{body[:600]}")
        return 0
    finally:
        await http.close()


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    raise SystemExit(asyncio.run(_run(Session.from_env())))


if __name__ == "__main__":
    main()
