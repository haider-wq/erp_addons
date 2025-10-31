# See LICENSE file for full copyright and licensing details.

import os

import odoo
from odoo import fields, models, _

REQUIRED_MODULES = [
    'web',
    'queue_job',
    'integration',
]

OPTIONAL_MODULES = [
    'integration_monitoring',
]

ODOOSH_SERVER_EXAMPLE = [
    '[queue_job] \n',
    'channels = root:1\n',
    'scheme = https\n',
    'port = 443\n',
]

CUSTOM_SERVER_EXAMPLE = [
    'workers = 2 ; set here amount of workers higher than 1\n',
    '[queue_job] \n',
    'channels = root:1\n',
]


class IntegrationInstallationWizard(models.TransientModel):
    _name = 'integration.installation.wizard'
    _description = 'Integration Installation Wizard'

    state = fields.Selection(
        selection=[
            ('step_main', 'Main'),
            ('step_success', 'Success'),
            ('step_error', 'Error'),
        ],
        string='State',
        default='step_main',
    )

    errors = fields.Text(
        string='Errors',
        default='No errors',
    )

    odoosh_template = fields.Text('Odoo.sh Template')
    custom_template = fields.Text('Custom Template')

    def check_odoo_setup_for_integration(self):
        """
        Check if the Odoo setup is compatible with the integration.

        This method checks if the Odoo configuration file (odoo.conf) contains the correct 'host'
        parameter, which should match the base URL of the Odoo instance. It also checks if the
        current module is installed as a server-wide module in the Odoo configuration.

        Returns:
            bool: True if the Odoo setup is compatible with the integration, False otherwise.
        """
        config = odoo.tools.config

        # Get the base URL from the configuration parameter
        base_url = self.get_base_url().split('//')[1]

        errors = []

        python_path = os.environ.get('PYTHONPATH') or ''
        is_odoosh = '.odoo.com' in base_url or '/odoo.sh' in python_path

        # Check if the Odoo installation is on Odoo.sh and the configuration file is the default
        # one provided by Odoo.sh
        if is_odoosh and os.path.isfile(config.rcfile):
            host_found = False

            with open(config.rcfile, 'r') as file:
                for line in file:
                    line = line.strip()
                    if line.startswith('host'):
                        if host_found:
                            errors.append(
                                'Multiple "host" parameters found in the "odoo.conf"'
                            )

                        host_found = True

                        # Check if the protocol is included in the host parameter
                        # The host parameter should only contain the domain name,
                        # without the protocol (e.g. "http://")
                        if '//' in line:
                            errors.append(
                                'The "host" parameter in the "odoo.conf" is not set correctly'
                            )

                        if base_url != line.split('=')[1].strip():
                            errors.append(
                                'The "host" parameter in the "odoo.conf" is not set correctly'
                            )

                if not host_found:
                    errors.append('The "host" parameter is not set in the "odoo.conf"')

        # Check installed modules
        modules_str = ','.join(REQUIRED_MODULES)
        server_wide_modules = config.options.get('server_wide_modules', '')

        # If the 'server_wide_modules' parameter is not set in the odoo.conf file, the system
        # automatically adds the 'base' value. This code snippet excludes the 'base' value and
        # any empty strings.
        server_wide_modules = set(
            rec.strip()
            for rec in server_wide_modules.split(',')
            if rec and rec != 'base'
        )

        if set(REQUIRED_MODULES) - server_wide_modules:
            errors.append(
                'The necessary module is not set as a server-wide module in the "odoo.conf"'
            )
            if server_wide_modules - set(REQUIRED_MODULES):
                modules_str = modules_str + ',' + ','.join(
                    server_wide_modules - set(REQUIRED_MODULES))

        installed_modules = self.env['ir.module.module'].search([('state', '=', 'installed')])
        integration_modules = installed_modules.filtered(
            lambda m: m.name.startswith('integration_') and 'extension' not in m.name
        )

        integration_modules_name = set(integration_modules.mapped('name'))
        missing_modules = integration_modules_name - server_wide_modules - set(OPTIONAL_MODULES)
        if missing_modules:
            modules_str = ','.join(REQUIRED_MODULES) + ',' + ','.join(missing_modules)
            errors.append(
                'The necessary module is not set as a server-wide module in the "odoo.conf"'
            )

        if errors:
            server_wide_modules_str = 'server_wide_modules = ' + modules_str + '\n'

            odoosh_template = ODOOSH_SERVER_EXAMPLE.copy()
            odoosh_template.insert(0, server_wide_modules_str)
            odoosh_template.insert(-1, 'host = ' + base_url + '\n')
            self.odoosh_template = ''.join(odoosh_template)

            custom_template = CUSTOM_SERVER_EXAMPLE.copy()
            custom_template.insert(1, server_wide_modules_str)
            self.custom_template = ''.join(custom_template)

            self.errors = ''.join([f'- {e}\n' for e in set(errors)])
            self.state = 'step_error'
        else:
            self.state = 'step_success'

        return {
            'type': 'ir.actions.act_window',
            'name': _('Let\'s ensure a seamless setup!'),
            'res_model': 'integration.installation.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def open_configuration_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Getting Started: E-Commerce Connector Made Easy'),
            'res_model': 'integration.configuration.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def close_wizard(self):
        action_id = self.env.ref('integration.integrations_list_action').id
        menu_id = self.env.ref('integration.view_sale_integration_kanban').id

        url_menu = '/web#action=%d&model=sale.integration&view_type=kanban&menu_id=%d&cids=1' \
            % (action_id, menu_id)

        return {
            'type': 'ir.actions.act_url',
            'url': url_menu,
            'target': 'self',
        }
