"""Lighter RH 人工持仓的 Extended 自动对冲入口。

默认 dry_run，只读取 Lighter primary 腿并打印 Extended 对冲意图；只有显式传入
``--live`` 才允许对冲腿下单。Lighter 腿始终只读，不持有任何签名密钥。
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

from adapters.extended_client import ExtendedClient  # noqa: E402
from adapters.lighter_client import LighterClient  # noqa: E402
from engine.hedge_engine import HedgeConfig, HedgeEngine, HedgeState  # noqa: E402
from infra.logger import get_logger  # noqa: E402

logger = get_logger("lighter_hedge")

_ROOT = Path(__file__).resolve().parent.parent
_HEDGE_SNAPSHOT = _ROOT / "data" / "lighter_hedge.jsonl"


class StartupError(RuntimeError):
    """启动自检失败，进程必须在接触交易账户前退出。"""


def _load_environment() -> None:
    """加载本地环境变量；未安装 python-dotenv 时沿用进程环境。"""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="Lighter RH 积分对冲守护进程")
    parser.add_argument("--live", action="store_true", help="真实下单（默认 dry_run）")
    parser.add_argument(
        "--account",
        default="X10_HEDGE",
        help="Extended 账户环境变量前缀（默认 X10_HEDGE）",
    )
    parser.add_argument(
        "--lighter-address",
        default=os.getenv("LIGHTER_RH_L1_ADDRESS"),
        help="Robinhood Wallet 地址（默认读取 LIGHTER_RH_L1_ADDRESS）",
    )
    parser.add_argument("--market", default="BTC", help="Lighter 标的（默认 BTC）")
    parser.add_argument(
        "--hedge-market",
        default="BTC-USD",
        help="Extended 对冲标的（默认 BTC-USD）",
    )
    parser.add_argument(
        "--max-primary-notional",
        type=Decimal,
        default=Decimal("2000"),
        help="primary 名义上限 USD（默认 2000）",
    )
    parser.add_argument("--interval", type=float, default=30.0, help="轮询秒数（默认 30）")
    parser.add_argument(
        "--maker-first-timeout",
        type=float,
        default=0.0,
        help="对冲先挂 maker 单等待成交的秒数，超时转吃单；0=直接吃单（默认）",
    )
    parser.add_argument(
        "--rebalance-threshold",
        type=Decimal,
        default=Decimal("0.02"),
        help="净 delta 再平衡比例阈值（默认 0.02）",
    )
    parser.add_argument(
        "--min-hedge-free-margin-ratio",
        type=Decimal,
        default=Decimal("0.20"),
        help="Extended 可用保证金率告警阈值（默认 0.20）",
    )
    return parser


def _append_snapshot(payload: dict, path: Path | None = None) -> None:
    """向对冲历史追加一条 JSON 快照。"""
    target = _HEDGE_SNAPSHOT if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _build_snapshot_callback(
    primary,
    hedge,
    *,
    interval: float,
    rebalance_threshold_ratio: Decimal,
    min_hedge_free_margin_ratio: Decimal,
    max_primary_notional: Decimal | None,
):
    """构造每轮快照回调；保证金查询失败也必须保留心跳。"""

    async def snapshot(state: HedgeState) -> None:
        margin_ratio = None
        margin_error = None
        try:
            margin_ratio = await hedge.get_free_margin_ratio()
        except Exception as exc:  # noqa: BLE001  保证金失败不能吞掉心跳
            margin_error = str(exc)
            logger.warning("读取 Extended 可用保证金率失败：%s", exc)

        # 权益仅供面板汇总用，取不到就留空——绝不能因此中断交易循环
        primary_collateral = None
        hedge_equity = None
        try:
            balance = await hedge.get_balance()
            hedge_equity = str(balance.equity)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 Extended 权益失败（不影响对冲）：%s", exc)
        try:
            primary_collateral = str(await primary.get_collateral())
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 Lighter 抵押品失败（不影响对冲）：%s", exc)

        primary_ok = state.primary is not None
        hedge_ok = state.hedge is not None

        # 名义金额只复用本轮 Lighter 持仓原始响应，不额外请求接口。
        primary_notional = None
        try:
            if primary_ok:
                raw = state.primary.raw
                raw_notional = (
                    raw.get("position_value")
                    if isinstance(raw, dict)
                    else getattr(raw, "position_value", None)
                )
                if raw_notional is not None:
                    primary_notional = str(raw_notional)
        except Exception as exc:  # noqa: BLE001 快照字段失败不能中断交易循环
            logger.warning("读取 Lighter 名义金额失败（不影响对冲）：%s", exc)

        # 两所原始字段拼写不同，分别照抄并独立容错。
        primary_unrealized = None
        try:
            if primary_ok:
                raw_unrealized = state.primary.raw.get("unrealized_pnl")
                if raw_unrealized is not None:
                    primary_unrealized = str(raw_unrealized)
        except Exception as exc:  # noqa: BLE001 快照字段失败不能中断交易循环
            logger.warning("读取 Lighter 浮盈失败（不影响对冲）：%s", exc)

        hedge_notional = None
        try:
            if hedge_ok:
                raw_hedge_notional = getattr(state.hedge.raw, "value", None)
                if raw_hedge_notional is not None:
                    hedge_notional = str(raw_hedge_notional)
        except Exception as exc:  # noqa: BLE001 快照字段失败不能中断交易循环
            logger.warning("读取 Extended 名义金额失败（不影响对冲）：%s", exc)

        hedge_unrealized = None
        try:
            if hedge_ok:
                raw_unrealized = getattr(
                    state.hedge.raw,
                    "unrealised_pnl",
                    None,
                )
                if raw_unrealized is not None:
                    hedge_unrealized = str(raw_unrealized)
        except Exception as exc:  # noqa: BLE001 快照字段失败不能中断交易循环
            logger.warning("读取 Extended 浮盈失败（不影响对冲）：%s", exc)

        payload = {
            "ts": time.time(),
            "interval": interval,
            "primary_size": (
                str(state.primary.signed_size) if primary_ok else None
            ),
            "hedge_size": str(state.hedge.signed_size) if hedge_ok else None,
            "net_delta": str(state.net_delta) if primary_ok and hedge_ok else None,
            "action": state.action_taken,
            "primary_read_ok": primary_ok,
            "hedge_read_ok": hedge_ok,
            "primary_notional_exceeded": state.primary_notional_exceeded,
            "rebalance_threshold_ratio": str(rebalance_threshold_ratio),
            "hedge_free_margin_ratio": (
                str(Decimal(str(margin_ratio)))
                if margin_ratio is not None
                else None
            ),
            "min_hedge_free_margin_ratio": str(min_hedge_free_margin_ratio),
            "hedge_margin_error": margin_error,
            "primary_collateral": primary_collateral,
            "hedge_equity": hedge_equity,
            "primary_notional": primary_notional,
            "max_primary_notional": (
                str(max_primary_notional)
                if max_primary_notional is not None
                else None
            ),
            "hedge_notional": hedge_notional,
            "primary_unrealized": primary_unrealized,
            "hedge_unrealized": hedge_unrealized,
        }
        _append_snapshot(payload)

    return snapshot


def _env_value(name: str) -> str | None:
    """读取并清理非空环境变量。"""
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _validate_startup(args: argparse.Namespace) -> tuple[str, str]:
    """校验地址与账户隔离，返回规范化账户前缀和 Lighter 地址。"""
    account_prefix = str(args.account).strip().upper()
    if account_prefix == "X10_GRID":
        raise StartupError("拒绝启动：X10_GRID 是实盘网格账户，本进程绝不能使用")
    if not account_prefix:
        raise StartupError("拒绝启动：Extended 账户前缀不能为空")

    lighter_address = str(args.lighter_address or "").strip()
    if not lighter_address:
        raise StartupError(
            "拒绝启动：请传 --lighter-address 或设置 LIGHTER_RH_L1_ADDRESS"
        )

    hedge_vault = _env_value("X10_HEDGE_VAULT_ID")
    grid_vault = _env_value("X10_GRID_VAULT_ID")
    if hedge_vault is not None and grid_vault is not None and hedge_vault == grid_vault:
        raise StartupError(
            "拒绝启动：X10_HEDGE_VAULT_ID 与 X10_GRID_VAULT_ID 相同"
        )

    selected_vault = _env_value(f"{account_prefix}_VAULT_ID")
    if (
        selected_vault is not None
        and grid_vault is not None
        and selected_vault == grid_vault
    ):
        raise StartupError(
            f"拒绝启动：{account_prefix}_VAULT_ID 与 X10_GRID_VAULT_ID 相同"
        )
    return account_prefix, lighter_address


def _startup_summary(
    *,
    account_prefix: str,
    lighter_address: str,
    account_index: int,
    args: argparse.Namespace,
) -> str:
    """生成同时用于控制台与文件日志的启动摘要。"""
    return "\n".join(
        [
            f"Lighter 地址：{lighter_address}",
            f"Lighter account_index：{account_index}",
            f"Extended 账户前缀：{account_prefix}",
            f"标的：{args.market} → {args.hedge_market}",
            f"名义上限：{args.max_primary_notional} USD",
            f"保证金率告警阈值：{args.min_hedge_free_margin_ratio:.0%}",
            f"maker 优先等待：{args.maker_first_timeout:g} 秒（0=关闭）",
            f"dry_run：{not args.live}",
        ]
    )


async def _main(args: argparse.Namespace) -> None:
    """完成启动自检、装配并运行对冲引擎。"""
    account_prefix, lighter_address = _validate_startup(args)
    primary = LighterClient(l1_address=lighter_address)
    hedge = None
    try:
        hedge = ExtendedClient.from_env(prefix=account_prefix)
        config = HedgeConfig(
            market=args.hedge_market,
            primary_market=args.market,
            poll_interval=args.interval,
            rebalance_threshold_ratio=args.rebalance_threshold,
            dry_run=not args.live,
            maker_first_timeout_s=args.maker_first_timeout,
            max_primary_notional=args.max_primary_notional,
            auth_error_types=(),
        )
        engine = HedgeEngine(
            primary,
            hedge,
            config,
            on_snapshot=_build_snapshot_callback(
                primary,
                hedge,
                interval=args.interval,
                rebalance_threshold_ratio=args.rebalance_threshold,
                min_hedge_free_margin_ratio=args.min_hedge_free_margin_ratio,
                max_primary_notional=args.max_primary_notional,
            ),
            on_auth_error=None,
        )

        # 先解析账户索引才能满足启动自检输出；run_forever 随后会统一连接两腿。
        await primary.connect()
        if primary.account_index is None:
            raise StartupError("拒绝启动：Lighter connect() 未返回 account_index")
        summary = _startup_summary(
            account_prefix=account_prefix,
            lighter_address=lighter_address,
            account_index=primary.account_index,
            args=args,
        )
        print(summary)
        logger.info("Lighter RH 积分对冲启动\n%s", summary)
        await engine.run_forever()
    finally:
        clients = [primary]
        if hedge is not None:
            clients.append(hedge)
        results = await asyncio.gather(
            *(client.close() for client in clients),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("关闭客户端失败：%s", result)


def main() -> None:
    """加载环境、解析参数并启动异步守护进程。"""
    _load_environment()
    parser = _build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(_main(args))
    except StartupError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
