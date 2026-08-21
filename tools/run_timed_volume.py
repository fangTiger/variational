"""定时定量双边对冲刷量入口。

默认只打印离线配置摘要；只有显式传入 ``--live`` 才构造并连接交易客户端。
本入口不读取或复用任何网格状态。
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from adapters.lighter_client import LighterClient  # noqa: E402
from infra.logger import get_logger  # noqa: E402
from timed_volume.strategy import (  # noqa: E402
    RoundDirection,
    TimedHedgedVolumeStrategy,
    TimedVolumeConfig,
    TimedVolumeResult,
    TimedVolumeState,
)

logger = get_logger("timed_volume")

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_STATE = _ROOT / "data" / "timed_volume" / "state.json"
_DEFAULT_HEARTBEAT = _ROOT / "data" / "timed_volume.jsonl"


def build_parser() -> argparse.ArgumentParser:
    """构建定时定量策略命令行参数。"""
    parser = argparse.ArgumentParser(description="Lighter 与 Extended 定时定量对冲刷量")
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实连接并下单；默认只打印离线配置摘要",
    )
    parser.add_argument("--market", default="BTC", help="Lighter 标的（默认 BTC）")
    parser.add_argument(
        "--hedge-market",
        default="BTC-USD",
        help="Extended 标的（默认 BTC-USD）",
    )
    parser.add_argument(
        "--notional",
        type=Decimal,
        default=Decimal("2000"),
        help="每轮单边名义额 USD（默认 2000）",
    )
    parser.add_argument(
        "--cycle-hours",
        type=float,
        default=2.0,
        help="每轮持仓小时数（默认 2）",
    )
    parser.add_argument(
        "--initial-direction",
        choices=("long", "short"),
        default="long",
        help="无历史记录时的首轮方向（默认 long）",
    )
    parser.add_argument(
        "--maker-timeout",
        type=float,
        default=300.0,
        help="maker 等待秒数，超时后才转市价（默认 300）",
    )
    parser.add_argument(
        "--maker-poll",
        type=float,
        default=1.0,
        help="maker 订单状态轮询秒数（默认 1）",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="空闲状态检查与心跳间隔秒数（默认 30）",
    )
    parser.add_argument(
        "--position-tolerance",
        type=Decimal,
        default=Decimal("0.000001"),
        help="两侧净敞口数量容差（默认 0.000001）",
    )
    parser.add_argument(
        "--state-path",
        default=str(_DEFAULT_STATE),
        help=f"轮次状态文件（默认 {_DEFAULT_STATE}）",
    )
    parser.add_argument(
        "--heartbeat-path",
        default=str(_DEFAULT_HEARTBEAT),
        help=f"独立 JSONL 心跳文件（默认 {_DEFAULT_HEARTBEAT}）",
    )
    parser.add_argument(
        "--lighter-address",
        default=os.environ.get("LIGHTER_RH_L1_ADDRESS"),
        help="Lighter L1 地址（默认读取 LIGHTER_RH_L1_ADDRESS）",
    )
    parser.add_argument(
        "--account",
        default="X10_HEDGE",
        help="Extended 环境变量前缀（默认 X10_HEDGE）",
    )
    return parser


def build_config(args: argparse.Namespace) -> TimedVolumeConfig:
    """把命令行参数转换成策略配置。"""
    return TimedVolumeConfig(
        primary_market=args.market,
        hedge_market=args.hedge_market,
        notional_usd=args.notional,
        cycle_seconds=args.cycle_hours * 3600.0,
        initial_direction=RoundDirection(args.initial_direction),
        maker_timeout_s=args.maker_timeout,
        maker_poll_s=args.maker_poll,
        position_tolerance=args.position_tolerance,
        state_path=Path(args.state_path),
    )


def startup_summary(
    args: argparse.Namespace,
    state: TimedVolumeState,
) -> str:
    """生成包含参数与当前轮次恢复状态的中文启动摘要。"""
    current_direction = (
        state.current_direction.value if state.current_direction is not None else "无"
    )
    lines = [
        "定时定量对冲配置",
        f"标的：{args.market} → {args.hedge_market}",
        f"周期：{args.cycle_hours:g} 小时",
        f"单边名义额：{args.notional} USD",
        f"初始方向：{args.initial_direction}",
        f"maker 优先等待：{args.maker_timeout:g} 秒",
        f"轮次状态：{args.state_path}",
        f"独立心跳：{args.heartbeat_path}",
        f"当前轮次：{state.round_index}",
        f"当前方向：{current_direction}",
        f"到期时刻：{state.due_at}",
        f"dry_run：{not args.live}",
    ]
    return "\n".join(lines)


def heartbeat_payload(
    result: TimedVolumeResult,
    *,
    now: float | None = None,
) -> dict:
    """把策略快照转换成独立 JSONL 心跳。"""

    def decimal_text(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    return {
        "ts": time.time() if now is None else float(now),
        "action": result.action,
        "round_index": result.round_index,
        "direction": result.direction.value if result.direction is not None else None,
        "due_at": result.due_at,
        "primary_size": decimal_text(result.primary_size),
        "hedge_size": decimal_text(result.hedge_size),
        "net_exposure": decimal_text(result.net_exposure),
        "hedge_available": result.hedge_available,
        "hedge_interlock_active": not result.hedge_available,
        "hedge_interlock_reason": result.interlock_reason,
        "warnings": list(result.warnings),
    }


def append_heartbeat(payload: dict, path: Path | str = _DEFAULT_HEARTBEAT) -> None:
    """向策略专属文件追加一条 UTF-8 JSON 心跳。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


