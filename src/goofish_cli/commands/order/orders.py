"""Create unpaid orders through the site's UI; never invoke payment."""
import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from goofish_cli.core import Session, Strategy, command
from goofish_cli.core.browser import goofish_page
from goofish_cli.core.errors import GoofishError
from goofish_cli.core.local_state import locked_state
from goofish_cli.core.write_operation import write_operation

STATE = Path.home() / '.goofish-cli' / 'unpaid-orders.json'
ADDRESS = '[class^="address--"]'
OPTION = '[class^="option--"]'


def token(value):
    return hashlib.sha256(value.encode()).hexdigest()


def money(value):
    try:
        amount = Decimal(str(value).replace('¥', '').replace('￥', '').replace(',', '').strip())
    except InvalidOperation:
        raise GoofishError('金额格式无效') from None
    if not amount.is_finite() or amount < 0:
        raise GoofishError('金额必须为非负有限数')
    return amount


def validate(item_id, quantity):
    if not re.fullmatch(r'\d+', item_id) or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise GoofishError('需要数字商品 ID 和正整数数量')


def success(raw):
    if not any(str(v).startswith('SUCCESS::') for v in raw.get('ret', [])):
        raise GoofishError('订单接口未成功；请检查登录、库存或风控')
    return raw.get('data') or {}


def components(data):
    return {c['render']: c.get('data', {}) for c in data.get('components', [])}


def summary(data, item_id):
    parts = components(data)
    item = parts.get('itemDescVO', {})
    info = (data.get('commonData', {}).get('itemBuyInfo') or [{}])[0]
    if str(info.get('itemId')) != item_id:
        raise GoofishError('订单商品身份不匹配，未提交')
    total = money(data.get('bottomBarVO', {}).get('totalPrice', ''))
    if str(info.get('disable', '')).lower() == 'true' or str(item.get('disable', '')).lower() == 'true':
        raise GoofishError('商品不可购买，未提交')
    return {'item_id': item_id, 'title': item.get('title', ''),
            'sku_id': str(item.get('skuId', '')), 'quantity': int(item.get('buyQuantity', 0)),
            'total_price': str(total), 'post_price': parts.get('postFeeVO', {}).get('postFee'),
            'currency': 'CNY'}


