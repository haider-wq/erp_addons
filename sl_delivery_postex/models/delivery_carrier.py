from odoo import api,models,fields,_
from urllib.parse import urlencode
from .postex_request import Postex
import base64

class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(
        selection_add=[('postex', 'Postex')],
        ondelete={'postex': lambda recs: recs.write({'delivery_type': 'fixed', 'fixed_price': 0})},
    )

    postex_production_api_key= fields.Text(
        string="Postex Api Key",
        help="Generate an Access Token from within the Sandbox Portal of Postex",
        copy=False, groups="base.group_system",
    )

    postex_orderType = fields.Selection(selection=[
        ('Normal', 'Normal'),
        ('Reverse', 'Reverse'),
        ('Replacement', 'Replacement'),
    ], string="Postex order type", default='Normal')




    def postex_cancel_shipment(self, pickings):
        """ Attempts to cancel shipment from within Envias
        backend. May run into issues if the shipment has already
        shipped or label been picked up by carrier.
        """
        postex = Postex(self, self.prod_environment, self.log_xml)
        for pick in pickings:
            if pick.carrier_id.delivery_type != 'postex' or not pick.carrier_tracking_ref:
                pick.message_post(body=pick.env._("postex order(s) not found to cancel shipment!"))
                continue

            invalid_trackings = postex._cancel_picking(pick)

            if invalid_trackings[0].get('status') == '0':
                pick.message_post(body=pick.env._("%(order_number)s", order_number=invalid_trackings)[0].get('response'))
            else:
                pick.write({
                    "carrier_tracking_ref": '',
                    "carrier_price": 0.00,
                })
                pick.message_post(body=pick.env._("%(order_number)s", order_number=invalid_trackings))

    def postex_rate_shipment(self, order):
        """ Returns shipping rate for the order and chosen shipping method."""

        order_weight = self.env.context.get('order_weight', None)
        postex = Postex(self, self.prod_environment, self.log_xml)
        result = postex._rate_request(
            order.partner_shipping_id,
            order.warehouse_id.partner_id or order.warehouse_id.company_id.partner_id,
            order,
            order_weight=order_weight
        )

        price = float(result['price'])
        return {
            'success': True,
            'price': price,
            'error_message': False,
            'warning_message': result.get('warning_message'),
        }

    def postex_send_shipping(self, pickings):
        """ Send shipment to postex for validation.
        Add shipment to cart, checkout, and generate label.
        """

        res = []
        postex = Postex(self, self.prod_environment, self.log_xml)
        attachment_ids = []
        for pick in pickings:
            shipment = postex._send_shipping(pick)
            pdf_name = f"postex_shipping_{shipment.get('tracking_number')}"
            pick.update({'carrier_tracking_ref': shipment.get('tracking_number')})
            data = postex.print_air_waybill(pick)

            pdf_name = pdf_name + ".pdf"
            attachment = self.env["ir.attachment"].create(
                {
                    "name": pdf_name,
                    "datas": base64.b64encode(data),
                    "type": "binary",
                    "res_model": "stock.picking",
                    "res_id": pick.id,
                }
            )
            attachment_ids.append(attachment.id)
            pick.message_post(attachment_ids=attachment_ids)
            res.append({
                'tracking_number': shipment.get('tracking_number'),
                'exact_price': shipment.get('exact_price')
            })
        return res

    def postex_get_tracking_link(self, picking):
        """ Returns the tracking link for a picking."""
        root_url = "https://merchant.postex.pk/main"

        params = {'trackingNumber': picking.carrier_tracking_ref}
        return f"{root_url}/order-detail?{urlencode(params)}"

