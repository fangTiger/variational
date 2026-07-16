"""Variational 只读端点批量导出（侦察用，须在与浏览器同 IP 的机器上跑）。

逐个调用只读 GET 端点，把原始 JSON 打印并保存到 data/variational_dump.json，
用于确认 /positions、/points/*、/metadata/* 等的真实字段结构。

不下任何单。会话来源：
- 环境变量：VARIATIONAL_COOKIE + VARIATIONAL_WALLET_ADDRESS [+ VARIATIONAL_USER_AGENT]
- 或 JSON 文件：--json secrets/variational_cookies.json

用法：
    python -m tools.dump_variational
    python -m tools.dump_variational --json secrets/variational_cookies.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from adapters.variational_client import Session, VariationalAuthError, VariationalClient

# 只读端点清单（均为 GET，安全）
ENDPOINTS = [
    "/positions",
    "/orders/v2",
    "/trades",
    "/funding/v2",
    "/portfolio/trade_volume",
    "/points/summary",
    "/points/multiplier_info",
    "/points/next_drop_ts",
    "/points/history",
    "/referrals/summary",
    "/metadata/config",
    "/metadata/supported_assets",
    "/metadata/tiers",
    "/v1/profile/account",
]

_OUT = Path(__file__).resolve().parent.parent / "data" / "variational_dump.json"


async def _run(session: Session) -> int:
    client = VariationalClient(session)
    results: dict[str, object] = {}
    try:
        for path in ENDPOINTS:
            try:
                data = await client.raw(path)
                results[path] = data
                preview = json.dumps(data, ensure_ascii=False)
                print(f"✅ {path}\n   {preview[:400]}\n")
            except VariationalAuthError as exc:
                print(f"❌ {path} 会话/挑战失败：{exc}")
                results[path] = {"__error__": str(exc)}
                # 认证类错误通常整体失效，直接停
                break
            except Exception as exc:  # noqa: BLE001 单端点失败不影响其它
                print(f"⚠️ {path} 出错：{type(exc).__name__}: {exc}")
                results[path] = {"__error__": f"{type(exc).__name__}: {exc}"}
    finally:
        await client.close()

    _OUT.parent.mkdir(exist_ok=True)
    _OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 已保存全部原始响应到 {_OUT}")
    print("请把这个文件（或上面的控制台输出）发回。")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Variational 只读端点批量导出")
    parser.add_argument("--json", help="会话 JSON 文件路径", default=None)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    session = Session.from_json(args.json) if args.json else Session.from_env()
    raise SystemExit(asyncio.run(_run(session)))


if __name__ == "__main__":
    main()
