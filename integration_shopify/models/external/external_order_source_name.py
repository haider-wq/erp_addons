# See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class ExternalOrderSourceName(models.Model):
    _name = 'external.order.source.name'
    _description = 'Order Source Name'

    _sql_constraints = [
        (
            'external_name_uniq',
            'unique (integration_id, external_name)',
            'External Name must be unique per integration',
        )
    ]

    integration_id = fields.Many2one(
        comodel_name='sale.integration',
        string='Sale Integration',
        ondelete='cascade',
    )

    external_name = fields.Char(
        string='External Name',
        required=True,
    )

    name = fields.Char(
        string='Name',
        required=True
    )

    def get_or_create(self, integration_id, external_name, name=None):
        """
        Retrieve or create an External Order Source Name record.

        :param integration_id: ID of the related integration.
        :param external_name: External identifier for the order source.
        :param name: Optional display name. Defaults to external_name.
        :return: recordset of the matched or created record (single record).
        """
        if not external_name:
            raise ValueError('External name must be provided.')

        name = name or external_name

        domain = [
            ('integration_id', '=', integration_id),
            ('external_name', '=', external_name)
        ]

        record = self.search(domain, limit=1)
        if record:
            return record

        return self.create({
            'integration_id': integration_id,
            'external_name': external_name,
            'name': name,
        })
