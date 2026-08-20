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
from grid.grid_engine import (  # noqa: E402
    RISK_LAYER_REQUIREMENTS,
    GridConfig,
    GridEngine,
)
from infra.logger import get_logger  # noqa: E402

logger = get_logger("lighter_mm")

_ROOT = Path(__file__).resolve().parent.parent
_MM_HEARTBEAT = _ROOT / "data" / "lighter_mm.jsonl"

# 与实盘 Extended 网格的 --max-inv 3750 对齐，且匹配 Lighter 账户抵押规模。
# 硬顶只用于防止手滑多打一个零，不限制有意且显式的规模调整。
MAX_INVENTORY_USD = 3750.0
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
        help=f"库存上限 USD（默认 500，硬顶 {MAX_INVENTORY_USD:g}）",
    )
    parser.add_argument(
        "--wallet-exposure-ratio",
        type=float,
        default=None,
        help="权益比例库存上限倍数；默认不启用，与 --max-inv 绝对硬顶取更紧者",
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
        "--trading-window-start",
        default=None,
        metavar="HH:MM",
        help="UTC+8 交易窗口起点；须与终点同时配置，默认不启用窗口",
    )
    parser.add_argument(
        "--trading-window-end",
        default=None,
        metavar="HH:MM",
        help="UTC+8 交易窗口终点；起点包含、终点不包含",
    )
    parser.add_argument(
        "--maker-first-timeout",
        type=float,
        default=15.0,
        help="计划离场先挂 maker 的等待秒数，超时必须转市价（默认 15）",
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
    parser.add_argument(
        "--max-drawdown",
        type=float,
        default=None,
        help="净值自峰值回撤阈值（默认值仍为 0.12，但必须显式传入或声明放弃）",
    )
    parser.add_argument(
        "--hard-stop-dist",
        type=float,
        default=None,
        help="距强平价硬止损比例（默认值仍为 0.12，但必须显式传入或声明放弃）",
    )
    parser.add_argument(
        "--hedge-heartbeat-path",
        default=None,
        help="对冲 JSONL 心跳路径；默认不配置，即不启用互锁",
    )
    parser.add_argument(
        "--hedge-interval",
        type=float,
        default=30.0,
        help="对冲轮询间隔秒数，互锁超时固定取其 3 倍（默认 30）",
    )
    parser.add_argument(
        "--waive-risk",
        action="append",
        default=[],
        choices=tuple(RISK_LAYER_REQUIREMENTS),
        metavar="LAYER",
        help="知情放弃指定风控层；可重复传入，默认不放弃任何层",
    )
    parser.add_argument(
        "--risk-selfcheck-only",
        action="store_true",
        help="只执行离线风控完整性自检，不连接交易所或进入交易循环",
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
    if args.unit > args.max_inv:
        _reject_startup("每格名义不得超过单边库存上限")
    if (
        getattr(args, "hedge_heartbeat_path", None) is not None
        and getattr(args, "hedge_interval", 0) <= 0
    ):
        _reject_startup("启用对冲互锁时，对冲轮询间隔必须大于 0")
    if (
        args.live
        and not getattr(args, "risk_selfcheck_only", False)
        and not (os.environ.get("LIGHTER_API_PRIVATE_KEY") or "").strip()
    ):
        _reject_startup("实盘模式缺少环境变量 LIGHTER_API_PRIVATE_KEY")


def _grid_config(args: argparse.Namespace) -> GridConfig:
    """把命令行参数转换为通用网格引擎配置。"""
    max_drawdown_arg = getattr(args, "max_drawdown", None)
    hard_stop_arg = getattr(args, "hard_stop_dist", None)
    max_drawdown = 0.12 if max_drawdown_arg is None else max_drawdown_arg
    hard_stop_dist = 0.12 if hard_stop_arg is None else hard_stop_arg
    explicit_risk_flags = tuple(
        flag
        for flag, present in (
            ("--trend-aware", args.trend_aware),
            ("--max-drawdown", max_drawdown_arg is not None),
            ("--hard-stop-dist", hard_stop_arg is not None),
            (
                "--hedge-heartbeat-path",
                bool(str(getattr(args, "hedge_heartbeat_path", "") or "").strip()),
            ),
        )
        if present
    )
    hedge_heartbeat_path = getattr(args, "hedge_heartbeat_path", None)
    hedge_interval = float(getattr(args, "hedge_interval", 30.0))
    return GridConfig(
        market=args.market,
        spacing_pct=args.spacing,
        unit_usd=args.unit,
        max_inventory_usd=args.max_inv,
        wallet_exposure_ratio=getattr(args, "wallet_exposure_ratio", None),
        levels_per_side=args.levels,
        poll_interval=args.interval,
        slow_interval=args.slow_interval,
        trading_window_start=getattr(args, "trading_window_start", None),
        trading_window_end=getattr(args, "trading_window_end", None),
        maker_first_timeout_s=getattr(args, "maker_first_timeout", 15.0),
        dry_run=not args.live,
        state_path=args.state_path,
        trend_aware=args.trend_aware,
        max_drawdown_pct=max_drawdown,
        hard_stop_dist=hard_stop_dist,
        risk_waivers=tuple(getattr(args, "waive_risk", ())),
        explicit_risk_flags=explicit_risk_flags,
        hedge_heartbeat_path=hedge_heartbeat_path,
        hedge_heartbeat_timeout_s=(
            3 * hedge_interval if hedge_heartbeat_path is not None else None
        ),
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
    trading_window_state = getattr(engine, "trading_window_state", "disabled")
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
        "trading_window_state": trading_window_state,
        "planned_stop": trading_window_state == "planned_stop",
        "hedge_interlock_active": getattr(
            engine,
            "hedge_interlock_active",
            False,
        ),
        "hedge_interlock_reason": getattr(
            engine,
            "hedge_interlock_reason",
            "未配置",
        ),
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
            trading_enabled=(
                args.live and not getattr(args, "risk_selfcheck_only", False)
            ),
        )
        candle_ext = ExtendedClient.from_env(args.candle_account)
        candle_source = ExtendedCandleSource(
            candle_ext,
            market_override="BTC-USD",
        )
        config = _grid_config(args)
        engine = GridEngine(ext, config, candle_source=candle_source)
        if getattr(args, "risk_selfcheck_only", False):
            engine.validate_risk_controls()
            logger.info("风控完整性自检通过；未连接交易所，未启动交易循环")
            return
        _install_heartbeat(engine, args)
        cap_mode = (
            "仅绝对硬顶"
            if config.wallet_exposure_ratio is None
            else f"权益×{config.wallet_exposure_ratio:g} 与绝对硬顶取更紧者"
        )
        window_summary_getter = getattr(engine, "trading_window_summary", None)
        window_summary = (
            window_summary_getter()
            if callable(window_summary_getter)
            else "未启用（全天交易）"
        )
        logger.info(
            "Lighter 做市启动：标的=%s，档位=每边%d，每格金额=$%g，"
            "库存硬顶=$%g，上限策略=%s，格距=%g，轮询间隔=%g秒，"
            "慢路径=%g秒，交易窗口=%s，对冲互锁=%s，dry_run=%s",
            args.market,
            args.levels,
            args.unit,
            args.max_inv,
            cap_mode,
            args.spacing,
            args.interval,
            args.slow_interval,
            window_summary,
            (
                f"启用（心跳={config.hedge_heartbeat_path}，"
                f"超时={config.hedge_heartbeat_timeout_s:g}秒）"
                if config.hedge_heartbeat_path is not None
                else "未配置"
            ),
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
