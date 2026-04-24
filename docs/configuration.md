# Twinevia Configuration

All runtime configuration is loaded from `app/config.py`.

The parser behavior is strict:

- booleans must be valid values such as `1/0`, `true/false`, `yes/no`, or `on/off`
- integers must parse cleanly
- comma-separated host lists are parsed with trimming
- `SESSION_COOKIE_SAMESITE` accepts `Lax`, `Strict`, or `None` case-insensitively and is normalized to Flask's expected casing

Production behavior is also strict:

- `FLASK_ENV=production` turns on the fail-closed startup checks
- `SECRET_KEY` must not be the development default
- SaaS billing and provider prerequisites must be present when `SAAS_MODE=1`
- `TRUSTED_HOSTS` is required for strict production validation

## Core Runtime

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | `dev-secret-key-change-in-production` | Required for any real deployment. |
| `FLASK_ENV` | unset | `development` enables debug; `production` enables strict startup validation. |
| `FLASK_DEBUG` | unset | Explicit debug override. |
| `TRUST_PROXY` | `0` | Enables `ProxyFix`; use only behind a trusted reverse proxy. |
| `SAAS_MODE` | `0` | Enables the SaaS control plane and SaaS readiness validation. |
| `SAAS_BASE_URL` | empty | Public base URL used for absolute links and Twilio webhook binding. |

## Platform Operations

| Variable | Default | Notes |
|---|---|---|
| `PLATFORM_SERVICE_RESTART_ENABLED` | `0` | Enables the platform restart control on `/platform`. |
| `PLATFORM_SERVICE_RESTART_SCRIPT` | `/usr/local/bin/restart-twinevia-saas-services` | Must be an absolute executable path. |
| `PLATFORM_SERVICE_RESTART_TIMEOUT_SECONDS` | `15` | Restart-helper command timeout. |
| `PLATFORM_SERVICE_RESTART_STALE_AFTER_SECONDS` | `300` | How long queued restart state can sit before refresh logic treats it as stale. |

## Session And Cookie Security

| Variable | Default | Notes |
|---|---|---|
| `SESSION_COOKIE_SAMESITE` | `Lax` | Input is case-insensitive; production must resolve to `Lax` or `Strict`. |
| `SESSION_COOKIE_SECURE` | `1` outside debug, `0` in debug | Production must be enabled. |
| `REMEMBER_COOKIE_SECURE` | mirrors `SESSION_COOKIE_SECURE` | Production must be enabled. |
| `SESSION_IDLE_TIMEOUT_MINUTES` | `30` | Also drives `PERMANENT_SESSION_LIFETIME`. |
| `REMEMBER_COOKIE_DURATION_DAYS` | `7` | Remember-me lifetime. |

Hard-coded secure defaults:

- `SESSION_COOKIE_HTTPONLY=True`
- `REMEMBER_COOKIE_HTTPONLY=True`
- `REMEMBER_COOKIE_SAMESITE=SESSION_COOKIE_SAMESITE`

## Auth Hardening

| Variable | Default |
|---|---|
| `AUTH_ATTEMPT_WINDOW_SECONDS` | `300` |
| `AUTH_LOCKOUT_SECONDS` | `900` |
| `AUTH_MAX_ATTEMPTS_IP_ACCOUNT` | `5` |
| `AUTH_MAX_ATTEMPTS_ACCOUNT` | `8` |
| `AUTH_MAX_ATTEMPTS_IP` | `30` |
| `AUTH_PASSWORD_MIN_LENGTH` | `12` |
| `AUTH_PASSWORD_POLICY_ENFORCE` | `1` |
| `PASSWORD_HISTORY_COUNT` | `3` |
| `AUTH_ALERTS_ENABLED` | `1` |
| `AUTH_EVENT_RETENTION_DAYS` | `180` |
| `AUTH_LOCKOUT_MAX_ATTEMPTS` | defaults to `AUTH_MAX_ATTEMPTS_IP_ACCOUNT` |
| `AUTH_LOCKOUT_WINDOW_SECONDS` | defaults to `AUTH_ATTEMPT_WINDOW_SECONDS` |

Production validation enforces:

- integer ranges
- secure cookie settings
- `AUTH_PASSWORD_POLICY_ENFORCE=1`
- reasonable relationships between the lockout thresholds

## Host And Proxy Controls

| Variable | Default | Notes |
|---|---|---|
| `TRUSTED_HOSTS` | empty | Comma-separated hostnames; required by strict production validation. |
| `TRUST_PROXY` | `0` | Enables forwarded host/scheme/IP handling. |

Important production rule:

- host-header enforcement runs only when `FLASK_ENV=production` and `TRUSTED_HOSTS` is non-empty

## Scheduler And Time

