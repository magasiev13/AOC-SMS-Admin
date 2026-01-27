# AGENTS.md

This repository welcomes AI agents. Use this guide to work safely, predictably, and with high quality.

## 1) Mission and constraints
- **Primary goal:** deliver correct, maintainable changes that match the request.
- **Be precise:** prefer small, verifiable changes over sweeping rewrites.
- **Honor intent:** if requirements are unclear, surface assumptions in the final summary.
- **Never fabricate results:** if you did not run a test or confirm behavior, say so.

## 2) Safety and responsibility (Codex principles)
- **Sandbox first:** avoid actions that affect external systems unless explicitly requested.
- **Least privilege:** do not access secrets or credentials; do not exfiltrate data.
- **No silent risks:** flag security, privacy, or data-loss concerns when you spot them.
- **Human-in-the-loop:** prefer changes that can be reviewed; avoid irreversible actions.

## 3) Repo quick map
- **App entry:** `wsgi.py` (Flask app factory in `app/__init__.py`).
- **Core code:** `app/` (routes, models, services, utils, migrations).
- **Tests:** `tests/` (pytest can run unittest-based tests).
- **Docs:** `docs/` (architecture, DB, API, services, config, deployment).

## 4) Build, lint, test commands
- **Install deps:** `python3 -m venv venv` and `pip install -r requirements.txt`.
- **Run dev server:** `flask --app wsgi:app run --debug`.
- **Run all tests:** `pytest`.
- **Coverage:** `pytest --cov=app`.
- **Single test (pytest):** `pytest tests/test_utils.py::TestNormalizePhone::test_us_number_without_country_code`.
- **Single test (unittest):** `python -m unittest tests.test_utils.TestNormalizePhone.test_us_number_without_country_code`.
- **Lint/format:** no repo linting tools or configs found; do not add new linters without explicit request.

## 4.5) Environment and runtime notes
- `.env` is loaded via python-dotenv; start from `.env.example`.
- Required env vars: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `SECRET_KEY`.
- SQLite DB lives in `instance/` (created automatically at runtime).
- Redis is required for RQ background jobs; queue name defaults to `sms`.
- Scheduler: dev uses APScheduler when `SCHEDULER_ENABLED=1`; prod uses systemd timer.
- Do not enable `sms-scheduler.service` directly; enable `sms-scheduler.timer` only.

## 5) Implementation workflow
1. **Clarify scope**: identify affected files and user-visible behavior.
2. **Plan**: outline steps mentally; keep changes minimal and cohesive.
3. **Implement**: make incremental edits; keep diffs small.
4. **Validate**: run targeted tests or checks where practical.
5. **Document**: summarize changes, tests, and any limitations.

## 6) Code style and conventions

### Imports
- Group in this order: standard library, third-party, local `app.*`.
- Prefer explicit imports; avoid `import *`.
- Keep module-level imports at top unless circular dependencies require local imports.

### Formatting
- 4-space indentation, no tabs.
- Follow existing style in each file; there is no enforced formatter.
- Keep lines readable; avoid very long lines when reasonable.

### Types
- Use type hints for public functions when practical.
- Reuse existing helper types (`Optional`, `list`, `dict`) rather than inventing new ones.
- Prefer `datetime` objects with explicit timezone handling (see below).

### Naming
- `snake_case` for functions/vars, `CamelCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Model table names use explicit `__tablename__` strings.
- Keep route handler names short and descriptive (`users_add`, `dashboard`).

### Error handling and logging
- Catch expected exceptions narrowly; log warnings with context.
- Roll back `db.session` on SQLAlchemy exceptions before continuing.
- In routes, surface user-facing errors with `flash` and redirect/render safely.

### Datetimes and timezones
- Store UTC times in the DB; convert to local timezone at the edges.
- Use `utc_now()` or `timezone.utc` consistently.
- When creating scheduled timestamps, convert to UTC then strip tzinfo for DB storage.

### Phone numbers and CSV parsing
- Normalize and validate phone numbers via `app.utils.normalize_phone` and `app.utils.validate_phone`.
- Use `parse_recipients_csv` / `parse_phones_csv` for CSV inputs.
- Suppress unsubscribed/suppressed contacts via `app.services.recipient_service` helpers.

### Flask patterns
- Register routes via blueprints (`app.routes.bp`, `app.auth.bp`).
- Guard protected views with `@login_required` and `@require_roles`.
- Use `current_app` for config and logging inside request handlers.

### Database patterns (SQLAlchemy)
- Use `db.Model` classes in `app/models.py` and `db.session` for persistence.
- Commit after unit-of-work; prefer single commit per logical action.
- Use `validate` decorators for normalization (see `SuppressedContact.phone`).
- On schema changes, add migrations under `app/migrations/` and apply via `dbdoctor`.

### Templates/static
- HTML templates in `app/templates/`, static assets in `app/static/`.
- Keep presentation logic in templates minimal; compute view data in routes.

### Background jobs and scheduler
- RQ jobs live in `app/tasks.py`; enqueue via `app.queue.get_queue()`.
- Handle retries via RQ Retry where appropriate (see queue usage in routes).
- Scheduler service is `app.services.scheduler_service`; do not spawn long-lived threads in routes.

### Security and data handling
- Never commit `.env` or secret material; keep secrets in environment variables.
- Use `normalize_phone` before storing phone numbers; avoid raw input.
- Respect suppression and unsubscribe lists for any send logic.
- When logging, avoid dumping full message bodies or credentials.

## 7) Tests
- Tests live in `tests/` and can be run via pytest.
- New behavior should include focused tests in `tests/`.
- Prefer small, deterministic tests; avoid network calls.
- pytest runs unittest-style classes without modification.
- When adding tests, keep fixtures local to the test module unless shared widely.

## 8) Cursor/Copilot rules
- No Cursor rules found in `.cursor/rules/` or `.cursorrules`.
- No Copilot instructions found in `.github/copilot-instructions.md`.

## 9) Quick ops guide (Codex)
- **Unit tests:** run `pytest`. If none exist, add focused tests in `tests/` and document how to run them.
- **DB doctor & migrations:** run `python -m app.dbdoctor --print` and `python -m app.dbdoctor --apply` (apply migrations before serving).
- **Systemd services:** expected units are `sms`, `sms-worker`, and `sms-scheduler.timer`; all load environment from `/opt/sms-admin/.env`.
- **Scheduler:** uses systemd timer (`sms-scheduler.timer`) that triggers `sms-scheduler.service` (Type=oneshot) every 30 seconds. Do NOT enable `sms-scheduler.service` directly—enable only the timer.
- **Documentation:** see `docs/` for architecture, database schema, API reference, services, config, deployment, troubleshooting.
- **Definition of done (prod):**
  - [ ] No 500s in logs after deploy.
  - [ ] Migrations run **before** serving traffic.
  - [ ] Logs are clear of errors for `sms` and `sms-scheduler`.

## 10) Communication standards
- Use clear, structured summaries.
- List commands you ran.
- Call out open questions or follow-ups.
- Avoid speculation; separate observations from assumptions.

## 11) What not to do
- Don’t introduce unused code, dead branches, or speculative features.
- Don’t change unrelated files.
- Don’t add dependencies without explicit request.
- Don’t bypass suppression/unsubscribe checks.

## 12) Deliverables checklist
- [ ] Changes are minimal and scoped.
- [ ] Tests or checks run (or skipped with reason).
- [ ] Summary is concise and accurate.
- [ ] No hidden side effects.
