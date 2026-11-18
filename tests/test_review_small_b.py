"""Staff invitations cannot satisfy or identify the owner invitation step."""
from types import SimpleNamespace

from tests.test_ux_recovery import ux


def test_only_owner_invitations_can_be_primary(ux):
    from app.routes import _primary_organization_invitation
    staff = SimpleNamespace(id=30, role='staff')
    older_owner = SimpleNamespace(id=10, role='owner')
    owner = SimpleNamespace(id=20, role='owner')
    assert _primary_organization_invitation(SimpleNamespace(invitations=[staff])) is None
    assert _primary_organization_invitation(SimpleNamespace(invitations=[staff, older_owner, owner])) is owner
    assert _primary_organization_invitation(SimpleNamespace(invitations=[])) is None


def test_staff_invitation_does_not_complete_owner_invited_step(ux):
    from app.routes import _organization_onboarding_view
    from app.models import Organization, OrganizationInvitation
    organization = ux.db.session.get(Organization, ux.other_id)
    ux.db.session.add(OrganizationInvitation(organization_id=organization.id,
        email='staff-only@fixture.test', role='staff', token='fixture-staff-only', status='pending'))
    ux.db.session.commit()
    view = _organization_onboarding_view(organization)
    step = next(step for step in view['steps'] if step['label'] == 'Owner invited')
    assert step['complete'] is False
    assert 'staff-only@fixture.test' not in step['detail']
    assert view['owner_invitation'] is None