| Variable | Default | Notes |
|---|---|---|
| `SCHEDULER_ENABLED` | `1` in debug, else `0` | For local/dev only; production uses systemd timers. |
| `SCHEDULED_MESSAGE_MAX_LAG` | `1440` | Minutes before a due send expires. |
| `SCHEDULED_PROCESSING_TIMEOUT_MINUTES` | `10` | Stuck-processing timeout. |
| `SCHEDULED_SEND_MAX_RETRIES` | `3` | Retry attempts for transient scheduled failures. |
| `SCHEDULED_SEND_RETRY_BACKOFF_SECONDS` | `60` | Base retry delay. |
| `SCHEDULED_SEND_RETRY_MAX_BACKOFF_SECONDS` | `900` | Retry backoff cap. |
| `APP_TIMEZONE` | `UTC` | Default display timezone when client timezone cookie is absent. |

## Redis And Queue

| Variable | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Shared by worker and local tooling. |
| `RQ_QUEUE_NAME` | `twinevia-saas` when `SAAS_MODE=1`, else `sms` | Queue default follows the active runtime family. |

## Hosted Compliance And A2P Defaults

When `SAAS_BASE_URL` is set, the app also exposes tenant-hosted SMS compliance pages at `/compliance/<organization-slug>/sms/privacy`, `/terms`, and `/opt-in`. These URLs are generated for every org and act as the automatic A2P fallback package whenever the tenant does not have a public site or the supplied public website/privacy/terms/CTA URLs fail validation.

The self-serve A2P flow defaults eligible EIN-backed businesses to `low_volume_standard` and defaults the campaign posture to `ACCOUNT_NOTIFICATION`. Organizations can still move to `standard` later when they need more throughput or explicitly request the upgrade.

When an org drifts away from the live Twilio A2P resources, the app now stores a recovery snapshot instead of silently rebuilding state. Background refresh is status-only: it reads Twilio state through the org's stored subaccount auth token, clears stale transient errors when the stored subaccount packet is still valid, and can detect stale provider identifiers, a missing campaign, or transient Twilio connectivity failures. It does not auto-create a new campaign or silently swap A2P resources. Platform admins must explicitly reconcile live Twilio resources and explicitly create a campaign from the onboarding page when a new Twilio vetting cycle would be triggered.

`TWILIO_A2P_ONBOARDING_ENABLED` gates automated Twilio onboarding/provisioning work. Status refresh for already-submitted A2P records can still run without it. `TWILIO_A2P_EVENT_STREAMS_ENABLED=1` is required if production should accept Twilio Event Streams callbacks at `/webhooks/twilio/a2p-events`.

## Database

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///.../instance/twinevia.db` when `SAAS_MODE=1`, else `sqlite:///.../instance/sms.db` | Primary SaaS production should override with PostgreSQL. |
| `SQLITE_TIMEOUT` | `30` | Applied only when the driver is SQLite. |

Important runtime distinction:

- `dbdoctor` is only for the legacy SQLite path
- `app.saas_db` / `twinevia-saas-dbdoctor` are the correct tools for SaaS databases

## Twilio And Messaging Provider Settings

| Variable | Default | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | unset | Parent/master account in platform-managed SaaS. |
| `TWILIO_AUTH_TOKEN` | unset | Parent/master auth token. |
| `TWILIO_API_KEY_SID` | unset | Optional production REST auth; must be paired with secret. |
| `TWILIO_API_KEY_SECRET` | unset | Optional production REST auth; must be paired with SID. |
| `TWILIO_FROM_NUMBER` | unset | Mainly used by the legacy single-tenant runtime. |
| `TWILIO_PLATFORM_FRIENDLY_NAME` | `Twinevia` | Default naming seed for platform-managed resources. |
| `TWILIO_CREDENTIAL_ENCRYPTION_KEY` | unset | Required for SaaS billing/provider validation. |
| `TWILIO_VALIDATE_INBOUND_SIGNATURE` | `1` | Signature validation for inbound Twilio requests. |
| `INBOUND_AUTO_REPLY_ENABLED` | `1` | Global inbound automation toggle. |
| `SURVEY_AMBIGUOUS_DUPLICATE_WINDOW_SECONDS` | `3` | Duplicate-answer safety window for surveys. |

## Twilio A2P Controls

| Variable | Default | Notes |
|---|---|---|
| `TWILIO_A2P_ONBOARDING_ENABLED` | `0` | Enables automated A2P workflows. |
| `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID` | unset | Required when A2P automation is enabled. Must be a `BU...` primary customer profile. |
| `TWILIO_A2P_NUMBER_COUNTRY` | `US` | Auto-buy country code. |
| `TWILIO_A2P_FAKE_QUEUE` | `0` | Test/development A2P queueing aid. |
| `TWILIO_A2P_EVENT_STREAMS_ENABLED` | `0` | Enables `/webhooks/twilio/a2p-events`. |
| `TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN` | unset | Bearer token expected on the A2P Event Streams sink. |

Operational note:

