# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import fields, models, api, _
from odoo.exceptions import ValidationError
from .postex_request import Postex
import base64
import io   # For handling binary data in memory
import logging

_logger = logging.getLogger(__name__)

class StockPackageType(models.Model):
    _inherit = 'stock.package.type'

    package_carrier_type = fields.Selection(selection_add=[('postex', 'Postex'),])
    shipper_package_code = fields.Char(compute='_compute_package_code', store=True, readonly=False)
    postex_mail_type = fields.Selection(
        selection=[
            ('pallet', 'Pallet'),
            ('box', 'Box'),
            ('envelope', 'Envelope')
        ],
        string="postex Package Type",
        help="Select the package type for the shipment",
        default="box",
    )

    @api.depends('postex_mail_type', 'package_carrier_type')
    def _compute_package_code(self):
        for package in self:
            if package.package_carrier_type == 'postex':
                package.shipper_package_code = package.postex_mail_type
            else:
                package.shipper_package_code = package.shipper_package_code

    @api.constrains('packaging_length', 'width', 'height', 'package_carrier_type')
    def _check_postex_required_value(self):
        for record in self:
            if record.package_carrier_type == 'postex' and \
                    any(dim <= 0 for dim in (record.packaging_length, record.width, record.height)):
                raise ValidationError(_('Length, Width, and Height is necessary for a postex Package.'))

    @api.depends('package_carrier_type')
    def _compute_length_uom_name(self):
        """
        Keep default length_uom and then convert it later down the line
        """
        super()._compute_length_uom_name()
        uom_name = self.env['product.template']._get_length_uom_name_from_ir_config_parameter()
        for package in self:
            if package.package_carrier_type == 'postex':
                package.length_uom_name = uom_name
