"""Tests for scheduler startup mode.

Run with: python -m unittest tests.test_scheduler_mode
"""

import os
import unittest
from unittest.mock import patch

from app import create_app, create_runtime_app
from app.services import scheduler_service


class TestSchedulerMode(unittest.TestCase):
    def setUp(self) -> None:
        self._original_scheduler_enabled = os.environ.get("SCHEDULER_ENABLED")
        self._original_scheduler_runner = os.environ.get("SCHEDULER_RUNNER")
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["SCHEDULER_ENABLED"] = "0"
        os.environ["FLASK_DEBUG"] = "1"
        scheduler_service._scheduler_initialized = False
        scheduler_service.scheduler = None

    def tearDown(self) -> None:
        scheduler_service.shutdown_scheduler()
        scheduler_service._scheduler_initialized = False
        scheduler_service.scheduler = None
        if self._original_scheduler_enabled is None:
            os.environ.pop("SCHEDULER_ENABLED", None)
        else:
            os.environ["SCHEDULER_ENABLED"] = self._original_scheduler_enabled
        if self._original_scheduler_runner is None:
            os.environ.pop("SCHEDULER_RUNNER", None)
        else:
            os.environ["SCHEDULER_RUNNER"] = self._original_scheduler_runner
        if self._original_flask_debug is None:
            os.environ.pop("FLASK_DEBUG", None)
        else:
            os.environ["FLASK_DEBUG"] = self._original_flask_debug

    def test_factory_is_safe_by_default_even_with_scheduler_env_flags(self) -> None:
        os.environ["SCHEDULER_ENABLED"] = "1"
        os.environ["SCHEDULER_RUNNER"] = "1"

        with patch("app._run_startup_tasks") as mock_startup:
            create_app()

        mock_startup.assert_not_called()
        self.assertFalse(scheduler_service._scheduler_initialized)
        self.assertIsNone(scheduler_service.scheduler)

    def test_runtime_app_runs_bootstrap_and_can_start_scheduler(self) -> None:
        os.environ["SCHEDULER_ENABLED"] = "1"

        with patch("app._run_startup_tasks") as mock_startup:
            with patch("app.services.scheduler_service.init_scheduler") as mock_init_scheduler:
                create_runtime_app(start_scheduler=True)

        mock_startup.assert_called_once()
        mock_init_scheduler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
