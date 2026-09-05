"""Swap carry 无人值守风控守护进程。

本进程只会减少或清空既有仓位，绝不包含开仓动作。launchd 每五分钟以
``--once`` 启动一轮；任何不确定的强平或交易时段状态都按失败关闭处理。
"""

from __future__ import annotations

# 必须在导入交易相关依赖前配好 CA。
from infra.runtime import ensure_ssl_cert

ensure_ssl_cert()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping, Sequence  # noqa: E402

from adapters.base import Position  # noqa: E402
from adapters.variational_client import (  # noqa: E402
    VariationalAuthError,
    VariationalJurisdictionError,
)
from engine.swap_trading_schedule import SwapTradingSchedule  # noqa: E402
from tools.alert_check import notify  # noqa: E402
from tools import hedge_swap_carry as execution  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KILL_SWITCH = execution.SWAP_CARRY_KILL_SWITCH
DEFAULT_HEARTBEAT = execution.SWAP_CARRY_GUARD_HEARTBEAT
DEFAULT_STATE = execution.SWAP_CARRY_GUARD_STATE
DEFAULT_AUDIT_LOG = PROJECT_ROOT / "data" / "swap_carry_guard_audit.jsonl"

IMBALANCE_RATIO = Decimal("0.05")
LIQUIDATION_ALERT_RATIO = Decimal("0.015")
PRE_CLOSE_MINUTES = 30
LONG_CLOSURE_THRESHOLD = timedelta(hours=4)
CLOSE_RETRIES = 3
RETRY_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class FlattenResult:
    """一次退出动作的结果。"""

    complete: bool
    pending_xaus: bool
    message: str


class CloseActionError(RuntimeError):
    """有限次数重试后仍无法完成的平仓错误。"""


def _json_default(value: object) -> str:
    """把审计载荷中的时间和十进制数稳定转换为字符串。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"无法序列化 {type(value).__name__}")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    """原子覆盖单份状态或心跳文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_audit(path: Path, payload: Mapping[str, object]) -> None:
    """追加一条不可覆盖的 JSONL 审计记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n"
        )


def _read_failure_count(path: Path) -> int:
    """读取上一轮连续失败数；损坏状态不冒充健康。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = int(payload.get("consecutive_failures", 0))
        return max(0, value)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _state_payload(
    *,
    observed_at: datetime,
    status: str,
    message: str,
    consecutive_failures: int,
) -> dict[str, object]:
    """构造供人工 status 置顶显示的显著状态。"""
    return {
        "timestamp": observed_at.isoformat(),
        "status": status,
        "message": message,
        "consecutive_failures": consecutive_failures,
    }


def _schedule_payload(schedule: SwapTradingSchedule | None) -> dict[str, object]:
    """把时段判断转换为心跳可读字段。"""
    if schedule is None:
        return {
            "is_tradable": False,
            "metadata_is_fresh": False,
            "time_until_close_seconds": None,
            "closure_duration_seconds": None,
            "reason": "时段元数据读取失败",
        }
    return {
        "is_tradable": schedule.is_tradable,
        "metadata_is_fresh": schedule.metadata_is_fresh,
        "time_until_close_seconds": (
            schedule.time_until_close.total_seconds()
            if schedule.time_until_close is not None
            else None
        ),
        "closure_duration_seconds": (
            schedule.closure_duration.total_seconds()
            if schedule.closure_duration is not None
            else None
        ),
        "next_close_at": (
            schedule.next_close_at.isoformat()
            if schedule.next_close_at is not None
            else None
        ),
        "next_open_at": (
            schedule.next_open_at.isoformat()
            if schedule.next_open_at is not None
            else None
        ),
        "reason": schedule.reason,
    }


def _position_notional(
    position: Position | None,
    price: Decimal | None,
) -> Decimal | None:
    """按元数据价格计算绝对名义；任一输入未知则保持未知。"""
    if position is None or price is None:
        return None
    return abs(position.signed_size) * price