def is_payment(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    return (host == 'alipay.com' or host.endswith('.alipay.com')
            or bool(re.search(r'/mtop\.order\.dopay/', parsed.path, re.I)))


async def payment_guard(route):
    if is_payment(route.request.url):
        await route.abort()
    else:
        await route.continue_()


@asynccontextmanager
async def unpaid_page():
    async with goofish_page() as shared:
        page = await shared.context.new_page()
        try:
            # Page-local routing leaves the sandbox's persistent page untouched.
            await page.route('**/*', payment_guard)
            yield page
        finally:
            # Closing the temporary page stops payment timers and removes its routes.
            await page.close()


async def render_after(page, action):
    async with page.expect_response(lambda r: '/mtop.taobao.idle.trade.order.render/' in r.url, timeout=45000) as pending:
        await action()
    return success(await (await pending.value).json())


async def open_order(page, item_id):
    return await render_after(page, lambda: page.goto('https://www.goofish.com/create-order?itemId=' + item_id))


def sku_choices(data):
    sku = components(data).get('skuSelectorVO', {})
    groups = sku.get('specificationInfoList', [])
    if len(groups) != 1:
        raise GoofishError('当前仅支持单规格组商品，请人工处理')
    details = sku.get('skuInfoDetails', {})
    result = []
    for spec in groups[0].get('specificationList', []):
        detail = details.get(spec['id'], {})
        result.append({'sku_id': str(detail.get('skuId', '')), 'label': spec['value'],
                       'available': detail.get('soldOut') == 'false' and int(detail.get('quantity', 0)) > 0,
                       'inventory': int(detail.get('quantity', 0)), 'price': detail.get('skuPrice')})
    return result


async def choices(page, data):
    addresses = []
    for node in await page.locator(ADDRESS).all():
        text = (await node.inner_text()).strip()
        region = text.splitlines()[0].split()
        phone = re.search(r'(?<!\d)1\d{10}(?!\d)', text)
        addresses.append({'address_id': token(text),
                          'region': ' '.join(region[:3]) if len(region) >= 3 else '地址 ' + str(len(addresses) + 1),
                          'phone_hint': '*******' + phone[0][-4:] if phone else '',
                          'selected': '245, 131, 0' in (await node.get_attribute('style') or '')})
    return sku_choices(data), addresses


async def select(page, selector, wanted):
    for node in await page.locator(selector).all():
        if token((await node.inner_text()).strip()) == wanted:
            # Already-selected controls do not trigger another render.
            style = await node.get_attribute('style') or ''
            selected = ('255, 230, 15' in style if selector == OPTION else '245, 131, 0' in style)
            if selected:
                return None
            return await render_after(page, node.click)
    raise GoofishError('预览选项已失效，请重新预览，未提交')


@command(namespace='order', name='preview', strategy=Strategy.COOKIE,
         description='只读预览订单规格、脱敏地址与费用。不下单。创建前须明确规格、地址、数量和总价上限；不得自行选择。')
def preview(item_id: str) -> dict:
    validate(item_id, 1)
    return asyncio.run(_preview(item_id))


async def _preview(item_id):
    async with goofish_page() as page:
        data = await open_order(page, item_id)
        await page.locator(ADDRESS).first.wait_for()
        options, addresses = await choices(page, data)
        return {**summary(data, item_id), 'specifications': options, 'addresses': addresses,
                'notice': '明确选择 sku_id 与 address_id；仅创建待付款订单，不付款。'}


@command(namespace='order', name='create-unpaid', strategy=Strategy.COOKIE, write=True,
         description='按预览 sku_id/address_id 创建待付款订单，禁止付款。必须有用户明确授权；使用固定 request_id，结果待核实时不得换 ID 重提。')
def create_unpaid(item_id: str, sku_id: str, address_id: str, quantity: int,
                  max_total_price: str, request_id: str) -> dict:
    validate(item_id, quantity)
    limit = money(max_total_price)
    if not sku_id or not address_id or not request_id.strip():
        raise GoofishError('需要预览规格、地址标识和 request_id')
    session = Session.load()
    key = token(session.unb + ':' + request_id)
    fingerprint = token(json.dumps([item_id, sku_id, address_id, quantity, str(limit)]))
    with locked_state(STATE) as state:
        if key in state:
            if state[key]['fingerprint'] != fingerprint:
                raise GoofishError('request_id 已用于不同参数')
            return state[key]['result']
        # Persist before any possible submission, including process crashes.
        state[key] = {'fingerprint': fingerprint, 'result': {'status': 'needs_review',
                       'request_id': request_id, 'message': '结果待核实，禁止自动重复提交'}}
    try:
        with write_operation(session, 'order.write'):
            result = asyncio.run(_create(item_id, sku_id, address_id, quantity, limit, key))
    except Exception:
        raise GoofishError('订单未完成或结果待核实；请查询我买到的，禁止自动重提') from None
    with locked_state(STATE) as state:
        state[key]['result'] = result
    return result


async def _create(item_id, sku_id, address_id, quantity, limit, key):
    async with unpaid_page() as page:
        data = await open_order(page, item_id)
        await page.locator(ADDRESS).first.wait_for()
        selected = next((s for s in sku_choices(data) if s['sku_id'] == sku_id), None)
        if not selected or not selected['available'] or selected['inventory'] < quantity:
            raise GoofishError('规格失效或库存不足，未提交')
        data = await select(page, OPTION, token(selected['label'])) or data
        data = await select(page, ADDRESS, address_id) or data
        field = page.get_by_role('spinbutton')
        if await field.count() == 0:
            field = page.locator('[class^="quantity--"] input')
        if int(await field.input_value()) != quantity:
            async def change_quantity():
                await field.fill(str(quantity))
                await field.press('Tab')
            data = await render_after(page, change_quantity)
        expected = summary(data, item_id)
        if expected['sku_id'] != sku_id or expected['quantity'] != quantity or money(expected['total_price']) > limit:
            raise GoofishError('数量不匹配或总价超限，未提交')
        async with page.expect_response(lambda r: '/mtop.taobao.idle.trade.order.create/' in r.url, timeout=45000) as pending:
            await page.get_by_text('确认购买', exact=True).click()
        created = success(await (await pending.value).json())
        order_id = created.get('bizOrderIdStr')
        if not isinstance(order_id, str) or not order_id.isdigit():
            raise GoofishError('创建响应缺少有效订单号，结果待核实')
        result = {**expected, 'order_id': order_id, 'status': 'created_unverified',
                  'order_url': 'https://www.goofish.com/order-detail?orderId=' + order_id}
        # Keep the receipt even if subsequent navigation/status verification fails.
        with locked_state(STATE) as state:
            state[key]['result'] = result
        try:
            await page.goto(result['order_url'])
            await page.get_by_text('等待买家付款', exact=False).first.wait_for(timeout=15000)
            result['status'] = 'unpaid'
        except Exception:
            pass
        return result
