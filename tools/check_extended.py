"""Extended 账户自检（只读）：验证 .env 密钥与适配器，确认字段结构。

不下任何单。用法：
    .venv/bin/python -m tools.check_extended
"""

from __future__ import annotations

# 必须在导入 adapters（进而导入 x10/aiohttp）之前配好 CA
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import asyncio  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402

MARKET = "BTC-USD"


async def _run() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    client = ExtendedClient.from_env()
    try:
        print("→ connect() 加载市场元数据 …")
        await client.connect()
        market = client._market(MARKET)
        print(f"  {MARKET} 最小下单量={market.trading_config.min_order_size} "
              f"最小价格变动={market.trading_config.min_price_change}")

        print("→ 行情 get_market_price …")
        price = await client.get_market_price(MARKET)
        print(f"  bid={price.bid} ask={price.ask} mid={price.mid}")

        print("→ 持仓 get_position …")
        pos = await client.get_position(MARKET)
        print(f"  signed_size={pos.signed_size}（0=无仓位，符合未入金预期）")

        print("→ 余额 get_balance …")
        bal = await client.get_balance()
        print(f"  余额原始字段：{bal}")

        print("\n✅ Extended 适配器只读链路验证通过。")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
