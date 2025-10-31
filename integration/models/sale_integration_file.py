# See LICENSE file for full copyright and licensing details.

import base64
import json

from odoo import fields, models, api, _
from odoo.exceptions import UserError, ValidationError


class SaleIntegrationFile(models.Model):
    _name = 'sale.integration.file'
    _description = 'External Order File'
    _order = 'create_date desc'

    _sql_constraints = [
        (
            'name_uniq', 'unique(si_id, name)',
            'Order name must be unique by partner!'
        )
    ]

    name = fields.Char(
        string='Name',
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('create_order', 'Create Order'),
            ('workflow_process', 'Run Workflow'),
            ('done', 'Done'),
            ('cancelled', 'Cancelled'),
            ('skipped', 'Skipped'),
            ('unknown', 'Unknown'),
        ],
        string='State',
        default='draft',
        readonly=True,
        copy=False,
    )
    si_id = fields.Many2one(
        comodel_name='sale.integration',
        string='Integration',
        required=True,
        ondelete='cascade',
        readonly=True,
    )
    file = fields.Binary(
        string='File',
    )
    order_id = fields.Many2one(
        string='Sales Order',
        comodel_name='sale.order',
        ondelete='set null',
    )
    raw_data = fields.Text(
        string='Raw Data in JSON',
        required=True,
        default='',
    )
    update_required = fields.Boolean(
        string='Update Required',
    )

    def action_done(self):
        self.unmark_for_update()
        return self.write({'state': 'done'})

    def action_create_order(self):
        return self.write({'state': 'create_order'})

    def action_process(self):
        self.unmark_for_update()
        return self.write({'state': 'workflow_process'})

    def action_cancel(self):
        records = self.filtered(lambda s: s.state in ['draft', 'unknown'])
        return records.write({'state': 'cancelled'})

    def action_draft(self):
        records = self.filtered(lambda s: s.state in ['cancelled', 'done'] and not s.order_id)
        return records.write({'state': 'draft'})

    def mark_for_update(self):
        records = self.filtered(lambda s: s.state != 'cancelled')
        return records.write({'update_required': True})

    def unmark_for_update(self):
        return self.write({'update_required': False})


