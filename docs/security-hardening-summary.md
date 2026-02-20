# Security Hardening Summary

## Scope
This branch hardens authentication/session configuration, production validation, deployment env synchronization, and local test reliability.

## What Changed

### 1) Config hardening defaults and documentation
- Added security-focused config keys in `app/config.py`:
  - `AUTH_ATTEMPT_WINDOW_SECONDS`
  - `AUTH_LOCKOUT_SECONDS`
  - `AUTH_MAX_ATTEMPTS_IP_ACCOUNT`
  - `AUTH_MAX_ATTEMPTS_ACCOUNT`
  - `AUTH_MAX_ATTEMPTS_IP`
  - `SESSION_IDLE_TIMEOUT_MINUTES`
  - `REMEMBER_COOKIE_DURATION_DAYS`
  - `AUTH_PASSWORD_MIN_LENGTH`
  - `AUTH_PASSWORD_POLICY_ENFORCE`
  - `TRUSTED_HOSTS`
- Added plain-English comments above security variables for non-technical operators.

### 2) Production fail-closed validation
- Added production config validation in `app/__init__.py`:
  - secure cookie expectations
  - integer/range validation for hardening keys
  - relational constraints for login limits
  - non-empty trusted hosts requirement
- Production startup now raises a clear runtime error if critical security config is invalid.

### 3) Login hardening now uses config values
- Updated `app/auth.py` rate-limiting to read config keys dynamically.
- Added layered counters for:
  - IP
  - account
  - IP+account
- Preserved legacy key compatibility where needed.

### 4) Password policy enforcement
- Added password policy checks in user create/edit flows and account password change.
- Policy is config-driven (`AUTH_PASSWORD_MIN_LENGTH`, `AUTH_PASSWORD_POLICY_ENFORCE`).

### 5) Bootstrap secret cleanup (new)
- Added automatic cleanup of `ADMIN_PASSWORD` after a successful admin password change in production.
- Implemented in `app/routes.py`:
  - Runs only when:
    - `FLASK_ENV=production`
    - `DEBUG` is false
    - current user matches `ADMIN_USERNAME`
  - Removes `ADMIN_PASSWORD=` from env file (default `/opt/sms-admin/.env`, override `SMS_ADMIN_ENV_FILE`).
  - Clears in-process `os.environ["ADMIN_PASSWORD"]` and app config value.
- Added regression test in `tests/test_password_change.py`.

### 6) Deployment env sync safety
- Added deploy script `deploy/deploy_sms_admin.sh` to:
  - append missing hardening keys only
  - preserve existing keys
  - warn when existing values are weaker/non-recommended
  - print sync summary (`appended`, `existing`, `warnings`)
- Updated workflow/deploy docs accordingly.

### 7) Local test reliability
- Added `run/test.sh` wrapper to keep test runs isolated to this repo.
- Ensures `venv`/dependencies are present and Python version is correct.
- Supports option-only invocations (for example `-q`, `--cov=app`) without collecting unrelated nested test directories.
- `run/setup.sh` now installs `pytest` and `pytest-cov`.

## Recommended Production Values
- `AUTH_ATTEMPT_WINDOW_SECONDS=300`
- `AUTH_LOCKOUT_SECONDS=900`
- `AUTH_MAX_ATTEMPTS_IP_ACCOUNT=5`
- `AUTH_MAX_ATTEMPTS_ACCOUNT=8`
- `AUTH_MAX_ATTEMPTS_IP=30`
- `SESSION_IDLE_TIMEOUT_MINUTES=30`
- `REMEMBER_COOKIE_DURATION_DAYS=7`
- `AUTH_PASSWORD_MIN_LENGTH=12`
- `AUTH_PASSWORD_POLICY_ENFORCE=1`
- `TRUSTED_HOSTS=<your production domain list>`

## Operational Notes
- `SECRET_KEY` must always be provided in production `.env`; fallback default is for local/dev only.
- Rotating `SECRET_KEY` invalidates active sessions.
- `ADMIN_PASSWORD` should be treated as bootstrap-only; this branch automates its removal after first successful admin password update in production.
