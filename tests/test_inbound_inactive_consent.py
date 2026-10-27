"""Consent ingestion must survive outbound provider/billing pauses.

Every case uses a fresh database. Setting INBOUND_TEST_POSTGRES_ROOT to the
isolated local audit server opts these same cases into real PostgreSQL.
"""
import importlib
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from unittest.mock import MagicMock, patch


class TestInboundInactiveConsent(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_env = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory(prefix="twinevia-inbound-consent-")
        self.postgres_root = os.environ.get("INBOUND_TEST_POSTGRES_ROOT")
        self.postgres_db_name = None
        if self.postgres_root:
            parsed = urlsplit(self.postgres_root)
            if (
                parsed.hostname != "127.0.0.1"
                or parsed.port != 55432
                or parsed.username != "audit"
                or parsed.path != "/postgres"
            ):
                raise RuntimeError("Inbound PostgreSQL tests accept only the isolated local audit root.")
            import psycopg
            from psycopg import sql

            self.postgres_db_name = "inbound_consent_" + uuid4().hex
            with psycopg.connect(self.postgres_root, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(
                        sql.Identifier(self.postgres_db_name)
                    )
                )
            self.database_url = urlunsplit(
                ("postgresql+psycopg", parsed.netloc, "/" + self.postgres_db_name, "", "")
            )
        else:
            self.database_url = "sqlite:///" + self.tmp.name + "/test.db"

        os.environ.update(
            DATABASE_URL=self.database_url,
            SAAS_MODE="1",
            FLASK_DEBUG="1",
            FLASK_ENV="testing",
            SCHEDULER_ENABLED="0",
            PYTHON_DOTENV_DISABLED="1",
            AUTH_ALERTS_ENABLED="0",
            SECRET_KEY="inbound-consent-local-test-secret",
        )
        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app import models
        from app.tenant import organization_context

        self.db = db
        self.models = models
        self.organization_context = organization_context
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            TWILIO_VALIDATE_INBOUND_SIGNATURE=True,
            TWILIO_AUTH_TOKEN="inbound-consent-fake-token",
            INBOUND_AUTO_REPLY_ENABLED=True,
            TWILIO_BROWSER_FAKE_SENDS=True,
        )
        self.assertEqual(self.app.config["SQLALCHEMY_DATABASE_URI"], self.database_url)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.org = models.Organization(name="Inbound fixture", slug="inbound-fixture", status="active")
        self.subscription = models.OrganizationSubscription(organization=self.org, status="complimentary")
        self.profile = models.OrganizationMessagingProfile(
            organization=self.org,
            provider_mode="platform_managed",
            provider_status="active",
            status="active",
            sender_review_status="approved",
            from_number="+15550009999",
            inbound_identity="+15550009999",
            messaging_service_sid="MGinboundfixture",
            twilio_subaccount_sid="ACinboundfixture",
        )
        db.session.add_all([self.org, self.subscription, self.profile])
        db.session.commit()
        self.org_id = self.org.id
        self.client = self.app.test_client()
        self.phone = "+17205550145"

    def tearDown(self) -> None:
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

    def _pause(self, status="suspended") -> None:
        self.profile.set_provider_status(status)
        self.db.session.commit()

    def _payload(self, body="STOP", sid="SM-inbound-stop", *, phone=None, profile=None):
        profile = profile or self.profile
        return {
            "From": phone or self.phone,
            "To": profile.from_number,
            "MessagingServiceSid": profile.messaging_service_sid,
            "AccountSid": profile.twilio_subaccount_sid,
            "Body": body,
            "MessageSid": sid,
        }

    def _post(self, payload, *, valid_signature=True):
        from twilio.request_validator import RequestValidator

        url = "http://localhost/webhooks/twilio/inbound"
        signature = RequestValidator(self.app.config["TWILIO_AUTH_TOKEN"]).compute_signature(url, payload)
        return self.client.post(
            "/webhooks/twilio/inbound",
            data=payload,
            headers={"X-Twilio-Signature": signature if valid_signature else "invalid-signature"},
        )

    @contextmanager
    def _no_provider(self):
        with patch(
            "app.services.inbox_service.get_twilio_service",
            side_effect=AssertionError("Paused inbound must never request an outbound provider."),
        ) as provider:
            yield
            provider.assert_not_called()

    def test_signed_stop_is_persisted_for_each_inactive_provider_state(self):
        for index, status in enumerate(("pending", "error", "suspended")):
            with self.subTest(status=status):
                self._pause(status)
                phone = "+1720555014" + str(index)
                sid = "SM-inbound-" + status
                with self._no_provider():
                    response = self._post(self._payload(sid=sid, phone=phone))
                self.assertEqual(response.status_code, 200)
                with self.organization_context(self.org_id):
                    self.assertIsNotNone(self.models.UnsubscribedContact.query.filter_by(phone=phone).one_or_none())
                    self.assertEqual(self.models.InboxMessage.query.filter_by(message_sid=sid, direction="inbound").count(), 1)
                    evidence = self.models.RecipientPermissionEvidence.query.filter_by(phone=phone).one()
                    self.assertEqual((evidence.purpose, evidence.decision, evidence.source), ("all", "denied", "inbound_stop"))
                    self.assertEqual(self.models.InboxMessage.query.filter_by(direction="outbound").count(), 0)

    def test_stop_and_duplicate_sid_are_atomic_and_idempotent(self):
        self._pause()
        payload = self._payload()
        with self._no_provider():
            self.assertEqual(self._post(payload).status_code, 200)
            self.assertEqual(self._post(payload).status_code, 200)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.InboxMessage.query.count(), 1)
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 1)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 1)
            self.assertEqual(self.models.InboxThread.query.one().unread_count, 1)

    def test_permission_write_failure_is_retried_without_acknowledged_data_loss(self):
        self._pause()
        payload = self._payload()
        with patch(
            "app.services.recipient_permission_service.record_permission_evidence",
            side_effect=RuntimeError("isolated permission write failure"),
        ), self._no_provider():
            self.assertEqual(self._post(payload).status_code, 500)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.InboxMessage.query.count(), 0)
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 0)
        with self._no_provider():
            self.assertEqual(self._post(payload).status_code, 200)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.InboxMessage.query.count(), 1)
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 1)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 1)

    def test_stop_is_recorded_when_billing_or_organization_is_inactive(self):
        for index, reason in enumerate(("billing", "organization")):
            with self.subTest(reason=reason):
                self.org.status = "suspended" if reason == "organization" else "active"
                self.subscription.status = "past_due" if reason == "billing" else "complimentary"
                self.db.session.commit()
                with self._no_provider():
                    response = self._post(self._payload(sid="SM-stop-" + reason, phone="+1720555020" + str(index)))
                self.assertEqual(response.status_code, 200)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 2)
            self.assertEqual(self.models.InboxMessage.query.filter_by(direction="outbound").count(), 0)

    def test_forged_signature_cannot_write_consent_or_inbox(self):
        self._pause()
        response = self._post(self._payload(), valid_signature=False)
        self.assertEqual(response.status_code, 403)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.InboxMessage.query.count(), 0)
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 0)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)

    def test_unknown_destination_does_not_create_inbox_or_consent(self):
        payload = self._payload()
        payload["To"] = "+15550008888"
        payload["MessagingServiceSid"] = "MGunknown"
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.InboxMessage.query.count(), 0)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)

    def test_invalid_sender_does_not_create_inbox_or_consent(self):
        response = self._post(self._payload(phone="not-a-phone"))
        self.assertEqual(response.status_code, 200)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.InboxMessage.query.count(), 0)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)

    def test_paused_keyword_is_stored_without_reply_or_automation_mutation(self):
        self._pause()
        with self.organization_context(self.org_id):
            rule = self.models.KeywordAutomationRule(keyword="INFO", response_body="Information", is_active=True)
            self.db.session.add(rule)
            self.db.session.commit()
            with self._no_provider():
                self.assertEqual(self._post(self._payload("INFO", "SM-paused-keyword")).status_code, 200)
            self.assertEqual(self.models.InboxMessage.query.filter_by(direction="inbound").count(), 1)
            self.assertEqual(self.models.InboxMessage.query.filter_by(direction="outbound").count(), 0)
            self.assertEqual(self.models.KeywordAutomationRule.query.one().match_count, 0)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)

    def _survey(self):
        survey = self.models.SurveyFlow(name="Consent survey", trigger_keyword="RSVP", is_active=True)
        survey.set_questions(["Your name?", "Guest count?"])
        self.db.session.add(survey)
        self.db.session.commit()
        return survey

    def test_paused_survey_trigger_does_not_start_an_undeliverable_flow(self):
        self._pause()
        with self.organization_context(self.org_id):
            self._survey()
            with self._no_provider():
                self.assertEqual(self._post(self._payload("RSVP", "SM-paused-survey")).status_code, 200)
            self.assertEqual(self.models.InboxMessage.query.filter_by(direction="inbound").count(), 1)
            self.assertEqual(self.models.SurveySession.query.count(), 0)

    def test_paused_survey_answer_is_kept_without_advancing_session(self):
        self._pause()
        with self.organization_context(self.org_id):
            survey = self._survey()
            thread = self.models.InboxThread(phone=self.phone)
            session = self.models.SurveySession(survey=survey, thread=thread, phone=self.phone, status="active", current_question_index=0)
            self.db.session.add_all([thread, session])
            self.db.session.commit()
            with self._no_provider():
                self.assertEqual(self._post(self._payload("Alex", "SM-paused-answer")).status_code, 200)
            self.assertEqual(session.current_question_index, 0)
            self.assertEqual(session.status, "active")
            self.assertEqual(self.models.SurveyResponse.query.count(), 0)
            self.assertEqual(self.models.InboxMessage.query.filter_by(direction="inbound").one().body, "Alex")

    def test_paused_stop_cancels_active_survey(self):
        self._pause()
        with self.organization_context(self.org_id):
            survey = self._survey()
            thread = self.models.InboxThread(phone=self.phone)
            session = self.models.SurveySession(survey=survey, thread=thread, phone=self.phone, status="active", current_question_index=0)
            self.db.session.add_all([thread, session])
            self.db.session.commit()
            with self._no_provider():
                self.assertEqual(self._post(self._payload()).status_code, 200)
            self.assertEqual(session.status, "cancelled")
            self.assertIsNotNone(session.completed_at)

    def test_paused_start_clears_transport_opt_out_without_broadcast_grant(self):
        self._pause()
        with self._no_provider():
            self.assertEqual(self._post(self._payload()).status_code, 200)
            self.assertEqual(self._post(self._payload("START", "SM-inbound-start")).status_code, 200)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 0)
            evidence = self.models.RecipientPermissionEvidence.query.filter_by(source="inbound_start").one()
            self.assertEqual((evidence.purpose, evidence.decision), ("all", "unknown"))
            self.assertEqual(self.models.RecipientPermissionEvidence.query.filter_by(decision="granted").count(), 0)
            self.assertEqual(self.models.InboxMessage.query.filter_by(direction="outbound").count(), 0)

    def test_paused_yes_during_survey_does_not_clear_an_opt_out(self):
        self._pause()
        with self.organization_context(self.org_id):
            survey = self._survey()
            thread = self.models.InboxThread(phone=self.phone)
            session = self.models.SurveySession(survey=survey, thread=thread, phone=self.phone, status="active", current_question_index=0)
            self.db.session.add_all([thread, session, self.models.UnsubscribedContact(phone=self.phone, source="manual")])
            self.db.session.commit()
            with self._no_provider():
                self.assertEqual(self._post(self._payload("YES", "SM-paused-yes")).status_code, 200)
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 1)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)
            self.assertEqual(session.current_question_index, 0)

    def test_provider_resume_does_not_remove_a_received_stop(self):
        self._pause()
        with self._no_provider():
            self.assertEqual(self._post(self._payload()).status_code, 200)
        self.profile.set_provider_status("active")
        self.db.session.commit()
        from app.services.twilio_service import TwilioService

        provider = TwilioService.__new__(TwilioService)
        provider.organization_id = self.org_id
        provider.account_sid = "ACinboundfixture"
        provider.from_number = self.profile.from_number
        provider.messaging_service_sid = self.profile.messaging_service_sid
        provider.client = MagicMock()
        with self.organization_context(self.org_id):
            result = provider.send_message(self.phone, "Blocked follow-up", send_kind="manual_reply")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "skipped")
        provider.client.messages.create.assert_not_called()

    def test_stop_for_shared_phone_remains_tenant_local(self):
        other = self.models.Organization(name="Other fixture", slug="other-fixture", status="active")
        self.db.session.add(other)
        self.db.session.commit()
        with self.organization_context(other.id):
            self.db.session.add(self.models.CommunityMember(phone=self.phone, name="Other tenant contact"))
            self.db.session.commit()
        self._pause()
        with self._no_provider():
            self.assertEqual(self._post(self._payload()).status_code, 200)
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 1)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 1)
        with self.organization_context(other.id):
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 0)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)
            self.assertEqual(self.models.InboxMessage.query.count(), 0)
            self.assertEqual(self.models.CommunityMember.query.one().name, "Other tenant contact")

    def test_postgres_concurrent_signed_stop_replay_is_exactly_once(self):
        if not self.postgres_root:
            self.skipTest("Concurrent inbound replay requires the isolated PostgreSQL audit root.")
        from app.services import inbox_service
        from twilio.request_validator import RequestValidator

        self._pause()
        payload = self._payload(sid="SM-concurrent-inactive-stop")
        signature = RequestValidator(self.app.config["TWILIO_AUTH_TOKEN"]).compute_signature(
            "http://localhost/webhooks/twilio/inbound", payload,
        )
        barrier = threading.Barrier(2)
        results, errors = [], []
        get_thread = inbox_service._get_or_create_thread

        def synchronized_thread(*args, **kwargs):
            thread = get_thread(*args, **kwargs)
            barrier.wait(timeout=5)
            return thread

        def deliver():
            try:
                with self.app.test_client() as client:
                    results.append(client.post(
                        "/webhooks/twilio/inbound", data=payload,
                        headers={"X-Twilio-Signature": signature},
                    ).status_code)
            except BaseException as error:
                errors.append(error)

        self.db.session.rollback()
        workers = [threading.Thread(target=deliver, name=f"inbound-stop-replay-{index}") for index in range(2)]
        with self._no_provider(), patch.object(inbox_service, "_get_or_create_thread", side_effect=synchronized_thread):
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [200, 200])
        self.db.session.expire_all()
        with self.organization_context(self.org_id):
            self.assertEqual(self.models.InboxThread.query.count(), 1)
            self.assertEqual(self.models.InboxMessage.query.count(), 1)
            self.assertEqual(self.models.UnsubscribedContact.query.count(), 1)
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 1)
            self.assertEqual(self.models.InboxThread.query.one().unread_count, 1)


if __name__ == "__main__":
    unittest.main()
