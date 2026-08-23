"""定时定量双边对冲刷量入口。

默认只打印离线配置摘要；只有显式传入 ``--live`` 才构造并连接交易客户端。
本入口不读取或复用任何网格状态。
"""

from __future__ import annotations

from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import fcntl  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable  # noqa: E402

from adapters.extended_client import ExtendedClient  # noqa: E402
from adapters.hyperliquid_client import HyperliquidClient  # noqa: E402
from adapters.lighter_client import LighterClient  # noqa: E402
from adapters.variational_client import (  # noqa: E402
    Session,
    VariationalAuthError,
    VariationalClient,
)
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
    parser = argparse.ArgumentParser(description="跨交易所定时定量对冲刷量")
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实连接并下单；默认只打印离线配置摘要",
    )
    parser.add_argument("--market", default="BTC", help="主腿标的（默认 BTC）")
    parser.add_argument(
        "--hedge-market",
        default="BTC-USD",
        help="Extended 标的（默认 BTC-USD）",
    )
    parser.add_argument(
        "--notional-min",
        type=int,
        default=2000,
        help="每轮单边名义额下限 USD（默认 2000）",
    )
    parser.add_argument(
        "--notional-max",
        type=int,
        default=2300,
        help="每轮单边名义额上限 USD（默认 2300）",
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
        help=(
            "净敞口数值误差容差下限；实际容差同时取两侧最小下单量较大者"
            "（默认 0.000001）"
        ),
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
        "--primary-venue",
        choices=("lighter", "variational"),
        default="lighter",
        help="主腿交易所（默认 lighter）",
    )
    parser.add_argument(
        "--account",
        default="X10_HEDGE",
        help="Extended 环境变量前缀（默认 X10_HEDGE，仅 --hedge-venue=extended 时生效）",
    )
    parser.add_argument(
        "--hedge-venue",
        choices=("extended", "hyperliquid"),
        default="extended",
        help="对冲腿交易所（默认 extended）",
    )
    parser.add_argument(
        "--hedge-env-prefix",
        default="HYPERLIQUID",
        help="Hyperliquid 环境变量前缀（默认 HYPERLIQUID）",
    )
    return parser


def _build_primary_client(args: argparse.Namespace):
    """按 --primary-venue 装配可交易主腿。"""
    if args.primary_venue == "variational":
        return VariationalClient(Session.from_env())
    if not str(args.lighter_address or "").strip():
        raise RuntimeError("拒绝启动：缺少 Lighter L1 地址")
    api_private_key = os.environ.get("LIGHTER_API_PRIVATE_KEY")
    if not api_private_key:
        raise RuntimeError("拒绝启动：缺少 LIGHTER_API_PRIVATE_KEY")
    return LighterClient(
        l1_address=args.lighter_address,
        trading_enabled=True,
        api_private_key=api_private_key,
        api_key_index=int(os.environ.get("LIGHTER_API_KEY_INDEX", "255")),
    )


def _build_hedge_client(args: argparse.Namespace):
    """按 --hedge-venue 装配对冲腿；两者都以显式交易开关构造。"""
    if args.hedge_venue == "hyperliquid":
        return HyperliquidClient.from_env(
            prefix=args.hedge_env_prefix,
            trading_enabled=True,
        )
    return ExtendedClient.from_env(prefix=args.account)


def _reload_variational_primary() -> VariationalClient:
    """覆盖重读 .env 后重建 Variational 主腿。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ImportError:
        pass
    return VariationalClient(Session.from_env())


def _primary_account_identity(args: argparse.Namespace) -> str | None:
    """从离线配置解析主腿公开账户标识。"""
    if args.primary_venue == "variational":
        return os.environ.get("VARIATIONAL_WALLET_ADDRESS") or None
    return str(args.lighter_address).strip() if args.lighter_address else None


def _hedge_account_identity(args: argparse.Namespace) -> str | None:
    """从离线配置解析对冲腿公开账户标识。"""
    if args.hedge_venue == "hyperliquid":
        return os.environ.get(f"{args.hedge_env_prefix}_ACCOUNT_ADDRESS") or None
    public_key = os.environ.get(f"{args.account}_PUBLIC_KEY")
    vault_id = os.environ.get(f"{args.account}_VAULT_ID")
    return public_key or (f"vault:{vault_id}" if vault_id else None)


def _mask_account(account: str | None) -> str:
    """脱敏公开账户，仅保留首尾各四位。"""
    normalized = str(account or "").strip()
    if not normalized:
        return "未配置"
    if len(normalized) <= 8:
        return "****"
    return f"{normalized[:4]}…{normalized[-4:]}"


def _validate_account_pair(
    *,
    primary_venue: str,
    primary_account: str | None,
    primary_market: str,
    hedge_venue: str,
    hedge_account: str | None,
    hedge_market: str,
) -> None:
    """拒绝同平台、同账户、同市场的自我对冲配置。"""
    if primary_venue.casefold() != hedge_venue.casefold():
        return
    if not primary_account or not hedge_account:
        raise RuntimeError("拒绝启动：同平台双腿缺少账户标识，无法完成账户隔离校验")
    same_account = primary_account.strip().casefold() == hedge_account.strip().casefold()
    same_market = primary_market.strip().casefold() == hedge_market.strip().casefold()
    if same_account and same_market:
        raise RuntimeError(
            "拒绝启动：主腿与对冲腿使用同一交易所账户的同一市场，"
            "会造成持仓互相冲销；请改用独立账户凭据"
        )


def _validate_account_isolation(args: argparse.Namespace) -> None:
    """使用命令行与环境变量完成联网前账户隔离校验。"""
    _validate_account_pair(
        primary_venue=args.primary_venue,
        primary_account=_primary_account_identity(args),
        primary_market=args.market,
        hedge_venue=args.hedge_venue,
        hedge_account=_hedge_account_identity(args),
        hedge_market=args.hedge_market,
    )


def _state_lock_path(state_path: Path | str) -> Path:
    """返回状态文件对应的进程锁路径。"""
    path = Path(state_path)
    return path.with_name(f"{path.name}.lock")


def _pid_is_alive(pid: int) -> bool:
    """检查进程是否仍存在；权限不足时按存活处理。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


