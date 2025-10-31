# See LICENSE file for full copyright and licensing details.
from copy import deepcopy

from odoo import models, fields, _
from odoo.exceptions import UserError, ValidationError


class ExternalOrderFulfillment(models.Model):
    _name = 'external.order.fulfillment'
    _inherit = 'external.order.resource'
    _description = 'External Order Fulfillment'

    external_location_id = fields.Char(
        string='External Location ID',
    )
    tracking_company = fields.Char(
        string='Tracking Company',
    )
    tracking_number = fields.Char(
        string='Tracking Number',
    )
    line_ids = fields.One2many(
        comodel_name='external.order.fulfillment.line',
        inverse_name='fulfillment_id',
        string='Lines',
    )
    do_cancel_external = fields.Boolean(
        string='Cancel Fulfillment',
        help='Technical field for cancel order flow only',
    )

    def _validate(self):
        """
        Apply received order `fulfillment` in Odoo.
        It mean to perform `delivery validation`.

        :return: tuple(bool, int)
        """
        self.internal_info = False

        if self.is_done:
            return True, []

        if not self.is_ecommerce_ok:
            self.internal_info = _('Skipped due to external restrictions')
            self.mark_skipped()
            return False, []

        pickings = self._get_pickings()
        if not pickings:
            self.internal_info = _('There are no pickings awaiting walidation')
            self.mark_done()
            return True, []

        picking = pickings.filtered(lambda x: x._check_for_fulfill(self.line_ids))[:1]
        # TODO: what if the external fulfilment is suitable only for the pickings recordset
        if not picking:
            self.internal_info = _('There are no fitting pickings.')
            return False, []

        try:
            result = picking._validate_external_fulfillment(self)
        except (UserError, ValidationError) as ex:
            self.internal_info = ex.args[0]
            self.mark_failed()
            return False, []

        picking.mark_integration_sent()
        self.mark_done()

        return result, picking.ids

    def _compute_is_ecommerce_ok(self):
        for rec in self:
            rec.is_ecommerce_ok = (rec.external_status == 'success')

    def cancel_in_ecommerce_system(self):
        self.ensure_one()
        result = self.integration_id.adapter.cancel_fulfillment(self.external_str_id)
        if not result.get('userErrors'):
            self.external_status = 'cancelled'
        return result

    def _get_pickings(self):
        pickings = self.erp_order_id._get_pickings_to_handle()

        if self.erp_order_id.is_available_multi_stock_for_so:
            warehouse = self.integration_id._get_wh_from_external_location(self.external_location_id)
            if warehouse:
                pickings = pickings.filtered(lambda x: x.location_id.warehouse_id.id == warehouse.id)

        return pickings

    def _prepare_vals_from_external(self, data: dict) -> dict:
        vals = deepcopy(data)

        lines = vals.pop('lines', [])
        # Clear existing lines and add new ones
        vals['line_ids'] = [(5,)] + [(0, 0, x) for x in lines]

        return vals
