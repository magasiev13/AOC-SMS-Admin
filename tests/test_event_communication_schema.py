"""Preserve schema-028 evidence and enforce the schema-031 recovery boundary."""
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest
from sqlalchemy import inspect, text

from tests.test_booking_projection import case
from tests.test_runtime_maintenance import runtime, _release, _switch, _state, REPOSITORY


NEW_TABLES = ('feedback_invitations', 'booking_inventory_pages', 'booking_inventories',
              'event_registration_sources', 'event_bookings')
MIGRATIONS = ('029_event_booking_ledger', '030_booking_inventories', '031_feedback_invitations')


def _table_hashes(connection, tables):
    result = {}
    for table in tables:
        rows = connection.execute(text(f'SELECT * FROM {table} ORDER BY id')).mappings().all()
        encoded = json.dumps([dict(row) for row in rows], sort_keys=True, default=str).encode()
        result[table] = hashlib.sha256(encoded).hexdigest()
    return result


def _seed_prior_evidence(case):
    from app.models import (Event, EventRegistration, CommunityMember, InboxThread, MessageLog,
                            ScheduledMessage, SurveyFlow, SurveySession, SurveyResponse)
    event = Event(organization_id=case.organization.id,title='Retained prior event',
                  external_source='aoc-wordpress',external_event_id='retained-54')
    case.db.session.add(event); case.db.session.flush()
    registration = EventRegistration(organization_id=case.organization.id,event_id=event.id,
        name='Original booking contact',phone='+17205550201',booking_spaces=10,
        external_source='aoc-wordpress',external_booking_id='events_manager:retained-order',
        external_booking_status='paid',external_updated_at=datetime(2026,10,20,12))
    member = CommunityMember(organization_id=case.organization.id,name='Retained community',phone='+17205550202')
    thread = InboxThread(organization_id=case.organization.id,phone='+17205550201',contact_name='Original booking contact')
    flow = SurveyFlow(organization_id=case.organization.id,name='Original feedback',trigger_keyword='RETAINED REVIEW',
        purpose='event_feedback',feedback_event_id=event.id,questions_json='["Original question?"]')
    case.db.session.add_all([registration,member,thread,flow]); case.db.session.flush()
    session = SurveySession(organization_id=case.organization.id,survey_id=flow.id,thread_id=thread.id,
        phone=registration.phone,status='completed',purpose='event_feedback',feedback_event_id=event.id,
        feedback_event_title=event.title,feedback_questions_json='["Original question?"]',
        feedback_completion_message='Original completion')
    case.db.session.add(session); case.db.session.flush()
    case.db.session.add(SurveyResponse(organization_id=case.organization.id,session_id=session.id,
        survey_id=flow.id,phone=registration.phone,question_index=0,question_prompt='Original question?',answer='Original answer'))
    case.db.session.add(MessageLog(organization_id=case.organization.id,message_body='Prior accepted message',
        target='community',status='ambiguous',total_recipients=2,success_count=1,failure_count=0,
        details='[{"sid":"SM-retained-evidence","success":true},{"status":"ambiguous"}]'))
    case.db.session.add(ScheduledMessage(organization_id=case.organization.id,message_body='Existing future reminder',
        target='community',status='pending',scheduled_at=datetime(2027,1,1,12)))
    case.db.session.commit()
    return event.id, registration.id


def test_schema028_upgrade_replay_preserves_all_old_rows_and_creates_no_authority_or_sends(case):
    from app.saas_migrations.runner import _ensure_migrations_table, _migration_files, run_pending_saas_migrations
    from app.models import EventBooking, EventRegistrationSource, FeedbackInvitation
    event_id, registration_id = _seed_prior_evidence(case)
    case.db.session.remove()
    with case.db.engine.begin() as connection:
        for table in NEW_TABLES:
            case.db.metadata.tables[table].drop(connection, checkfirst=True)
        _ensure_migrations_table(connection)
        for migration in _migration_files():
            if migration.version <= '028':
                connection.execute(text('INSERT INTO saas_schema_migrations (version,applied_at) VALUES (:v,:at)'),
                                   {'v':migration.version,'at':'2026-10-28T00:00:00Z'})
        old_tables = sorted(set(inspect(connection).get_table_names()) - set(NEW_TABLES) - {'saas_schema_migrations'})
        before = _table_hashes(connection, old_tables)
    assert run_pending_saas_migrations(case.db.engine,case.app.logger) == ['029','030','031']
    assert run_pending_saas_migrations(case.db.engine,case.app.logger) == []
    with case.db.engine.begin() as connection:
        for _ in range(2):
            for name in MIGRATIONS:
                importlib.import_module('app.saas_migrations.'+name).apply(connection,case.app.logger)
        assert _table_hashes(connection,old_tables) == before
    booking = EventBooking.query.one()
    assert (booking.event_id,booking.projection_registration_id,booking.party_size) == (event_id,registration_id,10)
    assert booking.legacy_evidence is True
    assert EventRegistrationSource.query.count() == 0 and FeedbackInvitation.query.count() == 0
    from app.services.booking_inventory_service import event_roster_coverage
    assert event_roster_coverage(case.db.session.get(case.Event,event_id))['trustworthy'] is False


