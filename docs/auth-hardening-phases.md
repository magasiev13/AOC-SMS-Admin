# Twinevia Auth And Account Security Reference

This file describes the current auth and account-security behavior in the app.

## Login Surfaces

### Workspace login

- route: `/login`
- audience: owners, staff, and legacy non-platform users

### Platform login

- route: `/platform/login`
- audience: platform admins

Unauthorized requests are redirected to the matching login surface based on the requested path.

## Session Model

- Flask-Login is configured with `session_protection = "strong"`
- `AppUser.get_id()` returns `"<user_id>:<session_nonce>"`
- rotating `session_nonce` invalidates outstanding sessions
- password changes and admin resets use nonce rotation

## Enforced Account Gates

### Mandatory password change

If `must_change_password` is set:

- the user is restricted to:
  - `/account/password`
  - `/account/security-contact`
  - logout

### Mandatory security contact

If the user has no phone:

- the user is redirected to `/account/security-contact`
- normal workspace or platform use is blocked until a phone is stored

### Suspended organizations

In SaaS mode, workspace users in a suspended org:

- are logged out
- have tenant context cleared
- are redirected back to login with an error

## Password Rules

Current password controls are config-driven:

- minimum length from `AUTH_PASSWORD_MIN_LENGTH`
- enforcement toggle from `AUTH_PASSWORD_POLICY_ENFORCE`
- reuse prevention from `PASSWORD_HISTORY_COUNT`

Password changes write to:

- `users.password_hash`
- `user_password_history`
- `auth_events`

## Login Lockout Rules

The lockout service tracks failures across these scopes:

- IP + username
- username across all IPs
- IP across all usernames

Backed by:

- `login_attempts`

Key config:

- `AUTH_ATTEMPT_WINDOW_SECONDS`
- `AUTH_LOCKOUT_SECONDS`
- `AUTH_MAX_ATTEMPTS_IP_ACCOUNT`
- `AUTH_MAX_ATTEMPTS_ACCOUNT`
- `AUTH_MAX_ATTEMPTS_IP`

## Audit Events

Security-relevant actions are written to `auth_events`, including:

- login failures and lockouts
- password changes
- security contact updates
- admin resets
- platform restart queue outcomes

Retention is controlled by:

- `AUTH_EVENT_RETENTION_DAYS`

## Security Alerts

When enabled:

- `AUTH_ALERTS_ENABLED=1`

The app can send SMS alerts for selected account-security events through `security_alert_service.py`.

## SaaS Routing Rules

`home_endpoint_for_user()` and related auth helpers route users based on:

- platform admin vs workspace user
- owner vs staff
- setup completeness
- organization status

Expected destinations:

- platform admin -> `/platform`
- owner with incomplete setup -> `/setup`
- staff with incomplete setup -> `/setup/pending`
- ready workspace user -> `/dashboard`

## Admin Recovery

### First SaaS platform admin

Provision with:

```bash
cd /opt/twinevia-saas
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --ensure-platform-admin'
```

For bootstrap-password cleanup on the SaaS runtime, `TWINEVIA_SAAS_ENV_FILE` is the canonical env-file override.

### Manual password reset

If database-level recovery is required:

- generate a new hash with Werkzeug
- update the user row
- rotate or replace `session_nonce` to invalidate existing sessions

Use DB-level recovery sparingly and record the action in your operational incident trail.
