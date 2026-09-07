"""Swap carry 逐笔现金及成交成本；十进制记账，读取只读，写入锁内幂等。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from contextvars import ContextVar
from functools import wraps
from decimal import Decimal
from pathlib import Path

from infra.data_paths import data_dir

MARKETS = frozenset({'XAUS', 'XAU', 'XAUT'})
LABELS = {'spread': '滑点', 'fee': '手续费', 'funding': '资金费',
          'switch': '切换（子项合计，不重复计入）', 'allocation': '保证金划转成本',
          'liquidation_penalty': '强平罚金'}
EXTRA_TYPES = {'realized_pnl', 'external_cashflow', 'unclassified', 'referral_reward'}
DEFAULT_PATH = data_dir() / 'swap_carry_cost_ledger.jsonl'
DEFAULT_RECON_PATH = data_dir() / 'swap_carry_cost_reconciliation.json'


def number(value):
    """拒绝缺失、布尔值及非有限金额。"""
    if isinstance(value, bool):
        raise ValueError('金额不能为布尔值')
    try:
        result = Decimal(str(value))
    except ArithmeticError as exc:
        raise ValueError('金额无效') from exc
    if not result.is_finite():
        raise ValueError('金额不是有限数')
    return result


def timestamp(value):
    """输入必须含时区，统一比较 UTC 时刻。"""
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('时间缺少时区')
    return result.astimezone(timezone.utc)


def event(key, ts, kind, market, amount, source, *, side=None, quantity=None,
          notional_usd=None, detail=None, parent_id=None):
    """创建内部标准事件；未知数量和名义保留 null，绝不按费率反推。"""
    row = dict(event_id=str(key), ts=timestamp(ts).isoformat(), event_type=kind,
               market=market, side=side, quantity=None if quantity is None else str(number(quantity)),
               notional_usd=None if notional_usd is None else str(number(notional_usd)),
               amount_usd=str(number(amount)), source=source, detail=detail or {})
    if parent_id:
        row['parent_id'] = parent_id
    validate(row)
    return row


def validate(row):
    """损坏行不能被当成空台账，以免重扫重复落盘。"""
    if not isinstance(row, dict) or not all(k in row for k in (
        'event_id', 'ts', 'event_type', 'market', 'side', 'quantity',
        'notional_usd', 'amount_usd', 'source', 'detail')):
        raise ValueError('成本记录缺少标准字段')
    if row['event_type'] not in LABELS.keys() | EXTRA_TYPES:
        raise ValueError('未知成本类型')
    if not row['event_id'] or not row['source'] or not isinstance(row['detail'], dict):
        raise ValueError('成本记录缺少来源或唯一键')
    if not isinstance(row['market'], str):
        raise ValueError('成本记录标的无效')
    timestamp(row['ts'])
    number(row['amount_usd'])
    for field in ('quantity', 'notional_usd'):
        if row[field] is not None:
            number(row[field])


def load(path=DEFAULT_PATH):
    """只读，缺失与损坏均返回降级原因。"""
    try:
        rows = []
        with Path(path).open(encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    validate(row)
                    rows.append(classify_record(row))
        if len({r['event_id'] for r in rows}) != len(rows):
            raise ValueError('台账存在重复唯一键')
        return rows, None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [], f'成本台账不可用：{exc}'


def classify_record(row):
    """旧台账只按确定的原始流水类型升级，未知类型继续保留。"""
    raw = row['detail'].get('raw_transfer')
    if (row['event_type'] == 'unclassified' and isinstance(raw, dict)
            and raw.get('transfer_type') == 'referral_reward'):
        return dict(row, event_type='referral_reward')
    return row


def _json_value(value):
    """保持守护内存态的 Decimal 与 datetime 精度，不泛化吞掉非法对象。"""
    if isinstance(value, Decimal):
        return str(number(value))
    if isinstance(value, datetime):
        return timestamp(value).isoformat()
    raise TypeError(f"成本明细包含不可序列化类型：{type(value).__name__}")


def append(path, rows):
    """文件锁覆盖读取去重及一次追加；批次先完整验证，坏文件拒绝继续写。"""
    rows = list(rows)
    for row in rows:
        validate(row)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing, error = load(path)
        if error and path.exists():
            raise ValueError(error)
        seen = {r['event_id'] for r in existing}
        fresh = []
        for row in rows:
            if row['event_id'] not in seen:
                fresh.append(row)
                seen.add(row['event_id'])
        if fresh:
            payload = ''.join(json.dumps(r, ensure_ascii=False, default=_json_value) + '\n' for r in fresh)
            with path.open('a', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        return len(fresh)


def fill_events(fill, ts, *, parent_id=None):
    """接受 RFQ 的全量成交口径；成交价是已接受报价价，不冒充成交历史回读。"""
    if fill.get('status') != 'succeeded' or fill.get('market') not in MARKETS:
        return []
    key = fill.get('rfq_id')
    if not key:
        raise ValueError('成交缺少 rfq_id，无法安全去重')
    qty = abs(number(fill['filled_quantity']))
    price, mid = number(fill['execution_price']), number(fill['quote_mid'])
    side = fill['side'].lower()
    if qty <= 0 or min(price, mid) <= 0 or side not in {'buy', 'sell'}:
        raise ValueError('成交数量、价格或方向无效')
    spread = (mid-price) * qty * (1 if side == 'buy' else -1)
    common = dict(side=side, quantity=qty, notional_usd=qty*price,
                  parent_id=parent_id)
    detail = dict(fill, price_basis='已接受 RFQ 报价；未独立回读 /trades')
    # 零手续费是明确的当前规则假设；实际 /transfers fee 另记权威收付。
    return [event(f'rfq:{key}:spread', ts, 'spread', fill['market'], spread,
                  '报价对比', detail=detail, **common),
            event(f'rfq:{key}:fee', ts, 'fee', fill['market'], 0,
                  '当前 Variational 零手续费规则',
                  detail={'说明': '规则值 0；实际手续费以 /transfers 追加为准'}, **common)]


def record_switch(path, record):
    """切换合计仅为明细索引；账户权益差保留为证据，不作为成本。"""
    if record.get('kind') == 'rehearsal':
        return 0
    parent = 'switch:' + record['started_at']
    rows = []
    for phase in ('close_phase', 'open_phase'):
        payload = record.get(phase) or {}
        for fill in payload.get('legs', []) + payload.get('rollback_legs', []):
            rows.extend(fill_events(fill, fill.get('ts', record['started_at']), parent_id=parent))
    # 每个最终切换记录包含已成交腿，失败切换同样计入成本。
    amount = sum((number(r['amount_usd']) for r in rows), Decimal(0))
    rows.append(event(parent, record['started_at'], 'switch', 'XAUS/XAU/XAUT',
                      amount, '切换台账', detail={
                          'account_equity_delta': record.get('measured_wear_usd'),
                          '说明': '各腿成本合计，排除账户价格浮动和其它策略，不参与总额求和'}))
    return append(path, rows)


def transfer_events(transfers):
    """真实 /transfers：qty 为有符号结算现金，funding_rate 只用于识别类型。"""
    rows = []
    for transfer in transfers:
        if transfer.get('asset') != 'USDC':
            continue
        instrument = transfer.get('reference_instrument') or {}
        market = instrument.get('underlying') or 'ACCOUNT'
        kind = transfer.get('transfer_type')
        if not kind and transfer.get('funding_rate') not in (None, ''):
            kind = 'funding'
        if kind in {'deposit', 'withdrawal'}:
            kind = 'external_cashflow'
        if kind not in {'funding', 'fee', 'realized_pnl', 'external_cashflow', 'liquidation_penalty', 'referral_reward'}:
            kind = 'unclassified'
        # 无 id 的实抓流水以稳定业务字段指纹识别；不依赖预测费率、采样或分页位置。
        identity = {k: transfer.get(k) for k in (
            'created_at', 'asset', 'qty', 'reference_instrument',
            'ref_instrument_position_qty', 'transfer_type', 'funding_interval_s')}
        key = transfer.get('id') or hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        position = transfer.get('ref_instrument_position_qty')
        rows.append(event(f'transfer:{key}', transfer['created_at'], kind, market,
                          transfer['qty'], '/transfers',
                          side=('long' if number(position) > 0 else 'short' if number(position) < 0 else None)
                          if position is not None else None,
                          quantity=abs(number(position)) if position is not None else None,
                          detail={'raw_transfer': transfer}))
    return rows


async def scan_transfers(client, path):
    """复用已验证分页读取器，扫描所有策略但汇总严格隔离受管标的。"""
    from tools.verify_funding_units import fetch_all_transfers
    async def fetch(limit, offset):
        return await client.raw(f'/transfers?limit={limit}&offset={offset}')
    rows = transfer_events(await fetch_all_transfers(fetch))
    return append(path, rows)


def select(rows, since=None, until=None, *, strict_start=False):
    """汇总过滤含 since；权益区间使用 (start, end]，防止边界重复。"""
    start = timestamp(since) if since else None
    end = timestamp(until) if until else None
    return [r for r in rows if (start is None or (timestamp(r['ts']) > start if strict_start
            else timestamp(r['ts']) >= start)) and (end is None or timestamp(r['ts']) <= end)]


def summarize(rows, scope='swap_carry'):
    """切换父项只展示；BTC 等其它策略绝不进入 carry 成本科目。"""
    result = {k: Decimal(0) for k in LABELS}
    for row in rows:
        kind = row['event_type']
        if kind in result and (scope == 'account' or row['market'] in MARKETS or
                               kind == 'switch' and row['market'] == 'XAUS/XAU/XAUT'):
            result[kind] += number(row['amount_usd'])
    result['total'] = sum((v for k, v in result.items() if k != 'switch'), Decimal(0))
    return result


def reconcile(rows, inputs, threshold='1'):
    """对账证据采用内部 schema；各项必须显式提供，不能把缺失数据填零。"""
    if inputs['scope'] not in {'account', 'swap_carry'} or not inputs['source']:
        raise ValueError('对账范围或证据来源无效')
    if timestamp(inputs['start_ts']) >= timestamp(inputs['end_ts']):
        raise ValueError('对账区间无效')
    threshold = number(threshold)
    if threshold < 0:
        raise ValueError('残差阈值不得为负')
    selected = select(rows, inputs['start_ts'], inputs['end_ts'], strict_start=True)
    ledger_mode = inputs.get('account_ledger_evidence') is True
    if ledger_mode:
        cost_rows = [r for r in selected if inputs['scope'] == 'account'
                     or r['market'] in MARKETS | {'ACCOUNT', 'XAUS/XAU/XAUT'}]
        result = summarize(cost_rows, 'account')
    else:
        result = summarize(selected)
    for key in ('start_equity', 'end_equity', 'realized_price_pnl', 'unrealized_change',
                'external_cashflow', 'other_strategies'):
        # 仅快照汇总允许显式未知浮动；保留 None，并标明只是已知科目的差额。
        result[key] = (None if ledger_mode and key == 'unrealized_change'
                       and inputs[key] is None else number(inputs[key]))
    if not ledger_mode and inputs['scope'] == 'swap_carry' and result['other_strategies'] != 0:
        raise ValueError('策略权益范围不得混入其它策略')
    for key in ('unclassified', 'referral_reward'):
        result[key] = sum((number(r['amount_usd']) for r in selected
                           if r['event_type'] == key), Decimal(0))
    # 平台成交价格口径已含 spread；这不是残差回填，而是确定性的重复计入抵销。
    result['spread_embedded_adjustment'] = (-result['spread']
        if inputs.get('pnl_basis') == 'platform_execution_price' else Decimal(0))
    result['equity_change'] = result['end_equity'] - result['start_equity']
    result['explained'] = result['total'] + sum((result[k] for k in (
        'realized_price_pnl', 'unrealized_change', 'external_cashflow',
        'other_strategies', 'spread_embedded_adjustment', 'unclassified', 'referral_reward')
        if result[k] is not None), Decimal(0))
    result['residual'] = result['equity_change'] - result['explained']
    result['residual_ratio'] = (abs(result['residual']) / abs(result['equity_change']) * 100
                                if result['equity_change'] else None)
    result['evidence_complete'] = result['unrealized_change'] is not None
    result['warning'] = abs(result['residual']) > threshold
    result.update(start_ts=inputs['start_ts'], end_ts=inputs['end_ts'],
                  scope=inputs['scope'], threshold=threshold, source=inputs['source'],
                  basis_note=inputs.get('basis_note', '按输入证据指定的价格盈亏口径对账'))
    return result


def read_report(rows, path=DEFAULT_RECON_PATH, since=None):
    """对账区间必须与证据吻合，不用整段权益差解释过滤后的流水。"""
    try:
        inputs = json.loads(Path(path).read_text(encoding='utf-8'))
        if since and timestamp(since) != timestamp(inputs['start_ts']):
            raise ValueError('--since 与权益快照起点不同，缺少该时点权益证据')
        return reconcile(rows, inputs, inputs.get('threshold', '1')), None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f'对账不可用：{exc}'


async def capture_reconciliation(client, ledger_path, report_path, *, now=None):
    """保存账户权益基线及逐标的浮动；BTC 现金与浮动单列其它策略。"""
    now = now or datetime.now(timezone.utc)
    portfolio = await client.raw('/portfolio')
    # /portfolio 实抓字段是 balance、upnl，没有 available_margin。
    equity = number(portfolio['balance']) + number(portfolio['upnl'])
    payload = await client.get_positions()
    positions = payload if isinstance(payload, list) else payload['positions']
    carry_upnl, other_upnl = Decimal(0), Decimal(0)
    for position in positions:
        info = position.get('position_info', position)
        underlying = info['instrument']['underlying']
        upnl = number(position['upnl'])
        if underlying in MARKETS:
            carry_upnl += upnl
        else:
            other_upnl += upnl
    if abs(carry_upnl + other_upnl - number(portfolio['upnl'])) > Decimal('.01'):
        raise ValueError('持仓浮动与账户浮动不同步，暂不保存对账快照')
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        current = dict(ts=now.isoformat(), equity=str(equity), carry_upnl=str(carry_upnl),
                       other_upnl=str(other_upnl))
        baseline = previous['baseline'] if previous else current
        rows, error = load(ledger_path)
        if error:
            raise ValueError(error)
        period = select(rows, baseline['ts'], current['ts'], strict_start=True)
        realized = sum((number(r['amount_usd']) for r in period
                        if r['market'] in MARKETS and r['event_type'] == 'realized_pnl'), Decimal(0))
        external = sum((number(r['amount_usd']) for r in period
                        if r['event_type'] == 'external_cashflow'), Decimal(0))
        other = sum((number(r['amount_usd']) for r in period
                     if r['market'] not in MARKETS | {'ACCOUNT', 'XAUS/XAU/XAUT'}
                     and r['event_type'] in {'funding', 'fee', 'realized_pnl', 'liquidation_penalty'}), Decimal(0))
        basis_start = spread_basis(select(rows, until=baseline['ts']))
        basis_end = spread_basis(select(rows, until=current['ts']))
        realized_spread = basis_end['realized'] - basis_start['realized']
        unrealized_spread = basis_end['unrealized'] - basis_start['unrealized']
        inputs = dict(baseline=baseline, start_ts=baseline['ts'], end_ts=current['ts'],
                      start_equity=baseline['equity'], end_equity=current['equity'],
                      realized_price_pnl=str(realized-realized_spread),
                      unrealized_change=str(carry_upnl-number(baseline['carry_upnl'])-unrealized_spread),
                      external_cashflow=str(external),
                      other_strategies=str(other+other_upnl-number(baseline['other_upnl'])),
                      scope='account', source='/portfolio.balance + upnl；/positions.upnl；/transfers',
                      pnl_basis='quote_mid_known_spread', threshold=previous.get('threshold', '1') if previous else '1',
                      realized_spread=str(realized_spread), unrealized_spread=str(unrealized_spread),
                      basis_note='平台价格盈亏剔除已记录滑点；未知历史滑点仍含在价格项中，不虚构拆分')
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(inputs, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
        return inputs


# 上下文变量让并发调用可注入路径，而不修改真实客户端或运行时配置。
ACTIVE_PATH = ContextVar('swap_carry_cost_path', default=DEFAULT_PATH)


def ledger_session(function):
    """守护轮次可显式传入台账路径，默认跟随注入的审计目录。"""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        path = kwargs.get('cost_ledger_path')
        if path is None and kwargs.get('audit_path') is not None:
            path = Path(kwargs['audit_path']).with_name(DEFAULT_PATH.name)
        token = ACTIVE_PATH.set(Path(path) if path is not None else DEFAULT_PATH)
        try:
            return await function(*args, **kwargs)
        finally:
            ACTIVE_PATH.reset(token)
    return wrapped


def record_allocation(path, ts, underlying, result):
    """仅确认转换记账；保证金本金是账户内部搬移，不是支出。"""
    if result.get('conversion_status') != 'confirmed':
        return 0
    key = result.get('conversion_id')
    if not key:
        raise ValueError('已确认保证金转换缺少 conversion_id')
    return append(path, [event('allocation:' + str(key), ts, 'allocation', underlying,
                               0, 'allocation 转换确认', detail={
                                   'conversion_id': str(key),
                                   '说明': '账户内部保证金划转，本金不计成本；当前转换无收费，实际收费流水另计'})])


def spread_basis(rows):
    """按加权持仓成本追踪已知滑点；平仓时把开仓滑点从浮动移入已实现。

    仅重分类有报价证据的 spread，不推算缺失历史。初始仓位未知的 reduce_only
    成交只确认当笔平仓滑点，不虚构其历史开仓成本。
    """
    inventory = {}
    realized = Decimal(0)
    for row in sorted(rows, key=lambda r: timestamp(r['ts'])):
        if row['event_type'] != 'spread' or row['market'] not in MARKETS:
            continue
        quantity = number(row['quantity'])
        signed = quantity * (1 if row['side'] == 'buy' else -1)
        amount = number(row['amount_usd'])
        held, embedded = inventory.get(row['market'], (Decimal(0), Decimal(0)))
        if held * signed < 0:
            closing = min(abs(held), quantity)
            released = embedded * closing / abs(held)
            realized += released + amount * closing / quantity
            embedded -= released
            held += signed
            if quantity > closing:
                embedded += amount * (quantity-closing) / quantity
        elif held == 0 and row['detail'].get('reduce_only') is True:
            # 缺失期初开仓历史时，不把已确认平仓误当成新反向仓位。
            realized += amount
        else:
            held += signed
            embedded += amount
        inventory[row['market']] = (held, embedded)
    return {'realized': realized,
            'unrealized': sum((item[1] for item in inventory.values()), Decimal(0))}


def reconciliation_period(rows, since=None):
    """区间来自全部台账，不随策略过滤或快照覆盖范围缩短。"""
    if not rows:
        raise ValueError('台账无事件，无法确定对账区间')
    start = timestamp(since) if since else min(timestamp(r['ts']) for r in rows)
    end = max(timestamp(r['ts']) for r in rows)
    if start >= end:
        raise ValueError(f'对账区间无效：({start.isoformat()}, {end.isoformat()}]')
    return start, end


def snapshot_report(rows, path, since=None, *, scope='account', threshold='1',
                    max_gap_seconds=900):
    """从组合权益和全量台账构造只读证据；未知浮动保留为空，不推算回填。"""
    try:
        start, end = reconciliation_period(rows, since)
        if scope not in {'account', 'swap_carry'}:
            raise ValueError('对账范围无效')
        if number(max_gap_seconds) < 0:
            raise ValueError('快照容差不得为负')
        snapshots = []
        with Path(path).open(encoding='utf-8') as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                ts = row['ts']
                when = (datetime.fromtimestamp(float(number(ts)), timezone.utc)
                        if isinstance(ts, (int, float)) else timestamp(ts))
                try:
                    if row.get('errors', {}).get('variational'):
                        continue
                    equity = number(row['accounts']['variational'])
                except (KeyError, ValueError, TypeError):
                    continue
                snapshots.append((when, equity))
        chosen = []
        missing = []
        for label, boundary in [('期初', start), ('期末', end)]:
            nearest = min(snapshots, key=lambda item: (abs((item[0]-boundary).total_seconds()),
                                                      item[0]), default=None)
            if nearest is None or abs((nearest[0]-boundary).total_seconds()) > max_gap_seconds:
                missing.append(f'缺少{label}权益快照（目标 {boundary.isoformat()}；容差 {max_gap_seconds} 秒）')
            else:
                chosen.append(nearest)
        if missing:
            raise ValueError('；'.join(missing))
        if chosen[0][0] >= chosen[1][0]:
            raise ValueError('期初/期末权益快照不能为同一时点或倒序')
        rows = [classify_record(row) for row in rows]
        period = select(rows, start.isoformat(), end.isoformat(), strict_start=True)
        def total(kinds, predicate=lambda row: True):
            return sum((number(r['amount_usd']) for r in period
                        if r['event_type'] in kinds and predicate(r)), Decimal(0))
        other_market = lambda row: row['market'] not in MARKETS | {'ACCOUNT', 'XAUS/XAU/XAUT'}
        # 外部充提、未知流水、推荐奖励属于账户公共科目，不归入其它策略。
        other = (total({'funding', 'fee', 'realized_pnl', 'allocation', 'liquidation_penalty'}, other_market)
                 if scope == 'swap_carry' else Decimal(0))
        realized = total({'realized_pnl'}, lambda row: scope == 'account' or not other_market(row))
        note = ('按平台成交价格汇总；权益快照缺少未实现浮动变化证据，残差为权益变化减已知科目，'
                '可能包含未实现盈亏；数值为零也不代表证据完整。流水区间为 (起点, 终点]，起点同刻流水不计入。')
        if scope == 'swap_carry':
            note += ' 权益变化含其它策略；账户公共费用保留在对应科目；其它策略盈亏单列非 XAUS/XAU/XAUT 的已知流水，浮动仍未知。'
        inputs = dict(start_ts=start.isoformat(), end_ts=end.isoformat(), scope=scope,
                      source=f'{path} accounts.variational；成本台账',
                      start_equity=chosen[0][1], end_equity=chosen[1][1],
                      realized_price_pnl=realized, unrealized_change=None,
                      external_cashflow=total({'external_cashflow'}), other_strategies=other,
                      account_ledger_evidence=True, pnl_basis='platform_execution_price', basis_note=note)
        report = reconcile(rows, inputs, threshold)
        report['snapshot_start_ts'] = chosen[0][0].isoformat()
        report['snapshot_end_ts'] = chosen[1][0].isoformat()
        report['snapshot_offsets_seconds'] = tuple((item[0]-boundary).total_seconds()
                                                   for item, boundary in zip(chosen, (start, end)))
        return report, None
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        return None, f'对账不可用：{exc}'
