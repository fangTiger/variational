"""隔离保证金的离线失败路径与金额语义测试。"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal as D
from unittest.mock import AsyncMock, Mock

import pytest

from adapters.variational_client import VariationalClient
from tools import run_swap_carry_guard as guard


def test_required_allocation():
    from engine.isolated_allocation import required_allocation
    assert required_allocation(D('1994.36'), D('99.72'), D('.08')) == D('251.2912')


@pytest.mark.parametrize('n,m,d', [('0','1','.08'), ('-1','1','.08'), ('1','-1','.08'), ('1','0','0'), ('NaN','0','.08'), ('1','Infinity','.08'), ('1','0','NaN'), ('1','2','.08')])
def test_invalid_allocation(n,m,d):
    from engine.isolated_allocation import required_allocation
    with pytest.raises(ValueError):
        required_allocation(D(n), D(m), D(d))


def snapshot(distance='.05', notional='1994.36'):
    return dict(initial_margin=D('199.44'), maintenance_margin=D('99.72'), notional=D(notional), distance=D(distance))


@pytest.mark.parametrize('case,expected,level', [
    ('cap',0,'warning'), ('balance',0,'warning'), ('missing_balance',0,'warning'),
    ('limit',0,'warning'), ('bad_readback',1,'critical'), ('timeout',1,'critical'),
    ('cross',0,'info'), ('healthy',0,'info'), ('dry_run',0,'info'), ('success',1,'info'),
])
def test_guard_allocation(tmp_path, case, expected, level):
    client = AsyncMock()
    initial = snapshot('.09' if case == 'healthy' else '.05', '10000' if case == 'cap' else '1994.36')
    client.get_isolated_allocation.side_effect = [initial] + [snapshot('.05' if case == 'bad_readback' else '.081')] * guard.execution._FLAT_TRIES
    configure_account(client, balance='300.44' if case == 'balance' else '1000')
    if case == 'missing_balance':
        del client.raw.return_value['balance']
    if case == 'timeout':
        client.set_isolated_allocation.side_effect = TimeoutError('请求超时')
    now = datetime(2026,9,7,tzinfo=timezone.utc)
    ledger = tmp_path / 'allocation.json'
    if case == 'limit':
        ledger.write_text(json.dumps({'date':'2026-09-07','attempts':30,'regular_count':30,'abnormal_count':0}))
    result = asyncio.run(guard._maintain_isolated_allocation(
        client, underlying='XAUS', mode=guard.MarginModeStatus('cross' if case == 'cross' else 'isolated','测试'),
        dry_run=case == 'dry_run', observed_at=now, ledger_path=ledger, audit_path=tmp_path/'audit.jsonl',
    ))
    assert client.set_isolated_allocation.await_count == expected
    assert result['level'] == level
    if expected:
        client.set_isolated_allocation.assert_awaited_once_with('XAUS', D('251.30'))
        assert json.loads(ledger.read_text())['attempts'] == 1
    if case in {'bad_readback','success'}:
        assert client.get_isolated_allocation.await_count == (1 + guard.execution._FLAT_TRIES if case == 'bad_readback' else 2)
    if case == 'balance':
        assert result['available_margin'] == D('21')
        assert '账户可用保证金不足' in result['message']
    if case == 'missing_balance':
        assert '/portfolio.balance' in result['message']
    if case == 'dry_run':
        assert 'dry-run' in result['message']
        assert not ledger.exists()
    if case == 'cross':
        assert result['message'] == '全仓腿无独立保证金桶，由账户级保证金率兜底'
        client.get_isolated_allocation.assert_not_awaited()


def test_client_read_and_write():
    client = object.__new__(VariationalClient)
    client.get_positions = AsyncMock(return_value=[{
        'position_info': {'instrument': {'underlying':'XAUS'}, 'qty':'.452241408'},
        'price_info': {'underlying_price':'4409.95'},
        'initial_margin':'199.44','maintenance_margin':'99.72','estimated_liquidation_price':'4189.70',
    }])
    client._instrument_kind_from_metadata = AsyncMock(return_value=('swap','commodity'))
    client._post = AsyncMock(return_value={'conversion_id': '3a63f279-test'})
    result = asyncio.run(client.get_isolated_allocation('XAUS'))
    assert result['distance'] == (D('4409.95')-D('4189.70'))/D('4409.95')
    assert abs(result['notional']-D('1994.36')) < D('.01')
    asyncio.run(client.set_isolated_allocation('XAUS',D('251.30')))
    client._post.assert_awaited_once_with('/sub_accounts/allocation', {
        'instrument': {'underlying':'XAUS','instrument_type':'swap','kind':'commodity','funding_interval_s':0,'settlement_asset':'USDC'},
        'target_allocation':'251.30',
    })


@pytest.mark.parametrize('value',['0','-1','NaN','Infinity'])
def test_client_rejects_invalid_target(value):
    client = object.__new__(VariationalClient)
    client._post = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(client.set_isolated_allocation('XAUS',D(value)))
    client._post.assert_not_awaited()


def test_panel_allocation_metric():
    from panel.providers import swap_carry
    client = AsyncMock()
    client.get_isolated_allocation.return_value = snapshot()
    metric = asyncio.run(swap_carry._allocation_metric(client, 'XAUS'))
    assert '当前桶 $194.45' in metric.value
    assert '目标桶 $251.30' in metric.value
    assert '5.00%' in metric.value
    assert '公式要求值' in metric.value
    assert '不含 allocation 追加部分' in metric.value


@pytest.mark.parametrize('method,path,body', [
    ('isolate','/sub_accounts/isolate',None), ('deisolate','/sub_accounts/deisolate',None),
    ('set_leverage','/settlement_pools/set_leverage',{'leverage':'3','asset':'USDC'}),
])
def test_manual_wrappers(method,path,body):
    client = object.__new__(VariationalClient)
    client._instrument_kind_from_metadata = AsyncMock(return_value=('swap','commodity'))
    client._post = AsyncMock(return_value={'conversion_id': '3a63f279-test'})
    args = (D('3'),'USDC') if method == 'set_leverage' else ('XAUS',)
    asyncio.run(getattr(client,method)(*args))
    assert client._post.await_args.args[0] == path
    if body is not None:
        assert client._post.await_args.args[1] == body
    else:
        assert client._post.await_args.args[1]['instrument']['funding_interval_s'] == 0


def test_dry_run_blocks_underlying_http():
    from types import SimpleNamespace
    client = object.__new__(VariationalClient)
    client._http = SimpleNamespace(request=AsyncMock())

    @guard._dry_run_http_guard
    async def probe(*, dry_run):
        with pytest.raises(ValueError, match='dry-run'):
            await client._request('POST','/quotes/indicative',{})
    asyncio.run(probe(dry_run=True))
    client._http.request.assert_not_awaited()


@pytest.mark.parametrize('corrupt', ['{', '{"date":"2026-09-07","attempts":-1}', '{"date":"2026-09-08","attempts":0}'])
def test_bad_ledger_fails_closed(tmp_path,corrupt):
    client = AsyncMock()
    ledger = tmp_path/'count.json'
    ledger.write_text(corrupt)
    result = asyncio.run(guard._maintain_isolated_allocation(
        client,underlying='XAUS',mode=guard.MarginModeStatus('isolated','测试'),dry_run=False,
        observed_at=datetime(2026,9,7,tzinfo=timezone.utc),ledger_path=ledger,audit_path=tmp_path/'audit.jsonl'))
    assert result['level'] == 'warning'
    client.set_isolated_allocation.assert_not_awaited()


@pytest.mark.parametrize('qty,liq,expected', [('-1','110','.10'),('1','90','.10')])
def test_client_direction(qty,liq,expected):
    client = object.__new__(VariationalClient)
    client.get_positions = AsyncMock(return_value=[{'instrument':{'underlying':'XAU'},'qty':qty,
        'mark_price':'100','initial_margin':'20','maintenance_margin':'5','estimated_liquidation_price':liq}])
    assert asyncio.run(client.get_isolated_allocation('XAU'))['distance'] == D(expected)


@pytest.mark.parametrize('rows', [[],[{},{}]])
def test_client_missing_or_duplicate_rejected(rows):
    client = object.__new__(VariationalClient)
    client.get_positions = AsyncMock(return_value=[{'instrument':{'underlying':'XAUS'},**r} for r in rows])
    with pytest.raises(ValueError):
        asyncio.run(client.get_isolated_allocation('XAUS'))


def test_unknown_margin_mode_cannot_authorize_money_write(tmp_path):
    client = AsyncMock()
    client.get_isolated_allocation.side_effect = [snapshot(),snapshot('.09')]
    configure_account(client)
    result = asyncio.run(guard._maintain_isolated_allocation(
        client,underlying='XAUS',mode=guard.MarginModeStatus('isolated','保守默认'),dry_run=False,
        observed_at=datetime(2026,9,7,tzinfo=timezone.utc),ledger_path=tmp_path/'count.json',audit_path=tmp_path/'audit.jsonl'))
    client.set_isolated_allocation.assert_not_awaited()
    assert result['level'] == 'warning'
    assert 'isolated_only' in result['message']
    assert 'margin_mode' in result['message']


def test_daily_limit_persists_across_rounds(tmp_path):
    client = AsyncMock()
    client.get_isolated_allocation.return_value = snapshot()
    configure_account(client)
    ledger = tmp_path/'count.json'
    ledger.write_text(json.dumps({'date':'2026-09-07','attempts':4,'regular_count':0,'abnormal_count':4}))
    async def run():
        for _ in range(2):
            await guard._maintain_isolated_allocation(
                client,underlying='XAUS',mode=guard.MarginModeStatus('isolated','测试'),dry_run=False,
                observed_at=datetime(2026,9,7,tzinfo=timezone.utc),ledger_path=ledger,audit_path=tmp_path/'audit.jsonl')
    asyncio.run(run())
    client.set_isolated_allocation.assert_awaited_once()
    assert json.loads(ledger.read_text())['abnormal_count'] == 5


def test_cap_environment_cannot_raise_hard_limit(tmp_path,monkeypatch):
    monkeypatch.setenv('MAX_ALLOCATION_USD','10000')
    client = AsyncMock()
    client.get_isolated_allocation.return_value = snapshot(notional='10000')
    configure_account(client, balance='10000')
    result = asyncio.run(guard._maintain_isolated_allocation(
        client,underlying='XAUS',mode=guard.MarginModeStatus('isolated','测试'),dry_run=False,
        observed_at=datetime(2026,9,7,tzinfo=timezone.utc),ledger_path=tmp_path/'count.json',audit_path=tmp_path/'audit.jsonl'))
    client.set_isolated_allocation.assert_not_awaited()
    assert result['level'] == 'warning'


def test_money_write_rejects_missing_instrument_metadata():
    client = object.__new__(VariationalClient)
    client._instrument_kind_from_metadata = AsyncMock(return_value=None)
    client._post = AsyncMock()
    with pytest.raises(ValueError,match='元数据'):
        asyncio.run(client.set_isolated_allocation('XAUS',D('250')))
    client._post.assert_not_awaited()


def configure_account(client, balance='1000'):
    """仅使用真实字段：账户 balance/upnl 和全部持仓的 initial_margin。"""
    client.set_isolated_allocation.return_value = '3a63f279-test'
    client.wait_allocation_conversion.return_value = 'confirmed'
    client.raw.return_value = {'balance': balance, 'upnl': '20'}
    client.get_positions.return_value = [
        {'position_info': {'instrument': {'underlying': 'XAUS'}}, 'initial_margin': '199.44'},
        {'position_info': {'instrument': {'underlying': 'XAU'}}, 'initial_margin': '50'},
        {'position_info': {'instrument': {'underlying': 'BTC'}}, 'initial_margin': '50'},
    ]
    assert set(client.raw.return_value) == {'balance', 'upnl'}
    assert 'available_margin' not in client.raw.return_value


@pytest.mark.parametrize('wrapped', [False, True])
def test_available_margin_uses_real_schema_and_all_positions(wrapped):
    client = AsyncMock()
    configure_account(client)
    if wrapped:
        client.get_positions.return_value = {'positions': client.get_positions.return_value}
    assert asyncio.run(guard._available_account_margin(client)) == D('720.56')
    client.raw.assert_awaited_once_with('/portfolio')
    client.get_positions.assert_awaited_once_with()


@pytest.mark.parametrize('field', ['balance', 'upnl', 'initial_margin'])
@pytest.mark.parametrize('value', ['missing', None, 'bad', 'NaN', 'Infinity'])
def test_available_margin_rejects_missing_or_invalid_fields(field, value):
    client = AsyncMock()
    configure_account(client)
    if field == 'initial_margin':
        source = client.get_positions.return_value[2]
        label = '/positions[2].initial_margin'
    else:
        source = client.raw.return_value
        label = '/portfolio.' + field
    if value == 'missing':
        del source[field]
    else:
        source[field] = value
    with pytest.raises(ValueError) as exc:
        asyncio.run(guard._available_account_margin(client))
    assert label in str(exc.value)


def test_available_margin_rejects_negative_initial_margin():
    client = AsyncMock()
    configure_account(client)
    client.get_positions.return_value[2]['initial_margin'] = '-1'
    with pytest.raises(ValueError, match=r'/positions\[2\].initial_margin'):
        asyncio.run(guard._available_account_margin(client))


def test_available_margin_preserves_negative_equity_and_empty_positions():
    client = AsyncMock()
    configure_account(client, balance='10')
    client.raw.return_value['upnl'] = '-20'
    client.get_positions.return_value = []
    assert asyncio.run(guard._available_account_margin(client)) == D('-10')



@pytest.fixture(autouse=True)
def allocation_polling(monkeypatch):
    """跳过真实等待和桌面通知，保留轮询次数校验。"""
    monkeypatch.setattr(guard.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(guard, 'notify', Mock())


@pytest.mark.parametrize('case', ['delayed', 'stale', 'post_failure'])
def test_allocation_readback_polling(tmp_path, monkeypatch, caplog, case):
    """经过真实客户端读取链路，证明每次回读都会重新请求仓位。"""
    from unittest.mock import Mock
    client = object.__new__(VariationalClient)
    before = {'instrument': {'underlying': 'XAUS'}, 'qty': '1', 'mark_price': '2000',
              'initial_margin': '200', 'maintenance_margin': '100',
              'estimated_liquidation_price': '1900'}
    after = {**before, 'initial_margin': '252', 'estimated_liquidation_price': '1838'}
    reads = [before, before, before]
    reads += [after] if case == 'delayed' else [before] * (guard.execution._FLAT_TRIES - 1)
    async def get(path):
        if path == '/portfolio':
            return {'balance': '1000', 'upnl': '0'}
        assert path == '/positions'
        return [reads.pop(0)]
    client._get = AsyncMock(side_effect=get)
    client.wait_allocation_conversion = AsyncMock(return_value='confirmed')
    client.set_isolated_allocation = AsyncMock(return_value='3a63f279-test',
        side_effect=TimeoutError('请求超时') if case == 'post_failure' else None)
    notice = Mock()
    monkeypatch.setattr(guard, 'notify', notice)
    caplog.set_level('INFO', logger=guard.__name__)
    result = asyncio.run(guard._maintain_isolated_allocation(
        client, underlying='XAUS', mode=guard.MarginModeStatus('isolated', '测试'),
        dry_run=False, observed_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        ledger_path=tmp_path/'count.json', audit_path=tmp_path/'audit.jsonl'))
    client.set_isolated_allocation.assert_awaited_once_with('XAUS', D('252.00'))
    position_reads = sum(call.args == ('/positions',) for call in client._get.await_args_list)
    if case == 'delayed':
        assert result['level'] == 'info'
        assert result['readback_attempts'] == 2
        assert position_reads == 4
        notice.assert_not_called()
        audit = json.loads((tmp_path/'audit.jsonl').read_text().splitlines()[-1])
        for record in (result, audit):
            assert D(record['before_allocation']) == D('195')
            assert D(record['before_distance']) == D('.05')
            assert D(record['after_allocation']) == D('253.9')
            assert D(record['after_distance']) == D('.081')
        assert any(r.levelname == 'INFO' and '达标' in r.message for r in caplog.records)
    else:
        assert result['level'] == 'critical'
        notice.assert_called_once()
        if case == 'stale':
            assert position_reads == 2 + guard.execution._FLAT_TRIES
            assert 'POST 成功但回读未达标' in result['message']
            assert '本轮不再重复 POST' in result['message']
        else:
            assert position_reads == 2
            assert 'POST 失败' in result['message']
            assert '本轮不重试' in result['message']


@pytest.mark.parametrize('case', ['cooldown', 'post_failure', 'abnormal_limit', 'regular_limit', 'success', 'calculation'])
def test_separate_allocation_budgets(tmp_path, case):
    """失败、成功和间隔分别计数，所有账户字段复用真实 schema。"""
    client = AsyncMock()
    configure_account(client)
    client.get_isolated_allocation.side_effect = [snapshot(),snapshot('.081')]
    if case == 'post_failure':
        client.set_isolated_allocation.side_effect = TimeoutError('超时')
    if case == 'calculation':
        client.get_isolated_allocation.side_effect = [snapshot(notional='NaN')]
    ledger = tmp_path/'counts.json'
    ledger.write_text(json.dumps({'date':'2026-09-07', 'attempts':0,
        'regular_count':30 if case == 'regular_limit' else 0,
        'abnormal_count':5 if case == 'abnormal_limit' else 0,
        'last_success_at':'2026-09-06T23:55:00+00:00' if case == 'cooldown' else None}))
    result = asyncio.run(guard._maintain_isolated_allocation(
        client, underlying='XAUS', mode=guard.MarginModeStatus('isolated','测试'), dry_run=False,
        observed_at=datetime(2026,9,7,tzinfo=timezone.utc),ledger_path=ledger,audit_path=tmp_path/'audit.jsonl'))
    saved = json.loads(ledger.read_text())
    if case in {'cooldown','abnormal_limit','regular_limit','calculation'}:
        client.set_isolated_allocation.assert_not_awaited()
    else:
        client.set_isolated_allocation.assert_awaited_once()
    assert saved['abnormal_count'] == (5 if case == 'abnormal_limit' else 1 if case in {'post_failure','calculation'} else 0)
    assert saved['regular_count'] == (30 if case == 'regular_limit' else 1 if case == 'success' else 0)
    assert result['level'] == ('critical' if case in {'abnormal_limit','post_failure'} else 'warning' if case in {'regular_limit','calculation'} else 'info')
    assert result['daily_regular_count'] == saved['regular_count']
    assert result['daily_abnormal_count'] == saved['abnormal_count']


def test_success_cooldown_across_rounds_then_allows_maintenance(tmp_path):
    """连续轮次按上次实际成功时间跳过，十五分钟后恢复常规维护。"""
    from datetime import timedelta
    client = AsyncMock()
    configure_account(client)
    client.get_isolated_allocation.side_effect = [snapshot(),snapshot('.081'),snapshot(),snapshot(),snapshot('.081')]
    now = datetime(2026,9,7,tzinfo=timezone.utc)
    results = []
    for minutes in (0,5,15):
        results.append(asyncio.run(guard._maintain_isolated_allocation(
            client,underlying='XAUS',mode=guard.MarginModeStatus('isolated','测试'),dry_run=False,
            observed_at=now+timedelta(minutes=minutes),ledger_path=tmp_path/'count.json',audit_path=tmp_path/'audit.jsonl')))
    assert client.set_isolated_allocation.await_count == 2
    assert [r['daily_regular_count'] for r in results] == [1,1,2]
    assert [r['daily_abnormal_count'] for r in results] == [0,0,0]


def test_legacy_ten_successes_do_not_block_new_maintenance(tmp_path):
    """旧版十次混合台账迁移后不能继续误伤正常维护。"""
    client = AsyncMock()
    configure_account(client)
    client.get_isolated_allocation.side_effect = [snapshot(),snapshot('.081')]
    ledger = tmp_path/'count.json'
    ledger.write_text(json.dumps({'date':'2026-09-07','attempts':10}))
    result = asyncio.run(guard._maintain_isolated_allocation(
        client,underlying='XAUS',mode=guard.MarginModeStatus('isolated','测试'),dry_run=False,
        observed_at=datetime(2026,9,7,tzinfo=timezone.utc),ledger_path=ledger,audit_path=tmp_path/'audit.jsonl'))
    client.set_isolated_allocation.assert_awaited_once()
    assert result['daily_regular_count'] == 11
    assert result['daily_abnormal_count'] == 0


def test_invalid_maintenance_counts_as_calculation_error(tmp_path):
    """维持保证金大于名义属于非法输入，不能无限次计算重试。"""
    client = AsyncMock()
    configure_account(client)
    client.get_isolated_allocation.return_value = {**snapshot(), 'maintenance_margin':D('3000')}
    ledger = tmp_path/'count.json'
    result = asyncio.run(guard._maintain_isolated_allocation(
        client,underlying='XAUS',mode=guard.MarginModeStatus('isolated','测试'),dry_run=False,
        observed_at=datetime(2026,9,7,tzinfo=timezone.utc),ledger_path=ledger,audit_path=tmp_path/'audit.jsonl'))
    client.set_isolated_allocation.assert_not_awaited()
    assert result['daily_abnormal_count'] == 1


def test_success_ledger_write_failure_does_not_repeat_or_corrupt_counts(tmp_path,monkeypatch):
    """成功回读后的台账写失败不能被当成暂时读仓失败而反复减异常额度。"""
    client = AsyncMock()
    configure_account(client)
    client.get_isolated_allocation.side_effect = [snapshot()] + [snapshot('.081')] * guard.execution._FLAT_TRIES
    original = guard._write_json
    writes = []
    def write(path, payload):
        writes.append(payload.copy())
        if len(writes) >= 2:
            raise OSError('磁盘写入失败')
        original(path,payload)
    monkeypatch.setattr(guard,'_write_json',write)
    ledger = tmp_path/'count.json'
    result = asyncio.run(guard._maintain_isolated_allocation(
        client,underlying='XAUS',mode=guard.MarginModeStatus('isolated','测试'),dry_run=False,
        observed_at=datetime(2026,9,7,tzinfo=timezone.utc),ledger_path=ledger,audit_path=tmp_path/'audit.jsonl'))
    assert len(writes) == 2
    assert result['level'] == 'critical'
    assert result['daily_abnormal_count'] == 1
    assert json.loads(ledger.read_text())['abnormal_count'] == 1
    client.set_isolated_allocation.assert_awaited_once()


@pytest.mark.parametrize('qty,liquidation', [('1', '4072.19'), ('-1', '4726.27')])
def test_actual_bucket_from_real_position_schema(qty, liquidation):
    """IM 固定为名义的 10%，真实桶必须从强平价反推。"""
    client = object.__new__(VariationalClient)
    client.get_positions = AsyncMock(return_value=[{
        'position_info': {'instrument': {'underlying': 'XAUS'},
                          'qty': str(D(qty) * D('1989.51') / D('4399.23'))},
        'price_info': {'underlying_price': '4399.23'},
        'initial_margin': '198.95', 'maintenance_margin': '99.48',
        'estimated_liquidation_price': liquidation,
    }])
    result = asyncio.run(client.get_isolated_allocation('XAUS'))
    assert abs(result['current_allocation'] - D('239.98')) < D('.01')
    assert result['distance'] == D('327.04') / D('4399.23')


@pytest.mark.parametrize('healthy', [True, False])
def test_fixed_im_does_not_hide_allocation_success(tmp_path, healthy):
    """完整真实字段读仓链路中，追加 allocation 不改变公式 IM。"""
    client = object.__new__(VariationalClient)
    before = {'position_info': {'instrument': {'underlying': 'XAUS'}, 'qty': '1'},
              'price_info': {'underlying_price': '2000'}, 'initial_margin': '200',
              'maintenance_margin': '100', 'estimated_liquidation_price': '1900'}
    after = {**before, 'estimated_liquidation_price': '1838'}
    client.get_positions = AsyncMock(side_effect=[[after]] if healthy else [[before], [before], [after]])
    client.raw = AsyncMock(return_value={'balance': '1000', 'upnl': '0'})
    client.set_isolated_allocation = AsyncMock(return_value='3a63f279-test')
    client.wait_allocation_conversion = AsyncMock(return_value='confirmed')
    result = asyncio.run(guard._maintain_isolated_allocation(
        client, underlying='XAUS', mode=guard.MarginModeStatus('isolated', '测试'),
        dry_run=False, observed_at=datetime(2026,9,7,tzinfo=timezone.utc),
        ledger_path=tmp_path/'count.json', audit_path=tmp_path/'audit.jsonl'))
    assert result['current_allocation'] == D('253.9')
    assert result['distance'] == D('.081')
    assert result['daily_abnormal_count'] == 0
    assert result['daily_regular_count'] == (0 if healthy else 1)
    assert client.set_isolated_allocation.await_count == (0 if healthy else 1)


def test_migrate_and_reset_allocation_state(tmp_path, monkeypatch):
    """重置命令只处理本地台账，保留原始计数供审计。"""
    state = tmp_path/'swap_carry_guard_state.json'
    legacy = tmp_path/'swap_carry_guard_state.json.allocation.json'
    saved = {'date':'2026-09-07', 'attempts':14, 'regular_count':10, 'abnormal_count':4}
    legacy.write_text(json.dumps(saved))
    canonical = guard._allocation_state_path(state)
    assert canonical.name == 'swap_carry_guard_allocation_state.json'
    assert json.loads(canonical.read_text()) == saved
    assert not legacy.exists()
    # 新文件优先，重复启动不能用旧计数覆盖新状态。
    legacy.write_text(json.dumps({**saved, 'attempts':99}))
    assert guard._allocation_state_path(state) == canonical
    assert json.loads(canonical.read_text()) == saved
    load = AsyncMock()
    monkeypatch.setattr(guard.execution, '_load', load)
    args = guard.build_parser().parse_args(['--state', str(state), '--reset-allocation-counters'])
    assert asyncio.run(guard._main(args)) == 0
    reset = json.loads(canonical.read_text())
    assert reset['abnormal_count'] == 0
    assert reset['regular_count'] == 10
    assert reset['attempts'] == 14
    assert reset['counter_reset']['previous_abnormal_count'] == 4
    load.assert_not_awaited()


@pytest.mark.parametrize('dry_run', [False, True])
def test_reset_respects_lock_and_dry_run(tmp_path, monkeypatch, dry_run):
    """dry-run 不改计数；实际重置遇到占锁必须失败，不能覆盖活跃轮次。"""
    import fcntl
    state = tmp_path/'guard_state.json'
    ledger = guard._allocation_state_path(state)
    saved = {'date':'2026-09-07', 'attempts':14, 'regular_count':10, 'abnormal_count':4}
    ledger.write_text(json.dumps(saved))
    load = AsyncMock()
    monkeypatch.setattr(guard.execution, '_load', load)
    args = guard.build_parser().parse_args([
        '--state', str(state), '--reset-allocation-counters', *(['--dry-run'] if dry_run else [])])
    with ledger.with_suffix('.json.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if dry_run:
            assert asyncio.run(guard._main(args)) == 0
        else:
            with pytest.raises(BlockingIOError):
                asyncio.run(guard._main(args))
    assert json.loads(ledger.read_text()) == saved
    load.assert_not_awaited()


def test_migration_refuses_active_legacy_lock(tmp_path):
    """旧守护仍在维护时，迁移不跨过旧锁。"""
    import fcntl
    state = tmp_path/'guard_state.json'
    legacy = tmp_path/'guard_state.json.allocation.json'
    legacy.write_text('{"date":"2026-09-07","attempts":14}')
    with legacy.with_suffix('.json.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            guard._allocation_state_path(state)
    assert legacy.exists()
    assert not (tmp_path/'guard_allocation_state.json').exists()


@pytest.mark.parametrize('status', ['rejected', 'timeout', 'confirmed'])
def test_conversion_confirmation_before_readback(tmp_path, monkeypatch, caplog, status):
    """真实持仓字段复现：POST 后仍旧，确认后才允许读取更新的强平价。"""
    client = object.__new__(VariationalClient)
    events = []
    before = {'position_info': {'instrument': {'underlying': 'XAUS'}, 'qty': '1'},
              'price_info': {'underlying_price': '2000'}, 'initial_margin': '200',
              'maintenance_margin': '100', 'estimated_liquidation_price': '1900'}
    confirmed = False
    reads_after = 0

    async def get(path):
        nonlocal confirmed, reads_after
        events.append(path)
        if path == '/portfolio':
            return {'balance': '1000', 'upnl': '0'}
        if path.startswith('/sub_accounts/conversions/'):
            if events.count(path) == 1:
                return {'status': 'pending'}
            if status == 'timeout':
                raise TimeoutError('转换确认超时')
            confirmed = status == 'confirmed'
            return {'status': status}
        assert path == '/positions'
        if confirmed:
            reads_after += 1
        # 确认后的第一次读仓仍可旧，第二次才更新。
        return [{**before, 'estimated_liquidation_price': '1838' if reads_after >= 2 else '1900'}]

    async def post(path, payload):
        events.append(path)
        return {'conversion_id': '3a63f279-test'}

    client._get = AsyncMock(side_effect=get)
    client._post = AsyncMock(side_effect=post)
    client._instrument_kind_from_metadata = AsyncMock(return_value=('swap', 'commodity'))
    monkeypatch.setenv('ALLOCATION_CONVERSION_TIMEOUT_SECONDS', '7')
    caplog.set_level('INFO', logger=guard.__name__)
    result = asyncio.run(guard._maintain_isolated_allocation(
        client, underlying='XAUS', mode=guard.MarginModeStatus('isolated', '测试'),
        dry_run=False, observed_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        ledger_path=tmp_path/'count.json', audit_path=tmp_path/'audit.jsonl'))
    client._post.assert_awaited_once()
    assert result['conversion_id'] == '3a63f279-test'
    posted = events.index('/sub_accounts/allocation')
    assert events[posted + 1:posted + 3] == ['/sub_accounts/conversions/3a63f279-test'] * 2
    ledger = json.loads((tmp_path/'count.json').read_text())
    assert result['daily_abnormal_count'] == ledger['abnormal_count'] == (1 if status == 'rejected' else 0)
    if status == 'confirmed':
        assert result['level'] == 'info'
        assert result['distance'] == D('.081')
        assert result['readback_attempts'] == 2
        assert ledger['regular_count'] == 1
    else:
        assert reads_after == 0
        assert '/positions' not in events[posted + 1:]
        assert result['level'] == ('critical' if status == 'rejected' else 'warning')
        assert ('POST 失败' if status == 'rejected' else '超时') in result['message']
        if status == 'timeout':
            assert any(r.levelname == 'WARNING' and '超时' in r.message for r in caplog.records)


def test_set_allocation_returns_conversion_id():
    client = object.__new__(VariationalClient)
    client._instrument_kind_from_metadata = AsyncMock(return_value=('swap', 'commodity'))
    client._post = AsyncMock(return_value={'conversion_id': 'c0a91108-test'})
    assert asyncio.run(client.set_isolated_allocation('XAUS', D('300'))) == 'c0a91108-test'


@pytest.mark.parametrize('terminal', ['confirmed', 'rejected'])
def test_wait_conversion_polls_terminal_status(monkeypatch, terminal):
    client = object.__new__(VariationalClient)
    client._get = AsyncMock(side_effect=[{'status': 'pending'}, {'status': terminal}])
    monkeypatch.setenv('ALLOCATION_CONVERSION_POLL_INTERVAL_SECONDS', '0.25')
    assert asyncio.run(client.wait_allocation_conversion('c0a91108-test', timeout=30)) == terminal
    assert client._get.await_count == 2
    client._get.assert_awaited_with('/sub_accounts/conversions/c0a91108-test')
    guard.asyncio.sleep.assert_awaited_once_with(0.25)


def test_wait_conversion_bounds_stalled_request():
    client = object.__new__(VariationalClient)
    async def stalled(path):
        await asyncio.Event().wait()
    client._get = AsyncMock(side_effect=stalled)
    with pytest.raises(TimeoutError):
        asyncio.run(client.wait_allocation_conversion('c0a91108-test', timeout=0.01))


def test_wait_conversion_pending_hits_total_deadline(monkeypatch):
    """持续 pending 也必须在总截止时间停止，不能只约束单次 GET。"""
    client = object.__new__(VariationalClient)
    client._get = AsyncMock(return_value={'status': 'pending'})
    # 用事件循环定时器提供离线等待，避免全局免等待夹具吞掉时钟推进。
    async def pause(delay):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        handle = loop.call_later(delay, future.set_result, None)
        try:
            await future
        finally:
            handle.cancel()
    monkeypatch.setattr(guard.asyncio, 'sleep', pause)
    monkeypatch.setenv('ALLOCATION_CONVERSION_POLL_INTERVAL_SECONDS', '0.001')
    with pytest.raises(TimeoutError):
        asyncio.run(client.wait_allocation_conversion('c0a91108-test', timeout=0.02))
    assert client._get.await_count > 1


def test_guard_forwards_configured_conversion_timeout(tmp_path, monkeypatch):
    client = AsyncMock()
    configure_account(client)
    client.get_isolated_allocation.side_effect = [snapshot(), snapshot('.081')]
    monkeypatch.setenv('ALLOCATION_CONVERSION_TIMEOUT_SECONDS', '7')
    result = asyncio.run(guard._maintain_isolated_allocation(
        client, underlying='XAUS', mode=guard.MarginModeStatus('isolated', '测试'),
        dry_run=False, observed_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        ledger_path=tmp_path/'count.json', audit_path=tmp_path/'audit.jsonl'))
    client.wait_allocation_conversion.assert_awaited_once_with('3a63f279-test', timeout=7.0)
    assert result['daily_abnormal_count'] == 0