@dataclass
class _StatePathLease:
    """持有状态文件的进程级排他租约。"""

    lock_path: Path
    descriptor: int
    owner_token: str

    def release(self) -> None:
        """仅释放当前实例持有的锁与锁文件。"""
        if self.descriptor < 0:
            return
        try:
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            raw = os.read(self.descriptor, 4096).decode("utf-8")
            payload = json.loads(raw)
            if payload.get("owner_token") == self.owner_token:
                self.lock_path.unlink(missing_ok=True)
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
            logger.error("状态文件锁清理失败：%s", exc)
        finally:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self.descriptor)
                self.descriptor = -1


def _acquire_state_path_lease(state_path: Path | str) -> _StatePathLease:
    """非阻塞占用状态路径，并记录持有者 PID 与启动时间。"""
    lock_path = _state_lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                owner = json.loads(os.read(descriptor, 4096).decode("utf-8"))
                owner_pid = int(owner.get("pid"))
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                owner_pid = -1
            raise RuntimeError(
                f"拒绝启动：状态文件 {Path(state_path)} 正在被 PID "
                f"{owner_pid if owner_pid > 0 else '未知'} 的实例占用"
            ) from exc

        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 4096)
        if raw:
            try:
                existing = json.loads(raw.decode("utf-8"))
                existing_pid = int(existing.get("pid"))
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"拒绝启动：状态文件锁 {lock_path} 内容损坏，无法确认持有者"
                ) from exc
            if existing_pid != os.getpid() and _pid_is_alive(existing_pid):
                raise RuntimeError(
                    f"拒绝启动：状态文件 {Path(state_path)} 正在被 PID "
                    f"{existing_pid} 的实例占用"
                )

        started_at = time.time()
        owner_token = f"{os.getpid()}:{time.time_ns()}"
        payload = {
            "pid": os.getpid(),
            "started_at": started_at,
            "owner_token": owner_token,
            "state_path": str(Path(state_path).resolve()),
        }
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        return _StatePathLease(lock_path, descriptor, owner_token)
    except Exception:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        raise


def build_config(args: argparse.Namespace) -> TimedVolumeConfig:
    """把命令行参数转换成策略配置。"""
    return TimedVolumeConfig(
        primary_market=args.market,
        hedge_market=args.hedge_market,
        notional_min_usd=args.notional_min,
        notional_max_usd=args.notional_max,
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
    current_notional = (
        f"{state.current_notional_usd} USD"
        if state.current_notional_usd is not None
        else "无"
    )
    lines = [
        "定时定量对冲配置",
        f"主腿：{args.primary_venue}，账户：{_mask_account(_primary_account_identity(args))}",
        f"对冲腿：{args.hedge_venue}，账户：{_mask_account(_hedge_account_identity(args))}",
        f"标的：{args.market} → {args.hedge_market}",
        f"周期：{args.cycle_hours:g} 小时",
        f"单边名义额区间：{args.notional_min}~{args.notional_max} USD",
        f"初始方向：{args.initial_direction}",
        f"maker 优先等待：{args.maker_timeout:g} 秒",
        f"轮次状态：{args.state_path}",
        f"独立心跳：{args.heartbeat_path}",
        f"当前轮次：{state.round_index}",
        f"当前方向：{current_direction}",
        f"当前轮名义额：{current_notional}",
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
        "primary_pnl": decimal_text(result.primary_pnl),
        "hedge_pnl": decimal_text(result.hedge_pnl),
        "primary_entry": decimal_text(result.primary_entry),
        "hedge_entry": decimal_text(result.hedge_entry),
        "pair_pnl": decimal_text(result.pair_pnl),
        "hedge_available": result.hedge_available,
        "hedge_interlock_active": not result.hedge_available,
        "hedge_interlock_reason": result.interlock_reason,
        "notional_usd": result.notional_usd,
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
            "auth_reload_failed",
        }
        hedge_tolerance = getattr(
            strategy,
            "hedge_tolerance",
            strategy.config.position_tolerance,
        )
        net_is_unsafe = (
            result.net_exposure is not None
            and hedge_tolerance is not None
            and abs(result.net_exposure) >= hedge_tolerance
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
    _validate_account_isolation(args)
    state_lease = _acquire_state_path_lease(config.state_path)
    primary = None
    hedge = None
    try:
        primary = _build_primary_client(args)
        hedge = _build_hedge_client(args)
        await asyncio.gather(primary.connect(), hedge.connect())
        strategy_kwargs = {}
        if args.primary_venue == "variational":
            strategy_kwargs = {
                "auth_error_types": (VariationalAuthError,),
                "on_auth_error": _reload_variational_primary,
            }
        strategy = TimedHedgedVolumeStrategy(
            primary,
            hedge,
            config,
            **strategy_kwargs,
        )
        summary = startup_summary(args, strategy.state)
        print(summary)
        logger.info("定时定量对冲启动\n%s", summary)
        await run_loop(
            strategy,
            poll_interval=args.poll_interval,
            heartbeat_path=args.heartbeat_path,
        )
    finally:
        close_calls = [
            client.close()
            for client in (primary, hedge)
            if client is not None
        ]
        results = await asyncio.gather(*close_calls, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("关闭交易客户端失败：%s", result)
        state_lease.release()


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
