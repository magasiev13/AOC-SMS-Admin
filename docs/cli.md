# Relayn CLI And Script Reference

This repo ships two families of operational tooling:

- Python CLIs for schema and import management
- shell wrappers for local setup, local runtime, tests, and signoff collection

## Schema CLIs

### `./venv/bin/python -m app.saas_db` / `saas-dbdoctor`

Canonical SaaS schema and import workflow.

Typical commands:

```bash
./venv/bin/python -m app.saas_db --print
./venv/bin/python -m app.saas_db --apply
./venv/bin/python -m app.saas_db --doctor
./venv/bin/python -m app.saas_db --ensure-platform-admin
```

Import helpers:

```bash
./venv/bin/python -m app.saas_db --import-legacy /path/to/legacy.db \
  --organization-name "Legacy Production" \
  --organization-slug legacy-production

./venv/bin/python -m app.saas_db --import-legacy-into-org /path/to/legacy.db \
  --organization-name "Legacy Production" \
  --organization-slug legacy-production \
  --provider-mode platform_managed
```

Installed production wrapper:

```bash
cd /opt/sms-saas
sudo -u smsadmin bash -lc 'cd /opt/sms-saas && set -a && source .env && set +a && saas-dbdoctor --apply'
sudo -u smsadmin bash -lc 'cd /opt/sms-saas && set -a && source .env && set +a && saas-dbdoctor --doctor'
```

The installed wrapper inherits the current shell environment. Source `/opt/sms-saas/.env` before using it directly on the server.

### `./venv/bin/python -m app.dbdoctor` / `dbdoctor`

Legacy SQLite compatibility tooling.

Typical commands:

```bash
./venv/bin/python -m app.dbdoctor --print
./venv/bin/python -m app.dbdoctor --apply
./venv/bin/python -m app.dbdoctor --doctor
```

Important restriction:

- `dbdoctor` rejects the SaaS non-SQLite path

## Local Bootstrap And Runtime Scripts

### `./run/setup.sh`

Creates a Python 3.11 virtualenv, installs requirements plus pytest tooling, preserves an existing `.env`, and ensures `instance/` exists.

### `./run/dev.sh`

Runs the Flask dev server from the local virtualenv.

```bash
./run/dev.sh
```

### `./run/worker.sh`

Starts an RQ worker using `.env` values when present.

```bash
./run/worker.sh
```

Defaults:

- `REDIS_URL=redis://localhost:6379/0`
- `RQ_QUEUE_NAME=sms`

### `./run/up.sh`

Starts the worker in the background and then launches the Flask dev server. Useful for the legacy flow or manual SaaS work when you are managing the scheduler and Stripe CLI separately.

### `./run/local_saas_stack.sh`

Primary local SaaS wrapper. It can:

- seed demo data
- start Redis-dependent app and worker processes
- run local scheduler
- start Stripe CLI forwarding
- optionally open the login page

Usage:

```bash
./run/local_saas_stack.sh --no-open
./run/local_saas_stack.sh --no-seed
./run/local_saas_stack.sh --keep-data
./run/local_saas_stack.sh --live-from-number +15551234567
```

Requirements:

- working local Redis
- Stripe CLI available as `stripe`
- valid SaaS env in `.env`

### `./run/seed_demo_saas.sh`

Seeds a deterministic multi-tenant SaaS demo environment through `app.demo_seed`.

```bash
./run/seed_demo_saas.sh --reset
./run/seed_demo_saas.sh --reset --live-from-number +15551234567 --live-messaging-service-sid MG...
```

## Verification And Test Wrappers

### `./run/verify.sh`

Static verification wrapper:

```bash
./run/verify.sh
```

What it does:

- uses the repo virtualenv Python when available
- enforces Python 3.11
- runs `compileall` over `app` and `tests`

### `./run/test.sh`

Pytest wrapper:

```bash
./run/test.sh
./run/test.sh --cov=app
./run/test.sh tests/test_billing_webhooks.py
```

What it does:

- bootstraps dependencies if needed
- enforces Python 3.11
- defaults to `pytest tests` when only option flags are passed
- uses `--import-mode=importlib`

### `./run/test_browser.sh`

Playwright browser wrapper:

```bash
./run/test_browser.sh
./run/test_browser.sh --headed
```

What it does:

- installs `node_modules` if missing
- installs Chromium if the Playwright cache is missing
- runs `npm run test:browser`

## Signoff And Evidence Collection

### `./run/public_readiness_local.sh`

Runs the deterministic local readiness gate and writes artifacts under:

```text
output/signoff/<run-id>/local/
```

### `./run/public_readiness_beta_snapshot.sh`

Collects read-only beta evidence for one organization slug and label:

```bash
./run/public_readiness_beta_snapshot.sh \
  --org-slug public-readiness-control \
  --label baseline
```

Artifacts land under:

```text
output/signoff/<run-id>/beta/<label>/
```

## Direct Runtime Commands

### Flask

```bash
flask --app wsgi:app run --debug
flask --app wsgi:app shell
flask --app wsgi:app routes
```

### Worker

Equivalent worker command:

```bash
./venv/bin/rq worker --url "$REDIS_URL" "$RQ_QUEUE_NAME"
```

### Development scheduler

```bash
SCHEDULER_ENABLED=1 SCHEDULER_RUNNER=1 ./venv/bin/python -m app.scheduler_runner
```

Production should use systemd timers instead of the in-process scheduler.

## Deploy Helpers

### SaaS

```bash
sudo ./deploy/install_saas.sh
sudo ./deploy/deploy_sms_saas.sh
```

### Legacy compatibility

```bash
sudo ./deploy/install.sh
sudo ./deploy/deploy_sms_admin.sh
```
