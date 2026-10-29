"""Booking identity/seat acceptance across the signed integration and projection."""
from datetime import datetime, timezone
import importlib
import logging

import pytest

from tests.test_aoc_event_sync import TestAocEventSyncWebhook as _AocFixture


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
