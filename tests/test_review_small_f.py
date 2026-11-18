"""Confirmed community consent hides redundant actions without granting event consent."""
from tests.consent_fixtures import historical_signup_contract
import pytest

from tests.test_ux_recovery import ux, login


@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('view', ['contactable', 'all'])
def test_community_permission_links_follow_confirmed_workspace_consent_only(ux, confirmed, view):
    historical_signup_contract(ux.org_id, confirmed)
    owner = ux.db.session.get(ux.models.AppUser, ux.users['owner'])
    ux.db.session.add(ux.models.CommunityMember(organization_id=ux.org_id, phone=owner.phone, name='Retained contact'))
    ux.db.session.commit()
    login(ux)
    response = ux.client.get('/community?view=' + view)
    assert response.status_code == 200
    if confirmed:
        assert b'Review Permission' not in response.data
        assert b'data-member-review=' not in response.data and b'id="memberPreviewReview"' not in response.data
        assert b'Sending still requires SMS permission' not in response.data
        assert b'Explicit opt-outs remain excluded' in response.data
    else:
        assert b'Review Permission' in response.data and b'data-member-review=' in response.data
        assert (b'Sending still requires SMS permission' in response.data) is (view == 'contactable')
    assert ux.models.RecipientPermissionEvidence.query.count() == 0


def test_confirmed_community_consent_keeps_event_permission_actions_and_existing_event_scope(ux):
    from app.services.recipient_permission_service import recipient_permission_status
    historical_signup_contract(ux.org_id, True)
    owner = ux.db.session.get(ux.models.AppUser, ux.users['owner'])
    event = ux.models.Event(organization_id=ux.org_id, title='Separate event consent')
    unrelated = ux.models.Event(organization_id=ux.org_id, title='Unrelated event audience')
    ux.db.session.add_all([event, unrelated])
    ux.db.session.flush()
    registration = ux.models.EventRegistration(organization_id=ux.org_id, event_id=event.id,
        phone=owner.phone, name='Retained event registrant')
    ux.db.session.add_all([registration, ux.models.CommunityMember(organization_id=ux.org_id,
        phone=owner.phone, name='Retained community contact')])
    ux.db.session.commit()
    assert recipient_permission_status(ux.org_id, owner.phone, purpose='community').allowed
    assert recipient_permission_status(ux.org_id, owner.phone, purpose='event', event_id=event.id).allowed
    assert not recipient_permission_status(ux.org_id, owner.phone, purpose='event', event_id=unrelated.id).allowed
    login(ux)
    response = ux.client.get(f'/events/{event.id}')
    assert response.status_code == 200 and b'Review Permission' in response.data
    assert f'/events/{event.id}/registrations/{registration.id}/permission'.encode() in response.data
