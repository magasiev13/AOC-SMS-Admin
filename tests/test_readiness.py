import importlib
import os
import tempfile
import unittest
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app.services.readiness_service import _systemd_timers_check


class TestSystemdReadiness(unittest.TestCase):
    def test_required_systemd_timers_must_all_be_active(self) -> None:
        app = Flask(__name__)
        app.config.update(
            READINESS_REQUIRED_SYSTEMD_TIMERS="twinevia-saas-scheduler.timer,twinevia-saas-backup.timer",
            READINESS_SYSTEMCTL_TIMEOUT_SECONDS=5,
        )
        results = (
            CompletedProcess(("systemctl",), 0, stdout="active\n", stderr=""),
            CompletedProcess(("systemctl",), 3, stdout="inactive\n", stderr=""),
        )

        with patch("app.services.readiness_service.subprocess.run", side_effect=results):
            check = _systemd_timers_check(app)

        self.assertFalse(check.ready)
        self.assertIn("twinevia-saas-backup.timer=inactive", check.detail)

    def test_required_systemd_timers_report_ready_when_active(self) -> None:
        app = Flask(__name__)
        app.config.update(
            READINESS_REQUIRED_SYSTEMD_TIMERS="twinevia-saas-scheduler.timer,twinevia-saas-backup.timer",
            READINESS_SYSTEMCTL_TIMEOUT_SECONDS=5,
        )
        result = CompletedProcess(("systemctl",), 0, stdout="active\n", stderr="")

        with patch("app.services.readiness_service.subprocess.run", return_value=result):
            check = _systemd_timers_check(app)

        self.assertTrue(check.ready)
        self.assertIn("twinevia-saas-scheduler.timer", check.detail)


class TestReadinessRoute(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        os.environ["FLASK_DEBUG"] = "1"
        os.environ["FLASK_ENV"] = "development"
        os.environ["SAAS_MODE"] = "1"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._temp_dir.name}/readiness-route.db"

        import app.config

        importlib.reload(app.config)
        from app import create_app

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            READINESS_TOKEN="readiness-route-token-with-32-characters",
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._original_env)
        self._temp_dir.cleanup()

    def test_readiness_route_hides_itself_without_the_token(self) -> None:
        response = self.client.get("/ready")

        self.assertEqual(response.status_code, 404)

    def test_readiness_route_returns_plain_status_without_caching(self) -> None:
        with patch(
            "app.routes.run_readiness_checks",
            return_value=SimpleNamespace(ready=True),
        ):
            response = self.client.get(
                "/ready",
                headers={"X-Twinevia-Readiness-Token": "readiness-route-token-with-32-characters"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "READY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_readiness_route_reports_dependency_failure(self) -> None:
        with patch(
            "app.routes.run_readiness_checks",
            return_value=SimpleNamespace(ready=False),
        ):
            response = self.client.get(
                "/ready",
                headers={"X-Twinevia-Readiness-Token": "readiness-route-token-with-32-characters"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_data(as_text=True), "NOT READY")


if __name__ == "__main__":
    unittest.main()
