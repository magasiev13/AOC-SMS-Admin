"""Signed completion proofs, source scopes and snapshot freshness acceptance."""
from datetime import datetime, timezone, timedelta
import hashlib
import json

import pytest

import tests.test_aoc_event_sync as _aoc


@pytest.fixture
def case():
    helper = _aoc.TestAocEventSyncWebhook()
    helper.setUp()
    yield helper
    helper.tearDown()


def send(case, payload):
    response = case._signed_post(payload, None, None)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def booking(case, key, phone, **changes):
    record = case._booking_payload(key, phone)
    record.update(source_scope='events_manager:54', **changes)
    return record


def mutation(case, record):
    return send(case, {'action':'booking_upsert', 'source':'aoc-wordpress',
                       'event':case._event_payload(), 'booking':record})


def inventory(case, records, key='inventory-1', pages=None, watermark='2026-06-23T19:00:00Z'):
    records_pages = pages if pages is not None else [records]
    encoded = [json.dumps(p, separators=(',', ':')) for p in records_pages]
    manifest = {'id':key, 'source_scope':'events_manager:54', 'watermark':watermark,
                'pages':len(records_pages), 'records':sum(map(len, records_pages))}
    base = {'protocol_version':2, 'source':'aoc-wordpress', 'event':case._event_payload(), 'inventory':manifest}
    payloads = [{**base, 'action':'inventory_page', 'page':i+1, 'bookings':p, 'records_json':encoded[i]}
                for i,p in enumerate(records_pages)]
    hashes = [hashlib.sha256(p.encode()).hexdigest() for p in encoded]
    completion = {**base, 'action':'inventory_complete',
                  'completion_proof':hashlib.sha256('\n'.join(hashes).encode()).hexdigest()}
    return payloads, completion


def test_incomplete_inventory_cannot_remove_and_legacy_heartbeat_proves_nothing(case):
    from app.models import EventBooking
    from app.services.booking_inventory_service import event_roster_coverage
    mutation(case, booking(case,'retained','+17205550101'))
    pages, completion = inventory(case, [], pages=[[],[]])
    send(case,pages[0]); send(case,completion)
    assert EventBooking.query.one().is_active
    send(case, {'action':'reconcile_complete','source':'aoc-wordpress'})
    assert not event_roster_coverage(case.Event.query.one())['trustworthy']


def test_complete_inventory_repairs_missed_cancellation_and_preserves_other_contact(case):
    from app.models import EventBooking, BookingInventory, EventRegistrationSource
    from app.services.booking_inventory_service import configure_event_source, event_roster_coverage
    mutation(case, booking(case,'missing','+17205550102'))
    mutation(case, booking(case,'retained','+17205550102',spaces=7))
    pages, completion = inventory(case, [booking(case,'retained','+17205550102',spaces=7)])
    send(case,completion); result=send(case,pages[0])
    assert result['status']=='complete'
    assert BookingInventory.query.one().status=='complete'
    assert not EventBooking.query.filter_by(external_booking_id='events_manager:missing').one().is_active
    assert case.EventRegistration.query.one().phone=='+17205550102'
    event=case.Event.query.one()
    assert not event_roster_coverage(event)['trustworthy']
    configure_event_source(event,'aoc-wordpress','events_manager:54',verified=True)
    assert event_roster_coverage(event)['trustworthy']
    assert not EventRegistrationSource.query.one().verified_by_user_id


def test_newer_mutation_survives_older_complete_snapshot(case):
    from app.models import EventBooking
    mutation(case, booking(case,'newer','+17205550103',source_revision=2))
    pages, completion = inventory(case, [])
    send(case,pages[0])
    mutation(case, booking(case,'newer','+17205550103',source_revision=3,updated_at='2026-06-23T20:00:00Z'))
    send(case,completion)
    assert EventBooking.query.one().is_active
    assert EventBooking.query.one().source_revision==3


def test_page_reuse_or_bad_completion_proof_never_mutates(case):
    from app.models import EventBooking
    mutation(case,booking(case,'retained','+17205550104'))
    pages,completion=inventory(case,[])
    send(case,pages[0])
    completion['completion_proof']='f'*64
    response=case._signed_post(completion,None,None)
    assert response.status_code==400
    assert EventBooking.query.one().is_active


