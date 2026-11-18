from datetime import datetime, timedelta, timezone
import json

from tests.test_ux_recovery import login, ux


def test_timeline_scopes_schedules_and_combines_dispatched_reminders(ux):
    now = datetime.now(timezone.utc)
    m = ux.models
    log = m.MessageLog(organization_id=ux.org_id, message_body='A dispatch', target='community', created_at=now)
    other = m.MessageLog(organization_id=ux.other_id, message_body='Private other tenant', target='community')
    ux.db.session.add_all([log, other])
    ux.db.session.flush()
    ux.db.session.add_all([
        m.ScheduledMessage(organization_id=ux.org_id, message_body='Old schedule body', target='community',
                           message_log_id=log.id, scheduled_at=now, automation_source='aoc_events', automation_kind='one_day', status='sent'),
        m.ScheduledMessage(organization_id=ux.org_id, message_body='Next schedule', target='community',
                           scheduled_at=now + timedelta(days=1)),
    ])
    ux.db.session.commit()
    login(ux, 'staff')
    response = ux.client.get('/messages')
    assert response.status_code == 200
    assert response.data.count(b'A dispatch') == 1
    assert b'Automatic reminder' in response.data and b'Next schedule' in response.data
    assert b'Old schedule body' not in response.data and b'Private other tenant' not in response.data
    assert ux.client.get('/home').status_code == 200
    pending = m.ScheduledMessage.query.filter_by(message_body='Next schedule').one()
    linked = m.ScheduledMessage.query.filter_by(message_body='Old schedule body').one()
    assert ux.client.get(f'/scheduled/status?id={pending.id}').get_json()['pending_ids'] == [pending.id]
    assert ux.client.get(f'/scheduled/status?id={linked.id}').get_json()['pending_ids'] == []
    login(ux, 'platform')
    assert ux.client.get('/messages').status_code in (302, 403)
    assert ux.client.get('/home').status_code in (302, 403)


def test_reachability_review_counts_unique_sends_and_expires_without_changing_permission(ux):
    from app.services.reachability_review_service import repeated_unreachable_contacts
    m = ux.models
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    phone = '+17205550199'
    def add_send(org, day, suffix, *, sids=1, status='undelivered', code='30003', indexed=False):
        sent = now - timedelta(days=day)
        details = [{'phone': phone, 'sid': f'SMreview{suffix}{i}'} for i in range(sids)]
        log = m.MessageLog(organization_id=org, target='community', message_body='Hi', created_at=sent,
                           details=json.dumps(details))
        ux.db.session.add(log)
        ux.db.session.flush()
        for detail in details:
            sid = detail['sid']
            ux.db.session.add(m.MessagingUsageRecord(organization_id=org, message_sid=sid, delivery_status=status,
                              delivery_error_code=code, delivery_finalized_at=sent))
            if indexed:
                ux.db.session.add(m.MessageDispatchAttempt(
                    organization_id=org, message_log_id=log.id, recipient_phone=phone, provider_message_sid=sid,
                    logical_send_key='key'+sid, message_body_digest='hash', send_kind='blast', status='sent'))
    add_send(ux.org_id, 29, 'a', indexed=True)
    add_send(ux.org_id, 12, 'b', sids=2)
    add_send(ux.org_id, 1, 'c')
    add_send(ux.org_id, 31, 'expired')
    add_send(ux.org_id, 1, 'pending', status='pending')
    add_send(ux.org_id, 1, 'othercode', code='30006')
    add_send(ux.other_id, 1, 'tenant')
    ux.db.session.commit()
    rows = repeated_unreachable_contacts(ux.org_id, now=now)
    assert [(r['phone'], r['count']) for r in rows] == [(phone, 3)]
    assert repeated_unreachable_contacts(ux.org_id, now=now + timedelta(days=2)) == []
    login(ux)
    page = ux.client.get('/community/delivery-review')
    assert page.status_code == 200 and b'3 separate sends' in page.data and phone.encode() in page.data
    login(ux, 'staff')
    assert ux.client.get('/community/delivery-review').status_code == 403
    assert b'/community/delivery-review' not in ux.client.get('/community').data
    assert m.SuppressedContact.query.count() == 0 and m.UnsubscribedContact.query.count() == 0


def test_estimated_send_overage_consumes_remaining_allowance_before_configured_price():
    from app.services.usage_summary_service import estimated_send_overage
    usage = {'billed': True, 'remaining_segments': 8, 'segment_rate': '0.0375', 'currency': 'usd'}
    assert estimated_send_overage(usage, 6) == '$0.00'
    assert estimated_send_overage(usage, 12) == '$0.15'
    assert estimated_send_overage({**usage, 'billed': False}, 100) is None


def test_today_projects_scoped_receipts_reminder_cost_and_completed_event_follow_up(ux):
    from tests.test_ux_recovery import permitted_contact
    from tests.test_workspace_usage import _mode

    _mode(ux, 'billed')
    permitted_contact(ux)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    m = ux.models
    past = m.Event(organization_id=ux.org_id, title='Finished workshop', date=(now - timedelta(days=3)).date())
    ux.db.session.add_all([
        past,
        m.Event(organization_id=ux.org_id, title='Cancelled workshop', external_status='cancelled',
                date=(now + timedelta(days=1)).date()),
        m.ScheduledMessage(organization_id=ux.org_id, message_body='Tomorrow reminder', target='community',
                           scheduled_at=now + timedelta(days=1), automation_kind='one_day', automation_source='event_sync'),
        m.ScheduledMessage(organization_id=ux.org_id, message_body='Held invitation', target='community',
                           scheduled_at=now, status='held'),
        m.ScheduledMessage(organization_id=ux.other_id, message_body='Other organization reminder', target='community',
                           scheduled_at=now + timedelta(hours=4), automation_kind='one_day', automation_source='event_sync'),
        m.MessagingUsageRecord(organization_id=ux.org_id, message_sid='SMtodayfailed', delivery_status='undelivered',
                               created_at=now),
    ])
    ux.db.session.commit()
    login(ux)
    page = ux.client.get('/home')
    assert page.status_code == 200
    for text in ('1 text not delivered', 'Held invitation', 'Tomorrow reminder', '1 eligible', '$0.00 extra',
                 'Finished workshop', 'segments used this period'):
        assert text in page.get_data(as_text=True)
    assert b'Cancelled workshop' not in page.data and b'Other organization reminder' not in page.data
    follow_up = f'/events/{past.id}/feedback/follow-up'
    assert follow_up.encode() in page.data
    assert ux.client.get(follow_up).location.endswith(f'/events/{past.id}/feedback/new')
    flow = m.SurveyFlow(organization_id=ux.org_id, name='Event feedback', trigger_keyword='EVENTREVIEW',
                        purpose='event_feedback', feedback_event_id=past.id, is_active=True)
    ux.db.session.add(flow)
    ux.db.session.commit()
    assert ux.client.get(follow_up).location.endswith(f'/events/{past.id}/feedback/{flow.id}/invite')
    assert m.OutboundRequest.query.count() == 0
