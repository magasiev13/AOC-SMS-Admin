import importlib
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestSecurityAlertService(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "security-alerts.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SAAS_MODE": "1",
                "SCHEDULER_ENABLED": "0",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, MessagingUsageRecord, Organization, OrganizationMembership

        self.db = db
        self.AppUser = AppUser
        self.MessagingUsageRecord = MessagingUsageRecord
        self.Organization = Organization
        self.OrganizationMembership = OrganizationMembership
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    @patch("app.services.security_alert_service.get_twilio_service")
    def test_send_security_alert_records_usage_for_org_user(self, mock_get_twilio_service) -> None:
        from app.services.security_alert_service import send_security_alert

        organization = self.Organization(name="Acme", slug="acme", status="active")
        user = self.AppUser(
            username="alert-user",
            email="alert-user@acme.test",
            phone="+15550001111",
            role="admin",
            must_change_password=False,
        )
        user.set_password("Password-pass1!")
        self.db.session.add_all([organization, user])
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="owner",
            )
        )
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM-auth-alert-1",
            "status": "sent",
            "account_sid": "ACauth-1",
        }
        mock_get_twilio_service.return_value = mock_service
        self.app.config["TESTING"] = False

        result = send_security_alert(user, "password_changed")

        self.assertEqual(result, {"success": True, "skipped": False, "reason": None})
        mock_service.send_message.assert_called_once()
        self.assertEqual(mock_service.send_message.call_args.kwargs["send_kind"], "auth_alert")
        record = self.MessagingUsageRecord.query.filter_by(message_sid="SM-auth-alert-1").one()
        self.assertEqual(record.organization_id, organization.id)
        self.assertEqual(record.source, "auth_alert")
        self.assertEqual(record.twilio_subaccount_sid, "ACauth-1")

    @patch("app.services.security_alert_service.get_twilio_service")
    def test_send_security_alert_logs_when_user_has_no_org(self, mock_get_twilio_service) -> None:
        from app.services.security_alert_service import send_security_alert

        user = self.AppUser(
            username="platform-alert",
            email="platform-alert@acme.test",
            phone="+15550002222",
            role="admin",
            is_platform_admin=True,
            must_change_password=False,
        )
        user.set_password("Password-pass1!")
        self.db.session.add(user)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM-auth-alert-2",
            "status": "sent",
            "account_sid": "ACauth-2",
        }
        mock_get_twilio_service.return_value = mock_service
        self.app.config["TESTING"] = False

        with patch.object(self.app.logger, "info") as logger_info:
            result = send_security_alert(user, "account_lockout")

        self.assertEqual(result, {"success": True, "skipped": False, "reason": None})
        self.assertIsNone(self.MessagingUsageRecord.query.filter_by(message_sid="SM-auth-alert-2").first())
        logger_info.assert_called_once()

    @patch("app.services.security_alert_service.get_twilio_service")
    def test_send_security_alert_respects_skipped_testing_policy(self, mock_get_twilio_service) -> None:
        from app.services.security_alert_service import send_security_alert

        user = self.AppUser(
            username="testing-alert",
            email="testing-alert@acme.test",
            phone="+15550003333",
            role="admin",
            must_change_password=False,
        )
        user.set_password("Password-pass1!")
        self.db.session.add(user)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": False,
            "skipped": True,
            "reason": "testing_live_send_blocked",
        }
        mock_get_twilio_service.return_value = mock_service

        result = send_security_alert(user, "account_lockout")

        self.assertEqual(
            result,
            {"success": False, "skipped": True, "reason": "testing_live_send_blocked"},
        )

    @patch("app.services.security_alert_service.get_twilio_service")
    def test_send_security_alert_allows_testing_override(self, mock_get_twilio_service) -> None:
        from app.services.security_alert_service import send_security_alert

        organization = self.Organization(name="Override Org", slug="override-org", status="active")
        user = self.AppUser(
            username="override-alert",
            email="override-alert@acme.test",
            phone="+15550004444",
            role="admin",
            must_change_password=False,
        )
        user.set_password("Password-pass1!")
        self.db.session.add_all([organization, user])
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="owner",
            )
        )
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM-auth-alert-override",
            "status": "sent",
            "account_sid": "ACauth-override",
        }
        mock_get_twilio_service.return_value = mock_service
        self.app.config["TWILIO_ALLOW_LIVE_SENDS_IN_TESTING"] = True

        result = send_security_alert(user, "account_lockout")

        self.assertEqual(result, {"success": True, "skipped": False, "reason": None})
        mock_service.send_message.assert_called_once()
        record = self.MessagingUsageRecord.query.filter_by(message_sid="SM-auth-alert-override").one()
        self.assertEqual(record.organization_id, organization.id)
        self.assertEqual(record.source, "auth_alert")


if __name__ == "__main__":
    unittest.main()
