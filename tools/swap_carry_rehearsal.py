"""结构切换的只读事前检查；只暴露 indicative 询价能力。"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median

from adapters.base import Side
from engine.swap_trading_schedule import parse_sessions, parse_trading_schedule
from tools import hedge_swap_carry as execution
from tools.show_switch_history import load_switch_history


class IndicativeOnly:
    """报价准备器仅获得询价能力，无法访问 accept 或其他写接口。"""

    def __init__(self, client):
        self.__client = client

    async def request_quote(self, *args, **kwargs):
        """高层 request_quote 固定调用 /quotes/indicative。"""
        return await self.__client.request_quote(*args, **kwargs)


def rehearsal_window(source, schedule, metadata, now, switch_lead, rehearsal_lead):
    """用会话绝对边界生成稳定窗口键，包含迟到启动时的补预演。"""
    if schedule is None or not schedule.metadata_is_fresh:
        return None
    if source.has_xaus:
        if (schedule.closure_duration is None
                or schedule.closure_duration <= timedelta(hours=4)
                or schedule.next_close_at is None):
            return None
        planned = schedule.next_close_at - switch_lead
        target = execution.XAU_XAUT
        reason = "XAUS 长休市前反向切换"
    else:
        target = execution.XAUS_XAU
        reason = "XAUS 恢复开市切换"
        if (schedule.is_tradable and schedule.closure_duration is not None
                and schedule.closure_duration > timedelta(hours=4)
                and schedule.time_until_close is not None
                and schedule.time_until_close <= switch_lead):
            # 已进入反向切换窗口，下一次开市应取未来边界，不能回到本周开市。
            planned = schedule.next_open_at
        elif schedule.is_tradable:
            record = execution._instrument_record(metadata, execution.XAUS_LEG)
            sessions = parse_sessions(record.get("trading_sessions"))
            planned = next(s.open_at for s in sessions if s.open_at <= now < s.close_at)
        elif (schedule.closure_duration is not None
              and schedule.closure_duration > timedelta(hours=4)):
            planned = schedule.next_open_at
        else:
            return None
    if planned is None or now < planned - rehearsal_lead:
        return None
    return {
        "window_id": f"{target.name}:{planned.isoformat()}",
        "planned_at": planned.isoformat(),
        "direction": {"from": source.name, "to": target.name},
        "trigger": reason,
    }


def duration_estimate(path: Path):
    """仅用真正完成的切换耗时计算中位数，排除预演和 dry-run。"""
    records, error = load_switch_history(path)
    values = []
    for record in records:
        if (record.get("kind") == "rehearsal" or record.get("dry_run")
                or record.get("status") != "completed"):
            continue
        try:
            value = float(record["total_duration_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            values.append(value)
    return (median(values), "历史真实切换耗时中位数") if values else (
        None, f"无估计：{error or '无有效历史切换'}")


def quote_detail(quote):
    """按真实双边报价计算相对中点的方向滑点。"""
    bid = execution._decimal(quote.payload.get("bid"), label="报价 bid", positive=True)
    ask = execution._decimal(quote.payload.get("ask"), label="报价 ask", positive=True)
    if ask < bid:
        raise ValueError("报价 ask 小于 bid")
    mid = (bid + ask) / 2
    direction = Decimal(1) if quote.side is Side.BUY else Decimal(-1)
    return {
        "market": quote.leg.underlying,
        "side": quote.side.value.lower(),
        "quantity": str(quote.qty),
        "notional_usd": str(quote.notional_usd),
        "execution_price": str(quote.execution_price),
        "quote_mid": str(mid),
        "slippage_bp": str(direction * (quote.execution_price - mid) / mid * 10000),
        "required_margin_usd": str(quote.required_margin_usd),
    }


async def perform_rehearsal(var, *, report, source, metadata, positions_payload,
                            now, notional, history_path, slippage_warning_bp):
    """逐项保留检查证据；单项异常阻断预演，但继续收集其余检查。"""
    from tools import run_swap_carry_guard as guard

    target = execution.resolve_structure(report["direction"]["to"])
    blocked = report["blocking_reasons"]
    warnings = report["warnings"]
    report.update(close_legs=[], open_legs=[], markets={}, funding_rates={})
    report["estimated_duration_ms"], report["duration_basis"] = duration_estimate(history_path)
    readonly = IndicativeOnly(var)

    def failure(label, exc):
        """检查失败不采用成功默认值。"""
        blocked.append(f"{label}：{type(exc).__name__}: {exc}")

    try:
        report["before"] = await guard._switch_snapshot(
            var, positions_payload=positions_payload, metadata=metadata)
    except Exception as exc:
        failure("当前仓位快照失败", exc)

    legs = {leg.underlying: leg for leg in (*source.legs, *target.legs)}
    for name, leg in legs.items():
        try:
            record = execution._instrument_record(metadata, leg)
            # 永续黄金腿没有时段字段，沿用交易所 24/7 合约语义；显式停市必须阻断。
            status = record.get("market_status")
            timed = leg.underlying == "XAUS" or bool(record.get("trading_sessions"))
            schedule = parse_trading_schedule(
                record.get("trading_sessions"), record.get("trading_schedule"), status, now
            ) if timed else None
            tradable = (schedule.is_tradable and schedule.metadata_is_fresh
                        if timed else status is None or str(status).lower() == "open")
            report["markets"][name] = {
                "tradable": tradable, "market_status": status,
                "time_to_close_seconds": schedule.time_until_close.total_seconds()
                if schedule and schedule.time_until_close is not None else None,
                "schedule_basis": schedule.reason if schedule else "24/7 永续合约",
            }
            if not tradable:
                blocked.append(f"{name} 当前不可交易")
        except Exception as exc:
            failure(f"{name} 交易状态失败", exc)
        try:
            rate = await execution._funding_rate_for_leg(var, leg)
            report["funding_rates"][name] = str(rate)
        except Exception as exc:
            failure(f"{name} 费率失败", exc)

    try:
        rates = {name: Decimal(value) for name, value in report["funding_rates"].items()}
        carry = execution._weighted_net_carry(target, rates)
        report["net_carry_annual"] = str(carry)
        if carry < guard.MIN_ENTRY_CARRY_ANNUAL:
            warnings.append(f"新结构净 carry {carry:.4%} 低于阈值 {guard.MIN_ENTRY_CARRY_ANNUAL:.4%}")
    except Exception as exc:
        failure("净 carry 计算失败", exc)

    try:
        positions = guard._carry_positions_from_payload(positions_payload)
        for leg in source.legs:
            try:
                quote = await execution._close_position_quote(readonly, leg, positions[leg.underlying])
                if quote is not None:
                    report["close_legs"].append(quote_detail(quote))
            except Exception as exc:
                failure(f"{leg.underlying} 平仓报价失败", exc)
    except Exception as exc:
        failure("平仓数量解析失败", exc)

    try:
        execution._validate_notional(notional)
        quotes, _ = await execution._prepare_open_quotes(
            readonly, notional, target, require_margin=False)
        quantities = guard._account_quantities(positions_payload)
        required = Decimal(0)
        for quote in quotes:
            name = quote.leg.underlying
            delta_key = "ask_margin_delta" if quote.side is Side.BUY else "bid_margin_delta"
            # 记录真实增量，但有旧仓时只按报价内权威参数估算全平后的独立新仓。
            delta = execution._decimal(
                quote.payload["margin_requirements"][delta_key]["initial_margin"],
                label=f"{name} {delta_key}.initial_margin")
            params = quote.payload.get("margin_params", {}).get("params", {})
            asset = params.get("asset_params", {}).get(name, {})
            if not asset and params.get("use_default_asset_param") is True:
                asset = params.get("default_asset_param", {})
            if asset.get("futures_initial_margin") is not None:
                ratio = execution._decimal(asset["futures_initial_margin"],
                                           label=f"{name} futures_initial_margin", positive=True)
                if ratio > 1:
                    raise ValueError(f"{name} 初始保证金率超过 1")
                basis = "报价参数中的初始保证金率 × 新仓名义（旧仓先全平）"
            elif quantities.get(name, Decimal(0)) == 0:
                ratio = execution._initial_margin_ratio(quote.payload, quote.side, quote.notional_usd)
                basis = "当前该腿为空，采用原始报价初始保证金增量"
            else:
                raise ValueError(f"{name} 存在旧仓但报价缺少专属 futures_initial_margin，无法估算全平后保证金")
            prepared = replace(quote, initial_margin_ratio=ratio)
            detail = quote_detail(prepared)
            detail.update(quoted_margin_delta_usd=str(delta), margin_basis=basis)
            report["open_legs"].append(detail)
            required += prepared.required_margin_usd
        available = await guard._available_account_margin(var)
        report["margin"] = {
            "available_usd": str(available), "required_usd": str(required),
            "sufficient": available >= required,
            "basis": "当前权益减全部持仓初始保证金，不预支平仓释放资金",
        }
        if available < required:
            blocked.append(f"可用保证金不足：可用 {available}，所需 {required}")
    except (Exception, SystemExit) as exc:
        failure("开仓报价或保证金检查失败", exc)

    for leg in report["close_legs"] + report["open_legs"]:
        if Decimal(leg["slippage_bp"]) > slippage_warning_bp:
            warnings.append(f"{leg['market']} {leg['side']} 滑点 {leg['slippage_bp']} bp 偏大")
    report["conclusion"] = "blocked" if blocked else "warning" if warnings else "ready"
    return report
