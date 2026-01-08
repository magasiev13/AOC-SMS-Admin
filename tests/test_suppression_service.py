"""Unit tests for suppression and recipient filtering services.

Run with: python -m unittest tests.test_suppression_service
"""

import importlib
import os
import sys
import tempfile
import unittest

from app.services.recipient_service import (
    filter_suppressed_recipients,
    filter_unsubscribed_recipients,
)
from app.services.suppression_service import classify_failure, process_failure_details


class TestSuppressionService(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "sms.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SCHEDULER_ENABLED": "0",
            }
        )

        if "app.config" in sys.modules:
            import app.config

            importlib.reload(app.config)

        from app import create_app, db

        self._db = db
        self._app = create_app(run_startup_tasks=False, start_scheduler=False)
        self._app.config["TESTING"] = True
        self._ctx = self._app.app_context()
        self._ctx.push()
        self._db.create_all()

    def tearDown(self) -> None:
        self._db.session.remove()
        self._db.drop_all()
        self._ctx.pop()
        os.environ.clear()
        os.environ.update(self._original_env)
        self._temp_dir.cleanup()

    def test_classify_failure_patterns(self) -> None:
        self.assertEqual(classify_failure("Reply STOP to opt out"), "opt_out")
        self.assertEqual(classify_failure("Carrier violation: 30005"), "hard_fail")
        self.assertEqual(classify_failure("Service unavailable: 503"), "soft_fail")

    def test_unsubscribed_contact_upsert_is_idempotent(self) -> None:
        details = [
            {
                "success": False,
                "status": "failed",
                "error": "User has unsubscribed",
                "phone": "+17205550100",
                "name": "Casey",
            }
        ]

        process_failure_details(details, source_message_log_id=101)

        updated_details = [
            {
                "success": False,
                "status": "failed",
                "error": "Reply STOP",
                "phone": "+17205550100",
            }
        ]

        process_failure_details(updated_details, source_message_log_id=102)

        from app.models import UnsubscribedContact

        entries = UnsubscribedContact.query.all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].reason, "Reply STOP")
        self.assertEqual(entries[0].source, "message_failure")

    def test_suppressed_contact_upsert_is_idempotent(self) -> None:
        details = [
            {
                "success": False,
                "status": "failed",
                "error": "Invalid number",
                "phone": "+17205550101",
            }
        ]

        process_failure_details(details, source_message_log_id=201)

        updated_details = [
            {
                "success": False,
                "status": "failed",
                "error": "Number is not valid",
                "phone": "+17205550101",
            }
        ]

        process_failure_details(updated_details, source_message_log_id=202)

        from app.models import SuppressedContact

        entries = SuppressedContact.query.all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].reason, "Number is not valid")
        self.assertEqual(entries[0].source, "message_failure")
        self.assertEqual(entries[0].source_message_log_id, 202)
        self.assertEqual(entries[0].category, "hard_fail")

    def test_recipient_filtering_excludes_unsubscribed_and_suppressed(self) -> None:
        from app import db
        from app.models import SuppressedContact, UnsubscribedContact

        db.session.add(
            UnsubscribedContact(
                name="Alex",
                phone="+17205550102",
                reason="Reply STOP",
                source="manual",
            )
        )
        db.session.add(
            SuppressedContact(
                phone="+17205550103",
                reason="Invalid number",
                category="hard_fail",
                source="message_failure",
                source_type="message_log",
                source_message_log_id=301,
            )
        )
        db.session.commit()

        recipients = [
            {"name": "Alex", "phone": "+17205550102"},
            {"name": "Blair", "phone": "+17205550103"},
            {"name": "Cory", "phone": "+17205550104"},
        ]

        remaining, skipped_unsubscribed, unsubscribed_phones = filter_unsubscribed_recipients(
            recipients
        )
        remaining, skipped_suppressed, suppressed_phones = filter_suppressed_recipients(remaining)

        self.assertEqual([r["phone"] for r in remaining], ["+17205550104"])
        self.assertEqual([r["phone"] for r in skipped_unsubscribed], ["+17205550102"])
        self.assertEqual([r["phone"] for r in skipped_suppressed], ["+17205550103"])
        self.assertEqual(unsubscribed_phones, {"+17205550102"})
        self.assertEqual(suppressed_phones, {"+17205550103"})


if __name__ == "__main__":
    unittest.main()
