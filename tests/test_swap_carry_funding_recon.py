"""资金费对账离线回归：流水沿用已抓取的真实 schema，不补造接口字段。"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from tests.test_verify_funding_units import BTC_TRANSFER


def transfer(day, days=1):
    """仅改真实流水的标的和数值；真实记录没有 apply_time 和价格。"""
    return {**BTC_TRANSFER, 'created_at': f'2026-09-{day:02d}T21:05:00Z',
            'funding_interval_s': 0, 'funding_rate': str(Decimal('.0001') * Decimal(str(days))),
            'qty': str(-Decimal('.2') * Decimal(str(days))), 'ref_instrument_position_qty': '1',
            'reference_instrument': {**BTC_TRANSFER['reference_instrument'],
                                     'underlying': 'XAUS', 'instrument_type': 'swap', 'funding_interval_s': 0}}


def sample(day, rate='-.0365'):
    """采样器现有 JSONL 的 long_rate 与 RFQ 字段，价格为独立估计证据。"""
    at = f'2026-09-{day:02d}T21:05:00Z'
    observed = f'2026-09-{day:02d}T21:00:00Z'
    return {'observed_at': observed, 'xaus': {'funding': {
        'long_rate': {'raw_rate': rate, 'normalized_annual_rate': rate,
                      'apply_time': at, 'observed_at': observed, 'coverage_days': 1,
                      'day_count_basis': 365}, 'apply_time': at}},
        'rfq': {'qty': '.5', 'xaus': {'bid': '1999', 'ask': '2001', 'approx_notional_usd': '448'}}}


def run(tmp_path, rows, samples):
    from tools.swap_carry_funding_recon import reconcile
    samples_path = tmp_path/'samples.jsonl'
    samples_path.write_text(''.join(json.dumps(s)+'\n' for s in samples))
    client = AsyncMock()
    client.raw.return_value = {'result': rows, 'pagination': {'object_count': len(rows)}}
    result = asyncio.run(reconcile(client, samples_path=samples_path, output_path=tmp_path/'recon.jsonl'))
    client.raw.assert_awaited_once_with('/transfers?limit=100&offset=0')
    return result


@pytest.mark.parametrize('day,days,previous,conclusion,covered', [
    (8,1,7,'日常计提正常',['2026-09-08']),
    (7,3,4,'周一追补模式',['2026-09-05','2026-09-06','2026-09-07']),
    (11,3,10,'周五预收模式',['2026-09-11','2026-09-12','2026-09-13']),
])
def test_coverage(tmp_path, day, days, previous, conclusion, covered):
    result = run(tmp_path, [transfer(day,days),transfer(previous)], [sample(previous),sample(day)])
    row = result['latest']
    assert row['conclusion'] == conclusion
    assert row['coverage_days'] == days
    assert row['covered_dates'] == covered
    assert Decimal(row['notional_usdc']) == 2000
    assert Decimal(row['actual_rate']) == Decimal('.0001') * Decimal(str(days))
    assert Decimal(row['ratio']) == days
    assert row['apply_time_source'] == '采样器预测计提时间'
    assert row['notional_source'] == '结算仓位数量 × 结算前 RFQ 中价（估计）'
    assert Decimal(row['interval_days']) == day-previous


def test_deviation_alert(tmp_path):
    result = run(tmp_path, [transfer(8,1.5)], [sample(8)])
    assert result['latest']['level'] == 'warning'
    assert '预测口径可能有问题' in result['latest']['conclusion']


def test_deduplicate_across_restarts(tmp_path):
    run(tmp_path,[transfer(8)],[sample(8)])
    before = (tmp_path/'recon.jsonl').read_bytes()
    result = run(tmp_path,[transfer(8),transfer(8)],[sample(8)])
    assert result['new_count'] == 0
    assert (tmp_path/'recon.jsonl').read_bytes() == before


def test_missing_prediction_does_not_invent_evidence(tmp_path):
    row = run(tmp_path,[transfer(8)],[])['latest']
    assert row['conclusion'] == '无预测可比'
    assert row['predicted_annual_rate'] is None
    assert row['coverage_days'] is None
    assert row['apply_time'] is None
    assert row['notional_usdc'] is None


def test_future_or_wrong_settlement_prediction_rejected(tmp_path):
    row = run(tmp_path,[transfer(8)],[sample(9), sample(7)])['latest']
    assert row['conclusion'] == '无预测可比'


def test_first_monday_without_previous_is_not_weekend_proof(tmp_path):
    row = run(tmp_path,[transfer(7,3)],[sample(7)])['latest']
    assert row['coverage_days'] == 3
    assert row['conclusion'] != '周一追补模式'


def test_cli_history(tmp_path,capsys):
    from tools.show_funding_recon import main
    run(tmp_path,[transfer(8),transfer(7)],[sample(8),sample(7)])
    assert main(['--path',str(tmp_path/'recon.jsonl'),'--last','1']) == 0
    output = capsys.readouterr().out
    assert '日常计提正常' in output
    assert '2026-09-08' in output
    assert '2026-09-07' not in output


def test_recon_consumes_actual_sampler_output_when_rfq_closed(tmp_path):
    """生产采样函数输出直接作为夹具，不构造新增字段。"""
    from tests.test_sample_swap_carry import _configured_client, NOW
    from tools.sample_swap_carry import sample_once
    client = _configured_client()
    client.configured['quote_xaus'] = RuntimeError('休市无 RFQ')
    observed = asyncio.run(sample_once(client, observed_at=NOW))
    paid = transfer(4)
    paid['qty'] = str(-Decimal('4435.04') * Decimal('.057214') / 365)
    row = run(tmp_path,[paid],[observed])['latest']
    assert row['conclusion'] == '日常计提正常'
    assert row['notional_source'] == '结算仓位数量 × 结算前元数据标记价（估计）'


def test_prediction_read_after_apply_is_rejected(tmp_path):
    observed = sample(8)
    observed['observed_at'] = '2026-09-08T21:06:00Z'
    paid = transfer(8)
    paid['created_at'] = '2026-09-08T21:07:00Z'
    assert run(tmp_path,[paid],[observed])['latest']['conclusion'] == '无预测可比'


def test_newest_prediction_and_price_do_not_use_funding_rate_as_notional(tmp_path):
    earlier = sample(8, '-.073')
    earlier['observed_at'] = earlier['xaus']['funding']['long_rate']['observed_at'] = '2026-09-08T20:30:00Z'
    paid = transfer(8)
    paid['funding_rate'] = '999'
    row = run(tmp_path,[paid],[sample(8),earlier])['latest']
    assert row['conclusion'] == '日常计提正常'
    assert Decimal(row['notional_usdc']) == 2000
    assert Decimal(row['predicted_annual_rate']) == Decimal('-.0365')


def test_empty_transfers_do_not_create_history(tmp_path):
    result = run(tmp_path,[],[])
    assert result['new_count'] == 0
    assert result['latest'] is None
    assert not (tmp_path/'recon.jsonl').exists()


def test_short_position_cannot_compare_with_long_prediction(tmp_path):
    paid = transfer(8)
    paid['ref_instrument_position_qty'] = '-1'
    row = run(tmp_path,[paid],[sample(8)])['latest']
    assert row['level'] == 'warning'
    assert row['coverage_days'] is None
    assert '空头' in row['conclusion']
