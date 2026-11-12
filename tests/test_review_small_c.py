"""Validation and durable sends have one visible feedback message."""
from html import unescape
import re

import pytest

from tests.test_ux_recovery import ux, login, permitted_contact, review_fields


@pytest.mark.parametrize('phone, expected', [('', 'Phone number is required.'),
    ('invalid fixture', 'Enter a valid phone number.'),
    ('+17205550820', 'A contact with this phone number already exists.')])
@pytest.mark.parametrize('editing', [False, True])
def test_invalid_or_duplicate_contact_has_one_inline_error(ux, phone, expected, editing):
    permitted_contact(ux)
    member = ux.models.CommunityMember(organization_id=ux.org_id, name='Other contact', phone='+17205550829')
    ux.db.session.add(member)
    ux.db.session.commit()
    member_id = member.id
    login(ux)
    path = f'/community/{member_id}/edit' if editing else '/community/add'
    response = ux.client.post(path, data={'name': 'Retained draft', 'phone': phone})
    assert response.status_code == 200
    html = unescape(response.get_data(as_text=True))
    assert html.count(expected) == 1
    assert len(re.findall(r'role="alert"', html)) == 1
    assert 'value="Retained draft"' in html and 'id="phone-error"' in html
    assert ux.models.CommunityMember.query.count() == 2


def test_durable_send_and_idempotent_retry_show_status_once(ux, monkeypatch):
    from app.services.outbound_request_service import request_status_message
    permitted_contact(ux)
    login(ux)
    monkeypatch.setattr('app.routes.enqueue_outbound_request', lambda _request: False)
    fields = review_fields(request_idempotency_key='single-confirmation-fixture')
    review = ux.client.post('/dashboard/review', data=fields)
    assert review.status_code == 200
    form = {**fields, 'review_token': review.json['review_token']}
    for _ in range(2):
        response = ux.client.post('/dashboard', data=form, follow_redirects=True)
        assert response.status_code == 200
        saved = ux.models.OutboundRequest.query.one()
        html = unescape(response.get_data(as_text=True))
        assert html.count(request_status_message(saved)) == 1
        assert 'Estimated billing:' not in html
        assert ux.models.MessageLog.query.count() == 1
