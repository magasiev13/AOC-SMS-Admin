"""Empty current events point to matching retained history within the tenant."""
from datetime import timedelta

from tests.test_ux_recovery import ux, login


def _event(ux, title, *, other=False, past=True, status=None):
    event = ux.models.Event(organization_id=ux.other_id if other else ux.org_id, title=title,
        date=ux.models.utc_now().date() + timedelta(days=-7 if past else 7), external_status=status)
    ux.db.session.add(event)
    return event


def test_empty_current_events_show_tenant_past_count_and_links_in_both_layouts(ux):
    _event(ux, 'Retained past event')
    _event(ux, 'Retained cancelled event', past=False, status='cancelled')
    _event(ux, 'Private other-tenant past event', other=True)
    ux.db.session.commit()
    login(ux)
    response = ux.client.get('/events')
    assert response.status_code == 200
    assert response.data.count(b'Past or inactive events: 2.') == 2
    assert response.data.count(b'View Past &amp; Inactive Events') == 2
    assert b'Create an event to start collecting registrations.' not in response.data
    history = ux.client.get('/events?view=past')
    assert b'Retained past event' in history.data and b'Retained cancelled event' in history.data
    assert b'Private other-tenant past event' not in history.data


def test_past_hint_respects_literal_search_and_preserves_it_in_history_link(ux):
    _event(ux, 'Retained 100% event')
    _event(ux, 'Another retained past event')
    _event(ux, 'Other tenant 100% event', other=True)
    ux.db.session.commit()
    login(ux)
    response = ux.client.get('/events?search=%25')
    assert response.status_code == 200
    assert response.data.count(b'Past or inactive events matching this search: 1.') == 2
    assert b'No Current Events Match' in response.data
    assert b'view=past&amp;search=%25' in response.data
    history = ux.client.get('/events?view=past&search=%25')
    assert b'Retained 100% event' in history.data and b'Another retained past event' not in history.data
    assert b'Other tenant 100% event' not in history.data
    no_matches = ux.client.get('/events?search=no-match')
    assert b'No Matching Events' in no_matches.data
    assert b'Past or inactive events matching this search:' not in no_matches.data


def test_other_tenant_history_alone_does_not_replace_new_workspace_empty_state(ux):
    _event(ux, 'Hidden other tenant history', other=True)
    ux.db.session.commit()
    login(ux)
    response = ux.client.get('/events')
    assert response.status_code == 200
    assert b'Create an event to start collecting registrations.' in response.data
    assert b'Past or inactive events:' not in response.data
    assert b'Hidden other tenant history' not in response.data
