"""Retained PR findings: security evidence, fixture state and failed-query recovery."""
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, text

from tests.test_daily_audiences import daily


LOGGER = logging.getLogger(__name__)


@pytest.fixture
def old_auth(tmp_path, monkeypatch):
    from app.migrations import runner
    migration = next(item for item in runner._migration_files() if item.name == '010_add_auth_hardening_tables_and_columns')
    original_hash = hashlib.sha256(migration.path.read_bytes()).hexdigest()
    monkeypatch.setattr(runner, '_migration_files', lambda: [migration])
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy-auth.db'))
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
        connection.execute(text('''CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY, client_ip VARCHAR(45), username VARCHAR(80),
            attempt_count INTEGER NOT NULL, first_attempt_at DATETIME NOT NULL,
            locked_until DATETIME, extra_evidence BLOB)'''))
    yield SimpleNamespace(engine=engine, runner=runner, migration=migration, original_hash=original_hash)
    assert hashlib.sha256(migration.path.read_bytes()).hexdigest() == original_hash
    engine.dispose()


def _insert_attempt(connection, identifier, *, ip='192.0.2.1', username='owner', count=1,
                    first='2026-10-27 10:00:00', lock=None):
    connection.execute(text('''INSERT INTO login_attempts
        (id, client_ip, username, attempt_count, first_attempt_at, locked_until, extra_evidence)
        VALUES (:id, :ip, :username, :count, :first, :lock, :extra)'''),
        {'id':identifier, 'ip':ip, 'username':username, 'count':count,
         'first':first, 'lock':lock, 'extra':b'original-history-' + str(identifier).encode()})


def test_pending_010_merges_conservatively_and_archives_every_original_field(old_auth):
    with old_auth.engine.begin() as connection:
        _insert_attempt(connection, 9, count=2, first='2026-10-27 10:00:00', lock='2026-10-27 12:30:00')
        _insert_attempt(connection, 3, count=4, first='2026-10-27 11:00:00', lock='2026-10-27 12:00:00')
        _insert_attempt(connection, 10, username='other', count=3)
    assert old_auth.runner.run_pending_migrations(old_auth.engine, LOGGER) == ['010']
    with old_auth.engine.connect() as connection:
        rows = connection.execute(text('SELECT * FROM login_attempts ORDER BY id')).mappings().all()
        assert len(rows) == 2
        merged = rows[0]
        assert (merged['id'], merged['attempt_count']) == (3, 6)
        assert merged['first_attempt_at'] == '2026-10-27 11:00:00'
        assert merged['locked_until'] == '2026-10-27 12:30:00'
        evidence = connection.execute(text('SELECT * FROM legacy_login_attempt_merge_evidence ORDER BY source_attempt_id')).mappings().all()
        assert [row['source_attempt_id'] for row in evidence] == [3, 9]
        original = [json.loads(row['source_record_json']) for row in evidence]
        assert [row['attempt_count'] for row in original] == [4, 2]
        assert [row['first_attempt_at'] for row in original] == ['2026-10-27 11:00:00', '2026-10-27 10:00:00']
        assert all(row['retained_attempt_id'] == 3 for row in evidence)
        assert original[0]['extra_evidence'] == {'sqlite_blob_base64':'b3JpZ2luYWwtaGlzdG9yeS0z'}
        assert connection.execute(text('SELECT COUNT(*) FROM auth_events')).scalar_one() == 0
    assert old_auth.runner.run_pending_migrations(old_auth.engine, LOGGER) == []
    with old_auth.engine.begin() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM legacy_login_attempt_merge_evidence')).scalar_one() == 2
        # Applied 010 is immutable: a later manually damaged schema must not
        # trigger an automatic merge merely because the runner is invoked.
        connection.execute(text('DROP INDEX ux_login_attempts_client_ip_username'))
        _insert_attempt(connection, 11, count=5)
    assert old_auth.runner.run_pending_migrations(old_auth.engine, LOGGER) == []
    with old_auth.engine.connect() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM login_attempts')).scalar_one() == 3
        assert connection.execute(text('SELECT COUNT(*) FROM legacy_login_attempt_merge_evidence')).scalar_one() == 2


