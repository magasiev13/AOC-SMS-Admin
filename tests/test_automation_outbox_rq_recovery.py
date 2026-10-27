"""Real process exits around new automatic replies, with owned PG/Redis only.

Opt in with the same exact loopback targets as test_outbound_rq_recovery.
The real RQ worker, ORM, permission checks and durable Twilio attempt run;
only provider construction/messages.create are replaced with a local ledger.
"""
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

# run/test.sh uses pytest importlib mode; subprocesses execute this file as a
# script. Load the shared exact-target fixture by its real path in both modes.
_support_spec = importlib.util.spec_from_file_location(
    "twinevia_outbound_rq_test_support", Path(__file__).with_name("test_outbound_rq_recovery.py"),
)
_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(_support)
REPOSITORY, provider_calls, rq_stack = _support.REPOSITORY, _support.provider_calls, _support.rq_stack


ACCOUNT = "AC" + "0" * 32
SERVICE = "MG" + "0" * 32
PHONE = "+17205550145"
SENDER = "+17205550999"
INBOUND_SID = "SM" + "1" * 32
REPLY = "Saved automatic response survives process interruption."


def _configure_inbound(stack):
    profile = stack.models.OrganizationMessagingProfile.query.filter_by(
        organization_id=stack.organization_id,
    ).one()
    profile.twilio_subaccount_sid = ACCOUNT
    profile.inbound_identity = SENDER
    stack.db.session.add(stack.models.KeywordAutomationRule(
        organization_id=stack.organization_id, keyword="CRASHCHECK",
        response_body=REPLY, is_active=True,
    ))
    stack.app.config["INBOUND_AUTO_REPLY_ENABLED"] = True
    stack.db.session.commit()


def _inbound_payload():
    return {
        "From": PHONE, "To": SENDER, "AccountSid": ACCOUNT,
        "MessagingServiceSid": SERVICE, "MessageSid": INBOUND_SID,
        "Body": "CRASHCHECK",
    }


def _post_inbound(app):
    from twilio.request_validator import RequestValidator

    payload = _inbound_payload()
    url = "http://localhost/webhooks/twilio/inbound"
    signature = RequestValidator(app.config["TWILIO_AUTH_TOKEN"]).compute_signature(url, payload)
    with app.test_client() as client:
        return client.post(
            "/webhooks/twilio/inbound", data=payload,
            headers={"X-Twilio-Signature": signature},
        )


def _process(stack, mode, *, expected_exit=0):
    environment = os.environ.copy()
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    paths = [str(REPOSITORY), str(REPOSITORY / "tests")]
    guard = Path("/tmp/twinevia-audit-safety")
    if guard.is_dir():
        paths.insert(0, str(guard))
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--automation-process", str(stack.ledger), mode],
        cwd=REPOSITORY, env=environment, capture_output=True, text=True, timeout=30,
    )
    (stack.worker_logs / f"automation-{mode}-{uuid4().hex}.log").write_text(
        process.stdout + process.stderr, encoding="utf-8",
    )
    assert process.returncode == expected_exit, process.stdout + process.stderr
    return process


def _assert_one_accepted_reply(stack):
    stack.db.session.expire_all()
    row = stack.models.AutomationReplyOutbox.query.one()
    attempt = stack.models.MessageDispatchAttempt.query.one()
    bubble = stack.models.InboxMessage.query.filter_by(direction="outbound").one()
    calls = provider_calls(stack)
    assert len(calls) == 1
    assert calls[0]["to"] == PHONE and calls[0]["body"] == REPLY
    assert calls[0]["database_backend_pid"] > 0
    assert calls[0]["pid"] != os.getpid()
    assert row.status == "accepted" and row.inbox_message_id == bubble.id
    assert attempt.status == "sent" and attempt.attempt_count == 1
    assert attempt.logical_send_key == row.logical_send_key
    assert attempt.send_kind == "automation_reply"
    assert bubble.body == row.body == REPLY
    assert bubble.delivery_status == "accepted"
    assert bubble.message_sid == attempt.provider_message_sid == calls[0]["sid"]
    assert stack.models.InboxMessage.query.filter_by(direction="inbound").count() == 1
    return row


def _replay_job(stack, row):
    stack.queue.enqueue(
        "app.services.automation_outbox_service.deliver_automation_job",
        stack.organization_id, row.id, job_id="automation-replay-" + uuid4().hex,
    )
    _process(stack, "accept")


