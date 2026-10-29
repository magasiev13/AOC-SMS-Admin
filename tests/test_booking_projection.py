"""Booking identity/seat acceptance across the signed integration and projection."""
from datetime import datetime, timezone, timedelta
import importlib
import json
import logging
from unittest.mock import patch

import pytest

from tests.test_aoc_event_sync import TestAocEventSyncWebhook as _AocFixture
from tests.test_event_feedback import feedback


@pytest.fixture
def case():
    helper = _AocFixture()
    helper.setUp()
    yield helper
    helper.tearDown()


def post(case, booking_id, phone, **changes):
    booking = case._booking_payload(booking_id, phone)
    booking.update(changes)
    response = case._signed_post({'action': 'booking_upsert', 'event': case._event_payload(),
                                 'booking': booking}, None, None)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response


def test_group_booking_means_one_booking_ten_places_one_contact(case):
    from app.services.booking_projection_service import roster_summary
    post(case, 'family', '+17205550101', spaces=10, status='approved')
    summary = roster_summary(case.Event.query.one())
    assert (summary['bookings'], summary['reserved_places'], summary['reachable_contacts']) == (1, 10, 1)
    assert summary['confirmed_bookings'] == 1
    assert summary['coverage_verified'] is False


def test_shared_phone_keeps_separate_bookings_and_survives_one_cancellation(case):
    from app.models import EventBooking
    from app.services.booking_projection_service import roster_summary
    from app.services.recipient_permission_service import recipient_permission_status
    post(case, 'first', '+17205550102', spaces=3)
    original_registration = case.EventRegistration.query.one().id
    post(case, 'second', '+17205550102', spaces=7)
    assert EventBooking.query.count() == 2
    assert case.EventRegistration.query.one().id == original_registration
    post(case, 'first', '+17205550102', spaces=3, status='cancelled', active=False,
         updated_at='2026-06-23T12:06:00-06:00')
    summary = roster_summary(case.Event.query.one())
    assert (summary['bookings'], summary['reserved_places'], summary['reachable_contacts']) == (1, 7, 1)
    assert case.EventRegistration.query.one().id == original_registration
    assert recipient_permission_status(case.organization.id, '+17205550102', purpose='event', event_id=case.Event.query.one().id).allowed
    assert EventBooking.query.filter_by(external_booking_id='events_manager:first').one().cancellation_history


@pytest.mark.parametrize('replacement,state', [('', 'missing'), ('invalid-number', 'invalid')])
def test_cleared_or_invalid_contact_retains_booking_places_removes_sms(case, replacement, state):
    from app.models import EventBooking
    from app.services.booking_projection_service import roster_summary
    post(case, 'contact-change', '+17205550103', spaces=10)
    response = post(case, 'contact-change', replacement, spaces=10, updated_at='2026-06-23T12:06:00-06:00')
    booking = EventBooking.query.one()
    assert booking.contact_state == state and booking.phone is None
    assert case.EventRegistration.query.count() == 0
    summary = roster_summary(case.Event.query.one())
    assert (summary['bookings'], summary['reserved_places'], summary['reachable_contacts']) == (1, 10, 0)
    assert replacement not in str(response.get_json()) if replacement else True


@pytest.mark.parametrize('phone,contact_state', [('+17205550121','valid'),('invalid-number','invalid')])
def test_minimal_source_deletion_preserves_booking_contact_and_cancellation_evidence(case,phone,contact_state):
    from app.models import EventBooking
    post(case,'minimal-delete',phone,spaces=10,name='Original group contact',person_id='person:retained',
         selections=[{'label':'Family places','quantity':10}],comment='Retained booking note',sms_consent=True)
    booking=EventBooking.query.one()
    retained={field:getattr(booking,field) for field in ('name','phone','contact_state','party_size',
        'external_person_id','selections_json','comment','sms_consent')}
    assert booking.contact_state == contact_state
    response=case._signed_post({'action':'booking_deleted','event':case._event_payload(),
        'booking':{'booking_id':'minimal-delete','updated_at':'2026-06-23T12:06:00-06:00'}},None,None)
    assert response.status_code == 200,response.get_data(as_text=True)
    case.db.session.refresh(booking)
    assert {field:getattr(booking,field) for field in retained} == retained
    assert booking.is_active is False and booking.status == 'cancelled'
    assert len(booking.cancellation_history) == 1 and booking.cancelled_at is not None
    assert case.EventRegistration.query.count() == 0


def test_source_deletion_can_explicitly_clear_previously_recorded_contact_details(case):
    from app.models import EventBooking
    post(case,'clear-delete','+17205550122',spaces=10,name='Original',person_id='retained',
         selections=[{'label':'Family places','quantity':10}],comment='Original comment',sms_consent=True)
    response=case._signed_post({'action':'booking_deleted','event':case._event_payload(),
        'booking':{'booking_id':'clear-delete','updated_at':'2026-06-23T12:06:00-06:00',
                   'phone':'','name':'','person_id':'','comment':'','selections':[],'sms_consent':None,'spaces':0}},None,None)
    assert response.status_code == 200,response.get_data(as_text=True)
    booking=EventBooking.query.one()
    assert (booking.name,booking.phone,booking.external_person_id,booking.comment,booking.sms_consent) == (None,None,None,None,None)
    assert booking.contact_state == 'missing' and booking.party_size == 0 and booking.selections_json == '[]'
    assert not booking.is_active and len(booking.cancellation_history) == 1
    assert case.EventRegistration.query.count() == 0