@pytest.mark.parametrize('has_username', [True, False])
def test_pending_010_preflight_matches_null_or_missing_username_normalization(old_auth, has_username):
    with old_auth.engine.begin() as connection:
        _insert_attempt(connection, 1, username=None, count=2)
        _insert_attempt(connection, 2, username='', count=3)
        if not has_username:
            connection.execute(text('ALTER TABLE login_attempts DROP COLUMN username'))
    old_auth.runner.run_pending_migrations(old_auth.engine, LOGGER)
    with old_auth.engine.connect() as connection:
        row = connection.execute(text('SELECT * FROM login_attempts')).mappings().one()
        assert (row['id'], row['username'], row['attempt_count']) == (1, '', 5)
        snapshots = [json.loads(value) for value in connection.execute(text('SELECT source_record_json FROM legacy_login_attempt_merge_evidence')).scalars()]
        assert len(snapshots) == 2
        assert all(('username' in item) == has_username for item in snapshots)


@pytest.mark.parametrize('invalid_field,value', [('count', -1), ('count', 'invalid'), ('first', 'not-a-time'), ('lock', 'not-a-time')])
def test_invalid_legacy_evidence_rolls_back_all_merges_and_leaves_010_pending(old_auth, invalid_field, value):
    with old_auth.engine.begin() as connection:
        _insert_attempt(connection, 1)
        _insert_attempt(connection, 2)
        _insert_attempt(connection, 3, ip='192.0.2.2')
        _insert_attempt(connection, 4, ip='192.0.2.2', **{invalid_field:value})
        before = connection.execute(text('SELECT * FROM login_attempts ORDER BY id')).all()
    with pytest.raises(RuntimeError, match='review'):
        old_auth.runner.run_pending_migrations(old_auth.engine, LOGGER)
    with old_auth.engine.connect() as connection:
        assert connection.execute(text('SELECT * FROM login_attempts ORDER BY id')).all() == before
        assert connection.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('schema_migrations', 'legacy_login_attempt_merge_evidence')")).scalar_one() == 0


def test_010_failure_after_preflight_rolls_back_archive_and_original_records(old_auth, monkeypatch):
    with old_auth.engine.begin() as connection:
        _insert_attempt(connection, 1)
        _insert_attempt(connection, 2)
        before = connection.execute(text('SELECT * FROM login_attempts ORDER BY id')).all()
    def fail_after_merge(connection, logger):
        assert connection.execute(text('SELECT COUNT(*) FROM login_attempts')).scalar_one() == 1
        raise RuntimeError('synthetic later migration failure')
    monkeypatch.setattr(old_auth.runner, '_load_migration', lambda migration: SimpleNamespace(apply=fail_after_merge))
    with pytest.raises(RuntimeError, match='synthetic later'):
        old_auth.runner.run_pending_migrations(old_auth.engine, LOGGER)
    with old_auth.engine.connect() as connection:
        assert connection.execute(text('SELECT * FROM login_attempts ORDER BY id')).all() == before


@pytest.mark.parametrize('module_name,class_name', [
    ('tests.test_inbox_routes', 'TestInboxRoutes'),
    ('tests.test_keyword_conflicts', 'TestKeywordCrossTableConflicts'),
])
def test_old_fixture_restores_existing_database_and_imported_config(tmp_path, module_name, class_name):
    sentinel = 'sqlite:///' + str(tmp_path / 'caller-owned-sentinel.db')
    code = '''import importlib, os
module_name, class_name = __import__('sys').argv[1:]
import app.config
importlib.reload(app.config)
expected = os.environ['DATABASE_URL']
fixture = getattr(importlib.import_module(module_name), class_name)()
fixture.setUp()
try:
    assert os.environ['DATABASE_URL'] != expected
finally:
    fixture.tearDown()
assert os.environ['DATABASE_URL'] == expected
assert app.config.Config.SQLALCHEMY_DATABASE_URI == expected
assert os.environ['FLASK_DEBUG'] == '0'
print('Caller environment and imported configuration restored.')
'''
    result = subprocess.run([sys.executable, '-c', code, module_name, class_name],
        env=dict(os.environ, DATABASE_URL=sentinel, FLASK_DEBUG='0', SAAS_MODE='0',
                 SCHEDULER_ENABLED='0', SECRET_KEY='isolated-fixture-sentinel', PYTHON_DOTENV_DISABLED='1'),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / 'caller-owned-sentinel.db').exists()


