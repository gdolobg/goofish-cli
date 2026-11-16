from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from goofish_cli.commands.order import orders as mod
from goofish_cli.core.errors import GoofishError


@pytest.mark.parametrize('url', [
    'https://h5api.m.goofish.com/h5/mtop.order.dopay/4.0/?data=x',
    'https://h5api.m.goofish.com/h5/mtop.order.doPay/4.0/',
    'https://tbapi.alipay.com/trade/pay', 'https://alipay.com/',
])
def test_payment_is_blocked(url):
    assert mod.is_payment(url)


@pytest.mark.parametrize('url', [
    'https://h5api.m.goofish.com/h5/mtop.taobao.idle.trade.order.create/5.0/',
    'https://www.goofish.com/order-detail?orderId=123',
    'https://h5api.m.goofish.com/h5/mtop.idle.trade.pay.info.query/1.0/',
])
def test_non_payment_requests_are_allowed(url):
    assert not mod.is_payment(url)


@pytest.mark.asyncio
async def test_guard_aborts_payment_without_continuing():
    route = AsyncMock()
    route.request.url = 'https://tbapi.alipay.com/trade/pay'
    await mod.payment_guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', 'no price'])
def test_invalid_price_fails_closed(value):
    with pytest.raises(GoofishError):
        mod.money(value)


def data(item_id='123', total='55.00'):
    return {'commonData': {'itemBuyInfo': [{'itemId': item_id}]},
            'bottomBarVO': {'totalPrice': total}, 'components': [
                {'render': 'itemDescVO', 'data': {'title': 'keyboard', 'buyQuantity': '1', 'skuId': 'sku'}},
                {'render': 'postFeeVO', 'data': {'postFee': '0.00'}}]}


def test_summary_keeps_decimal_and_checks_identity():
    assert mod.summary(data(), '123')['total_price'] == '55.00'
    with pytest.raises(GoofishError, match='身份'):
        mod.summary(data(), '999')


def test_sku_inventory_overrides_selectable_flag():
    raw = {'components': [{'render': 'skuSelectorVO', 'data': {
        'specificationInfoList': [{'specificationList': [
            {'id': 'path', 'value': 'red', 'canSelected': 'true'}]}],
        'skuInfoDetails': {'path': {'skuId': 'sku', 'quantity': '0', 'soldOut': 'true', 'skuPrice': '55'}}}}]}
    assert mod.sku_choices(raw) == [{'sku_id': 'sku', 'label': 'red', 'inventory': 0,
                                    'available': False, 'price': '55'}]


@pytest.mark.parametrize('quantity', [0, -1, True, 1.5])
def test_quantity_requires_positive_integer(quantity):
    with pytest.raises(GoofishError):
        mod.validate('123', quantity)


def test_idempotent_retry_does_not_send(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'STATE', tmp_path / 'orders.json')
    monkeypatch.setattr(mod.Session, 'load', lambda: type('Session', (), {'unb': 'me'})())
    result = {'order_id': '789', 'status': 'unpaid'}
    fingerprint = mod.token(mod.json.dumps(['123', 'sku', 'address', 1, str(Decimal('55'))]))
    with mod.locked_state(mod.STATE) as state:
        state[mod.token('me:req')] = {'fingerprint': fingerprint, 'result': result}
    assert mod.create_unpaid('123', 'sku', 'address', 1, '55', 'req') == result
    with pytest.raises(GoofishError, match='不同参数'):
        mod.create_unpaid('123', 'sku', 'address', 1, '56', 'req')


def test_unknown_retry_does_not_send(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'STATE', tmp_path / 'orders.json')
    monkeypatch.setattr(mod.Session, 'load', lambda: type('Session', (), {'unb': 'me'})())
    async def crash(*args):
        raise TimeoutError()
    from contextlib import nullcontext
    monkeypatch.setattr(mod, 'write_operation', lambda *args: nullcontext())
    monkeypatch.setattr(mod, '_create', crash)
    with pytest.raises(GoofishError, match='待核实'):
        mod.create_unpaid('123', 'sku', 'address', 1, '55', 'req')
    assert mod.create_unpaid('123', 'sku', 'address', 1, '55', 'req')['status'] == 'needs_review'


@pytest.mark.asyncio
@pytest.mark.parametrize('total,sku,expected', [('55', 'sku', 'unpaid'), ('56', 'sku', 'error'), ('55', 'other', 'error')])
async def test_create_guards_and_receipt(tmp_path, monkeypatch, total, sku, expected):
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock
    monkeypatch.setattr(mod, 'STATE', tmp_path / 'state.json')
    with mod.locked_state(mod.STATE) as state:
        state['key'] = {'result': {}}
    page = MagicMock()
    page.context.route = AsyncMock()
    page.context.new_page = AsyncMock(return_value=page)
    page.route = AsyncMock()
    page.close = AsyncMock()
    page.goto = AsyncMock()
    node = MagicMock()
    node.wait_for = AsyncMock()
    node.input_value = AsyncMock(return_value='1')
    node.count = AsyncMock(return_value=1)
    node.click = AsyncMock()
    node.first = node
    page.locator.return_value = node
    page.get_by_role.return_value = node
    page.get_by_text.return_value = node
    current = data(total=total)
    current['components'][0]['data']['skuId'] = sku
    monkeypatch.setattr(mod, 'open_order', AsyncMock(return_value=current))
    monkeypatch.setattr(mod, 'select', AsyncMock(return_value=None))
    monkeypatch.setattr(mod, 'sku_choices', lambda d: [{'sku_id': 'sku', 'available': True, 'inventory': 2, 'label': 'red'}])
    @asynccontextmanager
    async def browser():
        yield page
    monkeypatch.setattr(mod, 'goofish_page', browser)
    response = AsyncMock()
    response.json.return_value = {'ret': ['SUCCESS::ok'], 'data': {'bizOrderIdStr': '789', 'payUrl': 'secret'}}
    pending = MagicMock()
    pending.value = asyncio.get_running_loop().create_future()
    pending.value.set_result(response)
    @asynccontextmanager
    async def expect(*args, **kwargs):
        yield pending
    page.expect_response.side_effect = expect
    if expected == 'error':
        with pytest.raises(GoofishError):
            await mod._create('123', 'sku', 'address', 1, Decimal('55'), 'key')
        node.click.assert_not_awaited()
    else:
        result = await mod._create('123', 'sku', 'address', 1, Decimal('55'), 'key')
        assert result['status'] == 'unpaid'
        assert result['order_id'] == '789'
        assert 'payUrl' not in result
        node.click.assert_awaited_once()
    page.route.assert_awaited_once_with('**/*', mod.payment_guard)
    page.context.route.assert_not_awaited()
    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_orders_keep_shared_page_and_close_on_cancel(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock
    shared = MagicMock()
    shared.close = AsyncMock()
    pages = [MagicMock(), MagicMock()]
    for page in pages:
        page.route = AsyncMock()
        page.close = AsyncMock()
    shared.context.new_page = AsyncMock(side_effect=pages)
    @asynccontextmanager
    async def browser():
        yield shared
    monkeypatch.setattr(mod, 'goofish_page', browser)
    async with mod.unpaid_page() as first:
        assert first is pages[0]
    with pytest.raises(asyncio.CancelledError):
        async with mod.unpaid_page() as second:
            assert second is pages[1]
            raise asyncio.CancelledError()
    for page in pages:
        page.close.assert_awaited_once()
    shared.close.assert_not_awaited()