def test_phone_change_updates_only_its_contribution(case):
    from app.models import EventBooking
    post(case, 'one', '+17205550104', spaces=2)
    post(case, 'two', '+17205550104', spaces=5)
    post(case, 'one', '+17205550105', spaces=2, updated_at='2026-06-23T12:06:00-06:00')
    assert EventBooking.query.count() == 2
    assert {row.phone: row.booking_spaces for row in case.EventRegistration.query.all()} == {
        '+17205550104': 5, '+17205550105': 2,
    }


def test_cancelled_booking_rejects_duplicate_and_stale_reactivation(case):
    from app.models import EventBooking
    post(case, 'cancelled', '+17205550106')
    post(case, 'cancelled', '+17205550106', active=False, status='cancelled',
         updated_at='2026-06-23T12:06:00-06:00')
    post(case, 'cancelled', '+17205550106')
    post(case, 'cancelled', '+17205550106', updated_at='2026-06-23T12:06:00-06:00')
    assert EventBooking.query.count() == 1
    assert EventBooking.query.one().is_active is False
    assert case.EventRegistration.query.count() == 0
    assert len(EventBooking.query.one().cancellation_history) == 1


def test_distinct_forms_keep_identical_entry_numbers_separate(case):
    from app.models import EventBooking
    post(case, '45', '+17205550107', provider='wpforms', source_scope='wpforms:10')
    post(case, '45', '+17205550107', provider='wpforms', source_scope='wpforms:20')
    assert EventBooking.query.count() == 2
    assert case.EventRegistration.query.count() == 1


def test_legacy_registration_id_is_retained_without_claiming_verified_history(case):
    from app.models import EventBooking
    from app.services.booking_projection_service import ensure_legacy_bookings, roster_summary
    post(case, 'event-anchor', None, spaces=0)
    event = case.Event.query.one()
    registration = case.EventRegistration(organization_id=case.organization.id, event_id=event.id,
        name='Historical record', phone='+17205550108', booking_spaces=4)
    case.db.session.add(registration)
    case.db.session.flush()
    original_id = registration.id
    ensure_legacy_bookings(event)
    assert case.EventRegistration.query.one().id == original_id
    assert EventBooking.query.filter_by(projection_registration_id=original_id).one().legacy_evidence
    summary = roster_summary(event)
    assert summary['legacy_records'] == 1 and summary['coverage_verified'] is False


def test_inventory_cancellation_cannot_erase_newer_source_revision(case):
    from app.models import EventBooking
    from app.services.booking_projection_service import cancel_booking
    post(case, 'new-update', '+17205550109', source_revision=6,
         updated_at='2026-06-23T12:10:00-06:00')
    booking = EventBooking.query.one()
    assert not cancel_booking(booking, datetime.now(timezone.utc), source_revision=5,
        source_updated_at=datetime(2026, 6, 23, 18, 9), reason='complete_inventory_missing')
    assert booking.is_active and case.EventRegistration.query.count() == 1


def test_booking_ledger_is_tenant_scoped(case):
    from app.models import EventBooking
    from app.tenant import organization_context
    post(case, 'tenant', '+17205550110')
    with organization_context(case.other_organization.id):
        assert EventBooking.query.count() == 0
    with organization_context(case.organization.id):
        assert EventBooking.query.count() == 1


def test_additive_migration_is_idempotent_and_preserves_legacy_rows(case):
    from app.models import EventBooking
    post(case, 'event-anchor', None, spaces=0)
    event = case.Event.query.one()
    legacy = case.EventRegistration(organization_id=case.organization.id,event_id=event.id,
        name='Historical',phone='+17205550111')
    case.db.session.add(legacy)
    case.db.session.commit()
    migration = importlib.import_module('app.saas_migrations.029_event_booking_ledger')
    with case.db.engine.begin() as connection:
        migration.apply(connection, logging.getLogger(__name__))
        migration.apply(connection, logging.getLogger(__name__))
    assert case.EventRegistration.query.one().id == legacy.id
    assert EventBooking.query.filter_by(projection_registration_id=legacy.id).count() == 1


def test_duplicate_representations_require_an_explicit_link(case):
    from app.models import EventBooking
    from app.services.booking_projection_service import link_duplicate_booking, roster_summary
    post(case, 'primary', '+17205550112', spaces=10, source_scope='events_manager:54')
    post(case, 'copy', '+17205550112', spaces=10, provider='wpforms', source_scope='wpforms:20')
    summary = roster_summary(case.Event.query.one())
    assert summary['bookings'] == 2 and summary['reserved_places'] == 20
    first, second = EventBooking.query.order_by(EventBooking.id).all()
    link_duplicate_booking(second, first)
    summary = roster_summary(case.Event.query.one())
    assert summary['bookings'] == 1 and summary['reserved_places'] == 10
    assert EventBooking.query.count() == 2
    from app.services.booking_projection_service import unlink_duplicate_booking
    unlink_duplicate_booking(second)
    assert roster_summary(case.Event.query.one())['reserved_places'] == 20


def test_duplicate_links_do_not_create_chains_or_cycles(case):
    from app.models import EventBooking
    from app.services.booking_projection_service import link_duplicate_booking
    for key in ('one','two','three'):
        post(case, key, '+17205550119')
    one,two,three = EventBooking.query.order_by(EventBooking.id).all()
    link_duplicate_booking(two, one)
    with pytest.raises(ValueError, match='already represents'):
        link_duplicate_booking(one, three)
    with pytest.raises(ValueError, match='primary booking'):
        link_duplicate_booking(one, two)


