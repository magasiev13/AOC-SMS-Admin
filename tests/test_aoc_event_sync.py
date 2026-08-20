import hashlib
import hmac
import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any


class TestAocEventSyncWebhook(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = {
            "FLASK_DEBUG": os.environ.get("FLASK_DEBUG"),
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "SAAS_MODE": os.environ.get("SAAS_MODE"),
        }
        os.environ["FLASK_DEBUG"] = "1"
        os.environ["SAAS_MODE"] = "1"

        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import (
            CommunityMember,
            Event,
            EventRegistration,
            ExternalWebhookDelivery,
            Organization,
            ScheduledMessage,
        )

        self.db = db
        self.CommunityMember = CommunityMember
        self.Event = Event
        self.EventRegistration = EventRegistration
        self.ExternalWebhookDelivery = ExternalWebhookDelivery
        self.Organization = Organization
        self.ScheduledMessage = ScheduledMessage
        self.secret = "test-aoc-secret"
        self.delivery_counter = 0

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            AOC_EVENTS_WEBHOOK_ENABLED=True,
            AOC_EVENTS_WEBHOOK_SECRET=self.secret,
            AOC_EVENTS_WEBHOOK_TOLERANCE_SECONDS=300,
            AOC_EVENTS_ORGANIZATION_SLUG="armenians-of-colorado",
        )
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        self.organization = self.Organization(
            name="Armenians of Colorado",
            slug="armenians-of-colorado",
            status="active",
        )
        self.other_organization = self.Organization(
            name="Other Org",
            slug="other-org",
            status="active",
        )
        self.db.session.add_all([self.organization, self.other_organization])
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._ctx.pop()
        self._temp_dir.cleanup()

        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _event_payload(self) -> dict[str, Any]:
        return {
            "event_id": "54",
            "post_id": "100009085",
            "title": "Vardavar 2026",
            "slug": "vardavar-2026",
            "permalink": "https://armeniansofcolorado.org/events/vardavar-2026/",
            "status": "publish",
            "start_at": "2027-07-12T10:00:00-06:00",
            "end_at": "2027-07-12T14:00:00-06:00",
            "timezone": "America/Denver",
            "modified_at": "2026-06-23T12:00:00-06:00",
            "location": {
                "name": "Aurora Reservoir",
                "address": "5800 S Powhaton Rd",
                "town": "Aurora",
                "state": "CO",
                "postcode": "80016",
                "country": "US",
            },
            "rsvp_enabled": True,
            "capacity": 250,
        }

    def _booking_payload(self, booking_id: str, phone: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": "events_manager",
            "booking_id": booking_id,
            "status": "pending",
            "active": True,
            "sms_consent": True,
            "person_id": "991",
            "name": "Ani Petrosyan",
            "spaces": 2,
            "updated_at": "2026-06-23T12:05:00-06:00",
        }
        if phone is not None:
            payload["phone"] = phone
        return payload

    def _signed_post(
        self,
        payload: dict[str, Any],
        secret: str | None,
        timestamp: int | None,
    ):
        self.delivery_counter += 1
        return self._signed_post_with_delivery(
            payload,
            secret,
            timestamp,
            f"test-delivery-{self.delivery_counter}",
        )

    def _signed_post_with_delivery(
        self,
        payload: dict[str, Any],
        secret: str | None,
        timestamp: int | None,
        delivery_id: str,
    ):
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        request_timestamp = timestamp if timestamp is not None else int(datetime.now(timezone.utc).timestamp())
        signing_secret = secret if secret is not None else self.secret
        signature = hmac.new(
            signing_secret.encode("utf-8"),
            str(request_timestamp).encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        return self.client.post(
            "/webhooks/aoc/events",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-AOC-Timestamp": str(request_timestamp),
                "X-AOC-Signature": f"sha256={signature}",
                "X-AOC-Delivery-ID": delivery_id,
            },
        )

    def test_rejects_bad_signature(self) -> None:
        response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress", "event": self._event_payload()},
            secret="wrong-secret",
            timestamp=None,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.Event.query.count(), 0)

    def test_rejects_stale_timestamp(self) -> None:
        response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress", "event": self._event_payload()},
            secret=None,
            timestamp=1,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.Event.query.count(), 0)

    def test_rejects_malformed_payload(self) -> None:
        response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress"},
            secret=None,
            timestamp=None,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.Event.query.count(), 0)

    def test_reconcile_complete_is_idempotent(self) -> None:
        payload = {"action": "reconcile_complete", "source": "aoc-wordpress"}
        first_response = self._signed_post(payload, secret=None, timestamp=None)
        second_response = self._signed_post(payload, secret=None, timestamp=None)

        self.assertEqual(first_response.status_code, 200)
        self.assertTrue(first_response.get_json()["reconciled"])
        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(second_response.get_json()["reconciled"])
        self.assertEqual(self.Event.query.count(), 0)

    def test_event_upsert_creates_event_and_auto_reminders(self) -> None:
        response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress", "event": self._event_payload()},
            secret=None,
            timestamp=None,
        )

        self.assertEqual(response.status_code, 200)
        event = self.Event.query.one()
        self.assertEqual(event.organization_id, self.organization.id)
        self.assertEqual(event.external_source, "aoc-wordpress")
        self.assertEqual(event.external_event_id, "54")
        self.assertEqual(event.external_post_id, "100009085")
        self.assertEqual(event.title, "Vardavar 2026")
        self.assertEqual(event.date.isoformat(), "2027-07-12")
        self.assertEqual(event.location_name, "Aurora Reservoir")

        scheduled = self.ScheduledMessage.query.order_by(self.ScheduledMessage.automation_kind.asc()).all()
        self.assertEqual(len(scheduled), 4)
        self.assertEqual({message.automation_kind for message in scheduled}, {"invite", "seven_day", "one_day", "day_of"})
        self.assertEqual({message.organization_id for message in scheduled}, {self.organization.id})
        self.assertIsNone(
            next(message for message in scheduled if message.automation_kind == "invite").test_recipient_snapshot_json
        )
        day_of = next(message for message in scheduled if message.automation_kind == "day_of")
        self.assertEqual(day_of.target, "event")
        self.assertEqual(day_of.event_id, event.id)

    def test_duplicate_event_delivery_updates_without_duplicate_reminders(self) -> None:
        payload = {"action": "event_upsert", "source": "aoc-wordpress", "event": self._event_payload()}
        first_response = self._signed_post(payload, secret=None, timestamp=None)
        self.assertEqual(first_response.status_code, 200)

        updated_event = self._event_payload()
        updated_event["title"] = "Vardavar Festival 2026"
        updated_event["modified_at"] = "2026-06-23T12:01:00-06:00"
        second_response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress", "event": updated_event},
            secret=None,
            timestamp=None,
        )

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(self.Event.query.count(), 1)
        self.assertEqual(self.Event.query.one().title, "Vardavar Festival 2026")
        self.assertEqual(self.ScheduledMessage.query.count(), 4)

    def test_delivery_replay_returns_stored_result_without_reapplying_payload(self) -> None:
        payload = {"action": "event_upsert", "source": "aoc-wordpress", "event": self._event_payload()}
        first_response = self._signed_post_with_delivery(
            payload,
            None,
            None,
            "fixed-replay-delivery",
        )
        second_response = self._signed_post_with_delivery(
            payload,
            None,
            None,
            "fixed-replay-delivery",
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.get_json(), first_response.get_json())
        self.assertEqual(self.Event.query.count(), 1)
        self.assertEqual(self.ScheduledMessage.query.count(), 4)
        self.assertEqual(self.ExternalWebhookDelivery.query.count(), 1)

    def test_stale_event_revision_cannot_overwrite_newer_state(self) -> None:
        newer_event = self._event_payload()
        newer_event["title"] = "Newest Vardavar title"
        newer_event["modified_at"] = "2026-06-23T12:01:00-06:00"
        newer_response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress", "event": newer_event},
            secret=None,
            timestamp=None,
        )

        stale_event = self._event_payload()
        stale_event["title"] = "Stale Vardavar title"
        stale_response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress", "event": stale_event},
            secret=None,
            timestamp=None,
        )

        self.assertEqual(newer_response.status_code, 200)
        self.assertEqual(stale_response.status_code, 200)
        self.assertTrue(stale_response.get_json()["ignored"])
        self.assertEqual(stale_response.get_json()["reason"], "stale_source_revision")
        self.assertEqual(self.Event.query.one().title, "Newest Vardavar title")
        self.assertEqual(self.ScheduledMessage.query.count(), 4)

    def test_event_mutation_requires_timezone_aware_source_revision(self) -> None:
        event = self._event_payload()
        event.pop("modified_at")
        missing_response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress", "event": event},
            secret=None,
            timestamp=None,
        )
        event["modified_at"] = "2026-06-23T12:00:00"
        naive_response = self._signed_post(
            {"action": "event_upsert", "source": "aoc-wordpress", "event": event},
            secret=None,
            timestamp=None,
        )

        self.assertEqual(missing_response.status_code, 400)
        self.assertEqual(naive_response.status_code, 400)
        self.assertEqual(self.Event.query.count(), 0)

    def test_booking_upsert_creates_registration_and_community_member(self) -> None:
        booking = self._booking_payload("22", "+1 (720) 555-0123")
        booking["selections"] = [
            {"label": "Fruits and Drinks", "quantity": 1},
            {"label": "Desserts", "quantity": 1},
        ]
        booking["comment"] = "Bringing watermelon"
        response = self._signed_post(
            {
                "action": "booking_upsert",
                "source": "aoc-wordpress",
                "event": self._event_payload(),
                "booking": booking,
            },
            secret=None,
            timestamp=None,
        )

        self.assertEqual(response.status_code, 200)
        registration = self.EventRegistration.query.one()
        self.assertEqual(registration.organization_id, self.organization.id)
        self.assertEqual(registration.external_booking_id, "events_manager:22")
        self.assertEqual(registration.external_booking_status, "pending")
        self.assertEqual(registration.phone, "+17205550123")
        self.assertEqual(registration.booking_spaces, 2)
        self.assertEqual(
            registration.selection_summary,
            "Fruits and Drinks (1); Desserts (1)",
        )
        self.assertEqual(registration.booking_comment, "Bringing watermelon")
        member = self.CommunityMember.query.one()
        self.assertEqual(member.organization_id, self.organization.id)
        self.assertEqual(member.phone, "+17205550123")
        self.assertEqual(member.name, "Ani Petrosyan")

    def test_cancelled_booking_removes_registration_but_preserves_community_member(self) -> None:
        create_response = self._signed_post(
            {
                "action": "booking_upsert",
                "source": "aoc-wordpress",
                "event": self._event_payload(),
                "booking": self._booking_payload("23", "+17205550124"),
            },
            secret=None,
            timestamp=None,
        )
        self.assertEqual(create_response.status_code, 200)
        cancel_booking = self._booking_payload("23", "+17205550124")
        cancel_booking["status"] = "cancelled"
        cancel_booking["active"] = False
        cancel_booking["updated_at"] = "2026-06-23T12:06:00-06:00"

        cancel_response = self._signed_post(
            {
                "action": "booking_upsert",
                "source": "aoc-wordpress",
                "event": self._event_payload(),
                "booking": cancel_booking,
            },
            secret=None,
            timestamp=None,
        )

        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(self.EventRegistration.query.count(), 0)
        self.assertEqual(self.CommunityMember.query.count(), 1)

    def test_missing_phone_returns_warning_without_registration(self) -> None:
        response = self._signed_post(
            {
                "action": "booking_upsert",
                "source": "aoc-wordpress",
                "event": self._event_payload(),
                "booking": self._booking_payload("24", None),
            },
            secret=None,
            timestamp=None,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("did not include a phone number", response.get_json()["warnings"][0])
        self.assertEqual(self.EventRegistration.query.count(), 0)
        self.assertEqual(self.CommunityMember.query.count(), 0)

    def test_invalid_selection_payload_is_rejected(self) -> None:
        booking = self._booking_payload("25", "+17205550125")
        booking["selections"] = [{"label": "Food to Share", "quantity": 0}]
        response = self._signed_post(
            {
                "action": "booking_upsert",
                "source": "aoc-wordpress",
                "event": self._event_payload(),
                "booking": booking,
            },
            secret=None,
            timestamp=None,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.EventRegistration.query.count(), 0)

    def test_wpforms_booking_ids_are_namespaced_and_require_sms_consent(self) -> None:
        booking = self._booking_payload("45", "+17205550145")
        booking["provider"] = "wpforms"
        booking["status"] = "completed"
        booking["sms_consent"] = False

        no_consent_response = self._signed_post(
            {
                "action": "booking_upsert",
                "source": "aoc-wordpress",
                "event": self._event_payload(),
                "booking": booking,
            },
            secret=None,
            timestamp=None,
        )

        self.assertEqual(no_consent_response.status_code, 200)
        self.assertIn("did not include affirmative SMS consent", no_consent_response.get_json()["warnings"][0])
        self.assertEqual(self.EventRegistration.query.count(), 0)
        self.assertEqual(self.CommunityMember.query.count(), 0)

        booking["sms_consent"] = True
        booking["updated_at"] = "2026-06-23T12:06:00-06:00"
        consent_response = self._signed_post(
            {
                "action": "booking_upsert",
                "source": "aoc-wordpress",
                "event": self._event_payload(),
                "booking": booking,
            },
            secret=None,
            timestamp=None,
        )

        self.assertEqual(consent_response.status_code, 200)
        registration = self.EventRegistration.query.one()
        self.assertEqual(registration.external_booking_id, "wpforms:45")
        self.assertEqual(self.CommunityMember.query.count(), 1)
