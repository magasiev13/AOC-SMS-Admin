import importlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class TestAocScheduledLaunchGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self._temp_dir.name) / "aoc-cancellation.db"
        self.record_path = Path(self._temp_dir.name) / "records" / "aoc-launch.json"
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{database_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "aoc-cancellation-test-secret",
                "SAAS_MODE": "1",
                "SCHEDULER_ENABLED": "0",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.aoc_scheduled_cancel import cancel_dispatchable_messages
        from app.aoc_scheduled_guard import build_dispatchable_report
        from app.models import CommunityMember, Organization, ScheduledMessage

        self.db = db
        self.CommunityMember = CommunityMember
        self.Organization = Organization
        self.ScheduledMessage = ScheduledMessage
        self.cancel_dispatchable_messages = cancel_dispatchable_messages
        self.build_dispatchable_report = build_dispatchable_report

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._context = self.app.app_context()
        self._context.push()
        self.db.create_all()

        self.organization = self.Organization(
            name="Armenians of Colorado",
            slug="armenians-of-colorado",
            status="active",
        )
        self.db.session.add(self.organization)
        self.db.session.flush()
        self.db.session.add(
            self.CommunityMember(
                organization_id=self.organization.id,
                name="Pilot Recipient",
                phone="+15550001234",
            )
        )
        for index in range(2):
            self.db.session.add(
                self.ScheduledMessage(
                    organization_id=self.organization.id,
                    scheduled_at=datetime.now(timezone.utc) + timedelta(days=index + 1),
                    message_body=f"AOC launch hold message {index + 1}",
                    target="community",
                    status="pending",
                    test_mode=False,
                )
            )
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._context.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def test_records_and_cancels_exactly_two_dispatchable_messages(self) -> None:
        payload = self.cancel_dispatchable_messages(
            "armenians-of-colorado",
            2,
            self.record_path,
        )

        self.assertEqual(payload["cancellation_state"], "confirmed")
        self.assertEqual(len(payload["scheduled_messages"]), 2)
        self.assertEqual(payload["dispatchable_count_after_cancellation"], 0)
        self.assertTrue(self.record_path.is_file())
        self.assertEqual(stat.S_IMODE(self.record_path.stat().st_mode), 0o600)

        stored_payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(stored_payload["cancellation_state"], "confirmed")
        self.assertEqual(
            self.ScheduledMessage.query.filter_by(
                organization_id=self.organization.id,
                status="cancelled",
            ).count(),
            2,
        )

        report = self.build_dispatchable_report("armenians-of-colorado", self.record_path)
        self.assertEqual(report["dispatchable_count"], 0)
        self.assertTrue(report["record_confirmed"])

    def test_expected_count_mismatch_changes_nothing(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Expected 1 dispatchable AOC messages, found 2"):
            self.cancel_dispatchable_messages(
                "armenians-of-colorado",
                1,
                self.record_path,
            )

        self.assertFalse(self.record_path.exists())
        self.assertEqual(
            self.ScheduledMessage.query.filter_by(
                organization_id=self.organization.id,
                status="pending",
            ).count(),
            2,
        )


if __name__ == "__main__":
    unittest.main()