def test_generic_business_app_uses_same_ledger_and_permission_rules(case):
    from app.services.booking_projection_service import upsert_booking, roster_summary
    from app.services.recipient_permission_service import recipient_permission_status
    from app.tenant import organization_context
    post(case, 'anchor', None, spaces=0)
    event = case.Event.query.one()
    with organization_context(case.organization.id):
        booking = upsert_booking(event, {'booking_id': 'order:241', 'phone': '+17205550113',
            'spaces': 8, 'status': 'paid', 'sms_consent': True, 'updated_at': '2026-06-23T19:00:00Z'},
            'app:client-crm', datetime.now(timezone.utc), source_scope='orders:conference')
        assert booking.external_booking_id == 'order:241'
        assert recipient_permission_status(case.organization.id, booking.phone, purpose='event', event_id=event.id).allowed
    assert roster_summary(event)['reserved_places'] == 8


def test_generic_roster_reachable_count_requires_actual_event_permission(case):
    from app.services.booking_projection_service import upsert_booking, roster_summary
    from app.tenant import organization_context
    case.app.config['COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS'] = []
    post(case, 'anchor', None, spaces=0)
    event = case.Event.query.one()
    with organization_context(case.organization.id):
        upsert_booking(event, {'booking_id':'unknown-consent', 'phone':'+17205550120',
            'spaces':3,'status':'paid','updated_at':'2026-06-23T19:00:00Z'}, 'app:crm', datetime.now(timezone.utc))
    summary = roster_summary(event)
    assert summary['reserved_places'] == 3 and summary['supplied_contacts'] == 1
    assert summary['reachable_contacts'] == 0 and summary['permission_restricted_contacts'] == 1


def test_repeat_survey_completion_updates_the_same_booking(case):
    from app.services.booking_projection_service import create_local_booking, roster_summary
    from app.models import EventBooking
    post(case, 'anchor', None, spaces=0)
    event = case.Event.query.one()
    create_local_booking(event, 'Before', '+17205550114', source='registration_survey',
        external_booking_id='flow:20:thread:30')
    create_local_booking(event, 'After', '+17205550114', source='registration_survey',
        external_booking_id='flow:20:thread:30')
    assert EventBooking.query.filter_by(source='registration_survey').count() == 1
    assert case.EventRegistration.query.one().name == 'After'
    assert roster_summary(event)['reserved_places'] == 1


@pytest.mark.parametrize('spaces', [1.5, True, '1.5', -1])
def test_fractional_boolean_and_negative_places_are_rejected(case, spaces):
    booking = case._booking_payload('bad-places', '+17205550115')
    booking['spaces'] = spaces
    response = case._signed_post({'action':'booking_upsert', 'event':case._event_payload(),
                                 'booking':booking}, None, None)
    assert response.status_code == 400
    assert case.EventRegistration.query.count() == 0


def test_newer_numeric_revision_can_correct_phone_with_same_second_timestamp(case):
    post(case, 'same-second', '+17205550116', source_revision=1)
    post(case, 'same-second', '+17205550117', source_revision=2)
    assert case.EventRegistration.query.one().phone == '+17205550117'


def test_postgres_inventory_rechecks_a_preloaded_booking_after_event_lock(case):
    if case.db.engine.dialect.name != 'postgresql':
        pytest.skip('Requires the isolated PostgreSQL concurrency database.')
    from sqlalchemy import text
    from app.models import EventBooking
    from app.services.booking_projection_service import cancel_booking
    post(case, 'concurrent', '+17205550118', source_revision=1)
    stale = EventBooking.query.one()
    assert stale.source_revision == 1
    # Another committed source transaction advances the row while this ORM
    # session still holds its older loaded instance.
    with case.db.engine.begin() as connection:
        connection.execute(text('UPDATE event_bookings SET source_revision=10,source_updated_at=:modified WHERE id=:id'),
                           {'id':stale.id, 'modified':datetime(2026,6,23,19,0)})
    assert not cancel_booking(stale, datetime.now(timezone.utc), source_revision=5,
        source_updated_at=datetime(2026,6,23,18,30), reason='complete_inventory_missing')
    assert stale.source_revision == 10 and stale.is_active


@pytest.mark.parametrize('source_scope',['','orders:scoped'])
def test_batch_destination_swap_preserves_raw_and_scoped_projection_identities(case,source_scope):
    from app.models import EventBooking
    from app.services.booking_projection_service import (
        upsert_booking,reconcile_event_projection,apply_booking_permission_transitions,
    )
    post(case,'anchor',None,spaces=0)
    event=case.Event.query.one()
    phones=('+17205550236','+17205550237')
    for key,phone in zip(('first','second'),phones):
        upsert_booking(event,{'booking_id':key,'phone':phone,'spaces':2,'sms_consent':True,
            'source_revision':1,'updated_at':'2026-06-23T18:00:00Z'},'other-application',
            datetime.now(timezone.utc),source_scope=source_scope)
    case.db.session.commit()
    original={row.phone:row.id for row in case.EventRegistration.query.all()}
    transitions=[]
    for key,phone in zip(('first','second'),reversed(phones)):
        booking=upsert_booking(event,{'booking_id':key,'phone':phone,'spaces':2,'sms_consent':True,
            'source_revision':2,'updated_at':'2026-06-23T19:00:00Z'},'other-application',
            datetime.now(timezone.utc),source_scope=source_scope,project=False,seed_legacy=False,sync_permissions=False)
        transitions.append(booking._permission_transition)
    apply_booking_permission_transitions(event,transitions)
    reconcile_event_projection(event,seed_legacy=False)
    assert {row.phone:row.id for row in case.EventRegistration.query.all()}==original
    for booking in EventBooking.query.filter_by(source='other-application').all():
        row=case.EventRegistration.query.filter_by(phone=booking.phone).one()
        expected=f'ledger:{booking.id}' if source_scope else booking.external_booking_id
        assert row.external_booking_id==expected and row.id==original[booking.phone]


