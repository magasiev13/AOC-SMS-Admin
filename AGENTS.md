# PROJECT KNOWLEDGE BASE

**Generated:** 2025-02-11
**Commit:** 6c51e23
**Branch:** codex/csv-export-formula-injection-fix

## OVERVIEW

Flask SMS admin app for sending community/event SMS blasts via Twilio. Python 3.11 (supported/tested), SQLAlchemy/SQLite, Redis/RQ worker, systemd timer scheduler. Single-server Debian VPS deployment behind Nginx + Gunicorn.

## STRUCTURE

```
./
├── app/                  # Core application (see app/AGENTS.md)
│   ├── __init__.py       # App factory: create_app(), extensions, migrations, scheduler
│   ├── config.py         # Env-based config class
│   ├── models.py         # All 15 SQLAlchemy models
│   ├── routes.py         # Main blueprint, 40+ endpoints
│   ├── auth.py           # Auth blueprint, login/logout, rate limiting
│   ├── utils.py          # Phone normalization, CSV parsing, templating
│   ├── tasks.py          # RQ background jobs
│   ├── queue.py          # Redis/RQ connection
│   ├── dbdoctor.py       # DB health check CLI
│   ├── sort_utils.py     # Sort param validation
│   ├── scheduler_runner.py # Standalone scheduler entry
│   ├── services/         # Business logic (see app/services/AGENTS.md)
│   ├── migrations/       # Custom SQLite migrations (see app/migrations/AGENTS.md)
│   ├── templates/        # Jinja2 HTML (grouped by feature)
│   └── static/           # CSS, JS assets
├── tests/                # pytest suite (see tests/AGENTS.md)
├── deploy/               # systemd, nginx, install (see deploy/AGENTS.md)
├── docs/                 # Architecture, API, DB, services, deployment docs
├── bin/dbdoctor          # CLI wrapper
├── run/                  # Dev helper scripts (setup.sh, dev.sh, up.sh)
├── wsgi.py               # WSGI entry point
└── requirements.txt      # Pinned dependencies
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add route | `app/routes.py` | Blueprint `main`, use `@login_required` + `@require_roles` |
| Add model | `app/models.py` | Explicit `__tablename__`, use `utc_now` default |
| Schema change | `app/migrations/NNN_*.py` | See `app/migrations/AGENTS.md` for format |
| SMS sending logic | `app/services/twilio_service.py` | `TwilioService.send_bulk()` |
| Scheduled messages | `app/services/scheduler_service.py` | Processes pending `ScheduledMessage` |
| Recipient filtering | `app/services/recipient_service.py` | Filters unsubs + suppressions |
| Inbound SMS | `app/services/inbox_service.py` | Webhook handler, keyword matching, surveys |
| Background jobs | `app/tasks.py` | RQ jobs; each creates own app context |
| Phone normalization | `app/utils.py` | `normalize_phone()`, `validate_phone()` |
| CSV import | `app/utils.py` | `parse_recipients_csv()`, `parse_phones_csv()` |
| Auth / login | `app/auth.py` | Flask-Login, `require_roles()` decorator |
| Config / env vars | `app/config.py` | All from env; required: `TWILIO_*`, `SECRET_KEY` |
| Deploy / systemd | `deploy/` | `sms.service`, `sms-worker.service`, `sms-scheduler.timer` |
| DB migrations CLI | `app/dbdoctor.py` or `bin/dbdoctor` | `--print`, `--apply`, `--doctor` |

## CONVENTIONS

- **Imports**: stdlib, third-party, `app.*` (explicit, no wildcards)
- **Naming**: `snake_case` funcs/vars, `CamelCase` classes, `UPPER_SNAKE_CASE` constants
- **Indent**: 4 spaces, no tabs
- **Datetime**: Store UTC, convert at boundaries. Use `utc_now()` from `app.models`. Strip tzinfo before DB storage.
- **Phone numbers**: Always normalize with `app.utils.normalize_phone`. Validate with `validate_phone`. Assumes US (+1) without country code.
- **Blueprints**: `main` (routes.py) and `auth` (auth.py). Register in `create_app()`.
- **Route guards**: `@login_required` then `@require_roles('admin')` for admin-only.
- **Error handling**: Narrow catches. Rollback `db.session` on SQLAlchemy errors. Use `flash()` for user feedback.
- **Templates**: Minimal logic. Compute display data in routes/services.
- **Background work**: Queue via `app.queue.get_queue()`. Jobs in `app/tasks.py`. Each job creates own app context.
- **No linter enforced**: Don't add one without explicit request.

## ANTI-PATTERNS (THIS PROJECT)

- **DO NOT** enable `sms-scheduler.service` directly. Enable only `sms-scheduler.timer`.
- **DO NOT** spawn long-lived threads from routes.
- **DO NOT** log credentials or full sensitive message bodies.
- **DO NOT** use Alembic. This project uses custom SQLite migrations via `dbdoctor`.
- **DO NOT** add new dependencies without approval.
- **DO NOT** commit `.env` files.

## UNIQUE STYLES

- **Custom migration system**: Numbered Python files in `app/migrations/` with `apply(connection, logger)`. Tracked in `schema_migrations` table. Managed by `dbdoctor`.
- **Dual recipient pools**: `community_members` (community blasts) and `event_registrations` (event blasts) are separate. Never cross-target.
- **Scheduler dual-mode**: APScheduler (dev, `SCHEDULER_ENABLED=1`) vs systemd timer (prod). Runner flag: `SCHEDULER_RUNNER=1`.
- **RQ jobs create own app context**: `create_app(run_startup_tasks=False, start_scheduler=False)` inside each job.
- **CSV formula injection prevention**: `sanitize_csv_cell()` prefixes dangerous characters with `'`.
- **Keyword normalization**: Keywords uppercased and whitespace-collapsed via `normalize_keyword()`. Uniqueness enforced across `KeywordAutomationRule` and `SurveyFlow` tables.

## COMMANDS

```bash
# Dev
flask --app wsgi:app run --debug       # Dev server
./run/up.sh                            # Full stack (web + worker)
./run/dev.sh                           # Web only

# Test
pytest                                 # All tests
pytest --cov=app                       # With coverage
pytest tests/test_utils.py::TestNormalizePhone::test_us_number_without_country_code

# DB
python -m app.dbdoctor --print         # Inspect migrations
python -m app.dbdoctor --apply         # Apply pending migrations
python -m app.dbdoctor --doctor        # Full health check

# Deploy
sudo systemctl status sms sms-worker sms-scheduler.timer
journalctl -u sms-scheduler.service -f # Watch scheduler logs
```

## NOTES

- SQLite DB auto-created in `instance/`. WAL/SHM files may exist alongside.
- Redis required for RQ worker. Default queue name: `sms`.
- `.env` loaded via python-dotenv. Start from `.env.example`.
- `ExecStartPre=dbdoctor --apply` in systemd units auto-runs migrations on restart.
- Login rate limiting: 5 attempts / 5 min window, 10 min lockout. Tracked in `login_attempts` table.
- `SECRET_KEY` must be changed in production (runtime error if default + non-debug).
- Admin user auto-created on first run if `ADMIN_PASSWORD` set.
- Scheduled messages expire if older than `SCHEDULED_MESSAGE_MAX_LAG` minutes (default 1440 = 24h).
- Suppression backfill available as RQ job: `backfill_suppressions_job()`.
