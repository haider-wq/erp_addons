# See LICENSE file for full copyright and licensing details.

import sys
import json
import logging
import traceback
from time import time
from io import StringIO
from datetime import datetime, timedelta
from typing import List, Dict

import pytz
from dateutil import parser
from psycopg2 import OperationalError

from odoo import api, fields, models, registry, SUPERUSER_ID, _

from odoo.tools import config, ormcache
from odoo.tools.safe_eval import safe_eval
from odoo.exceptions import UserError, ValidationError

from ..api.no_api import NoAPIClient
from ..tools import (
    raise_requeue_job_on_concurrent_update,
    normalize_uom_name,
    _is_valid_email,
    track_changes,
    HtmlWrapper,
    Adapter,
    AdapterHub,
    PriceList,
    TemplateHub,
)
from ..exceptions import (
    ApiImportError,
    ApiExportError,
    NotMappedToExternal,
    NotMappedFromExternal,
    NoReferenceFieldDefined,
)


DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
MAPPING_EXCEPT_LIST = [
    'integration.account.tax.group.mapping',
    'integration.metafield.mapping',
    'integration.product.image.mapping',
]
LOG_SEPARATOR = '================================'
IMPORT_EXTERNAL_BLOCK = 150  # Don't make more, because of 414 Request-URI Too Large error
EXPORT_EXTERNAL_BLOCK = 500

IMAGE_FIELDS = ['image_1920', 'product_template_image_ids', 'product_variant_image_ids']
PRODUCT_QTY_FIELDS = ['free_qty', 'qty_available', 'virtual_available']
TRACKED_FIELDS_INITIAL_PRODUCT_EXPORT = ['integration_ids']
SEARCH_CUSTOMER_FIELDS = ['email', 'name', 'mobile', 'phone']

_logger = logging.getLogger(__name__)


