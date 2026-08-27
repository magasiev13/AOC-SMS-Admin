# Twinevia SaaS Rollout And Local Acceptance

This document covers the separate SaaS runtime, local acceptance flow, and rollout safety rules.

## Separate Runtime Rule

Treat the SaaS deployment as distinct from the legacy `sms` line.

- use `/opt/twinevia-saas`
- use `twinevia-saas*` units
- use queue `twinevia-saas`
- use a separate database
- use separate Stripe and Twilio webhook endpoints

Do not deploy the SaaS runtime over the legacy SQLite services.

## Required SaaS Env

Minimum runtime values:

- `SAAS_MODE=1`
- `DATABASE_URL=postgresql+psycopg://...`
- `REDIS_URL=redis://...`
- `RQ_QUEUE_NAME=twinevia-saas`
- `SAAS_BASE_URL=https://app.example.com`
- `STRIPE_SECRET_KEY=...`
- `STRIPE_WEBHOOK_SECRET=...`
- `STRIPE_PRICE_ID=...` or `STRIPE_MONTHLY_PRICE_ID=...`
- `STRIPE_ANNUAL_PRICE_ID=...`
- `STRIPE_ACTIVATION_PRICE_ID=...`
- `TWILIO_ACCOUNT_SID=...`
- `TWILIO_AUTH_TOKEN=...`
- `TWILIO_CREDENTIAL_ENCRYPTION_KEY=...`
- `SECRET_KEY=...`

Conditional:

- `TWILIO_API_KEY_SID` and `TWILIO_API_KEY_SECRET`
- `TWILIO_A2P_ONBOARDING_ENABLED=1`
- `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID=BU...`
- `BILLING_ANNUAL_ONLY_ORG_SLUGS` / `BILLING_ANNUAL_ONLY_ORG_IDS` only as break-glass checkout-offer overrides

Bootstrap-only:

- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

## First-Client Annual Upfront Offer

Use the platform admin UI before inviting or sending the first client to checkout:

1. Create or open the organization in Platform admin.
2. Open Access.
3. In Billing State, enable annual-only checkout.
4. Confirm the owner checkout page shows `First-client upfront pricing` and no `$59.99/mo` option.

Do not use manual payment tracking for this path. Stripe Checkout should charge the `$149.99` setup fee plus the `$600/year` subscription.

## Platform-Managed Messaging Strategy

- the parent/master Twilio account lives in `.env`
- each org gets its own provider state in `OrganizationMessagingProfile`
- customer-managed org secrets are stored encrypted in the DB
- platform-managed org provisioning and sender review happen under `/platform/organizations/<id>/messaging`
- A2P submission and review happen under `/platform/organizations/<id>/messaging/onboarding`

An org is send-ready only when:

- billing allows sending
- provider status is active
- a valid sender identity exists

## Stripe Webhook Path

Canonical path:

- `/webhooks/stripe`

Local development should use the Stripe CLI:

```bash
stripe listen --forward-to http://127.0.0.1:5000/webhooks/stripe
```

## Local Acceptance Flow

### Fast path

```bash
./run/local_saas_stack.sh --no-open
```

This wraps:

- demo seeding
- web app
- worker
- local scheduler
- Stripe CLI webhook forwarding

### Manual path

1. configure `.env` for SaaS
2. apply schema:

```bash
./venv/bin/python -m app.saas_db --apply
./venv/bin/python -m app.saas_db --ensure-platform-admin
```

3. start Redis
4. start app and worker:

```bash
./run/up.sh
```

5. start scheduler:

```bash
SCHEDULER_ENABLED=1 SCHEDULER_RUNNER=1 ./venv/bin/python -m app.scheduler_runner
```

6. start Stripe CLI forwarding:

```bash
stripe listen --forward-to http://127.0.0.1:5000/webhooks/stripe
```

## Owner And Staff Acceptance Checklist

1. sign in as a platform admin
2. create an organization
3. open the generated owner invite
4. accept the invite and verify the owner lands on `/setup`
5. complete checkout
6. confirm setup/billing state updates
7. provision or validate provider readiness from the platform side
8. if using A2P, save and submit onboarding or verify the correct pending state
9. create a staff invite
10. accept the staff invite in a separate session
11. confirm staff access to the workspace and `403` on billing/platform routes

## Local Messaging Verification

As an owner of a provisioned org, verify:

- sending stays blocked until billing and messaging state are both ready
- inbox activity routes to the org-owned sender identity
- scheduled sends are processed by the scheduler path
- logs and usage state update after sends

## Browser Smoke Tests

Install once:

```bash
npm install
npm run playwright:install
```

Run:

```bash
./run/test_browser.sh
```

The browser suite uses a deterministic local app instance and writes artifacts under `output/playwright/`.

## Production-Like Demo Seed

Use:

```bash
./run/seed_demo_saas.sh --reset
```

For seeded users and tenants, see [saas-demo-data.md](saas-demo-data.md).

## Rollout Safety Rules

- keep legacy and SaaS webhook URLs separate
- keep legacy and SaaS databases separate
- do not reuse queue `sms` for SaaS
- do not point SaaS health checks at the legacy gunicorn port
- do not treat Twilio ownership conflicts as a retryable “just try again” problem
- for production deploys, keep the existing PostgreSQL and Redis data plane and use `./run/production_cutover.sh` so snapshots, backups, branch guards, and post-deploy parity checks happen in one sequence