def _format_money(value: Decimal | None) -> str | None:
    """在心跳中使用固定两位十进制美元字符串。"""
    return format(value, ".2f") if value is not None else None


def _liquidation_distance(info: object, position: Position) -> Decimal:
    """严格读取 API 权威强平价并计算方向相关距离。"""
    if not isinstance(info, tuple) or len(info) != 2:
        raise ValueError("get_liquidation_info 未返回权威强平价")
    mark = execution._decimal(info[0], label="XAUS mark", positive=True)
    liquidation = execution._decimal(info[1], label="XAUS 强平价", positive=True)
    if position.signed_size > 0:
        return (mark - liquidation) / mark
    if position.signed_size < 0:
        return (liquidation - mark) / mark
    raise ValueError("空仓没有强平距离")


def _imbalance_reason(
    xaus_position: Position,
    xau_position: Position,
    xaus_notional: Decimal | None,
    xau_notional: Decimal | None,
) -> str | None:
    """识别单腿、方向异常及超过阈值的双腿名义差。"""
    if xaus_position.is_flat != xau_position.is_flat:
        remaining = "XAU" if xaus_position.is_flat else "XAUS"
        return f"单腿失衡：只剩 {remaining}"
    if xaus_position.is_flat and xau_position.is_flat:
        return None
    if xaus_position.signed_size <= 0 or xau_position.signed_size >= 0:
        return (
            "持仓方向异常："
            f"XAUS={xaus_position.signed_size} XAU={xau_position.signed_size}"
        )
    if xaus_notional is None or xau_notional is None:
        return None
    larger = max(xaus_notional, xau_notional)
    if larger == 0:
        return None
    ratio = abs(xaus_notional - xau_notional) / larger
    if ratio > IMBALANCE_RATIO:
        return f"双腿名义失衡 {ratio:.2%}，超过阈值 {IMBALANCE_RATIO:.2%}"
    return None


async def _close_leg(
    var: Any,
    position: Position,
    leg: execution.CarryLeg,
    *,
    dry_run: bool,
    audit_path: Path,
    observed_at: datetime,
) -> None:
    """有限重试 reduce_only 平掉一腿；认证类错误绝不静默重试。"""
    if position.is_flat:
        return
    last_error: Exception | None = None
    for attempt in range(1, CLOSE_RETRIES + 1):
        try:
            quote = await execution._close_position_quote(var, leg, position)
            if quote is None:
                return
            action = (
                f"reduce_only {quote.side.value.lower()} "
                f"{leg.underlying} {quote.qty}"
            )
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "close_attempt",
                    "market": leg.underlying,
                    "attempt": attempt,
                    "action": action,
                    "dry_run": dry_run,
                },
            )
            if dry_run:
                print(f"[DRY-RUN] 将执行：{action}")
                return
            print(f">>> 守护进程执行：{action}")
            result = await execution._accept_quote(var, quote, reduce_only=True)
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "close_succeeded",
                    "market": leg.underlying,
                    "attempt": attempt,
                    "result": execution._format_result(result),
                },
            )
            return
        except (VariationalJurisdictionError, VariationalAuthError):
            raise
        except Exception as exc:  # noqa: BLE001 平仓必须覆盖报价和 accept 的失败
            last_error = exc
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "close_failed",
                    "market": leg.underlying,
                    "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            if attempt < CLOSE_RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
    raise CloseActionError(
        f"{leg.underlying} 平仓连续失败 {CLOSE_RETRIES} 次：{last_error}"
    ) from last_error


