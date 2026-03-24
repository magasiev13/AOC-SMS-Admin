import importlib
import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class TestPlatformOperationsService(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = {
            "FLASK_DEBUG": os.environ.get("FLASK_DEBUG"),
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "SAAS_MODE": os.environ.get("SAAS_MODE"),
            "TWILIO_CREDENTIAL_ENCRYPTION_KEY": os.environ.get("TWILIO_CREDENTIAL_ENCRYPTION_KEY"),
        }
        os.environ["FLASK_DEBUG"] = "1"
        os.environ["SAAS_MODE"] = "1"
        os.environ["TWILIO_CREDENTIAL_ENCRYPTION_KEY"] = "4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o="
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, AuthEvent, PlatformServiceRestartRequest
        from app.migrations.runner import run_pending_migrations
        from app.services.platform_operations_service import (
            dispatch_platform_service_restart,
            enqueue_platform_service_restart_request,
            process_platform_service_restart_queue,
            refresh_platform_service_restart_status,
            request_platform_service_restart,
            request_platform_service_restart_status,
        )

        self.db = db
        self.AppUser = AppUser
        self.AuthEvent = AuthEvent
        self.PlatformServiceRestartRequest = PlatformServiceRestartRequest
        self.dispatch_platform_service_restart = dispatch_platform_service_restart
        self.enqueue_platform_service_restart_request = enqueue_platform_service_restart_request
        self.process_platform_service_restart_queue = process_platform_service_restart_queue
        self.refresh_platform_service_restart_status = refresh_platform_service_restart_status
        self.request_platform_service_restart = request_platform_service_restart
        self.request_platform_service_restart_status = request_platform_service_restart_status

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.helper_path = os.path.join(self._temp_dir.name, "restart-helper.sh")
        with open(self.helper_path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.helper_path, 0o755)
        self.app.config.update(
            TESTING=True,
            PLATFORM_SERVICE_RESTART_SCRIPT=self.helper_path,
            PLATFORM_SERVICE_RESTART_TIMEOUT_SECONDS=15,
            PLATFORM_SERVICE_RESTART_STALE_AFTER_SECONDS=300,
        )
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()
        run_pending_migrations(self.db.engine, self.app.logger)

        self.platform_admin = self.AppUser(
            username="platform-admin",
            email="platform@example.test",
            full_name="Platform Admin",
            phone="+15550000009",
            role="admin",
            is_platform_admin=True,
            must_change_password=False,
        )
        self.platform_admin.set_password("Platform-pass1!")
        self.db.session.add(self.platform_admin)
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

    def _create_request(self, *, status: str = "pending", **kwargs):
        request_row = self.PlatformServiceRestartRequest(
            requested_by_user_id=self.platform_admin.id,
            requested_username=self.platform_admin.username,
            client_ip="127.0.0.1",
            status=status,
            summary=kwargs.pop("summary", None),
            detail=kwargs.pop("detail", None),
            transient_unit=kwargs.pop("transient_unit", None),
            started_at=kwargs.pop("started_at", None),
            completed_at=kwargs.pop("completed_at", None),
            last_checked_at=kwargs.pop("last_checked_at", None),
        )
        self.db.session.add(request_row)
        self.db.session.commit()
        return request_row

    def test_enqueue_platform_service_restart_request_creates_pending_row(self) -> None:
        request_row, created = self.enqueue_platform_service_restart_request(
            requested_by_user=self.platform_admin,
            client_ip="127.0.0.1",
        )

        self.assertTrue(created)
        self.assertEqual(request_row.status, "pending")
        self.assertEqual(request_row.requested_by_user_id, self.platform_admin.id)
        self.assertEqual(request_row.requested_username, "platform-admin")
        self.assertEqual(request_row.summary, "Restart request queued. Waiting for the host processor.")

    def test_enqueue_platform_service_restart_request_dedupes_pending_and_queued_rows(self) -> None:
        pending_request, created = self.enqueue_platform_service_restart_request(
            requested_by_user=self.platform_admin,
            client_ip="127.0.0.1",
        )
        self.assertTrue(created)

        duplicate_pending, created_pending = self.enqueue_platform_service_restart_request(
            requested_by_user=self.platform_admin,
            client_ip="127.0.0.1",
        )
        self.assertFalse(created_pending)
        self.assertEqual(duplicate_pending.id, pending_request.id)

        pending_request.status = "queued"
        pending_request.transient_unit = "sms-saas-manual-restart-123"
        pending_request.summary = "Restart queued. The SaaS services are restarting."
        self.db.session.commit()

        duplicate_queued, created_queued = self.enqueue_platform_service_restart_request(
            requested_by_user=self.platform_admin,
            client_ip="127.0.0.1",
        )
        self.assertFalse(created_queued)
        self.assertEqual(duplicate_queued.id, pending_request.id)

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_request_platform_service_restart_uses_fixed_sudo_command_and_parses_json(self, mock_run) -> None:
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "queued",
                    "summary": "Restart queued. The SaaS services will recycle shortly.",
                    "detail": "Queued SaaS service restart using transient unit sms-saas-manual-restart-123.",
                    "transient_unit": "sms-saas-manual-restart-123",
                }
            ),
            stderr="",
        )

        result = self.request_platform_service_restart()

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["transient_unit"], "sms-saas-manual-restart-123")
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["sudo", "-n", self.helper_path])
        self.assertFalse(kwargs.get("shell", False))

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_request_platform_service_restart_status_uses_status_command_and_parses_json(self, mock_run) -> None:
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "queued",
                    "summary": "Restart queued. The SaaS services are restarting.",
                    "detail": "Transient unit sms-saas-manual-restart-123 is active/running.",
                    "transient_unit": "sms-saas-manual-restart-123",
                }
            ),
            stderr="",
        )

        result = self.request_platform_service_restart_status("sms-saas-manual-restart-123")

        self.assertEqual(result["status"], "queued")
        args, _kwargs = mock_run.call_args
        self.assertEqual(
            args[0],
            ["sudo", "-n", self.helper_path, "--status", "sms-saas-manual-restart-123"],
        )

    def test_request_platform_service_restart_rejects_non_executable_script(self) -> None:
        os.chmod(self.helper_path, 0o644)

        from app.services.platform_operations_service import PlatformServiceRestartError

        with self.assertRaises(PlatformServiceRestartError) as context:
            self.request_platform_service_restart()

        self.assertIn("not executable", str(context.exception))

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_dispatch_platform_service_restart_stores_transient_unit_and_queued_status(self, mock_run) -> None:
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "queued",
                    "summary": "Restart queued. The SaaS services will recycle shortly.",
                    "detail": "Queued SaaS service restart using transient unit sms-saas-manual-restart-123.",
                    "transient_unit": "sms-saas-manual-restart-123",
                }
            ),
            stderr="",
        )
        request_row = self._create_request(status="pending")

        updated = self.dispatch_platform_service_restart(request_row)

        self.assertEqual(updated.status, "queued")
        self.assertEqual(updated.transient_unit, "sms-saas-manual-restart-123")
        self.assertEqual(updated.attempt_count, 1)
        self.assertIsNotNone(updated.started_at)
        self.assertIsNotNone(updated.last_checked_at)

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_dispatch_platform_service_restart_failure_marks_request_failed(self, mock_run) -> None:
        mock_run.return_value = SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "status": "failed",
                    "summary": "Failed to queue the SaaS restart.",
                    "detail": "sudo helper failed",
                    "transient_unit": None,
                }
            ),
            stderr="",
        )
        request_row = self._create_request(status="pending")

        updated = self.dispatch_platform_service_restart(request_row)

        self.assertEqual(updated.status, "failed")
        self.assertEqual(updated.attempt_count, 1)
        self.assertEqual(updated.summary, "Restart request failed before queueing.")
        self.assertEqual(updated.detail, "sudo helper failed")
        event = self.AuthEvent.query.filter_by(event_type="platform_service_restart").order_by(self.AuthEvent.id.desc()).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.outcome, "failed")

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_refresh_platform_service_restart_status_success_moves_to_succeeded(self, mock_run) -> None:
        request_row = self._create_request(
            status="queued",
            transient_unit="sms-saas-manual-restart-123",
            summary="Restart queued. The SaaS services are restarting.",
            started_at=self.platform_admin.created_at,
        )
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "succeeded",
                    "summary": "Restart completed successfully.",
                    "detail": "Transient unit sms-saas-manual-restart-123 completed with result success.",
                    "transient_unit": "sms-saas-manual-restart-123",
                }
            ),
            stderr="",
        )

        updated = self.refresh_platform_service_restart_status(request_row)

        self.assertEqual(updated.status, "succeeded")
        self.assertIsNotNone(updated.completed_at)
        event = self.AuthEvent.query.filter_by(event_type="platform_service_restart").order_by(self.AuthEvent.id.desc()).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.outcome, "success")

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_refresh_platform_service_restart_status_failure_moves_to_failed(self, mock_run) -> None:
        request_row = self._create_request(
            status="queued",
            transient_unit="sms-saas-manual-restart-123",
            summary="Restart queued. The SaaS services are restarting.",
            started_at=self.platform_admin.created_at,
        )
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "failed",
                    "summary": "Restart failed.",
                    "detail": "Transient unit sms-saas-manual-restart-123 finished with result failed.",
                    "transient_unit": "sms-saas-manual-restart-123",
                }
            ),
            stderr="",
        )

        updated = self.refresh_platform_service_restart_status(request_row)

        self.assertEqual(updated.status, "failed")
        self.assertIsNotNone(updated.completed_at)

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_refresh_platform_service_restart_status_marks_stale_request_failed_without_helper_call(self, mock_run) -> None:
        from datetime import timedelta

        self.app.config["PLATFORM_SERVICE_RESTART_STALE_AFTER_SECONDS"] = 1
        request_row = self._create_request(
            status="queued",
            transient_unit="sms-saas-manual-restart-123",
            started_at=self.platform_admin.created_at - timedelta(seconds=10),
        )

        updated = self.refresh_platform_service_restart_status(request_row)

        self.assertEqual(updated.status, "failed")
        self.assertIn("timed out", updated.summary.lower())
        mock_run.assert_not_called()

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_dispatch_platform_service_restart_timeout_marks_request_failed(self, mock_run) -> None:
        request_row = self._create_request(status="pending")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["sudo"], timeout=15)

        updated = self.dispatch_platform_service_restart(request_row)

        self.assertEqual(updated.status, "failed")
        self.assertIn("Timed out", updated.detail)

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_refresh_platform_service_restart_status_timeout_marks_request_failed(self, mock_run) -> None:
        request_row = self._create_request(
            status="queued",
            transient_unit="sms-saas-manual-restart-123",
            started_at=self.platform_admin.created_at,
        )
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["sudo"], timeout=15)

        updated = self.refresh_platform_service_restart_status(request_row)

        self.assertEqual(updated.status, "failed")
        self.assertEqual(updated.summary, "Restart status check failed.")

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_process_platform_service_restart_queue_dispatches_pending_request(self, mock_run) -> None:
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "queued",
                    "summary": "Restart queued. The SaaS services will recycle shortly.",
                    "detail": "Queued SaaS service restart using transient unit sms-saas-manual-restart-123.",
                    "transient_unit": "sms-saas-manual-restart-123",
                }
            ),
            stderr="",
        )
        self._create_request(status="pending")

        summary = self.process_platform_service_restart_queue()

        self.assertEqual(summary["mode"], "dispatch")
        self.assertEqual(summary["status"], "queued")

    @patch("app.services.platform_operations_service.subprocess.run")
    def test_process_platform_service_restart_queue_polls_queued_request(self, mock_run) -> None:
        self._create_request(
            status="queued",
            transient_unit="sms-saas-manual-restart-123",
            started_at=self.platform_admin.created_at,
        )
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "succeeded",
                    "summary": "Restart completed successfully.",
                    "detail": "Transient unit sms-saas-manual-restart-123 completed with result success.",
                    "transient_unit": "sms-saas-manual-restart-123",
                }
            ),
            stderr="",
        )

        summary = self.process_platform_service_restart_queue()

        self.assertEqual(summary["mode"], "poll")
        self.assertEqual(summary["status"], "succeeded")


class TestRestartDeployArtifacts(unittest.TestCase):
    def test_install_script_installs_and_enables_restart_queue_timer(self) -> None:
        repo_root = "/Users/magasiev/.codex/worktrees/4f7e/AOC-SMS-saas"
        with open(os.path.join(repo_root, "deploy", "install_saas.sh"), "r", encoding="utf-8") as handle:
            install_script = handle.read()

        self.assertIn("sms-saas-platform-restart-queue.service", install_script)
        self.assertIn("sms-saas-platform-restart-queue.timer", install_script)
        self.assertIn("run_platform_restart_queue_once.sh", install_script)
        self.assertIn("sms-saas-platform-restart-queue.timer", install_script.split("enable --now", 1)[1])

    def test_restart_helper_status_rejects_unsupported_unit_safely(self) -> None:
        helper_path = "/Users/magasiev/.codex/worktrees/4f7e/AOC-SMS-saas/deploy/restart_sms_saas_services.sh"
        completed = subprocess.run(
            ["bash", helper_path, "--status", "bad unit name"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 64)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("Unsupported transient unit name.", payload["detail"])


if __name__ == "__main__":
    unittest.main()
