# tests/ — Test Suite

unittest-style tests run via pytest. Flat directory, no unit/integration split.

## STRUCTURE

```
tests/
├── conftest.py                     # sys.path setup, SECRET_KEY for test env
├── test_utils.py                   # Phone normalization, CSV parsing, templating
├── test_tasks.py                   # RQ job logic with mocked Twilio
├── test_inbox_service.py           # Inbound SMS, keyword matching, surveys
├── test_inbox_routes.py            # Inbox HTTP endpoints with Flask test client
├── test_inbox_automation_routes.py # Keyword/survey CRUD routes
├── test_inbox_keyword_conflicts.py # Cross-table keyword uniqueness
├── test_keyword_conflicts.py       # Keyword conflict edge cases
├── test_suppression_service.py     # Failure classification, suppression upsert
├── test_recipient_service.py       # Unsubscribe/suppress filtering
├── test_scheduled_messages.py      # Scheduler processing logic
├── test_scheduled_routes.py        # Schedule-related HTTP endpoints
├── test_export_csv_security.py     # CSV formula injection prevention
├── test_dbdoctor.py                # Migration inspection/application
├── test_migrations.py              # Individual migration scripts
├── test_sort_utils.py              # Sort parameter validation
├── test_scheduler_mode.py          # Scheduler enable/disable config
├── test_password_change.py         # Password change flow
├── test_user_creation.py           # User creation and roles
└── test_community_search_sidebar.py # Community member search
```

## CONVENTIONS

- **Framework**: `unittest.TestCase` subclasses, run by pytest.
- **Naming**: Files `test_*.py`, classes `Test*`, methods `test_*`.
- **Fixtures**: `setUp()` / `tearDown()` per test class. Shared setup in `conftest.py` (minimal).
- **App context**: Tests create app via `create_app()` with test config, use `app.app_context()`.
- **DB setup**: `db.create_all()` in setUp, `db.drop_all()` in tearDown. Fresh DB per test class.
- **Mocking**: `unittest.mock.patch` / `MagicMock` for Twilio, Redis, external services.
- **Assertions**: Standard unittest (`assertEqual`, `assertTrue`, `assertIn`).
- **Offline**: No real API calls. All external services mocked.

## ADDING A TEST

```python
import unittest
from app import create_app, db

class TestNewFeature(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_something(self):
        # ... test logic
        self.assertEqual(result, expected)
```

## ANTI-PATTERNS

- **DO NOT** make real HTTP/Twilio/Redis calls in tests.
- **DO NOT** share DB state between test classes (each class rebuilds).
- **DO NOT** delete failing tests to "pass" — fix the code or mark skip with reason.
