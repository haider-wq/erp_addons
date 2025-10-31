# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _
from odoo.exceptions import UserError, ValidationError

from ...exceptions import ApiImportError


REFUND_FOUND_ERROR = """\n\n
The order includes a "refund" transaction, which cannot be applied automatically.
To proceed, choose one of the following options:

- Adjust Auto-Workflow Settings
    - Uncheck the "Auto-Apply Payments from E-Commerce System" option in the
      E-Commerce Integrations → Stores → <Your Store> → Sales Orders tab.
    - Re-run this job to validate the invoice and register payments automatically.
      The connector will ignore transaction details from the e-commerce system and register payment based on
      validated invoice instead.

- Manually Process Transactions
    - Open sales order in Odoo and review transaction details in the E-Commerce Integration tab.
    - Process payments and refunds manually in the associated Odoo invoice to align with the e-commerce system.
    - Mark the auto-workflow steps as completed after adjustments ("Integration Workflow" button on sales order in Odoo)
"""


class ExternalOrderTransaction(models.Model):
    _name = 'external.order.transaction'
    _inherit = 'external.order.resource'
    _description = 'External Order Transaction'

    transaction = fields.Char(
        string='Transaction ID',
    )
    kind = fields.Selection(
        selection=[
            ('authorization', 'Authorization'),
            ('capture', 'Capture'),
            ('sale', 'Sale'),
            ('void', 'Void'),
            ('refund', 'Refund'),
            ('other', 'Other'),
        ],
        string='Type',
        default='other',
        help="""
            - authorization: Money that the customer has agreed to pay.
                The authorization period can be between 7 and 30 days (depending on your payment service)
                while a store waits for a payment to be captured.
            - capture: A transfer of money that was reserved during the authorization of a shop.
            - sale: The authorization and capture of a payment performed in one single step.
            - void: The cancellation of a pending authorization or capture.
            - refund: The partial or full return of captured money to the customer.
            - other: Any other type of transaction.
        """,
    )
    amount = fields.Char(
        string='Amount',
    )
    currency = fields.Char(
        string='Currency',
    )
    gateway = fields.Char(
        string='Gateway',
    )
    external_parent_str_id = fields.Char(
        string='Parent ID',
    )
    payment_ids = fields.One2many(
        comodel_name='account.payment',
        inverse_name='integration_transaction_id',
        string='Payments',
    )
    external_process_date = fields.Date(
        string='Process Date',
        default=fields.Date.today,
    )

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.erp_order_id.name}: {rec.name}'

    def _compute_is_ecommerce_ok(self):
        for rec in self:
            rec.is_ecommerce_ok = (rec.external_status == 'success')

    @property
    def float_amount(self):
        return float(self.amount)

    @property
    def is_refund(self):
        return self.kind == 'refund'

    def _validate(self):
        """
        Apply received order payment in Odoo.

        :return: tuple(bool, int)
        """
        self.internal_info = False

        if self.is_done:
            return True, []

        if not self.is_ecommerce_ok:
            self.internal_info = _('Skipped due to external restrictions')
            self.mark_skipped()
            return False, []

        invoices = self.erp_order_id.actual_invoice_ids\
            .filtered(lambda x: x.invoice_is_posted and x.invoice_to_pay)

        if not invoices:
            self.internal_info = _('There are no unpaid invoices.')
            self.mark_skipped()
            return False, []

        wizard = self.env['account.payment.register'] \
            .with_context(
                active_ids=invoices.ids,
                active_model=invoices._name,
                default_integration_id=self.integration_id.id,
            ).create({
                'amount': self.get_amount(),
                'journal_id': self.get_journal(),
                'payment_difference_handling': 'open',
            })

        if wizard.payment_difference < 0:
            wizard.payment_difference_handling = 'reconcile'
            wizard.writeoff_account_id = self.get_writeoff_account()

        try:
            payments = wizard._create_payments()
        except (UserError, ValidationError) as ex:
            self.internal_info = ex.args[0]
            self.mark_failed()
            return False, []

        self._add_payment_ids(payments.ids)
        self.mark_done()

        return True, payments.ids

    def get_journal(self):
        if self.gateway:
            payment_method = self.env['sale.order.payment.method'].from_external(self.integration_id, self.gateway)
            payment_method_external = payment_method.to_external_record(self.integration_id)
            payment_method_external._raise_for_missing_journal()

            journal = payment_method_external.payment_journal_id
        else:
            journal = self.erp_order_id.integration_pipeline.get_payment_journal_or_raise()

        return journal.id

    def get_amount(self):
        external_currency = self.env['res.currency'].search([
            ('name', '=ilike', self.currency.lower()),
        ], limit=1)

        if not external_currency:
            raise ApiImportError(
                _('Currency ISO code "%s" was not found in Odoo.') % self.currency
            )

        currency = self.erp_order_id.invoice_ids\
            .filtered(lambda x: x.invoice_is_posted)[0] \
            .currency_id

        if currency.id != external_currency.id:
            amount = currency._convert(
                from_amount=self.float_amount,
                to_currency=currency,
                company=self.erp_order_id.company_id,
                date=self.external_process_date,
            )
        else:
            amount = self.float_amount

        return amount

    def get_writeoff_account(self):
        writeoff_account = self.integration_id.integration_writeoff_account_id

        if not writeoff_account:
            raise ValidationError(_('%s: No Write-Off Account defined. ') % self.integration_name)

        return writeoff_account.id

    def _add_payment_ids(self, ids):
        self.payment_ids = [(4, id_, 0) for id_ in ids]

    def _raise_if_refund_found(self):
        if any(x.is_refund for x in self if not x.is_done):
            raise ValidationError(REFUND_FOUND_ERROR)