def test_real_inbound_process_exit_after_commit_before_enqueue_recovers_and_duplicate_jobs_do_not_resend(
    rq_stack, monkeypatch,
):
    from app.services import automation_outbox_service as outbox

    stack = rq_stack
    _configure_inbound(stack)
    stack.db.session.remove()
    # Exit from the real signed webhook immediately after its inbound + reply
    # transaction commits. No producer call or provider call can have occurred.
    _process(stack, "inbound-commit-exit", expected_exit=87)
    row = stack.models.AutomationReplyOutbox.query.one()
    assert row.status == "pending" and row.inbound_message_sid == INBOUND_SID
    assert stack.models.InboxMessage.query.filter_by(direction="inbound").count() == 1
    assert stack.models.InboxMessage.query.filter_by(direction="outbound").count() == 0
    assert stack.models.MessageDispatchAttempt.query.count() == 0
    assert stack.queue.count == 0 and provider_calls(stack) == []

    monkeypatch.setattr(outbox, "get_queue", lambda: stack.queue)
    assert outbox.recover_automation_replies() == {"seen": 1, "queued": 1, "finalized": 0}
    stack.db.session.refresh(row)
    row.next_enqueue_at = outbox._now() - timedelta(seconds=1)
    stack.db.session.commit()
    # The timer sees the real already-queued job rather than enqueueing another.
    assert outbox.recover_automation_replies() == {"seen": 1, "queued": 0, "finalized": 0}
    assert stack.queue.count == 1
    job_id = f"twinevia-automation-{stack.organization_id}-{row.id}"
    job = stack.queue.fetch_job(job_id)
    assert job is not None and job.args == (stack.organization_id, row.id)
    # Redis can contain duplicate IDs after a lost producer acknowledgement.
    stack.queue.enqueue(job.func_name, *job.args, **job.kwargs, job_id=job.id)
    assert stack.queue.count == 2
    _process(stack, "accept")
    row = _assert_one_accepted_reply(stack)

    assert _post_inbound(stack.app).status_code == 200
    assert stack.models.AutomationReplyOutbox.query.count() == 1
    assert stack.queue.count == 0
    _replay_job(stack, row)
    _assert_one_accepted_reply(stack)


def test_real_worker_exit_after_accepted_attempt_before_bubble_recovers_history_without_provider_or_resend(
    rq_stack, monkeypatch,
):
    from rq.registry import StartedJobRegistry
    from app.services import automation_outbox_service as outbox

    stack = rq_stack
    _configure_inbound(stack)
    monkeypatch.setattr(outbox, "get_queue", lambda: stack.queue)
    assert _post_inbound(stack.app).status_code == 200
    row = stack.models.AutomationReplyOutbox.query.one()
    row_id = row.id
    assert row.status == "queued" and stack.queue.count == 1
    stack.db.session.remove()
    # This is an actual worker process exit, after the attempt committed a SID
    # and before the bubble/outbox transaction can run its first write.
    _process(stack, "accepted-before-bubble-exit", expected_exit=86)
    row = stack.db.session.get(stack.models.AutomationReplyOutbox, row_id)
    attempt = stack.models.MessageDispatchAttempt.query.one()
    assert row.status == "processing" and row.inbox_message_id is None
    assert attempt.status == "sent" and attempt.attempt_count == 1
    assert attempt.provider_message_sid == provider_calls(stack)[0]["sid"]
    assert stack.models.InboxMessage.query.filter_by(direction="outbound").count() == 0
    job_id = f"twinevia-automation-{stack.organization_id}-{row.id}"
    job = stack.queue.fetch_job(job_id)
    assert job.get_status(refresh=True) == "started"

    # Model the passage of lease time without sleeping. RQ's abandoned-job
    # cleanup and the database timer both inspect their real persisted state.
    job.retries_left = 0
    job.save()
    registry = StartedJobRegistry(queue=stack.queue)
    assert job.id in registry.cleanup(timestamp=time.time() + job.timeout + 120)
    assert job.get_status(refresh=True) == "failed"
    row.next_enqueue_at = outbox._now() - timedelta(seconds=1)
    row.worker_lease_until = outbox._now() - timedelta(seconds=1)
    # Later provider/billing unavailability must not strand known acceptance.
    profile = stack.models.OrganizationMessagingProfile.query.one()
    profile.set_provider_status("suspended")
    stack.models.OrganizationSubscription.query.one().status = "inactive"
    stack.db.session.commit()
    def forbid_provider(*args, **kwargs):
        raise AssertionError("Known accepted attempt must recover without constructing a provider.")
    monkeypatch.setattr(outbox, "get_twilio_service", forbid_provider)
    assert outbox.recover_automation_replies() == {"seen": 1, "queued": 0, "finalized": 1}
    row = _assert_one_accepted_reply(stack)
    assert outbox.recover_automation_replies() == {"seen": 0, "queued": 0, "finalized": 0}

    # A delayed duplicate job in a new process must retain the same accepted
    # result even when its provider factory is forbidden there as well.
    stack.queue.enqueue(
        "app.services.automation_outbox_service.deliver_automation_job",
        stack.organization_id, row.id, job_id="late-automation-replay-" + uuid4().hex,
    )
    _process(stack, "forbid-provider")
    _assert_one_accepted_reply(stack)


