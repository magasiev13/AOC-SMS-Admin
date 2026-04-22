import importlib
import json
import os
from pathlib import Path
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
        pending_request.transient_unit = "twinevia-saas-manual-restart-123"
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
                    "detail": "Queued SaaS service restart using transient unit twinevia-saas-manual-restart-123.",
                    "transient_unit": "twinevia-saas-manual-restart-123",
                }
            ),
            stderr="",
        )

        result = self.request_platform_service_restart()

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["transient_unit"], "twinevia-saas-manual-restart-123")
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
                    "detail": "Transient unit twinevia-saas-manual-restart-123 is active/running.",
                    "transient_unit": "twinevia-saas-manual-restart-123",
                }
            ),
            stderr="",
        )

        result = self.request_platform_service_restart_status("twinevia-saas-manual-restart-123")

        self.assertEqual(result["status"], "queued")
        args, _kwargs = mock_run.call_args
        self.assertEqual(
            args[0],
            ["sudo", "-n", self.helper_path, "--status", "twinevia-saas-manual-restart-123"],
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
                    "detail": "Queued SaaS service restart using transient unit twinevia-saas-manual-restart-123.",
                    "transient_unit": "twinevia-saas-manual-restart-123",
                }
            ),
            stderr="",
        )
        request_row = self._create_request(status="pending")

        updated = self.dispatch_platform_service_restart(request_row)

        self.assertEqual(updated.status, "queued")
        self.assertEqual(updated.transient_unit, "twinevia-saas-manual-restart-123")
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
            transient_unit="twinevia-saas-manual-restart-123",
            summary="Restart queued. The SaaS services are restarting.",
            started_at=self.platform_admin.created_at,
        )
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "succeeded",
                    "summary": "Restart completed successfully.",
                    "detail": "Transient unit twinevia-saas-manual-restart-123 completed with result success.",
                    "transient_unit": "twinevia-saas-manual-restart-123",
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
            transient_unit="twinevia-saas-manual-restart-123",
            summary="Restart queued. The SaaS services are restarting.",
            started_at=self.platform_admin.created_at,
        )
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "failed",
                    "summary": "Restart failed.",
                    "detail": "Transient unit twinevia-saas-manual-restart-123 finished with result failed.",
                    "transient_unit": "twinevia-saas-manual-restart-123",
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
            transient_unit="twinevia-saas-manual-restart-123",
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
            transient_unit="twinevia-saas-manual-restart-123",
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
                    "detail": "Queued SaaS service restart using transient unit twinevia-saas-manual-restart-123.",
                    "transient_unit": "twinevia-saas-manual-restart-123",
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
            transient_unit="twinevia-saas-manual-restart-123",
            started_at=self.platform_admin.created_at,
        )
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "succeeded",
                    "summary": "Restart completed successfully.",
                    "detail": "Transient unit twinevia-saas-manual-restart-123 completed with result success.",
                    "transient_unit": "twinevia-saas-manual-restart-123",
                }
            ),
            stderr="",
        )

        summary = self.process_platform_service_restart_queue()

        self.assertEqual(summary["mode"], "poll")
        self.assertEqual(summary["status"], "succeeded")


class TestRestartDeployArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]

    def _read_repo_file(self, *parts: str) -> str:
        return (self.repo_root.joinpath(*parts)).read_text(encoding="utf-8")

    def test_install_script_installs_and_enables_restart_queue_timer(self) -> None:
        install_script = self._read_repo_file("deploy", "install_saas.sh")

        self.assertIn("twinevia-saas-platform-restart-queue.service", install_script)
        self.assertIn("twinevia-saas-platform-restart-queue.timer", install_script)
        self.assertIn("run_platform_restart_queue_once.sh", install_script)
        self.assertIn("resolve_app_user", install_script)
        self.assertIn("resolve_app_group", install_script)
        self.assertIn("install_repo_file", install_script)
        self.assertIn("realpath", install_script)
        self.assertIn("/usr/local/bin/twinevia-saas-dbdoctor", install_script)
        self.assertIn("/usr/local/bin/saas-dbdoctor", install_script)
        self.assertIn('upsert_env_key "RQ_QUEUE_NAME" "twinevia-saas"', install_script)
        self.assertIn('ensure_env_key "PLATFORM_SERVICE_RESTART_SCRIPT" "${RESTART_HELPER_DEST}"', install_script)
        self.assertIn("twinevia-saas-platform-restart-queue.timer", install_script.split("enable --now", 1)[1])

    def test_deploy_script_syncs_restart_helper_and_restart_queue_timer(self) -> None:
        deploy_script = self._read_repo_file("deploy", "deploy_twinevia_saas.sh")
        runtime_units = deploy_script.split("SAAS_RUNTIME_UNITS=(", 1)[1].split(")", 1)[0]

        self.assertIn("restart_twinevia_saas_services.sh", deploy_script)
        self.assertIn("restart-twinevia-saas-services", deploy_script)
        self.assertIn("twinevia-saas-restart.sudoers", deploy_script)
        self.assertIn("RESTART_SUDOERS_DEST", deploy_script)
        self.assertIn("resolve_app_user", deploy_script)
        self.assertIn("resolve_app_group", deploy_script)
        self.assertIn("TWINEVIA_SAAS_DBDOCTOR_BIN", deploy_script)
        self.assertIn("TWINEVIA_SAAS_DBDOCTOR_ALIAS_BIN", deploy_script)
        self.assertIn("EXPECTED_GIT_BRANCH", deploy_script)
        self.assertIn("EXPECTED_GIT_TRACKING_BRANCH", deploy_script)
        self.assertIn("assert_git_source", deploy_script)
        self.assertIn("LEGACY_SAAS_RUNTIME_UNITS", deploy_script)
        self.assertIn("retire_legacy_saas_runtime", deploy_script)
        self.assertIn("LOG_DIR", deploy_script)
        self.assertIn('upsert_env_key "RQ_QUEUE_NAME" "twinevia-saas"', deploy_script)
        self.assertIn('ensure_env_key "PLATFORM_SERVICE_RESTART_SCRIPT" "${RESTART_HELPER_DEST}"', deploy_script)
        self.assertIn("twinevia-saas-platform-restart-queue.service", deploy_script)
        self.assertIn("twinevia-saas-platform-restart-queue.timer", deploy_script)
        self.assertIn("systemctl daemon-reload", deploy_script)
        self.assertIn("visudo", deploy_script)
        self.assertIn('systemctl enable --now "${SAAS_RUNTIME_UNITS[@]}"', deploy_script)
        self.assertIn('sudo -n "${RESTART_HELPER_DEST}" --check', deploy_script)
        self.assertIn('"twinevia-saas-platform-restart-queue.timer"', runtime_units)

    def test_deploy_workflow_asserts_restart_queue_timer_and_helper(self) -> None:
        workflow = self._read_repo_file(".github", "workflows", "deploy-twinevia-production.yml")

        self.assertIn('branches: ["main"]', workflow)
        self.assertIn("default: main", workflow)
        self.assertIn("default: origin/main", workflow)
        self.assertNotIn("codex/saas-pilot-v2", workflow)
        self.assertIn("sudo systemctl is-active --quiet twinevia-saas-platform-restart-queue.timer", workflow)
        self.assertIn("restart-twinevia-saas-services --check", workflow)
        self.assertIn("/usr/local/bin/twinevia-saas-dbdoctor", workflow)
        self.assertIn("deploy_branch:", workflow)
        self.assertIn("deploy_tracking:", workflow)
        self.assertIn("TWINEVIA_DEPLOY_BRANCH", workflow)
        self.assertIn("TWINEVIA_DEPLOY_TRACKING", workflow)
        self.assertIn("TWINEVIA_SSH_TARGET", workflow)
        self.assertIn("TWINEVIA_SSH_KNOWN_HOSTS", workflow)
        self.assertIn("BETA_DEPLOY_BRANCH is deprecated", workflow)
        self.assertIn("BETA_DEPLOY_TRACKING is deprecated", workflow)
        self.assertIn("EXPECTED_GIT_BRANCH", workflow)
        self.assertIn("EXPECTED_GIT_TRACKING_BRANCH", workflow)
        self.assertIn("resolve_app_root", workflow)
        self.assertIn("systemctl show twinevia-saas.service -p WorkingDirectory --value", workflow)
        self.assertIn("/opt/sms-saas", workflow)
        self.assertIn("CURRENT_UNIT_PREFIX", workflow)
        self.assertIn('APP_ROOT="${APP_ROOT}" APP_USER="${APP_USER}"', workflow)
        self.assertNotIn("beta.theitwingman.com", workflow)
        self.assertIn("LEGACY_BETA_DEPLOY_BRANCH", workflow)

    def test_legacy_deploy_workflow_is_manual_only(self) -> None:
        workflow = self._read_repo_file(".github", "workflows", "deploy.yml")

        self.assertIn("Deploy Legacy SMS Admin (manual)", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn('branches: [ "main" ]', workflow)

    def test_production_cutover_script_collects_backups_and_snapshots(self) -> None:
        cutover_script = self._read_repo_file("run", "production_cutover.sh")

        self.assertIn("public_readiness_local.sh", cutover_script)
        self.assertIn("public_readiness_production_snapshot.sh", cutover_script)
        self.assertIn("pg_dump", cutover_script)
        self.assertIn("redis-cli", cutover_script)
        self.assertIn("deploy_twinevia_saas.sh", cutover_script)
        self.assertIn("live_tracking_branch.txt", cutover_script)
        self.assertIn("resolve_remote_app_root", cutover_script)
        self.assertIn("resolve_remote_app_user", cutover_script)
        self.assertIn("resolve_remote_unit_prefix", cutover_script)
        self.assertIn("TWINEVIA_SAAS_APP_ROOT", cutover_script)
        self.assertIn("TWINEVIA_SAAS_PYTHON", cutover_script)
        self.assertIn("TWINEVIA_PUBLIC_HOST", cutover_script)
        self.assertIn("TWINEVIA_SSH_TARGET", cutover_script)
        self.assertIn("www.twinevia.com", cutover_script)
        self.assertIn("systemctl show twinevia-saas.service -p WorkingDirectory --value", cutover_script)

    def test_production_cutover_script_supports_canonical_host_migration_and_records_layout(self) -> None:
        cutover_script = self._read_repo_file("run", "production_cutover.sh")

        self.assertIn("--canonicalize-host", cutover_script)
        self.assertIn("/opt/twinevia-saas", cutover_script)
        self.assertIn("/opt/sms-saas", cutover_script)
        self.assertIn("canonicalized.txt", cutover_script)
        self.assertIn("pre_app_root.txt", cutover_script)
        self.assertIn("post_app_root.txt", cutover_script)
        self.assertIn("pre_app_user.txt", cutover_script)
        self.assertIn("post_app_user.txt", cutover_script)
        self.assertIn("runtime_layout.pre.txt", cutover_script)
        self.assertIn("runtime_layout.post.txt", cutover_script)
        self.assertIn("assert_runtime_layout", cutover_script)
        self.assertIn('assert_runtime_layout "${RUN_DIR}/runtime_layout.post.txt" "twinevia" "/opt/twinevia-saas"', cutover_script)
        self.assertIn("install_saas.sh", cutover_script)

    def test_live_smoke_wrapper_requires_existing_credentials(self) -> None:
        live_smoke_path = self.repo_root / "run" / "public_readiness_live_smoke.sh"
        completed = subprocess.run(
            ["bash", str(live_smoke_path), "--run-id", "live-smoke-missing-creds"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "HOME": os.environ.get("HOME", ""),
                "PATH": os.environ.get("PATH", ""),
            },
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("TWINEVIA_OWNER_USERNAME", completed.stderr)
        self.assertIn("TWINEVIA_PLATFORM_PASSWORD", completed.stderr)

    def test_live_playwright_config_skips_local_web_server_boot(self) -> None:
        live_config = self._read_repo_file("playwright.live.config.js")

        self.assertIn("https://www.twinevia.com", live_config)
        self.assertIn("TWINEVIA_LIVE_BASE_URL", live_config)
        self.assertIn("live-production-smoke.spec.js", live_config)
        self.assertNotIn("webServer", live_config)

    def test_saas_deploy_scripts_prefer_saas_base_url_host_for_health_checks(self) -> None:
        install_script = self._read_repo_file("deploy", "install_saas.sh")
        deploy_script = self._read_repo_file("deploy", "deploy_twinevia_saas.sh")

        for script in (install_script, deploy_script):
            self.assertIn('current_env_value "SAAS_BASE_URL"', script)
            self.assertIn('canonical_host="$(printf', script)
            self.assertIn('printf \'%s\\n\' "${canonical_host}"', script)

    def test_production_snapshot_script_requires_explicit_ssh_target(self) -> None:
        snapshot_path = self.repo_root / "run" / "public_readiness_production_snapshot.sh"
        snapshot_script = self._read_repo_file("run", "public_readiness_production_snapshot.sh")
        self.assertIn("systemctl show twinevia-saas.service -p WorkingDirectory --value", snapshot_script)
        completed = subprocess.run(
            ["bash", str(snapshot_path), "--org-slug", "control", "--label", "baseline"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "HOME": os.environ.get("HOME", ""),
                "PATH": os.environ.get("PATH", ""),
            },
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("TWINEVIA_SSH_TARGET", completed.stderr)

    def test_production_cutover_script_requires_explicit_ssh_target(self) -> None:
        cutover_path = self.repo_root / "run" / "production_cutover.sh"
        completed = subprocess.run(
            ["bash", str(cutover_path), "--org-slug", "control"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "HOME": os.environ.get("HOME", ""),
                "PATH": os.environ.get("PATH", ""),
            },
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("TWINEVIA_SSH_TARGET", completed.stderr)

    def test_production_snapshot_script_warns_when_beta_env_aliases_are_used(self) -> None:
        snapshot_path = self.repo_root / "run" / "public_readiness_production_snapshot.sh"
        completed = subprocess.run(
            ["bash", str(snapshot_path), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "HOME": os.environ.get("HOME", ""),
                "PATH": os.environ.get("PATH", ""),
                "BETA_SIGNOFF_HOST": "legacy.example.com",
                "BETA_SIGNOFF_SSH_TARGET": "ubuntu@legacy.example.com",
            },
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("BETA_SIGNOFF_HOST is deprecated", completed.stderr)
        self.assertIn("BETA_SIGNOFF_SSH_TARGET is deprecated", completed.stderr)

    def test_production_snapshot_script_surfaces_customer_managed_reads_and_out_of_band_activity(self) -> None:
        snapshot_script = self._read_repo_file("run", "public_readiness_production_snapshot.sh")

        self.assertIn('provider_mode == \\"customer_managed\\"', snapshot_script)
        self.assertIn('profile.twilio_account_sid', snapshot_script)
        self.assertIn('_normalized_sid', snapshot_script)
        self.assertIn('twilio_out_of_band.json', snapshot_script)
        self.assertIn('Top out-of-band destinations', snapshot_script)

    def test_doc_smoke_runs_naming_audit_for_retired_beta_refs(self) -> None:
        doc_smoke_script = self._read_repo_file("run", "doc_smoke.sh")
        naming_audit_script = self._read_repo_file("run", "naming_audit.sh")

        self.assertIn("./run/naming_audit.sh", doc_smoke_script)
        self.assertIn("./run/public_readiness_live_smoke.sh --help", doc_smoke_script)
        self.assertIn("beta\\\\.theitwingman\\\\.com", naming_audit_script)
        self.assertIn("public_readiness_beta_snapshot\\\\.sh|beta_cutover\\\\.sh|beta-cutover", naming_audit_script)
        self.assertIn("\\\\bbeta (snapshot|cutover|host|deploy|signoff)\\\\b", naming_audit_script)

    def test_saas_unit_templates_use_rendered_user_group_and_canonical_dbdoctor(self) -> None:
        service_unit = self._read_repo_file("deploy", "twinevia-saas.service")

        self.assertIn("User=__APP_USER__", service_unit)
        self.assertIn("Group=__APP_GROUP__", service_unit)
        self.assertIn("ExecStartPre=__TWINEVIA_SAAS_DBDOCTOR_DEST__ --apply", service_unit)

    def test_saas_dbdoctor_wrappers_support_legacy_saas_root(self) -> None:
        canonical_wrapper = self._read_repo_file("bin", "twinevia-saas-dbdoctor")
        compatibility_wrapper = self._read_repo_file("bin", "saas-dbdoctor")

        self.assertIn("TWINEVIA_SAAS_APP_ROOT", canonical_wrapper)
        self.assertIn("/opt/sms-saas/venv/bin/python", canonical_wrapper)
        self.assertIn("TWINEVIA_SAAS_APP_ROOT", compatibility_wrapper)
        self.assertIn("/opt/sms-saas/venv/bin/python", compatibility_wrapper)

    def test_restart_helper_status_rejects_unsupported_unit_safely(self) -> None:
        helper_path = self.repo_root / "deploy" / "restart_twinevia_saas_services.sh"
        completed = subprocess.run(
            ["bash", str(helper_path), "--status", "bad unit name"],
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
