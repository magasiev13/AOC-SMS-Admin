import io
from importlib import import_module

from tests.test_ux_recovery import login, ux


def test_legacy_upgrade_preserves_only_preexisting_explicit_contracts(ux, monkeypatch):
    from app.services.recipient_permission_service import organization_has_confirmed_signup_consent
    migration = import_module('app.saas_migrations.035_consent_sources')
    monkeypatch.setenv('COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS', 'ux-fixture')
    with ux.db.engine.begin() as connection:
        migration.apply(connection, ux.app.logger)
        migration.apply(connection, ux.app.logger)
    assert ux.models.ConsentSource.query.count() == 1
    assert organization_has_confirmed_signup_consent(ux.org_id)
    assert not organization_has_confirmed_signup_consent(ux.other_id)
    monkeypatch.setenv('COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS', 'ux-other')
    assert not organization_has_confirmed_signup_consent(ux.other_id)


def submit_source(ux, **extra):
    data = dict(name='Newsletter signup form v1', kind='import', purpose='community',
                disclosure='Text me updates from my community.', attest='yes')
    data.update(extra)
    return ux.client.post('/community/consent-sources', data=data)


def review(ux, source, action='approve', **extra):
    return ux.client.post(f'/platform/organizations/{ux.org_id}/consent-sources',
                          data=dict(source_id=source.id, action=action, **extra))


def upload(ux, source_id, rows):
    data = {'file': (io.BytesIO(('name,phone,reference,consented_at\n' + rows).encode()), 'records.csv'),
            'source_id': str(source_id)}
    return ux.client.post('/community/import', data=data)


def test_approval_scope_traceability_replay_and_chronology(ux):
    from app.services.recipient_permission_service import recipient_permission_status, record_permission_evidence
    login(ux, 'staff')
    assert submit_source(ux).status_code == 403
    login(ux)
    assert submit_source(ux).status_code == 302
    source = ux.models.ConsentSource.query.one()
    row = 'First,+17205550116,form-A-35,2026-01-03T12:30:00Z\n'
    assert upload(ux, source.id, row).status_code == 404
    assert review(ux, source).status_code == 403
    login(ux, 'platform')
    assert ux.client.post(f'/platform/organizations/{ux.other_id}/consent-sources',
                          data=dict(source_id=source.id, action='approve')).status_code == 404
    assert review(ux, source).status_code == 302
    login(ux)
    assert b'Newsletter signup form v1' in ux.client.get('/community/import').data
    assert upload(ux, source.id, row).status_code == 302
    assert upload(ux, source.id, row).status_code == 302
    assert ux.models.RecipientPermissionEvidence.query.count() == 1
    assert recipient_permission_status(ux.org_id, '+17205550116').allowed
    record_permission_evidence(ux.org_id, '+17205550116', purpose='community', decision='denied',
                               source='manual', evidence_key='later-withdrawal')
    ux.db.session.commit()
    assert upload(ux, source.id, row).status_code == 302
    assert not recipient_permission_status(ux.org_id, '+17205550116').allowed
    assert upload(ux, source.id, row.replace('form-A-35', '')).status_code == 200
    assert upload(ux, source.id, row.replace('0116', '0117')).status_code == 200
    assert ux.models.CommunityMember.query.count() == 1
    login(ux, 'platform')
    assert review(ux, source, 'withdraw').status_code == 302
    login(ux)
    assert upload(ux, source.id, row).status_code == 404


def test_event_only_import_in_historical_workspace_keeps_its_scope(ux):
    from tests.consent_fixtures import historical_signup_contract
    from app.services.recipient_permission_service import recipient_permission_status

    historical_signup_contract(ux.org_id)
    existing = ux.models.CommunityMember(organization_id=ux.org_id, phone='+17205550110')
    event = ux.models.Event(organization_id=ux.org_id, title='Workshop')
    ux.db.session.add_all([existing, event])
    ux.db.session.commit()
    login(ux)
    assert submit_source(ux, purpose='event', event_id=event.id).status_code == 302
    source = ux.models.ConsentSource.query.filter_by(kind='import').one()
    login(ux, 'platform')
    assert review(ux, source).status_code == 302
    login(ux)
    phone = '+17205550111'
    assert upload(ux, source.id, f'New,{phone},event-form-1,2026-01-01T10:00:00Z\n').status_code == 302
    assert recipient_permission_status(ux.org_id, phone, purpose='event', event_id=event.id).allowed
    assert not recipient_permission_status(ux.org_id, phone).allowed
    assert recipient_permission_status(ux.org_id, existing.phone).allowed
    assert ux.models.EventRegistration.query.count() == 0