def test_source_scope_isolated_and_past_inventory_does_not_recreate_reminders(case):
    from app.models import EventBooking
    mutation(case,booking(case,'same','+17205550105'))
    other=booking(case,'same','+17205550106')
    other['source_scope']='events_manager:other'
    mutation(case,other)
    pages,completion=inventory(case,[])
    pages[0]['event']['start_at']='2020-01-01T10:00:00-07:00'
    completion['event']['start_at']='2020-01-01T10:00:00-07:00'
    schedules=case.ScheduledMessage.query.count()
    send(case,pages[0]);send(case,completion)
    assert EventBooking.query.filter_by(source_scope='events_manager:other').one().is_active
    assert case.ScheduledMessage.query.count()==schedules


def test_versioned_updates_in_same_second_use_numeric_revision(case):
    from app.models import EventBooking
    mutation(case,booking(case,'revision','+17205550107',source_revision=1,spaces=1))
    mutation(case,booking(case,'revision','+17205550107',source_revision=2,spaces=10))
    assert EventBooking.query.one().party_size==10
    mutation(case,booking(case,'revision','+17205550107',source_revision=1,spaces=1))
    assert EventBooking.query.one().party_size==10


def test_authority_requires_complete_source_and_tenant_scope(case):
    from app.models import Event,EventRegistrationSource
    from app.services.booking_inventory_service import configure_event_source, BookingInventoryError
    from app.tenant import organization_context
    mutation(case,booking(case,'scope','+17205550108'))
    with pytest.raises(BookingInventoryError):
        configure_event_source(case.Event.query.one(),'aoc-wordpress','events_manager:54',verified=True)
    case.db.session.rollback()
    pages,completion=inventory(case,[])
    send(case,pages[0]);send(case,completion)
    with organization_context(case.other_organization.id):
        assert EventRegistrationSource.query.count()==0


def test_newer_interrupted_inventory_holds_authority_after_older_inventory_finishes(case):
    from app.models import EventRegistrationSource
    from app.services.booking_inventory_service import configure_event_source,event_roster_coverage
    initial=booking(case,'retained','+17205550109')
    pages,complete=inventory(case,[initial],key='initial')
    send(case,pages[0]);send(case,complete)
    event=case.Event.query.one()
    configure_event_source(event,'aoc-wordpress','events_manager:54',verified=True)
    old_pages,old_complete=inventory(case,[initial],key='older',watermark='2026-06-24T19:00:00Z')
    newer_pages,newer_complete=inventory(case,[initial],key='newer',pages=[[initial],[]],watermark='2026-06-25T19:00:00Z')
    send(case,old_pages[0]);send(case,newer_pages[0]);send(case,old_complete)
    assert EventRegistrationSource.query.one().coverage_status=='receiving'
    assert not event_roster_coverage(event)['trustworthy']
    send(case,newer_complete)
    assert not event_roster_coverage(event)['trustworthy']
    send(case,newer_pages[1])
    assert event_roster_coverage(event)['trustworthy']


def test_new_unconfigured_booking_source_holds_previously_reviewed_roster(case):
    from app.services.booking_inventory_service import configure_event_source,event_roster_coverage
    initial=booking(case,'one','+17205550110')
    pages,complete=inventory(case,[initial])
    send(case,pages[0]);send(case,complete)
    event=case.Event.query.one()
    configure_event_source(event,'aoc-wordpress','events_manager:54',verified=True)
    assert event_roster_coverage(event)['trustworthy']
    new=booking(case,'other','+17205550111');new['source_scope']='events_manager:other'
    mutation(case,new)
    coverage=event_roster_coverage(event)
    assert not coverage['trustworthy']
    assert coverage['unconfigured_sources']==[{'source':'aoc-wordpress','source_scope':'events_manager:other'}]


