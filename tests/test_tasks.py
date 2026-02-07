import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestSendBulkJob(unittest.TestCase):
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

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import MessageLog
        from app.tasks import send_bulk_job

        self.db = db
        self.MessageLog = MessageLog
        self.send_bulk_job = send_bulk_job
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def _create_log(self, *, details: list | None = None) -> int:
        log = self.MessageLog(
            message_body="Test message",
            target="community",
            status="processing",
            total_recipients=0,
            success_count=0,
            failure_count=0,
            details=json.dumps(details or []),
        )
        self.db.session.add(log)
        self.db.session.commit()
        return log.id

    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    def test_generic_send_error_preserves_existing_details(self, mock_get_twilio, mock_process_failure_details) -> None:
        log_id = self._create_log(details=[{"phone": "+15550000001", "success": True, "error": None}])
        recipients = [
            {"phone": "+15550000001", "name": "Already Processed"},
            {"phone": "+15550000002", "name": "Will Fail"},
        ]

        mock_service = MagicMock()
        mock_service.send_bulk.side_effect = ValueError("provider down")
        mock_get_twilio.return_value = mock_service
        mock_process_failure_details.return_value = {}

        self.send_bulk_job(log_id, recipients, "Hello", delay=0)

        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "failed")
        details = json.loads(log.details or "[]")
        self.assertGreaterEqual(len(details), 2)
        self.assertEqual(details[0].get("phone"), "+15550000001")
        self.assertTrue(any(detail.get("error") == "provider down" for detail in details))

    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    def test_post_processing_failure_does_not_mark_successful_send_failed(
        self,
        mock_get_twilio,
        mock_process_failure_details,
    ) -> None:
        log_id = self._create_log(details=[])
        recipients = [{"phone": "+15550000003", "name": "Success"}]

        mock_service = MagicMock()
        mock_service.send_bulk.return_value = {
            "total": 1,
            "success_count": 1,
            "failure_count": 0,
            "details": [{"phone": "+15550000003", "success": True, "error": None}],
        }
        mock_get_twilio.return_value = mock_service
        mock_process_failure_details.side_effect = RuntimeError("post-processing failed")

        self.send_bulk_job(log_id, recipients, "Hello", delay=0)

        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "sent")
        details = json.loads(log.details or "[]")
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].get("phone"), "+15550000003")
        self.assertTrue(details[0].get("success"))


if __name__ == "__main__":
    unittest.main()
