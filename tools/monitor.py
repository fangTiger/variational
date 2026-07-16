"""监控入口：采集积分 + 资金费，打印并记录快照。

用法：
    # 单次
    PYTHONPATH=. .venv/bin/python -m tools.monitor --once
    # 每 5 分钟循环
    PYTHONPATH=. .venv/bin/python -m tools.monitor --interval 300
"""

from __future__ import annotations

# 必须在导入 x10 前配好 CA
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from adapters.variational_client import Session, VariationalClient  # noqa: E402
from tracking.metrics import MetricsTracker  # noqa: E402
from tracking.monitor import run_once  # noqa: E402


async def _main(once: bool, interval: float) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    var = VariationalClient(Session.from_env())
    ext = ExtendedClient.from_env()
    await ext.connect()
    tracker = MetricsTracker()

    try:
        while True:
            try:
                await run_once(var, ext, tracker)
            except Exception as exc:  # noqa: BLE001 单轮异常不退出
                print(f"⚠️ 本轮采集异常：{type(exc).__name__}: {exc}")
            if once:
                break
            await asyncio.sleep(interval)
    finally:
        await var.close()
        await ext.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="积分 + 资金费监控")
    parser.add_argument("--once", action="store_true", help="只采集一次")
    parser.add_argument("--interval", type=float, default=300, help="循环间隔秒（默认 300）")
    args = parser.parse_args()
    asyncio.run(_main(args.once, args.interval))


if __name__ == "__main__":
    main()
