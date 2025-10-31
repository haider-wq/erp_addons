{
    'name': 'Odoo Shopify Connector PRO',
    'category': 'Sales',
    'version': '18.0.1.12.2',
    'author': 'erptechnicals',
    'website': 'erptechnicals.com',
    'support': 'erptechnicals',
    'license': 'OPL-1',
    'depends': [
        'integration',
    ],
    'data': [
        # Security
        'security/ir.model.access.csv',
        # Data
        'data/ir_config_parameter_data.xml',
        'data/product_ecommerce_fields.xml',
        # Wizard
        'wizard/configuration_wizard_shopify.xml',
        'wizard/sale_order_cancel_views.xml',
        # Views
        'views/sale_order_views.xml',
        'views/delivery_carrier_views.xml',
        'views/sale_integration.xml',
        'views/fields/product_ecommerce_field.xml',
        'views/metafield_mapping_views.xml',
        # External
        'views/external/external_order_risk_views.xml',
        'views/external/external_sale_channel_views.xml',
        'views/external/external_order_source_name_views.xml',
        'views/external/menu.xml',
    ],
    'demo': [
    ],
    'external_dependencies': {
        'python': [
            'ShopifyAPI',
        ],
    },
    'installable': True,
    'application': True,
    "cloc_exclude": [
        "**/*"
    ]
}