class SaleIntegration(models.Model):
    _name = 'sale.integration'
    _description = 'E-Commerce Store'
    _inherit = ['mail.thread']

    _sql_constraints = [
        ('unique_name', 'unique(name)', 'A record with the same name already exists.'),
        ('order_name_ref_unique', 'unique(order_name_ref)', 'Sale Order prefix name should be unique.'),
    ]

    _adapter_hub_ = AdapterHub()

    name = fields.Char(
        string='Name',
        required=True,
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    integration_lang_id = fields.Many2one(
        comodel_name='res.lang',
        string='Default Integration Language',
        help=(
            'Choose the default language for this integration, which will be applied '
            'during import and export processes. Typically, this should align with the '
            'language used in the e-commerce system for consistency.'
        ),
    )
    lang_ids = fields.Many2many(
        comodel_name='res.lang',
        compute='_compute_lang_ids',
        string='Mapped Languages',
    )
    type_api = fields.Selection(
        selection=[('no_api', 'Not Use API')],
        string='API Service',
        required=True,
        ondelete={
            'no_api': 'cascade',
        },
        help=(
            'Select the API service type for the current Sales Integration. '
            'This setting should be configured initially and must remain unchanged '
            'after the initial setup to ensure proper integration functionality.'
        ),
    )
    state = fields.Selection(
        selection=[
            ('new', 'New'),
            ('draft', 'Draft'),
            ('active', 'Active'),
        ],
        string='Status',
        required=True,
        default='new',
    )
    field_ids = fields.One2many(
        comodel_name='sale.integration.api.field',
        inverse_name='sia_id',
        string='Fields',
    )
    test_method = fields.Selection(
        selection='_get_test_method',
        string='Debug Method Execution',
        help=(
            'Specify the method you wish to execute for testing purposes. Caution: Running '
            'test methods directly can alter your data and system behavior. This should only '
            'be done by users who understand the implications and have ensured that it is '
            'safe to proceed.'
        ),
    )
    location_line_ids = fields.One2many(
        comodel_name='external.stock.location.line',
        inverse_name='integration_id',
        string='Locations',
    )
    location_ids = fields.Many2many(  # TODO: Deprecated. Drop it after 1.12.0 integration release.
        comodel_name='stock.location',
        string='Depreceted Locations',
        domain=[
            ('usage', '=', 'internal'),
        ],
    )
    last_receive_orders_datetime = fields.Datetime(
        string='Last Receive Orders Time',
        required=True,
        default=fields.Datetime.now,
        help=(
            'Sets the import filter based on the order update time from the E-Commerce system. '
            'The "Last Receive Orders Time" is automatically updated after each order import '
            'into Odoo, ensuring only new updates are fetched.'
        ),
    )
    last_receive_orders_datetime_str = fields.Char(
        compute='_compute_last_receive_orders_datetime_str',
        string='Last Receive Orders Time String',
    )
    last_update_pricelist_items = fields.Datetime(
        string='Pricelist Last Sync Date',
        default=fields.Datetime.now,
        help=(
            'This timestamp indicates the last update for pricelist items. '
            'Only items modified after this date will be selected for export to ensure '
            'the synchronization includes the most recent changes.'
        ),
    )
    receive_orders_cron_id = fields.Many2one(
        comodel_name='ir.cron',
    )
    export_template_job_enabled = fields.Boolean(
        string='Enable Product Template Export Job',
        default=False,
        help=(
            'Check this option to activate the automatic export of product templates to the '
            'e-commerce system whenever relevant product fields are updated in Odoo.'
        ),
    )
    export_inventory_job_enabled = fields.Boolean(
        string='Real-Time Inventory Updates',
        default=False,
        help=(
            'Check this box to enable immediate synchronization of inventory levels to the '
            'e-commerce system following any stock changes in Odoo. This ensures your e-commerce '
            'platform reflects the most current stock information.'
        ),
    )
    synchronize_all_inventory_periodically = fields.Boolean(
        string='Scheduled Inventory Sync',
        default=False,
        help=(
            'Enable this to schedule periodic inventory updates from Odoo to the e-commerce '
            'system. This is done via a cron job that runs at intervals set by you, '
            'ideal for batch updating stock levels.'
        ),
    )
    inventory_synchronization_cron_id = fields.Many2one(
        comodel_name='ir.cron',
    )
    next_inventory_synchronization_date = fields.Datetime(
        string='Next Synchronization Date',
        compute='_compute_next_inventory_synchronization_date',
        readonly=False,
        inverse='_inverse_next_inventory_synchronization_date',
    )
    export_tracking_job_enabled = fields.Boolean(
        string='Enable Order Tracking Export Job',
        default=False,
        help=(
            'Check this option to automate the export of tracking numbers from Odoo to the '
            'e-commerce platform upon confirmation of the delivery order.'
        ),
    )
    export_sale_order_status_job_enabled = fields.Boolean(
        string='Enable Sale Order Status Export Job',
        default=False,
        help=(
            'Check this option to automatically update the sale order status on the '
            'e-commerce system corresponding to status changes in Odoo.'
        ),
    )
    product_ids = fields.Many2many(
        'product.template', 'sale_integration_product', 'sale_integration_id', 'product_id',
        'Products',
        copy=False,
        check_company=True,
    )

    discount_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Discount Product',
        domain="[('type', '=', 'service')]",
        help=(
            'Select a product to represent discounts as a separate line item within Odoo. '
            'This will ensure that discounts are itemized clearly on sales orders.'
        ),
    )

    positive_price_difference_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Price Difference Product (Positive)',
        domain="[('type', '=', 'service')]",
        help=(
            'Select a product to account for any positive discrepancies between the '
            'total amounts of orders in the E-Commerce system and Odoo. This product will be '
            'added to the sales order as a separate line to adjust for the price difference.'
        ),
    )

    negative_price_difference_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Price Difference Product (Negative)',
        domain="[('type', '=', 'service')]",
        help=(
            'Select a product to account for any negative discrepancies between the '
            'total amounts of orders in the E-Commerce system and Odoo. This product will be '
            'added to the sales order as a separate line to adjust for the price difference.'
        ),
    )

    gift_wrapping_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Gift Wrapping Product',
        help=(
            'Select a specific product to be used for gift-wrapping services. If a sales order '
            'in PrestaShop includes gift wrapping, this product will be added accordingly.'
        ),
    )

    run_action_on_cancel_so = fields.Boolean(
        string='Sync Cancelled SO Status',
        copy=False,
        help=(
            'Enable this option to automatically update the order status in the e-commerce '
            'system when a sales order is cancelled in Odoo.'
        ),
    )

    sub_status_cancel_id = fields.Many2one(
        comodel_name='sale.order.sub.status',
        string='Store Order Status for Cancelled SO',
        domain='[("integration_id", "=", id)]',
        copy=False,
        help=(
            'Specify the store order status that will be sent to the e-commerce system when '
            'a sales order is cancelled in Odoo.'
        ),
    )

    run_action_on_shipping_so = fields.Boolean(
        string='Sync Shipped SO Status',
        copy=False,
        help=(
            'Activate this option to update the order status in the e-commerce system '
            'automatically when a sales order is marked as shipped in Odoo.'
        ),
    )

    sub_status_shipped_id = fields.Many2one(
        comodel_name='sale.order.sub.status',
        string='Store Order Status for Shipped SO',
        domain='[("integration_id", "=", id)]',
        copy=False,
        help=(
            'Specify the store order status that will be sent to the e-commerce system for '
            'orders marked as shipped in Odoo.'
        ),
    )

    run_action_on_so_invoice_status = fields.Boolean(
        string='Sync Invoiced/Paid SO Status',
        copy=False,
        help=(
            'Enable this option to update the order status in the e-commerce system for '
            'sales orders that are invoiced or marked as paid in Odoo. Specific behaviors '
            'based on the payment method can be configured under '
            '"Auto-Workflow → Payment Methods", where you can adjust when the payment status '
            'is sent. By default, the action occurs when an order is fully invoiced, '
            'and all related invoices are "Paid" or "In Payment".'
        ),
    )

    sub_status_paid_id = fields.Many2one(
        comodel_name='sale.order.sub.status',
        string='Store Order Status for Invoiced/Paid SO',
        domain='[("integration_id", "=", id)]',
        copy=False,
        help=(
            'Choose the store order status to be applied to orders in the e-commerce system once '
            'the corresponding sales order in Odoo is fully paid.'
        ),
    )

    apply_to_products = fields.Boolean(
        string='Auto-Export New Products',
        default=False,
        help=(
            'Enable automatic export of newly created Odoo products to the e-commerce system, '
            'ensuring all new inventory items are synchronized.'
        ),
    )

    auto_create_products_on_so = fields.Boolean(
        string='Auto-Create Missing Products',
        default=False,
        help=(
            'Automatically create a new product in Odoo if a product from an imported '
            'sales order does not match any existing product mappings. If disabled, '
            'sales order import will fail, and manual products mapping will be required '
            'by an administrator.'
        )
    )

    auto_create_taxes_on_so = fields.Boolean(
        string='Auto-Create Missing Taxes',
        default=False,
        help=(
            'Enable the automatic creation of tax entries in Odoo when a sales order '
            'is imported with taxes that do not have predefined mappings. Without this '
            'setting enabled, the import of the sales order will fail, and manual tax '
            'mapping will be required by an administrator.'
        )
    )

    auto_create_delivery_carrier_on_so = fields.Boolean(
        string='Auto-Create Missing Delivery Carriers',
        default=False,
        help=(
            'Automatically generate a new delivery carrier entry in Odoo if the carrier '
            'specified in an imported sales order does not exist. If this option is not enabled, '
            'the absence of a delivery carrier will cause the sales order import to fail, '
            'and manual delivery carrier mapping will be required by an administrator.'
        ),
    )

    apply_external_fulfillments = fields.Boolean(
        string='Auto-Apply Fulfillments from E-Commerce System',
        default=True,
        help=(
            'Automatically apply fulfillments from the e-commerce system to matching'
            'sales orders in Odoo. This happens after orders are automatically confirmed in Odoo '
            '(if auto-workflow is enabled with "Confirm Order" action).'
        ),
    )

    apply_external_payments = fields.Boolean(
        string='Auto-Apply Payments from E-Commerce System',
        default=False,
        help=(
            'Automatically Apply Payments from E-Commerce System to Matching Sales Orders in Odoo. '
            'Payments from the E-Commerce System are now automatically applied to corresponding sales orders in Odoo. '
            'This process occurs after order invoices are automatically confirmed in Odoo '
            '(if the auto-workflow is enabled with the "Validate Invoice" action).'
        ),
    )

    integration_writeoff_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Default Write-off Account',
        default=False,
        help=(
            'The write-off account is used to record the difference between the payment '
            'downloaded from E-Commerce System and the invoice total in Odoo.'
        ),
    )

    force_full_fulfillment = fields.Boolean(
        string='Force Shopify Order Fulfillment (Debug)',
        default=False,
        help=(
            'Use only if experiencing fulfillment issues. Forces complete fulfillment of Shopify orders, '
            'even if there are discrepancies between the Odoo shipment and the original order '
            '(e.g., different quantities or missing items). This can be helpful in troubleshooting scenarios '
            'where automatic fulfillment fails.'
        ),
    )

    webhook_line_ids = fields.One2many(
        comodel_name='integration.webhook.line',
        inverse_name='integration_id',
        string='Webhook Lines',
        help="""WEBHOOK STATES:

            - Green: active webhook.
            - Red: inactive webhook.
            - Yellow: webhook need to be recreated, click button "Create Webhooks" above.
        """,
    )

    save_webhook_log = fields.Boolean(
        string='Enable Webhook Event Logging',
        help=(
            'Turn this on to log all webhook events related to order status changes in the '
            'e-commerce system. This can aid in monitoring and troubleshooting '
            'the order status update process.'
        ),
    )

    allow_import_images = fields.Boolean(
        string='Enable Images Import',
        default=True,
        help='Allow import images during product import from the e-commerce system.',
    )

    allow_export_images = fields.Boolean(
        string='Enable Images Export',
        default=True,
        help=(
            'Enable this feature to automatically update and export product images from Odoo '
            'to your e-commerce platform during products synchronization.'
        ),
    )

    synchronise_qty_field = fields.Selection(
        selection='_calc_qty_fields_selections',
        string='Inventory Quantity Source Field',
        required=True,
        default='free_qty',
        help=(
            'Select the field in Odoo which should be used to update the stock levels on the '
            'e-commerce platform. This is typically the "Quantity on Hand" or a custom field '
            'representing available stock.'
        ),
    )

    send_inactive_product = fields.Boolean(
        string='Mark as Inactive on First Export',
        help=(
            'Enable this to export new products as "inactive" to the external system, '
            'allowing for additional reviews and edits before they are published.'
        ),
    )

    request_order_url = fields.Char(
        string='Invoice Request Endpoint',
        compute='_compute_request_order_url',
        help=(
            'This is the fixed URL endpoint you should use in the e-commerce system '
            'to request invoices by order ID.'
        ),
    )

    mandatory_fields_initial_product_export = fields.Many2many(
        string='Required Fields for Initial Export',
        comodel_name='ir.model.fields',
        required=True,
        help=(
            'Determine the fields that must be populated in Odoo for a product '
            'to qualify for export to the external system.'
        ),
        domain='[("model", "=", "product.product")]',
        default=lambda self: self._get_default_mandatory_fields(),
    )

    select_send_sale_price = fields.Selection(
        string='Conversion method for Sales Price',
        selection=[
            ('no_changes', 'No changes'),
            ('tax_included', 'Send tax included Sales Price (B2C)'),
            ('tax_excluded', 'Send tax excluded Sales Price (B2B)'),
        ],
        default='no_changes',
        required=True,
        help=(
            'Specify the conversion method for the "Sales Price" during export, as set in the '
            '"Product field mapping". The conversion takes into account the tax settings '
            'from the Product template in Odoo.'
        ),
    )

    behavior_on_empty_tax = fields.Selection(
        selection=[
            ('leave_empty', 'Leave Empty'),
            ('set_special_tax', 'Set Special Tax'),
            ('take_from_product', ' Take from the Product'),
        ],
        string='Behavior on Empty Taxes',
        default='leave_empty',
        required=True,
        help=(
            'Define the action to be taken for sales order lines that do not have associated '
            'tax data. This helps maintain accurate tax reporting and compliance.'
        ),
    )

    zero_tax_id = fields.Many2one(
        comodel_name='account.tax',
        string='Special Zero Tax',
    )

    pricelist_integration = fields.Boolean(
        string='Pricelists Synchronization',
        help=(
            'Toggle this option to enable the synchronization of pricelists and their items '
            'between e-commerce system and Odoo. When active, updates to pricelists in either '
            'system can be imported and reflected in the other.'
        ),
    )

    invoice_report_id = fields.Many2one(
        comodel_name='ir.actions.report',
        string='Invoice Report Template',
        domain=[
            ('model', '=', 'account.move'),
        ],
        help=(
            'Select the invoice report template that will be utilized to generate '
            'the PDF file for invoices.'
        ),
        default=lambda self: self.env.ref('account.account_invoices').id,
    )

    behavior_on_non_existing_invoice = fields.Selection(
        selection=[
            ('return_not_exist', 'Return message that invoice does not exists'),
            ('try_generate', 'Try to generate and post invoice'),
        ],
        string='No-Invoice Response Action',
        default='return_not_exist',
        required=True,
        help=(
            'Determine the system\'s response when an invoice is not found for a sales order. '
            'Select "Return Message" to notify that the invoice doesn\'t exist, or choose '
            '"Generate and Post Invoice" to automatically create and finalize the invoice.'
        ),
    )

    use_async = fields.Boolean(
        string='Use asynchronous methods to download from E-Commerce System',
        default=lambda self: sys.version_info >= (3, 7),
    )

    orders_cut_off_datetime = fields.Datetime(
        string='Orders Cut-off date',
        default=fields.Datetime.now,
        help=(
            'Sets a cut-off date for order imports, based on the date of creation from the '
            'E-Commerce system. Orders created before the "Orders Cut-off Date" will not be '
            'imported, regardless of any updates or changes made after this date.'
        ),
    )

    orders_cut_off_datetime_str = fields.Char(
        string='Orders Cut-off Date String',
        compute='_compute_orders_cut_off_datetime_str',
    )

    check_weight_uoms = fields.Boolean(
        compute='_compute_check_weight_uoms',
        string='Check Weight Uoms',
    )

    price_including_taxes = fields.Boolean(
        string='Price Including Tax',
        help=(
            'Toggle this to align tax inclusion settings between Odoo and e-commerce system, '
            'ensuring taxes are correctly marked as "Included in Price" within Odoo.'
        ),
    )

    global_tracked_fields = fields.Many2many(
        string='Product Sync Watch Fields',
        comodel_name='ir.model.fields',
        relation='sale_integration_global_tracked_fields_rel',
        help=(
            'Choose the Odoo product fields that, when altered, will trigger an automatic '
            'export of the product details to the connected e-commerce platform.'
        ),
        domain='[("model", "in", ["product.template", "product.product"])]',
        default=lambda self: self._get_default_global_tracked_fields(),
    )

    temp_external_id = fields.Char(
        string='External ID',
    )

    validate_barcode = fields.Boolean(
        string='Variant Barcode Validation',
        help=(
            'Enable this option to check for missing barcodes for product variants. '
            'This setting only works when the "Receive field on import" option '
            'is enabled for the barcode field for this integration under '
            'Integration → Product Fields → Product Field Mapping.'
        ),
    )

    is_import_dynamic_attribute = fields.Boolean(
        string='Dynamic Attribute Import Mode',
        help=(
            'Enable this to allow the e-commerce connector to import product '
            'attributes dynamically, creating only the necessary variants in Odoo '
            'based on the available combinations in the external system, '
            'and avoiding the generation of non-existent variants.'
        ),
    )

    integration_pricelist_id = fields.Many2one(
        string='Regular Pricelist for Product Export',
        comodel_name='product.pricelist',
        help=(
            'Choose the price list that will be used to determine product prices during the '
            'export process. Note: this functionality is available only for basic pricelists '
            'with fixed price for products, min qty=0 and valid or '
            'empty values of pricelists periods.\n'
            'Note: this functionality is available only for basic pricelists with fixed price '
            'for products, min qty=0 and valid or empty values of pricelists periods. '
        ),
    )

    integration_sale_pricelist_id = fields.Many2one(
        string='Sale Pricelist for Product Export',
        comodel_name='product.pricelist',
        help=(
            'Select the pricelist that will be used to trigger the update of the entire product in your e-commerce '
            'system when pricelist item price changes occur. This pricelist will specifically be used for '
            'sales-related price updates. Ensure that this pricelist is configured with fixed prices for products, '
            'a minimum quantity of 0, and valid or empty pricelist periods.'
        ),
    )

    # The fields below used to show statistics card on kanban view
    orders_count = fields.Integer(
        string='Total Orders',
        compute='_compute_orders_count',
        store=False,
    )

    orders_today_count = fields.Integer(
        string='Today Orders',
        compute='_compute_orders_today_count',
        store=False,
    )

    queued_or_failed_orders_count = fields.Integer(
        string='Total Failed Orders',
        compute='_compute_queued_or_failed_orders_count',
        store=False,
    )

    product_bundle_policy = fields.Selection(
        selection=[
            ('create_bundle', 'Create as Bundle'),
            ('decompose_bundle', 'Decompose into Components'),
        ],
        string='Bundle Handling Policy',
        default='create_bundle',
        required=True,
        help=(
            'Choose how the connector should handle bundle or composite products included in '
            'incoming orders. Select "Create as Bundle" to treat the bundle as a single kit '
            'product in Odoo. Choose "Decompose into Components" if you prefer to break down '
            'the bundle into its individual component products for the order.'
        ),
    )

    separate_discount_line = fields.Boolean(
        string='Add Discounts as a Separate Order Lines',
        default=True,
        help=(
            'This setting controls how discounts are applied to orders. When enabled, each '
            'discount is added as a separate line item for every product that requires a discount. '
            'If disabled, the connector calculates the discount percentage and applies it directly '
            'to the corresponding product\'s order line by utilizing the Discount field.'
        ),
    )

    change_advanced_fields = fields.Boolean(
        string='Change Advanced Product Fields',
    )

    template_reference_id = fields.Many2one(
        comodel_name='product.ecommerce.field',
        string='Template Reference',
        ondelete='restrict',
    )

    product_reference_id = fields.Many2one(
        comodel_name='product.ecommerce.field',
        string='Variant Reference',
        ondelete='restrict',
    )

    template_barcode_id = fields.Many2one(
        comodel_name='product.ecommerce.field',
        string='Template Barcode',
        ondelete='restrict',
    )

    product_barcode_id = fields.Many2one(
        comodel_name='product.ecommerce.field',
        string='Variant Barcode',
        ondelete='restrict',
    )

    fallback_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Fallback Product',
        domain="[('type', '=', 'service')]",
        help=(
            'Use this field to select a default product for order lines with missing '
            'SKU and ID details. It\'s applicable when products are removed or '
            'orders contain custom items, ensuring uninterrupted order processing.'
        ),
    )

    use_manual_customer_mapping = fields.Boolean(
        string='Enable Manual Customer Mapping',
        help=(
            'Enable this option to switch to manual mapping of customers in the '
            'Mappings → Contacts section. This setting is suitable for B2B with a small '
            'customer base, where each customer\'s details require verification before being '
            'added to Odoo.'
        ),
    )

    emails_for_failed_mapping_notifications = fields.Char(
        string='Emails to Notify about Failed Customer Mapping',
        help=(
            'Enter a list of email addresses (separated by commas) to receive notifications in '
            'case of unsuccessful customer mapping. This feature will work only when manual '
            'customer mapping is enabled.'
        ),
    )

    skip_individual_contacts = fields.Boolean(
        string='Use Company as Contact When Available (Experimental)',
        help=(
            'When enabled, the connector will use the company as the contact instead of creating '
            'individual contact records when company information is available. This is particularly '
            'useful for B2B scenarios where you want to maintain a cleaner contact structure by '
            'avoiding duplicate individual contacts for the same company.'
        ),
    )

    use_vat_only_company_search = fields.Boolean(
        string='Search Companies by VAT Number Only',
        help=(
            'Enable to search companies by VAT number only. Ensure the "VAT / Reg. Number" field '
            'is filled for use.'
        ),
    )

    use_order_total_difference_correction = fields.Boolean(
        string='Order Total Difference Correction',
        default=True,
        help=(
            'Enable this option to automatically correct the difference in total order amount '
            'when importing orders from E-Commerce Systems.'
        ),
    )

    search_customer_fields_ids = fields.Many2many(
        string='Search Customer Fields',
        comodel_name='ir.model.fields',
        relation='integration_search_customer_fields_rel',
        column1='integration_id',
        column2='field_id',
        domain=lambda self: [
            ('model', '=', 'res.partner'), ('name', 'in', SEARCH_CUSTOMER_FIELDS),
        ],
        default=lambda self: self._default_search_customer_fields_ids(),
        help=(
            'Select fields to search when looking for customers '
            '(e.g. name, email, phone, mobile). '
            'These fields will be used in addition to the required fields: name, parent_id, '
            'and is_company.'
            '\n\n'
            'Users can customize the search by selecting or deselecting specific fields from the '
            'list of available options.'
            '\n\n'
            'Important note: customer fields not specified in this field (i.e. name, email, etc.) '
            'will be updated with the latest data from the e-commerce system.'
        ),
    )

    use_search_customer_fields_ids = fields.Boolean(
        string='Use Selected Customer Fields for Search',
        help=(
            'Indicates that a specific set of partner search fields is used. Technical field.'
        ),
    )

    ignore_vat_validation = fields.Boolean(
        string='Ignore VAT validation',
        help=(
            'If that is checked, connector will skip checking company tax identification number '
            'for validity and will insert it as is on the partner.'
        ),
    )

    import_order_enabled = fields.Boolean(
        string='Enable Order Import',
        related='receive_orders_cron_id.active',
        readonly=False,
        help=(
            'Enable this option to import orders from the connected E-Commerce System. '
            'This option will be automatically disabled when the integration is deactivated.'
        ),
    )

    use_odoo_so_numbering = fields.Boolean(
        string='Use Odoo SO numbering',
        help='Enable this option to use Odoo sales order numbering instead of the e-commerce system\'s numbering.',
    )

    update_fiscal_position = fields.Boolean(
        string='Automatically update taxes based on Fiscal Position',
    )

    default_tax_scope = fields.Selection(
        selection=[
            ('service', 'Services'),
            ('consu', 'Goods'),
        ],
        string='Tax Scope',
        help='Select a default tax scope to automatically create taxes.'
    )

    default_tax_group_id = fields.Many2one(
        string='Tax Group',
        comodel_name='account.tax.group',
        help='Select a default tax group to automatically create taxes.',
    )

    default_account_id = fields.Many2one(
        string='Account',
        comodel_name='account.account',
        check_company=True,
        help='Select a default account to automatically create taxes.',
    )

    update_stock_for_manufacture_boms = fields.Boolean(
        string='Calculate Stock for "Manufacture" BoMs',
        help=(
            'Enable this option to allow the connector to calculate stock levels for products '
            'with a "Manufacture" BoM based on the stock levels of its sub-components. The computed '
            'stock will be synchronized with the e-commerce store.'
        ),
    )

    ignore_boms_for_product_export = fields.Boolean(
        string='Ignore BoMs for Product Export',
        help=(
            'Enable this option to export products as standalone items, ignoring any attached BoMs.'
            'When enabled, products will be exported as simple products (not kits or bundles),'
            'and BoM-related details will not be included in the e-commerce system.'
        ),
    )

    is_integration_shopify = fields.Boolean(
        string='Is Shopify',
        compute='_compute_integration_type',
    )

    is_integration_prestashop = fields.Boolean(
        string='Is PrestaShop',
        compute='_compute_integration_type',
    )

    is_integration_magento_two = fields.Boolean(
        string='Is Magento 2',
        compute='_compute_integration_type',
    )

    is_integration_woocommerce = fields.Boolean(
        string='Is WooCommerce',
        compute='_compute_integration_type',
    )

    order_name_ref = fields.Char(
        string='Sales Order Prefix',
        help=(
            'Set a unique prefix for orders to easily identify those imported from this '
            'integration. This prefix will precede the order number in Odoo.'
        ),
    )

    so_external_reference_field = fields.Many2one(
        string='Sales Order External Reference Field',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help=(
            'Specify a Sales Order field (character or text types only) to record an external '
            'sales order number. This is supplementary to the "E-Commerce Order Reference" field '
            'and can be used for enhanced order tracking and integration purposes.'
        ),
        required=False,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "sale.order"), '
               '("ttype", "in", ("text", "char")) ]',
    )

    so_delivery_note_field = fields.Many2one(
        string='Sales Order Delivery Note Field',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help=(
            'Specify the Sales Order field (character or text types only) where the Delivery Note '
            'from the e-commerce system will be recorded. By default, the system uses the field '
            'specified under the e-commerce tab of the Sales Order. However, you can designate '
            'any compatible field, including those from third-party modules.'
        ),
        required=True,
        default=lambda self: self.env.ref('integration.field_sale_order__integration_delivery_note').id,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "sale.order"), '
               '("ttype", "in", ("text", "char")) ]',
    )

    picking_delivery_note_field = fields.Many2one(
        string='Stock Picking Delivery Note Field',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help=(
            'Specify the Stock Picking field (character or text types only) to capture the '
            'Delivery Note from the e-commerce system. The "Note" field on Stock Picking is used '
            'as the standard field, but you have the flexibility to assign any compatible field, '
            'including those from third-party modules.'
        ),
        required=True,
        default=lambda self: self.env.ref('stock.field_stock_picking__note').id,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "stock.picking"), '
               '("ttype", "in", ("text", "char")) ]',
    )

    default_sales_team_id = fields.Many2one(
        string='Default Sales Team for Orders',
        comodel_name='crm.team',
        help=(
            'Select a default Sales Team to be automatically assigned to all Sales Orders '
            'imported from the e-commerce system.'
        ),
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]"
    )

    default_sales_person_id = fields.Many2one(
        string='Force Sales Person',
        comodel_name='res.users',
        help='Select a default Sales Person to be automatically assigned to all new orders.',
        check_company=True,
        domain="['|', ('id', '=', 1), '&', ('active', '=', True), '|', ('company_id', '=', False), ('company_id', '=', company_id)]"  # NOQA
    )

    customer_company_vat_field = fields.Many2one(
        string='VAT/Reg. Number Field',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help=(
            'Identify the field in the Company record (limited to character type fields) to store '
            'the Company\'s VAT/Registration Number from the e-commerce system. While the default '
            'is the VAT field on the Company, you can designate any custom field, '
            'including those from third-party modules.'
        ),
        required=False,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "res.partner"), '
               '("ttype", "=", "char") ]',
    )

    customer_company_vat_field_name = fields.Char(
        related='customer_company_vat_field.name',
    )

    customer_personal_id_field = fields.Many2one(
        string='Personal ID Field',
        comodel_name='ir.model.fields',
        ondelete='cascade',
        help=(
            'Specify the field in the Contact record (limited to character type fields) where the '
            'Personal ID Number from the e-commerce system will be stored. This is particularly '
            'important for localizations that require personal identification, '
            'such as the Italian market.'
        ),
        required=False,
        domain='[("store", "=", True), '
               '("model_id.model", "=", "res.partner"), '
               '("ttype", "=", "char") ]',
    )

    default_customer = fields.Many2one(
        string='Default Customer',
        comodel_name='res.partner',
        help=(
            'Specify a default customer in Odoo for orders that lack customer information in the '
            'E-Commerce System. This ensures all orders are associated with a customer record.'
        ),
    )

    external_order_field_mapping_ids = fields.One2many(
        comodel_name='external.order.field.mapping',
        inverse_name='integration_id',
        context={'active_test': False},
        string='External Order Field Mappings',
        help=(
            'Define the mapping between fields in the e-commerce system and Odoo. '
        )
    )

    def _compute_integration_type(self):
        for rec in self:
            rec.is_integration_shopify = rec.type_api == 'shopify'
            rec.is_integration_prestashop = rec.type_api == 'prestashop'
            rec.is_integration_magento_two = rec.type_api == 'magento2'
            rec.is_integration_woocommerce = rec.type_api == 'woocommerce'

    @property
    def is_no_api(self):
        self.ensure_one()
        return self.type_api == 'no_api'

    def _default_search_customer_fields_ids(self):
        return self.env['ir.model.fields'].sudo().search([
            ('model', '=', 'res.partner'),
            ('name', 'in', SEARCH_CUSTOMER_FIELDS),
        ])

    def _set_default_template_reference_id(self):
        """Redefine it"""
        if not self.is_no_api:
            return False

        self.template_reference_id = self.env.ref(
            'integration.integration_template_reference_no_api_private').id
        return bool(self.template_reference_id)

    def _set_default_product_reference_id(self):
        """Redefine it"""
        if not self.is_no_api:
            return False

        self.product_reference_id = self.env.ref(
            'integration.integration_product_reference_no_api_private').id
        return bool(self.product_reference_id)

    def _set_default_template_barcode_id(self):
        """Redefine it"""
        if not self.is_no_api:
            return False

        self.template_barcode_id = self.env.ref(
            'integration.integration_template_barcode_no_api_private').id
        return bool(self.template_barcode_id)

    def _set_default_product_barcode_id(self):
        """Redefine it"""
        if not self.is_no_api:
            return False

        self.product_barcode_id = self.env.ref(
            'integration.integration_product_barcode_no_api_private').id
        return bool(self.product_barcode_id)

    @api.onchange('search_customer_fields_ids')
    def _onchange_search_customer_fields_ids(self):
        self.use_search_customer_fields_ids \
            = len(self.search_customer_fields_ids) < len(SEARCH_CUSTOMER_FIELDS)

    @api.constrains('search_customer_fields_ids')
    def _check_search_customer_fields_ids(self):
        if not self.search_customer_fields_ids:
            raise ValidationError(_(
                'At least one search customer field must remain.\n\n'
                'You cannot remove all search customer fields from the configuration.'
            ))

    @api.onchange('import_order_enabled')
    def _onchange_import_order_enabled(self):
        """
        Check if the receive orders cron is available.
        """
        if self.type_api and not self.receive_orders_cron_id:
            return {
                'warning': {
                    'title': _('Warning'),
                    'message': _(
                        'The receive orders cron has been removed. \nPlease deactivate and '
                        'reactivate the integration to create a new cron automatically.'
                    ),
                }
            }

    @api.onchange('customer_company_vat_field')
    def _onchange_customer_company_vat_field(self):
        """
        Update the 'use_search_company_by_vat' field based on the 'customer_company_vat_field'.

        If the VAT field is not specified, automatically disable the option to search for
        companies by VAT.
        """
        if not self.customer_company_vat_field:
            self.use_vat_only_company_search = False

    @api.onchange('emails_for_failed_mapping_notifications')
    def _onchange_emails_for_failed_mapping_notifications(self):
        """
        Onchange method triggered when notify_on_failed_mapping field changes.
        It validates the email addresses and displays a warning for invalid ones.
        """
        if self.emails_for_failed_mapping_notifications:
            email_addresses = [
                email.strip() for email in self.emails_for_failed_mapping_notifications.split(',')
            ]

            invalid_emails = [
                email for email in email_addresses if email and not _is_valid_email(email)
            ]

            if invalid_emails:
                return {
                    'warning': {
                        'title': 'Invalid Email Addresses',
                        'message': f"Invalid email addresses: {', '.join(invalid_emails)}"
                    }
                }

    @api.onchange('type_api')
    def _onchange_type_api(self):
        if not self.type_api:
            return

        # A.
        self._set_default_advanced_fields()

        # B.
        self.set_settings_value(
            'decimal_precision',
            self.company_id.currency_id.decimal_places or '2',
        )

        # C.
        self.validate_barcode = self.type_api in ('prestashop', 'shopify')

    def _get_default_global_tracked_fields(self):
        return self.env['ir.model.fields'].sudo().search([
            ('model', 'in', ['product.template', 'product.product']),
            ('name', 'in', TRACKED_FIELDS_INITIAL_PRODUCT_EXPORT),
        ])

    @api.onchange('orders_cut_off_datetime')
    def _onchange_orders_cut_off_date(self):
        if self.orders_cut_off_datetime:
            self.last_receive_orders_datetime = self.orders_cut_off_datetime

    def _get_default_mandatory_fields(self):
        return self.env['ir.model.fields'].sudo().search([
            ('model', '=', 'product.product'),
            ('name', '=', 'default_code'),
        ])

    def _compute_lang_ids(self):
        Mapping = self.env['integration.res.lang.mapping']

        for rec in self:
            records = Mapping.search([
                ('language_id', '!=', False),
                ('integration_id', '=', rec.id),
            ]).mapped('language_id')

            rec.lang_ids = records.filtered('active')

    def _compute_request_order_url(self):
        host = self.get_base_url_config()
        db_name = self.env.cr.dbname
        ResConfig = self.env['res.config.settings']
        integration_api_key = ResConfig.get_integration_api_key() or ''

        pattern = f'{host}/{db_name}/integration/integration_id/external-order/' \
                  f'%order_id%?integration_api_key={integration_api_key}'
        for integration in self:
            url = pattern.replace('integration_id', str(integration.id))
            integration.request_order_url = url

    def _compute_next_inventory_synchronization_date(self):
        for integration in self.filtered('inventory_synchronization_cron_id'):
            cron = integration.inventory_synchronization_cron_id.sudo()
            integration.next_inventory_synchronization_date = cron.nextcall

    def _inverse_next_inventory_synchronization_date(self):
        for integration in self.filtered('inventory_synchronization_cron_id'):
            cron = integration.inventory_synchronization_cron_id.sudo()
            cron.nextcall = integration.next_inventory_synchronization_date

    def _get_weight_integration_fields(self):
        """
        This method returns list of product.ecommerce.field for import/export weight
        It should be overloaded in connector modules
        """
        return []

    def _compute_check_weight_uoms(self):
        odoo_weight_uom = self.env['product.template']._get_weight_uom_id_from_ir_config_parameter()
        odoo_weight_uom_name = normalize_uom_name(odoo_weight_uom.name)

        for integration in self:
            if integration.state != 'active':
                integration.check_weight_uoms = True
                continue

            # Check that import and export product weight are switched on
            fields_name = integration._get_weight_integration_fields()
            fields_ids = [self.env.ref(x).id for x in fields_name]

            fields_mapping = self.env['product.ecommerce.field.mapping'].search([
                ('integration_id', '=', integration.id),
                ('ecommerce_field_id', 'in', fields_ids),
                ('receive_on_import', '=', True),
                ('send_on_update', '=', True),
            ])
            if not fields_mapping:
                integration.check_weight_uoms = True
                continue
            try:
                adapter = self._build_adapter()
                ext_weight_uom = adapter.get_weight_uoms()
            except Exception:
                integration.check_weight_uoms = True
                continue

            ext_weight_uom = [normalize_uom_name(x) for x in ext_weight_uom]
            result = all([odoo_weight_uom_name == x for x in ext_weight_uom])
            integration.check_weight_uoms = result

    @api.depends('last_receive_orders_datetime')
    def _compute_last_receive_orders_datetime_str(self):
        for integration in self:
            if integration.last_receive_orders_datetime:
                value = integration.last_receive_orders_datetime.strftime(
                    DATETIME_FORMAT,
                )
            else:
                value = ''

            integration.last_receive_orders_datetime_str = value

    @api.depends('orders_cut_off_datetime')
    def _compute_orders_cut_off_datetime_str(self):
        for integration in self:
            if integration.orders_cut_off_datetime:
                value = integration.orders_cut_off_datetime.strftime(
                    DATETIME_FORMAT,
                )
            else:
                value = ''

            integration.orders_cut_off_datetime_str = value

    def _compute_orders_count(self):
        """
        Compute number of external files (orders)
        """
        for integration in self:
            integration.orders_count = self.env['sale.integration.input.file'].search_count([
                ('si_id', '=', integration.id),
            ])

    def _compute_orders_today_count(self):
        """
        Compute number of external files (orders) created today
        """
        for integration in self:
            integration.orders_today_count = self.env['sale.integration.input.file'].search_count([
                ('si_id', '=', integration.id),
                ('create_date', '>=', fields.Datetime.now() - timedelta(days=1)),
            ])

    def _compute_queued_or_failed_orders_count(self):
        """
        Compute number of queued or failed orders based on empty Sales Order ID
        """
        for integration in self:
            integration.queued_or_failed_orders_count = self.env['sale.integration.input.file'].search_count([  # NOQA
                ('si_id', '=', integration.id),
                ('order_id', '=', False),
            ])

    @api.model
    def _calc_qty_fields_selections(self):
        return [
            (field_name, self.env['product.product']._get_field_string(field_name))
            for field_name in PRODUCT_QTY_FIELDS
        ]

    def is_carrier_tracking_required(self):
        """
        Value for filtering `done order pickings`
        during `order_export_tracking()` method performing.
        """
        return False

    def open_webhooks_logs(self):
        tree = self.env.ref('base.ir_logging_tree_view')
        form = self.env.ref('base.ir_logging_form_view')

        integration_log = self.env['ir.logging'].search([
            ('type', '=', 'client'),
            ('level', '=', 'DEBUG'),
            ('line', '=', str(self)),
            ('dbname', '=', self.env.cr.dbname),
        ])

        return {
            'type': 'ir.actions.act_window',
            'name': 'Integration Webhook Logs',
            'res_model': 'ir.logging',
            'view_mode': 'list,form',
            'view_ids': [
                (0, 0, {'view_mode': 'list', 'view_id': tree.id}),
                (0, 0, {'view_mode': 'form', 'view_id': form.id}),
            ],
            'domain': [('id', 'in', integration_log.ids)],
            'target': 'current',
        }

    def _get_error_webhook_message(self, error):
        return _('Not Implemented!')

    def create_webhooks(self, raise_original=False):
        self.ensure_one()
        routes_dict = self.prepare_webhook_routes()
        external_ids = self.webhook_line_ids.mapped('external_ref')
        adapter = self._build_adapter()

        try:
            adapter.unlink_existing_webhooks(external_ids)
            data_dict = adapter.create_webhooks_from_routes(routes_dict)
        except Exception as ex:
            if raise_original:
                raise ex

            message = self._get_error_webhook_message(ex) if ex.args else ex
            message_wizard = self.env['message.wizard'].create({
                'message': '',
                'message_html': message,
            })
            return message_wizard.run_wizard('integration_message_wizard_html_form')

        return self.create_integration_webhook_lines(data_dict)

    def drop_webhooks(self):
        result = False
        external_ids = self.webhook_line_ids.mapped('external_ref')

        try:
            adapter = self._build_adapter()
            result = adapter.unlink_existing_webhooks(external_ids)
        except Exception as ex:
            if ex.args:
                result = ex.args[0]
            _logger.error(ex)
        finally:
            self.webhook_line_ids.unlink()

        return result

    def create_integration_webhook_lines(self, data_dict):
        vals_list = list()
        default_vals = {
            'integration_id': self.id,
            'original_base_url': self._get_base_url_or_debug(),
        }

        for (controller_method, name, technical_name), reference in data_dict.items():
            vals = {
                'name': name,
                'technical_name': technical_name,
                'controller_method': controller_method,
                'external_ref': reference,
                **default_vals,
            }
            vals_list.append(vals)

        self.webhook_line_ids.unlink()
        return self.env['integration.webhook.line'].create(vals_list)

    def prepare_webhook_routes(self):
        result = dict()
        routes = self._retrieve_webhook_routes()

        for controller_method, names in routes.items():
            for name_tuple in names:
                route = self._build_webhook_route(controller_method)
                key_tuple = (controller_method,) + name_tuple
                result[key_tuple] = route

        return result

    def get_base_url_config(self):
        """
        Copy of method from model.py
        """
        return self.env['ir.config_parameter'].sudo().get_param('web.base.url')

    def _get_base_url_or_debug(self):
        debug_url = config.options.get('localhost_debug_url')
        if debug_url:
            return debug_url  # Fake url, just for localhost coding and bedug
        return self.get_base_url_config()

    def _build_webhook_route(self, controller_method):
        db_name = self.env.cr.dbname
        base_url = self._get_base_url_or_debug()
        return f'{base_url}/{db_name}/integration/{self.type_api}/{self.id}/{controller_method}'

    def _retrieve_webhook_routes(self):
        _logger.error('Webhook routes are not specified for the "%s".', self.name)
        return dict()

    def _get_configuration_postfix(self):
        self.ensure_one()
        return self.type_api

    def action_active(self):
        self.ensure_one()
        self.action_check_connection(raise_success=False)
        self.state = 'active'

    def action_draft(self):
        self.ensure_one()
        self.state = 'draft'
        self.import_order_enabled = False

    def action_open_shop(self):
        return {
            'type': 'ir.actions.act_url',
            'url': self.adapter.admin_url,
            'target': 'new',
        }

    def action_check_connection(self, raise_success=True):
        self.ensure_one()
        self._ensure_settings()

        try:
            # Attempt to build the adapter and check the connection
            adapter = self._build_adapter()
            connection_ok = adapter.check_connection()
        except Exception as e:
            # Catch any exception and raise a more user-friendly error message
            raise UserError(_(
                'Connection test failed due to an unexpected error.\n\n'
                'Error: %s\n\n'
                'Please verify the connection settings and try again. If the issue persists, contact support.'
            ) % str(e)) from e

        if connection_ok:
            if raise_success:
                message = _("Connection test successful!")
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'message': message,
                        'type': 'success',
                        'sticky': False,
                    }
                }
        else:
            # Raise a user-friendly message for connection failure
            raise UserError(_(
                'Connection failed.\n\n'
                'Please verify your connection settings (e.g., API keys, URLs, and credentials) and try again. '
                'If the problem persists, please contact support.'
            ))

    def is_integration_cancel_allowed(self):
        """Currently it's a Shopify only feature"""
        return False

    def _get_cancel_order_view_id(self):
        """Specific for every integration"""
        return False

    def _ensure_settings(self):
        """Hook method for redefining"""
        pass

    def action_view_all_external_orders_data(self):
        """
        Open view with all integration files. Used on kanban card.
        """
        return {
            'type': 'ir.actions.act_window',
            'name': 'External Orders',
            'res_model': 'sale.integration.input.file',
            'view_mode': 'list,form',
            'domain': [('si_id', '=', self.id)],
            'target': 'current',
        }

    def action_view_queued_or_failed_external_orders_data(self):
        """
        Open view with all queued or failed integration files. Used on kanban card.
        """
        return {
            'type': 'ir.actions.act_window',
            'name': 'Queued or Failed Orders',
            'res_model': 'sale.integration.input.file',
            'view_mode': 'list,form',
            'domain': [
                ('si_id', '=', self.id),
            ],
            'target': 'current',
            'context': {
                'search_default_without_sales_order': True,
            },
        }

    @property
    def is_active(self):
        return self.state == 'active'

    @property
    def is_installed_website_sale(self):
        return self.is_module_installed('website_sale')

    @property
    def is_installed_sale_product_configurator(self):
        return self.is_module_installed('sale_product_configurator')

    @property
    def allow_bundle_creation(self):
        return self.product_bundle_policy == 'create_bundle'

    @property
    def adapter(self):
        self.ensure_one()
        return self._build_adapter()

    def advanced_inventory(self):
        """Using external multi source locations"""
        return False

    def update_crons_activity(self):
        for integration in self:
            if not integration.receive_orders_cron_id:
                cron = integration._create_receive_orders_cron()
                integration.receive_orders_cron_id = cron

            if not integration.inventory_synchronization_cron_id:
                cron = integration._create_inventory_cron()
                integration.inventory_synchronization_cron_id = cron

            # Set the activity of receive orders cron
            integration.receive_orders_cron_id.active = (
                integration.state == 'active' and integration.import_order_enabled
            )

            # Set the activity of inventory synchronization
            integration.inventory_synchronization_cron_id.active = (
                integration.state == 'active'
                and integration.synchronize_all_inventory_periodically
            )

    def _create_receive_orders_cron(self):
        self.ensure_one()
        vals = {
            'name': f'Integration: {self.name} [{self.id}] Receive Orders',
            'model_id': self.env.ref('integration.model_sale_integration').id,
            'interval_type': 'minutes',
            'interval_number': 5,
            'code': f'model.browse({self.id}).integration_receive_orders_cron()',
            'user_id': SUPERUSER_ID,
        }
        return self.env['ir.cron'].create(vals)

    def _create_inventory_cron(self):
        self.ensure_one()

        # Calculating time in UTC for nextcall in 00:00:000 by users time zone
        tz = self.env.user.tz and pytz.timezone(self.env.user.tz) or pytz.utc
        now_user = datetime.now(tz=tz).replace(minute=0, second=0, tzinfo=None)
        now_utc = datetime.now().replace(minute=0, second=0)
        diff = now_user - now_utc
        nextcall = now_utc.replace(hour=0) - diff + timedelta(days=1)

        vals = {
            'name': f'Integration: {self.name} [{self.id}] Synchronize inventory',
            'model_id': self.env.ref('integration.model_sale_integration').id,
            'interval_type': 'days',
            'interval_number': 1,
            'code': f'model.browse({self.id}).integrationApiExportInventory()',
            'nextcall': nextcall.strftime('%Y-%m-%d %H:%M:%S'),
            'user_id': SUPERUSER_ID,
        }
        return self.env['ir.cron'].create(vals)

    def unlink(self):
        self.receive_orders_cron_id.unlink()
        self.inventory_synchronization_cron_id.unlink()
        return super(SaleIntegration, self).unlink()

    def get_class(self):
        """It's just a stub."""
        return NoAPIClient

    @api.model
    def get_integrations(self, job_name, company_id=False):
        domain = [('state', '=', 'active')]

        if job_name:
            domain.append((f'{job_name}_job_enabled', '=', True))

        if company_id:
            domain.append(('company_id', '=', company_id))

        return self.search(domain)

    @api.model
    def get_active_integrations(self):
        integrations = self.search([('state', '=', 'active')])
        return [{'id': x.id, 'name': x.name} for x in integrations]

    def job_enabled(self, name):
        self.ensure_one()

        if not self.is_active:
            return False

        return self[f'{name}_job_enabled']

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)

        for integration, vals in zip(res, vals_list):
            integration.write_settings_fields(vals)
            integration.create_fields_mapping_for_integration()
            integration._set_default_advanced_fields()
            integration.update_crons_activity()

        return res

    @track_changes(
        include_related_fields=[
            'field_ids',
            'location_line_ids',
            'order_metafield_mapping_ids',
            'customer_metafield_mapping_ids',
            'external_order_field_mapping_ids',
        ],
        sensitive_fields=['key', 'consumer_key', 'consumer_secret', 'wp_app_password'],
        exclude_fields=['last_receive_orders_datetime', 'last_update_pricelist_items'],
    )
    def write(self, vals):
        if not self:
            # If we have no records, we can't do anything
            return super().write(vals)

        res = super().write(vals)

        # 1. Write settings fields
        ctx = self.env.context.copy()
        if not ctx.get('skip_write_settings_fields'):
            res = self.write_settings_fields(vals)

        # 2. Update advanced fields
        if 'change_advanced_fields' in vals:
            if self.change_advanced_fields:
                self._fill_default_advanced_fields()
            else:
                self._set_default_advanced_fields()

        # 3. Update crons
        if 'synchronize_all_inventory_periodically' in vals or 'state' in vals:
            self.update_crons_activity()

        # 4. Invalidate Cache
        if 'use_async' in vals:
            self.invalidate_integration_cache()

        return res

    def create_fields_mapping_for_integration(self):
        self.ensure_one()

        field_ids = self.env['product.ecommerce.field'].search([
            ('type_api', '=', self.type_api),
            ('is_private', '=', False),
        ])

        for field in field_ids:
            field._create_mapping(self.id)

        return True

    def write_settings_fields(self, vals):
        self.ensure_one()

        res = True
        if 'type_api' in vals:
            settings_fields = self.get_default_settings_fields(vals['type_api'])
        else:
            settings_fields = self.get_default_settings_fields()

        if settings_fields is not None and settings_fields:
            exists_fields = self.field_ids.to_dictionary()
            settings_fields = self.convert_settings_fields(settings_fields)
            fields_list_to_add = [
                (0, 0, {
                    'name': field_name,
                    'description': field['description'],
                    'value': field['value'],
                    'eval': field['eval'],
                    'is_secure': field['is_secure'],
                })
                for field_name, field in settings_fields.items()
                if field_name not in exists_fields
            ]
            if fields_list_to_add:
                new_fields = {
                    'field_ids': fields_list_to_add
                }
                ctx = self.env.context.copy()
                ctx['skip_write_settings_fields'] = True
                res = self.with_context(ctx).write(new_fields)

        return res

    def get_settings_value(self, key, default_value=None):
        self.ensure_one()
        field = self.get_settings_field(key, default_value is None)

        if not field:
            return default_value

        value = field.value
        if value and field.eval:
            value = safe_eval(value)
        return value

    def set_settings_value(self, key, value, to_string=False):
        self.ensure_one()
        if to_string:
            value = str(value)
        field = self.get_settings_field(key)
        field.value = value

    def get_settings_field(self, key, raise_error=True):
        self.ensure_one()

        field = self.field_ids.filtered(lambda x: x.name == key)
        if field:
            return field

        # If field was not found the first time can be that this
        # is new setting and we need to add default value
        self.write_settings_fields({})

        field = self.field_ids.filtered(lambda x: x.name == key)

        if not field and raise_error:
            raise UserError(_(
                'The settings field with the key "%s" is missing.\n\n'
                'This issue may have been caused by recent changes to the settings, such as accidentally removing '
                'the field. Please re-add the field manually in the General tab of the integration settings.\n\n'
                'If you are unsure how to resolve this or the issue persists, please contact '
                'support: https://support.ventor.tech/'
            ) % key)

        return field

    def _ensure_not_null_setting(self, key_list: list):
        result = list()

        for key in key_list:
            field = self.get_settings_field(key, raise_error=False)

            if not field.value:
                raise UserError(_(
                    'The "%s" settings field is required for the integration "%s".\n\n'
                    'Please go to the "Quick Configuration" wizard in the integration settings to complete '
                    'the connection setup. Ensure that all required fields are filled in properly for '
                    'the integration to function correctly.\n\n'
                    'If you are unsure how to resolve this or the issue persists, please contact '
                    'support: https://support.ventor.tech/'
                ) % (key, self.name))

            result.append(field.value)

        return result

    def get_hash(self):
        values = self.field_ids.mapped('value')
        return hash(tuple(values))

    def increment_sync_token(self):
        field = self.get_settings_field('adapter_version')
        field.value = str(time())
        return field.value

    def invalidate_integration_cache(self):
        for rec in self:
            rec.increment_sync_token()

            wizard = rec._get_configuration_wizard()
            wizard.unlink()

    def _truncate_settings_url(self):
        self.ensure_one()
        full_settings_url = self.get_settings_value('url')

        # Cut off `https://`
        settings_url_list = full_settings_url.strip('/').split('//')
        settings_url = settings_url_list[-1]

        # Get `host` only
        settings_url_list = settings_url.split('/')
        settings_url = settings_url_list[0]

        if settings_url.startswith('www.'):
            settings_url = settings_url.lstrip('www.')
        return settings_url

    def convert_external_tax_to_odoo(self, tax_id):
        """Expected its own implementation for each integration."""
        return False

    @api.model
    def convert_settings_fields(self, settings_fields):
        return {
            field[0]: {
                'name': field[0],
                'description': field[1],
                'value': field[2],
                'eval': field[3] if len(field) > 3 else False,
                'is_secure': field[4] if len(field) > 4 else False,
            }
            for field in settings_fields
        }

    @staticmethod
    def get_external_block_limit():
        return IMPORT_EXTERNAL_BLOCK

    @api.model
    def get_default_settings_fields(self, type_api=None):
        return getattr(self.get_class(), 'settings_fields')

    def get_integration_location(self):
        self.ensure_one()

        if not self.location_line_ids:
            wh_ids = self.env['stock.warehouse'].search([
                ('company_id', '=', self.company_id.id)
            ])
            return wh_ids.mapped('lot_stock_id')

        return self.location_line_ids.mapped('erp_location_id')

    def initial_import_taxes(self):
        self.integrationApiImportTaxes()

    def initial_import_attributes(self):
        self.integrationApiImportAttributes()
        self.integrationApiImportAttributeValues()

    def initial_import_features(self):
        self.integrationApiImportFeatures()
        self.integrationApiImportFeatureValues()

    def initial_import_countries(self):
        self.integrationApiImportCountries()
        self.integrationApiImportStates()

    def integrationApiImportData(self):
        name = self.name
        self = self.with_context(company_id=self.company_id.id)

        job = self.with_delay(
            description=f'{name}: Initial Import. Languages',
            priority=2,
        ).integrationApiImportLanguages()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Shipping Methods',
            priority=2,
        ).integrationApiImportDeliveryMethods()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Taxes',
            priority=2,
        ).initial_import_taxes()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Payment Methods',
            priority=2,
        ).integrationApiImportPaymentMethods()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Attributes',
            priority=2,
        ).initial_import_attributes()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Features',
            priority=2,
        ).initial_import_features()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Countries',
            priority=2,
        ).initial_import_countries()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Categories',
            priority=2,
        ).integrationApiImportCategories()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Store Order Statuses',
            priority=2,
        ).integrationApiImportSaleOrderStatuses()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Pricelists',
            priority=2,
        ).integrationApiImportPricelists()
        self.job_log(job)

        job = self.with_delay(
            description=f'{name}: Initial Import. Locations',
            priority=2,
        ).integrationApiImportLocations()
        self.job_log(job)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Initial Import'),
                'message': 'Queue Jobs "Initial Import" are created',
                'type': 'success',
                'sticky': False,
            }
        }

    def action_import_master_data(self):
        return self.integrationApiImportData()

    def action_import_product_from_external(self):
        """
        The `Link Products` button.

        This action does not generate new product entries in Odoo. It's designed to
        establish links for products that already exist in both systems. If a product in your
        e-commerce system doesn't have a corresponding match in Odoo, it won't be created
        automatically. For these products, you'll need to navigate to 'Mappings → Products'
        and use the "Import Product" button to create and link them manually in Odoo.
        """
        action, tmpl_hub = self._validate_product_templates(False)
        if action:
            return action

        external_ids = tmpl_hub.get_template_ids()

        job_kwargs = {
            'priority': 2,
            'description': f'{self.name}: Initial Products Import (prepare products batches)',
        }

        job = self \
            .with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .integrationApiImportProducts(external_ids)

        self.job_log(job)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Initial Products Import'),
                'message': 'Queue Jobs "Initial Products Import" are created',
                'type': 'success',
                'sticky': False,
            }
        }

    def action_create_products_in_odoo(self):
        action, __ = self._validate_product_templates(False)  # TODO, maybe it's not necessary
        if action:
            return action

        job_kwargs = {
            'priority': 4,
            'description': f'{self.name}: Create Products In Odoo (prepare products batches)',
        }
        job = self \
            .with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .integrationApiCreateProductsInOdoo()

        self.job_log(job)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Create Products In Odoo'),
                'message': 'Queue Jobs "Create Products In Odoo" are created',
                'type': 'success',
                'sticky': False,
            }
        }

    def action_import_related_products(self):
        adapter = self._build_adapter()
        # Fetch data.
        adapter_products, template_router = adapter.get_products_for_accessories()

        model_name = 'product.template'
        ProductTemplateExternal = self.env[f'integration.{model_name}.external']
        ProductTemplateMapping = self.env[f'integration.{model_name}.mapping']
        mappings = self.env[f'integration.{model_name}.mapping']
        internal_field_name, external_field_name = ProductTemplateMapping._mapping_fields
        MessageWizard = self.env['message.wizard']

        # Create / update external and mappings.
        for product in adapter_products:
            name = product['name']
            # Get translation if name contains different languages
            if isinstance(name, dict) and name.get('language'):
                original, __ = ProductTemplateExternal.get_original_and_translation(name, self)

                if original:
                    name = original

            external_record = ProductTemplateExternal.create_or_update({
                'integration_id': self.id,
                'code': product['id'],
                'name': name,
                'external_reference': product.get('external_reference'),
            })
            external_record._post_import_external_one(product)

            mapping = ProductTemplateMapping.search([
                ('integration_id', '=', self.id),
                (external_field_name, '=', external_record.id),
            ])
            if not mapping:
                mapping = ProductTemplateMapping.create({
                    'integration_id': self.id,
                    external_field_name: external_record.id,
                })

            mappings |= mapping

        if not mappings:
            message_wizard = MessageWizard.create({
                'message': _('No related products to synchronize.'),
            })
            return message_wizard.run_wizard('integration_message_wizard_form')

        mappings_to_fix = mappings.filtered(lambda x: not getattr(x, internal_field_name))

        # Fix unmapped records if necessary. Format message.
        if mappings_to_fix:
            message = _(
                'Some of the related products are not yet synchronised to Odoo or not yet mapped '
                'to corresponding Odoo Products so it is not possible to import them. '
                'Please, make sure to launch products synchronisation again and make sure '
                'to map products in menu "Mappings → Products" '
                '(or create them in Odoo by clicking "Import Products" button in the same menu):'
            )
            mapping_names = mappings_to_fix.mapped(f'{external_field_name}.display_name')

            html_message = f'<div>{message}</div>'
            html_names = f'<ul>{"".join([f"<li>{x}</li>" for x in mapping_names])}</ul>'

            message_wizard = MessageWizard.create({
                'message': str(mappings_to_fix.ids),
                'message_html': html_message + '<br/>' + html_names,
            })
            return message_wizard.run_wizard('integration_message_wizard_form_mapping_product')

        # Assign related products to the parent product.
        templates = self.env[model_name]
        for template_external_id, related_products_ids in template_router.items():
            template = templates.from_external(self, template_external_id, False)

            optional_product_ids = self.env[model_name]
            for product_id in related_products_ids:
                optional_product_ids |= templates.from_external(self, product_id, False)

            template.optional_product_ids = [(6, 0, optional_product_ids.ids)]
            templates |= template

        # Summary. Format message.
        mapping_names = list()
        base_url = self.sudo().env['ir.config_parameter'].get_param('web.base.url')
        pattern = (
            '<a href="%s/web#id=%s&model=%s&view_type=form" target="_blank">%s</a>'
        )

        def _format_optional_products(template):
            names = template.optional_product_ids.mapped('name')
            html_names = f'<ul>{"".join([f"<li>{x}</li>" for x in names])}</ul>'
            template_name = pattern % (base_url, template.id, model_name, template.name)
            return f'<li>{template_name + html_names}</li>'

        for template in templates:
            mapping_names.append(
                _format_optional_products(template)
            )

        message = _('The Products were synchronized:\n%s') % (f'<ul>{"".join(mapping_names)}</ul>')

        message_wizard = MessageWizard.create({
            'message': '',
            'message_html': message,
        })
        return message_wizard.run_wizard('integration_message_wizard_html_form')

    def integrationApiImportDeliveryMethods(self):
        external_records, adapter_external_data = self._import_external(
            'integration.delivery.carrier.external',
            'get_delivery_methods',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportTaxes(self):
        external_records, adapter_external_data = self._import_external(
            'integration.account.tax.external',
            'get_taxes',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportPaymentMethods(self):
        external_records, adapter_external_data = self._import_external(
            'integration.sale.order.payment.method.external',
            'get_payment_methods',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportLanguages(self):
        external_records, adapter_external_data = self._import_external(
            'integration.res.lang.external',
            'get_languages',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportAttributes(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.attribute.external',
            'get_attributes',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportAttributeValues(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.attribute.value.external',
            'get_attribute_values',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportFeatures(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.feature.external',
            'get_features',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportFeatureValues(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.feature.value.external',
            'get_feature_values',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportPricelists(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.pricelist.external',
            'get_pricelists',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportLocations(self):
        external_records, __ = self._import_external(
            'integration.stock.location.external',
            'get_locations',
        )
        return external_records

    def integrationApiImportCountries(self):
        external_records, adapter_external_data = self._import_external(
            'integration.res.country.external',
            'get_countries',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportStates(self):
        external_records, adapter_external_data = self._import_external(
            'integration.res.country.state.external',
            'get_states',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportCategories(self):
        external_records, adapter_external_data = self._import_external(
            'integration.product.public.category.external',
            'get_categories',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportProducts(self, external_ids=None):
        block = 1
        limit = self.get_external_block_limit()
        template_ids = external_ids or self.adapter.get_product_template_ids()
        self = self.with_context(company_id=self.company_id.id)

        description_ = (
            f'{self.name}: Initial Products Import: Import Products Batch '
            '(create external records + auto-matching) [block %s]'
        )
        while template_ids:
            job = self.with_delay(
                priority=2,
                description=(description_ % block),
            ).import_external_product(template_ids[:limit])

            self.job_log(job)

            block += 1
            template_ids = template_ids[limit:]

    def integrationApiImportSaleOrderStatuses(self):
        external_records, adapter_external_data = self._import_external(
            'integration.sale.order.sub.status.external',
            'get_sale_order_statuses',
        )
        external_records._map_external(adapter_external_data)
        return external_records

    def integrationApiImportSingleCustomer(self):
        if not self.temp_external_id:
            return False

        customer_records = self.import_single_customer(self.temp_external_id)
        return customer_records

    def integrationApiPrintFields(self):
        # TODO: Deprecated? We have Import/Export wizard now.
        params = self.to_dictionary()
        params = json.dumps(params, indent=4, default=str)
        raise UserError(str(params))

    def integrationApiReceiveOrdersKwargs(self):
        kwargs = self.adapter.order_fetch_kwargs()
        raise UserError(str(kwargs))

    def integrationApiReceiveOrder(self):
        if not self.temp_external_id:
            return False

        return self.with_context(skip_create_order_from_input=True) \
            .fetch_order_by_id(self.temp_external_id)

    def integrationApiReceiveExternalProduct(self):
        if not self.temp_external_id:
            return False
        external_product = self.import_external_product(self.temp_external_id)
        return external_product

    def integrationApiCalculateProductQty(self):
        if not self.temp_external_id:
            return False

        result_text = self._prepare_calculation_qty_with_bom(self.temp_external_id)
        wizard = self.env['message.wizard'].create({'message': result_text})
        return wizard.run_wizard('integration_message_wizard_form')

    def _prepare_calculation_qty_with_bom(self, product_id):
        """
        1. Receives an ID of product.product
        2. Checks BOM lines to see how many units can be produced
        3. Builds a line-by-line explanation of required vs. available quantities
        4. Shows the final producible quantity in a UserError dialog
        """

        try:
            product_id = int(product_id)
        except ValueError:
            raise UserError(_('Invalid product ID: %s') % product_id)

        product = self.env['product.product'].browse(product_id)
        if not product:
            raise UserError(_(f'No product found with ID {product_id}.'))

        qty_field = self.synchronise_qty_field
        manufacture_boms = product.bom_ids.filtered(lambda b: b.type == 'normal')
        if not manufacture_boms:
            msg_no_bom = _(
                f'Product "{product.display_name}" (ID: {product.id}) has no manufacturing BOM.\n'
                f'Quantity for this product: {getattr(product, qty_field, 0)}'
            )
            raise UserError(msg_no_bom)

        manufacture_bom = manufacture_boms[0]

        msg_lines = []
        msg_lines.append(_(
            f'Calculating producible quantity for product variant:\n'
            f'  • Product Name: {product.display_name} (ID: {product.id})\n'
            f'  • Product UOM: {product.uom_id.display_name}\n'
            f'  • Using BOM: {manufacture_bom.display_name} (ID: {manufacture_bom.id})\n'
            f'  • BOM Qty: {manufacture_bom.product_qty}\n'
            f'  • BOM UOM: {manufacture_bom.product_uom_id.display_name}\n'
        ))

        # We'll keep track of each component's “maximum possible batches”
        # and then find the minimum of these across all BOM lines.
        min_possible_batches = None

        for bom_line in manufacture_bom.bom_line_ids:
            component = bom_line.product_id
            required_qty = bom_line.product_qty
            msg_lines.append(_(
                f'BOM Line:\n'
                f'  • Component Name: {component.display_name} (ID: {component.id})\n'
                f'  • Component Qty: {component[qty_field]}\n'
                f'  • Component UOM: {component.uom_id.display_name}\n'
                f'  • BOM Line Qty: {required_qty}\n'
                f'  • BOM Line UOM: {bom_line.product_uom_id.display_name}'
            ))

            # Skip consumables and services
            if component.type == 'service' or \
                    (component.type == 'consu' and not component.is_storable and not component.bom_ids):
                msg_lines.append(_('\tComponent is a consumable or service. Excluded from production calculation.\n'))
                continue

            if component.bom_ids and component.type in ['product', 'consu']:
                available_component_qty = component._compute_qty_producible(qty_field)
                msg_lines.append(_(
                    f'\tComponent has its own BOM.\n'
                    f'  • Recursively computing producible qty for this component: {available_component_qty}\n'
                    f'  • Returned value: {available_component_qty}\n'
                ))
            else:
                available_component_qty = getattr(component, qty_field, 0)

            # Convert units if the BOM line's UOM != component's UOM
            if bom_line.product_uom_id != component.uom_id:
                original_available_component_qty = available_component_qty
                available_component_qty = component.uom_id._compute_quantity(
                    available_component_qty, bom_line.product_uom_id
                )
                msg_lines.append(_(
                    f'\tComponent has a different UOM than the BOM line.\n'
                    f'\t  • Converted UOM: {component.uom_id.display_name} → {bom_line.product_uom_id.display_name}\n'
                    f'\t  • Converted value: {original_available_component_qty} → {available_component_qty}'
                ))

            # If required_qty is 0, it doesn't constrain production. But typically BOM lines have > 0.
            # We'll handle the scenario anyway.
            if not required_qty:
                # If required is 0, no limit is introduced by this line
                possible_batches = float('inf')
            else:
                possible_batches = available_component_qty // required_qty

            # Update our min_possible_batches across all lines
            if min_possible_batches is None:
                min_possible_batches = possible_batches
            else:
                min_possible_batches = min(min_possible_batches, possible_batches)

            msg_lines.append(_(
                f'  • Available Qty: {available_component_qty}\n'
                f'  • Possible Batches for this component: {possible_batches}\n'
            ))

        base_qty = getattr(product, qty_field, 0)

        # If we can produce “min_possible_batches” BOM sets, each BOM set yields `manufacture_bom.product_qty`
        # finished units of the main product.
        produced_qty = 0
        if min_possible_batches and min_possible_batches != float('inf'):
            produced_qty = min_possible_batches * manufacture_bom.product_qty
            msg_lines.append(_(
                f'Minimum possible batches across all components: {min_possible_batches}\n'
                f'Minimum possible batches {min_possible_batches} * BOM Qty {manufacture_bom.product_qty} ='
                f' {produced_qty}\n'
                f'Produced quantity: {produced_qty}\n'
            ))

        if product.uom_id != manufacture_bom.product_uom_id:
            original_produced_qty = produced_qty
            produced_qty = manufacture_bom.product_uom_id._compute_quantity(
                produced_qty, product.uom_id
            )
            msg_lines.append(_(
                f'Product "{product.display_name}" (ID: {product.id}) has a different UOM than the BOM line.\n'
                f'  • Converted UOM: {manufacture_bom.product_uom_id.display_name} → {product.uom_id.display_name}\n'
                f'  • Converted value: {original_produced_qty} → {produced_qty}'
            ))

        final_qty = base_qty + produced_qty

        msg_lines.append(_(
            f'\nBase available quantity of "{product.display_name}": {base_qty}\n'
            f'Total additional producible quantity: {produced_qty}\n'
            '--------------------------------------------------------\n'
            f'FINAL QUANTITY: {final_qty}\n'
            f'--------------------------------------------------------\n'
            f'The value returned by the method (for comparison): {product._compute_qty_producible(qty_field)}\n'
        ))

        msg_text = '\n'.join(msg_lines)
        return msg_text

    def run_create_products_in_odoo_by_blocks(self, external_templates):
        return external_templates.run_import_products()

    def integrationApiCreateProductsInOdoo(self):
        block = 1
        limit = self.get_external_block_limit()
        external_templates = self.env['integration.product.template.external'].search([
            ('integration_id', '=', self.id),
        ])

        self = self.with_context(company_id=self.company_id.id)
        description_ = f'{self.name}: Create Products In Odoo. Prepare Products For Creating [block %s]'

        while external_templates:
            _external_ids = external_templates[:limit]

            job = self.with_delay(
                priority=5,
                description=(description_ % block),
            ).run_create_products_in_odoo_by_blocks(_external_ids)

            _external_ids.job_log(job)

            block += 1
            external_templates = external_templates[limit:]

    def integrationApiProductsValidationTest(self):
        action, __ = self._validate_product_templates(True)
        return action

    def run_import_customers_by_blocks(self, exeternal_customer_ids):
        self = self.with_context(company_id=self.company_id.id)

        result = []
        for customer_id in exeternal_customer_ids:
            job_kwargs = self._job_kwargs_import_single_customer(customer_id)
            job = self.with_delay(**job_kwargs).import_single_customer(customer_id)

            self.job_log(job)
            result.append(job)

        return result

    def import_single_customer(self, external_customer_id):
        """
        Import a single customer along with their addresses.
        Args:
            external_customer_id (str): External ID of the customer.
        Returns:
            List: Imported partners (customer and their addresses).
        """
        adapter = self._build_adapter()

        customer, addresses = adapter.get_customer_and_addresses(external_customer_id)

        billing_addresses = list(filter(lambda x: x.get('type') in ('invoice', None), addresses))
        shipping_addresses = list(filter(lambda x: x.get('type') == 'delivery', addresses))

        imported_contacts = self.env['res.partner']
        for i, billing_address in enumerate(billing_addresses):
            shipping_address = shipping_addresses[i] if i < len(shipping_addresses) else None

            PartnerFactory = self.env['integration.res.partner.factory'].create_factory(
                self.id,
                customer_data=customer,
                billing_data=billing_address,
                shipping_data=shipping_address,
                is_initial_import=True,
            )

            partner, addresses = PartnerFactory.get_partner_and_addresses()

            imported_contacts |= partner
            imported_contacts |= addresses['billing']
            imported_contacts |= addresses['shipping']

        return imported_contacts

    def _fetch_external_tax(self, tax_id):
        adapter = self._build_adapter()
        adapter_data = adapter.get_single_tax(tax_id)
        return adapter_data

    def _import_external_tax(self, tax_id):
        adapter_external_data = self._fetch_external_tax(tax_id)

        if not adapter_external_data:
            return None

        external_record = self._import_external_record(
            self.env['integration.account.tax.external'],
            adapter_external_data,
        )
        mapping = external_record.create_or_update_mapping()

        return mapping._fix_unmapped_tax_one(external_data=adapter_external_data)

    def _fetch_external_carrier(self, carrier_data):
        return carrier_data

    def _import_external_carrier(self, carrier_data):
        adapter_external_data = self._fetch_external_carrier(carrier_data)

        external_record = self._import_external_record(
            self.env['integration.delivery.carrier.external'],
            adapter_external_data,
        )
        mapping = external_record.create_or_update_mapping()

        return mapping._fix_unmapped_shipping_one()

    def import_external_product(self, template_ids):
        """
        (1) receive actual product data
        (2) create or update externals/mappings
        (3) try to map external records (integration.product.template.mapping)
        """

        external_templates, external_variants, error_list = self._import_external_product(template_ids)

        message = ''

        if external_templates or external_variants:
            message += _('======SUCCESS======\n%s, %s\n\n') % (external_templates, external_variants)

        if error_list:
            message += _('======FAIL======\n%s') % '\n\n'.join(error_list)

        return message

    def _import_external_product(self, template_ids):
        if not isinstance(template_ids, list):
            template_ids = [template_ids]

        template_ids = [str(x) for x in template_ids]

        ExternalTemplate = self.env['integration.product.template.external']
        ExternalVariant = self.env['integration.product.product.external']

        external_templates = ExternalTemplate.browse()
        external_variants = ExternalVariant.browse()
        errors = []

        ext_templates_data = self.adapter.get_product_templates(template_ids)
        if not ext_templates_data:
            return external_templates, external_variants, errors

        for template_data in ext_templates_data.values():
            external_template = self._import_external_record(ExternalTemplate, template_data)
            external_template.create_or_update_mapping()
            external_templates |= external_template

            for variant_data in template_data['variants']:
                external_variant = self._import_external_record(ExternalVariant, variant_data)
                external_variant.create_or_update_mapping()
                external_variants |= external_variant

            if not template_data['variants']:
                # Create default external variant with "complex-zero" code like `100-0`
                default_external_variant = external_template._create_default_external_variant()
                default_external_variant.create_or_update_mapping()
                external_variants |= default_external_variant

            try:
                external_template.try_map_template_and_variants(template_data)
            except (ApiImportError, NotMappedToExternal, NotMappedFromExternal, AssertionError) as e:
                errors = errors or [_('Errors when trying to auto-match products:')]
                errors.append(str(e))

        return external_templates, external_variants, errors

    def _import_external_record(self, external_model, external_data):
        name = external_data.get('name')

        # Get translation if name contains different languages
        if isinstance(name, dict) and name.get('language'):
            original = external_model.get_original_name(name, self)

            if original:
                name = original

        if not name:
            name = external_data['id']

        if not external_model._pre_import_external_check(external_data, self):
            return external_model

        result = external_model.create_or_update({
            'integration_id': self.id,
            'code': external_data['id'],
            'name': name,
            'external_reference': external_data.get('external_reference'),
        })
        result._post_import_external_one(external_data)

        return result

    def _import_external(self, model, method, external_data=None):
        if not external_data:
            adapter = self._build_adapter()
            external_data = getattr(adapter, method)()

        external_records = self.env[model]
        for data in external_data:
            external_records |= self._import_external_record(external_records, data)

        external_records._post_import_external_multi(external_data)
        return external_records, external_data

    def export_template(self, template, export_images=False, make_validation=False, force=False):
        """
        Be careful, this method have to be private because of there are many external validations
        before its calling. Urgently recommend use the `template.trigger_export(*args, **kw)`
        """
        self.ensure_one()
        template.ensure_one()
        assert template.exists(), _(
            'Product Template with Id = %s doesn\'t exist or has been deleted' % template.id
        )

        _logger.info(
            '%s: Integration `export_template` started with params: %s, %s, %s, %s',
            self.name,
            template,
            f'export_images={export_images}',
            f'make_validation={make_validation}',
            f'force={force}',
        )

        self = self.with_context(company_id=self.company_id.id)

        template = template.with_context(
            lang=self.get_integration_lang_code(),
            default_integration_id=self.id,
        )

        if make_validation:
            template.validate_in_odoo(self, raise_error=True)

        template_for_export = template.to_export_format(self)
        # Now let's validate template in external system
        # In case we will be returned with external records to delete
        # we need to clean up and trigger export job again
        adapter = self._build_adapter()
        ext_records_to_delete, ext_records_to_update = adapter.validate_template(
            template_for_export,
        )

        if ext_records_to_delete:
            # Unlink found incorrect external records (external mappings)
            for record in ext_records_to_delete:
                model_name = record['model']
                external_record = self.env[f'integration.{model_name}.external'] \
                    .get_external_by_code(self, record['external_id'], raise_error=False)

                if external_record:
                    external_record.unlink()

            # Trigger export again and finish current task
            job_kwargs = self._job_kwargs_export_template(template, export_images, force=force)

            job = self.with_delay(**job_kwargs).export_template(
                template,
                export_images=export_images,
                make_validation=make_validation,
                force=force,
            )
            template.job_log(job)

            return _('Some products didn\'t exists in external system. '
                     'External records where cleaned up and export was triggered again ')

        # Now let's check if such product already exist in external system
        # so instead of creating new we can import existing one
        existing_external_product_id = adapter.find_existing_template(template_for_export)

        if existing_external_product_id:
            external_record = self.env['integration.product.template.external'].create_or_update({
                'integration_id': self.id,
                'code': existing_external_product_id,
            })
            external_record.create_or_update_mapping(odoo_id=template.id)

            job_kwargs = self._job_kwargs_import_product(external_record.code, external_record.name)
            job = self.with_delay(**job_kwargs).import_product(external_record.id)

            template.job_log(job)

            return _('Existing Product found in external system with id %s. Triggering job to '
                     'import product instead of exporting it') % existing_external_product_id

        # 0. START EXPORT
        results_list = []
        t_mapping, *v_mapping_list = adapter.export_template(template_for_export)

        external_template_id, __ = self._handle_mapping_data(
            template, t_mapping, v_mapping_list, ext_records_to_update,
        )

        results_list.append(
            _('SUCCESS! Product Template "%s" was exported successfully. Product Template Code in '
              'external system is "%s"') % (template.name, external_template_id.code)
        )

        # Determine the `force_export` flag
        first_time_export = not bool(template_for_export['external_id'])
        force_export = force or first_time_export

        # 1. Pricelists export.
        # Note: Always check the `pricelist_integration` property
        if self.pricelist_integration and force_export:
            # Do it in separate job in order to avoid rollback.
            job_kwargs = self._job_kwargs_export_specific_prices_template(template)
            job = self.with_delay(**job_kwargs).export_pricelists_one(template)
            template.job_log(job)

            results_list.append(LOG_SEPARATOR)
            results_list.append(_('Export Pricelists job was created.'))

        # 2. Images export.
        # Note: Always check the `allow_export_images` property
        if self.allow_export_images and (export_images or force_export):
            # Do it in separate job in order to avoid rollback.
            job_kwargs = self._job_kwargs_export_images(template)
            job = self.with_delay(**job_kwargs).export_template_images_verbose(template.id)
            template.job_log(job)

            results_list.append(LOG_SEPARATOR)
            results_list.append(_('Export Images job was created.'))

        # 3. Inventory export.
        # Note: Always check the `export_inventory_job_enabled` property
        if self.job_enabled('export_inventory') and force_export:

            # Check if inventory export is required
            results_list.append(LOG_SEPARATOR)

            if self._should_export_inventory(template):
                if template.exclude_from_synchronization_stock:
                    results_list.append(
                        _('Inventory export was skipped because the product is excluded from stock synchronization.')
                    )
                else:
                    # Export inventory in a separate job to avoid rollback in case of transaction errors
                    template._export_inventory_on_template(self)
                    results_list.append(_('Export Inventory job was created.'))
            else:
                results_list.append(
                    _('Inventory export was skipped because the product is not a product or a consumable with a BOM.')
                )

        # Update timestamp export for the external record
        external_record = template.to_external_record(self)
        external_record.update_timestamp_export()

        # Joining all results, so they will be visible in Job results log
        return '\n\n'.join(results_list)

    def _should_export_inventory(self, template):
        # Determine if the template qualifies for inventory export:
        #   1) Product (type == 'product'), or
        #   2) Consumable (type == 'consu') with a BOM (bill of materials).
        #   3) Check if the `update_stock_for_manufacture_boms` property is enabled.
        is_product = bool(template.is_consumable_storable)
        is_consumable_with_bom = (template.type == 'consu' and not template.is_storable and bool(template.bom_ids))
        return is_product or (self.update_stock_for_manufacture_boms and is_consumable_with_bom)

    def _handle_mapping_data(self, template, t_mapping, v_mapping_list, ext_records_to_update):
        # 1. Handle template
        assert t_mapping['model'] == 'product.template', _('Expected product template mapping.')

        is_only_template = (
            len(v_mapping_list) == 1 and v_mapping_list[0]['external_id'].endswith('-0')
        )
        ref_field = self.product_reference_name
        ext_reference = t_mapping.get('external_reference')
        if not ext_reference and is_only_template:
            ext_reference = getattr(template, ref_field)

        extra_vals = dict(
            name=template.name,
            external_reference=ext_reference,
        )
        tmpl_mapping = template.create_mapping(
            self,
            t_mapping['external_id'],
            extra_vals=extra_vals,
        )
        external_template_id = tmpl_mapping.external_template_id

        # 2. Handle variants
        external_variant_ids = self.env['integration.product.product.external']
        for v_mapping in v_mapping_list:
            variant = template.product_variant_ids.filtered(
                lambda x: x.id == v_mapping['id']
            )
            reference = v_mapping.get('external_reference') or getattr(variant, ref_field)
            extra_vals = dict(
                name=variant.name,
                external_reference=reference,
                deprecated_code=v_mapping['external_id'],
            )

            if v_mapping['id'] in ext_records_to_update:
                extra_vals['deprecated_code'] = ext_records_to_update[v_mapping['id']]

            mapping = variant.create_mapping(
                self,
                v_mapping['external_id'],
                extra_vals=extra_vals,
            )
            external_variant_ids |= mapping.external_product_id

        external_template_id.external_product_variant_ids = [(6, 0, external_variant_ids.ids)]
        return external_template_id, external_variant_ids

    def init_send_field_converter(self, *ar, **kw):
        raise NotImplementedError

    def init_receive_field_converter(self, *ar, **kw):
        raise NotImplementedError

    def convert_translated_field_to_integration_format(self, odoo_obj, field):
        converter = self.init_send_field_converter(odoo_obj)
        return converter.convert_translated_field_to_integration_format(field)

    def convert_translated_field_to_odoo_format(self, value):
        converter = self.init_receive_field_converter()
        return converter.convert_translated_field_to_odoo_format(value)

    def export_pricelist_items_to_external_cron(self):
        _logger.info('Call Integration cron: Send Pricelist Items')

        integration_ids = self.search([
            ('state', '=', 'active'),
            ('pricelist_integration', '=', True),
        ])

        result = list()
        for integration in integration_ids:
            job = integration \
                .with_context(company_id=self.company_id.id) \
                .with_delay(
                    description=f'{integration.name}: Export Pricelist Items Cron',
                ).export_pricelist_items_to_external()

            integration.job_log(job)
            result.append(job)

        return result

    def export_pricelist_items_to_external(self):
        self.ensure_one()
        mapping_ids = self.env['integration.product.pricelist.mapping'].search([
            ('pricelist_id', '!=', False),
            ('integration_id', '=', self.id),
            ('integration_id.state', '=', 'active'),
        ])
        pricelist_ids = mapping_ids.mapped('pricelist_id')

        if not pricelist_ids:
            return pricelist_ids._integration_not_mapped_error()

        return pricelist_ids.trigger_update_items_to_external(integration_id=self.id)

    def search_templates_for_specific_prices(self, pricelist_ids=None, item_ids=None):
        # TODO: use `pricelist_ids`` or 'item_ids' to do search more accurately
        mapping_ids = self.env['integration.product.template.mapping'].search([
            ('integration_id', '=', self.id),
            ('template_id.active', '=', True),
        ])
        return mapping_ids.mapped('template_id')

    def export_pricelists_multi(self, pricelist_ids=None, item_ids=None, updating=False):
        block_number = 1
        block_list = list()
        message_list = list()

        self = self.with_context(company_id=self.company_id.id)

        template_ids = self.search_templates_for_specific_prices(
            pricelist_ids=pricelist_ids,
            item_ids=item_ids,
        )
        converter = self.env['product.template'].init_template_export_converter(self)
        _template_ids = template_ids.browse().with_context(default_integration_id=self.id)
        _invalid_template_ids = _template_ids.browse()

        for template in template_ids:
            converter.replace_record(template)

            if not converter.ensure_template_mapped():
                message_list.append('%s was skipped due to not fully mapped.' % template)
                continue

            price_data = converter.convert_pricelists(
                pricelist_ids=pricelist_ids,
                item_ids=item_ids,
            )

            if price_data:
                if self._validate_pricelist_data(price_data):
                    _template_ids |= template
                    block_list.append(price_data)
                else:
                    _invalid_template_ids |= template

            if len(block_list) >= (EXPORT_EXTERNAL_BLOCK / 10):
                job_kwargs = self._job_kwargs_export_specific_prices_data(block_number)
                job = self.with_delay(**job_kwargs)._export_pricelist_data(block_list, updating)
                self.job_log(job)
                message_list.append(
                    _('Pricelists Batch (%s) was created: %s') % (block_number, job)
                )

                block_number += 1
                block_list = list()
                _template_ids = _template_ids.browse()

        if block_list:
            job_kwargs = self._job_kwargs_export_specific_prices_data(block_number)
            job = self.with_delay(**job_kwargs)._export_pricelist_data(block_list, updating)
            self.job_log(job)
            message_list.append(
                _('Pricelists Batch (%s) was created: %s') % (block_number, job)
            )

        for i_tmpl in _invalid_template_ids:
            job_kwargs = self._job_kwargs_export_specific_prices_template(i_tmpl)
            job = self.with_delay(**job_kwargs).export_pricelists_one(i_tmpl)
            i_tmpl.job_log(job)
            message_list.append(
                _('Pricelist items for %s have errors. Separate job was released.') % i_tmpl
            )

        return '\n'.join(message_list) or _('Skipped. Pricelist items for export not found.')

    def export_pricelists_one(self, template):
        converter = template.init_template_export_converter(self)
        data = converter.convert_pricelists(raise_error=True)

        if not data:
            message = _(
                '%s: there are no any specific prices for product template "%s"'
                % (self.name, template.display_name)
            )
            return message

        result = self._export_pricelist_data(data)
        return result

    def _export_pricelist_data(self, data, updating=False):
        """
        :return:

            [('product.template(145,) / 52: []', ['product.product(167,) / 52-0: [(12, 106)]'])]

                product.product(167,):  odoo product variant
                52-0: odoo product variant external code
                12: odoo pricelist item id
                106: external pricelist item id
        """
        if not isinstance(data, list):
            data = [data]

        adapter = self._build_adapter()
        result, sub_result, tmpl_ids = [], [], []

        for x_data in self._generate_pricelist_data(data):
            res = adapter.export_pricelists(x_data, updating)
            sub_result.append(res)

        for t_data_cls, v_data_cls_list in sub_result:
            tmpl_ids.append(t_data_cls.tmpl_id)

            t_dump = t_data_cls.dump()
            v_dump = [x.dump() for x in v_data_cls_list]
            result.append((t_dump, v_dump))

        if tmpl_ids:
            self.env['product.template']._unmark_force_sync_pricelist(tmpl_ids)

        return result

    def _generate_pricelist_data(self, data):
        for t_tuple, v_tuple_list in data:
            t_tuple_init = PriceList.from_tuple(t_tuple, self)
            v_tuple_list_init = [PriceList.from_tuple(x, self) for x in v_tuple_list]
            yield t_tuple_init, v_tuple_list_init

    @staticmethod
    def _validate_pricelist_data(price_data):
        """
        (
            (591, 'product.template', '64', [...], True),
            [
                (632, 'product.product', '64-168', [...], True),
                (636, 'product.product', '64-169', [...], True),
                (633, 'product.product', '64-170', [...], True),
            ]
        )
        """
        template, variants = price_data
        prices = sum([x[3] for x in variants], template[3])
        is_valid = all(x['_is_valid'] for x in prices)
        return is_valid

    def export_template_images_verbose(self, template_id: int, erase_mappings=False):
        template = self.env['product.template'].browse(template_id)

        try:
            # Attempt to export images
            result = self.export_template_images(template.id, erase_mappings=erase_mappings)
        except Exception:
            # Log the full traceback for debugging
            buff = StringIO()
            traceback.print_exc(file=buff)
            _logger.error(buff.getvalue())

            # Provide detailed error feedback to the user
            message = _(
                'Failed to export images for product template "%s".\n\n'
                'An unexpected error occurred during the export process.\n'
                'Please review the error details below or contact support (https://support.ventor.tech/) '
                'for assistance.\n\n'
                'Detailed Traceback:\n%s'
            ) % (template.name, buff.getvalue())
            raise ApiExportError(message)

        # Handle cases where there is nothing to export
        if result is None:
            message = _(
                'There are no images to export for product template "%s".\n\n'
                'Please ensure that the product template has images available for export.\n\n'
                'If the issue persists, contact support: https://support.ventor.tech/'
            ) % template.name
            return message

        # Handle failure to export images
        if result is False:
            message = _(
                'Failed to export images for product template "%s".\n\n'
                'This may be caused by broken or missing image files or incorrect configuration. '
                'Please verify the product template and try again.\n\n'
                'If the issue persists, contact support: https://support.ventor.tech/'
            ) % template.name
            raise ApiExportError(message)

        # Successful export
        message = _(
            'Images for product template "%s" were exported successfully:\n\n%s'
        ) % (template.name, result)
        return message

    def export_template_images(self, template_id: int, erase_mappings=False) -> List[Dict]:
        self.ensure_one()

        template = self.env['product.template'].browse(template_id)
        external_template = template.to_external_record(self)

        if erase_mappings:
            external_template.all_image_external_ids.unlink()

        datacls_list = template.with_context(
            lang=self.get_integration_lang_code(),
        ).to_images_export_format(self)

        return external_template._sync_images_data_out(datacls_list)

    def export_tracking(self, pickings):
        self.ensure_one()

        order = pickings.mapped('sale_id')
        assert len(order) == 1

        sale_order_id = order.to_external(self)
        tracking_data = pickings.to_export_format_multi(self)

        adapter = self._build_adapter()
        result = adapter.export_tracking(sale_order_id, tracking_data, force_done=self.force_full_fulfillment)

        if result:
            pickings.mark_integration_sent()

        return result

    def send_picking(self, picking):
        self.ensure_one()

        order = picking.sale_id
        sale_order_id = order.to_external(self)
        picking_data = picking.to_export_format(self)

        if not picking_data:
            raise ValidationError(_(
                'Sending was skipped because not all transfers are validated yet.\n\n'
                'Please ensure all other related transfers are validated before attempting to send this picking.'
            ))

        result = self.adapter.send_picking(sale_order_id, picking_data)

        if result:
            picking.mark_integration_sent()

        # The rest of logic can be applied only for Shopify and Magento 2 integrations
        # For other integrations, result will be empty list
        if result:
            for rec in result:
                rec['internal_status'] = 'done'

            picking.sale_id._apply_values_from_external({'order_fulfillments': result})

        return result

    def export_sale_order_status(self, order):
        self.ensure_one()

        adapter = self._build_adapter()
        vals = order._prepare_vals_for_sale_order_status()
        return adapter.export_sale_order_status(vals)

    def export_attribute(self, attribute):
        self.ensure_one()
        adapter = self._build_adapter()

        to_export = attribute.to_export_format(self)
        code = adapter.export_attribute(to_export)

        attribute.create_mapping(self, code, extra_vals={'name': attribute.name})

        return code

    def export_attribute_value(self, attribute_value):
        self.ensure_one()
        adapter = self._build_adapter()

        attribute_value_export = attribute_value.to_export_format(self)
        attribute_code = attribute_value_export.get('attribute')
        if not attribute_code:
            raise UserError(_(
                'The external attribute code cannot be empty for the attribute value "%s".\n\n'
            ) % attribute_value.name)

        attribute_value_code = adapter.export_attribute_value(attribute_value_export)

        attribute_value_mapping = attribute_value.create_mapping(
            self,
            attribute_value_code,
            extra_vals={'name': attribute_value.name},
        )
        external_attribute = self.env['integration.product.attribute.external'].search([
            ('code', '=', attribute_code),
            ('integration_id', '=', self.id),
        ])

        external_attribute_value = attribute_value_mapping.external_attribute_value_id
        external_attribute_value.external_attribute_id = external_attribute.id

        return attribute_value_code

    def export_feature(self, feature):
        self.ensure_one()
        adapter = self._build_adapter()

        to_export = feature.to_export_format(self)
        code = adapter.export_feature(to_export)

        feature.create_mapping(self, code, extra_vals={'name': feature.name})

        return code

    def export_feature_value(self, feature_value):
        self.ensure_one()
        adapter = self._build_adapter()

        feature_value_export = feature_value.to_export_format(self)
        feature_code = feature_value_export.get('feature_id')
        if not feature_code:
            raise UserError(_(
                'The external feature code cannot be empty for the feature value "%s".\n\n'
            ) % feature_value.name)

        feature_value_code = adapter.export_feature_value(feature_value_export)

        feature_value_mapping = feature_value.create_mapping(
            self,
            feature_value_code,
            extra_vals={'name': feature_value.name},
        )
        external_feature = self.env['integration.product.feature.external'].search([
            ('code', '=', feature_code),
            ('integration_id', '=', self.id),
        ])

        external_feature_value = feature_value_mapping.external_feature_value_id
        external_feature_value.external_feature_id = external_feature.id

        return feature_value_code

    def export_category(self, category):
        self.ensure_one()
        adapter = self._build_adapter()

        code = adapter.export_category(category.to_export_format(self))
        category.create_mapping(self, code, extra_vals={'name': category.name})

        return code

    def export_inventory_for_variant_with_delay(self, variant):
        variant.ensure_one()
        name = variant.display_name

        try:
            # Attempt to export the inventory for the variant
            result = self.export_inventory(variant, cron_operation=False)
        except Exception:
            buff = StringIO()
            traceback.print_exc(file=buff)
            _logger.error(buff.getvalue())

            # Provide detailed feedback to the user on the failure
            message = _(
                'Failed to export stock quantities for product variant "%s".\n\n'
                'An unexpected error occurred during the export process. Please review the details below '
                'or contact support for assistance.\n\n'
                'Detailed Traceback:\n%s'
            ) % (name, traceback.format_exc())
            raise ApiExportError(message)

        # If no result, inform the user that there's nothing to export
        if not result:
            message = _(
                'There are no stock quantities to export for product variant "%s".\n\n'
                'Ensure that the variant has stock quantities to export before trying again.'
            ) % name
            return message

        # Handle the result of the export
        __, export_success, err_msg = result[0]

        if not export_success:  # TODO: result may be a list of booleans (Presta case)
            # If export fails, use the error message or a default one
            message = err_msg or _('Failed to export inventory for Product Variant "%s".') % name
            raise ApiExportError(message)

        # Success message with details
        message = _(
            'Stock quantities for product variant "%s" were exported successfully:\n\n%s'
        ) % (name, export_success)
        return message

    def _get_skipped_variants_for_inventory(self, variant_ids):
        company_ids = (self.company_id.id, False)
        skipped_variant_ids = variant_ids.filtered(
            lambda x: self not in x.integration_ids or not (x.company_id.id in company_ids)
        )
        return skipped_variant_ids

    def export_inventory(self, variant_ids, cron_operation=True):
        # 1. Filter unsuitable records
        skipped_variant_ids = self._get_skipped_variants_for_inventory(variant_ids)

        if skipped_variant_ids:
            _logger.info(
                '%s export inventory: filtered product variants: %s',
                self.name,
                skipped_variant_ids.ids,
            )

        # 2. Prepare export data
        to_export_variant_ids = variant_ids - skipped_variant_ids
        inventory_data, fail_variant_ids = self._collect_inventory_data(
            to_export_variant_ids,
            raise_error=(not cron_operation),
        )

        if fail_variant_ids:
            _logger.info(
                '%s export inventory: skipped product variants: %s',
                self.name,
                fail_variant_ids.ids,
            )

        # 3. Create separate `obviously failed` jobs for unmapped records
        if cron_operation and fail_variant_ids:
            # According to `VSOPC-421` no need to do something with that
            pass

        if not inventory_data:
            return None

        # 4. Export
        result = self.adapter.export_inventory(inventory_data)

        # 5. Validate result
        if cron_operation:
            self._validate_export_inventory_result(result)

        return result

    def _collect_inventory_data(self, variant_ids, raise_error):
        self.ensure_one()

        # Check if inventory locations are specified
        if not self.location_line_ids:
            raise UserError(_(
                'The inventory locations for "%s" are not specified.\n\n'
                'Please go to "E-Commerce Integrations → Stores → %s → Inventory tab" and specify '
                'the locations where inventory will be managed.'
            ) % (self.name, self.name))

        inventory_data = dict()
        fail_variant_ids = self.env['product.product']
        location_ids_list = self.location_line_ids._group_by_exernal_code()

        multi_external_locations = len(location_ids_list) > 1

        # Check if advanced inventory is enabled and if multi-source is properly set up
        if self.advanced_inventory():
            if multi_external_locations and not all(x[0] for x in location_ids_list):
                raise UserError(_(
                    '%s: Multi-source inventory is not properly configured.\n\n'
                    'Please ensure that all external locations are set up correctly '
                    'in "E-Commerce Integrations → Stores → %s → Inventory tab".'
                ) % (self.name, self.name))
        else:
            if multi_external_locations:
                raise UserError(_(
                    '%s: Multi-source inventory is not allowed or is not properly configured.\n\n'
                    'Please review the "E-Commerce Integrations → Stores → %s → Inventory tab" and ensure '
                    'that multi-source inventory is either disabled or set up correctly.'
                ) % (self.name, self.name))

        # invalidate cache for all product's qty_fields
        # it seems that odoo doesn't recompute qty_fields.
        # if we read qty_fields, then change it, then read again.
        # doesn't seem to be a real case
        # (usually export_inventory is done in single transaction).
        # added to fix test, but I don't think that it affects performance very much.
        variant_ids.invalidate_recordset([self.synchronise_qty_field])

        for product in variant_ids:
            external_record = product.to_external_record(self, raise_error=raise_error)
            if not external_record:
                fail_variant_ids |= product
                continue

            data_list = list()
            for (external_location_id, locations) in location_ids_list:
                product = product.with_context(location=locations.ids)
                data = self._prepare_inventory_data(product, external_record, external_location_id)
                data_list.append(data)
            inventory_data[external_record.code] = data_list

        return inventory_data, fail_variant_ids

    def _validate_export_inventory_result(self, result_list):
        Variant = self.env['product.product']
        failed_variant_ids = Variant.browse()

        for external_id, result, err_msg in result_list:
            if not result:
                failed_variant_ids |= Variant.from_external(self, external_id)
            if err_msg:
                _logger.error(err_msg)

        if failed_variant_ids:
            self._create_separate_inventory_export_job(failed_variant_ids)
        return failed_variant_ids

    def _create_separate_inventory_export_job(self, variant_ids):
        _logger.warning(
            'Integration "%s": separate export inventory jobs for: %s', self.name, variant_ids,
        )
        # Create separate export jobs (isolated transactions) which will be highly likely
        # failed in order to notify user for existing troubles
        result = []
        self = self.with_context(company_id=self.company_id.id)
        variant_ids = variant_ids.with_context(default_integration_id=self.id)

        for variant in variant_ids:
            job_kwargs = self._job_kwargs_export_inventory_variant(variant)
            job = self \
                .with_delay(**job_kwargs) \
                .export_inventory_for_variant_with_delay(variant)

            variant.job_log(job)
            result.append(job)

        return result

    def _search_pricelist_mappings(self):
        pricelist_map_ids = self.env['integration.product.pricelist.mapping'].search([
            ('integration_id', '=', self.id),
        ])
        return pricelist_map_ids.mapped('pricelist_id').ids

    def _get_wh_from_external_location(self, external_location_code: str):
        record = self.location_line_ids.filtered(  # TODO: what if there will be a recordset
            lambda x: x.external_location_id.code == external_location_code,
        )
        return record[:1].warehouse_id

    def _build_adapter_core(self):
        settings = self.to_dictionary()
        adapter_core = settings['class'](settings)
        adapter_core._integration_id = self.id
        adapter_core._integration_name = self.name
        adapter_core._adapter_hash = self.get_hash()
        return adapter_core

    def _build_adapter(self):
        self.ensure_one()
        self.write_settings_fields({})
        adapter_core = self._adapter_hub_.get_core(self)
        return Adapter(adapter_core, self)

    def to_dictionary(self):
        self.ensure_one()
        return {
            'name': self.name,
            'type_api': self.type_api,
            'use_async': self.use_async,
            'class': self.get_class(),
            'fields': self.field_ids.to_dictionary(),
            'data_block_size': int(self.env['ir.config_parameter'].sudo().get_param(  # *
                'integration.import_data_block_size'))
        }

    def filter_received_orders(self, orders_data_list):
        """
        Global method to filter received orders.
        This method will call specific filtering methods based on the integration type.
        """
        self.ensure_one()
        integration_type = self.type_api

        filter_method_name = f'_filter_orders_{integration_type}'

        if hasattr(self, filter_method_name):
            filter_method = getattr(self, filter_method_name)
            return filter_method(orders_data_list)
        else:
            _logger.info(f'No filtering method found for integration type: {integration_type}')
            return orders_data_list

    def integrationApiReceiveOrders(self, update_dt=True):
        """
        Receive and process orders from the integration source.

        Args:
            update_dt (bool): Whether to update the 'last_receive_orders_datetime'
            attribute with the maximum 'updated_at' value from received orders.
            Defaults to True.

        Returns:
            recordset: A set of created input files for processed orders.
        """
        self.ensure_one()
        adapter = self._build_adapter()
        last_receive_dt = self.last_receive_orders_datetime

        _logger.info(
            '%s receive orders: from kwargs %s',
            self.name,
            adapter.order_fetch_kwargs(),
        )
        # 1. receive orders
        orders_data_list = adapter.receive_orders()

        # 2. filter orders
        filtered_orders_data_list = self.filter_received_orders(orders_data_list)

        # 3. create input files
        updated_at_list = list()
        created_input_files = self.env['sale.integration.input.file']
        for order_data in filtered_orders_data_list:
            input_file = self._create_input_file_from_received_data(order_data)
            created_input_files |= input_file

        # 4. update receive parameters
        updated_at_list = [order_data['updated_at'] for order_data in orders_data_list]
        if updated_at_list:
            last_receive_dt = self._find_max_datetime(updated_at_list)
        else:
            update_dt = False

        if update_dt:  # update
            self.last_receive_orders_datetime = last_receive_dt - timedelta(seconds=1)

        if len(orders_data_list) == adapter.order_limit_value():
            self.integration_receive_orders_cron()

        _logger.info(
            '%s receive orders: {count: %s, updated_at: %s, input_files: %s}',
            self.name,
            len(filtered_orders_data_list),
            last_receive_dt,
            created_input_files.ids,
        )
        return created_input_files

    def integrationApiClearIncorrectAttributeValueMappings(self):
        """Clear incorrect mappings for integration"""
        self.ensure_one()
        attribute_value_mappings = self.env['integration.product.attribute.value.mapping'].search([
            ('integration_id', '=', self.id),
            ('attribute_value_id', '!=', False),
        ])
        for value in attribute_value_mappings:
            if value.attribute_value_id:
                if value.attribute_value_id.attribute_id != value.get_attribute_id():
                    value.attribute_value_id = False
        return True

    def integrationApiClearIncorrectFeatureValueMappings(self):
        """Clear incorrect mappings for integration"""
        self.ensure_one()
        feature_value_mappings = self.env['integration.product.feature.value.mapping'].search([
            ('integration_id', '=', self.id),
            ('feature_value_id', '!=', False),
        ])
        for value in feature_value_mappings:
            if value.feature_value_id:
                if value.feature_value_id.feature_id != value.get_feature_id():
                    value.feature_value_id = False
        return True

    def integration_receive_orders_cron(self, cron_operation=True):
        job_kwargs = self._job_kwargs_receive_orders(cron=cron_operation)

        job = self \
            .with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .integrationApiReceiveOrders(update_dt=cron_operation)

        self.job_log(job)

        return job

    def is_importable_order_status(self, statuses: list[str]) -> bool:
        """
        Determines if an order should be imported based on its status.

        This method checks if the order's status is included in the configured list
        of allowed statuses from settings.

        Args:
            status_list (list[str]): List of order statuses to validate. Typically contains
                                    only one status except for Shopify integration.

        Returns:
            bool: True if the order status is in the allowed list, False otherwise.
        """
        status = statuses[0]  # Length of statuses list is 1, except Shopify
        statuses_str = self.get_settings_value('receive_orders_filter') or ''
        statuses_list = [s.strip() for s in statuses_str.split(',') if s.strip()]
        return status in statuses_list

    def fetch_order_by_id_with_delay(self, external_order_id):
        """
        Create a sale order in Odoo based on the external order ID.
        """
        self.ensure_one()
        job_kwargs = self._job_kwargs_receive_order(external_order_id)

        job = self \
            .with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .fetch_order_by_id(external_order_id, raise_error=True)

        self.job_log(job)

        return job

    def update_order_status_by_id_with_delay(self, external_order_id, pipeline_data: dict):
        """
        Update the status of a sale order in Odoo based on the external order ID.
        The `pipeline_data` must contain at least the `integration_workflow_states` key.
        """
        self.ensure_one()
        job_kwargs = self._job_kwargs_update_status_order(external_order_id)

        job = self \
            .with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .integration_update_status_order(external_order_id, pipeline_data)

        self.job_log(job)

        return job

    def cancel_order_by_id_with_delay(self, external_order_id, pipeline_data: dict):
        """
        Cancel a sale order in Odoo based on the external order ID.
        The `pipeline_data` must contain at least the `integration_workflow_states` key.
        """
        self.ensure_one()
        job_kwargs = self._job_kwargs_update_status_order(external_order_id)
        description_ = job_kwargs['description']

        job_kwargs.update(
            priority=5,
            description=description_.replace('Update', 'Cancel'),
        )

        job = self \
            .with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .integration_update_status_order(
                external_order_id,
                pipeline_data,
                cancel_order=True,
            )

        self.job_log(job)

        return job

    def integration_update_status_order(self, external_order_id, pipeline_data: dict, cancel_order=False):
        """
        Update the status of a sale order in Odoo based on the external order ID.
        The `pipeline_data` must contain at least the `integration_workflow_states` key.
        """
        self.ensure_one()

        input_file = self._get_input_file(external_order_id)

        if not input_file:
            _logger.info(f'External data for order with code={external_order_id} not found!')
            raise ApiImportError(
                f'{self.name} (update order status): Order with code={external_order_id} not found!'
                f'Most likely it was not imported (due to import order status filter).'
            )

        if cancel_order:
            if input_file.order_id:
                return input_file._run_cancel_order(pipeline_data)
            return False

        return input_file._build_and_run_order_pipeline(pipeline_data)

    def fetch_order_by_id(self, external_order_id, raise_error=False):
        self.ensure_one()

        # Build the adapter and attempt to receive order data
        adapter = self._build_adapter()
        input_data = adapter.receive_order(external_order_id)

        # Handle case where input data is not found for the external order
        if not input_data:
            message = _(
                '%s: Failed to receive order. External order with ID "%s" was not found.\n\n'
                'This could be due to one of the following reasons:\n'
                '- The order ID is incorrect (please double-check the ID).\n'
                '- The order has been removed from the external system.\n\n'
                'Please verify the external order ID or check the order in the external system.'
            ) % (self.name, external_order_id)
            _logger.info(message)

            if raise_error:
                raise ApiImportError(message)
            return False

        # Create input file from received data
        input_file = self._create_input_file_from_received_data(input_data)

        _logger.info(
            '%s: Successfully received order. Created input file "%s" from external order ID "%s".',
            self.name,
            input_file,
            external_order_id,
        )
        return input_file

    def create_product_by_id_with_delay(self, external_product_id):
        """
        Create a product in Odoo based on the external product ID.
        """
        self.ensure_one()

        job_kwargs = self._job_kwargs_import_product(
            external_product_id,
            self.env.context.get('external_product_name') or '',
        )
        job_kwargs['eta'] = 8

        job = self.with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .import_product_flow(external_product_id)

        self.job_log(job)

        return job

    def update_product_by_id_with_delay(self, external_product_id):
        """
        Update a product in Odoo based on the external product ID.
        """
        self.ensure_one()

        # Check if the product exists in Odoo, if not, create it.
        external_product = self._get_external_product(external_product_id)
        if not external_product:
            return self.create_product_by_id_with_delay(external_product_id)

        # Check if the product is mapped to an Odoo record, if not, drop it and create a new external product.
        mapping_record = external_product.mapping_record
        if not mapping_record.odoo_record:
            self.drop_external_record(external_product.id)
            return self.create_product_by_id_with_delay(external_product_id)

        job_kwargs = self._job_kwargs_update_product_in_odoo(
            external_product_id,
            self.env.context.get('external_product_name') or '',
        )

        job = external_product \
            .with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .import_one_product_by_hook()

        external_product.job_log(job)

        return job

    def delete_product_by_id_with_delay(self, external_product_id):
        """
        Delete a product in Odoo based on the external product ID.
        """
        self.ensure_one()

        external_product = self._get_external_product(external_product_id)
        if not external_product:
            _logger.info(f"Product with code={external_product_id} not found!")
            return True

        job_kwargs = self._job_kwargs_delete_product(external_product.code, external_product.name)

        job = self.with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .drop_external_record(external_product.id)

        self.job_log(job)

        return job

    def process_pipeline_by_id_with_delay(self, external_order_id, data, build_and_run=False):
        """
        Process a pipeline in Odoo based on the external order ID.
        """
        self.ensure_one()

        job_kwargs = self._job_kwargs_process_pipeline(external_order_id)

        job = self.with_context(company_id=self.company_id.id) \
            .with_delay(**job_kwargs) \
            .integration_process_pipeline(external_order_id, data, build_and_run=build_and_run)

        self.job_log(job)

        return job

    def integration_process_pipeline(self, external_order_id, data, build_and_run=False):
        self.ensure_one()

        input_file = self._get_input_file(external_order_id)
        if not input_file:
            _logger.info(
                f'Input file for order with code={external_order_id} not found!'
            )
            return False

        if build_and_run:
            return input_file._build_and_run_order_pipeline(data)

        return input_file.run_actual_pipeline()

    def _create_input_file_from_received_data(self, input_data):
        InputFile = self.env['sale.integration.input.file']
        domain = [
            ('si_id', '=', self.id),
            ('name', '=', input_data['id']),
        ]
        exists = InputFile.search(domain, limit=1)

        if exists:
            return InputFile

        vals = {
            **{k: v for k, __, v in domain},
            'raw_data': json.dumps(input_data['data'], indent=4),
            'update_required': True,
        }
        return InputFile.create(vals)

    def integrationApiExportInventory(self):
        if not self.synchronize_all_inventory_periodically:
            return False

        products = self.env['product.product'].search([
            ('type', '=', 'consu'),
            ('is_storable', '=', True),
            ('integration_ids.id', '=', self.id),
            ('exclude_from_synchronization', '=', False),
            ('exclude_from_synchronization_stock', '=', False),
        ])
        return products.export_inventory_by_jobs(self, cron_operation=True)

    def is_canceled_order_status(self, status_code: str) -> bool:
        """
        Determines if the given status code represents a canceled order.

        This method extracts the store order status ID from the provided status code
        and checks if it matches the predefined cancel status ID.

        Args:
            status_code (str): The order status code to evaluate.

        Returns:
            bool: True if the status code indicates a canceled order, False otherwise.
        """
        sub_status_id, _ = self._get_order_sub_status_tuple(status_code)
        return sub_status_id == self.sub_status_cancel_id

    def update_last_update_pricelist_items_to_now(self, value):
        self.last_update_pricelist_items = value

    def create_order_from_input(self, input_file):
        self.ensure_one()

        # 1. Parse the input file (eCommerce order json)
        order_data = input_file.parse()

        # 2.Check if the order has been canceled
        is_order_cancelled = order_data.pop('is_cancelled', False)

        # 3.Create the sale order from parsed data
        order = self.env['integration.sale.order.factory'] \
            .with_company(self.company_id) \
            .create_order(self, order_data)

        # 4. If the order is canceled on the store side, cancel it in Odoo
        if is_order_cancelled:
            _logger.info(f'Order "{order.name}" has been canceled on the store side. Canceling it in Odoo.')

            order._integration_action_cancel_no_dispatch()
            input_file.action_done()
            return order

        # 5. Process the order automatic workflow
        input_file.action_process()
        order._build_and_run_integration_workflow(order_data)

        return order

    def integrationApiCreateOrders(self):  # Seems this one not used currently
        self.ensure_one()

        input_files = self.env['sale.integration.input.file'].search([
            ('si_id', '=', self.id),
            ('state', '=', 'draft'),
        ])

        orders = self.env['sale.order']
        for input_file in input_files:
            orders += self.create_order_from_input(input_file)

        return orders

    @api.model
    def systray_get_integrations(self):
        integrations = self.sudo().search([
            ('state', '=', 'active'),
        ])

        result = []
        mapping_models = self.integration_mapping_models()

        for integration in integrations:
            failed_jobs_count = integration._get_integration_failed_jobs_count()

            missing_mappings_count = 0
            for model_name in mapping_models:
                mapping_model = self.env[model_name]
                internal_field_name, external_field_name = mapping_model._mapping_fields
                missing_mappings = mapping_model.search_count([
                    ('integration_id', '=', integration.id),
                    (internal_field_name, '=', False),
                    (external_field_name, '!=', False),
                ])

                missing_mappings_count += missing_mappings

            integration_stats = {
                'name': integration.name,
                'type_api': integration.type_api,
                'failed_jobs_count': failed_jobs_count,
                'missing_mappings_count': missing_mappings_count,
            }
            result.append(integration_stats)

        return result

    @ormcache()
    def integration_mapping_models(self):
        result = list()
        for model_name in self.env:
            if (
                model_name.startswith('integration.')
                and model_name.endswith('.mapping')
                and model_name not in MAPPING_EXCEPT_LIST
            ):
                result.append(model_name)
        return result

    def _get_integration_failed_jobs_count(self):
        failed_jobs_count = self.env['queue.job'].search_count([
            ('state', '=', 'failed'),
            ('integration_id', '=', self.id),
            ('company_id', '=', self.company_id.id)
        ])
        return failed_jobs_count

    def _get_integration_id_for_job(self):
        return self.id

    @api.model
    def _get_test_method(self):
        return [
            (method, method.replace('integrationApi', ''))
            for method in dir(self)
            if method.startswith('integrationApi') and callable(getattr(self, method))
        ]

    def test_job(self):
        method_name = self.test_method
        if not method_name:
            raise UserError(_(
                'No test method selected.\n\n'
                'Please select a test method from the dropdown above before clicking the button.'
            ))

        # Check if the method exists on the current object
        test_method = getattr(self, method_name, None)
        if test_method:
            return test_method()

        # If the method doesn't exist, log the issue and return True
        _logger.warning('The selected test method "%s" does not exist on the object.', method_name)
        return True

    def _ensure_ecommerce_field(self, name):
        """
        :name:
            - Name of the m2o field pointed to the Ecommerce Field --> product.ecommerce.field
        """
        field_id = getattr(self, name, False)

        if not field_id:
            set_value = getattr(self, f'_set_default_{name}', lambda: False)()

            if not set_value:
                raise UserError(_(
                    '%s: The essential field "%s" is not defined.\n\n'
                    'This field is required for the proper functioning of the integration. '
                    'Please ensure that the field is set correctly in the eCommerce settings.'
                ) % (self.name, name))

            field_id = getattr(self, name)

        field_id._ensure_mapping(self.id)

        return field_id

    def _set_default_advanced_fields(self):
        for rec in self:
            # Set references
            rec.template_reference_id.mark_mapping_inactive(rec.id)
            rec._set_default_template_reference_id()
            rec.product_reference_id.mark_mapping_inactive(rec.id)
            rec._set_default_product_reference_id()

            # Set barcodes
            rec.template_barcode_id.mark_mapping_inactive(rec.id)
            rec._set_default_template_barcode_id()
            rec.product_barcode_id.mark_mapping_inactive(rec.id)
            rec._set_default_product_barcode_id()

    def _fill_default_advanced_fields(self):
        for rec in self:
            # Fill references
            if not rec.template_reference_id:
                rec._set_default_template_reference_id()
            if not rec.product_reference_id:
                rec._set_default_product_reference_id()

            # Fill barcodes
            if not rec.template_barcode_id:
                rec._set_default_template_barcode_id()
            if not rec.product_barcode_id:
                rec._set_default_product_barcode_id()

    @property
    def product_reference_name(self):
        ecommerce_id = self._ensure_ecommerce_field('product_reference_id')
        return ecommerce_id.odoo_field_name

    @property
    def product_barcode_name(self):
        ecommerce_id = self._ensure_ecommerce_field('product_barcode_id')
        return ecommerce_id.odoo_field_name

    @property
    def template_reference_api_name(self):
        ecommerce_id = self._ensure_ecommerce_field('template_reference_id')
        return ecommerce_id.technical_name

    @property
    def variant_reference_api_name(self):
        ecommerce_id = self._ensure_ecommerce_field('product_reference_id')
        return ecommerce_id.technical_name

    @property
    def template_barcode_api_name(self):
        ecommerce_id = self._ensure_ecommerce_field('template_barcode_id')
        return ecommerce_id.technical_name

    @property
    def variant_barcode_api_name(self):
        ecommerce_id = self._ensure_ecommerce_field('product_barcode_id')
        return ecommerce_id.technical_name

    def _get_reference_field_name(self, odoo_model):
        if odoo_model._name in ('product.template', 'product.product'):
            return self.product_reference_name

        reference_field = getattr(odoo_model, '_internal_reference_field', None)
        if not reference_field:
            # Raise a custom exception with a clear, user-friendly message
            raise NoReferenceFieldDefined(_(
                'The model "%s" does not have an internal reference field defined (_internal_reference_field).\n\n'
                'This field is required for integration purposes. Please ensure that the model is properly configured '
                'or contact support for assistance.'
            ) % odoo_model._name)

        return reference_field

    def get_template_hub_class(self):
        return TemplateHub

    def _get_product_validation_domain(self):
        return [
            ('product_tmpl_id.exclude_from_synchronization', '=', False),
        ]

    def is_barcode_validation_required(self):
        if not self.validate_barcode:
            return False

        self._ensure_ecommerce_field('template_barcode_id')
        self._ensure_ecommerce_field('product_barcode_id')

        mapping_ids = (
            self.template_barcode_id.get_mapping_for_integration(self.id)
            + self.product_barcode_id.get_mapping_for_integration(self.id)
        )

        # Raise an error if no mappings are found
        if not mapping_ids:
            raise UserError(_(
                'Barcode validation is enabled, but no active mappings exist for the barcode fields '
                'between Odoo and the e-commerce system.\n\n'
                'To proceed, please add the required mappings for barcode fields in the integration settings, '
                'or disable the "Variant Barcode Validation" property if barcode validation is not needed.'
            ))

        return True

    def _validate_product_templates(self, show_message=False):
        tmpl_hub = self.adapter.get_templates_and_products_for_validation_test()

        ref_field = self.product_reference_name
        barcode_field = self.product_barcode_name
        ref_field_name = self.env['product.template']._get_field_string(ref_field)

        # Get product validation results from the e-commerce system
        template_ids, variant_ids = tmpl_hub.get_empty_ref_ids()
        repeated_configurations = tmpl_hub.get_repeated_configurations()
        nested_configurations = tmpl_hub.get_nested_configurations()
        duplicated_ref = tmpl_hub.get_dupl_refs()

        check_barcodes = self.is_barcode_validation_required()

        if check_barcodes:
            part_fill_bar = tmpl_hub.get_part_fill_barcodes()
            duplicated_bar = tmpl_hub.get_dupl_barcodes()
        else:
            part_fill_bar = duplicated_bar = False

        formatter = HtmlWrapper(self)

        # Add title and errors for the e-commerce system
        if any((template_ids, variant_ids, duplicated_ref, duplicated_bar)):
            formatter.add_title(_('E-COMMERCE SYSTEM VALIDATION'))

        # Missing reference fields in the e-commerce system
        if template_ids:
            formatter.add_sub_block_for_external_product_list(
                _('Products without "%s" in the e-commerce system:') % ref_field_name,
                template_ids,
            )

        # Partially filled barcodes
        if part_fill_bar:
            formatter.add_sub_block_for_external_product_list(
                _('Products with partially filled barcodes on variants in the e-commerce system:'),
                part_fill_bar,
            )

        # Missing reference fields in product variants
        if variant_ids:
            formatter.add_sub_block_for_external_product_list(
                _('Product variants IDs without "%s" in E-Commerce System:') % ref_field_name,
                variant_ids,
            )

        # Repeated configurations in the e-commerce system
        if repeated_configurations:
            formatter.add_sub_block_for_external_product_dict(
                _('Simple products assigned to multiple configurable products:'),
                repeated_configurations,
                wrap_key=True,
            )

        # Nested configurable products
        if nested_configurations:
            formatter.add_sub_block_for_external_product_dict(
                _('Configurable Product contains another Configurable Product.'),
                nested_configurations,
                wrap_key=True,
            )

        # Duplicated references in the e-commerce system
        if duplicated_ref:
            formatter.add_sub_block_for_external_product_dict(
                _('Duplicated references in the e-commerce system:'),
                duplicated_ref,
            )

        # Duplicated barcodes in the e-commerce system
        if duplicated_bar:
            formatter.add_sub_block_for_external_product_dict(
                _('Duplicated barcodes in E-Commerce System:'),
                duplicated_bar,
            )

        # Now validate Odoo products
        search_product_domain = self._get_product_validation_domain()
        field_list = [  # Don't touch fields sequence
            'id', 'name', barcode_field, ref_field, 'product_tmpl_id',
        ]
        odoo_variant_ids = self.env['product.product'].search_read(
            search_product_domain,
            fields=field_list,
        )
        tmpl_hub_odoo = tmpl_hub.__class__.from_odoo(
            odoo_variant_ids, reference=ref_field, barcode=barcode_field)

        __, variant_odoo_ids = tmpl_hub_odoo.get_empty_ref_ids()
        duplicated_ref_odoo = tmpl_hub_odoo.get_dupl_refs()

        if check_barcodes:
            duplicated_bar_odoo = tmpl_hub_odoo.get_dupl_barcodes()
        else:
            duplicated_bar_odoo = False

        # Add title and errors for Odoo
        if any((variant_odoo_ids, duplicated_ref_odoo, duplicated_bar_odoo)):
            formatter.add_title(_('ODOO SYSTEM VALIDATION'))

        # Missing reference fields in Odoo product variants
        if variant_odoo_ids:
            formatter.add_sub_block_for_internal_variant_list(
                _('Product variants without "%s" in Odoo:') % ref_field_name,
                variant_odoo_ids,
            )

        # Duplicated references in Odoo
        if duplicated_ref_odoo:
            formatter.add_sub_block_for_internal_variant_dict(
                _('Duplicated references in Odoo:'),
                duplicated_ref_odoo,
            )

        # Duplicated barcodes in Odoo
        if duplicated_bar_odoo:
            formatter.add_sub_block_for_internal_variant_dict(
                _('Duplicated barcodes in Odoo:'),
                duplicated_bar_odoo,
            )

        # If there are no issues but the show_message flag is set, raise a success message
        if not formatter.has_message and show_message:
            raise UserError(_('All products are correctly configured.'))

        # If any messages were collected, display them in a wizard
        if formatter.has_message:
            message_wizard = self.env['message.wizard'].create({
                'message': 'Warning!',  # Hidden field
                'message_html': formatter.dump(),
            })
            action = message_wizard.run_wizard('integration_message_wizard_validate_template_form')
            return action, tmpl_hub

        return False, tmpl_hub

    @raise_requeue_job_on_concurrent_update
    def import_product(
        self,
        external_template_id: int,
        import_images: bool = False,
        trigger_export_other: bool = False,
    ):
        """
        :external_template_id:
            - int (ID of the `integration.product.template.external` ORM record)
        """
        self.ensure_one()

        external_template = self.env['integration.product.template.external'] \
            .browse(external_template_id)

        template_data, variants_data, bom_data, external_images = self.adapter.get_product_for_import(
            external_template.code,
            import_images=import_images,
        )

        try:
            template = external_template\
                .with_context(integration_import_images=import_images) \
                ._import_one_product(template_data, variants_data, bom_data, external_images)
        except OperationalError:
            raise
        except Exception as ex:
            raise ValidationError(_(
                'Failed to import the product from the external system.\n\n'
                'Debug Information:\n\nError: %s\n\n'
                'External Template Code: %s\n\n'
                'Template Data:\n\t%s\n\n'
                'Variants Data:\n\t%s\n\n'
                'BOM Data:\n\t%s\n\n'
            ) % (ex.args[0], external_template.code, template_data, variants_data, bom_data))

        template = template.with_context(skip_product_export=False)

        if trigger_export_other:
            for integration in template.integration_ids.filtered(lambda x: x != self):
                template.trigger_export(export_images=import_images, force_integrations=integration)

        return template

    def import_product_flow(self, external_template_id: str):
        """
        Full import for the new product (not existing in DB):
            a) create an external-record
            b) run full import for the external-record
        """
        self.ensure_one()

        if self._get_external_product(external_template_id):
            return False

        external_template, __, errors = self._import_external_product(external_template_id)

        if errors:
            _logger.warning(f'Errors during template import {external_template_id}:\n' + '\n'.join(errors))

        if not external_template:
            _logger.warning(f'External product {external_template_id} could not be imported.')
            return False

        odoo_record = external_template.odoo_record
        if odoo_record or self.auto_create_products_on_so:
            # Calling the method to update the product in Odoo.
            # Example: In WooCommerce with multi-language enabled, if a translated product is modified,
            # the connector receives the translated product's ID but the API returns the main product instead.
            # Since the main product already exists in Odoo, no update occurs for the translated version.

            odoo_record = external_template \
                .import_one_product(import_images=self.allow_import_images)

        return odoo_record

    def drop_external_record(self, odoo_external_product_id):
        record = self.env['integration.product.template.external'].browse(odoo_external_product_id)
        record_format = record.format_recordset()
        return record_format, record.unlink()

    def action_run_configuration_wizard(self):
        configuration_wizard = self._build_configuration_wizard()
        configuration_wizard.init_configuration()
        return configuration_wizard.get_action_view()

    def _get_configuration_wizard(self):
        integration_postfix = self._get_configuration_postfix()
        configuration_wizard = self.env['configuration.wizard.' + integration_postfix].search(
            [('integration_id', '=', self.id)],
            limit=1,
        )
        return configuration_wizard

    def _build_configuration_wizard(self):
        configuration_wizard = self._get_configuration_wizard()
        if not configuration_wizard:
            configuration_wizard = configuration_wizard.create({'integration_id': self.id})
        return configuration_wizard

    def _should_link_parent_contact(self):
        # Since we are having `external_company_name` this one may de dropped.
        return True

    def force_set_inactive(self):
        return {'active': False}

    def _set_zero_time_zone(self, external_date, to_string=False):
        """
        Set time zone to UTC+0
        :param external_date - datetime as a string or datetime class
        :return: datetime object with time zone UTC+0
        """
        if isinstance(external_date, str):
            external_date = parser.isoparse(external_date)

        if external_date.tzinfo:
            external_date = external_date.astimezone(pytz.utc).replace(tzinfo=None)

        if to_string:
            return datetime.strftime(external_date, DATETIME_FORMAT)
        return external_date

    def _find_max_datetime(self, datetime_list: list):
        date_max = max(x for x in datetime_list)
        return self._set_zero_time_zone(date_max)

    def _job_kwargs_receive_orders(self, cron=True):
        return {
            'priority': 6,
            'description': f'{self.name}: Receive Orders (cron={cron})',
            'identity_key': f'receive_orders_cron-{self.type_api}_{self.id}',
        }

    def _job_kwargs_receive_order(self, order_id):
        source = self.env.context.get('integration_event_source', '')
        return {
            'eta': 8,
            'priority': 7,
            'description': f'{self.name}: Receive One Order (id={order_id}){f" [{source}]" if source else ""}',
            'identity_key': f'receive_order-{self.type_api}_{self.id}_{order_id}',
        }

    def _job_kwargs_update_status_order(self, order_id):
        source = self.env.context.get('integration_event_source', '')
        return {
            'eta': 8,
            'priority': 9,
            'description': f'{self.name}: Update Order Status (id={order_id}){f" [{source}]" if source else ""}',
            'identity_key': f'update_order_status-{self.type_api}_{self.id}_{order_id}',
        }

    def _job_kwargs_export_template(self, template, export_images, force=False):
        return {
            'eta': int(self.get_settings_value('export_template_delay', default_value=0)),
            'priority': 11,
            'identity_key': (
                f'export_template-{self.id}_{template}_{int(export_images)}_{int(force)}'
            ),
            'description': f'{self.name}: Export Single Product "{template.display_name}"',
        }

    def _job_kwargs_import_product(self, external_id, name):
        source = self.env.context.get('integration_event_source', '')
        description = (
            f'{self.name}: Import External Product "{name}" [{external_id}]'
            f'{f" [{source}]" if source else ""}'
        )
        return {
            'priority': 17,
            'identity_key': f'import_external_product-{self.id}-{external_id}',
            'description': description,
        }

    def _job_kwargs_update_product_in_odoo(self, external_id, name):
        source = self.env.context.get('integration_event_source', '')
        description = (
            f'{self.name}: Update External Product "{name}" [{external_id}] in Odoo'
            f'{f" [{source}]" if source else ""}'
        )

        return {
            'eta': 10,
            'priority': 17,
            'identity_key': f'update_external_product-{self.id}-{external_id}',
            'description': description,
        }

    def _job_kwargs_delete_product(self, external_id, name):
        source = self.env.context.get('integration_event_source', '')
        description = (
            f'{self.name}: Delete External Product "{name}" [{external_id}] in Odoo'
            f'{f" [{source}]" if source else ""}'
        )
        return {
            'priority': 17,
            'identity_key': f'delete_external_product-{self.id}-{external_id}',
            'description': description,
        }

    def _job_kwargs_process_pipeline(self, external_order_id):
        source = self.env.context.get('integration_event_source', '')
        description = (
            f'{self.name}: Process Pipeline for Order "{external_order_id}"'
            f'{f" [{source}]" if source else ""}'
        )
        return {
            'priority': 9,
            'identity_key': f'process_pipeline-{self.id}_{external_order_id}',
            'description': description,
        }

    def _job_kwargs_export_images(self, template):
        return {
            'eta': 5,
            'priority': 12,
            'identity_key': f'export_images-{self.id}-{template}',
            'description': f'{self.name}: Export Images for the Product"{template.display_name}"',
        }

    def _job_kwargs_import_images(self, template):
        return {
            'priority': 18,
            'identity_key': f'import_images-{self.id}-{template}',
            'description': f'{self.name}: Import Images for the Product "{template.display_name}"',
        }

    def _job_kwargs_export_inventory_variant(self, variant, cron=True):
        return {
            'priority': 13,
            'identity_key': f'export_inventory-{self.id}-{variant}',
            'description': (
                f'{self.name}: Export Inventory for the Product Variant "{variant.display_name}" (cron={cron})'
            ),
        }

    def _job_kwargs_create_order_from_input(self, input_file):
        return {
            'priority': 8,
            'identity_key': f'create_order-{self.id}-{input_file}',
            'eta': 60 * int(self.get_settings_value('process_order_delay') or 0),
            'description': f'{self.name}: Create Order from Input File "{input_file.display_name}"',
        }

    def _job_kwargs_export_specific_prices_template(self, template):
        return {
            'priority': 16,
            'identity_key': f'export_specific_prices-{self.id}-{template}',
            'description': (
                f'{self.name}: Export Specific Prices for "{template.display_name}" template'
            ),
        }

    def _job_kwargs_prepare_specific_prices(self, pricelist_ids):
        ids = '-'.join(map(str, pricelist_ids.ids))
        names = ', '.join(pricelist_ids.mapped('name'))
        return {
            'priority': 14,
            'identity_key': f'prepare_prices_from_pricelists-{self.id}_{ids}',
            'description': (
                f'{self.name}: Export. Prepare Specific Prices from Pricelists ({names})'
            ),
        }

    def _job_kwargs_export_specific_prices_data(self, idx):
        return {
            'priority': 15,
            'identity_key': f'export_specific_prices_block-{self.id}-{idx}',
            'description': f'{self.name}: Export Specific Prices ({idx})',
        }

    def _job_kwargs_apply_stock_single(self, external_id, location):
        name = f'{external_id} → {location.display_name}'
        return {
            'priority': 4,
            'identity_key': f'integration-apply-stock-single_{self.id}-{external_id}-{location.id}',
            'description': f'{self.name}: Apply Stock Levels for Single Product: {name}',
        }

    def _job_kwargs_apply_stock_multi(self, location_line, block=1):
        location_id = location_line.id
        location_name = location_line.erp_location_id.display_name
        return {
            'priority': 3,
            'identity_key': f'integration-apply-stock-multi_{self.id}-{location_id}-{block}',
            'description': f'{self.name}: Apply Stock Levels to "{location_name}" ({block})',
        }

    def _job_kwargs_import_stock_from_location(self, location_line, block=1):
        location_id = location_line.erp_location_id.id
        location_name = location_line.erp_location_id.display_name

        ext_location_id = location_line.external_location_id.id
        ext_location_name = location_line.external_location_id.name or self.name

        complex_id = f'{ext_location_id}-{location_id}'
        complex_name = f'{ext_location_name} → {location_name}'

        return {
            'priority': 3,
            'identity_key': f'integration-import-stock-location_{self.id}-{complex_id}-{block}',
            'description': f'{self.name}: Import Stock Levels for "{complex_name}" ({block})',
        }

    def _job_kwargs_import_single_customer(self, customer_id):
        return {
            'priority': 4,
            'description': f'{self.name}: Import for Single Customer "%s"' % customer_id,
            'identity_key': f'integration_import_single_customer-{self.id}-{customer_id}',
        }

    def _job_kwargs_export_picking(self, picking):
        return {
            'priority': 10,
            'identity_key': f'integration_send_picking-{self.id}-{picking.id}',
            'description': f'{self.name}: Send Picking "{picking.name}" ({picking.id})',
        }

    def get_integration_lang_code(self):
        """
        Returns the language code (e.g., `en_US`) based on the `integration_lang_id` field value.
        Essential conditions: the language must be required and active.
        """
        self.ensure_one()

        lang = self.integration_lang_id

        if not lang:
            raise UserError(_(
                f'%s: Integration language is not defined.\n\n'
                f'Please go to "E-Commerce Integrations → Stores → {self.name} → Quick Configuration wizard" '
                f'and set the "Integration Language".'
            ) % self.name)

        code = lang.code

        if not self.env['res.lang']._lang_get(code):
            raise UserError(_(
                '%s: The language "%s" is inactive or not available in Odoo.\n\n'
                'Please ensure that the language is active in the system settings by going to '
                '"Settings → Translations → Languages".'
            ) % (self.name, code))

        return code

    def get_shop_lang_code(self):
        """
        Return language code like `en_US` based on the `lang` adapter property
        with essential conditions: required=True, active=True
        """
        lang = self._get_shop_lang()
        return lang.code

    def get_adapter_lang_code(self):
        """
        Return language code like the `mapping-formatted` code:
            '1', 'en', 'eng', 'Eng', 'en_EN', etc.
        """
        return self.adapter.lang

    def _get_shop_lang(self, raise_error=True):
        external_language_code = self.get_adapter_lang_code()

        lang = self.env['res.lang'].from_external(
            self,
            external_language_code,
            raise_error=raise_error,
        )

        if raise_error:
            assert lang.active, f'Inactive language: {lang.code}'

        return lang

    def import_stock_levels_integration(self, location_line):
        self.ensure_one()

        idx = int()
        adapter = self._build_adapter()
        limit = self.get_external_block_limit()

        location = location_line.erp_location_id
        external_location_code = location_line.external_location_id.code

        stock_levels_data = adapter.get_stock_levels(external_location_code)
        stock_levels = [(key, value) for key, value in stock_levels_data.items()]
        self = self.with_context(company_id=self.company_id.id)

        while stock_levels:
            idx += 1
            job_kwargs = self._job_kwargs_apply_stock_multi(location_line, block=idx)

            job = self \
                .with_delay(**job_kwargs) \
                .run_apply_stock_levels_by_blocks(stock_levels[:limit], location)

            self.job_log(job)
            stock_levels = stock_levels[limit:]

        return True

    def run_apply_stock_levels_by_blocks(self, stock_levels, location):
        for external_code, qty in stock_levels:
            self._integration_apply_stock_qty(location, external_code, qty)
        return location, stock_levels

    def _integration_apply_stock_qty(self, location, external_code, qty, delay=True):
        ProductProductExternal = self.env['integration.product.product.external'].with_context(
            company_id=self.company_id.id,
        )
        external_id = ProductProductExternal.get_external_by_code(
            self,
            external_code,
            raise_error=False,
        )
        if not external_id:
            _logger.warning(
                '%s: import stock levels. Cannot find external record "%s" in ERP',
                self.name,
                external_code,
            )
            return False

        if delay:
            job_kwargs = self._job_kwargs_apply_stock_single(external_code, location)
            job = external_id\
                .with_delay(**job_kwargs) \
                .apply_stock_levels(qty, location)

            erp_product = external_id.mapping_model.to_odoo(
                self,
                external_code,
                raise_error=False,
            )
            record = erp_product or external_id
            record.with_context(default_integration_id=self.id).job_log(job)
        else:
            job = external_id.apply_stock_levels(qty, location)

        return job

    def _get_trackable_fields(self):
        """Get fields that can be updated on external system"""
        field_ids = self.env['product.ecommerce.field.mapping'].search([
            ('active', '=', True),
            ('send_on_update', '=', True),
            ('integration_id', '=', self.id),
        ])
        return field_ids.sudo().trackable_fields_rel

    def _is_need_export_product(self, field_vals):
        """
        :field_vals: dict from product `create` or `write` method
        """
        if not self.job_enabled('export_template'):
            return False

        self_su = self.sudo()
        changed_fields = self_su.env['ir.model.fields'].sudo().search([
            ('name', 'in', list(field_vals.keys())),
            ('model', 'in', ('product.template', 'product.product')),
        ])

        trackable_fields = self_su._get_trackable_fields()
        trackable_fields |= self_su.global_tracked_fields

        return bool(changed_fields & trackable_fields)

    def _is_need_export_images(self, vals):
        if not (self.job_enabled('export_template') and self.allow_export_images):
            return False

        return bool(set(IMAGE_FIELDS).intersection(set(vals.keys())))

    def _get_original_from_translations(self, translations):
        """
        Retrieve the original text from a dictionary of translations based on the integration language.

        :param translations: dict containing language keys and their respective translations.
        :return: The original translation in the integration language or the full translation dictionary if not found.
        """
        if not (isinstance(translations, dict) and translations.get('language')):
            return translations

        odoo_lang = self.integration_lang_id

        if odoo_lang.id not in translations['language']:
            raise ApiImportError(_(
                'Cannot find the default language with ID "%s" in the list of translations: %s.\n\n'
                'Please ensure that the correct language is configured in the e-commerce system.'
            ) % (odoo_lang.id, list(translations['language'].keys())))

        return translations['language'][odoo_lang.id]

    def _prepare_inventory_data(self, product, ext_product, ext_location_id):
        """
        Prepare inventory data for export.

        :param product: The Odoo product record.
        :param ext_product: The external product.
        :param ext_location_id: The external location ID for which inventory data is being prepared.
        :return: A dictionary containing inventory data.
        """

        qty_field = self.synchronise_qty_field
        qty = 0  # Default value

        if self.update_stock_for_manufacture_boms and product.bom_ids:
            qty = product._compute_qty_producible(qty_field)
        elif hasattr(product, qty_field):
            qty = getattr(product, qty_field)
        else:
            raise ValidationError(_(
                f'There is no {qty_field} field in product.product module to get quantity of '
                'product for inventory export'
            ))

        return {
            'qty': qty,
            'external_reference': ext_product.external_reference,
            'external_location_id': ext_location_id,
        }

    def open_import_export_integration_wizard(self):
        """
        Returns action window with 'Import/Export Integration Wizard'
        """
        self.ensure_one()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Import/Export Integration Wizard'),
            'res_model': 'import.export.integration.wizard',
            'view_mode': 'form',
            'view_id': self.env.ref('integration.import_export_integration_wizard_form').id,
            'target': 'new',
            'context': self.env.context,

        }

    @staticmethod
    def _raise_notification(ttype: str, message: str):
        """
        :ttype:
            - success
            - warning
        """
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': message,
                'type': ttype,
                'sticky': False,
            }
        }

    def _get_input_file(self, external_order_id):
        input_file = self.env['sale.integration.input.file'].search([
            ('si_id', '=', self.id),
            ('name', '=', str(external_order_id)),
        ], limit=1)

        if not input_file:
            message = (
                f'{self.name}: Integration Input-file not found '
                f'(external_id={external_order_id})'
            )
            _logger.warning(message)
            return False

        if not input_file.order_id:
            input_file.mark_for_update()

            if self.save_webhook_log:
                self._save_log({
                    'name': f'Input file {input_file.name} changelog',
                    'type': 'client',
                    'level': 'DEBUG',
                    'dbname': self.env.cr.dbname,
                    'message': 'Input file was marked as "Update Required"',
                    'path': self.__module__,
                    'func': self.__class__.__name__,
                    'line': str(input_file.si_id),
                })

            _logger.warning(f'{self.name}: Odoo Sales Order not found (external_id={external_order_id})')
            raise ValidationError(
                _('Odoo Sales Order not found (external_id=%s)') % external_order_id
            )

        return input_file

    def _get_order_sub_status_tuple(self, status_code):
        _name = 'sale.order.sub.status'

        sub_status_id = self.env[_name].from_external(self, status_code, False)
        external_sub_status = self.env[f'integration.{_name}.external'] \
            .get_external_by_code(self, status_code, False)

        if not sub_status_id:
            _logger.error(
                f'{self.name}: Store order status not found (status_code={status_code})'
            )

        return sub_status_id, external_sub_status

    @staticmethod
    def _validate_external_id(external_id):
        """
        Validate the external ID.
        Raises:
            ValidationError: If the external ID is invalid.
        """
        if not external_id:
            _logger.warning(f'Invalid external ID: {external_id}')
            raise ValidationError(f'Invalid external ID: {external_id}.')

        try:
            external_id = int(external_id)

            if external_id <= 0:
                _logger.warning(f'Invalid external ID: {external_id}')
                raise ValidationError(f'Invalid external ID: {external_id}.')

        except (ValueError, TypeError):
            _logger.warning(f'Invalid external ID: {external_id}')
            raise ValidationError(f"Invalid external ID: {external_id}.")

        return external_id

    def _get_external_product(self, external_id):
        """
        Get the external product by ID.
        """
        try:
            self._validate_external_id(external_id)
        except ValidationError:
            return None

        record = self.env['integration.product.template.external'] \
            .get_external_by_code(self, external_id, raise_error=False)

        return record

    def _save_log(self, vals):
        try:
            db_registry = registry(self.env.cr.dbname)
            with db_registry.cursor() as new_cr:
                new_env = api.Environment(new_cr, SUPERUSER_ID, {})
                log = new_env['ir.logging'].create(vals)
        except Exception as ex:
            log = self.env['ir.logging']

        return log