async def run_loop(
    strategy: TimedHedgedVolumeStrategy,
    *,
    poll_interval: float,
    heartbeat_path: Path | str,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    """运行策略循环；平仓后无等待推进下一轮，其余状态按配置轮询。"""
    should_stop = stop_requested or (lambda: False)
    while not should_stop():
        result = await strategy.run_once()
        try:
            append_heartbeat(heartbeat_payload(result), heartbeat_path)
        except Exception as exc:  # noqa: BLE001 心跳失败不得中断风险收敛
            logger.error("定时策略心跳写入失败（不停止收敛）：%s", exc)
        logger.info(
            "定时轮次动作=%s，轮次=%d，方向=%s，到期=%s，净敞口=%s，互锁=%s",
            result.action,
            result.round_index,
            result.direction.value if result.direction is not None else "无",
            result.due_at,
            result.net_exposure,
            result.interlock_reason,
        )
        if result.action == "closed":
            continue
        urgent_actions = {
            "position_read_failed",
            "execution_uncertain",
            "convergence_failed",
            "close_failed_neutral",
        }
        net_is_unsafe = (
            result.net_exposure is not None
            and abs(result.net_exposure) > strategy.config.position_tolerance
        )
        delay = 1.0 if result.action in urgent_actions or net_is_unsafe else poll_interval
        await asyncio.sleep(delay)


async def run(args: argparse.Namespace) -> None:
    """装配客户端与策略；默认离线，只有 ``--live`` 才接触交易账户。"""
    config = build_config(args)
    if args.poll_interval <= 0:
        raise ValueError("轮询间隔必须大于零")
    if not args.live:
        print(startup_summary(args, TimedVolumeState()))
        return
    if not str(args.lighter_address or "").strip():
        raise RuntimeError("拒绝启动：缺少 Lighter L1 地址")
    api_private_key = os.environ.get("LIGHTER_API_PRIVATE_KEY")
    if not api_private_key:
        raise RuntimeError("拒绝启动：缺少 LIGHTER_API_PRIVATE_KEY")

    primary = LighterClient(
        l1_address=args.lighter_address,
        trading_enabled=True,
        api_private_key=api_private_key,
        api_key_index=int(os.environ.get("LIGHTER_API_KEY_INDEX", "255")),
    )
    hedge = ExtendedClient.from_env(prefix=args.account)
    try:
        await asyncio.gather(primary.connect(), hedge.connect())
        strategy = TimedHedgedVolumeStrategy(primary, hedge, config)
        summary = startup_summary(args, strategy.state)
        print(summary)
        logger.info("定时定量对冲启动\n%s", summary)
        await run_loop(
            strategy,
            poll_interval=args.poll_interval,
            heartbeat_path=args.heartbeat_path,
        )
    finally:
        results = await asyncio.gather(
            primary.close(),
            hedge.close(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("关闭交易客户端失败：%s", result)


def main() -> None:
    """加载本地环境、解析参数并进入异步入口。"""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