async def _flatten(
    var: Any,
    *,
    xaus_position: Position,
    xau_position: Position,
    schedule: SwapTradingSchedule | None,
    xaus_known_closed: bool,
    dry_run: bool,
    audit_path: Path,
    observed_at: datetime,
) -> FlattenResult:
    """按时段能力清空仓位；XAUS 明确休市时保留显著待处理状态。"""
    if xaus_position.is_flat and xau_position.is_flat:
        return FlattenResult(True, False, "当前已空仓")

    if xaus_known_closed:
        if not xau_position.is_flat:
            await _close_leg(
                var,
                xau_position,
                execution.XAU_LEG,
                dry_run=dry_run,
                audit_path=audit_path,
                observed_at=observed_at,
            )
        if not xaus_position.is_flat:
            message = "XAUS 当前休市无法平仓，已记录待处理状态"
            print(f"🚨 {message}")
            _append_audit(
                audit_path,
                {
                    "timestamp": observed_at,
                    "event": "pending_xaus_close",
                    "message": message,
                    "dry_run": dry_run,
                },
            )
            return FlattenResult(False, True, message)
        return FlattenResult(True, False, "XAU 已平仓")

    if not xaus_position.is_flat:
        await _close_leg(
            var,
            xaus_position,
            execution.XAUS_LEG,
            dry_run=dry_run,
            audit_path=audit_path,
            observed_at=observed_at,
        )
    if not xau_position.is_flat:
        await _close_leg(
            var,
            xau_position,
            execution.XAU_LEG,
            dry_run=dry_run,
            audit_path=audit_path,
            observed_at=observed_at,
        )
    return FlattenResult(True, False, "平仓动作已完成" if not dry_run else "已列出平仓动作")


