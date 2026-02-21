# Auth Hardening Phases (Codex-Owned)

Status legend:
- `[ ]` Not Started
- `[~]` In Progress
- `[x]` Done
- `[!]` Blocked

Rules:
- Owner for all tasks: Codex
- Only Codex updates status/evidence in this file.
- A task is `[x]` only after implementation + verification evidence is recorded.

## Phase 0 — Tracking + Baseline

### PH0-01 Create tracker file
- Status: `[x]`
- Owner: Codex
- Files: `docs/auth-hardening-phases.md`
- DoD: Tracking file exists with all phase tasks and status protocol.
- Evidence: Created file at 2026-02-21.

### PH0-02 Confirm Python 3.11 runtime for tests
- Status: `[x]`
- Owner: Codex
- Files: N/A
- DoD: Confirm python3.11 execution path and record result.
- Evidence: `python3.11 --version` -> `Python 3.11.14` on 2026-02-21.

### PH0-03 Record baseline auth behavior
- Status: `[x]`
- Owner: Codex
- Files: `app/auth.py`, `app/routes.py`
- DoD: Baseline behavior recorded (GET logout, password change does not force logout).
- Evidence:
  - `/logout` route is `GET` in `app/auth.py`.
  - `/account/password` success path redirects to dashboard in `app/routes.py` without forced logout.

## Phase 1 — Core Auth Hardening

### PH1-01 Migration 010 (auth hardening tables/columns)
- Status: `[x]`
- Owner: Codex
- Files: `app/migrations/010_add_auth_hardening_tables_and_columns.py`
- DoD: Adds users.phone, users.session_nonce, login_attempts.username, password history and auth events tables/indexes.
- Evidence:
  - Added `app/migrations/010_add_auth_hardening_tables_and_columns.py`.
  - Validated via `./venv/bin/python -m pytest -q tests/test_migrations.py tests/test_dbdoctor.py` -> pass.

### PH1-02 Model updates for new auth types
- Status: `[x]`
- Owner: Codex
- Files: `app/models.py`
- DoD: AppUser supports nonce-bound session IDs; new models exist.
- Evidence:
  - Updated `app/models.py` with `AppUser.phone`, `AppUser.session_nonce`, nonce-bound `get_id()`, `UserPasswordHistory`, `AuthEvent`, `LoginAttempt.username`.

### PH1-03 Auth security service
- Status: `[x]`
- Owner: Codex
- Files: `app/services/auth_security_service.py`
- DoD: Password policy/history and auth event helpers implemented.
- Evidence:
  - Added `app/services/auth_security_service.py` with lockout helpers, password policy/reuse logic, event recording, and retention prune.

### PH1-04 Login hardening
- Status: `[x]`
- Owner: Codex
- Files: `app/auth.py`
- DoD: Username-aware lockout + session-fixation mitigation on login.
- Evidence:
  - Updated `app/auth.py` login flow with account+IP lockout checks, fixation mitigation (`session.clear()` before `login_user`), and audit events.

### PH1-05 POST-only logout
- Status: `[x]`
- Owner: Codex
- Files: `app/auth.py`, `app/templates/base.html`
- DoD: Logout endpoint is POST and UI uses CSRF-protected form submit.
- Evidence:
  - `app/auth.py` logout route changed to `POST`.
  - Verified by `./venv/bin/python -m pytest -q tests/test_auth_hardening.py` (`test_logout_requires_post`) -> pass.

### PH1-06 Password change hardening
- Status: `[x]`
- Owner: Codex
- Files: `app/routes.py`
- DoD: Complexity/reuse checks and forced re-login after password update.
- Evidence:
  - Updated `app/routes.py` change password flow with complexity/reuse checks, nonce rotation, password history write, logout + re-login requirement.
  - Verified by `./venv/bin/python -m pytest -q tests/test_password_change.py tests/test_auth_hardening.py` -> pass.

### PH1-07 Navigation logout migration
- Status: `[x]`
- Owner: Codex
- Files: `app/templates/base.html`
- DoD: Desktop/mobile logout links converted to POST forms.
- Evidence:
  - Converted desktop/mobile logout links in `app/templates/base.html` to CSRF-protected POST forms.

## Phase 2 — Phone Gate + Alerting + Admin Reset

### PH2-01 Add security-contact route
- Status: `[x]`
- Owner: Codex
- Files: `app/routes.py`
- DoD: GET/POST `/account/security-contact` implemented.
- Evidence:
  - Implemented `GET/POST /account/security-contact` in `app/routes.py`.

### PH2-02 Add security-contact template
- Status: `[x]`
- Owner: Codex
- Files: `app/templates/auth/security_contact.html`
- DoD: Mandatory phone setup UI implemented.
- Evidence:
  - Added `app/templates/auth/security_contact.html`.

### PH2-03 Missing-phone access gate
- Status: `[x]`
- Owner: Codex
- Files: `app/auth.py`
- DoD: Authenticated users without phone are redirected to security-contact page.
- Evidence:
  - Added authenticated missing-phone gate in `app/auth.py`.
  - Verified by `./venv/bin/python -m pytest -q tests/test_auth_hardening.py` (`test_missing_phone_is_redirected_to_security_contact`) -> pass.

