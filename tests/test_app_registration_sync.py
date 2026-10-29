"""Connected applications share the booking contract without tenant/source impersonation."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import uuid

from cryptography.fernet import Fernet
import pytest

import tests.test_aoc_event_sync as _aoc


@pytest.fixture
def case():
    helper = _aoc.TestAocEventSyncWebhook()
    helper.setUp()
    helper.app.config['TWILIO_CREDENTIAL_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
    from app.services.aoc_event_sync_service import configure_event_sync_integration
    helper.integration,helper.app_secret=configure_event_sync_integration(helper.organization.id,True,None,provider='app')
    helper.db.session.commit()
    yield helper
    helper.tearDown()


def post(case,payload,organization_slug=None,secret=None):
    body=json.dumps(payload,separators=(',',':')).encode()
    timestamp=str(int(datetime.now(timezone.utc).timestamp()))
    signature=hmac.new((secret or case.app_secret).encode(),timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
    return case.client.post('/webhooks/event-sync/'+(organization_slug or case.organization.slug)+'/app',data=body,
        headers={'Content-Type':'application/json','X-Twinevia-Timestamp':timestamp,
                 'X-Twinevia-Signature':'sha256='+signature,'X-Twinevia-Delivery-ID':str(uuid.uuid4())})


def payload(case,key='family'):
    return {'protocol_version':2,'source':'commerce','action':'booking_upsert','event':case._event_payload(),
            'booking':{'booking_id':key,'source_scope':'store:42','status':'confirmed','spaces':10,
                       'name':'Family Contact','phone':'+17205550101','sms_consent':True,'source_revision':1,
                       'updated_at':'2026-06-23T18:00:00Z'}}


def test_custom_application_booking_is_namespaced_and_does_not_create_reminders(case):
    from app.models import EventBooking
    from app.services.booking_projection_service import roster_summary
    response=post(case,payload(case))
    assert response.status_code==200,response.get_data(as_text=True)
    booking=EventBooking.query.one()
    assert booking.source==f'app:{case.integration.id}:commerce'
    assert booking.external_booking_id=='family'
    summary=roster_summary(case.Event.query.one())
    assert (summary['bookings'],summary['reserved_places'],summary['reachable_contacts'])==(1,10,1)
    assert case.ScheduledMessage.query.count()==0


def test_application_credentials_cannot_cross_tenants_or_impersonate_internal_sources(case):
    assert post(case,payload(case),organization_slug=case.other_organization.slug).status_code==403
    data=payload(case);data['source']='manual'
    assert post(case,data).status_code==200
    from app.models import EventBooking
    assert EventBooking.query.one().source==f'app:{case.integration.id}:manual'
    data['source']='aoc-wordpress:forged'
    assert post(case,data).status_code==400


def test_two_apps_and_scopes_keep_independent_bookings_at_same_destination(case):
    from app.models import EventBooking
    first=payload(case)
    assert post(case,first).status_code==200
    second=payload(case);second['source']='membership'
    assert post(case,second).status_code==200
    assert EventBooking.query.count()==2
    # These are separate external events until explicitly linked by an operator.
    assert case.Event.query.count()==2
    first['booking']['source_revision']=2;first['booking']['status']='cancelled';first['booking']['active']=False
    assert post(case,first).status_code==200
    assert EventBooking.query.filter_by(is_active=True).count()==1


def test_application_complete_inventory_uses_same_proof_and_requires_explicit_authority(case):
    from app.models import EventBooking,EventRegistrationSource
    from app.services.booking_inventory_service import event_roster_coverage
    data=payload(case)
    assert post(case,data).status_code==200
    encoded='[]'
    manifest={'id':'app-inventory-1','source_scope':'store:42','watermark':'2026-06-24T18:00:00Z','pages':1,'records':0}
    base={'protocol_version':2,'source':'commerce','event':data['event'],'inventory':manifest}
    page={**base,'action':'inventory_page','page':1,'bookings':[],'records_json':encoded}
    complete={**base,'action':'inventory_complete','completion_proof':hashlib.sha256(hashlib.sha256(encoded.encode()).hexdigest().encode()).hexdigest()}
    assert post(case,page).status_code==200
    assert EventBooking.query.one().is_active
    assert post(case,complete).status_code==200
    assert not EventBooking.query.one().is_active
    assert EventRegistrationSource.query.one().coverage_status=='complete'
    assert not event_roster_coverage(case.Event.query.one())['trustworthy']


def test_application_source_requires_version_and_explicit_revision_and_scope(case):
    data=payload(case);data.pop('protocol_version')
    assert post(case,data).status_code==400
    data=payload(case);data['booking'].pop('source_revision')
    assert post(case,data).status_code==400
    data=payload(case);data['booking'].pop('source_scope')
    assert post(case,data).status_code==400


def test_explicit_owner_source_binding_can_link_apps_to_one_event(case):
    from app.models import Event,EventBooking
    from app.services.booking_inventory_service import configure_event_source
    data=payload(case)
    assert post(case,data).status_code==200
    event=case.Event.query.one()
    second=payload(case,'second-booking');second['source']='membership'
    second['event']['target_event_id']=event.id
    assert post(case,second).status_code==400
    configure_event_source(event,f'app:{case.integration.id}:membership','store:42')
    case.db.session.commit()
    assert post(case,second).status_code==200
    assert Event.query.count()==1
    assert EventBooking.query.count()==2
    assert case.EventRegistration.query.count()==1
    event_payload=second['event'].copy();event_payload['target_event_id']=999999
    second['event']=event_payload;second['booking']['source_revision']=2
    assert post(case,second).status_code==400


def test_unversioned_event_mutation_cannot_clear_existing_source_revision(case):
    from app.services.app_registration_sync_service import process_app_registration_payload
    from app.services.aoc_event_sync_service import AocEventSyncPayloadError
    event_data={'protocol_version':2,'source':'crm','action':'event_upsert','event':case._event_payload()}
    assert post(case,event_data).status_code==200
    event_data['event'].pop('modified_at')
    event_data['event']['title']='Older unversioned title'
    with pytest.raises(AocEventSyncPayloadError):
        process_app_registration_payload(event_data,case.integration,datetime.now(timezone.utc))
    assert case.Event.query.one().title!='Older unversioned title'
    assert case.Event.query.one().external_modified_at is not None


def test_malformed_linked_booking_context_returns_controlled_error(case):
    from app.services.booking_inventory_service import configure_event_source
    data=payload(case)
    assert post(case,data).status_code==200
    event=case.Event.query.one()
    configure_event_source(event,f'app:{case.integration.id}:commerce','store:42')
    case.db.session.commit()
    data['event']['target_event_id']=event.id
    data['booking']=['malformed']
    response=post(case,data)
    assert response.status_code==400


@pytest.mark.parametrize('changes',[
    {'active':'false'}, {'sms_consent':'true'}, {'phone':{'phone':'+17205550101'}},
    {'external_booking_id':'conflicting-alias'}, {'status':['confirmed']},
])
def test_invalid_application_booking_types_do_not_create_audience(case,changes):
    from app.models import EventBooking
    data=payload(case);data['booking'].update(changes)
    assert post(case,data).status_code==400
    assert EventBooking.query.count()==0


def test_large_connection_identity_rejects_oversized_source_before_database_write(case):
    from app.models import EventBooking
    case.integration.id = 2147483647
    case.db.session.commit()
    data = payload(case)
    data['source'] = 's' * 40
    response = post(case, data)
    assert response.status_code == 400
    assert case.Event.query.count() == 0 and EventBooking.query.count() == 0
