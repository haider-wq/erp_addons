from odoo import models, fields, _
from odoo.exceptions import UserError


class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    shopify_code = fields.Char(
        string='Shopify Code',
    )

    integration_send_tracking_url = fields.Boolean(
        string='Send Tracking URL',
        help=(
            'When checkbox is enabled, connector should send tracking URL '
            '(if carrier provides information about the URL).'
        ),
    )

    def get_external_carrier_code(self, integration):
        if integration.is_integration_shopify:
            value = self.shopify_code
            if not value:
                raise UserError(_(
                    'The "Shopify Code" field is not set for the carrier "%s". Please specify it to '
                    'enable sending the tracking number from Odoo to Shopify. '
                    'You can configure this field in the menu: '
                    '"Inventory → Configuration → Shipping Methods". '
                ) % self.name)

            return value

        return super(DeliveryCarrier, self).get_external_carrier_code(integration)

    def _get_carrier_by_external_name(self, integration, external_name):
        if integration.is_integration_shopify:
            carrier = self.env['delivery.carrier'].search([
                ('shopify_code', '=', external_name),
            ], limit=1)

            if carrier:
                return carrier

        return super(DeliveryCarrier, self)._get_carrier_by_external_name(integration, external_name)
