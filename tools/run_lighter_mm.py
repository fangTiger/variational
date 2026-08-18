"""Lighter BTC 无对冲做市入口。默认 dry-run；仅 ``--live`` 允许真实下单。"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from adapters.lighter_client import LighterClient  # noqa: E402
from adapters.market_data import ExtendedCandleSource  # noqa: E402
from grid.grid_engine import GridConfig, GridEngine  # noqa: E402
from infra.logger import get_logger  # noqa: E402

logger = get_logger("lighter_mm")

_ROOT = Path(__file__).resolve().parent.parent
_MM_HEARTBEAT = _ROOT / "data" / "lighter_mm.jsonl"

# 实盘库存硬顶必须独立于命令行参数，防止手滑放大风险。
MAX_INVENTORY_USD = 500.0
MIN_UNIT_USD = 15.0


def _load_environment() -> None:
    """加载本地环境变量；未安装 python-dotenv 时沿用进程环境。"""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def build_parser() -> argparse.ArgumentParser:
    """构建 Lighter 做市命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="Lighter 无对冲网格做市机器人")
    parser.add_argument("--live", action="store_true", help="真实下单（默认 dry_run）")
    parser.add_argument("--market", default="BTC", help="Lighter 标的名（默认 BTC）")
    parser.add_argument("--unit", type=float, default=50.0, help="每格名义 USD（默认 50）")
    parser.add_argument("--levels", type=int, default=4, help="上下各挂几档（默认 4）")
    parser.add_argument(
        "--max-inv",
        type=float,
        default=500.0,
        help="库存上限 USD（默认 500，硬顶 500）",
    )
    parser.add_argument(
        "--spacing",
        type=float,
        default=0.000986,
        help="相邻网格格距（默认 0.000986）",
    )
    parser.add_argument("--interval", type=float, default=2.5, help="快轮询秒数（默认 2.5）")
    parser.add_argument(
        "--slow-interval",
        type=float,
        default=30.0,
        help="慢路径秒数（默认 30）",
    )
    parser.add_argument(
        "--lighter-address",
        default=os.environ.get("LIGHTER_RH_L1_ADDRESS"),
        help="Lighter L1 地址（默认读取 LIGHTER_RH_L1_ADDRESS）",
    )
    parser.add_argument(
        "--candle-account",
        default="X10_HEDGE",
        help="Extended K 线账户前缀（默认 X10_HEDGE）",
    )
    parser.add_argument(
        "--state-path",
        default="data/lighter_mm/state.json",
        help="做市状态文件路径（默认 data/lighter_mm/state.json）",
    )
    parser.add_argument(
        "--trend-aware",
        action="store_true",
        help="启用趋势感知路径（默认关闭）",
    )
    return parser


def _reject_startup(message: str) -> None:
    """打印中文拒绝原因并终止进程。"""
    print(f"拒绝启动：{message}", file=sys.stderr)
    raise SystemExit(2)


def validate_args(args: argparse.Namespace) -> None:
    """在构造任何客户端前校验做市资金与签名安全边界。"""
    if not str(args.lighter_address or "").strip():
        _reject_startup("缺少 Lighter L1 地址")
    state_dir = Path(args.state_path).parent.resolve()
    extended_state_dir = Path("data/grid_state.json").parent.resolve()
    if state_dir == extended_state_dir:
        _reject_startup(
            "状态目录会与 Extended 网格的 equity_peak.json / fills.jsonl / "
            "grid_live.json 撞车"
        )
    if args.levels <= 0:
        _reject_startup("上下档位数必须大于 0")
    if args.max_inv > MAX_INVENTORY_USD:
        _reject_startup(f"库存上限不得超过硬顶 {MAX_INVENTORY_USD:g} USD")
    if args.unit < MIN_UNIT_USD:
        _reject_startup(f"每格名义不得低于 {MIN_UNIT_USD:g} USD")
    if args.unit * args.levels > args.max_inv:
        _reject_startup("每格名义乘档位数超过单边库存上限")
    if args.live and not (os.environ.get("LIGHTER_API_PRIVATE_KEY") or "").strip():
        _reject_startup("实盘模式缺少环境变量 LIGHTER_API_PRIVATE_KEY")


def _grid_config(args: argparse.Namespace) -> GridConfig:
    """把命令行参数转换为通用网格引擎配置。"""
    return GridConfig(
        market=args.market,
        spacing_pct=args.spacing,
        unit_usd=args.unit,
        max_inventory_usd=args.max_inv,
        levels_per_side=args.levels,
        poll_interval=args.interval,
        slow_interval=args.slow_interval,
        dry_run=not args.live,
        state_path=args.state_path,
        trend_aware=args.trend_aware,
    )


