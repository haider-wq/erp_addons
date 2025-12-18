import json
import pytz
import random
import requests
from odoo import api, fields, models,_
from odoo.exceptions import ValidationError
from dateutil.relativedelta import relativedelta
from datetime import timedelta
from odoo.tools import date_utils, email_split, is_html_empty, groupby, parse_contact_from_email, SQL





class InheritStudentCourseReg(models.Model):
    _inherit = "op.subject.registration"

    course_id = fields.Many2one('op.course', 'Course',required=False,
                                tracking=True)


class InheritStudentCourse(models.Model):
    _inherit = "op.student.course"

    course_id = fields.Many2one('op.course', 'Course', required=False, tracking=True)

class InheritStudent(models.Model):
    _inherit = "op.student"

    gender = fields.Selection(
        [('m', 'Male'), ('f', 'Female'), ('o', 'Other')],
        string='Gender',
        required=False)


class InheritOpBatch(models.Model):
    _inherit = "op.batch"

    program_id = fields.Many2one('op.program',required=True)
    courses_line_ids = fields.One2many('op.admission.fees.line','batch_id',string='Program Courses')
    course_id = fields.Many2one('op.course','Course',required=False)

    @api.onchange('program_id')
    def _onchange_program_id(self):
        courses = self.env['op.course'].search([('program_id', '=', self.program_id.id)])

        # Clear existing admission fee lines
        self.courses_line_ids = [(5, 0, 0)]  # This removes all existing lines

        # Create new lines
        fee_lines = []
        sequence = 1
        for course in courses:
            fee_lines.append((0, 0, {
                'course_id': course.id,
                'sequence': sequence,
            }))
            sequence += 1

        self.courses_line_ids = fee_lines

class InheritAdmissionRegisterFeesLine(models.Model):
    _inherit = 'op.admission.fees.line'

    batch_id = fields.Many2one('op.batch',string="Course",required=True)
    sequence = fields.Integer()

class InheritAdmissionRegister1(models.Model):
    _inherit = 'op.admission'

    batch_id = fields.Many2one('op.batch','Batch',domain="batch_domain",required=False)
    gender = fields.Selection(
        [('m', 'Male'), ('f', 'Female'), ('o', 'Other')],
        string='Gender',
        required=False)
    batch_domain = fields.Char(compute="_compute_batch_domain")

    @api.depends('program_id')
    def _compute_batch_domain(self):
        for rec in self:
            batches_ids = []
            batches = self.env['op.batch'].search([('program_id', '=',rec.program_id.id)])
            if batches:
                batches_ids = batches.mapped('id')
            if not batches_ids:
                rec.batch_domain = json.dumps([])
            else:
                domain = [('id','in',batches_ids)]
                rec.batch_domain = json.dumps(domain)


class InheritAdmissionRegister(models.Model):
    _inherit = 'op.admission.register'






    @api.onchange('program_id')
    def _onchange_program_id(self):
        courses = self.env['op.course'].search([('program_id', '=', self.program_id.id)])
        # Clear existing admission fee lines
        self.admission_fees_line_ids = [(5, 0, 0)]  # This removes all existing lines
        # Create new lines
        fee_lines = []
        for course in courses:
            fee_lines.append((0, 0, {
                'course_id': course.id,
            }))

        self.admission_fees_line_ids = fee_lines




class InheritAdmission(models.Model):
    _inherit = 'op.admission'

    crm_id = fields.Many2one('crm.lead')
    course_id = fields.Many2one('op.course', 'Course',required=False)
    last_name = fields.Char('Last Name', required=False, translate=True)
    first_name = fields.Char('First Name',required=False, translate=True)
    birth_date = fields.Date('Birth Date', required=False)
    batch_id = fields.Many2one('op.batch', 'Batch', required=False)
    register_id = fields.Many2one('op.admission.register', 'Admission Register', required=False)
    gender = fields.Selection([('m', 'Male'), ('f', 'Female'), ('o', 'Other')],string='Gender',required=False)
    program_id = fields.Many2one('op.program')

    @api.constrains('register_id', 'application_date')
    def _check_admission_register(self):
        for rec in self:
            if rec.register_id:
                start_date = fields.Date.from_string(rec.register_id.start_date)
                end_date = fields.Date.from_string(rec.register_id.end_date)
                application_date = fields.Date.from_string(rec.application_date)
                if application_date < start_date or application_date > end_date:
                    raise ValidationError(_(
                        "Application Date should be between Start Date & End Date of Admission Register."))

