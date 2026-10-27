"""Durable intent and queue recovery, with opt-in local PostgreSQL races.

The default cases use private SQLite databases. Set OUTBOX_TEST_POSTGRES_ROOT
to the explicit audit cluster to run every case on a unique UTF-8 PostgreSQL
database and enable the two-session contention cases. Providers and Redis are
always fakes; this module does not construct the production application.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import os
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session


LOCAL_POSTGRES_ROOT = "postgresql://audit@127.0.0.1:55432/postgres"


@pytest.fixture
def outbox(tmp_path):
    from app import db, models
    from app.services import outbound_request_service as service
    from app.tenant import init_tenant_scoping

    postgres_root = os.environ.get("OUTBOX_TEST_POSTGRES_ROOT")
    database_name = None
    if postgres_root:
        if postgres_root != LOCAL_POSTGRES_ROOT:
            raise RuntimeError("Outbox tests accept only the isolated loopback audit database root.")
        import psycopg
        from psycopg import sql

        database_name = "outbox_test_" + uuid4().hex
        with psycopg.connect(postgres_root, autocommit=True) as connection:
            connection.execute(sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
            ).format(sql.Identifier(database_name)))
        database_uri = "postgresql+psycopg://audit@127.0.0.1:55432/" + database_name
    else:
        database_uri = "sqlite:///" + str(tmp_path / "outbox.db")

    app = Flask(__name__)
    app.config.update(
        TESTING=True, SAAS_MODE=True, SQLALCHEMY_DATABASE_URI=database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        REDIS_URL="redis://127.0.0.1:1/0", RQ_QUEUE_NAME="twinevia-outbox-isolated-test",
    )
    db.init_app(app)
    init_tenant_scoping()
    try:
        with app.app_context():
            db.create_all()
            org = models.Organization(name="Outbox fixture", slug="outbox-fixture")
            other_org = models.Organization(name="Other fixture", slug="other-outbox-fixture")
            actor = models.AppUser(username="outbox-owner", password_hash="unused-test-hash")
            other_actor = models.AppUser(username="outbox-staff", password_hash="unused-test-hash")
            db.session.add_all([org, other_org, actor, other_actor])
            db.session.commit()
            fixture = SimpleNamespace(
                app=app, db=db, models=models, service=service, postgres=bool(postgres_root),
                organization_id=org.id, other_organization_id=other_org.id,
                actor=SimpleNamespace(id=actor.id, username=actor.username),
                other_actor=SimpleNamespace(id=other_actor.id, username=other_actor.username),
            )
            yield fixture
            db.session.remove()
            db.engine.dispose()
    finally:
        if database_name:
            import psycopg
            from psycopg import sql

            with psycopg.connect(LOCAL_POSTGRES_ROOT, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def reserve(outbox, *, key="request-1", actor=None, organization_id=None,
            kind="blast", intent=None, recipients=None, message_body=None):
    intent = deepcopy(intent if intent is not None else {
        "target": "community", "message_body": "Hello from the saved request", "test_mode": False,
    })
    return outbox.service.reserve_outbound_request(
        organization_id=organization_id or outbox.organization_id,
        actor=actor or outbox.actor, key=key, kind=kind, intent=intent,
        recipient_data=recipients if recipients is not None else [{"phone": "+17205550145", "name": "Saved name"}],
        message_body=message_body if message_body is not None else intent["message_body"],
    )


class FakeQueue:
    def __init__(self, *, enqueue=None, ping=None):
        self.calls = []
        self.jobs = {}
        self.enqueue_callback = enqueue
        self.connection = SimpleNamespace(ping=ping or (lambda: True))

    def enqueue(self, *args, **kwargs):
        self.calls.append((deepcopy(args), dict(kwargs)))
        self.jobs[kwargs["job_id"]] = SimpleNamespace(get_status=lambda **_: "queued")
        if self.enqueue_callback:
            return self.enqueue_callback(*args, **kwargs)
        return self.jobs[kwargs["job_id"]]

    def fetch_job(self, job_id):
        return self.jobs.get(job_id)


def make_due(outbox, request):
    request.next_enqueue_at = outbox.service._now() - timedelta(seconds=1)
    outbox.db.session.commit()


def test_request_identity_is_bound_to_organization_actor_and_key(outbox):
    first, created = reserve(outbox)
    assert created
    same, created = reserve(outbox)
    assert not created
    assert same.id == first.id
    other_actor, created = reserve(outbox, actor=outbox.other_actor)
    assert created
    other_org, created = reserve(outbox, organization_id=outbox.other_organization_id)
    assert created
    assert len({first.id, other_actor.id, other_org.id}) == 3
    assert outbox.models.OutboundRequest.query.count() == 3
    assert outbox.models.MessageLog.query.count() == 3


def test_tenant_scope_hides_requests_and_logs_from_other_workspace(outbox):
    from app.tenant import organization_context

    ours, _ = reserve(outbox)
    theirs, _ = reserve(outbox, organization_id=outbox.other_organization_id)
    with organization_context(outbox.organization_id):
        assert [row.id for row in outbox.models.OutboundRequest.query.all()] == [ours.id]
        assert outbox.models.OutboundRequest.query.filter_by(id=theirs.id).first() is None
        assert outbox.models.MessageLog.query.filter_by(id=theirs.message_log_id).first() is None


@pytest.mark.parametrize("change", ["body", "target", "kind"])
def test_same_key_rejects_changed_intent_without_new_request_or_log(outbox, change):
    first, _ = reserve(outbox)
    intent = json.loads(first.payload_json)["intent"]
    if change == "body":
        intent["message_body"] = "Different content"
    elif change == "target":
        intent["target"] = "event"
        intent["event_id"] = 123
    with pytest.raises(outbox.service.OutboundRequestConflict):
        reserve(outbox, kind="reply" if change == "kind" else "blast", intent=intent)
    outbox.db.session.rollback()
    assert outbox.models.OutboundRequest.query.count() == 1
    assert outbox.models.MessageLog.query.count() == 1


def test_keyless_compatibility_retries_reuse_saved_snapshot_even_after_acceptance(outbox):
    intent = {"target": "community", "message_body": "Old form content", "test_mode": False}
    service = outbox.service
    key = service.normalize_request_key(None, kind="blast", intent=intent)
    assert key == service.normalize_request_key("  ", kind="blast", intent=dict(reversed(list(intent.items()))))
    assert key.startswith("compat-") and len(key) <= 64
    first, _ = reserve(outbox, key=key, intent=intent)
    service.complete_request(first, {"success": True, "sid": "SMtest"})
    outbox.db.session.commit()
    second, created = reserve(outbox, key=key, intent=intent, recipients=[{"phone": "+17205550146"}])
    assert not created
    assert second.id == first.id
    assert json.loads(second.payload_json)["recipients"][0]["phone"] == "+17205550145"
    assert outbox.models.MessageLog.query.count() == 1


@pytest.mark.parametrize("state", ["pending", "queued", "queue_unknown", "processing", "ambiguous"])
def test_fresh_token_cannot_duplicate_identical_unresolved_intent(outbox, state):
    first, _ = reserve(outbox)
    first.status = state
    outbox.db.session.commit()
    second, created = reserve(outbox, key="fresh-form-token")
    assert not created, f"A fresh token must not duplicate an unresolved {state} send"
    assert second.id == first.id
    assert outbox.models.MessageLog.query.count() == 1


def test_fresh_token_allows_deliberate_identical_send_after_provider_acceptance(outbox):
    first, _ = reserve(outbox)
    outbox.service.complete_request(first, {"success": True, "sid": "SMtest"})
    outbox.db.session.commit()
    second, created = reserve(outbox, key="new-deliberate-send")
    assert created and second.id != first.id
    assert second.message_log_id != first.message_log_id


@pytest.mark.parametrize("key", ["x" * 65, "has spaces", "slash/key", "unicode-\u00e9", "line\nbreak"])
def test_invalid_request_key_rejected_before_reservation(outbox, key):
    with pytest.raises(ValueError):
        outbox.service.normalize_request_key(key, kind="blast", intent={"message_body": "test"})
    assert outbox.models.OutboundRequest.query.count() == 0


def test_committed_snapshot_precedes_queue_and_cannot_drift_after_reservation(outbox):
    recipients = [{"phone": "+17205550145", "name": "Original recipient"}]
    intent = {"target": "community", "message_body": "Original message", "test_mode": False}
    request, _ = reserve(outbox, intent=intent, recipients=recipients)
    request_id, log_id = request.id, request.message_log_id
    recipients[0]["phone"] = "+17205550146"
    intent["message_body"] = "Mutated message"

    def observed_enqueue(*args, **kwargs):
        with Session(outbox.db.engine) as observer:
            saved = observer.scalar(select(outbox.models.OutboundRequest).where(
                outbox.models.OutboundRequest.id == request_id,
            ))
            log = observer.get(outbox.models.MessageLog, log_id)
            assert saved is not None and log is not None
            assert saved.payload_json == request.payload_json
            assert saved.enqueue_lease is not None
            assert log.message_body == "Original message"
        assert args[0] == "app.tasks.send_bulk_job"
        assert args[1:3] == (log_id, outbox.organization_id)
        assert args[3] == [{"phone": "+17205550145", "name": "Original recipient"}]
        assert args[4] == "Original message"
        assert kwargs["job_id"] == outbox.service.request_job_id(request)

    queue = FakeQueue(enqueue=observed_enqueue)
    assert outbox.service.enqueue_outbound_request(request, queue=queue)
    assert request.status == "queued"
    assert len(queue.calls) == 1


@pytest.mark.parametrize("redis_retained_job", [False, True])
def test_queue_accept_then_timeout_recovers_same_job_and_log(outbox, monkeypatch, redis_retained_job):
    request, _ = reserve(outbox)
    request_id, log_id = request.id, request.message_log_id

    def acknowledgement_lost(*args, **kwargs):
        raise TimeoutError("accepted job but reply was lost")

    queue = FakeQueue(enqueue=acknowledgement_lost)
    assert not outbox.service.enqueue_outbound_request(request, queue=queue)
    assert request.status == "queue_unknown"
    assert "reply was lost" in request.last_error
    if not redis_retained_job:
        queue.jobs.clear()
        queue.enqueue_callback = None
    make_due(outbox, request)
    monkeypatch.setattr(outbox.service, "get_queue", lambda: queue)
    assert outbox.service.reconcile_outbound_requests() == {
        "requests_seen": 1, "requests_queued": 0 if redis_retained_job else 1,
    }
    assert len(queue.calls) == (1 if redis_retained_job else 2)
    assert {call[1]["job_id"] for call in queue.calls} == {
        f"twinevia-send-{outbox.organization_id}-{request_id}",
    }
    assert {call[0][1] for call in queue.calls} == {log_id}
    assert outbox.models.MessageLog.query.count() == 1


@pytest.mark.parametrize("lose_acknowledgement", [False, True])
@pytest.mark.parametrize("result,expected_state", [
    ({"success": True, "sid": "SMworker"}, "accepted"),
    ({"success": False, "error": "Provider rejected the message"}, "failed"),
    ({"details": [{"status": "ambiguous", "sid": "SMworker"}]}, "ambiguous"),
])
def test_queue_acknowledgement_racing_worker_completion_cannot_regress_result(
    outbox, lose_acknowledgement, result, expected_state,
):
    request, _ = reserve(outbox)

    def worker_finishes_first(*args, **kwargs):
        with outbox.app.app_context():
            worker_request = outbox.db.session.get(outbox.models.OutboundRequest, request.id)
            outbox.service.complete_request(worker_request, result)
            outbox.db.session.commit()
        if lose_acknowledgement:
            raise TimeoutError("reply lost after worker completion")

    queue = FakeQueue(enqueue=worker_finishes_first)
    assert outbox.service.enqueue_outbound_request(request, queue=queue) is not lose_acknowledgement
    assert request.status == expected_state
    assert json.loads(request.result_json) == result
    assert request.last_error == result.get("error") and request.next_enqueue_at is None
    assert request.enqueue_lease is None and request.enqueue_lease_until is None


@pytest.mark.parametrize("state", ["pending", "queue_unknown", "queued", "processing"])
def test_recovery_requeues_committed_request_when_redis_job_is_missing(outbox, monkeypatch, state):
    request, _ = reserve(outbox)
    request.status = state
    make_due(outbox, request)
    queue = FakeQueue()
    monkeypatch.setattr(outbox.service, "get_queue", lambda: queue)
    assert outbox.service.reconcile_outbound_requests() == {"requests_seen": 1, "requests_queued": 1}
    outbox.db.session.refresh(request)
    assert request.status == ("processing" if state == "processing" else "queued")
    assert len(queue.calls) == 1
    assert queue.calls[0][0][1] == request.message_log_id
    assert queue.calls[0][1]["job_id"] == outbox.service.request_job_id(request)
    assert outbox.models.OutboundRequest.query.count() == outbox.models.MessageLog.query.count() == 1


@pytest.mark.parametrize("job_status", ["queued", "started", "deferred", "scheduled"])
def test_recovery_leaves_existing_active_queue_job_untouched(outbox, monkeypatch, job_status):
    request, _ = reserve(outbox)
    make_due(outbox, request)
    queue = FakeQueue()
    queue.jobs[outbox.service.request_job_id(request)] = SimpleNamespace(
        get_status=lambda **_: job_status,
    )
    monkeypatch.setattr(outbox.service, "get_queue", lambda: queue)
    assert outbox.service.reconcile_outbound_requests() == {"requests_seen": 1, "requests_queued": 0}
    assert queue.calls == []
    assert request.next_enqueue_at > outbox.service._now()


def test_queue_offline_preserves_committed_intent_for_timer_recovery(outbox, monkeypatch):
    request, _ = reserve(outbox)
    saved_payload = request.payload_json

    def offline():
        raise ConnectionError("isolated queue offline")

    offline_queue = FakeQueue(ping=offline)
    assert not outbox.service.enqueue_outbound_request(request, queue=offline_queue)
    assert request.status == "pending" and request.enqueue_attempts == 1
    assert request.payload_json == saved_payload
    assert request.enqueue_lease is None and request.next_enqueue_at is not None
    assert offline_queue.calls == []
    outbox.db.session.remove()
    request = outbox.db.session.get(outbox.models.OutboundRequest, request.id)
    assert request is not None and request.payload_json == saved_payload
    make_due(outbox, request)
    recovered_queue = FakeQueue()
    monkeypatch.setattr(outbox.service, "get_queue", lambda: recovered_queue)
    assert outbox.service.reconcile_outbound_requests()["requests_queued"] == 1
    assert len(recovered_queue.calls) == 1


@pytest.mark.parametrize("state", ["accepted", "failed", "ambiguous"])
def test_terminal_requests_are_never_automatically_reenqueued(outbox, monkeypatch, state):
    request, _ = reserve(outbox)
    request.status = state
    make_due(outbox, request)
    queue = FakeQueue()
    monkeypatch.setattr(outbox.service, "get_queue", lambda: queue)
    assert not outbox.service.enqueue_outbound_request(request, queue=queue)
    assert outbox.service.reconcile_outbound_requests() == {"requests_seen": 0, "requests_queued": 0}
    assert queue.calls == []


def test_reply_reservation_queues_same_request_without_creating_blast_log(outbox):
    request, _ = reserve(outbox, kind="reply", intent={"message_body": "Reply", "thread_id": 12})
    queue = FakeQueue()
    assert outbox.service.enqueue_outbound_request(request, queue=queue)
    assert request.message_log_id is None and outbox.models.MessageLog.query.count() == 0
    assert queue.calls[0][0] == (
        "app.tasks.send_reply_request_job", outbox.organization_id, request.id,
    )
    assert outbox.service.claim_reply_request(request)
    assert not outbox.service.claim_reply_request(request)
    assert request.status == "processing"


@pytest.mark.parametrize("kind", ["blast", "reply"])
@pytest.mark.parametrize("result,expected_state", [
    ({"success": True, "sid": "SMcompleted"}, "accepted"),
    ({"success": False, "error": "Provider rejected the message"}, "failed"),
    ({"details": [{"status": "ambiguous"}]}, "ambiguous"),
])
def test_stale_worker_start_cannot_regress_another_sessions_terminal_result(outbox, kind, result, expected_state):
    request, _ = reserve(outbox, kind=kind)
    assert request.status == "pending"
    # Hold the original ORM instance while a second application/session commits
    # completion. Starting execution must compare the current database state.
    with outbox.app.app_context():
        completed = outbox.db.session.get(outbox.models.OutboundRequest, request.id)
        outbox.service.complete_request(completed, result)
        outbox.db.session.commit()
    assert request.status == "pending"
    assert not outbox.service.start_request_execution(request)
    assert request.status == expected_state
    assert json.loads(request.result_json) == result
    assert request.next_enqueue_at is None


@pytest.mark.parametrize("same_token", [False, True])
def test_postgres_two_sessions_reserve_one_request_and_one_log(outbox, same_token):
    if not outbox.postgres:
        pytest.skip("Real PostgreSQL contention requires OUTBOX_TEST_POSTGRES_ROOT.")
    start = Barrier(2)

    def submit(index):
        with outbox.app.app_context():
            backend_id = outbox.db.session.scalar(text("SELECT pg_backend_pid()"))
            start.wait(timeout=10)
            request, created = reserve(outbox, key="same-request" if same_token else f"form-{index}")
            return request.id, request.message_log_id, created, backend_id

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(submit, range(2)))
    assert len({result[3] for result in results}) == 2
    assert len({result[0] for result in results}) == len({result[1] for result in results}) == 1
    assert sorted(result[2] for result in results) == [False, True]
    assert outbox.models.OutboundRequest.query.count() == outbox.models.MessageLog.query.count() == 1


def test_postgres_outbox_enqueue_lease_allows_only_one_concurrent_queue_operation(outbox):
    if not outbox.postgres:
        pytest.skip("Real PostgreSQL contention requires OUTBOX_TEST_POSTGRES_ROOT.")
    request, _ = reserve(outbox)
    request_id = request.id
    entered, release = Event(), Event()
    calls, calls_lock = [], Lock()

    def slow_queue(*args, **kwargs):
        with calls_lock:
            calls.append(kwargs["job_id"])
        entered.set()
        assert release.wait(timeout=15), "test queue lease wait timed out"

    queue = FakeQueue(enqueue=slow_queue)

    def enqueue():
        with outbox.app.app_context():
            local_request = outbox.db.session.get(outbox.models.OutboundRequest, request_id)
            backend_id = outbox.db.session.scalar(text("SELECT pg_backend_pid()"))
            return outbox.service.enqueue_outbound_request(local_request, queue=queue), backend_id

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(enqueue)
        try:
            assert entered.wait(timeout=10), "first queue operation was not reached"
            second = workers.submit(enqueue).result(timeout=10)
            assert second[0] is False
        finally:
            release.set()
        first_result = first.result(timeout=10)
    assert first_result[0] is True
    assert first_result[1] != second[1]
    assert calls == [outbox.service.request_job_id(request)]
    outbox.db.session.refresh(request)
    assert request.status == "queued" and request.enqueue_attempts == 1
    assert request.enqueue_lease is None and request.enqueue_lease_until is None