def _append_heartbeat(payload: dict, path: Path | None = None) -> None:
    """向 Lighter 做市历史追加一条 JSON 心跳。"""
    target = _MM_HEARTBEAT if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _heartbeat_payload(
    engine,
    args: argparse.Namespace,
    *,
    success: bool,
) -> dict:
    """根据入口配置与引擎本轮缓存构造心跳。"""
    position_size = getattr(
        engine,
        "_lighter_mm_round_inv",
        getattr(engine, "_last_inv", None),
    )
    mark_price = getattr(
        engine,
        "_lighter_mm_round_mark",
        getattr(engine, "_last_mark", None),
    )
    inventory_usd = None
    if position_size is not None and mark_price is not None:
        try:
            inventory_usd = float(position_size) * float(mark_price)
        except (TypeError, ValueError, OverflowError):
            inventory_usd = None
    orders = getattr(engine, "_orders", None)
    return {
        "ts": time.time(),
        "market": args.market,
        "dry_run": not args.live,
        "levels": args.levels,
        "unit": args.unit,
        "max_inv": args.max_inv,
        "interval": args.interval,
        "position_size": (
            str(position_size) if position_size is not None else None
        ),
        "inventory_usd": inventory_usd,
        "open_orders": len(orders) if orders is not None else None,
        "success": success,
    }


def _install_heartbeat(engine, args: argparse.Namespace) -> None:
    """在不改动通用引擎的前提下，为每轮执行安装心跳。"""
    if getattr(engine, "_lighter_mm_heartbeat_installed", False):
        return

    original_run_once = getattr(engine, "run_once", None)
    if not callable(original_run_once):
        return

    # 通用引擎没有公开逐轮回调。这里复用其本轮已经读取的持仓与已维护的挂单表，
    # 避免为了心跳额外访问交易所；若轮次在读取持仓前失败，持仓字段会保留为空。
    original_inventory = getattr(engine, "_inv", None)
    if callable(original_inventory):

        async def observe_inventory(*call_args, **call_kwargs):
            result = await original_inventory(*call_args, **call_kwargs)
            engine._last_inv = result[0]
            engine._last_mark = result[1]
            engine._lighter_mm_round_inv = result[0]
            engine._lighter_mm_round_mark = result[1]
            return result

        engine._inv = observe_inventory

    async def run_once_with_heartbeat(*call_args, **call_kwargs):
        engine._lighter_mm_round_inv = None
        engine._lighter_mm_round_mark = None
        success = False
        try:
            result = await original_run_once(*call_args, **call_kwargs)
            success = True
            return result
        finally:
            try:
                _append_heartbeat(
                    _heartbeat_payload(engine, args, success=success)
                )
            except Exception as exc:  # noqa: BLE001 心跳失败不能改变交易轮次结果
                logger.error("写入 Lighter 做市心跳失败：%s", exc)

    engine.run_once = run_once_with_heartbeat
    engine._lighter_mm_heartbeat_installed = True


async def _close_clients(*clients) -> None:
    """并行关闭已构造客户端，一个失败不阻断另一个。"""
    active_clients = [client for client in clients if client is not None]
    results = await asyncio.gather(
        *(client.close() for client in active_clients),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            logger.error("关闭客户端失败：%s", result)


async def _main(args: argparse.Namespace) -> None:
    """完成启动自检、引擎装配、运行与客户端回收。"""
    validate_args(args)
    Path(args.state_path).parent.mkdir(parents=True, exist_ok=True)
    ext = None
    candle_ext = None
    try:
        ext = LighterClient(
            l1_address=args.lighter_address,
            api_private_key=os.environ.get("LIGHTER_API_PRIVATE_KEY"),
            api_key_index=int(os.environ.get("LIGHTER_API_KEY_INDEX", 255)),
            trading_enabled=args.live,
        )
        candle_ext = ExtendedClient.from_env(args.candle_account)
        candle_source = ExtendedCandleSource(
            candle_ext,
            market_override="BTC-USD",
        )
        config = _grid_config(args)
        engine = GridEngine(ext, config, candle_source=candle_source)
        _install_heartbeat(engine, args)
        logger.info(
            "Lighter 做市启动：标的=%s，档位=每边%d，每格金额=$%g，"
            "库存上限=$%g，格距=%g，轮询间隔=%g秒，慢路径=%g秒，dry_run=%s",
            args.market,
            args.levels,
            args.unit,
            args.max_inv,
            args.spacing,
            args.interval,
            args.slow_interval,
            not args.live,
        )
        await engine.run_forever()
    finally:
        await _close_clients(ext, candle_ext)


def main() -> None:
    """加载环境、解析参数并启动异步做市守护进程。"""
    _load_environment()
    args = build_parser().parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