def test_explicit_local_review_and_later_local_bookings_keep_current_coverage(case):
    from app.services.booking_inventory_service import review_local_source,event_roster_coverage
    from app.services.booking_projection_service import upsert_booking
    mutation(case,booking(case,'event-seed','+17205550112',active=False,status='cancelled'))
    # A local event can own its entire source roster without an external connector.
    event=case.Event.query.one()
    upsert_booking(event,{'booking_id':'local-1','phone':'+17205550113','spaces':2},'manual',datetime.now(timezone.utc))
    review_local_source(event,'manual')
    assert event_roster_coverage(event)['trustworthy']
    upsert_booking(event,{'booking_id':'local-2','phone':'+17205550114','spaces':3},'manual',datetime.now(timezone.utc))
    assert event_roster_coverage(event)['trustworthy']


def test_distinct_inventories_with_tied_watermarks_hold_without_removing(case):
    from app.models import EventBooking,EventRegistrationSource
    initial=booking(case,'retained','+17205550115')
    mutation(case,initial)
    first_pages,first_complete=inventory(case,[initial],key='tie-first')
    second_pages,second_complete=inventory(case,[],key='tie-second')
    send(case,first_pages[0])
    assert send(case,second_pages[0])['status']=='conflict'
    assert send(case,first_complete)['status']=='conflict'
    assert send(case,second_complete)['status']=='conflict'
    assert EventBooking.query.one().is_active
    assert EventRegistrationSource.query.one().coverage_status=='conflict'
    newer_pages,newer_complete=inventory(case,[],key='tie-resolved',watermark='2026-06-23T19:01:00Z')
    send(case,newer_pages[0]);send(case,newer_complete)
    assert not EventBooking.query.one().is_active
    assert EventRegistrationSource.query.one().coverage_status=='complete'


def test_equal_tick_source_revision_survives_absence_with_coverage_hold(case):
    from app.models import EventBooking,EventRegistrationSource
    record=booking(case,'same-tick','+17205550116',source_revision=7,updated_at='2026-06-23T19:00:00Z')
    mutation(case,record)
    pages,complete=inventory(case,[])
    send(case,pages[0]);send(case,complete)
    assert EventBooking.query.one().is_active
    assert EventRegistrationSource.query.one().coverage_status=='ambiguous'
    newer_pages,newer_complete=inventory(case,[],key='later-tick',watermark='2026-06-23T19:01:00Z')
    send(case,newer_pages[0]);send(case,newer_complete)
    assert not EventBooking.query.one().is_active


def test_inventory_rejects_alias_conflicts_and_impossible_record_count(case):
    from app.models import EventBooking
    initial=booking(case,'retained','+17205550117')
    mutation(case,initial)
    invalid=booking(case,'different','+17205550118');invalid['external_booking_id']='same-alias'
    pages,complete=inventory(case,[invalid])
    send(case,pages[0])
    assert case._signed_post(complete,None,None).status_code==400
    assert EventBooking.query.one().is_active
    pages,complete=inventory(case,[],key='impossible-count')
    pages[0]['inventory']['records']=501
    assert case._signed_post(pages[0],None,None).status_code==400


def test_multipage_inventory_projects_once_with_linear_query_bound(case,monkeypatch):
    from sqlalchemy import event as sql_event
    from unittest.mock import Mock
    from app.services import booking_projection_service as projection
    from app.models import EventBooking
    records=[booking(case,f'group-{index}','+17205550119',spaces=2,source_revision=1) for index in range(125)]
    pages,complete=inventory(case,records,pages=[records[:50],records[50:100],records[100:]],key='large-roster')
    for page in pages:
        send(case,page)
    assert EventBooking.query.count()==0
    projected=Mock(wraps=projection.reconcile_event_projection)
    seeded=Mock(wraps=projection.ensure_legacy_bookings)
    monkeypatch.setattr(projection,'reconcile_event_projection',projected)
    monkeypatch.setattr(projection,'ensure_legacy_bookings',seeded)
    statements=[]
    def record_statement(connection,cursor,statement,parameters,context,executemany):
        statements.append(statement.lower())
    sql_event.listen(case.db.engine,'before_cursor_execute',record_statement)
    try:
        assert send(case,complete)['status']=='complete'
    finally:
        sql_event.remove(case.db.engine,'before_cursor_execute',record_statement)
    assert projected.call_count==1
    assert seeded.call_count==1
    assert len(statements)<125*35+100
    scans=[statement for statement in statements if 'from event_bookings' in statement and 'external_booking_id =' not in statement.split('where')[-1] and 'id =' not in statement.split('where')[-1].replace('organization_id =','').replace('event_id =','')]
    assert len(scans)<10
    assert EventBooking.query.count()==125
    assert case.EventRegistration.query.count()==1
    assert case.EventRegistration.query.one().booking_spaces==250


