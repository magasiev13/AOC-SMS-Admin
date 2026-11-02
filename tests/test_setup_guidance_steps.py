"""Setup errors return owners to the relevant step without saving invalid drafts."""
from html.parser import HTMLParser
from unittest.mock import MagicMock

import pytest

from tests.test_ux_recovery import ux, login


class SetupDocument(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.form = {}
        self.fields = {}
        self.inputs = []
        self.links = []
        self.textarea = None
        self.select = None
        self.options = []
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'form' and 'data-organization-setup' in attrs:
            self.form = attrs
        if tag == 'input' and attrs.get('name'):
            self.fields[attrs['name']] = attrs
            self.inputs.append(attrs)
        if tag == 'textarea' and attrs.get('name'):
            self.textarea = attrs['name']
            self.fields[self.textarea] = {**attrs, 'value': ''}
        if tag == 'select' and attrs.get('name'):
            self.select = attrs['name']
            self.options = []
            self.fields[self.select] = attrs
        if tag == 'option' and self.select:
            self.options.append(attrs)
        if tag == 'a' and 'data-organization-error-link' in attrs:
            self.links.append(attrs.get('href'))

    def handle_data(self, data):
        if self.textarea:
            self.fields[self.textarea]['value'] += data

    def handle_endtag(self, tag):
        if tag == 'textarea':
            self.textarea = None
        if tag == 'select' and self.select:
            selected = next((option for option in self.options if 'selected' in option),
                            self.options[0] if self.options else {})
            self.fields[self.select]['value'] = selected.get('value', '')
            self.select = None
            self.options = []


def valid_details(**overrides):
    payload = {
        'action': 'save_compliance',
        'registration_path': 'low_volume_standard',
        'number_strategy': 'auto_buy',
        'business_type': 'Limited Liability Corporation',
        'legal_business_name': 'Retained Organization Draft',
        'public_brand_name': 'Retained Organization Draft',
        'business_industry': 'TECHNOLOGY',
        'has_business_tax_id': 'on',
        'business_regions': ['USA_AND_CANADA'],
        'business_registration_identifier': 'EIN',
        'business_registration_number': '98-7654321',
        'submission_source_mode': 'hosted_fallback',
        'email': 'draft@setup.test',
        'notification_email': 'alerts@setup.test',
        'phone_number': '+17205550851',
        'mobile_number': '+17205550852',
        'first_name': 'Test',
        'last_name': 'Owner',
        'business_title': 'Owner',
        'job_position': 'Director',
        'address_country': 'US',
        'address_line1': '123 Setup Way',
        'address_city': 'Denver',
        'address_region': 'CO',
        'address_postal_code': '80202',
        'campaign_use_case': 'ACCOUNT_NOTIFICATION',
        'campaign_description': 'Requested order updates and reminders',
        'message_flow': 'Customers opt in on our signup page for requested updates and reply STOP to unsubscribe.',
        'message_samples': 'Your requested order is ready. Reply STOP to unsubscribe.\nYour requested appointment reminder. Reply STOP to unsubscribe.',
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize('requested_step', ['', '?step=launch'])
@pytest.mark.parametrize('field,value,error', [
    ('business_registration_number', '123', 'Enter a valid EIN using 9 digits.'),
    ('message_flow', 'Customers opt in on the signup page for requested order updates.',
     'Message flow must mention STOP or unsubscribe instructions.'),
])
def test_invalid_setup_post_returns_relevant_field_and_preserves_unsaved_values(
        ux, monkeypatch, requested_step, field, value, error):
    from app.services.twilio_a2p_service import ensure_a2p_onboarding

    org = ux.db.session.get(ux.models.Organization, ux.org_id)
    onboarding = ensure_a2p_onboarding(org)
    ux.db.session.commit()
    before = (onboarding.business_name, onboarding.email, onboarding.onboarding_status,
              onboarding.business_registration_number_encrypted, onboarding.submitted_at)
    audit_count = ux.models.OrganizationProviderAuditLog.query.count()
    queue = MagicMock()
    monkeypatch.setattr('app.services.twilio_a2p_service.get_queue', queue)
    login(ux)

    response = ux.client.post('/setup' + requested_step, data=valid_details(**{field: value}))

    assert response.status_code == 200
    parsed = SetupDocument(response)
    assert b'data-current-step="compliance"' in response.data
    assert parsed.form['data-initial-error-field'] == field
    assert '#' + field in parsed.links
    assert error.encode() in response.data
    assert parsed.fields['legal_business_name']['value'] == 'Retained Organization Draft'
    assert parsed.fields['email']['value'] == 'draft@setup.test'
    assert b'Retained Organization Draft' in response.data
    ux.db.session.refresh(onboarding)
    assert (onboarding.business_name, onboarding.email, onboarding.onboarding_status,
            onboarding.business_registration_number_encrypted, onboarding.submitted_at) == before
    assert ux.models.OrganizationProviderAuditLog.query.count() == audit_count
    assert ux.models.OrganizationA2POnboarding.query.count() == 1
    queue.assert_not_called()


def test_staff_cannot_use_setup_step_post_to_save_an_owner_draft(ux, monkeypatch):
    from app.services.twilio_a2p_service import ensure_a2p_onboarding

    org = ux.db.session.get(ux.models.Organization, ux.org_id)
    onboarding = ensure_a2p_onboarding(org)
    ux.db.session.commit()
    before = onboarding.business_name
    queue = MagicMock()
    monkeypatch.setattr('app.services.twilio_a2p_service.get_queue', queue)
    login(ux, 'staff')

    response = ux.client.post('/setup?step=compliance', data=valid_details(
        organization_id=str(ux.other_id), legal_business_name='Unauthorized Draft'))

    assert response.status_code == 302 and response.location.endswith('/setup/pending')
    ux.db.session.refresh(onboarding)
    assert onboarding.business_name == before
    assert ux.models.OrganizationA2POnboarding.query.count() == 1
    queue.assert_not_called()


def test_unchecking_saved_public_website_uses_hosted_pages_and_saves_other_details(ux, monkeypatch):
    from app.services.twilio_a2p_service import ensure_a2p_onboarding

    ux.app.config['TWILIO_CREDENTIAL_ENCRYPTION_KEY'] = '4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o='
    ux.app.config['APP_BASE_URL'] = 'https://setup.twinevia.test'
    org = ux.db.session.get(ux.models.Organization, ux.org_id)
    onboarding = ensure_a2p_onboarding(org)
    onboarding.has_public_website = True
    onboarding.has_business_tax_id = True
    onboarding.submission_source_mode = 'external_site'
    onboarding.external_website_url = 'https://old.setup.test'
    onboarding.external_privacy_policy_url = 'https://old.setup.test/privacy'
    onboarding.external_terms_and_conditions_url = 'https://old.setup.test/terms'
    onboarding.external_cta_proof_url = 'https://old.setup.test/signup'
    ux.db.session.commit()
    external_validation = MagicMock(side_effect=AssertionError('Unchecked external website must not be requested.'))
    monkeypatch.setattr('app.services.twilio_a2p_service._validate_external_submission_urls', external_validation)
    queue = MagicMock()
    monkeypatch.setattr('app.services.twilio_a2p_service.get_queue', queue)
    login(ux)

    # An unchecked HTML checkbox is omitted from the POST, while the old
    # onboarding record still has has_public_website=True.
    payload = valid_details()
    assert 'has_public_website' not in payload
    response = ux.client.post('/setup?step=compliance', data=payload)

    assert response.status_code == 302, SetupDocument(response).form.get('data-initial-error-field')
    assert response.location.endswith('/setup?step=review')
    ux.db.session.refresh(onboarding)
    assert onboarding.has_public_website is False
    assert onboarding.submission_source_mode == 'hosted_fallback'
    assert onboarding.website_url == 'https://setup.twinevia.test/compliance/ux-fixture/sms/opt-in'
    assert onboarding.business_name == payload['legal_business_name']
    assert onboarding.email == payload['email']
    assert onboarding.notification_email == payload['notification_email']
    external_validation.assert_not_called()
    queue.assert_not_called()


def test_unchecking_saved_tax_id_does_not_inherit_old_permission_or_save_other_fields(ux, monkeypatch):
    from app.services.twilio_a2p_service import ensure_a2p_onboarding

    org = ux.db.session.get(ux.models.Organization, ux.org_id)
    onboarding = ensure_a2p_onboarding(org)
    onboarding.has_business_tax_id = True
    ux.db.session.commit()
    before = (onboarding.business_name, onboarding.email, onboarding.has_business_tax_id)
    queue = MagicMock()
    monkeypatch.setattr('app.services.twilio_a2p_service.get_queue', queue)
    login(ux)
    payload = valid_details()
    payload.pop('has_business_tax_id')

    response = ux.client.post('/setup?step=compliance', data=payload)

    assert response.status_code == 200
    parsed = SetupDocument(response)
    assert parsed.form['data-initial-error-field'] == 'has_business_tax_id'
    assert '#has_business_tax_id' in parsed.links
    assert 'checked' not in parsed.fields['has_business_tax_id']
    assert b'Non-sole-proprietor onboarding requires a business EIN or tax ID.' in response.data
    ux.db.session.refresh(onboarding)
    assert (onboarding.business_name, onboarding.email, onboarding.has_business_tax_id) == before
    queue.assert_not_called()


def test_partial_onboarding_renders_missing_text_values_blank_in_all_parts(ux):
    from app.services.twilio_a2p_service import ensure_a2p_onboarding

    org = ux.db.session.get(ux.models.Organization, ux.org_id)
    onboarding = ensure_a2p_onboarding(org)
    nullable_fields = [
        'email', 'notification_email', 'phone_number', 'mobile_number',
        'address_line1', 'address_line2', 'address_city', 'address_region', 'address_postal_code',
        'first_name', 'last_name', 'business_title', 'social_profile_url',
        'external_website_url', 'external_privacy_policy_url',
        'external_terms_and_conditions_url', 'external_cta_proof_url',
        'message_flow', 'campaign_description', 'opt_in_message', 'opt_out_message', 'help_message',
    ]
    for field in nullable_fields + ['business_type', 'job_position', 'address_country']:
        setattr(onboarding, field, None)
    onboarding.has_public_website = True
    onboarding.submission_source_mode = 'external_site'
    ux.db.session.commit()
    login(ux)

    response = ux.client.get('/setup?step=compliance')

    assert response.status_code == 200
    parsed = SetupDocument(response)
    for field in nullable_fields + ['business_type', 'job_position', 'message_samples']:
        assert parsed.fields[field]['value'] == '', field
    assert parsed.fields['address_country']['value'] == 'US'
    assert parsed.fields['legal_business_name']['value'] == org.name
    assert parsed.fields['public_brand_name']['value'] == org.name
    assert 'checked' in parsed.fields['has_public_website']


def test_failed_post_retains_explicit_blank_and_literal_none_values(ux, monkeypatch):
    from app.services.twilio_a2p_service import ensure_a2p_onboarding

    org = ux.db.session.get(ux.models.Organization, ux.org_id)
    onboarding = ensure_a2p_onboarding(org)
    onboarding.address_line2 = 'Saved Suite'
    onboarding.email = 'saved@setup.test'
    onboarding.opt_out_message = 'Saved reply'
    ux.db.session.commit()
    queue = MagicMock()
    monkeypatch.setattr('app.services.twilio_a2p_service.get_queue', queue)
    login(ux)
    response = ux.client.post('/setup?step=compliance', data=valid_details(
        business_registration_number='123', address_line1='None', address_line2='',
        email='', first_name='None', address_country='CA', message_flow=' None ', opt_out_message='None'))

    assert response.status_code == 200
    parsed = SetupDocument(response)
    for field, expected in {
        'address_line1': 'None', 'address_line2': '', 'email': '', 'first_name': 'None',
        'address_country': 'CA', 'message_flow': ' None ', 'opt_out_message': 'None',
    }.items():
        assert parsed.fields[field]['value'] == expected, field
    ux.db.session.refresh(onboarding)
    assert (onboarding.address_line2, onboarding.email, onboarding.opt_out_message) == (
        'Saved Suite', 'saved@setup.test', 'Saved reply')
    queue.assert_not_called()


def test_rejected_post_does_not_recheck_unselected_business_regions(ux, monkeypatch):
    from app.services.twilio_a2p_service import ensure_a2p_onboarding

    org = ux.db.session.get(ux.models.Organization, ux.org_id)
    onboarding = ensure_a2p_onboarding(org)
    onboarding.business_regions_json = '["USA_AND_CANADA"]'
    ux.db.session.commit()
    queue = MagicMock()
    monkeypatch.setattr('app.services.twilio_a2p_service.get_queue', queue)
    login(ux)
    payload = valid_details()
    payload.pop('business_regions')
    response = ux.client.post('/setup?step=compliance', data=payload)

    assert response.status_code == 200
    parsed = SetupDocument(response)
    assert parsed.form['data-initial-error-field'] == 'business_regions'
    assert '#business_regions' in parsed.links
    regions = [field for field in parsed.inputs if field['name'] == 'business_regions']
    assert regions and all('checked' not in field for field in regions)
    ux.db.session.refresh(onboarding)
    assert onboarding.business_regions_json == '["USA_AND_CANADA"]'
    queue.assert_not_called()