class SaleIntegrationInputFile(models.Model):
    _name = 'sale.integration.input.file'
    _inherit = 'sale.integration.file'
    _description = 'External Order'

    display_data = fields.Text(
        string='External Order Data',
        compute='_compute_display_data',
        inverse='_inverse_display_data',
    )

    order_reference = fields.Char(
        string='Order Reference',
        compute='_compute_order_reference',
        help='Reference received from the input file',
    )
    has_error = fields.Boolean(
        string='Has Error',
        compute='_compute_has_error',
    )

    @api.depends('state')
    def _compute_has_error(self):
        for rec in self:
            rec.has_error = rec._has_error()

    def _has_error(self):
        if self.state not in ('create_order', 'workflow_process'):
            return False

        order = self.order_id
        if not order:
            return True
        pipeline = order.integration_pipeline
        if not pipeline:
            return True
        if not pipeline.is_done:
            return True

        return False

    @api.model_create_multi
    def create(self, vals_list):
        records = super(SaleIntegrationInputFile, self).create(vals_list)

        if self.env.context.get('skip_create_order_from_input'):
            return records

        for rec in records:
            rec.process()

        return records

    def _get_integration_id_for_job(self):
        return self.si_id.id

    def _get_external_reference(self):
        return self._get_external_reference_root('')

    def _get_external_reference_root(self, key):
        if not key:
            return ''

        try:
            data_dict = json.loads(self.raw_data)
        except json.decoder.JSONDecodeError:
            data_dict = {}

        for key in key.split('.'):
            data_dict = data_dict.get(key, {})
        return data_dict or ''

    @api.depends('raw_data')
    def _compute_order_reference(self):
        for input_file in self:
            order_reference = input_file._get_external_reference() if self.si_id else ''
            input_file.order_reference = order_reference

    @api.depends('file', 'raw_data')
    def _compute_display_data(self):
        for input_file in self:
            try:
                input_file.display_data = json.dumps(
                    input_file.with_context(bin_size=False).to_dict(),
                    indent=4,
                )
            except json.decoder.JSONDecodeError:
                input_file.display_data = '{}'

    def _inverse_display_data(self):
        for input_file in self:
            # Check if display_data is empty
            if not input_file.display_data:
                raise UserError(_(
                    'No data provided for processing.\n\n'
                    'The file appears to be empty. Please ensure that the input file contains valid data.'
                ))

            try:
                # Attempt to load JSON data
                json.loads(input_file.display_data)
                input_file.raw_data = input_file.display_data
            except json.decoder.JSONDecodeError as e:
                # Raise a technical error indicating the file format is incorrect
                raise UserError(_(
                    'The file format is incorrect.\n\n'
                    'An error occurred while processing the file: %s\n'
                    'Please ensure the file is a valid JSON format and try again.'
                ) % e.msg)

            # If no errors, assign the raw data
            input_file.raw_data = input_file.display_data

    def to_dict(self):
        self.ensure_one()

        if self.raw_data:
            json_str = self.raw_data
        else:
            json_str = base64.b64decode(self.file)

        data = json.loads(json_str)
        data['_odoo_id'] = self.id

        return data

    def print_parsed_data(self):
        self.ensure_one()
        data = self.parse()

        wizard = self.env['message.wizard'].create({
            'message': json.dumps(data, indent=8),
        })

        return wizard.run_wizard('integration_message_wizard_form')

    def parse(self):
        self.ensure_one()

        if self.update_required:
            if not self._update_from_external():
                raise ValidationError(_(
                    'Failed to update external order information from the store.\n\n'
                    'This issue is most commonly caused by the order being removed from the e-commerce system. '
                    'If the order is no longer available, the connector cannot update its data.\n\n'
                    'Please ensure the external system is accessible and the order still exists. '
                    'If the issue persists, contact support: https://support.ventor.tech/'
                ))

        data = self.si_id.adapter.parse_order(self.to_dict())
        data['related_input_files'] = [(6, 0, self.ids)]

        return data

    def process(self):
        self.ensure_one()

        if self.order_id:
            self.action_process()

            job_kwargs = self._job_kwargs_process_input_file()
            job = self \
                .with_context(company_id=self.si_id.company_id.id) \
                .with_delay(**job_kwargs).run_current_pipeline()

            self.order_id.job_log(job)
        else:
            self.action_create_order()

            si = self.si_id.with_context(company_id=self.si_id.company_id.id)
            job_kwargs = si._job_kwargs_create_order_from_input(self)

            job = si.with_delay(**job_kwargs).create_order_from_input(self)

            self.job_log(job)

        return job

    def process_no_job(self):
        self.ensure_one()

        if self.order_id:
            self.action_process()
            job_kwargs = self._job_kwargs_process_input_file()

            job = self \
                .with_context(company_id=self.si_id.company_id.id) \
                .with_delay(**job_kwargs).run_current_pipeline()

            self.order_id.job_log(job)
            return job

        self.action_create_order()

        return self.si_id.create_order_from_input(self)

    def cancel_order_in_ecommerce_system(self, params: dict):
        self.ensure_one()
        return self.si_id.adapter.cancel_order(self.name, params)

    def action_update_from_external(self):
        results = list()

        for rec in self:
            res = rec._update_from_external()
            results.append(res)

        return results

    def run_actual_pipeline(self, skip_dispatch=True):
        self.ensure_one()

        if not self.order_id:
            return False

        data = self._prepare_actual_pipeline_data()
        if not data:
            return False

        return self._build_and_run_order_pipeline(data, skip_dispatch=skip_dispatch)

    def run_current_pipeline(self, skip_dispatch=False):
        self.ensure_one()

        if not self.order_id:
            return False

        data = self.parse()
        return self._build_and_run_order_pipeline(data, skip_dispatch=skip_dispatch)

    def update_current_pipeline(self):
        self.ensure_one()

        pipiline = self.order_id.integration_pipeline
        if not pipiline:
            return {}, {}

        data = self.parse()
        return pipiline._update_pipeline(data)

    def open_pipeline_form(self):
        pipeline = self.order_id.integration_pipeline

        if pipeline and not pipeline.input_file_id:
            pipeline.input_file_id = self.id

        return self.order_id.action_integration_pipeline_form()

    def open_job_logs(self):
        self.ensure_one()
        job_log_ids = self.env['job.log'].search([
            ('input_file_id', '=', self.id),
        ])
        return job_log_ids.open_tree_view()

    def action_update_current_pipeline(self):
        for rec in self:
            rec.update_current_pipeline()

    def action_run_current_pipeline(self):
        for rec in self:
            rec.run_current_pipeline()

    def _update_from_external(self):
        self.ensure_one()

        integration = self.si_id
        adapter = integration._build_adapter()
        input_data = adapter.receive_order(self.name)

        if not input_data:
            return False

        self.raw_data = json.dumps(input_data['data'], indent=4)
        return True

    def _job_kwargs_process_input_file(self):
        return {
            'priority': 9,
            'description': f'{self.si_id.name}: "{self.name}" >> Create Order From input',
            'identity_key': f'process_input_file_{self.si_id}_{self.name}',
        }

    def run_cancel_order_job(self, data):
        self.ensure_one()

        order = self.order_id.with_context(company_id=self.si_id.company_id.id)
        if not order:
            return False

        job_kwargs = order._job_kwargs_run_integration_workflow(task='cancel', priority=5)
        job_kwargs['description'] = f'{self.si_id.name}: Order № "{order.display_name}" >> Cancel Order (by webhook)'

        job = self.with_context(company_id=self.si_id.company_id.id) \
            .with_delay(**job_kwargs) \
            ._run_cancel_order(data)

        order.job_log(job)

        return job

    def _run_cancel_order(self, data):
        order = self.order_id

        # Additional Order adjustments
        updated_data = order._adjust_integration_external_data(data)
        order.with_context(skip_integration_order_post_action=True)._apply_values_from_external(updated_data)

        # Cancel order without sending info to the e-commerce system
        order._integration_action_cancel_no_dispatch()

        return True

    def _prepare_actual_pipeline_data(self):
        adapter = self.si_id._build_adapter()
        input_data = adapter.receive_order(self.name)

        if not input_data:
            return False

        order_data = adapter.parse_order(input_data['data'])
        return {
            'payment_method': order_data.get('payment_method'),
            'integration_workflow_states': order_data.get('integration_workflow_states', []),
            'external_tags': order_data['external_tags'],
        }

    def _build_and_run_order_pipeline(self, data, skip_dispatch=True):
        order = self.order_id.with_context(default_skip_dispatch=skip_dispatch)

        if not order:
            return False

        # Additional Order adjustments
        updated_data = order._adjust_integration_external_data(data)
        order._apply_values_from_external(updated_data)

        # Build and run workflow
        return order._build_and_run_integration_workflow(updated_data)

    def _get_file_id_for_log(self):
        return self.id

    def open_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
