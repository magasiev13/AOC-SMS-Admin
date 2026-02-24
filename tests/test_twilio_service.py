import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class TestTwilioInboundSignatureValidation(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "twilio-service.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "TWILIO_AUTH_TOKEN": "test-auth-token",
                "SCHEDULER_ENABLED": "0",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._ctx = self.app.app_context()
        self._ctx.push()

    def tearDown(self) -> None:
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def test_missing_auth_token_returns_reason(self) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        self.app.config["TWILIO_AUTH_TOKEN"] = None
        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            "signature",
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "missing_auth_token")

    def test_missing_signature_returns_reason(self) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            None,
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "missing_signature")

    @patch("app.services.twilio_service.RequestValidator")
    def test_validator_exception_returns_reason(self, mock_validator) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        mock_validator.return_value.validate.side_effect = RuntimeError("validator exploded")
        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            "signature",
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "validator_exception")

    @patch("app.services.twilio_service.RequestValidator")
    def test_invalid_signature_returns_reason(self, mock_validator) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        mock_validator.return_value.validate.return_value = False
        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            "signature",
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "invalid_signature")


if __name__ == "__main__":
    unittest.main()