async def run_once(
    var: Any,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    kill_switch_path: Path = DEFAULT_KILL_SWITCH,
    heartbeat_path: Path = DEFAULT_HEARTBEAT,
    state_path: Path = DEFAULT_STATE,
    audit_path: Path = DEFAULT_AUDIT_LOG,
) -> int:
    """按固定优先级执行一轮风控，并无条件尝试写入心跳。"""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("now 必须包含时区")
    observed_at = observed_at.astimezone(timezone.utc)
    previous_failures = _read_failure_count(state_path)
    consecutive_failures = previous_failures
    conclusion = "本轮尚未完成"
    result_code = 1
    xaus_position: Position | None = None
    xau_position: Position | None = None
    xaus_price: Decimal | None = None
    xau_price: Decimal | None = None
    schedule: SwapTradingSchedule | None = None
    schedule_error: str | None = None
    market_status: str | None = None

    _append_audit(
        audit_path,
        {
            "timestamp": observed_at,
            "event": "round_started",
            "dry_run": dry_run,
            "kill_switch": kill_switch_path.exists(),
        },
    )
    try:
        xaus_position = await execution._get_position(var, execution.XAUS_LEG)
        xau_position = await execution._get_position(var, execution.XAU_LEG)

        try:
            metadata, record, schedule = await execution._load_schedule(
                var, now=observed_at
            )
            raw_market_status = record.get("market_status")
            if not isinstance(raw_market_status, str) or not raw_market_status.strip():
                schedule_error = "XAUS market_status 元数据缺失"
            else:
                market_status = raw_market_status.strip().lower()
            xaus_price = execution._metadata_price(metadata, execution.XAUS_LEG)
            xau_price = execution._metadata_price(metadata, execution.XAU_LEG)
        except Exception as exc:  # noqa: BLE001 时段读取失败后仍要尝试降险
            schedule_error = f"{type(exc).__name__}: {exc}"

        xaus_notional = _position_notional(xaus_position, xaus_price)
        xau_notional = _position_notional(xau_position, xau_price)

        reason: str | None = None
        state_status = "healthy"

        # 优先级 1：kill switch 无条件高于所有其他判断。
        if kill_switch_path.exists():
            reason = "kill switch 已激活"
            state_status = "kill_switch_active"
        else:
            # 优先级 2：单腿、方向或名义失衡。
            reason = _imbalance_reason(
                xaus_position,
                xau_position,
                xaus_notional,
                xau_notional,
            )

        # 优先级 3：权威强平价缺失也视为不安全。
        if reason is None and not xaus_position.is_flat:
            try:
                liquidation_info = await var.get_liquidation_info(
                    execution.XAUS_LEG.underlying,
                    exact=True,
                )
                distance = _liquidation_distance(liquidation_info, xaus_position)
                if distance < LIQUIDATION_ALERT_RATIO:
                    reason = (
                        f"XAUS 强平距离 {distance:.2%} 低于阈值 "
                        f"{LIQUIDATION_ALERT_RATIO:.2%}"
                    )
            except Exception as exc:  # noqa: BLE001 读不到权威值必须平仓
                reason = f"XAUS 强平价不可用，按不安全处理：{exc}"

        if reason is None and not (
            xaus_position.is_flat and xau_position.is_flat
        ) and (xaus_notional is None or xau_notional is None):
            reason = "无法确认双腿名义，不能验证失衡阈值"

        # 优先级 4：交易时段必须新鲜、完整；每日短休市明确穿越。
        if reason is None and not (
            xaus_position.is_flat and xau_position.is_flat
        ):
            if schedule_error is not None:
                reason = f"XAUS 时段元数据不可用：{schedule_error}"
            elif schedule is None or not schedule.metadata_is_fresh:
                detail = schedule.reason if schedule is not None else "无解析结果"
                reason = f"XAUS 时段元数据不安全：{detail}"
            elif market_status == "open" and not schedule.is_tradable:
                reason = "XAUS market_status 与 trading_sessions 状态矛盾"
            elif schedule.closure_duration is None:
                reason = "XAUS 时段元数据缺少完整休市长度"
            elif schedule.closure_duration > LONG_CLOSURE_THRESHOLD:
                if not schedule.is_tradable:
                    reason = "XAUS 已进入长休市且仍有持仓"
                elif schedule.time_until_close is None:
                    reason = "XAUS 长休市前缺少剩余时间"
                elif schedule.time_until_close <= timedelta(
                    minutes=PRE_CLOSE_MINUTES
                ):
                    reason = (
                        f"XAUS 长休市 {schedule.closure_duration} 将在 "
                        f"{schedule.time_until_close} 后开始"
                    )

        if reason is None:
            conclusion = "无需动作：仓位与风控检查正常"
            consecutive_failures = 0
            _write_json(
                state_path,
                _state_payload(
                    observed_at=observed_at,
                    status="healthy",
                    message=conclusion,
                    consecutive_failures=0,
                ),
            )
            result_code = 0
        else:
            print(f"守护进程命中风控：{reason}")
            flatten_result = await _flatten(
                var,
                xaus_position=xaus_position,
                xau_position=xau_position,
                schedule=schedule,
                xaus_known_closed=(
                    schedule is not None
                    and schedule.metadata_is_fresh
                    and market_status is not None
                    and market_status != "open"
                ),
                dry_run=dry_run,
                audit_path=audit_path,
                observed_at=observed_at,
            )
            conclusion = f"{reason}；{flatten_result.message}"
            if flatten_result.complete:
                if not dry_run:
                    xaus_size, xau_size, net_delta = await execution._await_flat(var)
                    xaus_position = Position(
                        execution.XAUS_LEG.underlying, xaus_size
                    )
                    xau_position = Position(execution.XAU_LEG.underlying, xau_size)
                    if xaus_size != 0 or xau_size != 0:
                        raise CloseActionError(
                            "平仓 accept 已返回，但轮询后仓位仍未归零："
                            f"XAUS={xaus_size} XAU={xau_size} 净 delta={net_delta}"
                        )
                    _append_audit(
                        audit_path,
                        {
                            "timestamp": observed_at,
                            "event": "flat_confirmed",
                            "xaus_size": xaus_size,
                            "xau_size": xau_size,
                            "net_delta": net_delta,
                        },
                    )
                consecutive_failures = 0
                completed_status = (
                    "dry_run"
                    if dry_run
                    else state_status if state_status != "healthy" else "flattened"
                )
                _write_json(
                    state_path,
                    _state_payload(
                        observed_at=observed_at,
                        status=completed_status,
                        message=conclusion,
                        consecutive_failures=0,
                    ),
                )
                result_code = 0
            else:
                consecutive_failures = previous_failures + 1
                _write_json(
                    state_path,
                    _state_payload(
                        observed_at=observed_at,
                        status="pending_xaus_close",
                        message=conclusion,
                        consecutive_failures=consecutive_failures,
                    ),
                )
                result_code = 1
    except (VariationalJurisdictionError, VariationalAuthError) as exc:
        consecutive_failures = previous_failures + 1
        category = (
            "地区封锁"
            if isinstance(exc, VariationalJurisdictionError)
            else "会话失效"
        )
        conclusion = f"{category}导致守护进程无法平仓：{exc}"
        _write_json(
            state_path,
            _state_payload(
                observed_at=observed_at,
                status="action_failed",
                message=conclusion,
                consecutive_failures=consecutive_failures,
            ),
        )
        notify("Swap carry 无法自动平仓", conclusion)
        print(f"🚨 {conclusion}")
        result_code = 1
    except Exception as exc:  # noqa: BLE001 任意未知错误都不得静默
        consecutive_failures = previous_failures + 1
        conclusion = f"守护轮次失败，无法确认已安全平仓：{type(exc).__name__}: {exc}"
        _write_json(
            state_path,
            _state_payload(
                observed_at=observed_at,
                status="action_failed",
                message=conclusion,
                consecutive_failures=consecutive_failures,
            ),
        )
        notify("Swap carry 守护进程失败", conclusion)
        print(f"🚨 {conclusion}")
        result_code = 1
    finally:
        # 成交或部分失败后重新读仓，令心跳反映本轮结束时的真实快照。
        try:
            xaus_position = await execution._get_position(var, execution.XAUS_LEG)
            xau_position = await execution._get_position(var, execution.XAU_LEG)
        except Exception as exc:  # noqa: BLE001 心跳仍需保存其他已知字段
            conclusion = f"{conclusion}；结束读仓失败：{type(exc).__name__}: {exc}"

        xaus_notional = _position_notional(xaus_position, xaus_price)
        xau_notional = _position_notional(xau_position, xau_price)
        net_delta = (
            xaus_position.signed_size + xau_position.signed_size
            if xaus_position is not None and xau_position is not None
            else None
        )
        heartbeat = {
            "timestamp": observed_at.isoformat(),
            "conclusion": conclusion,
            "xaus_notional": _format_money(xaus_notional),
            "xau_notional": _format_money(xau_notional),
            "net_delta": str(net_delta) if net_delta is not None else None,
            "xaus_schedule": _schedule_payload(schedule),
            "consecutive_failures": consecutive_failures,
            "dry_run": dry_run,
        }
        _write_json(heartbeat_path, heartbeat)
        _append_audit(
            audit_path,
            {
                "timestamp": observed_at,
                "event": "round_finished",
                "result_code": result_code,
                **heartbeat,
            },
        )
    return result_code


async def _main(args: argparse.Namespace) -> int:
    """构造真实客户端，执行一轮并始终释放 HTTP 会话。"""
    var = await execution._load()
    try:
        return await run_once(
            var,
            dry_run=args.dry_run,
            kill_switch_path=args.kill_switch,
            heartbeat_path=args.heartbeat,
            state_path=args.state,
            audit_path=args.audit_log,
        )
    finally:
        await var.close()


def build_parser() -> argparse.ArgumentParser:
    """构造只支持单轮降险的命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Variational swap carry 无人值守风控守护进程（只平仓）"
    )
    parser.add_argument("--once", action="store_true", help="执行一轮后退出")
    parser.add_argument("--dry-run", action="store_true", help="判定并询价，但不 accept")
    parser.add_argument("--kill-switch", type=Path, default=DEFAULT_KILL_SWITCH)
    parser.add_argument("--heartbeat", type=Path, default=DEFAULT_HEARTBEAT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """清理代理变量后运行单轮守护；永不进入自动开仓循环。"""
    args = build_parser().parse_args(argv)
    removed = execution._load_environment_without_proxy()
    if removed:
        print(f"已清除代理环境变量：{'、'.join(removed)}")
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