@pytest.mark.parametrize('reverse_records',[False,True])
def test_complete_inventory_can_swap_two_booking_destinations_without_replacing_contact_ids(case,reverse_records):
    from app.models import EventBooking
    from app.services.booking_projection_service import upsert_booking
    first_phone,second_phone='+17205550231','+17205550232'
    mutation(case,booking(case,'swap-first',first_phone,spaces=3,source_revision=1))
    mutation(case,booking(case,'swap-second',second_phone,spaces=5,source_revision=1))
    event=case.Event.query.one()
    unrelated=upsert_booking(event,{'booking_id':'unrelated-transaction','phone':'+17205550233',
        'name':'Unrelated contact','spaces':4,'sms_consent':True,'updated_at':'2026-06-23T18:10:00Z'},
        'other-application',datetime.now(timezone.utc),source_scope='orders:separate')
    legacy=case.EventRegistration(organization_id=case.organization.id,event_id=event.id,
        phone='+17205550234',name='Legacy evidence',booking_spaces=2)
    case.db.session.add(legacy);case.db.session.commit()
    original_ids={row.phone:row.id for row in case.EventRegistration.query.all()}
    unrelated_identity=(unrelated.id,unrelated.source,unrelated.source_scope,unrelated.external_booking_id,
                        unrelated.projection_registration_id)
    records=[booking(case,'swap-first',second_phone,spaces=3,source_revision=2,updated_at='2026-06-23T19:00:00Z'),
             booking(case,'swap-second',first_phone,spaces=5,source_revision=2,updated_at='2026-06-23T19:00:00Z')]
    if reverse_records:
        records.reverse()
    pages,complete=inventory(case,records,key='swap-roster',watermark='2026-06-23T20:00:00Z')
    send(case,pages[0])
    assert send(case,complete)['status']=='complete'
    case.db.session.expire_all()
    registrations={row.phone:row for row in case.EventRegistration.query.all()}
    assert {phone:row.id for phone,row in registrations.items()}==original_ids
    assert registrations[first_phone].booking_spaces==5 and registrations[second_phone].booking_spaces==3
    first=EventBooking.query.filter_by(external_booking_id='events_manager:swap-first').one()
    second=EventBooking.query.filter_by(external_booking_id='events_manager:swap-second').one()
    assert (first.phone,first.projection_registration_id)==(second_phone,original_ids[second_phone])
    assert (second.phone,second.projection_registration_id)==(first_phone,original_ids[first_phone])
    assert registrations[second_phone].external_booking_id==f'ledger:{first.id}'
    assert registrations[first_phone].external_booking_id==f'ledger:{second.id}'
    assert unrelated_identity==(unrelated.id,unrelated.source,unrelated.source_scope,unrelated.external_booking_id,
                                unrelated.projection_registration_id)
    assert registrations['+17205550233'].booking_spaces==4
    assert registrations['+17205550234'].name=='Legacy evidence' and registrations['+17205550234'].booking_spaces==2
    from app.services.recipient_permission_service import recipient_permission_status
    assert recipient_permission_status(case.organization.id,first_phone,purpose='event',event_id=event.id).allowed
    assert recipient_permission_status(case.organization.id,second_phone,purpose='event',event_id=event.id).allowed


