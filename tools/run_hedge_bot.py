"""自动对冲守护进程：维持 delta 中性 + 保证金降险 + Cookie 自愈 + 权益记录。

引擎每轮：读两腿 → 单腿故障则急停平仓 → 保证金过低则按比例降险 → 净 delta 漂移则
再平衡 Extended → 记录权益快照。primary(Variational) 会话失效时自动从 .env 重读 Cookie。

⚠️ 默认 dry_run（只算不下单）。加 --live 才真实交易。需在允许交易的 IP 上跑。
前提：仓位已用 tools.hedge open 开好；本进程只负责维持与保护，不主动开新仓。

用法（Codex 允许交易 IP）：
    # 先干跑观察决策
    PYTHONPATH=. .venv/bin/python -m tools.run_hedge_bot --interval 60
    # 真实自动管理
    PYTHONPATH=. .venv/bin/python -m tools.run_hedge_bot --live --interval 60
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from adapters.variational_client import Session, VariationalClient  # noqa: E402
from engine.hedge_engine import HedgeConfig, HedgeEngine  # noqa: E402
from tracking.track_equity_util import snapshot_and_append  # noqa: E402


def _reload_primary() -> VariationalClient:
    """从 .env 重读 Cookie 重建 Variational 客户端（override 覆盖已加载的旧值）。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ImportError:
        pass
    return VariationalClient(Session.from_env())


async def _main(live: bool, interval: float) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    var = VariationalClient(Session.from_env())
    ext = ExtendedClient.from_env()

    config = HedgeConfig(dry_run=not live, poll_interval=interval)
    engine = HedgeEngine(var, ext, config)

    # 每轮记录权益快照（用引擎当前的 primary/hedge，兼容 Cookie 自愈换过的对象）
    async def _snap(_state) -> None:
        try:
            snap = await snapshot_and_append(engine.primary, engine.hedge)
            print(json.dumps({k: snap[k] for k in ("total_equity", "net_delta", "points_total")}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 快照失败：{exc}")

    engine._on_snapshot = _snap
    engine._on_auth_error = _reload_primary

    print(f"启动自动对冲守护（{'实盘' if live else 'dry_run'}，间隔 {interval}s）。Ctrl+C 停止。")
    try:
        await engine.run_forever()
    finally:
        await var.close()
        await ext.close()


def main() -> None:
    p = argparse.ArgumentParser(description="自动对冲守护进程")
    p.add_argument("--live", action="store_true", help="真实交易（默认 dry_run）")
    p.add_argument("--interval", type=float, default=60, help="轮询间隔秒（默认60）")
    args = p.parse_args()
    asyncio.run(_main(args.live, args.interval))


if __name__ == "__main__":
    main()
