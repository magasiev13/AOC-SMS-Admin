# SaaS Pilot Rollout

This branch adds a separate SaaS pilot deployment line. Do not deploy it over the legacy `sms` services.

## Separate Runtime

- Use a separate checkout path such as `/opt/sms-saas`
- Use separate service names:
  - `sms-saas.service`
  - `sms-saas-worker.service`
  - `sms-saas-scheduler.service`
  - `sms-saas-scheduler.timer`
  - `sms-saas-billing-reconcile.service`
  - `sms-saas-billing-reconcile.timer`
- Use separate logs under `/var/log/sms-saas`
- Use a separate host or subdomain such as `beta.<host>` or `app.<host>`

## Required SaaS Env

- `SAAS_MODE=1`
- `DATABASE_URL=postgresql+psycopg://...`
- `REDIS_URL=redis://...`
- `RQ_QUEUE_NAME=sms-saas`
- `SAAS_BASE_URL=https://beta.example.com`
- `STRIPE_SECRET_KEY=...`
- `STRIPE_WEBHOOK_SECRET=...`
- `STRIPE_PRICE_ID=...`
- `TWILIO_ACCOUNT_SID=...`
- `TWILIO_AUTH_TOKEN=...`
- `TWILIO_CREDENTIAL_ENCRYPTION_KEY=...`
- `ADMIN_USERNAME=admin` for the first platform admin (optional if `admin` is fine)
- `ADMIN_PASSWORD=...` for first-time platform admin provisioning only

## Platform-Managed Twilio Strategy

- Keep `TWILIO_ACCOUNT_SID=AC...`, `TWILIO_AUTH_TOKEN=...`, and `TWILIO_CREDENTIAL_ENCRYPTION_KEY=...` in `.env`.
- The platform account is the master Twilio account. Each organization should be provisioned with its own Twilio subaccount and Messaging Service.
- Use `/platform/organizations/<id>/messaging` to provision the provider, then assign an approved sender number and phone number SID.
- Per-org Twilio secrets are stored encrypted at rest in the database. Do not add organization-specific tokens to `.env`.
- Messaging stays `pending` until billing is active, compliance is acknowledged, and the sender review is approved.
- The platform admin should not paste `AC...` or `MG...` values into the organization create form. Provisioning happens from the managed messaging screen.

## Stripe Webhooks

- Canonical webhook path: `/webhooks/stripe`
- Required event subscriptions:
  - `checkout.session.completed`
  - `customer.subscription.created`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_succeeded`
  - `invoice.payment_failed`

### Local Development

- Use the Stripe CLI, not a Dashboard event destination.
- Forward to:
  - `stripe listen --forward-to http://127.0.0.1:5000/webhooks/stripe`
- Use the CLI-provided `whsec_...` value as local `STRIPE_WEBHOOK_SECRET`.

## Local Acceptance Runbook

### Start the local stack

1. Populate `.env` with SaaS values:
   - `SAAS_MODE=1`
   - local `DATABASE_URL`
   - local `REDIS_URL`
   - `RQ_QUEUE_NAME=sms-saas`
   - `SAAS_BASE_URL=http://127.0.0.1:5000`
   - Stripe test keys and local `STRIPE_WEBHOOK_SECRET`
2. Apply the explicit SaaS schema:
   - `./venv/bin/python -m app.saas_db --apply`
3. Ensure the first platform admin exists:
   - `./venv/bin/python -m app.saas_db --ensure-platform-admin`
4. Start Redis.
5. Start web + worker:
   - `./run/up.sh`
6. Start the scheduler in a second terminal:
   - `SCHEDULER_ENABLED=1 SCHEDULER_RUNNER=1 ./venv/bin/python -m app.scheduler_runner`
7. Start Stripe webhook forwarding in a third terminal:
   - `stripe listen --forward-to http://127.0.0.1:5000/webhooks/stripe`

### Run the owner + staff flow

1. Sign in as the platform admin.
2. Open `/platform/organizations` and create a business.
3. From the Organizations page, use the visible owner invite link:
   - `Open invite` to launch it
   - `Copy link` if you want to open it in a private window