class PartnerInherit(models.Model):
    _inherit = 'res.partner'

    last_name = fields.Char()
    program_id = fields.Many2one('op.program')
    record_type = fields.Selection(
        [('a', 'Administrative'),('b', 'Academic Program'),('c', 'Business Organization'),('d', 'Career Services'),('e', 'Customer'),('f', 'Educational Institution'),('g','Household Account'),('h','Sports Organization'),('i', 'University Department'),('j','Vendor'),('k','Advertisement'),('l','Partner'),('m','Website'),('n','ERP College')],
        string='Record type',
        required=False)
    is_owner = fields.Boolean()
    student_mobile = fields.Char()
    is_student_address = fields.Boolean(default=False)
    student_street = fields.Char()
    student_street2 = fields.Char()
    student_zip = fields.Char()
    student_city = fields.Char()
    student_state_id = fields.Many2one("res.country.state")
    student_country_id = fields.Many2one('res.country')
    primary_contact = fields.Many2one('res.partner',store=True)


class Lead2OpportunityPartner(models.TransientModel):
    _inherit = 'crm.lead2opportunity.partner'

    partner_id = fields.Many2one(
        'res.partner',
        'Customer',
        domain="[('customer_rank', '>', 0)]",
        compute='_compute_partner_id',
        readonly=False,
        store=True,
        compute_sudo=False
    )

    def _compute_partner_id(self):
        pass

    @api.depends('lead_id', 'partner_id')
    def _compute_duplicated_lead_ids(self):
        for convert in self:
            active_id = convert.env.context.get('active_id')
            lead = self.env['crm.lead'].browse(active_id)

            opportunity = self.env['crm.lead'].search([
                ('type', '=', 'opportunity'),
                # ('id', '=',active_id)  # exclude current lead
                ('name', '=', lead.name),
                # ('create_date', '>=',fields.Date.today())
            ])
            convert.duplicated_lead_ids = opportunity.ids


    def _action_convert(self):
        """ """
        result_opportunities = self.env['crm.lead'].browse(self._context.get('active_ids', []))
        vendor_partner = self.env['res.partner'].search([('name', '=', result_opportunities.partner_id.name)], limit=1)
        owner_id = self.env['res.partner'].search([('name', '=', result_opportunities.owner_id.name)])
        vendor_partner.write({'supplier_rank': 1, 'customer_rank': 0, 'company_type': 'company'})
        partner_id = self.env['res.partner'].search([
            ('name', '=', result_opportunities.name),
            '|', '|',
            ('phone', '=', result_opportunities.phone),
            ('mobile', '=', result_opportunities.mobile),
            ('email', '=', result_opportunities.email_from),
            ('parent_id', '=', vendor_partner.id)
        ])

        if not partner_id:
            partner = self.env['res.partner'].create({
                'name': result_opportunities.name,
                'last_name': result_opportunities.last_name,
                'parent_id': vendor_partner.id,
                'primary_contact':result_opportunities.owner_id.id,
                'mobile': result_opportunities.student_mobile,
                'phone': result_opportunities.student_phone,
                'student_street': result_opportunities.student_street,
                'is_student_address':True,
                'student_street2': result_opportunities.student_street2,
                'email': result_opportunities.email_from,
                'program_id': result_opportunities.program_id.id,
                'student_zip': result_opportunities.student_zip,
                'student_city': result_opportunities.student_city,
                'student_state_id': result_opportunities.student_state_id.id

            })

            admission = self.env['op.admission'].create({
                'name': result_opportunities.name,
                # 'first_name': result_opportunities.name,
                'crm_id':result_opportunities.id,
                'email': result_opportunities.email_from,
                'phone': result_opportunities.phone,
                'mobile': result_opportunities.student_mobile,
                'program_id':result_opportunities.program_id.id,
                'street': result_opportunities.student_street,
                'street2': result_opportunities.student_street2,
                'zip':result_opportunities.student_zip,
                'city':result_opportunities.student_city,
                'state_id':result_opportunities.student_state_id.id,
                'country_id':result_opportunities.student_country_id.id


            })

        if not owner_id:
            owner_partner = self.env['res.partner'].create({
                'name': result_opportunities.owner_id.name,
                'customer_rank': 1,
                'parent_id': vendor_partner.id,
                'primary_contact':result_opportunities.owner_id.id,
                'is_owner': True
            })
            print(result_opportunities)

            result_opportunities.write({'contact_id':result_opportunities.owner_id.id})
        if owner_id:
            owner_id.write({'parent_id': vendor_partner.id,'is_student_address':False,'is_owner': True,'primary_contact':result_opportunities.owner_id.id})
        if self.partner_id:
            self.partner_id.write({'parent_id': vendor_partner.id,'primary_contact':result_opportunities.owner_id.id})

        self._convert_and_allocate(result_opportunities, [self.user_id.id], team_id=self.team_id.id)
        return result_opportunities[0]




