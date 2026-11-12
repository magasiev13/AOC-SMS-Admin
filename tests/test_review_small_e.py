"""Onboarding headlines acknowledge provider-accepted live messages."""
import pytest

from app.routes import _organization_onboarding_view
from tests.test_ux_recovery import ux, login


@pytest.mark.parametrize('test_mode,accepted,other_tenant,live', [
    (False, 1, False, True),
    (True, 1, False, False),
    (False, 0, False, False),
    (False, 1, True, False),
])
def test_only_this_workspaces_live_acceptance_replaces_testing_headline(ux, test_mode, accepted, other_tenant, live):
    organization = ux.db.session.get(ux.models.Organization, ux.org_id)
    before = _organization_onboarding_view(organization)
    assert before['headline'] == 'Ready for owner + staff testing'
    record = ux.models.MessageLog(organization_id=ux.other_id if other_tenant else ux.org_id,
        message_body='Retained acceptance fixture', target='community', status='failed', test_mode=test_mode,
        total_recipients=2, success_count=accepted, failure_count=2-accepted, details='[]')
    ux.db.session.add(record)
    ux.db.session.commit()
    after = _organization_onboarding_view(organization)
    assert after['headline'] == ('Live messaging in use' if live else before['headline'])
    assert after['completed_required'] == before['completed_required']
    assert after['required_total'] == before['required_total']
    assert (record.test_mode, record.success_count, record.status) == (test_mode, accepted, 'failed')
    login(ux)
    response = ux.client.get('/billing')
    assert response.status_code == 200
    if live:
        assert b'Live messaging in use' in response.data and b'Ready for owner + staff testing' not in response.data
