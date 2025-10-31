# See LICENSE file for full copyright and licensing details.

import logging

from odoo import models, fields


_logger = logging.getLogger(__name__)


class ExternalOrderResource(models.AbstractModel):
    _name = 'external.order.resource'
    _description = 'External Order Resource'

    name = fields.Char(
        string='Name',
    )
    internal_status = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('skipped', 'Skipped'),
            ('failed', 'Failed'),
            ('done', 'Done'),
        ],
        string='Internal Status',
        default='draft',
        required=True,
    )
    external_str_id = fields.Char(
        string='External ID',
    )
    external_order_str_id = fields.Char(
        string='External Order ID',
    )
    external_status = fields.Char(
        string='External Status',
    )
    internal_info = fields.Char(
        string='Internal Info',
    )
    erp_order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Odoo Order',
        ondelete='cascade',
    )
    integration_id = fields.Many2one(
        related='erp_order_id.integration_id',
    )
    integration_name = fields.Char(
        string='Integration Name',  # Avoid name duplicating with the `name` field
        related='integration_id.name',
    )
    is_ecommerce_ok = fields.Boolean(
        string='External Status OK',
        compute='_compute_is_ecommerce_ok',
    )

    @property
    def is_done(self):
        return self.internal_status == 'done'

    def _compute_is_ecommerce_ok(self):
        for rec in self:
            rec.is_ecommerce_ok = False

    def mark_done(self):
        self.write({'internal_status': 'done'})

    def mark_skipped(self):
        self.write({'internal_status': 'skipped'})

    def mark_failed(self):
        self.write({'internal_status': 'failed'})

    def _get_or_create_from_external(self, data):
        record = self.search([
            ('external_str_id', '=', data['external_str_id']),
            ('integration_id', '=', self._context.get('integration_id', False)),
        ], limit=1)

        data['external_order_str_id'] = self.env.context.get('external_order_id')
        vals = self._prepare_vals_from_external(data)

        if not record:
            record = self.create(vals)
        else:
            record.write(vals)

        return record

    def validate(self):
        result, ids = self._validate()

        if not result:
            _logger.warning(
                '%s: %s (order=%s; external_id=%s; internal_status=%s) was not applied: %s',
                self.integration_id.name,
                self._description,
                self.erp_order_id.name,
                self.external_str_id,
                self.internal_status,
                self.internal_info,
            )

        return result, ids

    def _validate(self):
        raise NotImplementedError

    def _prepare_vals_from_external(self, data: dict) -> dict:
        """Redefine it for specific integration."""
        return data
