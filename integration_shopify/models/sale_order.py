# See LICENSE file for full copyright and licensing details.

import logging

from odoo import models, fields, api


_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    is_risky_sale = fields.Boolean(
        string='Risky Order',
        compute='_compute_is_risky_sale',
        store=True,
    )
    order_risk_ids = fields.One2many(
        comodel_name='external.order.risk',
        inverse_name='erp_order_id',
        string='Risks',
        copy=False,
    )
    shopify_fulfilment_status = fields.Many2one(
        string='Shopify Fulfillment Status',
        comodel_name='sale.order.sub.status',
        domain='[("integration_id", "=", integration_id)]',
        ondelete='set null',
        tracking=True,
        copy=False,
    )

    integration_sale_channel_id = fields.Many2one(
        string='E-Commerce Sales Channel',
        comodel_name='external.sale.channel',
        copy=False,
        help=(
            'The specific sales channel (e.g., Online Store, POS, Facebook) where this Shopify order originated.'
        ),
    )

    integration_order_source_name_id = fields.Many2one(
        string='E-Commerce Order Source Name',
        comodel_name='external.order.source.name',
        copy=False,
        help=(
            'The name of the source associated with the order.'
        ),
    )

    payment_method_ids = fields.Many2many(
        string='E-Commerce Payment Methods',
        comodel_name='sale.order.payment.method',
        domain='[("integration_id", "=", integration_id)]',
        copy=False,
    )

    def _shopify_cancel_order(self, *args, **kw):
        # from _perform_method_by_name calling
        _logger.info('SaleOrder _shopify_cancel_order(). Not implemented.')
        pass

    def _shopify_shipped_order(self, *args, **kw):
        # from _perform_method_by_name calling
        _logger.info('SaleOrder _shopify_shipped_order(). Not implemented.')
        pass

    def _shopify_paid_order(self, *args, **kw):
        # from _perform_method_by_name calling
        _logger.info('SaleOrder _shopify_paid_order()')

        status = self.env['sale.order.sub.status'].from_external(self.integration_id, 'paid')
        self.sub_status_id = status.id

    def _prepare_vals_for_sale_order_status(self):
        res = super(SaleOrder, self)._prepare_vals_for_sale_order_status()

        if self.integration_id.is_shopify():
            res['amount'] = str(self.amount_total)
            res['currency'] = self.currency_id.name

        return res

    @api.depends('order_risk_ids')
    def _compute_is_risky_sale(self):
        threshold = self._get_order_fraud_threshold()

        for rec in self:
            order_risk_ids = rec.order_risk_ids

            if order_risk_ids.filtered(lambda x: x.recommendation == 'cancel'):
                value = True
            elif order_risk_ids.filtered(lambda x: float(x.score) > threshold):
                value = True
            else:
                value = False

            rec.is_risky_sale = value

    def _adjust_integration_external_data(self, external_data: dict) -> dict:
        # Perform the common logic in the super() method
        res = super(SaleOrder, self)._adjust_integration_external_data(external_data)

        if not self.integration_id.is_shopify():
            return res

        external_order_id = self.external_order_name

        adapter = self.integration_id.adapter
        if 'order_risks' not in external_data:
            # 1. Fetch Order Risks
            order_risks = adapter.fetch_order_risks(external_order_id)
            external_data['order_risks'] = order_risks

        if 'payment_transactions' not in external_data:
            # 2. Fetch Order Payments
            payment_txns = adapter.fetch_order_transactions(external_order_id)
            external_data['payment_transactions'] = payment_txns

        if 'order_fulfillments' not in external_data:
            # 3. Fetch Order Fulfillments
            order_fulfillments = adapter.fetch_order_fulfillments(external_order_id)
            external_data['order_fulfillments'] = order_fulfillments

        return external_data

    def _apply_values_from_external(self, external_data: dict) -> dict:
        if self.integration_id.is_shopify():
            vals = dict()
            # 0. State update --> partially updated in the super() method
            if external_data.get('integration_workflow_states'):
                # 1. Update Statuses
                fulfillment_code = external_data['integration_workflow_states'][1]

                sub_status_fulfillment = self.env['integration.sale.order.factory'] \
                    ._get_order_sub_status(self.integration_id, fulfillment_code)

                vals['shopify_fulfilment_status'] = sub_status_fulfillment.id

            # 1. Tags
            if external_data.get('external_tags'):
                # 2. Update Tags
                ExternalTag = self.env['external.integration.tag'].with_context(
                    default_integration_id=self.integration_id.id,
                )

                tags = list()
                for tag_name in external_data['external_tags']:
                    tag = ExternalTag._get_or_create_tag_from_name(tag_name)
                    tags.append((4, tag.id, 0))

                vals['external_tag_ids'] = tags

            # 2. Risks
            if external_data.get('order_risks'):
                # 3. Update Risks
                ExternalOrderRisk = self.env['external.order.risk']

                risks = list()
                for risk_data in external_data['order_risks']:
                    risk = ExternalOrderRisk._create_or_update_risk_from_external(risk_data)
                    risks.append((4, risk.id, 0))

                vals['order_risk_ids'] = risks

            # 3. Fulfillments
            if external_data.get('order_fulfillments'):
                Fulfillment = self.env['external.order.fulfillment'] \
                    .with_context(integration_id=self.integration_id.id)

                fulfillments = list()
                for fulfill_data in external_data['order_fulfillments']:
                    record = Fulfillment._get_or_create_from_external(fulfill_data)
                    fulfillments.append((4, record.id, 0))

                vals['external_fulfillment_ids'] = fulfillments

            if vals:
                self.with_context(skip_dispatch_to_external=True).write(vals)

        return super(SaleOrder, self)._apply_values_from_external(external_data)

    def _get_order_fraud_threshold(self):
        threshold = self.env['ir.config_parameter'].sudo().get_param(
            'integration.fraud_threshold',
        )
        return float(threshold)