### PH2-04 Users CRUD requires phone
- Status: `[x]`
- Owner: Codex
- Files: `app/routes.py`, `app/templates/users/form.html`, `app/templates/users/list.html`
- DoD: User create/edit validates and persists phone.
- Evidence:
  - Added phone validation and uniqueness checks in `app/routes.py` user add/edit.
  - Updated `app/templates/users/form.html` and `app/templates/users/list.html` to include phone.
  - Updated `tests/test_user_creation.py` for required phone + stronger password policy.

### PH2-05 Security alert service
- Status: `[x]`
- Owner: Codex
- Files: `app/services/security_alert_service.py`
- DoD: SMS alerts for password change/admin reset/lockout events.
- Evidence:
  - Added `app/services/security_alert_service.py` with per-user SMS alert delivery helpers.

### PH2-06 Admin reset semantics
- Status: `[x]`
- Owner: Codex
- Files: `app/routes.py`
- DoD: Admin password reset revokes sessions + forces password change.
- Evidence:
  - In `app/routes.py` admin password reset now rotates nonce, forces `must_change_password=True`, stores password history, writes auth event, and sends alert.

### PH2-07 Non-blocking alert failures
- Status: `[x]`
- Owner: Codex
- Files: `app/services/security_alert_service.py`, `app/routes.py`, `app/auth.py`
- DoD: Auth actions proceed when alert SMS fails; failure is audited.
- Evidence:
  - `app/auth.py` and `app/routes.py` record `alert_sms_failed` audit events on alert send failure without blocking auth/password flows.

## Phase 3 — Security Event Visibility + Retention

### PH3-01 Add security events route
- Status: `[x]`
- Owner: Codex
- Files: `app/routes.py`
- DoD: Admin-only security event page route with filters.
- Evidence:
  - Added admin-only `/security/events` in `app/routes.py`.

### PH3-02 Add security events template
- Status: `[x]`
- Owner: Codex
- Files: `app/templates/security/events.html`
- DoD: Render audit event list and filter UI.
- Evidence:
  - Added `app/templates/security/events.html` with filter UI and results table.

### PH3-03 Event retention pruning
- Status: `[x]`
- Owner: Codex
- Files: `app/services/auth_security_service.py`
- DoD: Retain auth events for 180 days.
- Evidence:
  - Implemented prune-once-per-day behavior in `app/services/auth_security_service.py` using `AUTH_EVENT_RETENTION_DAYS`.

### PH3-04 Add nav access for admins
- Status: `[x]`
- Owner: Codex
- Files: `app/templates/base.html`
- DoD: Admin nav includes security events page.
- Evidence:
  - Added admin security nav entries in `app/templates/base.html` (desktop + mobile).

## Phase 4 — Tests + Docs + Final Verification

### PH4-01 Tests
- Status: `[x]`
- Owner: Codex
- Files: `tests/*`
- DoD: Existing + new auth tests pass in supported runtime.
- Evidence:
  - Added explicit Phase 4 files:
    - `tests/test_password_policy.py`
    - `tests/test_auth_session_invalidation.py`
    - `tests/test_login_lockout.py`
    - `tests/test_security_contact_gate.py`
    - `tests/test_security_events_routes.py`
  - `./venv/bin/python -m pytest -q tests/test_password_policy.py tests/test_auth_session_invalidation.py tests/test_login_lockout.py tests/test_security_contact_gate.py tests/test_security_events_routes.py` -> `12 passed`.
  - `./venv/bin/python -m pytest -q tests/test_auth_hardening.py tests/test_password_change.py tests/test_user_creation.py tests/test_inbox_routes.py tests/test_events_routes.py tests/test_scheduled_routes.py tests/test_community_search_sidebar.py tests/test_export_csv_security.py tests/test_inbox_keyword_conflicts.py tests/test_keyword_conflicts.py tests/test_inbox_automation_routes.py tests/test_migrations.py tests/test_dbdoctor.py` -> `82 passed`.
  - `./venv/bin/python -m pytest -q tests/test_auth_hardening.py tests/test_password_change.py tests/test_user_creation.py` -> `9 passed`.
  - `./venv/bin/python -m pytest -q tests/test_inbox_routes.py tests/test_events_routes.py tests/test_scheduled_routes.py tests/test_community_search_sidebar.py tests/test_export_csv_security.py tests/test_inbox_keyword_conflicts.py tests/test_keyword_conflicts.py tests/test_inbox_automation_routes.py` -> `67 passed`.
  - `./venv/bin/python -m pytest -q tests/test_migrations.py tests/test_dbdoctor.py` -> `6 passed`.
  - `./venv/bin/python -m pytest -q` -> `163 passed`.

### PH4-02 Docs and config samples
- Status: `[x]`
- Owner: Codex
- Files: `docs/api.md`, `docs/database.md`, `docs/configuration.md`, `docs/troubleshooting.md`, `.env.example`, `README.md`
- DoD: Docs reflect new routes, auth behavior, env vars.
- Evidence:
  - Updated `.env.example`, `docs/api.md`, `docs/configuration.md`, `docs/database.md`, `docs/troubleshooting.md`, and `README.md`.

### PH4-03 Final verification and phase closure
- Status: `[x]`
- Owner: Codex
- Files: `docs/auth-hardening-phases.md`
- DoD: Completed phase statuses + verification commands recorded.
- Evidence:
  - Completed phase statuses and evidence in this file.
  - Added `pytest.ini` with `testpaths = tests` to scope suite collection to this repo's tests and avoid nested `AOC-SMS-Admin/tests` collisions.
  - Full-suite verification now passes in Python 3.11: `./venv/bin/python -m pytest -q` -> `163 passed`.
