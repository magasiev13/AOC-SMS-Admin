# Relayn SaaS Operations

Day-2 operational runbook for the primary SaaS deployment line.

## Deployment Topology

Canonical SaaS deployment:

- app root: `/opt/sms-saas`
- app user: `smsadmin`
- env file: `/opt/sms-saas/.env`
- web bind: `127.0.0.1:8100`
- queue name: `sms-saas`
- service family: `sms-saas*`

Keep this deployment isolated from the legacy line:

- do not share `/opt/sms-admin`
- do not share the legacy SQLite DB
- do not point the SaaS worker at queue `sms`

## Required Runtime State

Minimum expected env:

- `SAAS_MODE=1`
- `DATABASE_URL=postgresql+psycopg://...`
- `REDIS_URL=redis://localhost:6379/0`
- `RQ_QUEUE_NAME=sms-saas`
- `SAAS_BASE_URL=https://app.example.com`
- `SECRET_KEY=...`
- `STRIPE_SECRET_KEY=...`
- `STRIPE_WEBHOOK_SECRET=...`
- `STRIPE_PRICE_ID=...`
- `TWILIO_ACCOUNT_SID=...`
- `TWILIO_AUTH_TOKEN=...`
- `TWILIO_CREDENTIAL_ENCRYPTION_KEY=...`
- `TRUSTED_HOSTS=...`

Conditional env:

- `TWILIO_API_KEY_SID` and `TWILIO_API_KEY_SECRET`
- `TWILIO_A2P_ONBOARDING_ENABLED=1`
- `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID=BU...`
- `PLATFORM_SERVICE_RESTART_ENABLED=1`

Bootstrap-only values:

- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ADMIN_EMAIL`

Once the first platform admin exists, `ADMIN_PASSWORD` is no longer required for routine deploys.

## Core Operational Commands

### Schema and readiness

```bash
cd /opt/sms-saas
sudo -u smsadmin bash -lc 'cd /opt/sms-saas && set -a && source .env && set +a && saas-dbdoctor --print'
sudo -u smsadmin bash -lc 'cd /opt/sms-saas && set -a && source .env && set +a && saas-dbdoctor --apply'
sudo -u smsadmin bash -lc 'cd /opt/sms-saas && set -a && source .env && set +a && saas-dbdoctor --doctor'
sudo -u smsadmin bash -lc 'cd /opt/sms-saas && set -a && source .env && set +a && saas-dbdoctor --ensure-platform-admin'
```

### Service checks

```bash
sudo systemctl status sms-saas sms-saas-worker sms-saas-scheduler.timer --no-pager
sudo systemctl status sms-saas-billing-reconcile.timer sms-saas-platform-restart-queue.timer sms-saas-a2p-reconcile.timer --no-pager
```

### Health check

```bash
curl -fsS -H "Host: app.example.com" http://127.0.0.1:8100/health
```

## Deploy Updates

Canonical update flow:

```bash
cd /opt/sms-saas
sudo ./deploy/deploy_sms_saas.sh
```

What it refreshes:

- git checkout contents
- Python dependencies
- SaaS schema migrations
- platform-admin bootstrap state
- restart helper and sudoers
- systemd unit files
- active SaaS services and timers

## Timers And Background Jobs

### Scheduler

- timer: `sms-saas-scheduler.timer`
- service: `sms-saas-scheduler.service`
- role: due scheduled sends and retry processing

### Billing reconciliation

- timer: `sms-saas-billing-reconcile.timer`
- service: `sms-saas-billing-reconcile.service`
- role: subscription/usage reconciliation and overage posting

### Platform restart queue

- timer: `sms-saas-platform-restart-queue.timer`
- service: `sms-saas-platform-restart-queue.service`
- role: queued restart dispatch and status refresh

### A2P reconciliation

- timer: `sms-saas-a2p-reconcile.timer`
- service: `sms-saas-a2p-reconcile.service`
- role: Twilio A2P state refresh and recovery

## Restart Helper Operations

When platform restart control is enabled:

- web requests only queue `PlatformServiceRestartRequest` rows
- host restarts happen out-of-band through `restart-sms-saas-services`
- the helper must be runnable by `smsadmin` via `sudo -n`

Validation command:

```bash
sudo -u smsadmin sudo -n /usr/local/bin/restart-sms-saas-services --check
```

## Backup And Restore

### PostgreSQL

Backup:

```bash
sudo -u smsadmin bash -lc '
  cd /opt/sms-saas &&
  set -a &&
  source .env &&
  set +a &&
  pg_dump "$DATABASE_URL" > /var/backups/relayn-$(date +%Y%m%d-%H%M%S).sql
'
```

Restore into a fresh target:

```bash
TARGET_DATABASE_URL='postgresql://user:password@127.0.0.1:5432/relayn_restore'
psql "$TARGET_DATABASE_URL" < /var/backups/relayn-YYYYMMDD-HHMMSS.sql
```

### Redis

Use managed persistence or copy the Redis persistence files during a controlled window.

Simple local example:

```bash
sudo systemctl stop redis-server
sudo cp /var/lib/redis/dump.rdb /var/backups/redis-dump-$(date +%Y%m%d-%H%M%S).rdb
sudo systemctl start redis-server
```

### Critical local files

Also retain:

- `/opt/sms-saas/.env`
- reverse-proxy config
- any deploy-specific secrets or CI metadata outside the repo

## Cutover From Legacy

Recommended cutover flow:

1. freeze writes on the legacy app
2. capture a final SQLite snapshot
3. import the snapshot into SaaS with `app.saas_db`
4. verify users, invites, recipients, logs, scheduled messages, and inbox state
5. switch traffic to the SaaS runtime
6. keep the legacy snapshot for rollback and audit

Example import:

```bash
./venv/bin/python -m app.saas_db --import-legacy /path/to/legacy.db \
  --organization-name "Legacy Production" \
  --organization-slug legacy-production
```

## Legacy Compatibility Note

Use `dbdoctor` and the `/opt/sms-admin` service family only for the legacy deployment. Do not mix legacy schema tools with the SaaS PostgreSQL database.
