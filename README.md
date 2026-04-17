# Twinevia

Twinevia is a multi-tenant messaging workspace for organizations that need owner/staff access, billing, Twilio sender provisioning, A2P onboarding, inbox automation, scheduled messaging, and operational auditability.

This repository still contains the original single-tenant `Twinevia Legacy` runtime, but the SaaS/PostgreSQL deployment line is the primary production path and the default perspective for this documentation set.

## What This Repo Contains

- SaaS control plane flows for signup, invites, setup, billing, and platform administration
- Tenant-scoped messaging workspaces with dashboard sends, community and event recipients, inbox, surveys, keywords, logs, and scheduled messages
- Twilio provider provisioning for platform-managed or customer-managed messaging
- Stripe-backed subscription and usage billing flows
- Redis/RQ worker processing plus systemd timer-driven background jobs
- Browser smoke tests, deterministic signoff scripts, and deploy helpers for both SaaS and legacy runtimes

## Runtime Modes

### Primary Production Runtime: Twinevia

- `SAAS_MODE=1`
- PostgreSQL-backed application database
- Redis queue name `twinevia-saas`
- Explicit SaaS schema tooling via `./venv/bin/python -m app.saas_db` locally or `twinevia-saas-dbdoctor` in production
- Compatibility alias: `saas-dbdoctor`
- SaaS deploy units under `deploy/twinevia-saas*`
- Canonical production root: `/opt/twinevia-saas`

### Secondary Compatibility Runtime: Twinevia Legacy

- `SAAS_MODE=0`
- SQLite-backed application database
- Redis queue name `sms`
- Legacy schema tooling via `./venv/bin/python -m app.dbdoctor` locally or `dbdoctor` in production
- Legacy deploy units under `deploy/sms*`
- Canonical legacy root: `/opt/sms-admin`

The docs in `docs/` are SaaS-first. Legacy details remain documented only where needed for compatibility, migration, or support of the existing deploy line.

## Repo Map

- `app/`: Flask app factory, models, routes, tenant scoping, services, templates, static assets
- `app/migrations/`: legacy SQLite migration system
- `app/saas_migrations/`: explicit SaaS schema migration system
- `bin/`: installed CLI wrappers such as `dbdoctor`, `twinevia-saas-dbdoctor`, and the retained SaaS alias `saas-dbdoctor`
- `deploy/`: systemd units, install scripts, restart helpers, and deploy wrappers
- `docs/`: reference docs, runbooks, rollout guides, and signoff procedures
- `run/`: local setup, local stack startup, test wrappers, demo seeding, and signoff scripts
- `tests/`: pytest suite and Playwright browser coverage

## Local SaaS Quick Start

### 1. Bootstrap the repo

```bash
./run/setup.sh
cp .env.example .env
```

The setup wrapper creates a Python 3.11 virtualenv, installs runtime and test dependencies, and preserves an existing `.env`. `.env.example` is a local bootstrap sample only; do not copy it directly onto a live SaaS host. Use [docs/deployment.md](docs/deployment.md) and [docs/ubuntu-vps-saas-checklist.md](docs/ubuntu-vps-saas-checklist.md) for the production env contract.

### 2. Fill the minimum SaaS settings

At minimum, local SaaS work normally needs:

- `SAAS_MODE=1`
- `DATABASE_URL=...`
- `REDIS_URL=redis://localhost:6379/0`
- `RQ_QUEUE_NAME=twinevia-saas`
- `SAAS_BASE_URL=http://127.0.0.1:5000`
- `STRIPE_SECRET_KEY=...`
- `STRIPE_WEBHOOK_SECRET=...`
- `STRIPE_PRICE_ID=...`
- `TWILIO_ACCOUNT_SID=...`
- `TWILIO_AUTH_TOKEN=...`
- `TWILIO_CREDENTIAL_ENCRYPTION_KEY=...`
- `SECRET_KEY=...`

See [docs/configuration.md](docs/configuration.md) for the full config surface.

### 3. Apply the SaaS schema and provision the first platform admin

```bash
./venv/bin/python -m app.saas_db --apply
./venv/bin/python -m app.saas_db --ensure-platform-admin
```

### 4. Start the local stack

All-in-one local SaaS stack:

```bash
./run/local_saas_stack.sh --no-open
```

That wrapper expects:

- a working local Redis instance
- the Stripe CLI (`stripe`) for local webhook forwarding
- valid SaaS env in `.env`

Manual alternative:

```bash
./run/up.sh
SCHEDULER_ENABLED=1 SCHEDULER_RUNNER=1 ./venv/bin/python -m app.scheduler_runner
stripe listen --forward-to http://127.0.0.1:5000/webhooks/stripe
```

## Quality Gates

Backend verification:

```bash
./run/verify.sh
./run/test.sh
```

Browser smoke coverage:

```bash
./run/test_browser.sh
```

Deterministic signoff artifacts:

```bash
./run/public_readiness_local.sh
./run/public_readiness_production_snapshot.sh --org-slug public-readiness-control --label baseline
TWINEVIA_OWNER_USERNAME=owner@example.com \
TWINEVIA_OWNER_PASSWORD=... \
TWINEVIA_PLATFORM_USERNAME=platform@example.com \
TWINEVIA_PLATFORM_PASSWORD=... \
./run/public_readiness_live_smoke.sh
```

Artifacts are written under `output/`.

## Production Overview

### SaaS Production

- install: `sudo ./deploy/install_saas.sh`
- update: `sudo ./deploy/deploy_twinevia_saas.sh`
- web service: `twinevia-saas.service`
- worker: `twinevia-saas-worker.service`
- timers: `twinevia-saas-scheduler.timer`, `twinevia-saas-billing-reconcile.timer`, `twinevia-saas-platform-restart-queue.timer`, `twinevia-saas-a2p-reconcile.timer`
- direct health target: `http://127.0.0.1:8100/health`

### Legacy Compatibility Deployment

- install: `sudo ./deploy/install.sh`
- update: `sudo ./deploy/deploy_sms_admin.sh`
- web service: `sms.service`
- worker: `sms-worker.service`
- timer: `sms-scheduler.timer`
- direct health target: `http://127.0.0.1:8000/health`

See [docs/deployment.md](docs/deployment.md) for the SaaS production guide and legacy appendix.

## Documentation Index

- [Documentation Overview](docs/README.md)
- [Architecture](docs/architecture.md)
- [API Reference](docs/api.md)
- [Database](docs/database.md)
- [Services](docs/services.md)
- [Configuration](docs/configuration.md)
- [CLI Tools](docs/cli.md)
- [Deployment](docs/deployment.md)
- [SaaS Operations](docs/saas-operations.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Public Readiness Signoff](docs/public-readiness-signoff.md)

## Naming Note

User-facing docs refer to the product as `Twinevia`. The primary SaaS runtime now uses the `twinevia-saas` family for service units, deploy roots, logs, queue names, restart helpers, Unix account defaults, and the canonical `twinevia-saas-dbdoctor` wrapper. The retained `saas-dbdoctor` alias and the legacy `/opt/sms-admin` runtime remain intentional compatibility surfaces. External repo/worktree naming is out of repo scope.
