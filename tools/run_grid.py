"""网格守护进程（Extended BTC）。默认 dry_run；--live 才真实下单。

live 前提：账户须为该标的空仓（若 farm 的对冲腿还在，会拒绝启动）。

用法：
    PYTHONPATH=. .venv/bin/python -m tools.run_grid --interval 60
    PYTHONPATH=. .venv/bin/python -m tools.run_grid --live --spacing 0.02 --max-inv 400
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from grid.grid_engine import GridConfig, GridEngine  # noqa: E402

_fng_cache = {"ts": 0, "val": None}


def _current_fng() -> int | None:
    """当前恐惧贪婪值，缓存 1 小时。"""
    if time.time() - _fng_cache["ts"] < 3600 and _fng_cache["val"] is not None:
        return _fng_cache["val"]
    try:
        with urllib.request.urlopen("https://api.alternative.me/fng/?limit=1", timeout=10) as r:
            v = int(json.loads(r.read())["data"][0]["value"])
        _fng_cache.update(ts=time.time(), val=v)
        return v
    except Exception:  # noqa: BLE001
        return _fng_cache["val"]


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    p = argparse.ArgumentParser(description="网格守护进程")
    p.add_argument("--live", action="store_true", help="真实下单（默认 dry_run）")
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--spacing", type=float, default=0.02, help="格距（默认2%）")
    p.add_argument("--unit", type=float, default=50.0, help="每格名义USD")
    p.add_argument("--levels", type=int, default=4, help="上下各挂几格（受库存上限与±5%限价约束）")
    p.add_argument("--max-inv", type=float, default=400.0, help="最大库存名义USD")
    p.add_argument("--adx-off", type=float, default=999.0,
                   help="ADX熔断阈值(默认999=禁用；调低才启用强趋势保护)")
    p.add_argument("--adx-resume", type=float, default=999.0,
                   help="ADX恢复阈值(迟滞：熔断后须回落到此值以下才恢复)")
    p.add_argument("--donchian", type=int, default=48,
                   help="Donchian通道周期(小时)，仅供日志，不再触发急停")
    p.add_argument("--account", default="X10_GRID",
                   help="账户环境变量前缀（默认X10_GRID网格账户；用X10则跑farm账户）")
    p.add_argument("--trend-aware", action="store_true",
                   help="启用有界趋势感知(默认关闭=现有行为)")
    p.add_argument("--band-k", type=float, default=1.75, help="band 半宽=k×ATR")
    p.add_argument("--hard-stop-dist", type=float, default=0.12,
                   help="距强平价触发硬止损的比例")
    return p


def _grid_config(args: argparse.Namespace) -> GridConfig:
    """把命令行参数转换为网格配置。"""
    return GridConfig(
        dry_run=not args.live,
        spacing_pct=args.spacing,
        unit_usd=args.unit,
        levels_per_side=args.levels,
        max_inventory_usd=args.max_inv,
        adx_off=args.adx_off,
        adx_resume=args.adx_resume,
        donchian_period=args.donchian,
        poll_interval=args.interval,
        trend_aware=args.trend_aware,
        band_k=args.band_k,
        hard_stop_dist=args.hard_stop_dist,
    )


async def _main(args) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    ext = ExtendedClient.from_env(prefix=args.account)
    print(f"网格账户前缀：{args.account}")
    config = _grid_config(args)
    engine = GridEngine(ext, config, fng_provider=_current_fng)
    trend_mode = "trend-aware" if args.trend_aware else "legacy"
    print(f"网格守护启动（{'实盘' if args.live else 'dry_run'}，{trend_mode}，"
          f"格距{args.spacing*100:.2f}%，"
          f"每边{args.levels}档×${args.unit}，库存上限${args.max_inv}）。Ctrl+C 停止。")
    try:
        await engine.run_forever()
    finally:
        await ext.close()


def main() -> None:
    args = _build_parser().parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
