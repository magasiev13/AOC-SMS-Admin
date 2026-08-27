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
| `SAAS_BASE_URL` | empty | Compatibility base URL; production sets it to `https://app.twinevia.com`. |
| `PUBLIC_BASE_URL` | `SAAS_BASE_URL` | Marketing and public-policy origin. Managed-pilot production requires `https://twinevia.com`. |
| `APP_BASE_URL` | `SAAS_BASE_URL` | Application, invitation, Checkout, and provider-callback origin. Managed-pilot production requires `https://app.twinevia.com`. |
| `APP_RELEASE_ID` | empty | Immutable release identifier supplied by `.release.env`; required in production. |
| `MANAGED_PILOT_ENABLED` | `1` | Keeps anonymous self-service tenant creation disabled. Required for launch. |
| `CUSTOMER_POLICY_VERSION` | `2026-08-18-managed-pilot-v1` | Base policy acceptance version. Production defines this and each policy-specific version explicitly. |

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
| `AUTH_TRUSTED_BROWSER_COOKIE_NAME` | `twinevia_trusted_browser` |
| `AUTH_TRUSTED_BROWSER_MAX_AGE_SECONDS` | `2592000` |
| `AUTH_ALERT_COOLDOWN_SECONDS` | `900` |
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

## Request And Shared-Capacity Limits

| Variable | Default |
|---|---:|
| `MAX_CONTENT_LENGTH` | `2097152` |
| `MAX_FORM_MEMORY_SIZE` | `262144` |
| `MAX_FORM_PARTS` | `100` |
| `WEBHOOK_MAX_BYTES` | `262144` |
| `CSV_IMPORT_MAX_BYTES` | `1048576` |
| `CSV_IMPORT_MAX_ROWS` | `5000` |
| `CSV_IMPORT_MAX_COLUMNS` | `25` |
| `CSV_IMPORT_MAX_CELL_CHARS` | `2000` |
| `CSV_EXPORT_MAX_ROWS` | `25000` |
| `SEND_MAX_RECIPIENTS` | `5000` |
| `SEND_MAX_SEGMENTS` | `15000` |
| `RECIPIENT_SNAPSHOT_MAX_BYTES` | `1048576` |
| `TENANT_MAX_PROCESSING_MESSAGE_LOGS` | `5` |
| `SCHEDULED_MAX_PENDING_PER_ORGANIZATION` | `25` |

Production validates every range and requires these variables to be explicitly present.

## Browser Security Headers

| Variable | Default | Notes |
|---|---|---|
| `SECURITY_HEADERS_ENABLED` | `1` | Enables app-level browser security headers. Production must keep this enabled. |
| `SECURITY_HSTS_ENABLED` | `1` when `FLASK_ENV=production`, else `0` | Emits HSTS on HTTPS requests. Production must keep this enabled. |
| `SECURITY_HSTS_MAX_AGE` | `31536000` | HSTS lifetime in seconds. Production validation accepts 300 to 63072000. |
| `SECURITY_REFERRER_POLICY` | `strict-origin-when-cross-origin` | Referrer policy header value. |
| `SECURITY_PERMISSIONS_POLICY` | `camera=(), microphone=(), geolocation=(), payment=()` | Permissions policy header value. |
| `SECURITY_CONTENT_SECURITY_POLICY` | built-in Bootstrap/Chart.js-compatible policy | CSP header value. Production must not be empty. |

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

The production `/ready` check also verifies that every timer named by
`READINESS_REQUIRED_SYSTEMD_TIMERS` is active. The default list covers the
send scheduler, billing reconciliation, A2P reconciliation, encrypted backup,
and readiness timers. `READINESS_SYSTEMCTL_TIMEOUT_SECONDS` defaults to `5`.

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
| `TWILIO_VALIDATE_INBOUND_SIGNATURE` | `1` | Signature validation for inbound Twilio requests. Production must keep this enabled. |
| `INBOUND_AUTO_REPLY_ENABLED` | `1` | Global inbound automation toggle. |
| `SURVEY_AMBIGUOUS_DUPLICATE_WINDOW_SECONDS` | `3` | Duplicate-answer safety window for surveys. |

