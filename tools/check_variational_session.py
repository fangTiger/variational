"""Variational 会话自检：用捕获的 Cookie 调只读接口，验证会话是否有效。

不下任何单。会话来源（二选一）：
- 环境变量：VARIATIONAL_COOKIE + VARIATIONAL_WALLET_ADDRESS
- JSON 文件：--json secrets/variational_cookies.json

用法：
    python -m tools.check_variational_session
    python -m tools.check_variational_session --json secrets/variational_cookies.json
"""

from __future__ import annotations

import argparse
import asyncio
import json

from adapters.variational_client import Session, VariationalAuthError, VariationalClient


async def _run(session: Session) -> int:
    client = VariationalClient(session)
    try:
        print("→ 调用 /positions …")
        positions = await client.get_positions()
        print("  持仓:", json.dumps(positions, ensure_ascii=False)[:300])

        print("→ 调用 /points/summary …")
        points = await client.get_points_summary()
        print("  积分:", json.dumps(points, ensure_ascii=False)[:300])

        print("\n✅ 会话有效。")
        return 0
    except VariationalAuthError as exc:
        print(f"\n❌ 会话无效：{exc}\n请重新登录导出 Cookie（见 docs/guides/导出-Variational-会话Cookie.md）")
        return 1
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Variational 会话自检（只读）")
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
