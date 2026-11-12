"""Workspace date display is consistent and cannot alter stored scheduling."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re

from flask import Flask, render_template_string
import pytest

from app.datetime_display import format_date, format_datetime
from tests.test_event_feedback import feedback
from tests.test_ux_recovery import ux, login, permitted_contact, review_fields


@pytest.mark.parametrize('value, expected', [
    (datetime(2026, 10, 31, 14, 2), 'Sat, Oct 31, 2026 · 8:02 AM'),
    (datetime(2026, 10, 31, 14, 2, tzinfo=timezone.utc), 'Sat, Oct 31, 2026 · 8:02 AM'),
    (datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc), 'Sun, Nov 1, 2026 · 1:30 AM'),
    (datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc), 'Sun, Nov 1, 2026 · 1:30 AM'),
    (datetime(2026, 10, 31, 2, 5), 'Fri, Oct 30, 2026 · 8:05 PM'),
])
def test_datetime_converts_naive_utc_and_aware_values_with_dst(value, expected):
    app = Flask(__name__)
    app.config['APP_TIMEZONE'] = 'America/Denver'
    original = value
    with app.app_context():
        assert format_datetime(value) == expected
    assert value == original


def test_date_only_preserves_calendar_dates_and_converts_datetime_boundaries():
    app = Flask(__name__)
    app.config['APP_TIMEZONE'] = 'America/Denver'
    with app.app_context():
        assert format_date(date(2026, 10, 31)) == 'Oct 31, 2026'
        assert format_date(datetime(2026, 10, 31, 2, 5)) == 'Oct 30, 2026'
        assert format_date(None) == format_datetime(None) == '-'
        assert format_datetime(None, missing=None) is None


@pytest.mark.parametrize('zone', ['Invalid/Timezone', '', None])
def test_invalid_or_missing_timezone_falls_back_to_utc(zone):
    app = Flask(__name__)
    app.config['APP_TIMEZONE'] = zone
    with app.app_context():
        assert format_datetime(datetime(2026, 10, 31, 14, 2)) == 'Sat, Oct 31, 2026 · 2:02 PM'


def test_filters_and_route_helper_share_workspace_time_and_ignore_browser_cookie(ux):
    from app.routes import _format_datetime_display
    ux.app.config['APP_TIMEZONE'] = 'America/Denver'
    instant = datetime(2026, 10, 31, 14, 2, tzinfo=timezone.utc)
    with ux.app.test_request_context('/', headers={'Cookie': 'client_timezone=Asia%2FTokyo'}):
        assert render_template_string('{{ instant|localtime }}', instant=instant) == 'Sat, Oct 31, 2026 · 8:02 AM'
        assert render_template_string('{{ instant|localdate }}', instant=instant) == 'Oct 31, 2026'
        assert _format_datetime_display(instant) == format_datetime(instant)
        assert _format_datetime_display(None) is None


def test_contact_event_message_and_security_pages_render_shared_dates(ux):
    ux.app.config['APP_TIMEZONE'] = 'America/Denver'
    stamp = datetime(2026, 10, 31, 2, 5)
    member = ux.models.CommunityMember(organization_id=ux.org_id, name='Date display fixture',
        phone='+15550009201', created_at=stamp)
    event = ux.models.Event(organization_id=ux.org_id, title='Date-only display fixture',
        date=date(2026, 10, 31), created_at=stamp)
    log = ux.models.MessageLog(organization_id=ux.org_id, message_body='Date display historical result',
        target='community', status='sent', total_recipients=1, success_count=1,
        details=json.dumps([{'name': 'Date result fixture', 'success': True, 'sid': 'SMdatefixture'}]), created_at=stamp)
    audit = ux.models.AuthEvent(organization_id=ux.org_id, event_type='display_fixture',
        outcome='success', username='Date display fixture', created_at=stamp)
    ux.db.session.add_all([member, event, log, audit])
    ux.db.session.commit()
    login(ux)
    ux.client.set_cookie('client_timezone', 'Asia/Tokyo')
    contacts = ux.client.get('/community')
    assert b'Oct 30, 2026' in contacts.data
    events = ux.client.get('/events?view=past')
    assert b'Oct 31, 2026' in events.data and b'Oct 30, 2026' in events.data
    details = ux.client.get(f'/logs/{log.id}')
    assert 'Fri, Oct 30, 2026 · 8:05 PM' in details.get_data(as_text=True)
    security = ux.client.get('/security/events')
    assert 'Fri, Oct 30, 2026 · 8:05 PM' in security.get_data(as_text=True)
    for response in (contacts, events, details, security):
        assert response.status_code == 200
        assert b'UTC' not in response.data
    edit = ux.client.get(f'/events/{event.id}/edit')
    assert b'value="2026-10-31"' in edit.data
    ux.db.session.refresh(event)
    assert event.date == date(2026, 10, 31)
    ux.db.session.refresh(log)
    assert log.created_at == stamp


def test_customer_templates_have_no_custom_date_formats_or_utc_labels():
    root = Path(__file__).resolve().parents[1] / 'app/templates'
    for path in root.rglob('*.html'):
        if 'platform' in path.parts:
            continue
        template = path.read_text()
        assert not re.search(r'\|localtime\(', template), path
        if path != root / 'events/form.html':
            assert '.strftime(' not in template, path
        assert not re.search(r'(Time \(UTC\)|UTC timestamp|}} UTC)', template), path


def test_security_date_filters_follow_the_full_workspace_day_across_dst(ux):
    ux.app.config['APP_TIMEZONE'] = 'America/Denver'
    # The fall-back date spans 25 hours: 06:00 UTC through 07:00 UTC next day.
    for name, stamp in [
        ('Before workspace day', datetime(2026, 11, 1, 5, 59)),
        ('First instant of workspace day', datetime(2026, 11, 1, 6, 0)),
        ('Last instant of workspace day', datetime(2026, 11, 2, 6, 59)),
        ('After workspace day', datetime(2026, 11, 2, 7, 0)),
    ]:
        ux.db.session.add(ux.models.AuthEvent(organization_id=ux.org_id, event_type='display_boundary',
            outcome='success', username=name, created_at=stamp))
    ux.db.session.commit()
    login(ux)
    response = ux.client.get('/security/events?date_from=2026-11-01&date_to=2026-11-01&event_type=display_boundary')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'First instant of workspace day' in html and 'Last instant of workspace day' in html
    assert 'Before workspace day' not in html and 'After workspace day' not in html


def test_broadcast_review_displays_workspace_time_but_keeps_selected_send_instant(ux):
    ux.app.config['APP_TIMEZONE'] = 'America/Denver'
    permitted_contact(ux)
    login(ux)
    values = review_fields(schedule_later='on', schedule_date='2099-12-31',
                           schedule_time='23:59', client_timezone='Asia/Tokyo')
    review = ux.client.post('/dashboard/review', data=values).get_json()
    expected = datetime(2099, 12, 31, 14, 59)
    assert review['timing'] == format_datetime(expected)
    response = ux.client.post('/dashboard', data={**values, 'review_token': review['review_token']})
    assert response.status_code == 302
    schedule = ux.models.ScheduledMessage.query.one()
    assert schedule.scheduled_at == expected
    assert schedule.message_body == values['message_body'] + '\n\nReply STOP to unsubscribe.'


def test_feedback_review_and_history_share_workspace_display_without_changing_schedule(feedback):
    from tests.test_feedback_invitations import setup
    f = feedback
    f.app.config['APP_TIMEZONE'] = 'America/Denver'
    service, flow, _contact, values, _key, review, invitation = setup(f, timing='later')
    schedule = invitation.scheduled_message
    expected = datetime.strptime(values['schedule_date'] + ' ' + values['schedule_time'], '%Y-%m-%d %H:%M')
    assert schedule.scheduled_at == expected and invitation.timezone == 'UTC'
    assert review['timing'] == format_datetime(expected)
    history = service.invitation_history(f.event, flow)
    assert history[0]['scheduled_local'] == review['timing']
    response = f.client.get(f'/events/{f.event.id}/feedback/{flow.id}')
    assert response.status_code == 200
    assert review['timing'] in response.get_data(as_text=True)
    assert b' UTC' not in response.data and b'(UTC)' not in response.data