@pytest.mark.parametrize('source,revision',[('aoc-wordpress',7),('other-application',None)])
def test_scope_recovery_does_not_reinterpret_numeric_or_generic_unscoped_identities(case,source,revision):
    from app.models import EventBooking
    from app.services.booking_projection_service import upsert_booking
    post(case,'anchor',None,spaces=0)
    event=case.Event.query.one()
    payload={'booking_id':'deliberate-unscoped','phone':'+17205550243','spaces':3,
             'updated_at':'2026-06-23T18:00:00Z','source_revision':revision}
    old=upsert_booking(event,payload,source,datetime.now(timezone.utc))
    old_id=old.id
    payload.update(source_revision=8,updated_at='2026-06-23T19:00:00Z')
    scoped=upsert_booking(event,payload,source,datetime.now(timezone.utc),source_scope='orders:new')
    assert scoped.id!=old_id
    assert EventBooking.query.filter_by(id=old_id).one().source_scope==''
    assert EventBooking.query.filter_by(id=old_id).one().source_revision==revision


def _linked_source_pair(f, *, secondary_phone=None, confirmed_signup=False):
    from app.services.booking_projection_service import upsert_booking
    f.app.config['COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS']=[f.org.slug] if confirmed_signup else []
    now=f.m.utc_now()
    base={'phone':'+17205550251','sms_consent':True,'community_sms_consent':True,
          'spaces':1,'source_revision':1,'updated_at':(now-timedelta(minutes=10)).isoformat()}
    primary=upsert_booking(f.event,{**base,'booking_id':'primary'},'fixture-bookings',now,source_scope='orders:fixture')
    secondary=upsert_booking(f.event,{**base,'booking_id':'secondary','phone':secondary_phone or base['phone']},
        'fixture-bookings',now,source_scope='orders:fixture')
    return primary,secondary


def _ordinary_event_snapshot(f, phone):
    schedule=f.m.ScheduledMessage(organization_id=f.org.id,event_id=f.event.id,target='event',
        message_body='Ordinary scheduled event message',status='pending',
        scheduled_at=f.m.utc_now().replace(tzinfo=None)-timedelta(minutes=1),test_mode=False,
        test_recipient_snapshot_json=json.dumps([{'phone':phone,'name':'Saved event contact'}]))
    f.db.session.add(schedule);f.db.session.commit()
    return schedule


def _run_ordinary_event_scheduler(f):
    from app.services.scheduler_service import send_scheduled_messages
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app)
    f.db.session.expire_all()


@pytest.mark.parametrize('confirmed_signup',[False,True])
def test_primary_cancellation_and_late_linked_duplicate_updates_cannot_send_old_event_snapshot(feedback,confirmed_signup):
    from app.services.booking_projection_service import upsert_booking,link_duplicate_booking
    from app.services.recipient_permission_service import recipient_permission_status
    f=feedback
    with f.organization_context(f.org.id):
        primary,secondary=_linked_source_pair(f,confirmed_signup=confirmed_signup)
        phone=primary.phone
        link_duplicate_booking(secondary,primary)
        schedule=_ordinary_event_snapshot(f,phone)
        upsert_booking(f.event,{'booking_id':'primary','source_revision':2,
            'updated_at':(f.m.utc_now()-timedelta(minutes=2)).isoformat()},'fixture-bookings',
            f.m.utc_now(),source_scope='orders:fixture',deleted=True)
        f.db.session.commit()
    _run_ordinary_event_scheduler(f)
    assert f.provider.client.messages.create.call_count==0
    assert f.m.EventRegistration.query.filter_by(event_id=f.event.id).count()==0
    assert not recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed
    assert recipient_permission_status(f.org.id,phone,purpose='community').allowed

    with f.organization_context(f.org.id):
        upsert_booking(f.event,{'booking_id':'secondary','phone':phone,'sms_consent':True,
            'community_sms_consent':True,'source_revision':2,'updated_at':f.m.utc_now().isoformat()},
            'fixture-bookings',f.m.utc_now(),source_scope='orders:fixture')
        f.db.session.commit()
        _ordinary_event_snapshot(f,phone)
    _run_ordinary_event_scheduler(f)
    assert f.provider.client.messages.create.call_count==0
    assert not recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed
    assert recipient_permission_status(f.org.id,phone,purpose='community').allowed


def test_independent_active_booking_keeps_event_snapshot_eligible_after_linked_primary_cancellation(feedback):
    from app.services.booking_projection_service import upsert_booking,link_duplicate_booking
    from app.services.recipient_permission_service import recipient_permission_status
    f=feedback
    with f.organization_context(f.org.id):
        primary,secondary=_linked_source_pair(f,confirmed_signup=True)
        phone=primary.phone
        link_duplicate_booking(secondary,primary)
        upsert_booking(f.event,{'booking_id':'independent','phone':phone,'spaces':2,
            'sms_consent':True,'community_sms_consent':True,'source_revision':1,
            'updated_at':(f.m.utc_now()-timedelta(minutes=5)).isoformat()},
            'fixture-bookings',f.m.utc_now(),source_scope='orders:fixture')
        _ordinary_event_snapshot(f,phone)
        upsert_booking(f.event,{'booking_id':'primary','source_revision':2,
            'updated_at':(f.m.utc_now()-timedelta(minutes=2)).isoformat()},'fixture-bookings',
            f.m.utc_now(),source_scope='orders:fixture',deleted=True)
        f.db.session.commit()
    _run_ordinary_event_scheduler(f);_run_ordinary_event_scheduler(f)
    assert f.provider.client.messages.create.call_count==1
    assert f.m.EventRegistration.query.filter_by(event_id=f.event.id,phone=phone).count()==1
    assert recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed
    assert recipient_permission_status(f.org.id,phone,purpose='community').allowed