def test_first_replay_cannot_overwrite_newer_legacy_evidence_or_adopt_its_scope(case):
    from app.models import EventBooking
    from app.services.booking_projection_service import upsert_booking
    event_id, registration_id = _seed_prior_evidence(case)
    event = case.db.session.get(case.Event,event_id)
    booking = upsert_booking(event,{'booking_id':'retained-order','phone':'+17205550299',
        'spaces':1,'name':'Stale replay','updated_at':'2026-10-19T12:00:00Z'},
        'aoc-wordpress',datetime.now(timezone.utc),source_scope='events_manager:retained-54')
    assert booking._ledger_applied is False and booking.legacy_evidence is True
    assert booking.source_scope == ''
    case.db.session.commit()
    retained = case.db.session.get(case.EventRegistration,registration_id)
    assert retained.name == 'Original booking contact' and retained.booking_spaces == 10
    assert retained.phone == '+17205550201' and EventBooking.query.count() == 1


def test_source_rule_configuration_and_migration_replay_keep_one_reviewed_rule(case):
    from app.models import EventRegistrationSource
    from app.services.booking_inventory_service import configure_event_source
    event_id,_ = _seed_prior_evidence(case)
    event=case.db.session.get(case.Event,event_id)
    first=configure_event_source(event,'app:business-crm','orders:conference')
    second=configure_event_source(event,'app:business-crm','orders:conference')
    assert first.id == second.id
    first.last_complete_at=datetime(2026,10,25,12)
    first.last_watermark=datetime(2026,10,25,11)
    first.last_inventory_id='retained-complete-inventory'
    first.coverage_status='complete'
    case.db.session.flush()
    configure_event_source(event,'app:business-crm','orders:conference',verified=True)
    case.db.session.commit()
    with case.db.engine.begin() as connection:
        before=_table_hashes(connection,['event_registration_sources'])
        for _ in range(2):
            for name in MIGRATIONS:
                importlib.import_module('app.saas_migrations.'+name).apply(connection,case.app.logger)
        assert _table_hashes(connection,['event_registration_sources']) == before
    assert EventRegistrationSource.query.count() == 1
    assert EventRegistrationSource.query.one().authority_enabled is True


def test_inventory_absence_tombstone_survives_higher_numeric_but_older_source_time(case):
    from app.services.booking_projection_service import upsert_booking,cancel_booking
    from app.models import EventBooking
    event_id,_ = _seed_prior_evidence(case)
    event=case.db.session.get(case.Event,event_id)
    payload={'booking_id':'live-order','phone':'+17205550203','spaces':4,'source_revision':1,
             'updated_at':'2026-10-25T18:00:00Z','sms_consent':True}
    booking=upsert_booking(event,payload,'app:crm',datetime.now(timezone.utc),source_scope='orders:conference')
    assert cancel_booking(booking,datetime.now(timezone.utc),source_updated_at=datetime(2026,10,25,19),
                          reason='complete_inventory_missing')
    # A higher per-record revision may still have preceded the complete inventory.
    stale={**payload,'source_revision':2,'updated_at':'2026-10-25T18:30:00Z'}
    upsert_booking(event,stale,'app:crm',datetime.now(timezone.utc),source_scope='orders:conference')
    assert booking.is_active is False
    # A delayed cancelled update must not erase the retained absence watermark.
    upsert_booking(event,{**stale,'active':False,'status':'cancelled'},'app:crm',datetime.now(timezone.utc),source_scope='orders:conference')
    upsert_booking(event,{**payload,'source_revision':3,'updated_at':'2026-10-25T18:45:00Z'},
                   'app:crm',datetime.now(timezone.utc),source_scope='orders:conference')
    assert booking.is_active is False
    # Equal timestamps still do not prove a transaction occurred after absence.
    upsert_booking(event,{**payload,'source_revision':4,'updated_at':'2026-10-25T19:00:00Z'},
                   'app:crm',datetime.now(timezone.utc),source_scope='orders:conference')
    assert booking.is_active is False
    upsert_booking(event,{**payload,'source_revision':5,'updated_at':'2026-10-25T19:00:01Z'},
                   'app:crm',datetime.now(timezone.utc),source_scope='orders:conference')
    assert booking.is_active is True
    assert EventBooking.query.filter_by(source='app:crm').count() == 1


