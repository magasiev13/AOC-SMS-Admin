"""Unit tests for recipient filtering normalization contract.

Run with: python -m unittest tests.test_recipient_service
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
from app.utils import normalize_phone


def normalize_recipients(recipients: list[dict]) -> list[dict]:
    """Normalize recipient phones before filtering.

    Recipient filters expect E.164-normalized phone values, so normalize mixed
    formatting here before calling filter_* helpers.
    """
    normalized = []
    for recipient in recipients:
        phone = recipient.get("phone")
        normalized_phone = normalize_phone(phone) if phone else phone
        normalized.append({**recipient, "phone": normalized_phone})
    return normalized


class TestRecipientFilteringNormalization(unittest.TestCase):
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

    def test_filters_respect_normalized_mixed_formatting(self) -> None:
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
                source_message_log_id=401,
            )
        )
        db.session.commit()

        recipients = [
            {"name": "Alex", "phone": "(720) 555-0102"},
            {"name": "Blair", "phone": "720.555.0103"},
            {"name": "Cory", "phone": "+1 720 555 0104"},
        ]

        recipients = normalize_recipients(recipients)

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