def test_complete_inventory_clear_and_cancellation_keep_explicit_consent_decisions(case):
    from app.models import EventBooking
    from app.services.recipient_permission_service import recipient_permission_status
    case.app.config['COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS']=[]
    phone='+17205550235'
    mutation(case,booking(case,'clear-contact',phone,spaces=3,source_revision=1,community_sms_consent=True))
    mutation(case,booking(case,'retained-contact',phone,spaces=5,source_revision=1,community_sms_consent=True))
    event=case.Event.query.one()
    records=[booking(case,'clear-contact','',spaces=3,source_revision=2,community_sms_consent=False,
                     updated_at='2026-06-23T19:00:00Z'),
             booking(case,'retained-contact',phone,spaces=5,source_revision=2,community_sms_consent=True,
                     updated_at='2026-06-23T19:00:00Z')]
    pages,complete=inventory(case,records,key='clear-roster',watermark='2026-06-23T20:00:00Z')
    send(case,pages[0]);assert send(case,complete)['status']=='complete'
    assert case.EventRegistration.query.one().phone==phone
    assert case.EventRegistration.query.one().booking_spaces==5
    assert recipient_permission_status(case.organization.id,phone,purpose='event',event_id=event.id).allowed
    assert not recipient_permission_status(case.organization.id,phone,purpose='community').allowed

    # A later complete roster omits the final contactable booking, preserving
    # the explicit community withdrawal while removing its event contribution.
    records[0].update(source_revision=3,updated_at='2026-06-23T20:00:00Z')
    pages,complete=inventory(case,records[:1],key='cancel-roster',watermark='2026-06-23T21:00:00Z')
    send(case,pages[0]);assert send(case,complete)['status']=='complete'
    assert case.EventRegistration.query.count()==0
    assert not EventBooking.query.filter_by(external_booking_id='events_manager:retained-contact').one().is_active
    assert not recipient_permission_status(case.organization.id,phone,purpose='event',event_id=event.id).allowed
    assert not recipient_permission_status(case.organization.id,phone,purpose='community').allowed


def test_old_unscoped_wordpress_callback_is_recovered_by_later_scoped_inventory(case):
    from app.models import EventBooking
    old=case._booking_payload('old-then-scoped','+17205550238')
    send(case,{'action':'booking_upsert','source':'aoc-wordpress','event':case._event_payload(),'booking':old})
    retained=EventBooking.query.one()
    original_id=retained.id
    projection_id=case.EventRegistration.query.one().id
    assert not retained.legacy_evidence and retained.source_scope=='' and retained.source_revision is None
    record=booking(case,'old-then-scoped','+17205550238',source_revision=2,spaces=7,
                   updated_at='2026-06-23T19:00:00Z')
    pages,complete=inventory(case,[record],key='recover-old-callback',watermark='2026-06-23T20:00:00Z')
    send(case,pages[0]);assert send(case,complete)['status']=='complete'
    assert EventBooking.query.count()==1
    retained=EventBooking.query.one()
    assert retained.id==original_id and retained.source_scope=='events_manager:54' and retained.source_revision==2
    assert retained.party_size==7 and case.EventRegistration.query.one().booking_spaces==7
    assert case.EventRegistration.query.one().id==projection_id
    # A delayed old integration callback cannot remove its established scope or
    # downgrade a numeric source revision, even with a later replay-time stamp.
    old.update(phone='+17205550239',updated_at='2026-06-23T21:00:00Z')
    send(case,{'action':'booking_upsert','source':'aoc-wordpress','event':case._event_payload(),'booking':old})
    assert EventBooking.query.count()==1
    assert EventBooking.query.one().source_scope=='events_manager:54'
    assert EventBooking.query.one().phone=='+17205550238'


def test_older_inventory_does_not_adopt_or_replace_unscoped_wordpress_state(case):
    from app.models import EventBooking
    old=case._booking_payload('old-newer-than-snapshot','+17205550240')
    old.update(updated_at='2026-06-23T19:00:00Z',spaces=7)
    send(case,{'action':'booking_upsert','source':'aoc-wordpress','event':case._event_payload(),'booking':old})
    original=EventBooking.query.one().id
    stale=booking(case,'old-newer-than-snapshot','+17205550241',source_revision=2,spaces=1,
                  updated_at='2026-06-23T18:30:00Z')
    pages,complete=inventory(case,[stale],key='stale-adoption',watermark='2026-06-23T20:00:00Z')
    send(case,pages[0]);send(case,complete)
    retained=EventBooking.query.one()
    assert (retained.id,retained.source_scope,retained.source_revision,retained.phone,retained.party_size)==(
        original,'',None,'+17205550240',7)