def _entry(ledger_path, mode):
    from redis import Redis
    from rq import Queue, SimpleWorker
    from sqlalchemy import text
    import app.config
    from app import create_app, db
    from app.services import automation_outbox_service as outbox
    from app.services.twilio_service import TwilioService

    database = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "")
    if not database.startswith("postgresql+psycopg://audit@127.0.0.1:55432/outbox_rq_"):
        raise RuntimeError("Automation subprocess requires its owned loopback PostgreSQL database.")
    if not redis_url.startswith("redis://127.0.0.1:"):
        raise RuntimeError("Automation subprocess requires its owned loopback Redis process.")
    app.config.Config.TESTING = True

    if mode == "inbound-commit-exit":
        app_instance = create_app(run_startup_tasks=False, start_scheduler=False)
        app_instance.config.update(TESTING=True, WTF_CSRF_ENABLED=False, INBOUND_AUTO_REPLY_ENABLED=True)
        def after_inbound_commit(organization_id, inbound_id):
            # A separate connection can see both records, proving commit before
            # this process disappears. The response has not reached the caller.
            from sqlalchemy import create_engine
            engine = create_engine(database)
            with engine.connect() as independent:
                assert independent.scalar(text(
                    "SELECT count(*) FROM automation_reply_outbox WHERE organization_id=:org AND inbound_message_id=:inbound"
                ), {"org": organization_id, "inbound": inbound_id}) == 1
                assert independent.scalar(text(
                    "SELECT count(*) FROM inbox_messages WHERE id=:inbound AND organization_id=:org"
                ), {"org": organization_id, "inbound": inbound_id}) == 1
            engine.dispose()
            os._exit(87)
        with patch.object(outbox, "enqueue_for_inbound", side_effect=after_inbound_commit):
            response = _post_inbound(app_instance)
        raise AssertionError(f"Expected actual process exit after inbound commit; status={response.status_code}")

    ledger = Path(ledger_path)
    def provider_create(**kwargs):
        sid = "SM" + uuid4().hex
        entry = dict(kwargs, sid=sid, pid=os.getpid(), database_backend_pid=db.session.scalar(text("SELECT pg_backend_pid()")))
        with ledger.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return SimpleNamespace(sid=sid, status="queued", account_sid=ACCOUNT, num_segments="1", price=None, price_unit="usd")

    def provider_factory(organization_id):
        if mode == "forbid-provider":
            raise AssertionError("A replay constructed a provider despite committed acceptance.")
        provider = TwilioService.__new__(TwilioService)
        provider.organization_id, provider.account_sid = organization_id, ACCOUNT
        provider.from_number, provider.messaging_service_sid = SENDER, SERVICE
        provider.client = MagicMock()
        provider.client.messages.create.side_effect = provider_create
        return provider

    original_finish = outbox._finish
    def finish_or_exit(row, thread, result, *, status=None):
        if mode == "accepted-before-bubble-exit":
            attempt = outbox._attempt(row)
            assert attempt is not None and attempt.status == "sent" and attempt.provider_message_sid
            os._exit(86)
        return original_finish(row, thread, result, status=status)

    connection = Redis.from_url(redis_url)
    queue = Queue(os.environ["RQ_QUEUE_NAME"], connection=connection)
    with patch.object(outbox, "get_twilio_service", side_effect=provider_factory), patch.object(outbox, "_finish", side_effect=finish_or_exit):
        SimpleWorker([queue], connection=connection, name="automation-worker-" + uuid4().hex).work(
            burst=True, with_scheduler=False, logging_level="WARNING",
        )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--automation-process":
        _entry(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit("Use pytest with the explicit owned PostgreSQL/Redis targets.")
