# Copyright 2016-2025 Skyalbs Pakistan (https://skylabs.app)
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
# @author Skylabs <skylabs@skyrocket.com.pk>

{
    "name": "Delivery PostEx-Skylabs",
    "version": "18.0.0.0.1",
    "category": "Sales, Stock",
    "license": "OPL-1",
    "development_status": "Production/Stable",
    "summary": "Couriers Integrations can help you to track your courier status",
    "author": "Skylabs",
    "website": "https://skylabs.app",
    "depends": ['base', 'delivery', 'stock_delivery', 'base_address_extended', 'phone_validation'],
    "data": [
        "data/delivery_postex.xml",
        "views/delivery_courier.xml",
    ],
    'assets': {
        'web.assets_backend': [
        ]
    },
    'images': ['static/description/banner.gif'],
    'currency': 'USD',
    'price': 10.00,
    "installable": True,
}
