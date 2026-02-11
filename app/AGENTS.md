# app/ — Core Application

Flask application package. Entry point: `create_app()` in `__init__.py`.

## STRUCTURE

```
app/
├── __init__.py          # App factory, extension init, migration runner, admin seed
├── config.py            # All config from env vars (Config class)
├── models.py            # 15 SQLAlchemy models (see below)
├── routes.py            # Blueprint 'main', 40+ endpoints, all UI routes
├── auth.py              # Blueprint 'auth', login/logout, rate limiting, require_roles()
├── utils.py             # normalize_phone, validate_phone, CSV parsers, template rendering
├── tasks.py             # RQ job definitions: send_bulk_job, backfill_suppressions_job
├── queue.py             # get_queue() → Redis/RQ connection
├── dbdoctor.py          # CLI: --print, --apply, --doctor
├── sort_utils.py        # normalize_sort_params() for safe column sorting
├── scheduler_runner.py  # Standalone entry for systemd scheduler oneshot
├── services/            # Business logic layer (see services/AGENTS.md)
├── migrations/          # Custom SQLite migrations (see migrations/AGENTS.md)
├── templates/           # Jinja2 grouped by feature (auth/, community/, events/, inbox/, etc.)
└── static/              # css/style.css, js/timezone.js, js/sidebar.js
```

## WHERE TO LOOK

| Task | File | Key symbols |
|------|------|-------------|
| Add/modify route | `routes.py` | `bp = Blueprint('main', ...)`, use `@login_required` |
| Add admin-only route | `routes.py` | Stack `@login_required` then `@require_roles('admin')` |
| Add/modify model | `models.py` | Subclass `db.Model`, set `__tablename__`, use `utc_now` default |
| Phone handling | `utils.py` | `normalize_phone()`, `validate_phone()`, `_looks_like_phone()` |
| CSV import logic | `utils.py` | `parse_recipients_csv()`, `parse_phones_csv()` |
| CSV export safety | `utils.py` | `sanitize_csv_cell()` — prefix formula chars with `'` |
| Message templating | `utils.py` | `render_message_template()`, tokens: `{name}`, `{first_name}`, `{full_name}` |
| Keyword normalization | `utils.py` | `normalize_keyword()` — uppercase + collapse whitespace |
| Background job | `tasks.py` | Each job calls `create_app(run_startup_tasks=False, start_scheduler=False)` |
| Queue connection | `queue.py` | `get_queue()` returns RQ queue for `sms` |
| App config | `config.py` | All from `os.environ`; required: `TWILIO_*`, `SECRET_KEY` |
| Password change flow | `auth.py` | `enforce_password_change()` redirects if `must_change_password` |
| Sort validation | `sort_utils.py` | `normalize_sort_params(allowed_columns, ...)` |

## MODELS (15 total in models.py)

| Model | Table | Purpose |
|-------|-------|---------|
| `AppUser` | `users` | Login users with roles (admin, social_manager) |
| `CommunityMember` | `community_members` | Community blast recipients |
| `UnsubscribedContact` | `unsubscribed_contacts` | Opted-out phones (STOP, manual) |
| `SuppressedContact` | `suppressed_contacts` | Invalid/failed phones (auto-detected) |
| `Event` | `events` | Event definitions |
| `EventRegistration` | `event_registrations` | Per-event recipient registrations |
| `MessageLog` | `message_logs` | Send history with per-recipient JSON details |
| `InboxThread` | `inbox_threads` | Conversation threads grouped by phone |
| `InboxMessage` | `inbox_messages` | Individual inbound/outbound messages |
| `KeywordAutomationRule` | `keyword_automation_rules` | Auto-reply rules triggered by keyword |
| `SurveyFlow` | `survey_flows` | Multi-step survey definitions |
| `SurveySession` | `survey_sessions` | Per-phone survey progress |
| `SurveyResponse` | `survey_responses` | Individual survey answers |
| `ScheduledMessage` | `scheduled_messages` | Future-scheduled blasts |
| `LoginAttempt` | `login_attempts` | Failed login tracking for rate limiting |

## CONVENTIONS (app-specific)

- **App factory pattern**: `create_app()` handles extensions, blueprints, migrations, admin seed, scheduler.
- **Two blueprints only**: `main` (routes.py) and `auth` (auth.py). Register new routes in one of these.
- **Model validators**: Use `@validates('field')` for auto-normalization (e.g., phone, keyword).
- **`localtime` filter**: Template filter in `__init__.py` converts UTC to client timezone via cookie.
- **Config safety**: Runtime error if `SECRET_KEY` is default in non-debug mode.
- **Proxy support**: `ProxyFix` middleware enabled when `TRUST_PROXY=1`.

## ANTI-PATTERNS

- **DO NOT** import models at module level in `__init__.py` (causes circular imports). Use local imports.
- **DO NOT** add a third blueprint without discussing architecture impact.
- **DO NOT** store timezone-aware datetimes in SQLite. Strip tzinfo before storage.
