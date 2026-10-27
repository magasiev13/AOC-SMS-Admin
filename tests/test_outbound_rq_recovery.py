"""Opt-in PostgreSQL/Redis/RQ recovery through the real Twilio API boundary.

Set OUTBOX_TEST_POSTGRES_ROOT to the exact loopback audit cluster and
OUTBOX_TEST_REDIS_SERVER to a local redis-server binary. Every test owns its
database, Redis process, worker subprocesses, and provider ledger. No real
provider client request is allowed: MessageList.create is replaced in workers.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from threading import Event
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest


LOCAL_POSTGRES_ROOT = "postgresql://audit@127.0.0.1:55432/postgres"
REPOSITORY = Path(__file__).resolve().parents[1]
PROVIDER_PHONES = ["+17205550145", "+17205550146"]


@pytest.fixture
def rq_stack(tmp_path, monkeypatch):
    root = os.environ.get("OUTBOX_TEST_POSTGRES_ROOT")
    if not root:
        pytest.skip("Real queue recovery requires the explicit isolated PostgreSQL root.")
    if root != LOCAL_POSTGRES_ROOT:
        raise RuntimeError("RQ tests accept only the explicit loopback audit PostgreSQL root.")
    redis_binary = os.environ.get("OUTBOX_TEST_REDIS_SERVER") or shutil.which("redis-server")
    if not redis_binary or not Path(redis_binary).is_file():
        pytest.skip("Real queue recovery requires a local redis-server binary.")
    import psycopg
    from psycopg import sql
    from redis import Redis
    from rq import Queue

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        redis_port = listener.getsockname()[1]
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    redis_log = (tmp_path / "redis.log").open("wb")
    redis_process = subprocess.Popen([
        redis_binary, "--bind", "127.0.0.1", "--port", str(redis_port),
        "--save", "", "--appendonly", "no", "--protected-mode", "yes",
        "--dir", str(tmp_path), "--daemonize", "no",
    ], stdout=redis_log, stderr=subprocess.STDOUT)
    connection = Redis.from_url(redis_url)
    database_name = "outbox_rq_" + uuid4().hex
    created_database = False
    try:
        deadline = time.monotonic() + 8
        while True:
            try:
                assert connection.ping()
                break
            except ConnectionError:
                if time.monotonic() >= deadline or redis_process.poll() is not None:
                    raise RuntimeError("The private Redis process did not start.")
                time.sleep(0.05)
            except Exception:
                if time.monotonic() >= deadline or redis_process.poll() is not None:
                    raise
                time.sleep(0.05)
        with psycopg.connect(root, autocommit=True) as postgres:
            postgres.execute(sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
            ).format(sql.Identifier(database_name)))
        created_database = True
        database_url = "postgresql+psycopg://audit@127.0.0.1:55432/" + database_name
        queue_name = "twinevia-isolated-rq-" + uuid4().hex
        for key, value in {
            "DATABASE_URL": database_url, "REDIS_URL": redis_url,
            "RQ_QUEUE_NAME": queue_name, "SAAS_MODE": "1", "FLASK_ENV": "testing",
            "FLASK_DEBUG": "1", "SECRET_KEY": "isolated-rq-recovery-test",
            "SCHEDULER_ENABLED": "0", "PYTHON_DOTENV_DISABLED": "1",
            "AUTH_ALERTS_ENABLED": "0", "TWILIO_BROWSER_FAKE_SENDS": "0",
            "TWILIO_ALLOW_LIVE_SENDS_IN_TESTING": "1",
            "TWILIO_MASTER_ACCOUNT_SID": "AC" + "0" * 32,
            "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
            "TWILIO_AUTH_TOKEN": "unused-isolated-auth-token",
            "TWILIO_MASTER_AUTH_TOKEN": "unused-isolated-auth-token",
            "TWILIO_FROM_NUMBER": "+17205550999", "STRIPE_SECRET_KEY": "",
        }.items():
            monkeypatch.setenv(key, value)
        import app.config
        importlib.reload(app.config)
        monkeypatch.setattr(app.config.Config, "TESTING", True, raising=False)
        from app import create_app, db, models
        from app.services import outbound_request_service as service
        from app.services.recipient_permission_service import record_permission_evidence

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        assert app.config["SQLALCHEMY_DATABASE_URI"] == database_url
        assert app.config["REDIS_URL"] == redis_url
        with app.app_context():
            try:
                db.create_all()
                org = models.Organization(name="Isolated RQ tenant", slug="isolated-rq")
                actor = models.AppUser(username="isolated-rq-owner", password_hash="unused-test-hash")
                db.session.add_all([
                    org, actor,
                    models.OrganizationSubscription(organization=org, status="complimentary"),
                    models.OrganizationMessagingProfile(
                        organization=org, provider_mode="platform_managed", provider_status="active",
                        status="active", sender_review_status="approved", from_number="+17205550999",
                        messaging_service_sid="MG" + "0" * 32, phone_number_sid="PN" + "0" * 32,
                    ),
                ])
                db.session.flush()
                for index, phone in enumerate(PROVIDER_PHONES):
                    record_permission_evidence(
                        org.id, phone, purpose="community", decision="granted",
                        source="owner_verified", evidence_key=f"isolated-proof-{index}",
                        verified=True, metadata={"proof": "Controlled test recipient opt-in evidence."},
                    )
                db.session.commit()
                yield SimpleNamespace(
                    app=app, db=db, models=models, service=service, connection=connection,
                    queue=Queue(queue_name, connection=connection), organization_id=org.id,
                    actor=SimpleNamespace(id=actor.id, username=actor.username),
                    recipients=[{"phone": phone, "name": f"Recipient {index + 1}"}
                                for index, phone in enumerate(PROVIDER_PHONES)],
                    body="Hello {first_name}", ledger=tmp_path / "provider-ledger.jsonl",
                    worker_logs=tmp_path,
                )
            finally:
                db.session.remove()
                db.engine.dispose()
    finally:
        connection.close()
        redis_process.terminate()
        try:
            redis_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            redis_process.kill()
            redis_process.wait(timeout=5)
        redis_log.close()
        if created_database:
            with psycopg.connect(root, autocommit=True) as postgres:
                postgres.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def saved_request(stack, key="isolated-form-token"):
    request, created = stack.service.reserve_outbound_request(
        organization_id=stack.organization_id, actor=stack.actor, key=key, kind="blast",
        intent={"target": "community", "message_body": stack.body, "test_mode": False},
        recipient_data=stack.recipients, message_body=stack.body,
    )
    assert created
    return request


def worker(stack, *, mode="accept", expected_exit=0):
    environment = os.environ.copy()
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    safety_directory = Path("/tmp/twinevia-audit-safety")
    paths = ([str(safety_directory)] if safety_directory.is_dir() else []) + [str(REPOSITORY)]
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--rq-worker", str(stack.ledger), mode],
        cwd=REPOSITORY, env=environment, capture_output=True, text=True, timeout=30,
    )
    (stack.worker_logs / f"worker-{mode}-{uuid4().hex}.log").write_text(
        process.stdout + process.stderr, encoding="utf-8",
    )
    assert process.returncode == expected_exit, process.stdout + process.stderr
    return process


def provider_calls(stack):
    if not stack.ledger.exists():
        return []
    return [json.loads(line) for line in stack.ledger.read_text().splitlines()]


def assert_one_call_per_phone(stack):
    calls = provider_calls(stack)
    assert Counter(call["to"] for call in calls) == Counter({phone: 1 for phone in PROVIDER_PHONES})
    assert all(call["database_backend_pid"] > 0 for call in calls)
    assert len({call["pid"] for call in calls}) >= 1


def make_due(stack, request):
    request.next_enqueue_at = stack.service._now() - timedelta(seconds=1)
    stack.db.session.commit()


def replay_saved_job(stack, request):
    payload = json.loads(request.payload_json)
    stack.queue.enqueue(
        "app.tasks.send_bulk_job", request.message_log_id, stack.organization_id,
        payload["recipients"], payload["message_body"], delay=0,
        job_id="replay-" + uuid4().hex,
    )
    worker(stack)


def test_real_queue_acceptance_timeout_recovers_existing_job_and_replay_is_safe(rq_stack):
    stack = rq_stack
    request = saved_request(stack)

    class LostAcknowledgementQueue:
        connection = stack.connection

        def enqueue(self, *args, **kwargs):
            stack.queue.enqueue(*args, **kwargs)
            raise TimeoutError("real Redis accepted the job; HTTP acknowledgement was lost")

    assert not stack.service.enqueue_outbound_request(request, queue=LostAcknowledgementQueue())
    assert request.status == "queue_unknown" and stack.queue.count == 1
    make_due(stack, request)
    assert stack.service.reconcile_outbound_requests() == {"requests_seen": 1, "requests_queued": 0}
    assert stack.queue.count == 1
    # Redis can contain the same accepted job ID twice after an uncertain
    # producer acknowledgement. Exercise the actual RQ list duplication too.
    job = stack.queue.fetch_job(stack.service.request_job_id(request))
    stack.queue.enqueue(job.func_name, *job.args, **job.kwargs, job_id=job.id)
    assert stack.queue.count == 2
    worker(stack)
    stack.db.session.refresh(request)
    assert request.status == "accepted"
    assert stack.db.session.get(stack.models.MessageLog, request.message_log_id).status == "sent"
    replay_saved_job(stack, request)
    assert_one_call_per_phone(stack)
    assert stack.models.MessageDispatchAttempt.query.count() == 2
    assert {attempt.attempt_count for attempt in stack.models.MessageDispatchAttempt.query.all()} == {1}


def test_web_process_stops_after_database_commit_before_enqueue_then_timer_recovers(rq_stack):
    stack = rq_stack
    request = saved_request(stack)
    request_id, log_id = request.id, request.message_log_id
    assert stack.queue.count == 0
    stack.db.session.remove()
    assert stack.service.reconcile_outbound_requests() == {"requests_seen": 1, "requests_queued": 1}
    request = stack.db.session.get(stack.models.OutboundRequest, request_id)
    job = stack.queue.fetch_job(stack.service.request_job_id(request))
    assert job.args[0] == log_id
    worker(stack)
    stack.db.session.refresh(request)
    assert request.status == "accepted"
    replay_saved_job(stack, request)
    assert_one_call_per_phone(stack)
    assert stack.models.OutboundRequest.query.count() == stack.models.MessageLog.query.count() == 1


def test_changed_redis_job_payload_is_rejected_then_recovered_from_saved_snapshot(rq_stack):
    stack = rq_stack
    request = saved_request(stack)
    assert stack.service.enqueue_outbound_request(request, queue=stack.queue)
    job_id = stack.service.request_job_id(request)
    stack.queue.fetch_job(job_id).delete()
    stack.queue.enqueue(
        "app.tasks.send_bulk_job", request.message_log_id, stack.organization_id,
        stack.recipients, "Different content injected into queue", delay=0, job_id=job_id,
    )
    worker(stack)
    assert stack.queue.fetch_job(job_id).get_status(refresh=True) == "failed"
    assert provider_calls(stack) == []
    assert stack.models.MessageDispatchAttempt.query.count() == 0
    stack.db.session.refresh(request)
    assert request.status == "queued"
    assert json.loads(request.payload_json)["message_body"] == stack.body
    make_due(stack, request)
    assert stack.service.reconcile_outbound_requests() == {"requests_seen": 1, "requests_queued": 1}
    worker(stack)
    stack.db.session.refresh(request)
    assert request.status == "accepted"
    assert_one_call_per_phone(stack)
    assert {call["body"] for call in provider_calls(stack)} == {"Hello Recipient"}


def test_provider_acceptance_timeout_survives_redis_loss_recovery_and_fresh_form(rq_stack):
    stack = rq_stack
    request = saved_request(stack)
    assert stack.service.enqueue_outbound_request(request, queue=stack.queue)
    worker(stack, mode="accept-then-timeout")
    stack.db.session.refresh(request)
    assert request.status == "ambiguous"
    assert {attempt.status for attempt in stack.models.MessageDispatchAttempt.query.all()} == {"ambiguous"}
    assert_one_call_per_phone(stack)
    stack.connection.flushdb()  # This Redis process is owned solely by this fixture.
    make_due(stack, request)
    assert stack.service.reconcile_outbound_requests() == {"requests_seen": 0, "requests_queued": 0}
    repeated, created = stack.service.reserve_outbound_request(
        organization_id=stack.organization_id, actor=stack.actor, key="fresh-form-after-redis-loss",
        kind="blast", intent=json.loads(request.payload_json)["intent"],
        recipient_data=stack.recipients, message_body=stack.body,
    )
    assert not created and repeated.id == request.id
    replay_saved_job(stack, request)
    assert_one_call_per_phone(stack)
    assert stack.models.MessageLog.query.count() == 1


def test_worker_process_crash_after_sent_attempt_recovers_without_duplicate_provider_call(rq_stack):
    from rq.registry import StartedJobRegistry

    stack = rq_stack
    request = saved_request(stack)
    assert stack.service.enqueue_outbound_request(request, queue=stack.queue)
    worker(stack, mode="crash-after-sent-attempt", expected_exit=86)
    stack.db.session.refresh(request)
    assert request.status == "processing"
    log = stack.db.session.get(stack.models.MessageLog, request.message_log_id)
    assert log.status == "processing" and json.loads(log.details) == []
    attempt = stack.models.MessageDispatchAttempt.query.one()
    assert attempt.status == "sent" and attempt.attempt_count == 1
    assert len(provider_calls(stack)) == 1
    job = stack.queue.fetch_job(stack.service.request_job_id(request))
    assert job.get_status(refresh=True) == "started"
    # Advance RQ's abandoned-job cleanup clock; the worker process has actually
    # exited. Disable its automatic retry so the explicit outbox timer owns this
    # recovery, then reuse the same job/log identity.
    job.retries_left = 0
    job.save()
    registry = StartedJobRegistry(queue=stack.queue)
    assert job.id in registry.cleanup(timestamp=time.time() + job.timeout + 120)
    assert job.get_status(refresh=True) == "failed"
    make_due(stack, request)
    assert stack.service.reconcile_outbound_requests() == {"requests_seen": 1, "requests_queued": 1}
    worker(stack)
    stack.db.session.refresh(request)
    stack.db.session.refresh(log)
    assert request.status == "accepted" and log.status == "sent"
    assert log.success_count == 2
    replay_saved_job(stack, request)
    assert_one_call_per_phone(stack)
    assert {attempt.status for attempt in stack.models.MessageDispatchAttempt.query.all()} == {"sent"}
    assert {attempt.attempt_count for attempt in stack.models.MessageDispatchAttempt.query.all()} == {1}


def add_committed_attempt(stack, request, *, status="sent", phone=PROVIDER_PHONES[0]):
    attempt = stack.models.MessageDispatchAttempt(
        organization_id=stack.organization_id, message_log_id=request.message_log_id,
        logical_send_key=f"blast-log:{request.message_log_id}:finalization-fixture",
        recipient_phone=phone, message_body_digest=hashlib.sha256(b"Hello Recipient").hexdigest(),
        send_kind="blast", status=status, attempt_count=1,
        provider_message_sid="SM" + "a" * 32 if status == "sent" else None,
    )
    stack.db.session.add(attempt)
    request.status = "processing"
    stack.db.session.commit()
    return attempt.id


def test_stale_duplicate_worker_cannot_downgrade_committed_accepted_finalization(rq_stack):
    from sqlalchemy import text
    from app.tasks import _finish_durable_request
    from app.tenant import organization_context

    stack = rq_stack
    stack.recipients = stack.recipients[:1]
    request = saved_request(stack)
    request_id, log_id = request.id, request.message_log_id
    attempt_id = add_committed_attempt(stack, request, status="sending")
    stale_captured, accepted_committed = Event(), Event()

    def stale_worker():
        with stack.app.app_context(), organization_context(stack.organization_id):
            backend_id = stack.db.session.scalar(text("SELECT pg_backend_pid()"))
            log = stack.db.session.get(stack.models.MessageLog, log_id)
            log.total_recipients = 1
            log.status = "failed"
            log.details = json.dumps([{
                "phone": PROVIDER_PHONES[0], "status": "sending", "success": False,
                "sid": None, "error": "The other worker is still submitting this attempt.",
            }])
            stale_captured.set()
            assert accepted_committed.wait(timeout=10)
            assert _finish_durable_request(log)
            return backend_id

    def successful_worker():
        assert stale_captured.wait(timeout=10)
        with stack.app.app_context(), organization_context(stack.organization_id):
            backend_id = stack.db.session.scalar(text("SELECT pg_backend_pid()"))
            attempt = stack.db.session.get(stack.models.MessageDispatchAttempt, attempt_id)
            attempt.status = "sent"
            attempt.provider_message_sid = "SM" + "a" * 32
            stack.db.session.commit()
            log = stack.db.session.get(stack.models.MessageLog, log_id)
            log.total_recipients = 1
            log.status = "sent"
            log.details = json.dumps([{
                "phone": PROVIDER_PHONES[0], "status": "sent", "success": True,
                "sid": "SM" + "a" * 32, "error": None,
            }])
            assert _finish_durable_request(log)
            accepted_committed.set()
            return backend_id

    with ThreadPoolExecutor(max_workers=2) as workers:
        stale = workers.submit(stale_worker)
        accepted = workers.submit(successful_worker)
        assert stale.result(timeout=15) != accepted.result(timeout=15)
    stack.db.session.expire_all()
    request = stack.db.session.get(stack.models.OutboundRequest, request_id)
    log = stack.db.session.get(stack.models.MessageLog, log_id)
    assert request.status == "accepted" and log.status == "sent"
    assert log.success_count == 1 and log.failure_count == 0
    details = json.loads(log.details)
    assert len(details) == 1 and details[0]["sid"] == "SM" + "a" * 32
    assert details[0]["success"] is True and details[0]["status"] == "sent"


def test_permission_exclusion_during_recovery_preserves_committed_acceptance_history(rq_stack):
    from app.tasks import _finish_durable_request

    stack = rq_stack
    stack.recipients = stack.recipients[:1]
    request = saved_request(stack)
    add_committed_attempt(stack, request)
    log = stack.db.session.get(stack.models.MessageLog, request.message_log_id)
    log.total_recipients = 1
    log.status = "failed"
    log.details = json.dumps([{
        "phone": PROVIDER_PHONES[0], "success": None, "status": "excluded",
        "error": "Permission was revoked after the provider accepted this attempt.",
    }])
    assert _finish_durable_request(log, error="No recipients remain eligible.")
    stack.db.session.refresh(log)
    details = json.loads(log.details)
    assert log.success_count == 1 and log.failure_count == 0
    assert len(details) == 1 and details[0]["status"] == "sent"
    assert details[0]["success"] is True and details[0]["sid"] == "SM" + "a" * 32
    assert stack.models.MessageDispatchAttempt.query.one().attempt_count == 1
    stack.db.session.refresh(request)
    assert request.status == "accepted" and log.status == "sent"
    assert request.last_error is None and json.loads(request.result_json)["error"] is None


def test_partial_fatal_error_cannot_be_reported_as_fully_accepted(rq_stack):
    from app.tasks import _finish_durable_request

    stack = rq_stack
    request = saved_request(stack)
    add_committed_attempt(stack, request)
    log = stack.db.session.get(stack.models.MessageLog, request.message_log_id)
    log.total_recipients = 2
    log.status = "failed"
    log.success_count = 1
    log.failure_count = 1
    log.details = json.dumps([
        {"phone": PROVIDER_PHONES[0], "status": "sent", "success": True, "sid": "SM" + "a" * 32},
        {"error": "Persistence failed before the second recipient was submitted."},
    ])
    assert _finish_durable_request(log, error="Persistence failed before the second recipient was submitted.")
    stack.db.session.refresh(request)
    stack.db.session.refresh(log)
    assert request.status == "failed" and log.status == "failed"
    assert log.success_count == 1
    assert json.loads(request.result_json)["success"] is False
    assert any(row.get("sid") == "SM" + "a" * 32 for row in json.loads(log.details))


@pytest.mark.parametrize("old_signature", ["explicit-organization", "omitted-organization"])
def test_existing_saas_queued_jobs_without_new_request_rows_keep_signature_compatibility(rq_stack, old_signature):
    stack = rq_stack
    log = stack.models.MessageLog(
        organization_id=stack.organization_id, message_body=stack.body, target="community",
        status="processing", details="[]", test_mode=False,
    )
    stack.db.session.add(log)
    stack.db.session.commit()
    args = [log.id]
    if old_signature == "explicit-organization":
        args.append(stack.organization_id)
    args.extend([stack.recipients, stack.body])
    for _ in range(2):
        stack.queue.enqueue("app.tasks.send_bulk_job", *args, delay=0, job_id="old-job-" + uuid4().hex)
        worker(stack)
    stack.db.session.refresh(log)
    assert log.status == "sent" and log.success_count == 2 and log.failure_count == 0
    assert stack.models.OutboundRequest.query.count() == 0
    assert stack.models.MessageDispatchAttempt.query.count() == 2
    assert_one_call_per_phone(stack)


def test_known_acceptance_after_duplicate_ambiguity_and_worker_exit_has_explicit_safe_recovery(rq_stack, monkeypatch):
    """A late committed SID remains recoverable when its finalizer never runs."""
    from threading import current_thread
    from unittest.mock import patch
    from sqlalchemy import text
    import app.tasks as tasks
    from app.services.outbound_review_service import finalize_known_outbound_request

    stack = rq_stack
    stack.recipients = stack.recipients[:1]
    request = saved_request(stack)
    request_id, log_id = request.id, request.message_log_id
    entered, release = Event(), Event()
    submitted_sid = 'SM' + uuid4().hex

    def provider_create(_client, **kwargs):
        entry = dict(kwargs, sid=submitted_sid, pid=os.getpid(), database_backend_pid=stack.db.session.scalar(text('SELECT pg_backend_pid()')))
        with stack.ledger.open('a', encoding='utf-8') as output:
            output.write(json.dumps(entry, sort_keys=True) + '\n')
        entered.set()
        assert release.wait(timeout=10), 'Original provider submission barrier timed out.'
        return SimpleNamespace(sid=submitted_sid, status='queued', account_sid='AC' + '0' * 32)

    original_finish = tasks._finish_durable_request

    def finish(log, **kwargs):
        if current_thread().name.endswith('_0'):
            # Models process death after the provider outcome committed. The
            # application context rolls back only this unsaved aggregate.
            raise SystemExit('Original worker exited before aggregate finalization.')
        return original_finish(log, **kwargs)

    monkeypatch.setattr(tasks, 'create_app', lambda **_: stack.app)
    monkeypatch.setattr(tasks, '_finish_durable_request', finish)
    monkeypatch.setattr('twilio.rest.api.v2010.account.message.MessageList.create', provider_create)

    def execute():
        tasks.send_bulk_job(log_id, stack.organization_id, stack.recipients, stack.body, delay=0)

    stack.db.session.remove()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='known-race-worker') as workers:
        original = workers.submit(execute)
        assert entered.wait(timeout=10), 'Original provider submission was not reached.'
        try:
            duplicate = workers.submit(execute)
            duplicate.result(timeout=10)
        finally:
            release.set()
        with pytest.raises(SystemExit, match='before aggregate finalization'):
            original.result(timeout=10)

    stack.db.session.expire_all()
    request = stack.db.session.get(stack.models.OutboundRequest, request_id)
    attempt = stack.models.MessageDispatchAttempt.query.one()
    assert request.status == 'ambiguous'
    assert (attempt.status, attempt.provider_message_sid, attempt.attempt_count) == ('sent', submitted_sid, 1)
    assert len(provider_calls(stack)) == 1
    # Automatic recovery must still avoid an unresolved aggregate. An explicit
    # operator can now prove the complete frozen audience is already known.
    make_due(stack, request)
    assert stack.service.reconcile_outbound_requests() == {'requests_seen': 0, 'requests_queued': 0}
    admin = stack.models.AppUser(username='known-outcome-reviewer', password_hash='unused-test-hash', is_platform_admin=True)
    stack.db.session.add(admin)
    stack.db.session.commit()
    with patch('app.services.twilio_service._client_for_usage_reconciliation') as provider_lookup, patch.object(stack.service, 'enqueue_outbound_request') as enqueue:
        reviewed = finalize_known_outbound_request(
            stack.organization_id, request_id, actor_user_id=admin.id,
            evidence_reference='isolated-PG-barrier/committed-acceptance-after-duplicate-worker',
        )
    provider_lookup.assert_not_called()
    enqueue.assert_not_called()
    assert reviewed['request_status'] == 'pending'
    assert stack.queue.count == 0 and len(provider_calls(stack)) == 1
    assert stack.service.reconcile_outbound_requests() == {'requests_seen': 1, 'requests_queued': 1}
    worker(stack)
    stack.db.session.refresh(request)
    log = stack.db.session.get(stack.models.MessageLog, log_id)
    assert request.status == 'accepted' and log.status == 'sent'
    assert log.success_count == 1 and log.failure_count == 0
    assert len(provider_calls(stack)) == 1
    assert stack.models.MessageDispatchAttempt.query.one().provider_message_sid == submitted_sid
    assert json.loads(request.result_json)['operator_reviews'][-1]['action'] == 'finalize_known'


def _worker_entry(ledger_path, mode):
    """A real worker process; the only provider substitution is MessageList.create."""
    from unittest.mock import patch
    from redis import Redis
    from rq import Queue, SimpleWorker
    from sqlalchemy import text
    from app import db
    import app.config
    from app.services.twilio_service import TwilioService

    database = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "")
    if not database.startswith("postgresql+psycopg://audit@127.0.0.1:55432/outbox_rq_"):
        raise RuntimeError("Worker requires its owned isolated PostgreSQL database.")
    if not redis_url.startswith("redis://127.0.0.1:"):
        raise RuntimeError("Worker requires the owned loopback Redis process.")
    app.config.Config.TESTING = True
    ledger = Path(ledger_path)

    def provider_create(self, **kwargs):
        sid = "SM" + uuid4().hex
        entry = dict(kwargs, sid=sid, pid=os.getpid(), database_backend_pid=db.session.scalar(text("SELECT pg_backend_pid()")))
        with ledger.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        if mode == "accept-then-timeout":
            raise TimeoutError("Controlled provider accepted the message; response was lost.")
        return SimpleNamespace(
            sid=sid, status="queued", account_sid="AC" + "0" * 32,
            num_segments="1", price=None, price_unit="usd",
        )

    original_attempt = TwilioService.send_message_with_attempt

    def crash_after_committed_attempt(self, *args, **kwargs):
        result = original_attempt(self, *args, **kwargs)
        if result.get("success") and not result.get("idempotent_replay"):
            os._exit(86)
        return result

    connection = Redis.from_url(redis_url)
    queue = Queue(os.environ["RQ_QUEUE_NAME"], connection=connection)
    with patch("twilio.rest.api.v2010.account.message.MessageList.create", provider_create):
        if mode == "crash-after-sent-attempt":
            TwilioService.send_message_with_attempt = crash_after_committed_attempt
        worker = SimpleWorker([queue], connection=connection, name="isolated-worker-" + uuid4().hex)
        worker.work(burst=True, with_scheduler=False, logging_level="WARNING")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--rq-worker":
        _worker_entry(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit("Use pytest for isolated RQ recovery verification.")
