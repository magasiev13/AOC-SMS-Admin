import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestLogsRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, CommunityMember, MessageLog

        self.db = db
        self.AppUser = AppUser
        self.CommunityMember = CommunityMember
        self.MessageLog = MessageLog

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        admin = self.AppUser(
            username="admin",
            phone="+15550000022",
            role="admin",
            must_change_password=False,
        )
        admin.set_password("admin-pass")
        self.db.session.add(admin)
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._app_context.pop()
        self._temp_dir.cleanup()
        if self._original_flask_debug is None:
            os.environ.pop("FLASK_DEBUG", None)
        else:
            os.environ["FLASK_DEBUG"] = self._original_flask_debug
        os.environ.pop("DATABASE_URL", None)

    def _login(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin-pass"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def _create_log(self, details_payload: str) -> int:
        log = self.MessageLog(
            message_body="Test log body",
            target="community",
            status="failed",
            total_recipients=1,
            success_count=0,
            failure_count=1,
            details=details_payload,
        )
        self.db.session.add(log)
        self.db.session.commit()
        return log.id

    def test_log_detail_supports_legacy_object_payload(self) -> None:
        self._login()
        log_id = self._create_log(
            json.dumps(
                {
                    "details": [
                        {
                            "phone": "+15551234567",
                            "success": False,
                            "error": "Carrier rejection",
                        }
                    ]
                }
            )
        )

        response = self.client.get(f"/logs/{log_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("+15551234567", html)
        self.assertIn("Carrier rejection", html)


    def test_log_detail_handles_numeric_phone_values(self) -> None:
        self._login()
        log_id = self._create_log(
            json.dumps(
                [
                    {
                        "phone": 15557654321,
                        "success": False,
                        "error": "Carrier rejection",
                    }
                ]
            )
        )

        response = self.client.get(f"/logs/{log_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Carrier rejection", html)

    def test_log_detail_ignores_non_dict_detail_entries(self) -> None:
        self._login()
        log_id = self._create_log(
            json.dumps(
                [
                    "bad",
                    {
                        "phone": "+15557654321",
                        "success": False,
                        "error": "Temporary failure",
                    },
                    42,
                ]
            )
        )

        response = self.client.get(f"/logs/{log_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("+15557654321", html)
        self.assertIn("Temporary failure", html)

    def test_log_detail_renders_when_details_json_array_is_empty(self) -> None:
        self._login()
        log_id = self._create_log("[]")

        response = self.client.get(f"/logs/{log_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Message Log Details", html)

    def test_dashboard_send_redirects_to_log_detail_for_general_blast(self) -> None:
        self._login()
        self.db.session.add(self.CommunityMember(name="Member", phone="+15551234567"))
        self.db.session.commit()

        mock_queue = MagicMock()
        with patch("app.queue.get_queue", return_value=mock_queue):
            response = self.client.post(
                "/dashboard",
                data={
                    "message_body": "Hello everyone",
                    "target": "community",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Message Log Details", response.get_data(as_text=True))
        mock_queue.enqueue.assert_called_once()

    def test_dashboard_send_redirects_to_log_detail_for_test_mode(self) -> None:
        self._login()
        self.app.config["ADMIN_TEST_PHONE"] = "+15550009999"

        mock_queue = MagicMock()
        with patch("app.queue.get_queue", return_value=mock_queue):
            response = self.client.post(
                "/dashboard",
                data={
                    "message_body": "Test mode message",
                    "target": "community",
                    "test_mode": "on",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Message Log Details", response.get_data(as_text=True))
        mock_queue.enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