4. Accept the owner invite.
5. Complete Stripe test checkout.
6. Return to `/platform/organizations/<id>/messaging`.
7. Click `Provision Provider` and confirm the org gets a Twilio subaccount plus Messaging Service.
8. Enter:
   - the approved Twilio sender number
   - the phone number SID for that number
   - `approved` sender review status
   - compliance acknowledgement
9. Confirm the provider status becomes `active`.
10. Open `/users` and create a staff invitation from the owner account.
11. Use the visible staff invite link from the pending invitation table.
12. Accept the staff invite in a separate browser session.
13. Confirm the staff user reaches the dashboard and gets `403` on `/billing`.

### Verify message behavior locally

1. As the owner of a provisioned organization, confirm sending is enabled only when billing is `trialing` or `active` and the provider status is `active`.
2. Verify inbound routing using the organization-owned sender identity.
3. Confirm that unprovisioned organizations remain `pending` for messaging.
4. Create a scheduled send and confirm the scheduler processes it.
5. Run one manual billing reconciliation check:
   - `APP_ROOT="$(pwd)" ./deploy/run_billing_reconcile_once.sh`

### Local acceptance criteria

- No DB inspection or shell token lookup is needed for normal onboarding.
- The Organizations page shows onboarding progress, billing state, messaging state, and owner invite access.
- The Organizations page allows the platform admin to provision, suspend, resume, and review provider readiness per organization.
- The Users page shows pending invitation links for local owner/staff testing.
- The Billing page explains the current state, the next step, and whether sending is enabled.
- Staff users cannot access billing or platform admin surfaces.

## Browser Smoke Tests

### Install once

- `npm install`
- `npm run playwright:install`

### Run the browser suite

- `./run/test_browser.sh`

This launches a deterministic local Flask server on `http://127.0.0.1:5010` using a seeded SQLite database under `.playwright/`.

### Seeded browser accounts

- Platform admin:
  - `platform@browser.test`
  - `Platform-pass1!`
- Owner:
  - `owner@browser.test`
  - `Owner-pass1!`
- Staff:
  - `staff@browser.test`
  - `Staff-pass1!`

### What the browser suite covers

- Platform admin can review onboarding progress and owner invite access.
- Owner sees human-readable billing state and pending invitation links.
- Staff receives `403` on billing.

### Browser test artifacts

- HTML report:
  - `output/playwright/report/`
- Failure traces, video, screenshots:
  - `output/playwright/test-results/`

## Production-Like Demo Seed

- Use `./run/seed_demo_saas.sh --reset` to load a realistic multi-organization local dataset.
- For account credentials, seeded organizations, and the suggested manual acceptance flow, see:
  - [docs/saas-demo-data.md](/Users/magasiev/Desktop/Projects/AOC-SMS-saas/docs/saas-demo-data.md)

### Staging

- Configure a Stripe Dashboard webhook endpoint for:
  - `https://beta.<host>/webhooks/stripe`
- Use a staging-specific webhook secret.

### Production

- Configure a separate Stripe Dashboard webhook endpoint for:
  - `https://app.<host>/webhooks/stripe`
- Use a production-specific webhook secret.
- Do not reuse local CLI webhook secrets in staging or production.

## Separate SaaS Commands

- Install / update schema:
  - `python -m app.saas_db --apply`
- Ensure the first platform admin exists:
  - `python -m app.saas_db --ensure-platform-admin`
- Validate schema readiness:
  - `python -m app.saas_db --doctor`
- Import a legacy production snapshot into one organization during cutover:
  - `python -m app.saas_db --import-legacy /path/to/legacy.db --organization-name "Legacy Production" --organization-slug legacy-production`

## Safety Rules

- Keep legacy production on the baseline tag until the pilot is stable.
- Do not share webhook URLs between legacy and SaaS.
- Do not reuse the legacy SQLite database for SaaS.
- Do not point the SaaS worker at the legacy queue name.
- Do not use `/stripe/webhook`; Stripe must point at `/webhooks/stripe`.
