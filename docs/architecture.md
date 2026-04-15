# Relayn Architecture

Relayn is a Flask application with two supported runtime modes:

- primary: multi-tenant SaaS on PostgreSQL with explicit SaaS schema management
- secondary: legacy single-tenant `SMS Admin` on SQLite with the older `dbdoctor` migration path

The production design is SaaS-first. Legacy behavior remains in the codebase for compatibility and migration support.

## System At A Glance

```text
Browser / Reverse Proxy
  -> Flask app (wsgi.py -> create_runtime_app)
    -> auth and account gates
    -> tenant-scoped workspace routes
    -> platform admin routes
    -> public webhooks
    -> service layer
      -> Stripe billing and webhook sync
      -> Twilio provider provisioning and A2P onboarding
      -> inbox automation and survey flows
      -> scheduled send processing
      -> platform restart queue
    -> SQLAlchemy models
      -> PostgreSQL (primary SaaS)
      -> SQLite (legacy compatibility / local demos)
    -> Redis / RQ worker
    -> systemd timers for scheduler, billing reconciliation, A2P reconciliation, restart queue
```

## Runtime Entry Points

### App factory

- `app.create_app(run_startup_tasks=False, start_scheduler=False)`
  - pure application factory
  - used by tests, workers, CLI helpers, and validation checks
- `app.create_runtime_app(start_scheduler=False)`
  - runtime entrypoint with startup side effects
  - runs schema readiness/bootstrap work
  - optionally starts the in-process scheduler when explicitly requested

### WSGI entrypoint

- `wsgi.py`
  - loads `.env` with `python-dotenv`
  - creates the runtime app
  - starts the in-process scheduler only when `SCHEDULER_ENABLED=1`

### Production validation

When `FLASK_ENV=production` and the app is not in debug mode, startup validates:

- `SECRET_KEY`
- cookie security settings
- auth hardening numeric ranges
- `TRUSTED_HOSTS`
- SaaS billing requirements when `SAAS_MODE=1`

## Tenant Model

### SaaS control plane

The SaaS product introduces a platform layer above individual workspaces:

- platform admins sign in through `/platform/login`
- businesses are represented by `Organization`
- owner/staff assignments live in `OrganizationMembership`
- invite flows use `OrganizationInvitation`
- billing state lives in `OrganizationSubscription`
- messaging provider state lives in `OrganizationMessagingProfile`
- Twilio A2P state lives in `OrganizationA2POnboarding`

### Tenant scoping

Tenant isolation is implemented in `app/tenant.py`:

- request-time org context is stored in a `ContextVar`
- `auth.py` sets the current org for non-platform-admin SaaS users
- SQLAlchemy `do_orm_execute` adds tenant criteria automatically for scoped models
- SQLAlchemy `before_flush` auto-populates `organization_id` on new tenant-scoped rows

This keeps platform tables global while ensuring workspace data stays organization-bound.

## Request And Job Flows

### Auth and session flow

- session auth is handled with Flask-Login
- user IDs are nonce-bound via `AppUser.get_id()`
- password changes and admin resets rotate `session_nonce`
- users without a security phone are forced through `/account/security-contact`
- owner/staff SaaS users are routed to `/setup`, `/setup/pending`, or `/dashboard` depending on workspace readiness

### Owner setup flow

Typical SaaS onboarding path:

1. owner accepts `/invites/<token>` or signs up through `/signup`
2. owner lands on `/setup`
3. billing is started through Stripe checkout
4. Twilio provider readiness is configured:
   - platform-managed provider via platform admin
   - customer-managed provider via saved external credentials
5. Twilio A2P onboarding is saved, submitted, refreshed, or canceled
6. workspace becomes send-enabled only when billing and provider readiness allow it

### Outbound messaging flow

1. user submits from `/dashboard`
2. app resolves target recipients from community members or event registrations
3. app filters unsubscribed and suppressed contacts
4. immediate sends are queued to RQ
5. scheduled sends are stored in `ScheduledMessage`
6. worker or scheduler calls `TwilioService`
7. logs, suppressions, usage records, and audit records are updated

### Inbound messaging flow

1. Twilio posts to `/webhooks/twilio/inbound`
2. signature validation runs when enabled
3. `inbox_service.process_inbound_sms()` creates or updates thread/message state
4. STOP/START logic updates unsubscribe state
5. keyword automation and survey sessions may send automated replies
6. workspace users view and reply through `/inbox`

### Billing flow

1. checkout session is created from `/setup/billing/checkout` or `/billing/checkout`
2. Stripe sends events to `/webhooks/stripe`
3. `billing_service.process_stripe_webhook_event()` deduplicates via `StripeWebhookEvent`
4. subscription state updates are written to `OrganizationSubscription`
5. usage reconciliation jobs turn outbound message usage into overage billing periods

### Platform operations flow

1. platform admin queues restart from `/platform/operations/restart-services`
2. request is stored in `PlatformServiceRestartRequest`
3. `sms-saas-platform-restart-queue.timer` invokes the queue processor
4. helper script is executed via `sudo -n`
5. final state is written back and recorded as an auth event

## Background Processing

### RQ worker

`app/tasks.py` runs background jobs in isolated app contexts:

- bulk send job
- suppression backfill job
- any queued provider/A2P work

### systemd timers

SaaS production uses timer-driven oneshot jobs instead of long-lived background threads:

- `sms-saas-scheduler.timer`: scheduled outbound sends
- `sms-saas-billing-reconcile.timer`: billing reconciliation
- `sms-saas-platform-restart-queue.timer`: queued restart dispatch/status refresh
- `sms-saas-a2p-reconcile.timer`: Twilio A2P reconciliation

The in-process APScheduler path exists only for development and only starts when explicitly requested.

## Persistence Layers

### Primary SaaS path

- database: PostgreSQL via `DATABASE_URL`
- queue: Redis via `REDIS_URL`
- schema CLI: `./venv/bin/python -m app.saas_db` locally / `saas-dbdoctor` in production

### Legacy compatibility path

- database: SQLite via `DATABASE_URL` defaulting to `instance/sms.db`
- queue: Redis via `REDIS_URL`
- schema CLI: `./venv/bin/python -m app.dbdoctor` locally / `dbdoctor` in production

## Deployment Families

### SaaS family

- root: `/opt/sms-saas`
- gunicorn bind: `127.0.0.1:8100`
- service units: `sms-saas*`
- deploy helpers: `deploy/install_saas.sh`, `deploy/deploy_sms_saas.sh`

### Legacy family

- root: `/opt/sms-admin`
- gunicorn bind: `127.0.0.1:8000`
- service units: `sms*`
- deploy helpers: `deploy/install.sh`, `deploy/deploy_sms_admin.sh`

## Key Constraints

- `dbdoctor` is for the legacy SQLite path only
- non-SQLite SaaS schema management is explicit and should not be handled by legacy migration tooling
- platform admins are not allowed to act as workspace users inside tenant-scoped routes
- outbound sending is blocked until both billing state and messaging provider state allow it
- A2P state is part of workspace readiness, not a standalone admin-only concern