def test_link_to_inactive_primary_removes_secondary_destination_and_unlink_restores_saved_event_consent(feedback):
    from app.services.booking_projection_service import upsert_booking,link_duplicate_booking,unlink_duplicate_booking
    from app.services.recipient_permission_service import recipient_permission_status
    f=feedback
    with f.organization_context(f.org.id):
        primary,secondary=_linked_source_pair(f,secondary_phone='+17205550252')
        phone=secondary.phone
        upsert_booking(f.event,{'booking_id':'primary','source_revision':2,
            'updated_at':(f.m.utc_now()-timedelta(minutes=2)).isoformat()},'fixture-bookings',
            f.m.utc_now(),source_scope='orders:fixture',deleted=True)
        schedule=_ordinary_event_snapshot(f,phone)
        link_duplicate_booking(secondary,primary);f.db.session.commit()
    _run_ordinary_event_scheduler(f)
    assert f.provider.client.messages.create.call_count==0
    assert not recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed
    assert recipient_permission_status(f.org.id,phone,purpose='community').allowed
    with f.organization_context(f.org.id):
        unlink_duplicate_booking(secondary);f.db.session.commit()
    assert recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed
    assert recipient_permission_status(f.org.id,phone,purpose='community').allowed


def test_unlink_does_not_regrant_an_explicit_event_withdrawal(feedback):
    from app.services.booking_projection_service import link_duplicate_booking,unlink_duplicate_booking
    from app.services.recipient_permission_service import record_permission_evidence,recipient_permission_status
    f=feedback
    with f.organization_context(f.org.id):
        primary,secondary=_linked_source_pair(f,secondary_phone='+17205550253')
        phone=secondary.phone
        link_duplicate_booking(secondary,primary)
        assert not recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed
        assert recipient_permission_status(f.org.id,phone,purpose='community').allowed
        record_permission_evidence(f.org.id,phone,purpose='event',event_id=f.event.id,
            decision='denied',source='owner_review',evidence_key='explicit-event-withdrawal',occurred_at=f.m.utc_now())
        unlink_duplicate_booking(secondary);f.db.session.commit()
    assert not recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed
    assert recipient_permission_status(f.org.id,phone,purpose='community').allowed


@pytest.mark.parametrize('revision', [None, 7])
def test_generic_scopes_keep_independent_booking_ids_across_events(case, revision):
    from app.models import EventBooking
    from app.services.booking_projection_service import upsert_booking
    post(case, 'anchor', None, spaces=0)
    first_event = case.Event.query.one()
    other_event = case.Event(organization_id=case.organization.id, title='Another company event')
    case.db.session.add(other_event)
    case.db.session.flush()
    payload = {'booking_id':'store-local-id', 'phone':'+17205550244', 'spaces':3,
               'updated_at':'2026-06-23T18:00:00Z', 'source_revision':revision}
    old = upsert_booking(first_event, payload, 'other-application', datetime.now(timezone.utc))
    scoped = upsert_booking(other_event, {**payload, 'source_revision':8}, 'other-application',
                            datetime.now(timezone.utc), source_scope='another-store')
    assert old.id != scoped.id and old.event_id == first_event.id and scoped.event_id == other_event.id
    assert EventBooking.query.filter_by(id=old.id).one().source_scope == ''
    assert EventBooking.query.filter_by(id=scoped.id).one().source_scope == 'another-store'


def test_queued_unscoped_wordpress_callback_reuses_identity_without_revision_downgrade(case):
    from app.models import EventBooking
    post(case,'cutover-entry','+17205550131',provider='wpforms',source_scope='wpforms:123',
         source_revision=7,spaces=10)
    original=EventBooking.query.one()
    original_id=original.id
    post(case,'cutover-entry','+17205550132',provider='wpforms',spaces=1,
         updated_at='2026-06-23T14:00:00-06:00')
    assert EventBooking.query.count()==1
    retained=EventBooking.query.one()
    assert retained.id==original_id and retained.source_scope=='wpforms:123'
    assert retained.source_revision==7 and retained.party_size==10 and retained.phone=='+17205550131'
    deleted=case._signed_post({'action':'booking_deleted','event':case._event_payload(),
        'booking':{'provider':'wpforms','booking_id':'cutover-entry','updated_at':'2026-06-23T15:00:00-06:00'}},None,None)
    assert deleted.status_code==200
    assert EventBooking.query.one().is_active
    assert EventBooking.query.one().source_revision==7
    assert case.EventRegistration.query.one().booking_spaces==10


def test_unscoped_wordpress_callback_retains_scope_when_timestamp_version_is_current(case):
    from app.models import EventBooking
    post(case,'older-adapter','+17205550133',provider='wpforms',source_scope='wpforms:124',spaces=2)
    booking_id=EventBooking.query.one().id
    post(case,'older-adapter','+17205550133',provider='wpforms',spaces=4,
         updated_at='2026-06-23T13:00:00-06:00')
    assert EventBooking.query.count()==1
    retained=EventBooking.query.one()
    assert retained.id==booking_id and retained.source_scope=='wpforms:124' and retained.party_size==4


def test_unscoped_wordpress_callback_rejects_ambiguous_scoped_identity_without_cloning(case):
    from app.models import EventBooking
    post(case,'shared-entry-id','+17205550134',provider='wpforms',source_scope='wpforms:125',spaces=2)
    post(case,'shared-entry-id','+17205550134',provider='wpforms',source_scope='wpforms:126',spaces=3)
    response=case._signed_post({'action':'booking_upsert','event':case._event_payload(),
        'booking':case._booking_payload('shared-entry-id','+17205550134')|{'provider':'wpforms','updated_at':'2026-06-23T14:00:00-06:00'}},None,None)
    assert response.status_code==400
    assert EventBooking.query.count()==2
    assert {row.source_scope for row in EventBooking.query.all()}=={'wpforms:125','wpforms:126'}
    assert case.EventRegistration.query.one().booking_spaces==5


