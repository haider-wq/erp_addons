# See LICENSE file for full copyright and licensing details.

from typing import Any, Dict, List, Tuple

from odoo import api, fields, models, registry
from odoo.tools import escape_psql

from .sale_integration import SEARCH_CUSTOMER_FIELDS

PROXY_FIELDS = [
    'pricelist_id',
    'person_name',
    'email',
    'language',
    'person_id_number',
    'company_name',
    'company_reg_number',
    'street',
    'street2',
    'city',
    'country',
    'country_code',
    'state',
    'state_code',
    'phone',
    'mobile',
    'other',
    'zip',
]

ADDRESS_MATCH_SIMPLE_FIELDS = [
    'street',
    'street2',
    'city',
    'zip',
    'email',
    'phone',
    'mobile',
]

ADDRESS_MATCH_COMPLEX_FIELDS = [
    'country_id',
    'state_id',
]

PROXY_TYPES = [
    'customer',
    'shipping_address',
    'billing_address',
    'other_address',
]

PARTNER_SEARCH_CRITERIA = [
    ('parent_id', '='),
    ('is_company', '='),
    ('type', '='),
]

COMPANY_SEARCH_CRITERIA = [
    ('name', '=ilike'),
    ('is_company', '='),
]

ADDRESS_SEARCH_CRITERIA = [
    ('name', '=ilike'),
    ('parent_id', '='),
]