## Twilio A2P Controls

| Variable | Default | Notes |
|---|---|---|
| `TWILIO_A2P_ONBOARDING_ENABLED` | `0` | Enables automated A2P workflows. |
| `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID` | unset | Required when A2P automation is enabled. Must be a `BU...` primary customer profile. |
| `TWILIO_A2P_NUMBER_COUNTRY` | `US` | Auto-buy country code. |
| `TWILIO_A2P_FAKE_QUEUE` | `0` | Test/development A2P queueing aid. |
| `TWILIO_A2P_EVENT_STREAMS_ENABLED` | `0` | Enables `/webhooks/twilio/a2p-events`; every request requires the organization-bound Twilio signature. |

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
| `STRIPE_WEBHOOK_ENDPOINT_ID` | unset | Dedicated live endpoint; startup verifies URL, enabled state, and required events. |
| `STRIPE_PORTAL_CONFIGURATION_ID` | unset | Dedicated live portal configuration; startup verifies its price allowlist. |
| `STRIPE_EXPECTED_ACCOUNT_ID` | `acct_1TCY8xEksbf3Q3Fg` | Live Twinevia Stripe account that must own all configured prices. |
| `STRIPE_PRICE_ID` | unset | Legacy alias for the monthly recurring price ID. Keep set until all deploy scripts use `STRIPE_MONTHLY_PRICE_ID`. |
| `STRIPE_MONTHLY_PRICE_ID` | `STRIPE_PRICE_ID` | Monthly recurring price ID for the `$59.99/mo` option. Required unless `STRIPE_PRICE_ID` is set. |
| `STRIPE_ANNUAL_PRICE_ID` | unset | Annual recurring price ID for the `$600/year upfront` option. Required for SaaS billing validation. |
| `STRIPE_ACTIVATION_PRICE_ID` | unset | Required one-time activation price charged on first paid signup. |
| `STRIPE_GROWTH_PRICE_ID` | unset | Optional Growth recurring price ID for plan allowance mapping. |
| `STRIPE_SCALE_PRICE_ID` | unset | Optional Scale recurring price ID for plan allowance mapping. |
| `STRIPE_FAKE_CHECKOUT_ENABLED` | `0` | Enables `/_test/stripe/checkout/<session_id>`. |
| `BILLING_TRIAL_DAYS` | `0` | Trial length for new checkout sessions. Production should stay at `0`. |
| `BILLING_INCLUDED_OUTBOUND_SEGMENTS` | `1000` | Legacy fallback included outbound usage when a Stripe price ID is not in the plan catalog. |
| `BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS` | `1000` | Included outbound SMS segments per month for the monthly option. |
| `BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS` | `1000` | Included outbound SMS segments per month for the annual option. |
| `BILLING_GROWTH_INCLUDED_OUTBOUND_SEGMENTS` | `3000` | Growth plan included outbound SMS segments. |
| `BILLING_SCALE_INCLUDED_OUTBOUND_SEGMENTS` | `10000` | Scale plan included outbound SMS segments. |
| `BILLING_USAGE_CURRENCY` | `usd` | Usage billing currency. |
| `BILLING_OUTBOUND_SEGMENT_RATE_USD` | `0.0300` | Per-segment sell rate. |
| `BILLING_MONTHLY_PRICE_USD` | `59.99` | Display amount for the monthly option. Stripe price amount remains authoritative. |
| `BILLING_ANNUAL_PRICE_USD` | `600.00` | Display amount for the annual upfront option. Stripe price amount remains authoritative. |
| `BILLING_ACTIVATION_FEE_USD` | `149.99` | Display and verification amount for the universal one-time setup fee. Stripe price amount remains authoritative. |
| `BILLING_OFFER_VERSION` | `2026-08-managed-pilot-v2` | Stored with organizations and Checkout sessions so incompatible open sessions can expire. |
| `BILLING_ANNUAL_ONLY_ORG_SLUGS` | unset | Break-glass comma-separated organization slugs that should only see the annual upfront checkout offer. Prefer the platform admin billing-offer toggle for normal setup. |
| `BILLING_ANNUAL_ONLY_ORG_IDS` | unset | Break-glass comma-separated organization IDs that should only see the annual upfront checkout offer. Prefer the platform admin billing-offer toggle for normal setup. |

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

