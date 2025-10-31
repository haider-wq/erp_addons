#  See LICENSE file for full copyright and licensing details.

import json
import logging
from typing import List, Dict

import requests

from odoo import _
from odoo.exceptions import UserError, ValidationError

from .tools import catch_exception
from .exceptions import ShopifyApiException

_logger = logging.getLogger(__name__)

try:
    from shopify import (
        Session,
        AccessScope,
        ShopifyResource,
        Country,
        Image,
        Shop,
        Order,
        Product,
        Variant,
        FulfillmentOrders,
        Collect,
        CustomCollection,
        InventoryItem,
        InventoryLevel,
        Webhook,
        Customer,
        Transaction,
        Metafield,
        Location,
        OrderRisk,
    )
    from shopify.resources.fulfillment import FulfillmentV2
except (ImportError, IOError) as ex:
    _logger.error(ex)


SHOP = 'shop'
ORDER = 'order'
TEMPLATE = 'product'
VARIANT = 'variant'
IMAGE = 'image'
COUNTRY = 'country'
FULFILLMENT = 'fulfillment'
FULFILLMENT_ORDER = 'fulfillment_order'
COLLECT = 'collect'
CATEGORY = 'category'
INVENT_ITEM = 'inventory_item'
INVENT_LEVEL = 'inventory_level'
ACCESS_SCOPE = 'access_scope'
WEBHOOK = 'webhook'
CUSTOMER = 'customer'
TRANSACTION = 'transaction'
METAFIELD = 'metafield'
LOCATION = 'location'
ORDER_RISK = 'order_risk'

SHOPIFY_FETCH_LIMIT = 250


class CurrentShop(Shop):

    _singular = 'shop'
    _plural = 'shops'

    @catch_exception
    def _fetch_current(self):
        return self.__class__.current()


class WebhookPatch(Webhook):

    _singular = 'webhook'
    _plural = 'webhooks'

    @catch_exception
    def save(self):
        """
        Redefined method because of we need to POST `webhook` in other way than just a common
        post-request. I don't know why it's not implemented in the ShopifyAPI Lib.
        """
        if not self.is_new():
            return super(WebhookPatch, self).save()

        url = self._build_post_url()
        headers = self._build_post_headers()
        data = {
            'webhook': self.to_dict(),
        }
        response = self._send_request('POST', url, headers=headers, data=data)

        self._update(self.__class__.format.decode(response.content))
        return self

    def _send_request(self, method, url, params=None, headers=None, data=None):
        _logger.debug('%s %s %s %s', method, url, params, data)

        response = requests.request(
            method,
            url,
            params=params,
            json=data,
            headers=headers,
        )

        self._check_response(response)
        return response

    def _build_post_url(self):
        return f'{self._site}/webhooks.json'

    def _build_post_headers(self):
        headers = self.klass.headers
        headers['Content-Type'] = 'application/json'
        return headers

    def _check_response(self, response):
        if not response.ok:
            raise ShopifyApiException(response.text)


class CustomCollectionPatch(CustomCollection):

    _singular = 'custom_collection'
    _plural = 'custom_collections'

    @catch_exception
    def add_product(self, product):
        return super(CustomCollectionPatch, self).add_product(product)

    @catch_exception
    def remove_product(self, product):
        return super(CustomCollectionPatch, self).remove_product(product)


class FulfillmentOrdersPatch(FulfillmentOrders):

    _singular = 'fulfillment_order'
    _plural = 'fulfillment_orders'

    @catch_exception
    def move(self, external_location_id: int, line_items: List[Dict]):

        body = {
            'fulfillment_order': {
                'new_location_id': external_location_id,
                'fulfillment_order_line_items': line_items,
            }
        }

        response = self.klass.connection.post(
            f'{self._site}/fulfillment_orders/{self.id}/move.json',
            self.klass.headers,
            json.dumps(body).encode(),
        )

        body = self.klass.format.decode(response.body)

        original_order = self.klass(prefix_options={'order_id': self.order_id})
        original_order._update(body['original_fulfillment_order'])

        moved_order = self.klass(prefix_options={'order_id': self.order_id})
        moved_order._update(body['moved_fulfillment_order'])

        return original_order, moved_order

    def _prepare_pending_lines(self):
        lines = filter(lambda x: x.quantity and x.fulfillable_quantity, self.line_items)
        return [dict(id=x.id, quantity=x.fulfillable_quantity) for x in lines]

    def _prepare_pending_line(self, line_item_id: int, qty: int):
        pending_qty = self._get_pending_qty(line_item_id)

        if not pending_qty:
            return dict()

        if qty > pending_qty:
            qty = pending_qty

        return {
            'quantity': qty,
            'id': self._get_id_by_order_line(line_item_id),
        }

    def _get_pending_line_ids(self):
        lines = filter(lambda x: x.quantity and x.fulfillable_quantity, self.line_items)
        return [x.line_item_id for x in lines]

    def _get_pending_qty(self, line_item_id: int):
        line = self._get_line(line_item_id)
        return line.fulfillable_quantity if line else int()

    def _get_id_by_order_line(self, line_item_id: int):
        line = self._get_line(line_item_id)
        return line.id if line else line

    def _get_line(self, line_item_id: int):
        lines = list(
            filter(lambda x: x.quantity and x.line_item_id == line_item_id, self.line_items)
        )
        return lines[0] if lines else False