class IntegrationResPartnerProxy(models.TransientModel):
    _name = 'integration.res.partner.proxy'
    _description = 'Integration Res Partner Proxy'

    # Fields that should be ignored when comparing addresses for uniqueness
    ADDRESS_UNIQUENESS_IGNORED_FIELDS = [
        'parent_id',
        'type',
        'external_company_name',
        'category_id',
        'lang',
        'active',
        'create_date',
        'write_date',
    ]

    type = fields.Selection(
        selection=[
            ('customer', 'Customer'),
            ('shipping_address', 'Shipping Address'),
            ('billing_address', 'Billing Address'),
            ('other_address', 'Other Address'),
        ],
        string='Proxy Type',
        required=True,
    )

    factory_id = fields.Many2one(
        string='Factory',
        comodel_name='integration.res.partner.factory',
        help=(
            'Factory associated with this proxy.'
        ),
    )

    integration_id = fields.Many2one(
        string='Integration',
        comodel_name='sale.integration',
        related='factory_id.integration_id',
        help=(
            'The Sale integration associated with this proxy.'
        ),
    )

    partner_id = fields.Many2one(
        string='Partner',
        comodel_name='res.partner',
        help=(
            'Technical field for storing the current parent.'
        ),
    )

    company_partner_id = fields.Many2one(
        string='Company',
        comodel_name='res.partner',
        help=(
            'Technical field for storing the current company.'
        ),
    )

    # Fields for customer
    external_id = fields.Char(string='External ID')
    pricelist_id = fields.Char(string='Pricelist ID')
    person_name = fields.Char(string='Person Name', default='')
    email = fields.Char(string='Email', default='')
    phone = fields.Char(string='Phone', default='')
    mobile = fields.Char(string='Mobile', default='')
    language = fields.Char(string='Language')

    # Fields for address
    person_id_number = fields.Char(string='Person ID Number')
    company_name = fields.Char(string='Company Name')
    company_reg_number = fields.Char(string='Company Reg Number')
    street = fields.Char(string='Street')
    street2 = fields.Char(string='Street2')
    city = fields.Char(string='City')
    country = fields.Char(string='Country')
    country_code = fields.Char(string='Country Code')
    state = fields.Char(string='State')
    state_code = fields.Char(string='State Code')
    other = fields.Char(string='Other')
    zip = fields.Char(string='Zip')

    def get_proxy_fields(self) -> List:
        return PROXY_FIELDS

    def get_address_match_simple_fields(self) -> List:
        """
        Returns list of simple fields that are used for address matching.
        In most cases it is important to additionally add relative fields to get a unique match.
        (e.g. countr_id, state_id, etc.)
        """
        return ADDRESS_MATCH_SIMPLE_FIELDS

    def get_address_match_complex_fields(self) -> List:
        """
        Returns list of complex fields that are used for address matching.
        """
        return ADDRESS_MATCH_COMPLEX_FIELDS

    def get_address_match_fields(self) -> List:
        """
        Returns list of fields that are used for address matching.
        """
        return ADDRESS_MATCH_SIMPLE_FIELDS + ADDRESS_MATCH_COMPLEX_FIELDS

    def create_proxy(self, type_: str, factory_id: int, data: dict) -> models.Model:
        """
        Create a proxy instance with cleaned values based on the provided data.
        Args:
            type_: The type of the proxy.
            factory_id: The ID of the factory associated with the proxy.
            data : The input data dictionary.
        Returns:
            Recordset: The created proxy instance.
        """
        data = self._prepare_data(type_, data)

        # If no person name and email is provided, return an empty proxy
        if not data.get('person_name') and not data.get('email'):
            return self.env['integration.res.partner.proxy']

        data['factory_id'] = factory_id

        return self.create([data])

    def _prepare_data(self, type_: str, data: Dict) -> Dict:
        """
        Prepare data for creating an instance of the proxy class with cleaned values.
        Args:
            type_: The type of the proxy.
            data: The input data dictionary.
        Returns:
            dict: A dictionary containing cleaned values for creating an instance of the proxy class
        """
        if type_ not in PROXY_TYPES:
            raise ValueError(
                f'Technical error: Invalid data type. Expected a dictionary, but got "{type(data).__name__}".\n'
                'This issue may be caused by improper use of the method. Please ensure the input data '
                'is correctly formatted.'
            )

        if not isinstance(data, dict):
            raise ValueError(f'Data should be a dictionary; "{data}" specified.')

        # Remove 'type' key as it's no longer needed and remove keys with empty values
        data.pop('type', None)
        data = {k: v for k, v in data.items() if v not in ['', None, [], {}]}
        if not data:
            return {}

        proxy_fields = self.get_proxy_fields()
        prepared_data = {
            'type': type_,
            **self._clear_optional_fields_values(data, proxy_fields),
        }

        if type_ == 'customer':
            prepared_data['external_id'] = data.get('id', '').strip()

        return prepared_data

    def _clear_optional_fields_values(self, data: Dict, field_names: List) -> Dict:
        """
        Retrieve optional string fields from data, stripping whitespace if present.
        Args:
            data: The input data dictionary.
            field_names: A list of field names to retrieve from the data dictionary.
        Returns:
            dict: A dictionary containing optional string fields with whitespace stripped.
        """
        cleaned_data = dict()

        for key, value in data.items():
            if key in field_names:
                if isinstance(value, str):
                    cleaned_data[key] = value.strip()
                else:
                    cleaned_data[key] = value

        return cleaned_data

    @api.model
    def get_customer(self, raise_error: bool = True) -> models.Model:
        """
        Get the mapped customer.

        This method retrieves the customer partner that has been mapped
        by the user.

        Returns:
            models.Model: The retrieved customer partner instance.
        """
        partner = self.env['res.partner'].from_external(
            self.integration_id, self.external_id, raise_error,
        )

        self.partner_id = partner

        return partner

    @api.model
    def get_or_create_partner(self) -> models.Model:
        """
        Get or create a partner.

        This method retrieves an existing partner based on the external ID,
        or creates a new partner if no matching partner is found.
        If a company name is provided, it also retrieves or creates the company
        associated with the partner. Additionally, it links the external partner
        if it exists, and checks for an existing mapping between the integration
        and the external ID, creating one if none is found.

        Returns:
            models.Model: The retrieved or created partner instance.
        """
        if self.company_name and self.integration_id.skip_individual_contacts:
            company = self._get_or_create_company()

            if self.external_id:
                self._create_or_update_mapping()
                company._link_external_partner(self.integration_id, self.external_id)

            self.partner_id = company

            return company

        partner_vals = self._prepare_partner_vals()
        domain = self._collect_partner_search_domain(partner_vals)

        partner = self.env['res.partner'].search(domain)
        if len(partner) > 1:
            partner = min(partner, key=lambda p: p.create_date)

        if partner:
            # Update it with address fields if they are empty
            self._write_address_fields_if_empty(partner)
        else:
            partner = self._create_partner(partner_vals)

        # Get and set customer's pricelist from external system (if this feature is enabled)
        if self.integration_id.pricelist_integration and self.pricelist_id:
            pricelist = self.env['product.pricelist'].from_external(
                self.integration_id,
                self.pricelist_id,
                raise_error=False,
            )
            if pricelist:
                partner = partner.with_company(self.integration_id.company_id)
                partner.property_product_pricelist = pricelist.id

        self.partner_id = partner

        if self.external_id:
            self._create_or_update_mapping()
            partner._link_external_partner(self.integration_id, self.external_id)

        return partner

    def _prepare_partner_vals(self) -> Dict:
        """
        Prepare partner values based on the provided data.
        Returns:
            A dictionary containing prepared partner values.
        """
        partner_vals = {
            'parent_id': False,
            'is_company': False,
            'type': 'contact',
        }

        if self.person_name:
            partner_vals['name'] = ' '.join(self.person_name.split())

        if self.email:
            partner_vals['email'] = self.email

        if self.phone:
            partner_vals['phone'] = self.phone

        if self.mobile:
            partner_vals['mobile'] = self.mobile

        # Link this address to the company by setting its parent ID.
        # This step is important for maintaining data integrity and reducing duplicates,
        # as it ensures that the created address is associated with the correct company.
        if self.company_name:
            company = self._get_or_create_company()
            partner_vals['parent_id'] = company.id

        # Since billing_address is written to partner,
        # it is necessary to fill in all fields that are used for the address.
        address_match_fields = self.get_address_match_simple_fields()
        for key in address_match_fields:
            if hasattr(self, key):
                partner_vals[key] = getattr(self, key)

        # Additionally add relative address fields to get a unique match.
        country = self._find_odoo_country()
        if country:
            partner_vals['country_id'] = country.id

        state = self._find_odoo_state(country)
        if state:
            partner_vals['state_id'] = state.id

        # Set customer language if available
        if self.language:
            language = self.env['res.lang'].from_external(self.integration_id, self.language)

            if language:
                partner_vals['lang'] = language.code

        # Handle `Person ID`
        person_id_field = self.integration_id.customer_personal_id_field
        if person_id_field:
            partner_vals[person_id_field.name] = self.person_id_number

        return partner_vals

    def _prepare_company_vals(self) -> Dict:
        """
        Prepare company values for creating a new company partner.
        Returns:
            dict: A dictionary containing the prepared company values.
        """
        company_vals = {
            'name': self.company_name,
            'parent_id': False,
            'is_company': True,
        }

        # Add VAT field value if available
        company_vals.update(self._get_vat())

        return company_vals

    @api.model
    def _get_or_create_company(self) -> models.Model:
        """
        Get or create an Odoo company based on company values.
        If company exists, fills address fields only if all necessary fields are available.
        Returns:
            models.Model: The retrieved or created company partner record.
        """
        if self.company_partner_id:
            return self.company_partner_id

        ResPartner = self.env['res.partner']

        company_vals = self._prepare_company_vals()

        domain = self._collect_company_search_domain(company_vals)
        company = ResPartner.search(domain, limit=1)

        if not company:
            # If company does not exist, create a new one
            tag = self._get_integration_tag()
            company_vals['category_id'] = [(6, 0, tag.ids)]

            # The context key 'no_vat_validation' allows you to store/set a VAT number without
            # doing validations.
            ctx = dict(self.env.context)
            if self.integration_id.ignore_vat_validation:
                ctx.update({'no_vat_validation': True})

            company = ResPartner.with_context(ctx).create(company_vals)

        # Check if address fields are empty and if so, write the address fields to the company
        self._write_address_fields_if_empty(company)

        self.company_partner_id = company

        return company

    def _collect_partner_search_domain(self, partner_vals: Dict) -> List[Tuple[str, str, str]]:
        """
        Collects the search domain based on partner values.
        Args:
            partner_vals : A dictionary containing partner values.
        Returns:
            list: A list of tuples representing the search domain criteria.
        """

        def _get_operator(field: str) -> str:
            return '=ilike' if field in ['name', 'email'] else '='

        search_criteria = PARTNER_SEARCH_CRITERIA.copy()

        customer_field_names = self.integration_id.sudo().search_customer_fields_ids.mapped('name')
        for field_name in customer_field_names:
            if partner_vals.get(field_name):
                search_criteria.append((field_name, _get_operator(field_name),))

        # If the user has selected to search partners by specific fields, but there are no values
        # in partner_vals for those fields, the search will be performed using all possible fields.
        if len(PARTNER_SEARCH_CRITERIA) == len(search_criteria) and SEARCH_CUSTOMER_FIELDS != customer_field_names:
            for field_name in SEARCH_CUSTOMER_FIELDS:
                if partner_vals.get(field_name):
                    search_criteria.append((field_name, _get_operator(field_name),))

        domain = self._build_search_domain(search_criteria, partner_vals)

        # Add personal ID field to the domain if specified
        person_id_field = self.integration_id.customer_personal_id_field
        if person_id_field and self.person_id_number:
            domain.append((person_id_field.name, '=', self.person_id_number))

        return domain

    def _collect_company_search_domain(self, company_vals: Dict) -> List[Tuple[str, str, Any]]:
        """
        Collect the search domain for finding companies based on the provided company values.
        Args:
            company_vals: Dictionary of company values.
        Returns:
            The search domain criteria.
        """
        search_criteria = COMPANY_SEARCH_CRITERIA.copy()

        # Check if there is a company VAT field defined in the integration settings
        company_vat_field = self.integration_id.customer_company_vat_field
        if company_vat_field and company_vals.get(company_vat_field.name):
            if self.integration_id.use_vat_only_company_search:
                # If configured to use VAT only for company search, update search criteria
                # accordingly
                search_criteria = [(company_vat_field.name, '='), ('is_company', '=')]
                # After this line, no new search criteria should be added to 'search_criteria'.
                return self._build_search_domain(search_criteria, company_vals)
            else:
                search_criteria.append((company_vat_field.name, '='))

        return self._build_search_domain(search_criteria, company_vals)

    @api.model
    def _create_partner(self, partner_vals: Dict) -> models.Model:
        """
        Create an Odoo partner based on the provided partner values.

        This method adds a tag with the integration name for the new partner.
        It creates the partner record with the provided values.
        It also creates a mapping between the integration and the external partner.
        """
        # Add tag with integration Name for new partner
        tag = self._get_integration_tag()
        partner_vals['category_id'] = [(6, 0, tag.ids)]

        ctx = {'res_partner_search_mode': 'customer'}
        partner = self.env['res.partner'].with_context(**ctx).create(partner_vals)

        return partner

    def _has_address_changes(self, partner: models.Model, new_address_vals: Dict) -> bool:
        """
        Compare existing partner's address fields with new address values to determine
        if a new address record is needed.

        This method checks if there are any differences between the existing partner's address fields
        and the new address values that would warrant creating a new address record. It handles both
        relational fields (like country_id, state_id) and text fields with special comparison rules.
        Certain fields are ignored during comparison as they don't affect the address uniqueness.

        Args:
            partner: The existing partner record to compare against
            new_address_vals: Dictionary containing new address values to compare with

        Returns:
            bool: True if there are significant differences that require a new address record,
                 False if the existing address can be reused
        """
        for field, new_value in new_address_vals.items():
            # Skip fields that don't affect address uniqueness
            if field in self.ADDRESS_UNIQUENESS_IGNORED_FIELDS:
                continue

            # Handle relational fields (Many2one, etc.)
            if isinstance(partner[field], models.Model):
                if partner[field].id != new_value:
                    return True
                continue

            # Skip if both values are empty
            if not bool(partner[field]) and not bool(new_value):
                continue

            # Convert values to strings for comparison
            partner_value = str(partner[field]) if partner[field] else ''
            new_value = str(new_value) if new_value else ''

            # Fields that require case-insensitive comparison
            case_insensitive_fields = ['name', 'street', 'street2', 'city', 'zip', 'email']
            if field in case_insensitive_fields:
                if partner_value.strip().lower() != new_value.strip().lower():
                    return True
            else:
                if partner_value.strip() != new_value.strip():
                    return True

        return False

    def _write_address_fields_if_empty(self, partner: models.Model) -> None:
        """
        Write address fields to a contact if they are empty.
        """
        if all(not partner[field] for field in self.get_address_match_fields()):
            company_address_vals = {}
            address_match_fields = self.get_address_match_simple_fields()
            for key in address_match_fields:
                if hasattr(self, key):
                    company_address_vals[key] = getattr(self, key)

            # Add relative address fields to get a unique match
            country = self._find_odoo_country()
            if country:
                company_address_vals['country_id'] = country.id

            state = self._find_odoo_state(country)
            if state:
                company_address_vals['state_id'] = state.id

            partner.write(company_address_vals)

    @api.model
    def _get_or_create_address(self) -> models.Model:
        """
        Get or create an address based on the prepared address values.
        Returns:
            models.Model: The created or existing address partner record.
        """
        ResPartner = self.env['res.partner']
        partner = self.factory_id.customer_id

        address_vals = self._prepare_address_vals()

        # Remove keys from address_vals as it is not needed for the validation.
        vals = address_vals.copy()
        vals.pop('type', None)
        vals.pop('parent_id', None)

        # If address_vals is written on the partner, return the partner
        if (
            # If the option to skip individual contacts is enabled, we should use the
            # company as a contact.
            not self.integration_id.skip_individual_contacts
            or not self.company_name
        ):
            if not self._has_address_changes(partner, address_vals):
                return partner
        elif self.company_name:
            company = self._get_or_create_company()

            # In most cases it makes no sense to do this check because name in company contact
            # and name in address are not the same (address will have person name)
            if not self._has_address_changes(company, address_vals):
                return company

        domain = self._collect_address_search_domain(address_vals)
        address = ResPartner.search(domain)

        if not address:
            tag = self._get_integration_tag()
            address_vals['category_id'] = [(6, 0, tag.ids)]

            address = ResPartner.create(address_vals)

        # If 'type' is provided in address_vals, filter the results
        elif 'type' in address_vals:
            address = address.filtered(lambda x: x.type == address_vals['type']) or address

        return address[0] if address else ResPartner

    def _collect_address_search_domain(self, address_vals: Dict) -> List[Tuple]:
        """
        Build a search domain for finding addresses based on the provided address values.
        """
        search_criteria = ADDRESS_SEARCH_CRITERIA.copy()

        for field in ['email', 'phone']:
            if address_vals.get(field):
                search_criteria.append((field, '=ilike'))

        search_criteria.extend([
            ('street', '=ilike'),
            ('street2', '=ilike'),
            ('city', '=ilike'),
            ('zip', '=ilike'),
            ('state_id', '='),
            ('country_id', '='),
            ('external_company_name', '=ilike'),
        ])

        domain = self._build_search_domain(search_criteria, address_vals)

        domain.append(('type', 'in', ['other', 'invoice', 'delivery']))

        return domain

    def _prepare_address_vals(self) -> Dict:
        """
        Prepare address values.
        This method constructs a dictionary containing the values required to create or update
        an address record in Odoo. It gathers basic address information such as the name, type,
        parent company, country, state, and additional address fields specified by the integration
        settings. It also handles company-specific fields such as the external company name and VAT.
        Returns:
            A dictionary containing the prepared address values.
        """
        address_vals = {
            'parent_id': self.factory_id.customer_id.id,
        }

        # Set the address type based on the proxy type.
        if self.type == 'billing_address':
            address_vals['type'] = 'invoice'
        elif self.type == 'shipping_address':
            address_vals['type'] = 'delivery'
        else:
            address_vals['type'] = 'other'

        # Remove extra spaces from name
        if self.person_name:
            address_vals['name'] = ' '.join(self.person_name.split())

        # Set the company as the parent for the address by linking its ID.
        # This step is important for maintaining data integrity and reducing duplicates,
        # as it ensures that the created address is associated with the correct company.

        # If manual customer mapping is enabled and a company is present on the address, we
        # skip processing the company. This is because, with manual mapping, we retrieve the
        # partner from the mapping and do not add a company to it.
        if self.company_name and not self.integration_id.use_manual_customer_mapping:
            company = self._get_or_create_company()
            address_vals['parent_id'] = company.id

        address_match_fields = self.get_address_match_simple_fields()
        for key in address_match_fields:
            if hasattr(self, key):
                address_vals[key] = getattr(self, key)

        # Add relative address fields to get a unique match.
        country = self._find_odoo_country()
        if country:
            address_vals['country_id'] = country.id

        state = self._find_odoo_state(country)
        if state:
            address_vals['state_id'] = state.id

        # Set customer language if available
        if self.language:
            language = self.env['res.lang'].from_external(self.integration_id, self.language)
            if language:
                address_vals['lang'] = language.code

        # Adding Company Specific fields
        if self.company_name:
            address_vals['external_company_name'] = self.company_name

        return address_vals

    @api.model
    def _find_odoo_country(self) -> models.Model:
        """
        Find the corresponding Odoo country based on the provided data.
        """
        country = self.env['res.country']

        if self.country:
            country = country.from_external(self.integration_id, self.country)
        elif self.country_code:
            country = self.env['res.country'].search([
                ('code', '=ilike', self.country_code),
            ], limit=1)

        return country

    def _find_odoo_state(self, odoo_country: models.Model) -> models.Model:
        """
        Find the corresponding Odoo state based on the provided country.
        """
        state = self.env['res.country.state']

        if not state.search([('country_id', '=', odoo_country.id)]):
            return state

        if self.state:
            state = state.from_external(self.integration_id, self.state)
        elif self.state_code and odoo_country:
            state = state.search([
                ('country_id', '=', odoo_country.id),
                ('code', '=ilike', self.state_code),
            ], limit=1)

        return state

    def _get_integration_tag(self) -> models.Model:
        """
        Retrieve or create an integration tag for the current integration.
        """
        ResPartnerTag = self.env['res.partner.category']
        main_tag = self.env.ref('integration.main_integration_tag', False) or ResPartnerTag

        tag = ResPartnerTag.search([
            ('name', '=', self.integration_id.name),
            ('parent_id', '=', main_tag.id),
        ])

        if not tag:
            tag = ResPartnerTag.sudo().create({
                'name': self.integration_id.name,
                'parent_id': main_tag.id,
            })

        return tag

    def _build_search_domain(self, search_criteria: List, values: Dict) -> List:
        """
        Build a search domain based on the provided search criteria and values.
        """
        domain = []

        for key, op in search_criteria:
            value = values.get(key, '')

            if value:
                # Escape the value if the operator is 'ilike'
                if isinstance(value, str) and op == '=ilike':
                    value = escape_psql(value)
                domain.append((key, op, value))
            else:
                # If there is no value, use the 'in' operator and an empty list for filtering
                domain.append((key, 'in', ['', False]))

        return domain

    @api.model
    def _create_or_update_mapping(self, with_new_cursor=False) -> models.Model:
        """
        Creates or updates a mapping for the integration and external partner.
        If a mapping exists, updates the partner_id. Otherwise, creates a new one.
        Args:
            with_new_cursor: Whether to create the mapping with a new cursor.
        Returns:
            models.Model: The created or updated mapping.
        """
        if not self.external_id:
            return self.env['integration.res.partner.mapping']

        ResPartner = self.env['res.partner']
        external_mapping = ResPartner.get_mapping(self.integration_id, self.external_id)

        if external_mapping:
            external_mapping.partner_id = self.partner_id
        else:
            # Use Odoo's transaction mechanism for isolation
            if with_new_cursor:
                db_registry = registry(self.env.cr.dbname)
                with db_registry.cursor() as new_cr:
                    new_env = api.Environment(new_cr, self.env.uid, {})
                    integration_id = new_env['sale.integration'].browse(self.integration_id.id)
                    external_mapping = new_env['res.partner'].create_mapping(
                        integration_id,
                        self.external_id,
                        extra_vals={'name': self.person_name},
                    )
            else:
                external_mapping = self.partner_id.create_mapping(
                    self.integration_id,
                    self.external_id,
                    extra_vals={'name': self.person_name},
                )

        return external_mapping

    def _post_update_partner(self, partner: models.Model):
        return partner

    def _get_vat(self) -> Dict:
        """
        Prepare VAT value.
        """
        vals = {}

        company_vat_field = self.integration_id.customer_company_vat_field
        company_reg_number = self.company_reg_number
        country = self._find_odoo_country()

        if company_vat_field and company_reg_number:
            is_valid_vat, error_msg = self._validate_vat(company_reg_number, country)

            partner = self.factory_id.customer_id
            if is_valid_vat:
                vals[company_vat_field.name] = company_reg_number

            # Log validation failure message if applicable
            elif error_msg and partner:
                message = f'VAT validation failed for "{company_reg_number}". Error: {error_msg}.'
                self._log_message(partner, 'Issue with VAT number', message)

        return vals

    def _log_message(self, partner, subject, body):
        """
        Log a message for the given partner.
        """
        partner._message_log(
            body=body,
            subject=subject,
            author_id=self.env.user.partner_id.id,
            message_type='comment',
        )

    def _validate_vat(self, company_reg_number: str, country: str) -> tuple:
        """
        Validate VAT number based on the integration settings.
        """
        if self.integration_id.ignore_vat_validation:
            return True, None

        # If no country is found, add the VAT number to the partner without validation
        if not country:
            return True, 'VAT cannot be validated as no country is specified for the address.'

        return self.env['res.partner']._validate_integration_vat(company_reg_number, country)
