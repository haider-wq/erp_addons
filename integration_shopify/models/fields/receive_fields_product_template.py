# See LICENSE file for full copyright and licensing details.

from odoo import _
from odoo.exceptions import UserError
from odoo.addons.integration.models.fields.common_fields import GENERAL_GROUP
from odoo.addons.integration.models.fields import ProductTemplateReceiveMixin

from .receive_fields import ReceiveFieldsShopify
from ...shopify.shopify_client import COLLECT
from ...shopify.shopify_order import format_attr_value_code


class ReceiveFieldsProductTemplateShopify(ReceiveFieldsShopify, ProductTemplateReceiveMixin):

    def __init__(self, integration, odoo_obj=False, external_obj=False):
        super().__init__(integration, odoo_obj, external_obj)

        if not self.odoo_obj:
            self.odoo_obj = self.env['product.template']

    def get_ext_attr(self, ext_attr_name):
        if ext_attr_name == 'attr_values_ids_by_attr_id':
            attributes = self.adapter._attribute_value_from_template(self.external_obj)

            return self.find_attributes_in_odoo(
                format_attr_value_code(name, value) for (name, value) in attributes
            )

        raise UserError(_(
            'The attribute "%s" does not exist or is unsupported for import. '
            'Please verify the attribute name and try again.'
        ) % ext_attr_name)

    def convert_from_external(self):
        result = self.calculate_receive_fields()

        # 'type' field should be set only during the initial import
        # (to avoid issues with 'type' field changes on products synchronisation -
        # Odoo doesn't allow to change existing products types)
        if not self.odoo_obj:
            result['type'] = 'consu'
            result['is_storable'] = True

        return result

    def receive_integration_name(self, field_name):
        name_value = self.convert_translated_field_to_odoo_format(self.external_obj.title)
        odoo_field = self.odoo_obj.get_integration_name_field()
        return {
            odoo_field: name_value,
        }

    def receive_product_status_spf(self, field_name):
        return {
            'sale_ok': self.external_obj.status == 'active',
            'active': self.external_obj.status != 'archived',
        }

    def receive_categories(self, field_name):
        collects = self.adapter.fetch_multi(
            COLLECT,
            params={
                'product_id': self.external_obj.id,
            },
            fields=['collection_id'],
        )

        ext_category_ids = [x.collection_id for x in collects]

        return {
            field_name: [(6, 0, self.find_categories_in_odoo(ext_category_ids))],
        }

    def receive_list_price(self, field_name):
        # TODO price_including_taxes
        # The API only returns prices for variants, so the method returns an empty dictionary.
        if len(self.external_obj.variants) != 1:
            return {}

        # Price shouldn't be imported if pricelist for export was set (excluding the first time import)
        if self.integration.integration_pricelist_id and not self.first_time_import:
            return {}

        price = float(self.external_obj.variants[0].price)

        return {field_name: price}

    def receive_product_tags(self, field_name):
        ProductFeatureValue = self.env['product.feature.value']
        FeatureValueExternal = self.env['integration.product.feature.value.external']
        odoo_features = [(5, 0)]

        feature = self.env['product.feature'].from_external(self.integration, GENERAL_GROUP)
        tags = self.external_obj.tags
        tags = tags.split(',')

        for tag in tags:
            tag = tag.strip()

            if not tag:
                continue

            feature_value = ProductFeatureValue.from_external(
                self.integration, tag, raise_error=False)

            if not feature_value:
                feature_value = ProductFeatureValue.create({
                    'feature_id': feature.id,
                    'name': tag,
                })

                external_feature = feature.to_external_record(self.integration)

                feature_value_external = FeatureValueExternal.create_or_update({
                    'integration_id': self.integration.id,
                    'code': tag,
                    'name': tag,
                    'external_feature_id': external_feature.id,
                })

                feature_value_external.create_or_update_mapping(odoo_id=feature_value.id)

            odoo_features.append((0, 0, {
                'feature_id': feature.id,
                'feature_value_id': feature_value.id,
            }))

        return {
            field_name: odoo_features,
        }