def test_dashboard_chart_connection_loss_rolls_back_before_later_postgres_queries(daily):
    if not daily.root:
        pytest.skip('Requires explicit owned AUDIENCE_TEST_POSTGRES_ROOT for real PostgreSQL connection failure.')
    triggered = []
    def terminate_owned_chart_connection(connection, cursor, statement, parameters, context, executemany):
        if not triggered and 'FROM message_logs' in statement and 'message_logs.created_at >=' in statement:
            triggered.append(True)
            # Only this fixture's current backend is terminated, on its own
            # disposable database. SQLAlchemy wraps the actual driver error.
            return 'SELECT pg_terminate_backend(pg_backend_pid())', {}
        return statement, parameters
    event.listen(daily.db.engine, 'before_cursor_execute', terminate_owned_chart_connection, retval=True)
    try:
        response = daily.client.get('/dashboard')
        assert triggered == [True]
        assert response.status_code == 200
        assert daily.m.Organization.query.count() == 2
        daily.provider.client.messages.create.assert_not_called()
    finally:
        event.remove(daily.db.engine, 'before_cursor_execute', terminate_owned_chart_connection)
        daily.db.session.rollback()


@pytest.fixture
def legacy_bulk(monkeypatch):
    monkeypatch.setenv('SAAS_MODE', '0')
    from tests.test_tasks import TestSendBulkJob
    fixture = TestSendBulkJob()
    fixture.setUp()
    fixture.app.config['SAAS_MODE'] = False
    with patch('app.tasks.create_app', return_value=fixture.app), patch('app.tasks.process_failure_details'), patch('app.tasks.record_usage_candidates'):
        yield fixture
    fixture.tearDown()


def _legacy_provider(phone):
    provider = MagicMock()
    provider.send_bulk.return_value = {'total':1, 'success_count':1, 'failure_count':0,
        'details':[{'phone':phone, 'success':True, 'sid':'SM-local-legacy', 'status':'queued'}]}
    return provider


def test_legacy_initialization_failure_does_not_advance_first_recipient_on_retry(legacy_bulk):
    fixture = legacy_bulk
    log_id = fixture._create_log()
    phone = '+15550000711'
    provider = _legacy_provider(phone)
    with patch('app.tasks.get_twilio_service', side_effect=[ValueError('synthetic initialization failure'), provider]):
        fixture.send_bulk_job(log_id, [{'phone':phone}], 'Hello', delay=0)
        fixture.send_bulk_job(log_id, [{'phone':phone}], 'Hello', delay=0)
    provider.send_bulk.assert_called_once()
    assert provider.send_bulk.call_args.args[0] == [{'phone':phone}]
    fixture.db.session.expire_all()
    log = fixture.db.session.get(fixture.MessageLog, log_id)
    assert log.status == 'sent' and log.success_count == 1
    assert len([row for row in json.loads(log.details) if row.get('sid')]) == 1


def test_legacy_resume_matches_recipient_identity_instead_of_position(legacy_bulk):
    fixture = legacy_bulk
    first, second = '+15550000712', '+15550000713'
    log_id = fixture._create_log(details=[{'phone':second, 'success':True, 'sid':'SM-prior', 'status':'queued'}])
    provider = _legacy_provider(first)
    with patch('app.tasks.get_twilio_service', return_value=provider):
        fixture.send_bulk_job(log_id, [{'phone':first}, {'phone':second}], 'Hello', delay=0)
    assert provider.send_bulk.call_args.args[0] == [{'phone':first}]
    fixture.db.session.expire_all()
    log = fixture.db.session.get(fixture.MessageLog, log_id)
    assert log.success_count == 2 and log.total_recipients == 2


