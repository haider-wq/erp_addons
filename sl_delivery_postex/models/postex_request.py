# Part of Odoo. See LICENSE file for full copyright and licensing details.
import json
import logging
import re
import requests
import base64
import math


from datetime import datetime
from markupsafe import Markup
from textwrap import shorten
from urllib.parse import urlencode, quote

from odoo.exceptions import ValidationError, UserError
from odoo.tools.float_utils import float_round
from odoo.tools import format_list

_logger = logging.getLogger(__name__)

SANDBOX_URL = "https://api.postex.pk/"
PROD_URL = "https://api.postex.pk/"
postex_CONTENT_LENGTH_LIMIT = 35
shipengine_uom_const = {"oz": "ounce", "lb": "pound", "g": "gram", "kg": "kilogram"}


class Postex:
    def __init__(self, carrier, prod_environment, debug_logger):
        self.url = PROD_URL
        # if prod_environment else SANDBOX_URL
        self.tracking_url = PROD_URL
        # if prod_environment else SANDBOX_URL
        self.session = requests.Session()
        self.carrier = carrier
        self.debug_logger = debug_logger
        self.token = carrier.postex_production_api_key

    def _make_api_request(self, endpoint, method='GET', data=None, params=None, is_airway_bill=False):
        """ Make an api call, return response for multiple api requests of postex"""

        headers = {
            'Content-Type': "application/json; charset=utf-8",
            'Accept': "application/json",
            'Token': self.token,
        }
            # 'Authorization': f"{self.token}",

        access_url = self.url + endpoint
        try:
            # postex does not handle UTF-8 Strings according to the JSON Spec properly.
            # Requests sent with escaped unicode (\\u) characters will fail on postex's side and not
            # be able to be properly parsed. We need to encode the data with ensure_ascii set to
            # false to send the data with (\x) instead.
            if data:
                data = json.dumps(data, ensure_ascii=False).encode('utf8')

            # Log the request details for debugging purposes
            self.debug_logger("%s\n%s\n%s" % (access_url, method, data), 'postex_request_%s' % endpoint)
            # Make the API request

            response = self.session.request(method=method, url=access_url, data=data, headers=headers, params=params, timeout=30, stream=True)
            if is_airway_bill:
                return response.content
            # Parse the response as JSON
            response_json = response.json()
            # Log the response details for debugging purposes
            self.debug_logger("%s\n%s\n%s" % (response.url, response.status_code, response.text), 'postex_response_%s' % endpoint)
            return response_json
        except requests.exceptions.ConnectionError as error:
            _logger.warning('Connection Error: %s with the given URL: %s', error, access_url)
            return {'error': {'description': 'timeout', 'message': "Cannot reach the server. Please try again later."}}
        except requests.exceptions.ReadTimeout as error:
            _logger.warning('Timeout Error: %s with the given URL: %s', error, access_url)
            return {'error': {'description': 'timeout', 'message': "Cannot reach the server. Please try again later."}}
        except json.decoder.JSONDecodeError as error:
            _logger.warning('JSONDecodeError: %s', error)
            return {'error': {'description': 'JSONDecodeError', 'message': str(error)}}
        except Exception as error:
            _logger.warning('UnknownException: %s', error)
            return {'error': {'description': 'Exception', 'message': str(error)}}



    def _get_rate(self, shipper, recipient, order, order_weight=None):
        """ Fetch rate from postex API based on the parameters.
        url: ship/rate
        """
        data = {
        }
            # 'service_type_id': 1,
            # 'origin_city_id': self._get_postex_city(shipper).get('id'),
            # 'destination_city_id': self._get_postex_city(recipient).get('id'),
            # 'estimated_weight': order_weight,
            # 'shipping_mode_id': int(self.carrier.postex_shipping_mode),
            # 'amount': int(order.amount_total),
        rate_json = self._make_api_request('api/charges_calculate', method="POST", data=data)

        if not rate_json.get('information'):
            # Error message found
            return {'error_found': str(rate_json)}

        services = rate_json.get('information').get('charges')

        return {
            'price': services.get('total_charges')
        }

    def _rate_request(self, recipient, shipper, order, order_weight=False):
        """ Returns the dictionary of shipment rate from postex
        url: ship/rate
        """
        if not order:
            raise UserError(order.env._("Sale Order is required to get rate."))
        products = order.order_line.product_id
        bad_products = products.filtered(lambda prod: not prod.weight and prod.type == 'consu').mapped('name')
        if bad_products:
            product_names = ",".join(bad_products)
            raise ValidationError(order.env._(
                "postex Error: The following products don't have weights set: %(product_names)s",
                product_names=product_names
            ))
        return self._get_rate(shipper, recipient, order, order_weight=order_weight)

    def _fetch_postex_carriers(self, params=None):
        """ Import all available carriers from postex for specific country
        query_url: available-service/{country_code}/{international}/{shipment_type}
        """
        # https: // api - services.postex.pk / api / order / v1 / track - order
        # https: // api.postex.pk / services / integration / api / order / v1 / track - order
        carrier_json = self._make_api_request('services/integration/api/order/v1/track-order/', method="POST", data=params)
        if not carrier_json.get('transactionStatus'):
            status = ""
        else:
            status = carrier_json.get('response')[0].get('status_message') if 'status_message' in carrier_json.get('response')[0] else carrier_json.get('response')
        return status

    def _get_shipping_lines(self, package):
        """ Returns the shipping products from the specific
        picking to create the order.
        """
        line_by_product = []
        original_prices = []
        picking = package.picking_id

        for commodity in package.commodities:
            unit_price = round(commodity.monetary_value, 2)
            # Price of the item must be in the currency you created your postex account with
            unit_price_in_currency = unit_price
            item = {
                'description': shorten(commodity.product_id.name, postex_CONTENT_LENGTH_LIMIT, placeholder="..."),
                'quantity': commodity.qty,
                'price': unit_price_in_currency,
            }
            if commodity.product_id.hs_code:
                # Pass international information if it's necessary
                item |= {
                    'productCode': commodity.product_id.hs_code.replace('.', '') or '',
                    'countryOfManufacture': commodity.country_of_origin or ''
                }
            line_by_product.append(item)
            # Store the original currency price so we don't lose rounding precision for declaredValue
            original_prices.append(unit_price * commodity.qty)

        return line_by_product, original_prices

    def print_air_waybill(self, picking):
        body = {
            'trackingNumbers': picking.carrier_tracking_ref,
        }

        air_waybill_response = self._make_api_request('services/integration/api/order/v1/get-invoice', params=body, is_airway_bill=True)
        return air_waybill_response

    def _get_pickup_address_code(self, partner):
        return '004'
        # response = self._make_api_request('services/integration/api/order/v1/get-merchant-address', params={'cityName': partner})
        # what_is_response = response.get('addressCode')
    def _get_shipping_params(self, picking):
        """ Returns the shipping data from picking for create a postex Order."""
        bad_products = picking.move_line_ids.product_id.filtered(lambda prod: not prod.weight and prod.type == 'consu').mapped('name')
        if bad_products:
            product_names = ",".join(bad_products)
            raise ValidationError(picking.env._(
                "postex Error: The following products don't have weights set: %(product_names)s",
                product_names=product_names
            ))
        picking_data = {
            'orderRefNumber': picking.origin, #need to discuss
            'customerName': picking.partner_id.name,
            'customerPhone': picking.partner_id.phone,
            'deliveryAddress': f"{picking.partner_id.street_number} {picking.partner_id.street} {picking.partner_id.street2} {picking.partner_id.street_name} {picking.partner_id.city} {picking.partner_id.country_id.name}",
            'cityName': self._get_postex_city(picking.partner_id),
            'invoiceDivision': 0,
            'pickupAddressCode': '001',
            'invoicePayment': math.ceil(picking.sale_id.amount_total),
            'items': int(sum(picking.move_ids.mapped('quantity'))),
            'orderType': self.carrier.postex_orderType
        }
            # 'pickupAddressCode': self._get_pickup_address_code(self.carrier.partner_id.city),
        return picking_data

    def _send_shipping(self, picking):
        """ Returns a dictionary containing:
            - Price of the shipment
            - All tracking numbers for each package
            - postex Order Ids for cancelation.
        url(s): ship/generate
        """
        tracking_number = None
        res = {
            'exact_price': 0.00,
            'tracking_number': '',
        }
        picking_data = self._get_shipping_params(picking)

        ship_response = self._make_api_request('services/integration/api/order/v3/create-order', method='POST', data=picking_data)

        if not ship_response.get('dist'):
            raise UserError(str(ship_response))
        if ship_response.get('dist'):
            dist = ship_response.get('dist')
            tracking_number = dist.get('trackingNumber')

        params = {
            'trackingNumber': tracking_number
        }
        tracking_url = "https://merchant.postex.pk/main/order-detail" + f"?{urlencode(params)}"
        formatted_tracking = format_list(picking.env, [tracking_number])

        carrier_tracking_link = Markup("<a href='%s'>%s</a><br/>") % (tracking_url, formatted_tracking)

        # pickings where we should leave a lognote
        lognote_pickings = picking.sale_id.picking_ids if picking.sale_id else picking

        logmessage = Markup(
            "{header}<br/><b>{tracking_header}</b> {tracking_link}<br/>"
        ).format(
            header=picking.env._("Shipment created into postex"),
            tracking_header=picking.env._("Tracking Numbers:"),
            tracking_link=carrier_tracking_link
        )

        for pick in lognote_pickings:
            pick.message_post(body=logmessage)

        res['tracking_number'] = tracking_number
        return res

    def _cancel_picking(self, picking):
        """ Cancel the individual order
        Can end up failing still even if we check that it can be canceled.
        Returns a list of any tracking that failed to cancel.
        url: api/shipment/cancel
        """
        tracking_numbers = picking.carrier_tracking_ref

        invalid_trackings = []
        for tracking_number in tracking_numbers.split(','):
            body = {
                'trackingNumber': tracking_number
            }

            cancel_response = self._make_api_request('services/integration/api/order/v1/cancel-order', method='PUT', data=body)
            invalid_trackings.append(cancel_response)
        return invalid_trackings

    def _get_postex_vat(self, partner):
        """ postex requires the vat of the partner.
        If the partner is a delivery address then the vat is on the parent_id

        In colombia, postex doesn't expect any periods or - in the vat even
        though we store it so we must strip it.
        """
        vat = partner.parent_id.vat if partner.type != 'contact' else partner.vat
        if vat and partner.country_id == partner.env.ref("base.co"):
            vat = vat.split("-", 1)[0]
            vat = re.sub("[^+0-9]", "", vat)
        return vat

    def _get_postex_city(self, partner):
        partner_city = partner.city
        return partner_city

    def _get_postex_street(self, partner):
        street = partner.street_name or partner.street
        return street

    def _get_postex_district(self, partner):
        """ postex requires the city to be sent twice for chile.
        Skip district code and do that instead.

        For Mexico postex requires the colony field if it's set and
        l10n_mx_edi_extended is installed.
        """
        if partner.country_id == partner.env.ref("base.cl"):
            return self._get_postex_city(partner)

        l10n_mx = partner.env['ir.module.module'].search([('name', '=', 'l10n_mx_edi_extended')])
        if l10n_mx.state == 'installed' and partner.l10n_mx_edi_colony:
            return partner.l10n_mx_edi_colony

        district = partner.street2
        return district


