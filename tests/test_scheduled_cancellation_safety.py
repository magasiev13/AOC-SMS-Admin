"""Cancellation must linearize with provider submissions and preserve outcomes.

The default cases use a fresh SQLite compatibility database. Set
CANCELLATION_TEST_POSTGRES_ROOT to the dedicated local audit root to exercise
the same cases and the explicit multi-session PostgreSQL lock barriers.
"""
import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from unittest.mock import MagicMock, patch


class TestScheduledCancellationSafety(unittest.TestCase):
    def setUp(self):
        self.saved_env = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory(prefix="twinevia-schedule-cancellation-")
        self.postgres_root = os.environ.get("CANCELLATION_TEST_POSTGRES_ROOT")
        self.postgres_db_name = None
        if self.postgres_root:
            parsed = urlsplit(self.postgres_root)
            if (
                parsed.hostname != "127.0.0.1" or parsed.port != 55432
                or parsed.username != "audit" or parsed.path != "/postgres"
            ):
                raise RuntimeError("Cancellation tests accept only the isolated local PostgreSQL audit root.")
            import psycopg
            from psycopg import sql

            self.postgres_db_name = "schedule_cancel_" + uuid4().hex
            with psycopg.connect(self.postgres_root, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(
                        sql.Identifier(self.postgres_db_name)
                    )
                )
            self.database_url = urlunsplit(("postgresql+psycopg", parsed.netloc, "/" + self.postgres_db_name, "", ""))
        else:
            self.database_url = "sqlite:///" + self.tmp.name + "/test.db"
        os.environ.update(
            DATABASE_URL=self.database_url, SAAS_MODE="1", FLASK_DEBUG="1",
            FLASK_ENV="testing", SCHEDULER_ENABLED="0", AUTH_ALERTS_ENABLED="0",
            PYTHON_DOTENV_DISABLED="1", SECRET_KEY="schedule-cancellation-local-test-secret",
        )
        import app.config

        importlib.reload(app.config)
        from app import create_app, db, models
        from app.tenant import organization_context

        self.db, self.models = db, models
        self.organization_context = organization_context
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True, WTF_CSRF_ENABLED=False,
            TWILIO_BROWSER_FAKE_SENDS=False,
            # All provider calls below stop at an in-memory mock. The socket
            # guard additionally forbids reaching any external provider.
            TWILIO_ALLOW_LIVE_SENDS_IN_TESTING=True,
        )
        self.assertEqual(self.app.config["SQLALCHEMY_DATABASE_URI"], self.database_url)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.org = models.Organization(name="Cancellation fixture", slug="cancellation-fixture", status="active")
        subscription = models.OrganizationSubscription(organization=self.org, status="complimentary")
        profile = models.OrganizationMessagingProfile(
            organization=self.org, provider_mode="platform_managed",
            provider_status="active", status="active", sender_review_status="approved",
            from_number="+15550009999", inbound_identity="+15550009999",
            messaging_service_sid="MGcancellationfixture", twilio_subaccount_sid="ACcancellationfixture",
        )
        self.owner = models.AppUser(
            username="cancel-owner", email="owner@cancel.example.test", full_name="Cancellation Owner",
            phone="+17205550000", password_hash="local-test-no-login", role="admin",
            must_change_password=False,
        )
        self.owner.set_password("Cancellation-test-owner1!")
        db.session.add_all([self.org, subscription, profile, self.owner])
        db.session.flush()
        db.session.add(models.OrganizationMembership(organization_id=self.org.id, user_id=self.owner.id, role="owner"))
        for policy, config_key in (
            ("terms", "TERMS_POLICY_VERSION"), ("privacy", "PRIVACY_POLICY_VERSION"),
            ("acceptable_use", "ACCEPTABLE_USE_POLICY_VERSION"), ("sms", "SMS_POLICY_VERSION"),
            ("billing", "BILLING_POLICY_VERSION"),
        ):
            db.session.add(models.CustomerPolicyAcceptance(
                organization_id=self.org.id, user_id=self.owner.id, policy_name=policy,
                policy_version=self.app.config[config_key], accepted_ip="127.0.0.1",
            ))
        db.session.commit()
        self.org_id = self.org.id
        self.client = self.app.test_client()
        response = self.client.post("/login", data={
            "username": self.owner.email, "password": "Cancellation-test-owner1!",
        })
        self.assertEqual(response.status_code, 302)
        self.phones = ["+17205550145", "+17205550146", "+17205550147"]

    def tearDown(self):
        self.db.session.remove()
        self.db.engine.dispose()
        self.context.pop()
        if self.postgres_db_name:
            import psycopg
            from psycopg import sql

            with psycopg.connect(self.postgres_root, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.postgres_db_name)))
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self.saved_env)

    def _schedule(self, *, status="pending", phones=None, test_mode=False, grant=True):
        from app.services.recipient_permission_service import record_permission_evidence

        phones = self.phones[:2] if phones is None else phones
        scheduled = self.models.ScheduledMessage(
            organization_id=self.org_id, message_body="Hello {name}", target="community",
            scheduled_at=self.models.utc_now().replace(tzinfo=None) - timedelta(minutes=1),
            status=status, test_mode=test_mode,
            test_recipient_snapshot_json=json.dumps([{"phone": phone, "name": "Fixture"} for phone in phones]),
            processing_started_at=self.models.utc_now() if status == "processing" else None,
            attempt_count=1 if status == "processing" else 0,
        )
        self.db.session.add(scheduled)
        if grant:
            with self.organization_context(self.org_id):
                for phone in phones:
                    record_permission_evidence(
                        self.org_id, phone, purpose="community", decision="granted",
                        source="owner_verified", evidence_key="fixture:" + phone, verified=True,
                        metadata={"proof": "Isolated test fixture explicit consent"},
                    )
        self.db.session.commit()
        return scheduled.id

    def _provider(self):
        from app.services.twilio_service import TwilioService

        service = TwilioService.__new__(TwilioService)
        service.organization_id = self.org_id
        service.account_sid = "ACcancellationfixture"
        service.from_number = "+15550009999"
        service.messaging_service_sid = "MGcancellationfixture"
        service.client = MagicMock()
        service.client.messages.create.side_effect = lambda **params: SimpleNamespace(
            sid="SM" + params["to"][-8:], status="queued", account_sid=service.account_sid,
            num_segments="1", price=None, price_unit="usd",
        )
        return service

    def _cancel(self, scheduled_id):
        from app.services.scheduled_cancellation_service import apply_scheduled_cancellation, lock_scheduled_message

        with self.organization_context(self.org_id):
            scheduled = lock_scheduled_message(scheduled_id, self.org_id)
            accepted = apply_scheduled_cancellation(scheduled)
            self.db.session.commit()
        return accepted

    def _run(self, service):
        from app.services.scheduler_service import send_scheduled_messages

        with patch("app.services.scheduler_service.get_twilio_service", return_value=service):
            send_scheduled_messages(self.app)
        self.db.session.expire_all()

    def _state(self, scheduled_id):
        self.db.session.expire_all()
        with self.organization_context(self.org_id):
            scheduled = self.db.session.get(self.models.ScheduledMessage, scheduled_id)
            log = self.db.session.get(self.models.MessageLog, scheduled.message_log_id) if scheduled.message_log_id else None
            return scheduled, log

    def test_cancel_before_first_provider_submission_sends_nothing(self):
        scheduled_id = self._schedule()
        service = self._provider()
        from app.services.scheduler_service import send_scheduled_messages

        def cancel_before_provider(_org_id):
            self.assertTrue(self._cancel(scheduled_id))
            return service

        with patch("app.services.scheduler_service.get_twilio_service", side_effect=cancel_before_provider):
            send_scheduled_messages(self.app)
        scheduled, log = self._state(scheduled_id)
        service.client.messages.create.assert_not_called()
        self.assertEqual(scheduled.status, "cancelled")
        self.assertEqual((log.total_recipients, log.success_count, log.failure_count, log.status), (2, 0, 0, "cancelled"))
        self.assertIn("2 not submitted", scheduled.error_message)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.MessageDispatchAttempt.query.count(), 0)

    def test_cancel_between_recipients_retains_accepted_count_and_stops_rest(self):
        scheduled_id = self._schedule()
        service = self._provider()
        with patch("app.services.twilio_service.time.sleep", side_effect=lambda _: self._cancel(scheduled_id)):
            self._run(service)
        scheduled, log = self._state(scheduled_id)
        self.assertEqual(service.client.messages.create.call_count, 1)
        self.assertEqual(scheduled.status, "cancelled")
        self.assertEqual((log.total_recipients, log.success_count, log.failure_count, log.status), (2, 1, 0, "cancelled"))
        self.assertIn("1 provider-accepted; 0 unresolved; 1 not submitted", scheduled.error_message)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.MessageDispatchAttempt.query.count(), 1)
            self.assertEqual(self.models.MessageDispatchAttempt.query.one().status, "sent")

    def test_cancel_after_ambiguous_first_submission_retains_unresolved_outcome(self):
        scheduled_id = self._schedule()
        service = self._provider()
        service.client.messages.create.side_effect = TimeoutError("isolated response lost after submission")
        with patch("app.services.twilio_service.time.sleep", side_effect=lambda _: self._cancel(scheduled_id)):
            self._run(service)
        scheduled, log = self._state(scheduled_id)
        self.assertEqual(service.client.messages.create.call_count, 1)
        self.assertEqual((scheduled.status, log.status, log.success_count, log.failure_count), ("cancelled", "cancelled", 0, 1))
        self.assertIn("0 provider-accepted; 1 unresolved; 1 not submitted", scheduled.error_message)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.MessageDispatchAttempt.query.one().status, "ambiguous")
        self._run(service)
        self.assertEqual(service.client.messages.create.call_count, 1)

    def test_cancellation_during_finalization_is_not_overwritten_as_sent(self):
        scheduled_id = self._schedule(phones=self.phones[:1])
        service = self._provider()
        original = service.send_bulk_with_attempts

        def cancel_at_return(*args):
            result = original(*args)
            self.assertTrue(self._cancel(scheduled_id))
            return result

        with patch.object(service, "send_bulk_with_attempts", side_effect=cancel_at_return):
            self._run(service)
        scheduled, log = self._state(scheduled_id)
        self.assertEqual((scheduled.status, log.status, log.success_count), ("cancelled", "cancelled", 1))
        self.assertIn("0 not submitted", scheduled.error_message)

    def test_cancellation_between_attempt_claim_commit_and_provider_call_is_rechecked(self):
        from app.services import scheduled_cancellation_service

        scheduled_id = self._schedule(status="processing", phones=self.phones[:1])
        service = self._provider()
        lock = scheduled_cancellation_service.lock_scheduled_message
        call_count = 0

        def intercept_lock(*args):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                self.assertTrue(self._cancel(scheduled_id))
            return lock(*args)

        with self.organization_context(self.org_id), patch.object(
            scheduled_cancellation_service, "lock_scheduled_message", side_effect=intercept_lock,
        ):
            result = service.send_message_with_attempt(
                self.phones[0], "Claim gap", "cancel-at-claim", None, scheduled_id, "blast",
            )
            attempt = self.models.MessageDispatchAttempt.query.one()
            self.assertEqual(attempt.status, "failed")
            self.assertIn("not_submitted", attempt.last_error)
        self.assertTrue(result["cancelled"])
        service.client.messages.create.assert_not_called()

    def test_late_transient_and_permanent_errors_never_requeue_cancellation(self):
        from app.services.twilio_service import TwilioTransientError

        for error in (TwilioTransientError("isolated transient"), RuntimeError("isolated permanent")):
            with self.subTest(error=type(error).__name__):
                scheduled_id = self._schedule(phones=self.phones[:1])
                service = self._provider()

                def cancelled_failure(*_args):
                    self.assertTrue(self._cancel(scheduled_id))
                    raise error

                with patch.object(service, "send_bulk_with_attempts", side_effect=cancelled_failure):
                    self._run(service)
                scheduled, _ = self._state(scheduled_id)
                self.assertEqual(scheduled.status, "cancelled")
                self.assertIsNone(scheduled.next_retry_at)
                self.assertIsNone(scheduled.processing_started_at)
                service.client.messages.create.assert_not_called()

    def test_direct_retry_handler_refreshes_stale_processing_object(self):
        from app.services.scheduler_service import _handle_transient_failure

        scheduled_id = self._schedule(status="processing")
        with self.organization_context(self.org_id):
            stale = self.db.session.get(self.models.ScheduledMessage, scheduled_id)
            self.assertTrue(self._cancel(scheduled_id))
            # Simulate an identity-map object still holding the old value. The
            # helper must not autoflush that value over persisted cancellation.
            stale.status = "processing"
            result = _handle_transient_failure(
                scheduled=stale, error=TimeoutError("isolated"), now=self.models.utc_now().replace(tzinfo=None),
                max_retries=3, base_backoff_seconds=60, max_backoff_seconds=900,
                send_fingerprint=None, unique_recipient_count=2, duplicate_count=0, db=self.db,
            )
        self.assertIsNone(result)
        self.assertEqual(self._state(scheduled_id)[0].status, "cancelled")

    def test_legacy_compatibility_cancellation_stops_bulk_and_preserves_partial_log(self):
        self.app.config["SAAS_MODE"] = False
        scheduled_id = self._schedule()
        service = self._provider()
        with patch("app.services.twilio_service.time.sleep", side_effect=lambda _: self._cancel(scheduled_id)):
            self._run(service)
        scheduled, log = self._state(scheduled_id)
        self.assertEqual(service.client.messages.create.call_count, 1)
        self.assertEqual((scheduled.status, log.status, log.success_count, log.total_recipients), ("cancelled", "cancelled", 1, 2))

    def test_unknown_permission_is_excluded_before_scheduler_provider_access(self):
        scheduled_id = self._schedule(grant=False)
        service = self._provider()
        self._run(service)
        scheduled, _ = self._state(scheduled_id)
        self.assertEqual(scheduled.status, "failed")
        self.assertIn("permissions", scheduled.error_message)
        service.client.messages.create.assert_not_called()

    def test_scheduled_test_uses_current_allowlist_without_widening_snapshot(self):
        scheduled_id = self._schedule(test_mode=True, grant=False)
        self.db.session.add_all([
            self.models.OrganizationTestRecipient(organization_id=self.org_id, phone=self.phones[0]),
            self.models.OrganizationTestRecipient(organization_id=self.org_id, phone=self.phones[2]),
        ])
        self.db.session.commit()
        service = self._provider()
        self._run(service)
        scheduled, log = self._state(scheduled_id)
        self.assertEqual(scheduled.status, "sent")
        self.assertEqual((log.total_recipients, log.success_count), (1, 1))
        service.client.messages.create.assert_called_once()
        self.assertEqual(service.client.messages.create.call_args.kwargs["to"], self.phones[0])
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.MessageDispatchAttempt.query.one().send_kind, "manual_live_test")

    def test_saved_test_recipient_still_obeys_stop_and_suppression(self):
        scheduled_id = self._schedule(test_mode=True, grant=False)
        self.db.session.add_all([
            self.models.OrganizationTestRecipient(organization_id=self.org_id, phone=phone)
            for phone in self.phones[:2]
        ])
        self.db.session.add(self.models.UnsubscribedContact(organization_id=self.org_id, phone=self.phones[0]))
        self.db.session.add(self.models.SuppressedContact(
            organization_id=self.org_id, phone=self.phones[1], reason="local test suppression", category="hard_fail",
        ))
        self.db.session.commit()
        service = self._provider()
        self._run(service)
        self.assertEqual(self._state(scheduled_id)[0].status, "failed")
        service.client.messages.create.assert_not_called()

    def test_owner_cancel_route_and_bulk_cancel_are_tenant_scoped(self):
        scheduled_id = self._schedule()
        other_org = self.models.Organization(name="Other workspace", slug="other-cancel", status="active")
        self.db.session.add(other_org)
        self.db.session.flush()
        other = self.models.ScheduledMessage(
            organization_id=other_org.id, message_body="Other message", target="community",
            scheduled_at=self.models.utc_now(), status="pending",
        )
        self.db.session.add(other)
        self.db.session.commit()
        other_id = other.id
        denied = self.client.post(f"/scheduled/{other_id}/cancel")
        self.assertEqual(denied.status_code, 404, denied.headers.get("Location"))
        response = self.client.post("/scheduled/bulk-cancel", data={"scheduled_ids": f"{scheduled_id},{other_id}"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._state(scheduled_id)[0].status, "cancelled")
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(self.models.ScheduledMessage, other_id).status, "pending")

    def test_staff_cannot_cancel_and_active_or_dispatch_history_cannot_be_deleted(self):
        scheduled_id = self._schedule()
        membership = self.models.OrganizationMembership.query.filter_by(user_id=self.owner.id).one()
        membership.role = "staff"
        self.db.session.commit()
        denied = self.client.post(f"/scheduled/{scheduled_id}/cancel")
        self.assertEqual(denied.status_code, 403, denied.headers.get("Location"))
        membership.role = "owner"
        self.db.session.commit()
        self.assertEqual(self.client.post(f"/scheduled/{scheduled_id}/delete").status_code, 302)
        self.assertIsNotNone(self._state(scheduled_id)[0])
        self._cancel(scheduled_id)
        self.db.session.add(self.models.MessageDispatchAttempt(
            organization_id=self.org_id, scheduled_message_id=scheduled_id, logical_send_key="retained-test-attempt",
            recipient_phone=self.phones[0], message_body_digest="a" * 64, send_kind="blast", status="sent",
        ))
        self.db.session.commit()
        self.assertEqual(self.client.post(f"/scheduled/{scheduled_id}/delete").status_code, 302)
        self.assertIsNotNone(self._state(scheduled_id)[0])
        disposable_id = self._schedule(status="cancelled", phones=[])
        self.assertEqual(self.client.post("/scheduled/bulk-delete", data={"scheduled_ids": f"{scheduled_id},{disposable_id}"}).status_code, 302)
        self.db.session.expire_all()
        self.assertIsNone(self.db.session.get(self.models.ScheduledMessage, disposable_id))
        self.assertIsNotNone(self.db.session.get(self.models.ScheduledMessage, scheduled_id))

    def test_schedule_templates_show_partial_summary_and_available_controls(self):
        scheduled_id = self._schedule()
        service = self._provider()
        with patch("app.services.twilio_service.time.sleep", side_effect=lambda _: self._cancel(scheduled_id)):
            self._run(service)
        processing_id = self._schedule(status="processing", phones=[])
        response = self.client.get("/scheduled")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertGreaterEqual(body.count("1 provider-accepted; 0 unresolved; 1 not submitted"), 3)
        self.assertIn(f'data-form-id="cancel-scheduled-{processing_id}"', body)
        self.assertNotIn(f'data-form-id="delete-scheduled-{processing_id}"', body)
        self.assertNotIn(f'data-form-id="delete-scheduled-{scheduled_id}"', body)
        self.assertNotIn(f'id="past-select-{scheduled_id}"', body)
        self.assertIn("History retained", body)

    def test_log_templates_do_not_present_ambiguous_or_cancelled_as_success(self):
        for status in ("ambiguous", "cancelled"):
            with self.subTest(status=status):
                log = self.models.MessageLog(
                    organization_id=self.org_id, message_body="Uncertain result", target="community",
                    status=status, total_recipients=1, success_count=0, failure_count=0,
                    details=json.dumps([{"phone": self.phones[0], "status": "ambiguous", "success": False}]),
                )
                self.db.session.add(log)
                self.db.session.commit()
                expected = "Acceptance unresolved" if status == "ambiguous" else "Cancelled"
                for path in ("/logs", f"/logs/{log.id}"):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    body = response.get_data(as_text=True)
                    self.assertIn(expected, body)
                    self.assertNotIn(f'bg-success">\n                                    {status}', body)

    def test_postgres_cancel_waits_for_inflight_then_stops_next_submission(self):
        self._assert_postgres_cancel_waits_for_inflight()

    def test_postgres_cancel_after_ambiguous_inflight_preserves_unresolved_count(self):
        self._assert_postgres_cancel_waits_for_inflight(TimeoutError("isolated provider response lost"))

    def _assert_postgres_cancel_waits_for_inflight(self, provider_error=None):
        if not self.postgres_root:
            self.skipTest("Explicit PostgreSQL multi-session lock barrier requires the isolated audit root.")
        import psycopg
        from sqlalchemy import text
        from app.services.scheduler_service import send_scheduled_messages

        scheduled_id = self._schedule()
        service = self._provider()
        entered_provider = threading.Event()
        release_provider = threading.Event()
        cancel_started = threading.Event()
        cancel_done = threading.Event()
        cancel_backend_pid = []
        errors = []
        outcomes = []
        original_create = service.client.messages.create.side_effect

        def inflight_provider(**params):
            entered_provider.set()
            if not release_provider.wait(8):
                raise TimeoutError("Test barrier was not released")
            if provider_error is not None:
                raise provider_error
            return original_create(**params)

        def worker():
            try:
                send_scheduled_messages(self.app)
            except BaseException as error:
                errors.append(error)

        def cancel_worker():
            try:
                with self.app.app_context(), self.organization_context(self.org_id):
                    cancel_backend_pid.append(self.db.session.execute(text("SELECT pg_backend_pid()")).scalar_one())
                    cancel_started.set()
                    outcomes.append(self._cancel(scheduled_id))
                    cancel_done.set()
            except BaseException as error:
                errors.append(error)

        service.client.messages.create.side_effect = inflight_provider
        self.db.session.rollback()
        sender = threading.Thread(target=worker, name="scheduled-test-sender")
        canceller = threading.Thread(target=cancel_worker, name="scheduled-test-canceller")
        with patch("app.services.scheduler_service.get_twilio_service", return_value=service):
            sender.start()
            try:
                self.assertTrue(entered_provider.wait(8), "Worker never reached the mocked provider.")
                canceller.start()
                self.assertTrue(cancel_started.wait(3))
                observed_wait = False
                last_observed = None
                deadline = time.monotonic() + 4
                with psycopg.connect(self.postgres_root, autocommit=True, client_encoding="UTF8") as observer:
                    while time.monotonic() < deadline:
                        row = observer.execute(
                            "SELECT wait_event_type, cardinality(pg_blocking_pids(pid)), state, query FROM pg_stat_activity "
                            "WHERE pid = %s",
                            (cancel_backend_pid[0],),
                        ).fetchone()
                        last_observed = row
                        if row and row[0] == "Lock" and row[1] > 0:
                            observed_wait = True
                            break
                        time.sleep(0.01)
                self.assertTrue(observed_wait, f"Cancellation did not wait on the in-flight schedule row lock: {last_observed}; errors={errors}")
                self.assertFalse(cancel_done.is_set(), "Cancellation acknowledged while a provider request was in flight.")
            finally:
                release_provider.set()
                sender.join(10)
                if canceller.ident:
                    canceller.join(10)
        self.assertFalse(sender.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, [True])
        self.assertTrue(cancel_done.is_set())
        self.assertEqual(service.client.messages.create.call_count, 1)
        scheduled, log = self._state(scheduled_id)
        self.assertEqual((scheduled.status, log.status, log.success_count, log.total_recipients), (
            "cancelled", "cancelled", 0 if provider_error is not None else 1, 2,
        ))
        if provider_error is not None:
            self.assertIn("1 unresolved", scheduled.error_message)
        self.assertIn("1 not submitted", scheduled.error_message)

    def test_postgres_watchdog_skips_locked_inflight_submission(self):
        if not self.postgres_root:
            self.skipTest("Explicit PostgreSQL multi-session lock barrier requires the isolated audit root.")
        from app.services.scheduler_service import send_scheduled_messages

        scheduled_id = self._schedule(status="processing", phones=self.phones[:1])
        scheduled = self.db.session.get(self.models.ScheduledMessage, scheduled_id)
        scheduled.processing_started_at = self.models.utc_now() - timedelta(minutes=30)
        self.db.session.commit()
        self.app.config["SCHEDULED_PROCESSING_TIMEOUT_MINUTES"] = 1
        service = self._provider()
        entered_provider, release_provider = threading.Event(), threading.Event()
        watchdog_done = threading.Event()
        errors, outcomes = [], []
        original_create = service.client.messages.create.side_effect

        def inflight_provider(**params):
            entered_provider.set()
            if not release_provider.wait(8):
                raise TimeoutError("Test barrier was not released")
            return original_create(**params)

        def worker():
            try:
                with self.app.app_context(), self.organization_context(self.org_id):
                    outcomes.append(service.send_message_with_attempt(
                        self.phones[0], "Watchdog barrier", "watchdog-barrier", None, scheduled_id, "blast",
                    ))
            except BaseException as error:
                errors.append(error)

        def watchdog():
            try:
                send_scheduled_messages(self.app)
                watchdog_done.set()
            except BaseException as error:
                errors.append(error)

        service.client.messages.create.side_effect = inflight_provider
        self.db.session.rollback()
        sender = threading.Thread(target=worker, name="scheduled-watchdog-test-sender")
        timer = threading.Thread(target=watchdog, name="scheduled-watchdog-test-timer")
        sender.start()
        try:
            self.assertTrue(entered_provider.wait(8))
            timer.start()
            self.assertTrue(watchdog_done.wait(3), "Watchdog blocked on or claimed an in-flight locked schedule.")
        finally:
            release_provider.set()
            sender.join(10)
            if timer.ident:
                timer.join(10)
        self.assertEqual(errors, [])
        self.assertFalse(sender.is_alive())
        self.assertFalse(timer.is_alive())
        self.assertTrue(outcomes[0]["success"])
        self.assertEqual(self._state(scheduled_id)[0].status, "processing")