def _expanded_release(runtime, name, *, schema='031', missing_feature=None):
    # Start from the historical fixture without changing its historical helpers.
    base, _ = _release(runtime.root,name+'-base',schema='028')
    for migration in MIGRATIONS:
        if migration[:3] <= schema:
            relative = 'app/saas_migrations/'+migration+'.py'
            shutil.copyfile(REPOSITORY/relative,runtime.root/relative)
    contract_path = runtime.root/'deploy/runtime-contract.json'
    contract = json.loads((REPOSITORY/'deploy/runtime-contract.json').read_text())
    contract.update(schema_min=schema,schema_max=schema)
    if missing_feature:
        contract.pop(missing_feature,None)
    contract_path.write_text(json.dumps(contract,sort_keys=True)+'\n')
    subprocess.run(['git','-C',str(runtime.root),'add','app/saas_migrations','deploy/runtime-contract.json'],check=True)
    subprocess.run(['git','-C',str(runtime.root),'-c','user.name=Schema fixture','-c','user.email=schema@fixture.test',
        '-c','commit.gpgsign=false','commit','--quiet','-m',name],check=True)
    sha = subprocess.run(['git','-C',str(runtime.root),'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
    archive = subprocess.run(['git','-C',str(runtime.root),'archive','--format=tar',sha],capture_output=True,check=True).stdout
    target=runtime.root/'releases'/name
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(target,filter='data')
    (target/'release.json').write_text(json.dumps({'release_id':name,'source_sha':sha}))
    (target/'.release.env').write_text(f'APP_RELEASE_ID={name}\nAPP_RELEASE_SHA={sha}\n')
    return target,sha


@pytest.mark.parametrize('missing_feature', ['booking_projection_isolation','complete_inventory_authority',
                                           'explicit_feedback_invitation_authority'])
def test_schema031_recovery_requires_all_event_communication_guards(runtime,missing_feature):
    target,_ = _expanded_release(runtime,'missing-'+missing_feature,missing_feature=missing_feature)
    with pytest.raises(runtime.module.RuntimeControlError,match='authority guards'):
        runtime.controller.verify_target(target)
    assert _state(runtime)['calls'] == [] and _state(runtime)['provider_calls'] == []


def test_schema031_rejects_standalone_rollback_to_schema028_and_accepts_matching_source(runtime):
    modern,sha = _expanded_release(runtime,'reviewed-schema031')
    _switch(runtime,modern)
    assert runtime.controller.verify_target(modern)['source_sha'] == sha
    with pytest.raises(runtime.module.RuntimeControlError,match='expanded schema'):
        runtime.controller.verify_target(runtime.original)
    assert _state(runtime)['calls'] == [] and _state(runtime)['provider_calls'] == []


def test_historical_first_promotion_exceptions_do_not_waive_schema031_guard(runtime):
    snapshot=runtime.controller.capture(kind='deploy')
    modern,_ = _expanded_release(runtime,'captured-schema031')
    _switch(runtime,modern)
    calls=list(_state(runtime)['calls'])
    with pytest.raises(runtime.module.RuntimeControlError,match='expanded schema'):
        runtime.controller.verify_target(runtime.original,transition_snapshot=snapshot)
    assert _state(runtime)['calls'] == calls and _state(runtime)['provider_calls'] == []


def test_runtime_contract_and_schema_migration_tip_agree():
    from app.saas_migrations.runner import _migration_files
    contract=json.loads((REPOSITORY/'deploy/runtime-contract.json').read_text())
    assert contract['schema_min'] == contract['schema_max'] == _migration_files()[-1].version == '031'
    assert all(contract[name] is True for name in ('booking_projection_isolation','complete_inventory_authority',
                                                  'explicit_feedback_invitation_authority'))


def test_real_systemd_fixture_archives_the_declared_schema_tip(tmp_path):
    spec = importlib.util.spec_from_file_location('owned_systemd_schema_fixture', REPOSITORY/'run/test_runtime_systemd.py')
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    fixture = object.__new__(harness.OwnedSystemdFixture)
    fixture.root = tmp_path/'owned-source'
    fixture.root.mkdir()
    subprocess.run(['git', '-C', str(fixture.root), 'init', '--quiet'], check=True)
    release = fixture.build_release('schema031')
    path = release['path']
    contract = json.loads((path/'deploy/runtime-contract.json').read_text())
    versions = sorted(source.name[:3] for source in (path/'app/saas_migrations').glob('[0-9][0-9][0-9]_*.py'))
    assert versions[-1] == contract['schema_max'] == '031'
    assert {'029','030','031'} <= set(versions)