- Twinevia stores the Messaging Service campaign association SID as `campaign_sid` (`QE...`)
- Twilio Console may also expose a separate console campaign ID (`CM...`) in status metadata
- status refresh requires the org's stored subaccount auth token for platform-managed A2P reads; it does not infer tenant state from the parent account

## Billing And Stripe

| Variable | Default | Notes |
|---|---|---|
| `STRIPE_SECRET_KEY` | unset | Required for SaaS billing validation. |
| `STRIPE_PUBLISHABLE_KEY` | unset | UI/client-side Stripe usage if needed. |
| `STRIPE_WEBHOOK_SECRET` | unset | Required for webhook verification and SaaS billing validation. |
| `STRIPE_PRICE_ID` | unset | Required Starter recurring price ID and default plan for legacy compatibility. |
| `STRIPE_ACTIVATION_PRICE_ID` | unset | Required one-time activation price charged on first paid signup. |
| `STRIPE_GROWTH_PRICE_ID` | unset | Optional Growth recurring price ID for plan allowance mapping. |
| `STRIPE_SCALE_PRICE_ID` | unset | Optional Scale recurring price ID for plan allowance mapping. |
| `STRIPE_FAKE_CHECKOUT_ENABLED` | `0` | Enables `/_test/stripe/checkout/<session_id>`. |
| `BILLING_TRIAL_DAYS` | `0` | Trial length for new checkout sessions. Production should stay at `0`. |
| `BILLING_INCLUDED_OUTBOUND_SEGMENTS` | `1000` | Legacy fallback included outbound usage when a Stripe price ID is not in the plan catalog. |
| `BILLING_STARTER_INCLUDED_OUTBOUND_SEGMENTS` | `1000` | Starter plan included outbound SMS segments. |
| `BILLING_GROWTH_INCLUDED_OUTBOUND_SEGMENTS` | `3000` | Growth plan included outbound SMS segments. |
| `BILLING_SCALE_INCLUDED_OUTBOUND_SEGMENTS` | `10000` | Scale plan included outbound SMS segments. |
| `BILLING_USAGE_CURRENCY` | `usd` | Usage billing currency. |
| `BILLING_OUTBOUND_SEGMENT_RATE_USD` | `0.0300` | Per-segment sell rate. |
| `BILLING_ACTIVATION_FEE_USD` | `149.00` | Display amount for the one-time activation fee. Stripe price amount remains authoritative. |

## Admin And Local Tooling

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Bootstrap admin username. |
| `ADMIN_EMAIL` | unset | Optional bootstrap email. |
| `ADMIN_PASSWORD` | unset | Required for first admin bootstrap in legacy production and for first SaaS platform-admin provisioning. |
| `TWINEVIA_SAAS_ENV_FILE` | unset | Optional SaaS-specific env-file override used when production password changes remove bootstrap `ADMIN_PASSWORD`. |
| `PLAYWRIGHT_ARTIFACT_DIR` | empty | Overrides browser artifact output location. |

Workspace test recipients are tenant-scoped data managed from the Twinevia UI, not a server-level environment variable.

Compatibility note:

- `SMS_ADMIN_ENV_FILE` remains accepted as a legacy fallback after `TWINEVIA_SAAS_ENV_FILE`

## Production Validation Summary

### When `SAAS_MODE=1` and not debug

Startup requires:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_PRICE_ID`
- `STRIPE_ACTIVATION_PRICE_ID`
- `SAAS_BASE_URL`
- `TWILIO_CREDENTIAL_ENCRYPTION_KEY`

And also:

- `TWILIO_API_KEY_SID` and `TWILIO_API_KEY_SECRET` together, if either is set
- `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID` when `TWILIO_A2P_ONBOARDING_ENABLED=1`

### When `FLASK_ENV=production`

Startup additionally requires:

- secure session and remember cookies
- `SESSION_COOKIE_SAMESITE` of `Lax` or `Strict`
- non-empty `TRUSTED_HOSTS`
- valid hardening ranges and relationships

## Recommended Production Baseline

```env
FLASK_ENV=production
TRUST_PROXY=1
TRUSTED_HOSTS=app.example.com,www.example.com
SESSION_COOKIE_SECURE=1
REMEMBER_COOKIE_SECURE=1
SESSION_COOKIE_SAMESITE=Lax
SAAS_MODE=1
SCHEDULER_ENABLED=0
RQ_QUEUE_NAME=twinevia-saas
```

## Runtime Profiles

### SaaS production

- PostgreSQL in `DATABASE_URL`
- `SAAS_MODE=1`
- `RQ_QUEUE_NAME=twinevia-saas`
- systemd `twinevia-saas*` units and timers

### Legacy compatibility deployment

- SQLite `DATABASE_URL` or default instance DB
- `SAAS_MODE=0`
- `RQ_QUEUE_NAME=sms`
- systemd `sms*` units and timer
- `TWILIO_FROM_NUMBER` still matters directly here