def test_scoped_wordpress_inventory_cannot_move_an_unscoped_identity_to_another_event(case):
    from app.models import EventBooking
    old=case._booking_payload('old-bound-event','+17205550242')
    send(case,{'action':'booking_upsert','source':'aoc-wordpress','event':case._event_payload(),'booking':old})
    original=EventBooking.query.one()
    original_event_id=original.event_id
    moved=booking(case,'old-bound-event','+17205550242',source_revision=2,updated_at='2026-06-23T19:00:00Z')
    moved['source_scope']='events_manager:other-event'
    pages,complete=inventory(case,[moved],key='wrong-event',watermark='2026-06-23T20:00:00Z')
    for item in [*pages,complete]:
        item['inventory']['source_scope']='events_manager:other-event'
        item['event']={**item['event'],'event_id':'other-event'}
    send(case,pages[0])
    assert case._signed_post(complete,None,None).status_code==400
    assert EventBooking.query.count()==1
    assert (EventBooking.query.one().event_id,EventBooking.query.one().source_scope)==(original_event_id,'')


def coverage_fixture(case):
    """A reviewed source can own an empty event; no synthetic roster import."""
    from app.models import EventRegistrationSource
    event=case.Event(organization_id=case.organization.id,title='Coverage evidence')
    case.db.session.add(event);case.db.session.flush()
    when=datetime(2026,6,23,19)
    source=EventRegistrationSource(organization_id=case.organization.id,event_id=event.id,
        source='application',source_scope='form:one',authority_enabled=True,
        coverage_status='complete',last_complete_at=when)
    case.db.session.add(source);case.db.session.flush()
    return event,source


def coverage_booking(case,event,key,**changes):
    from app.models import EventBooking
    values={'organization_id':event.organization_id,'event_id':event.id,
        'source':'application','source_scope':'form:one','external_booking_id':key,
        'is_active':False,'legacy_evidence':False}
    values.update(changes)
    row=EventBooking(**values)
    case.db.session.add(row);case.db.session.flush()
    return row


@pytest.mark.parametrize('target_state,unresolved',[
    ('primary',0),('cancelled_primary',0),('missing',1),('legacy',2),
    ('duplicate',1),('other_event',1),('other_organization',1),
])
def test_coverage_legacy_resolution_requires_same_event_primary_identity(case,target_state,unresolved):
    from app.services.booking_inventory_service import event_roster_coverage
    event,source=coverage_fixture(case)
    target_event=event
    if target_state in {'other_event','other_organization'}:
        target_event=case.Event(organization_id=(case.other_organization.id if target_state=='other_organization'
                                               else case.organization.id),title='Other identity context')
        case.db.session.add(target_event);case.db.session.flush()
    primary=None
    if target_state!='missing':
        primary=coverage_booking(case,target_event,'primary',is_active=target_state=='primary',
            legacy_evidence=target_state=='legacy')
        if target_state=='duplicate':
            root=coverage_booking(case,event,'primary-of-primary')
            primary.duplicate_of_id=root.id
    # Cancelled unresolved history still matters; a cancelled primary can
    # nevertheless prove that two historical identities represent one booking.
    coverage_booking(case,event,'historical',legacy_evidence=True,
        duplicate_of_id=primary.id if primary else None)
    result=event_roster_coverage(event)
    assert result['legacy_evidence_count']==unresolved
    assert result['trustworthy']==(unresolved==0)
    assert bool(result['reason'] and 'Historical' in result['reason'])==(unresolved>0)


def test_coverage_untracked_registration_uses_any_same_event_retained_link(case):
    from app.services.booking_inventory_service import event_roster_coverage
    event,source=coverage_fixture(case)
    registrations=[case.EventRegistration(organization_id=event.organization_id,event_id=event.id,
        phone=f'+172055504{index:02d}') for index in range(3)]
    case.db.session.add_all(registrations);case.db.session.flush()
    primary=coverage_booking(case,event,'inactive-link',projection_registration_id=registrations[0].id)
    coverage_booking(case,event,'duplicate-link',duplicate_of_id=primary.id,
        projection_registration_id=registrations[1].id)
    coverage_booking(case,event,'null-link')
    other=case.Event(organization_id=event.organization_id,title='Unrelated event')
    case.db.session.add(other);case.db.session.flush()
    coverage_booking(case,other,'wrong-event-link',projection_registration_id=registrations[2].id)
    result=event_roster_coverage(event)
    assert result['legacy_evidence_count']==1 and not result['trustworthy']
    assert 'Historical' in result['reason']
    coverage_booking(case,event,'final-retained-link',projection_registration_id=registrations[2].id)
    assert event_roster_coverage(event)['trustworthy']


