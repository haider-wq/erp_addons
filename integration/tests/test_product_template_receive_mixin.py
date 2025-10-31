# See LICENSE file for full copyright and licensing details.

from unittest.mock import patch

from odoo.tests import tagged

from .config.integration_init import OdooIntegrationInit
from ..models.sale_integration import SaleIntegration
from ..models.fields.receive_fields_product_template import ProductTemplateReceiveMixin


class TestProductTemplateReceive(ProductTemplateReceiveMixin):
    def __init__(self, integration):
        self.integration = integration


@tagged('post_install', '-at_install', 'test_integration_core')
class TestProductTemplateReceiveMixin(OdooIntegrationInit):

    def setUp(self):
        super(TestProductTemplateReceiveMixin, self).setUp()

    # integration/models/fields/receive_fields_product_template.py
    @patch.object(SaleIntegration, 'get_adapter_lang_code')
    def test_parse_langs(self, mock_get_adapter_lang_code):
        """
        Test the _parse_langs method of a class.

        This method tests the _parse_langs method's behavior when provided with different
        input scenarios.

        Note: The logic involving 'variations' is specific to testing in the context of
        Magento 2 integration.

        Args:
            mock_get_adapter_lang_code (MagicMock): A mock object for
            the get_adapter_lang_code method.
        Returns:
            None
        """
        # create instance
        instance = TestProductTemplateReceive(self.integration_no_api_1)

        # mock get_adapter_lang_code method
        mock_get_adapter_lang_code.return_value = 'en_US'

        # check if vals does not have attr
        vals = {'name': 'Test'}
        result_1 = instance._parse_langs(vals, 'description', {})
        self.assertIsNone(result_1)

        # check if vals has attr
        result_2 = instance._parse_langs(vals, 'name', {})
        self.assertEqual(result_2, 'Test')