class CalendarEvent(models.Model):
    _inherit = 'crm.lead'

    student_street = fields.Char()
    student_street2 = fields.Char()
    last_name = fields.Char()
    erp_company_id = fields.Many2one('res.partner')
    contact_id = fields.Many2one('res.partner',readonly=False,store=True,related='erp_company_id.primary_contact')
    partner_id = fields.Many2one('res.partner')

    student_zip = fields.Char()
    Location = fields.Char()
    program_id = fields.Many2one('op.program')
    residance_status = fields.Selection([('a','I am a Citizen or Permanent Resident'),('b','I am an International Student'),('c','I am a Refugee'),('d','I am a Work Permit Holder'),('e','I have a Visitor Visa')])
    lead_source = fields.Selection([('a','Website'),('b','Internal Employee'),('c','Ambassador'),('d','Business Listing'),('e','Social Media'),('f','Facebook'),('g','LinkedIn'),('h','Email'),('f','Google'),('g','Word of mouth'),('h','Employee Referral'),('i','Partner'),('j','Walk-in'),('k','Web'),('l','CTI')])
    asn = fields.Char()
    student_city = fields.Char()
    email = fields.Char(compute='_compute_student_email_info')
    phone = fields.Char(invisible=1)
    mobile = fields.Char(invisible=1)
    vendor_phone = fields.Char(compute='_compute_owner_phone_info',store=True)
    vendor_mobile = fields.Char(compute='_compute_owner_mobile_info',store=True)

    student_phone = fields.Char()
    student_mobile = fields.Char()

    vendor_street = fields.Char(compute='_compute_owner_street_info',store=True)
    vendor_street2 = fields.Char(compute='_compute_owner_street2_info',store=True)
    vendor_zip = fields.Char(compute='_compute_owner_zip_info',store=True)
    vendor_email = fields.Char(compute='_compute_owner_email_info')
    vendor_city = fields.Char(compute='_compute_owner_city_info')
    vendor_state_id = fields.Many2one("res.country.state",compute='_compute_owner_state_info')
    vendor_country_id = fields.Many2one('res.country',compute='_compute_country_info')

    student_state_id = fields.Many2one("res.country.state")
    student_country_id = fields.Many2one('res.country')

    contact_name = fields.Char('Contact Name', index='trigram', tracking=30, readonly=False, store=True)
    owner_id = fields.Many2one('res.partner',readonly=False,compute='_compute_owner_id',store=True)
    email_from = fields.Char(
        'Email', tracking=40, index='trigram',
        compute='_compute_email_from', inverse='_inverse_email_from', readonly=False, store=True)

    class_attendance = fields.Boolean()
    orientation_attended = fields.Boolean()
    govt_id = fields.Boolean()
    student_badge = fields.Boolean()
    english_proficiency_proof = fields.Boolean()
    contract_signature = fields.Boolean()
    sif = fields.Boolean()
    sle = fields.Boolean()
    laptop_allowance = fields.Boolean()
    deposit = fields.Boolean()

    @api.onchange('partner_id')
    def clear_vendor_address(self):
        for rec in self:
            if rec.partner_id:
                rec.street = ''
                rec.street2 = ''
                rec.zip = ''
                rec.city = ''
                rec.state_id = False
                rec.country_id = False
                rec.phone = ''
                rec.mobile = ''





    def _compute_email_from(self):
        pass

    def _inverse_email_from(self):
        pass

    def action_create_admission(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Application'),
            'view_mode':'list,form',
            'domain': [('crm_id', '=',self.id)],
            'res_model':'op.admission',
            'context': {'create': False, 'active_test': False},


        }

    def _find_matching_partner(self, email_only=False):
        """ Try to find a matching partner with available information on the
        lead, using notably customer's name, email, ...

        :param email_only: Only find a matching based on the email. To use
            for automatic process where ilike based on name can be too dangerous
        :return: partner browse record
        """
        self.ensure_one()
        partner = self.partner_id

        if not partner and self.email_from:
            partner = self.env['res.partner'].search([('email', '=', self.email_from),('customer_rank', '>',0)], limit=1)

        if not partner and not email_only:
            # search through the existing partners based on the lead's partner or contact name
            # to be aligned with _create_customer, search on lead's name as last possibility
            for customer_potential_name in [self[field_name] for field_name in ['partner_name', 'contact_name', 'name'] if self[field_name]]:
                partner = self.env['res.partner'].search([('name', 'ilike', customer_potential_name),('customer_rank', '>',0)], limit=1)
                if partner:
                    break

        return partner

    def _create_customer(self):
        """ Create a partner from lead data and link it to the lead.

        :return: newly-created partner browse record
        """
        Partner = self.env['res.partner']
        contact_name = self.contact_name
        if not contact_name:
            contact_name = parse_contact_from_email(self.email_from)[0] if self.email_from else False

        if self.partner_name:
            partner_company = Partner.create(self._prepare_customer_values(self.partner_name, is_company=True))
        elif self.partner_id:
            partner_company = self.partner_id
        else:
            partner_company = None

        if contact_name:
            return Partner.create(self._prepare_customer_values(contact_name, is_company=False, parent_id=partner_company.id if partner_company else False))

        if partner_company:
            return partner_company
        return Partner.create(self._prepare_customer_values(self.name, is_company=False))

    def action_set_won(self):
        """ Won semantic: probability = 100 (active untouched) """
        self.action_unarchive()
        # group the leads by team_id, in order to write once by values couple (each write leads to frequency increment)
        leads_by_won_stage = {}
        for lead in self:
            won_stages = self._stage_find(domain=[('is_won', '=', True)], limit=None)
            partner = self.env['res.partner'].search(
                [('name', '=', lead.name)])
            sis = self.env['op.student'].search([('name', '=', lead.name)])
            if not len(partner) > 1:
                print(partner.name)
                partner.write({'is_student': True, 'customer_rank': 1})
                if sis:
                    sis.write({'name': lead.name, 'partner_id': partner.id, 'first_name': lead.name,
                               'last_name': partner.last_name})
                if not sis:
                    sis.create({'name': lead.name,'partner_id': partner.id, 'first_name': lead.name,
                                'last_name': partner.last_name,'street':partner.student_street,'street2':partner.student_street2,'city':partner.student_city,'zip':partner.student_zip,'state_id':partner.student_state_id.id,'country_id':partner.student_country_id.id})
            else:
                for c in partner:
                    c.write({'is_student': True, 'customer_rank': 1})
            # ABD : We could have a mixed pipeline, with "won" stages being separated by "standard"
            # stages. In the future, we may want to prevent any "standard" stage to have a higher
            # sequence than any "won" stage. But while this is not the case, searching
            # for the "won" stage while alterning the sequence order (see below) will correctly
            # handle such a case :
            #       stage sequence : [x] [x (won)] [y] [y (won)] [z] [z (won)]
            #       when in stage [y] and marked as "won", should go to the stage [y (won)],
            #       not in [x (won)] nor [z (won)]
            stage_id = next((stage for stage in won_stages if stage.sequence > lead.stage_id.sequence), None)
            if not stage_id:
                stage_id = next((stage for stage in reversed(won_stages) if stage.sequence <= lead.stage_id.sequence), won_stages)
            if stage_id in leads_by_won_stage:
                leads_by_won_stage[stage_id] += lead
            else:
                leads_by_won_stage[stage_id] = lead
        for won_stage_id, leads in leads_by_won_stage.items():
            leads.write({'stage_id': won_stage_id.id, 'probability': 100})
        return True

    @api.depends('partner_id.child_ids.is_owner')
    def _compute_owner_id(self):
        for record in self:
            if record.partner_id:
                owner_candidates = record.partner_id.child_ids.filtered(lambda child: child.is_owner and child.create_date).sorted(key=lambda c: c.create_date)
                record.owner_id = owner_candidates[0] if owner_candidates else False
            else:
                record.owner_id = False

    @api.depends('owner_id')
    def _compute_contact_id(self):
        """ compute the new values when partner_id has changed """
        for lead in self:
                lead.contact_id = lead.owner_id

    @api.depends('partner_id')
    def _compute_student_mobile_info(self):
        for lead in self:
            lead.student_mobile = lead.mobile

    @api.depends('partner_id')
    def _compute_student_street_info(self):
        for lead in self:
            lead.student_street = lead.student_street

    @api.depends('partner_id')
    def _compute_student_phone_info(self):
        for lead in self:
            lead.student_phone = lead.student_phone

    @api.depends('partner_id')
    def _compute_student_email_info(self):
        for lead in self:
            lead.email = lead.email

    @api.depends('partner_id')
    def _compute_student_street2_info(self):
        for lead in self:
            lead.student_street2 = lead.student_street2

    @api.depends('partner_id')
    def _compute_student_city_info(self):
        for lead in self:
            lead.student_city = lead.student_city

    @api.depends('partner_id')
    def _compute_student_zip_info(self):
        for lead in self:
            lead.student_zip = lead.student_zip


    @api.depends('partner_id')
    def _compute_owner_mobile_info(self):
        for lead in self:
            lead.vendor_mobile = lead.partner_id.mobile

    @api.depends('partner_id')
    def _compute_owner_phone_info(self):
        for lead in self:
            lead.vendor_phone = lead.partner_id.phone

    @api.depends('partner_id')
    def _compute_owner_street_info(self):
        for lead in self:
            lead.vendor_street = lead.partner_id.street

    @api.depends('partner_id')
    def _compute_owner_street2_info(self):
        for lead in self:
            lead.vendor_street2 = lead.partner_id.street2

    @api.depends('partner_id')
    def _compute_owner_zip_info(self):
        for lead in self:
            lead.vendor_zip = lead.partner_id.zip

    @api.depends('partner_id')
    def _compute_owner_email_info(self):
        for lead in self:
            lead.vendor_email = lead.partner_id.email

    @api.depends('partner_id')
    def _compute_owner_city_info(self):
        for lead in self:
            lead.vendor_city = lead.partner_id.city

    @api.depends('partner_id')
    def _compute_owner_state_info(self):
        for lead in self:
            lead.vendor_state_id = lead.partner_id.state_id

    @api.depends('partner_id')
    def _compute_country_info(self):
        for lead in self:
            lead.vendor_country_id = lead.partner_id.country_id