def test_coverage_missing_scopes_include_noncontactable_active_primary_only(case):
    from app.services.booking_inventory_service import event_roster_coverage
    event,source=coverage_fixture(case)
    primary=coverage_booking(case,event,'primary',is_active=True)
    coverage_booking(case,event,'invalid',is_active=True,source_scope='form:invalid',
        phone=None,contact_state='invalid',party_size=0)
    coverage_booking(case,event,'same-scope',is_active=True,source_scope='form:invalid')
    coverage_booking(case,event,'missing',is_active=True,source_scope='form:missing',contact_state='missing')
    coverage_booking(case,event,'inactive',source_scope='form:inactive')
    coverage_booking(case,event,'duplicate',is_active=True,source_scope='form:duplicate',duplicate_of_id=primary.id)
    result=event_roster_coverage(event)
    assert result['unconfigured_source_count']==2
    assert result['unconfigured_sources']==[
        {'source':'application','source_scope':'form:invalid'},
        {'source':'application','source_scope':'form:missing'},
    ]
    assert 'unconfigured' in result['reason'] and not result['trustworthy']


@pytest.mark.parametrize('status,authority,reason',[
    ('complete',True,None),('complete',False,'explicitly approve'),
    ('unverified',True,'explicitly approve'),('receiving',True,'explicitly approve'),
    ('conflict',True,'share a watermark'),('ambiguous',True,'changed at the inventory watermark'),
])
def test_coverage_requires_every_configured_source_even_when_empty(case,status,authority,reason):
    from app.models import EventRegistrationSource
    from app.services.booking_inventory_service import event_roster_coverage
    event,source=coverage_fixture(case)
    later=source.last_complete_at+timedelta(hours=1)
    second=EventRegistrationSource(organization_id=event.organization_id,event_id=event.id,
        source='another-application',source_scope='form:empty',coverage_status=status,
        authority_enabled=authority,last_complete_at=later)
    case.db.session.add(second);case.db.session.flush()
    result=event_roster_coverage(event)
    assert result['trustworthy']==(reason is None)
    assert result['last_complete_at']==source.last_complete_at
    if reason:assert reason in result['reason']
    second.last_complete_at=None
    assert event_roster_coverage(event)['last_complete_at'] is None
    case.db.session.delete(source);case.db.session.delete(second);case.db.session.flush()
    result=event_roster_coverage(event)
    assert not result['trustworthy'] and result['last_complete_at'] is None


def test_coverage_reason_priority_and_fresh_source_state_between_guards(case):
    from app.models import EventRegistrationSource,EventBooking
    from app.services.booking_inventory_service import event_roster_coverage
    event,source=coverage_fixture(case)
    case.db.session.commit()
    assert event_roster_coverage(event)['trustworthy']
    # A separate committed mutation must defeat even an already-loaded source
    # identity in the worker's ORM session. No cached approval is sufficient.
    source_id,event_id,organization_id=source.id,event.id,event.organization_id
    with case.db.engine.begin() as connection:
        connection.execute(EventRegistrationSource.__table__.update().where(
            EventRegistrationSource.id==source_id).values(coverage_status='conflict'))
    assert source.coverage_status=='complete'
    assert 'share a watermark' in event_roster_coverage(event)['reason']
    with case.db.engine.begin() as connection:
        connection.execute(EventBooking.__table__.insert().values(organization_id=organization_id,
            event_id=event_id,source='new-app',source_scope='form:new',external_booking_id='late',is_active=True))
    assert 'unconfigured' in event_roster_coverage(event)['reason']
    coverage_booking(case,event,'unresolved',legacy_evidence=True)
    assert 'Historical' in event_roster_coverage(event)['reason']
    source.coverage_status='ambiguous';case.db.session.flush()
    assert 'Historical' in event_roster_coverage(event)['reason']
