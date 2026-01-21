"""Tests for scheduled message processing.

Run with: pytest tests/test_scheduled_messages.py -v
"""

import os
import unittest
import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app import create_app, db
from app.models import ScheduledMessage, CommunityMember
from app.services.scheduler_service import send_scheduled_messages


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TestScheduledMessageProcessing(unittest.TestCase):
    """Test that pending scheduled messages are picked up and processed."""

    def setUp(self):
        """Create test app and database."""
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        import app.config

        importlib.reload(app.config)
        self.app = create_app(run_startup_tasks=False)
        self.app.config["TESTING"] = True
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        """Clean up database and app context."""
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_pending_message_due_is_picked_up(self):
        """A scheduled message with scheduled_at <= now should transition off pending."""
        # Create a community member as recipient
        member = CommunityMember(name="Test User", phone="+15551234567")
        db.session.add(member)
        
        # Create a scheduled message that is due (scheduled_at in the past)
        past_time = utc_now_naive() - timedelta(minutes=1)
        scheduled = ScheduledMessage(
            message_body="Test message",
            target="community",
            scheduled_at=past_time,
            status="pending",
            test_mode=False,
        )
        db.session.add(scheduled)
        db.session.commit()
        msg_id = scheduled.id

        # Verify initial state
        self.assertEqual(scheduled.status, "pending")

        # Mock Twilio to avoid actual SMS sends
        mock_result = {
            "total": 1,
            "success_count": 1,
            "failure_count": 0,
            "details": [{"phone": "+15551234567", "status": "sent", "sid": "SM123"}],
        }
        with patch("app.services.scheduler_service.get_twilio_service") as mock_twilio:
            mock_service = MagicMock()
            mock_service.send_bulk.return_value = mock_result
            mock_twilio.return_value = mock_service

            # Run the scheduler
            send_scheduled_messages(self.app)

        # Refresh from DB and check status transitioned
        db.session.expire_all()
        updated = db.session.get(ScheduledMessage, msg_id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, "sent", "Message should transition from pending to sent")
        self.assertIsNotNone(updated.sent_at)

    def test_future_message_not_picked_up(self):
        """A scheduled message with scheduled_at > now should remain pending."""
        member = CommunityMember(name="Test User", phone="+15551234567")
        db.session.add(member)
        
        # Create a scheduled message in the future
        future_time = utc_now_naive() + timedelta(hours=1)
        scheduled = ScheduledMessage(
            message_body="Future message",
            target="community",
            scheduled_at=future_time,
            status="pending",
            test_mode=False,
        )
        db.session.add(scheduled)
        db.session.commit()
        msg_id = scheduled.id

        # Run the scheduler
        with patch("app.services.scheduler_service.get_twilio_service"):
            send_scheduled_messages(self.app)

        # Refresh and verify still pending
        db.session.expire_all()
        updated = db.session.get(ScheduledMessage, msg_id)
        self.assertEqual(updated.status, "pending", "Future message should remain pending")

    def test_stuck_processing_marked_failed(self):
        """A message stuck in 'processing' for >10 minutes should be marked failed."""
        # Create a message that has been stuck in processing
        stuck_time = utc_now_naive() - timedelta(minutes=15)
        scheduled = ScheduledMessage(
            message_body="Stuck message",
            target="community",
            scheduled_at=stuck_time,
            status="processing",
            test_mode=False,
        )
        db.session.add(scheduled)
        db.session.commit()
        msg_id = scheduled.id

        # Run the scheduler
        with patch("app.services.scheduler_service.get_twilio_service"):
            send_scheduled_messages(self.app)

        # Refresh and verify marked as failed
        db.session.expire_all()
        updated = db.session.get(ScheduledMessage, msg_id)
        self.assertEqual(updated.status, "failed", "Stuck processing message should be marked failed")
        self.assertIn("timed out", updated.error_message)

    def test_status_transitions_pending_to_processing_to_sent(self):
        """Verify the full status transition: pending -> processing -> sent."""
        member = CommunityMember(name="Test User", phone="+15551234567")
        db.session.add(member)
        
        past_time = utc_now_naive() - timedelta(seconds=30)
        scheduled = ScheduledMessage(
            message_body="Transition test",
            target="community",
            scheduled_at=past_time,
            status="pending",
            test_mode=False,
        )
        db.session.add(scheduled)
        db.session.commit()
        msg_id = scheduled.id

        statuses_seen = []

        def capture_status(*args, **kwargs):
            """Capture status during send_bulk call (when status is 'processing')."""
            db.session.expire_all()
            msg = db.session.get(ScheduledMessage, msg_id)
            statuses_seen.append(msg.status)
            return {
                "total": 1,
                "success_count": 1,
                "failure_count": 0,
                "details": [],
            }

        with patch("app.services.scheduler_service.get_twilio_service") as mock_twilio:
            mock_service = MagicMock()
            mock_service.send_bulk.side_effect = capture_status
            mock_twilio.return_value = mock_service

            send_scheduled_messages(self.app)

        # Verify processing was seen during send
        self.assertIn("processing", statuses_seen, "Status should be 'processing' during send")

        # Verify final status is sent
        db.session.expire_all()
        updated = db.session.get(ScheduledMessage, msg_id)
        self.assertEqual(updated.status, "sent")


if __name__ == "__main__":
    unittest.main()