class Client:

    classes = {
        SHOP: CurrentShop,
        ORDER: Order,
        TEMPLATE: Product,
        VARIANT: Variant,
        IMAGE: Image,
        COUNTRY: Country,
        FULFILLMENT: FulfillmentV2,
        FULFILLMENT_ORDER: FulfillmentOrdersPatch,
        COLLECT: Collect,
        CATEGORY: CustomCollectionPatch,
        INVENT_ITEM: InventoryItem,
        INVENT_LEVEL: InventoryLevel,
        ACCESS_SCOPE: AccessScope,
        WEBHOOK: WebhookPatch,
        CUSTOMER: Customer,
        TRANSACTION: Transaction,
        METAFIELD: Metafield,
        LOCATION: Location,
        ORDER_RISK: OrderRisk,
    }

    def __init__(self, settings):
        self._session = Session(
            settings['fields']['url']['value'],
            settings['fields']['version']['value'],
            settings['fields']['key']['value'],
        )
        self.activate_session()
        self.api_version = self._session.version.name
        self.shop = self._model_init(SHOP)._fetch_current()

    def __repr__(self):
        return f'<ShopifyClient ({self.shop.name}) at {hex(id(self))}>'

    def deactivate_session(self):
        ShopifyResource.clear_session()

    def activate_session(self):
        ShopifyResource.activate_session(self._session)

    def _model(self, name):
        if name not in self.classes:
            raise UserError(_(
                'Unsupported Shopify client model name: "%s". This is a technical error, and it '
                'must be resolved by a developer. Please contact the support team or your system '
                'administrator for assistance.'
            ) % name)

        return self.classes[name]

    def _model_init(self, name, **kw):
        return self._model(name)(**kw)

    def _get_admin_url(self):
        return f'{self._session.protocol}://{self._session.url}/admin'

    def _get_access_scope(self):
        scopes = self._fetch_multi(ACCESS_SCOPE, None, None, None)
        return [scope.handle for scope in scopes]

    def _get_location_id(self):
        location = self.shop.primary_location_id
        if not location:
            raise ValidationError(_(
                'The primary Shop Location is not specified in your store\'s admin settings. '
                'Please go to your store\'s admin panel and set the primary location for this shop. '
                'This is required to proceed with integration operations.'
            ))

        return location

    def _get_weight_uom(self):
        return self.shop.weight_unit

    @catch_exception
    def _save(self, record):
        result = record.save()

        if not result:
            error = record.errors.errors
            record_json = record.to_json()
            _logger.error('Shopify  external-save-error: %s', error)
            _logger.error('Shopify record: %s', record_json)
            raise ShopifyApiException({'ERROR': error, 'RECORD': record_json})

        return result

    @catch_exception
    def _apply(self, name, *args):
        shopify_cls = self._model(name)
        return shopify_cls.set(*args)

    @catch_exception
    def _destroy(self, record):
        return record.destroy()

    @catch_exception
    def _refresh(self, record):
        return record.reload()

    @catch_exception
    def _fetch_one(self, name, record_id, fields):
        kwargs = dict()
        shopify_cls = self._model(name)

        if fields:
            fields.append('id')
            kwargs['fields'] = ','.join(set(fields))

        return shopify_cls.find(record_id, **kwargs)

    @catch_exception
    def _fetch_multi(self, name, params, fields, quantity):
        """
        Parameters:
            name: ShopifyAPI Resource py-library class-name
            params: dict
            fields: list
            quantity: int

        Important:
            Don't pass to params 'quantity' more than 250.
        """

        if quantity and quantity < SHOPIFY_FETCH_LIMIT:
            limit = quantity
        else:
            limit = SHOPIFY_FETCH_LIMIT

        kwargs = dict(limit=limit, order='id ASC')

        if params:
            kwargs.update(params)

        if fields:
            fields.append('id')
            kwargs['fields'] = ','.join(set(fields))

        shopify_cls = self._model(name)
        records = shopify_cls.find(**kwargs)
        result = list(records)

        if quantity and len(result) <= quantity:
            return result

        while records.next_page_url:
            records = shopify_cls.find(from_=records.next_page_url)
            result.extend(list(records))

            if quantity and len(result) <= quantity:
                break

        return result[:quantity]
