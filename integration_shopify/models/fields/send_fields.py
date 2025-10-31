# See LICENSE file for full copyright and licensing details.

from odoo import _

from odoo.addons.integration.exceptions import ApiExportError
from odoo.addons.integration.models.fields import SendFields

from ...shopify_api import METAFIELDS_NAME


class SendFieldsShopify(SendFields):

    def convert_translated_field_to_integration_format(self, field_name):
        external_code = self.adapter.lang
        language = self.env['res.lang'].from_external(self.integration, external_code)

        return getattr(self.odoo_obj.with_context(lang=language.code), field_name)

    def _get_simple_value(self, ecommerce_field):
        result = super(SendFieldsShopify, self)._get_simple_value(ecommerce_field)

        field_name = result and list(result.keys())[0] or ''

        # Handle Shopify metafields
        if field_name.startswith(f'{METAFIELDS_NAME}.'):
            if not ecommerce_field.shopify_metafield_type:
                raise ApiExportError(_(
                    'To export the metafield "%s", the "namespace" and "type" must be specified. '
                    'Please, go to "E-Commerce Integrations → Product Fields → All Product Fields" '
                    'and ensure these fields are filled in. Refer to Shopify '
                    'Settings → Custom Data → Products for guidance.'
                ) % field_name)

            # Parse the metafield components
            try:
                __, namespace, key = field_name.split('.')
            except ValueError:
                raise ApiExportError(_(
                    'The metafield "%s" has an invalid format. It must follow the structure '
                    '"%s.<Namespace>.<Key>".' % (field_name, METAFIELDS_NAME)
                ))

            # Construct the metafield value
            meta_value = {
                'key': key,
                'value': result[field_name],
                'namespace': namespace,
                'type': ecommerce_field.shopify_metafield_type,
            }
            result[field_name] = meta_value

        return result

    def _update_calculated_fields(self, vals, field_values):
        for field_name, field_value in field_values.items():
            if field_name.startswith(f'{METAFIELDS_NAME}.'):
                field_name = METAFIELDS_NAME
                field_value = vals.get(METAFIELDS_NAME, []) + [field_value]

            vals[field_name] = field_value

        return vals

    def _prepare_simple_value(self, ecommerce_field, odoo_value):
        field_name = ecommerce_field.technical_name

        if not field_name.startswith(f'{METAFIELDS_NAME}.'):
            return super()._prepare_simple_value(ecommerce_field, odoo_value)

        metafield_type = ecommerce_field.shopify_metafield_type
        odoo_field_type = ecommerce_field.odoo_field_id.ttype

        # Process metafields with Date and Datetime types. If corresponding Odoo fields have
        # Date or Datetime types, we need to convert the value to the string format.
        if metafield_type == 'date' and odoo_field_type in ('date', 'datetime'):
            return odoo_value and odoo_value.strftime('%Y-%m-%d')

        if metafield_type == 'date_time' and odoo_field_type in ('date', 'datetime'):
            return odoo_value and odoo_value.strftime('%Y-%m-%dT%H:%M:%SZ')

        return odoo_value