def test_unscoped_wordpress_identity_matching_uses_provider_and_event_not_phone(case):
    from app.models import Event,EventBooking
    from app.services.booking_projection_service import upsert_booking
    post(case,'same-id','+17205550135',provider='wpforms',source_scope='wpforms:127',spaces=2)
    event=case.Event.query.one()
    # A different provider with the same numeric ID and destination is independent.
    post(case,'same-id','+17205550135',provider='events_manager',spaces=3)
    assert EventBooking.query.count()==2
    assert case.EventRegistration.query.one().booking_spaces==5
    other_event=Event(organization_id=case.organization.id,title='Other event',date=event.date)
    case.db.session.add(other_event);case.db.session.flush()
    with pytest.raises(ValueError,match='cannot move'):
        upsert_booking(other_event,{'provider':'wpforms','booking_id':'same-id','phone':'+17205550135',
            'updated_at':'2026-06-23T20:00:00Z'},'aoc-wordpress',datetime.now(timezone.utc))
    assert EventBooking.query.count()==2


def _signed_app_consent_fixture(case, *, confirmed_signup=False):
    from cryptography.fernet import Fernet
    from app.services.aoc_event_sync_service import configure_event_sync_integration
    from tests.test_app_registration_sync import payload, post as app_post
    case.organization.name='Independent company'
    case.organization.slug='independent-company'
    case.app.config['COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS']=[case.organization.slug] if confirmed_signup else []
    case.app.config['TWILIO_CREDENTIAL_ENCRYPTION_KEY']=Fernet.generate_key().decode()
    case.integration,case.app_secret=configure_event_sync_integration(case.organization.id,True,None,provider='app')
    case.db.session.commit()
    data=payload(case,'consented-order')
    data['booking']['community_sms_consent']=True
    assert app_post(case,data).status_code==200
    return data,app_post


@pytest.mark.parametrize('contact_update',[{}, {'phone':''}, {'phone':'invalid-number'}])
def test_signed_cancellation_explicit_community_withdrawal_applies_with_missing_or_cleared_phone(case,contact_update):
    from app.models import EventBooking,RecipientPermissionEvidence
    from app.services.recipient_permission_service import recipient_permission_status,filter_permission_recipients
    data,app_post=_signed_app_consent_fixture(case)
    phone=data['booking']['phone'];event=case.Event.query.one()
    assert recipient_permission_status(case.organization.id,phone,purpose='community').allowed
    deleted={**data,'action':'booking_deleted','booking':{
        'booking_id':data['booking']['booking_id'],'source_scope':data['booking']['source_scope'],
        'source_revision':2,'updated_at':'2026-06-23T18:01:00Z','community_sms_consent':False,**contact_update}}
    response=app_post(case,deleted)
    assert response.status_code==200,response.get_data(as_text=True)
    assert not EventBooking.query.one().is_active
    assert case.EventRegistration.query.count()==0
    status=recipient_permission_status(case.organization.id,phone,purpose='community')
    assert status.decision=='denied' and not status.allowed
    allowed,skipped,_=filter_permission_recipients([{'phone':phone,'name':'Former booking contact'}],
        organization_id=case.organization.id,purpose='community')
    assert allowed==[] and len(skipped)==1
    newest=RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='event').order_by(RecipientPermissionEvidence.id.desc()).first()
    assert newest is None or newest.decision!='granted'
    community=RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='community').filter(
        RecipientPermissionEvidence.source.like('event_sync:%')).order_by(RecipientPermissionEvidence.id.desc()).first()
    assert community.decision=='denied'


def test_plain_cancellation_preserves_independent_community_permission_and_sibling_event_audience(case):
    from app.services.recipient_permission_service import recipient_permission_status,record_permission_evidence
    from app.models import EventBooking
    data,app_post=_signed_app_consent_fixture(case)
    phone=data['booking']['phone'];event=case.Event.query.one()
    sibling={**data,'booking':{**data['booking'],'booking_id':'other-active-order'}}
    assert app_post(case,sibling).status_code==200
    record_permission_evidence(case.organization.id,phone,purpose='community',decision='granted',
        source='owner_review',evidence_key='independent-community-grant',verified=True,
        occurred_at=datetime(2026,6,23,18,0,30,tzinfo=timezone.utc),
        metadata={'proof':'Separately retained community signup reviewed by the owner.'})
    case.db.session.commit()
    deleted={**data,'action':'booking_deleted','booking':{'booking_id':data['booking']['booking_id'],
        'source_scope':data['booking']['source_scope'],'source_revision':2,'updated_at':'2026-06-23T18:01:00Z'}}
    assert app_post(case,deleted).status_code==200
    assert recipient_permission_status(case.organization.id,phone,purpose='community').allowed
    assert recipient_permission_status(case.organization.id,phone,purpose='event',event_id=event.id).allowed
    assert case.EventRegistration.query.one().phone==phone
    assert EventBooking.query.filter_by(is_active=True).count()==1


