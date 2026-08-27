# Twinevia Security Hardening Reference

This file is the current production security checklist for the app, not a historical branch summary.

## Startup Gates

When not in debug mode:

- `SECRET_KEY` must not be the development default
- SaaS billing prerequisites are validated when `SAAS_MODE=1`

When `FLASK_ENV=production`:

- `SESSION_COOKIE_SECURE` must be enabled
- `REMEMBER_COOKIE_SECURE` must be enabled
- `SESSION_COOKIE_HTTPONLY` and `REMEMBER_COOKIE_HTTPONLY` must remain enabled
- `SESSION_COOKIE_SAMESITE` must be `Lax` or `Strict`
- `TWILIO_VALIDATE_INBOUND_SIGNATURE` must be enabled
- `SECURITY_HEADERS_ENABLED` must be enabled
- `SECURITY_HSTS_ENABLED` must be enabled
- `SECURITY_CONTENT_SECURITY_POLICY` must not be empty
- login hardening values must fall within sane ranges
- `AUTH_PASSWORD_POLICY_ENFORCE` must be enabled
- `TRUSTED_HOSTS` must be non-empty

## Required Production Controls

- `TRUST_PROXY=1` only behind a trusted reverse proxy
- `TRUSTED_HOSTS` set to real public hostnames
- `SAAS_MODE=1` for the primary runtime
- `SCHEDULER_ENABLED=0` in production; use systemd timers
- `RQ_QUEUE_NAME=twinevia-saas` for the SaaS runtime
- browser security headers are emitted from Flask, including CSP, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, and HSTS on HTTPS requests
- SaaS web, worker, scheduler, billing reconcile, and A2P reconcile units run with systemd sandboxing such as `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, and an empty capability bounding set

## Auth And Session Controls

Current auth hardening model:

- nonce-bound Flask-Login session IDs
- forced password change support
- mandatory security phone capture
- database-backed lockout counters
- password reuse prevention
- auth event audit trail

Recommended production baseline:

```env
AUTH_ATTEMPT_WINDOW_SECONDS=300
AUTH_LOCKOUT_SECONDS=900
AUTH_MAX_ATTEMPTS_IP_ACCOUNT=5
AUTH_MAX_ATTEMPTS_ACCOUNT=8
AUTH_MAX_ATTEMPTS_IP=30
AUTH_TRUSTED_BROWSER_COOKIE_NAME=twinevia_trusted_browser
AUTH_TRUSTED_BROWSER_MAX_AGE_SECONDS=2592000
AUTH_ALERT_COOLDOWN_SECONDS=900
SESSION_IDLE_TIMEOUT_MINUTES=30
REMEMBER_COOKIE_DURATION_DAYS=7
AUTH_PASSWORD_MIN_LENGTH=12
AUTH_PASSWORD_POLICY_ENFORCE=1
PASSWORD_HISTORY_COUNT=3
AUTH_ALERTS_ENABLED=1
AUTH_EVENT_RETENTION_DAYS=180
```

## SaaS Billing And Provider Controls

When `SAAS_MODE=1`, production should treat these as required:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_MONTHLY_PRICE_ID` or legacy `STRIPE_PRICE_ID`
- `STRIPE_ANNUAL_PRICE_ID`
- `STRIPE_ACTIVATION_PRICE_ID`
- `SAAS_BASE_URL`
- `TWILIO_CREDENTIAL_ENCRYPTION_KEY`

And conditionally:

- `TWILIO_API_KEY_SID` and `TWILIO_API_KEY_SECRET` together
- `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID` when automated A2P onboarding is enabled

## Webhook Safety

- Stripe webhook verification depends on `STRIPE_WEBHOOK_SECRET`
- inbound Twilio signature verification is required in production with `TWILIO_VALIDATE_INBOUND_SIGNATURE=1`
- optional Twilio A2P Event Streams require an organization-bound Twilio signature; no global bearer fallback is accepted

## Platform Restart Safety

If platform restart control is enabled:

- the feature should remain platform-admin-only
- the helper path must be absolute and executable
- the helper must run via `sudo -n`
- restart requests should flow through `PlatformServiceRestartRequest`, not inline shell execution from web requests

## Bootstrap Admin Handling

### SaaS

- `ADMIN_PASSWORD` is used for first platform-admin provisioning
- after the first platform admin exists, routine deploys should not depend on it

### Legacy compatibility path

The old public legacy runtime is retired. `ADMIN_PASSWORD` cleanup guidance applies to SaaS bootstrap and local compatibility workflows only.

## Operational Checklist

Before exposing the app publicly, confirm:

- the sourced `twinevia-saas-dbdoctor --doctor` check exits `0`
- `twinevia-saas`, `twinevia-saas-worker`, and all required timers are active
- health checks succeed with an allowed `Host` header
- platform login and workspace login both work
- invite acceptance, billing return, and workspace setup do not leak cross-tenant data
- provider ownership and sender identity are unique per organization