## Readiness, Backup, And Restore

| Variable | Default | Notes |
|---|---|---|
| `READINESS_TOKEN` | unset | Minimum 32-character secret for the internal `/ready` route. |
| `READINESS_WORKER_MAX_AGE_SECONDS` | `120` | Maximum accepted RQ heartbeat age. |
| `OPERATIONS_MONITORING_MODE` | `webhook` | `webhook` for direct alert/heartbeat URLs or `github_actions` for the external monitor workflow and GitHub issues. |
| `OPERATIONS_GITHUB_REPOSITORY` | unset | `owner/repository` destination for GitHub Actions monitoring, backup artifacts, and incident issues. |
| `ALERT_WEBHOOK_URL` | unset | HTTPS operational-alert destination required in `webhook` monitoring mode. |
| `UPTIME_MONITOR_HEARTBEAT_URL` | unset | HTTPS heartbeat destination required in `webhook` monitoring mode. |
| `BACKUP_LOCAL_DIR` | `/var/backups/twinevia-saas` | Dedicated local encrypted-archive directory. |
| `BACKUP_OFFSITE_MODE` | `mounted` | `mounted` for a verified remote filesystem or `github_actions` for encrypted workflow artifacts. |
| `BACKUP_OFFSITE_DESTINATION` | unset | Separately mounted off-host copy destination required only in `mounted` mode. |
| `BACKUP_ENCRYPTION_PASSPHRASE_FILE` | unset | Root-managed passphrase file outside the repository. |
| `BACKUP_RETENTION_DAYS` | `35` | Local and off-host archive retention. |
| `BACKUP_STATUS_FILE` | `/var/lib/twinevia-saas/backup-status.json` | Latest verified encrypted/off-host backup proof. |
| `BACKUP_MAX_AGE_HOURS` | `30` | Readiness freshness limit. |
| `RESTORE_DRILL_STATUS_FILE` | `/var/lib/twinevia-saas/restore-drill-status.json` | Latest isolated restore proof. |
| `RESTORE_DRILL_DATABASE_URL` | unset | Dedicated non-production PostgreSQL restore target. |
| `RESTORE_DRILL_DATABASE_NAME` | unset | Exact destructive-confirmation name for the restore target. |
| `RESTORE_DRILL_MAX_AGE_DAYS` | `90` | Readiness freshness limit. |
| `AOC_SCHEDULED_CANCELLATION_RECORD_FILE` | unset | Private record proving every dispatchable AOC launch send present at maintenance time was captured and canceled. |

## Production Validation Summary

### When `SAAS_MODE=1` and not debug

Startup requires:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_MONTHLY_PRICE_ID` or legacy `STRIPE_PRICE_ID`
- `STRIPE_ANNUAL_PRICE_ID`
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
- the exact Twinevia public/app hosts and all explicit managed-pilot limit, policy, billing, readiness, backup, restore, and AOC cancellation configuration
- a readable backup passphrase file and identifiable immutable `APP_RELEASE_ID`

## Recommended Production Baseline

```env
FLASK_ENV=production
TRUST_PROXY=1
TRUSTED_HOSTS=twinevia.com,www.twinevia.com,app.twinevia.com
SESSION_COOKIE_SECURE=1
REMEMBER_COOKIE_SECURE=1
SESSION_COOKIE_SAMESITE=Lax
SAAS_MODE=1
SCHEDULER_ENABLED=0
RQ_QUEUE_NAME=twinevia-saas
PUBLIC_BASE_URL=https://twinevia.com
APP_BASE_URL=https://app.twinevia.com
MANAGED_PILOT_ENABLED=1
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
