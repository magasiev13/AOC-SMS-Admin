"""Tests for scheduler startup mode.

Run with: python -m unittest tests.test_scheduler_mode
"""

import os
import unittest

from app import create_app
from app.services import scheduler_service


class TestSchedulerMode(unittest.TestCase):
    def setUp(self) -> None:
        self._original_scheduler_enabled = os.environ.get("SCHEDULER_ENABLED")
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
        if self._original_flask_debug is None:
            os.environ.pop("FLASK_DEBUG", None)
        else:
            os.environ["FLASK_DEBUG"] = self._original_flask_debug

    def test_web_app_mode_does_not_start_scheduler(self) -> None:
        create_app(run_startup_tasks=False)
        self.assertFalse(scheduler_service._scheduler_initialized)
        self.assertIsNone(scheduler_service.scheduler)


if __name__ == "__main__":
    unittest.main()
