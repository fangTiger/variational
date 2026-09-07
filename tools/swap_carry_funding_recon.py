"""XAUS 已结算流水与结算前采样对账；不调用任何写接口。"""
from __future__ import annotations

import fcntl
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from tools.verify_funding_units import fetch_all_transfers


def _time(value):
    """只接受带时区的时间，统一按 UTC 判定周末。"""
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('时间缺少时区')
    return parsed.astimezone(timezone.utc)


def _number(value):
    """拒绝 NaN、Infinity 和缺失金额。"""
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('金额或费率不是有限数')
    return number


def read_history(path):
    """只读 JSONL；损坏记录不能当成空历史从而重复落盘。"""
    if not Path(path).exists():
        return []
    with Path(path).open(encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _prediction(samples, created):
    """匹配结算前读取且指向本次计提的最近预测，拒绝事后值与跨期值。"""
    candidates = []
    for sample in samples:
        try:
            rate = sample['xaus']['funding']['long_rate']
            observed = _time(rate['observed_at'])
            sampled = _time(sample['observed_at'])
            apply = _time(rate['apply_time'])
            annual = _number(rate['normalized_annual_rate'])
            if (observed <= created and sampled <= created and observed < apply and sampled < apply
                    and abs(created - apply) <= timedelta(minutes=15)):
                candidates.append((observed, sampled, apply, annual, sample))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return max(candidates, key=lambda entry: entry[:2]) if candidates else None


def _record(transfer, samples, previous):
    """按独立价格证据反算实际费率，保留原始流水便于复核。"""
    created = _time(transfer['created_at'])
    if created is None:
        raise ValueError('结算流水缺少 created_at')
    qty = _number(transfer['qty'])
    position = _number(transfer['ref_instrument_position_qty'])
    prediction = _prediction(samples, created)
    apply = _time(transfer.get('apply_time'))
    source = '结算流水' if apply else None
    if apply is None and prediction:
        apply, source = prediction[2], '采样器预测计提时间'
    row = dict(created_at=transfer['created_at'], apply_time=apply.isoformat() if apply else None,
               apply_time_source=source, qty=str(qty), position_qty=str(position),
               notional_usdc=None, notional_source=None, actual_rate=None,
               predicted_annual_rate=None, predicted_daily_rate=None, prediction_observed_at=None,
               ratio=None, deviation_bp=None, coverage_days=None, covered_dates=[],
               multi_day=False, interval_days=None, level='warning', conclusion='无预测可比',
               raw_transfer=transfer)
    if previous and apply:
        prior = _time(previous.get('apply_time'))
        if prior:
            row['interval_days'] = str(Decimal(str((apply-prior).total_seconds())) / Decimal(86400))
    price = None
    # 真实已抓取流水不含价格；有独立价格时优先使用，否则保留估计来源。
    if transfer.get('ref_instrument_price') is not None:
        price = _number(transfer['ref_instrument_price'])
        row['notional_source'] = '结算仓位数量 × 流水历史价格'
    elif prediction:
        try:
            sampled = prediction[1]
            if created - sampled <= timedelta(hours=2):
                mark = prediction[4]['xaus'].get('mark_price')
                if mark is not None:
                    price = _number(mark)
                    row['notional_source'] = '结算仓位数量 × 结算前元数据标记价（估计）'
                else:
                    quote = prediction[4]['rfq']['xaus']
                    bid, ask = _number(quote['bid']), _number(quote['ask'])
                    if 0 < bid <= ask:
                        price = (bid + ask) / 2
                        row['notional_source'] = '结算仓位数量 × 结算前 RFQ 中价（估计）'
                row['price_observed_at'] = sampled.isoformat()
        except (KeyError, TypeError, ValueError, ArithmeticError):
            pass
    if price is not None and price > 0 and position != 0:
        notional = abs(position) * price
        row.update(notional_usdc=str(notional), actual_rate=str(abs(qty) / notional))
    if not prediction:
        return row
    annual = prediction[3]
    daily = abs(annual) / 365
    row.update(predicted_annual_rate=str(annual), predicted_daily_rate=str(daily),
               prediction_observed_at=prediction[0].isoformat(),
               predicted_long_rate=prediction[4]['xaus']['funding']['long_rate'])
    if position < 0:
        row['conclusion'] = '空头持仓不能使用 long_rate 预测对比'
        return row
    if row['actual_rate'] is None:
        row['conclusion'] = '缺少独立结算名义，无法反算实际费率'
        return row
    if daily == 0:
        row['conclusion'] = '预测为零，无法推断覆盖天数；预测口径可能有问题'
        return row
    actual = Decimal(row['actual_rate'])
    ratio = actual / daily
    days = int(ratio.to_integral_value(rounding=ROUND_HALF_UP))
    row.update(ratio=str(ratio), deviation_bp=str((actual-daily)*10000),
               coverage_days=days, multi_day=days != 1)
    return row


async def reconcile(var, *, samples_path: Path, output_path: Path,
                    deviation_threshold: Decimal = Decimal('.20')):
    """每轮分页读取流水；文件锁下去重与追加，重启不重复，异常由守护层隔离。"""
    threshold = _number(deviation_threshold)
    if not 0 <= threshold < 1:
        raise ValueError('偏差阈值须在 [0,1) 内')
    async def fetch(limit, offset):
        return await var.raw(f'/transfers?limit={limit}&offset={offset}')
    transfers = await fetch_all_transfers(fetch)
    selected = [row for row in transfers
                if isinstance(row.get('reference_instrument'), dict)
                and row['reference_instrument'].get('underlying') == 'XAUS'
                and row['reference_instrument'].get('instrument_type') == 'swap'
                and row.get('asset') == 'USDC' and row.get('funding_rate') not in (None, '')]
    selected.sort(key=lambda row: _time(row['created_at']))
    samples = read_history(samples_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.with_suffix(output_path.suffix+'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        history = read_history(output_path)
        seen = {row['transfer_key'] for row in history}
        new = []
        for transfer in selected:
            key = hashlib.sha256(json.dumps(transfer, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if key in seen:
                continue
            earlier = [r for r in history if _time(r['created_at']) < _time(transfer['created_at'])]
            previous = max(earlier, key=lambda r: _time(r['created_at'])) if earlier else None
            row = _record(transfer, samples, previous)
            row['transfer_key'] = key
            if row['coverage_days'] is not None:
                days = row['coverage_days']
                ratio = Decimal(row['ratio'])
                apply = _time(row['apply_time'])
                # 单日偏差保留原值；识别三日模式时另检查距三倍预测的残差。
                expected = 3 if apply and days == 3 and apply.weekday() in (0,4) else 1
                residual = abs(ratio / expected - 1)
                row['adjusted_deviation_ratio'] = str(residual)
                row['deviation_threshold'] = str(threshold)
                if residual > threshold or Decimal(row['qty']) * Decimal(row['predicted_annual_rate']) < 0:
                    row['conclusion'] = '实际与预测偏差超阈值或收付方向异常，预测口径可能有问题'
                elif days == 1:
                    row.update(level='info', conclusion='日常计提正常', covered_dates=[apply.date().isoformat()])
                elif days == 3 and apply.weekday() == 4:
                    row.update(level='info', conclusion='周五预收模式',
                               covered_dates=[(apply+timedelta(days=i)).date().isoformat() for i in range(3)])
                elif days == 3 and apply.weekday() == 0 and row['interval_days'] is not None and Decimal(row['interval_days']) >= 3:
                    row.update(level='info', conclusion='周一追补模式',
                               covered_dates=[(apply-timedelta(days=i)).date().isoformat() for i in (2,1,0)])
                else:
                    row['conclusion'] = '多日计提，覆盖日期及方向待确认'
            with output_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False)+'\n')
            history.append(row)
            seen.add(key)
            new.append(row)
        latest = max(history, key=lambda r: _time(r['created_at'])) if history else None
        return {'new_count':len(new), 'records':new, 'latest':latest,
                'conclusion':latest['conclusion'] if latest else '尚无 XAUS 资金费结算'}