def test_explicit_community_withdrawal_does_not_remove_another_booking_event_permission(case):
    from app.services.recipient_permission_service import recipient_permission_status
    data,app_post=_signed_app_consent_fixture(case)
    phone=data['booking']['phone'];event=case.Event.query.one()
    sibling={**data,'booking':{**data['booking'],'booking_id':'remaining-order'}}
    assert app_post(case,sibling).status_code==200
    deleted={**data,'action':'booking_deleted','booking':{'booking_id':data['booking']['booking_id'],
        'source_scope':data['booking']['source_scope'],'source_revision':2,'updated_at':'2026-06-23T18:01:00Z',
        'community_sms_consent':False}}
    assert app_post(case,deleted).status_code==200
    assert not recipient_permission_status(case.organization.id,phone,purpose='community').allowed
    assert recipient_permission_status(case.organization.id,phone,purpose='event',event_id=event.id).allowed
    assert case.EventRegistration.query.one().phone==phone
    # A later sibling event update without a community signal is not a new opt-in.
    sibling['booking'].pop('community_sms_consent')
    sibling['booking'].update(source_revision=2,updated_at='2026-06-23T18:02:00Z')
    assert app_post(case,sibling).status_code==200
    assert not recipient_permission_status(case.organization.id,phone,purpose='community').allowed
    assert recipient_permission_status(case.organization.id,phone,purpose='event',event_id=event.id).allowed


def test_cancelled_record_can_record_explicit_community_permission_without_new_event_grant_and_STOP_wins(case):
    from app.services.recipient_permission_service import recipient_permission_status,record_permission_evidence
    from app.models import RecipientPermissionEvidence
    data,app_post=_signed_app_consent_fixture(case)
    phone=data['booking']['phone'];event=case.Event.query.one()
    deleted={**data,'action':'booking_deleted','booking':{'booking_id':data['booking']['booking_id'],
        'source_scope':data['booking']['source_scope'],'source_revision':2,'updated_at':'2026-06-23T18:01:00Z',
        'community_sms_consent':False}}
    assert app_post(case,deleted).status_code==200
    record_permission_evidence(case.organization.id,phone,purpose='all',decision='denied',source='inbound_stop',
        evidence_key='owned-stop',occurred_at=datetime(2026,6,23,18,2,tzinfo=timezone.utc))
    case.db.session.commit()
    count_before=RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='event',decision='granted').count()
    deleted['booking'].update(source_revision=3,updated_at='2026-06-23T18:03:00Z',community_sms_consent=True,sms_consent=True)
    assert app_post(case,deleted).status_code==200
    assert RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='event',decision='granted').count()==count_before
    status=recipient_permission_status(case.organization.id,phone,purpose='community')
    assert not status.allowed and status.reason=='permission_revoked'


def test_confirmed_signup_contract_retains_raw_false_on_deletion_without_inventing_broad_optout(case):
    import json
    from app.models import RecipientPermissionEvidence
    from app.services.recipient_permission_service import recipient_permission_status
    data,app_post=_signed_app_consent_fixture(case,confirmed_signup=True)
    phone=data['booking']['phone']
    deleted={**data,'action':'booking_deleted','booking':{'booking_id':data['booking']['booking_id'],
        'source_scope':data['booking']['source_scope'],'source_revision':2,'updated_at':'2026-06-23T18:01:00Z',
        'community_sms_consent':False}}
    assert app_post(case,deleted).status_code==200
    assert recipient_permission_status(case.organization.id,phone,purpose='community').allowed
    entries=RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='community').order_by(RecipientPermissionEvidence.id.desc()).all()
    assert any(json.loads(entry.metadata_json or '{}').get('community_sms_consent') is False and
        json.loads(entry.metadata_json or '{}').get('consent_basis')=='confirmed_community_signup' for entry in entries)
    assert not any(entry.source.startswith('booking_community:') for entry in entries)


def test_registration_survey_relink_keeps_each_event_booking_and_same_event_repeats_idempotent(feedback):
    from tests.test_event_feedback import input_message,drain
    f=feedback
    flow=f.m.SurveyFlow(organization=f.org,name='Reusable registration',trigger_keyword='RELINK RSVP',linked_event=f.event)
    flow.set_questions(['Your name?'])
    second=f.m.Event(organization=f.org,title='Second registration event')
    f.db.session.add_all([flow,second]);f.db.session.commit()

    def complete(name):
        assert input_message(f,flow.trigger_keyword)['status']=='survey_started'
        drain(f)
        assert input_message(f,name)['status']=='survey_response'
        drain(f)

    complete('First event attendee')
    first=f.m.EventBooking.query.filter_by(event_id=f.event.id,source='registration_survey').one()
    first_id,first_registration_id=first.id,first.projection_registration_id
    complete('Updated first attendee')
    assert f.m.EventBooking.query.count()==1
    assert f.m.EventBooking.query.one().id==first_id
    assert f.m.EventRegistration.query.one().id==first_registration_id
    assert f.m.EventRegistration.query.one().name=='Updated first attendee'
    flow.linked_event_id=second.id;f.db.session.commit()
    complete('Second event attendee')
    assert f.m.EventBooking.query.count()==2
    first=f.m.EventBooking.query.filter_by(id=first_id).one()
    second_booking=f.m.EventBooking.query.filter_by(event_id=second.id,source='registration_survey').one()
    second_id,second_registration_id=second_booking.id,second_booking.projection_registration_id
    assert first.event_id==f.event.id and first.name=='Updated first attendee'
    assert first.projection_registration_id==first_registration_id
    assert first.external_booking_id!=second_booking.external_booking_id
    complete('Updated second attendee')
    assert f.m.EventBooking.query.count()==2
    assert f.m.EventBooking.query.filter_by(id=second_id).one().name=='Updated second attendee'
    assert f.m.EventRegistration.query.filter_by(event_id=second.id).one().id==second_registration_id
    flow.linked_event_id=f.event.id;f.db.session.commit()
    complete('Returning first attendee')
    assert f.m.EventBooking.query.count()==2
    assert f.m.EventBooking.query.filter_by(id=first_id).one().name=='Returning first attendee'
    assert f.m.EventBooking.query.filter_by(id=second_id).one().name=='Updated second attendee'
    assert f.m.EventRegistration.query.filter_by(event_id=f.event.id).one().id==first_registration_id
    assert all(row.party_size==1 for row in f.m.EventBooking.query.all())
    assert all(session.purpose=='survey' and session.feedback_event_id is None for session in f.m.SurveySession.query.all())