@pytest.mark.parametrize('detail', [
    {'phone':'+15550000714', 'success':None, 'status':'ambiguous'},
    {'error':'Historical unkeyed provider outcome cannot be attributed.'},
    {'success':True, 'sid':'SM-unattributed'},
    {'success':True, 'sid':'SM-unattributed', 'not_submitted':True},
])
def test_legacy_unknown_provider_evidence_is_held_without_resubmission(legacy_bulk, detail):
    fixture = legacy_bulk
    log_id = fixture._create_log(details=[detail])
    with patch('app.tasks.get_twilio_service') as provider:
        fixture.send_bulk_job(log_id, [{'phone':'+15550000714'}, {'phone':'+15550000715'}], 'Hello', delay=0)
    provider.assert_not_called()
    fixture.db.session.expire_all()
    log = fixture.db.session.get(fixture.MessageLog, log_id)
    assert log.status == 'ambiguous'
    assert json.loads(log.details) == [detail]


@pytest.mark.parametrize('error_type', ['generic', 'transient_unknown', 'transient_500', 'transient_503'])
def test_legacy_unknown_ack_keeps_accepted_progress_and_cannot_resubmit(legacy_bulk, error_type):
    from app.services.twilio_service import TwilioTransientError
    from twilio.base.exceptions import TwilioRestException
    fixture = legacy_bulk
    first, second = '+15550000716', '+15550000717'
    log_id = fixture._create_log()
    progress = {'details':[{'phone':first, 'success':True, 'sid':'SM-known-before-unknown', 'status':'queued'}]}
    if error_type == 'generic':
        error = RuntimeError('Synthetic provider acknowledgement lost')
        error.results = progress
    else:
        error = TwilioTransientError('Synthetic transient outcome', results=progress)
        if error_type != 'transient_unknown':
            error.__cause__ = TwilioRestException(int(error_type.rsplit('_', 1)[1]), 'https://provider.example.test/messages')
    provider = MagicMock()
    provider.send_bulk.side_effect = error
    with patch('app.tasks.get_twilio_service', return_value=provider) as getter:
        fixture.send_bulk_job(log_id, [{'phone':first}, {'phone':second}], 'Hello', delay=0)
        fixture.send_bulk_job(log_id, [{'phone':first}, {'phone':second}], 'Hello', delay=0)
    assert getter.call_count == 1
    provider.send_bulk.assert_called_once()
    fixture.db.session.expire_all()
    log = fixture.db.session.get(fixture.MessageLog, log_id)
    assert log.status == 'ambiguous' and log.success_count == 1 and log.failure_count == 0
    assert [row['sid'] for row in json.loads(log.details) if row.get('sid')] == ['SM-known-before-unknown']


def test_legacy_explicit_429_retries_only_the_remaining_recipient(legacy_bulk):
    from app.services.twilio_service import TwilioTransientError
    from twilio.base.exceptions import TwilioRestException
    fixture = legacy_bulk
    first, second = '+15550000718', '+15550000719'
    log_id = fixture._create_log()
    progress = {'total':2, 'success_count':1, 'failure_count':0,
                'details':[{'phone':first, 'success':True, 'sid':'SM-before-429', 'status':'queued'}]}
    error = TwilioTransientError('Actual safe rejection', results=progress, failed_index=1)
    error.__cause__ = TwilioRestException(429, 'https://provider.example.test/messages', code=20429)
    rejected = MagicMock()
    rejected.send_bulk.side_effect = error
    recovered = _legacy_provider(second)
    with patch('app.tasks.get_twilio_service', side_effect=[rejected, recovered]), patch('app.tasks.get_current_job', return_value=SimpleNamespace(retries_left=1)):
        with pytest.raises(TwilioTransientError):
            fixture.send_bulk_job(log_id, [{'phone':first}, {'phone':second}], 'Hello', delay=0)
        fixture.send_bulk_job(log_id, [{'phone':first}, {'phone':second}], 'Hello', delay=0)
    assert recovered.send_bulk.call_args.args[0] == [{'phone':second}]
    fixture.db.session.expire_all()
    log = fixture.db.session.get(fixture.MessageLog, log_id)
    assert log.status == 'sent' and log.success_count == 2 and log.total_recipients == 2