@pytest.mark.parametrize('later_signal',['unchanged','missing',True])
def test_linked_source_event_withdrawal_survives_unlink_and_blocks_ordinary_snapshot(feedback,later_signal):
    from app.services.booking_projection_service import upsert_booking,link_duplicate_booking,unlink_duplicate_booking
    from app.services.recipient_permission_service import recipient_permission_status
    f=feedback
    with f.organization_context(f.org.id):
        primary,secondary=_linked_source_pair(f,secondary_phone='+17205550261')
        phone=secondary.phone
        link_duplicate_booking(secondary,primary)
        grants_before=f.m.RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='event',decision='granted').count()
        data={'booking_id':'secondary','phone':phone,'source_revision':2,'sms_consent':False,
              'community_sms_consent':True,'updated_at':f.m.utc_now().isoformat()}
        upsert_booking(f.event,data,'fixture-bookings',f.m.utc_now(),source_scope='orders:fixture')
        if later_signal!='unchanged':
            data.update(source_revision=3,updated_at=f.m.utc_now().isoformat())
            if later_signal=='missing':data.pop('sms_consent')
            else:data['sms_consent']=later_signal
            upsert_booking(f.event,data,'fixture-bookings',f.m.utc_now(),source_scope='orders:fixture')
        assert f.m.RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='event',decision='granted').count()==grants_before
        unlink_duplicate_booking(secondary);f.db.session.commit()
        _ordinary_event_snapshot(f,phone)
    _run_ordinary_event_scheduler(f)
    assert f.provider.client.messages.create.call_count==0
    assert not recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed
    assert recipient_permission_status(f.org.id,phone,purpose='community').allowed
    negatives=f.m.RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='event',decision='denied').all()
    assert any(json.loads(row.metadata_json or '{}').get('sms_consent') is False and
               json.loads(row.metadata_json or '{}').get('reason') is None for row in negatives)


def test_later_verified_owner_reoptin_can_restore_event_consent_even_when_saved_source_flag_is_false(feedback):
    from app.services.booking_projection_service import upsert_booking,link_duplicate_booking,unlink_duplicate_booking
    from app.services.recipient_permission_service import record_permission_evidence,recipient_permission_status
    f=feedback
    with f.organization_context(f.org.id):
        primary,secondary=_linked_source_pair(f,secondary_phone='+17205550262')
        phone=secondary.phone
        link_duplicate_booking(secondary,primary)
        upsert_booking(f.event,{'booking_id':'secondary','phone':phone,'source_revision':2,'sms_consent':False,
            'community_sms_consent':True,'updated_at':f.m.utc_now().isoformat()},
            'fixture-bookings',f.m.utc_now(),source_scope='orders:fixture')
        assert secondary.sms_consent is False
        record_permission_evidence(f.org.id,phone,purpose='event',event_id=f.event.id,decision='granted',
            source='owner_review',evidence_key='genuine-later-event-reoptin',occurred_at=f.m.utc_now(),verified=True,
            metadata={'proof':'Owner verified a later explicit event-purpose opt-in.'})
        unlink_duplicate_booking(secondary);f.db.session.commit()
        _ordinary_event_snapshot(f,phone)
    _run_ordinary_event_scheduler(f)
    assert f.provider.client.messages.create.call_count==1
    assert recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed


@pytest.mark.parametrize('stop_before_unlink',[False,True])
def test_confirmed_signup_raw_false_on_linked_duplicate_creates_no_event_grant_and_STOP_still_wins(feedback,stop_before_unlink):
    from app.services.booking_projection_service import upsert_booking,link_duplicate_booking,unlink_duplicate_booking
    from app.services.recipient_permission_service import record_permission_evidence,recipient_permission_status
    f=feedback
    with f.organization_context(f.org.id):
        primary,secondary=_linked_source_pair(f,secondary_phone='+17205550263',confirmed_signup=True)
        phone=secondary.phone
        link_duplicate_booking(secondary,primary)
        grants_before=f.m.RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='event',decision='granted').count()
        upsert_booking(f.event,{'booking_id':'secondary','phone':phone,'source_revision':2,'sms_consent':False,
            'community_sms_consent':True,'updated_at':f.m.utc_now().isoformat()},
            'fixture-bookings',f.m.utc_now(),source_scope='orders:fixture')
        assert secondary.sms_consent is False
        assert f.m.RecipientPermissionEvidence.query.filter_by(phone=phone,purpose='event',decision='granted').count()==grants_before
        assert recipient_permission_status(f.org.id,phone,purpose='community').allowed
        if stop_before_unlink:
            record_permission_evidence(f.org.id,phone,purpose='all',decision='denied',source='inbound_stop',
                evidence_key='actual-stop-before-unlink',occurred_at=f.m.utc_now())
        unlink_duplicate_booking(secondary);f.db.session.commit()
        _ordinary_event_snapshot(f,phone)
    _run_ordinary_event_scheduler(f)
    assert f.provider.client.messages.create.call_count==(0 if stop_before_unlink else 1)
    assert recipient_permission_status(f.org.id,phone,purpose='event',event_id=f.event.id).allowed is not stop_before_unlink
