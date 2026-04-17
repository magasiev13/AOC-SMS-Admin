# Twinevia Deployment Guide

This is the canonical production deployment guide for Twinevia.

Primary target:

- `SAAS_MODE=1`
- PostgreSQL
- Redis
- `/opt/twinevia-saas`
- `twinevia-saas*` systemd units

The `Twinevia Legacy` deployment line is still supported, but it is documented only in the appendix.

## SaaS Production Prerequisites

- Debian or Ubuntu with Python 3.11 available
- systemd
- Redis
- PostgreSQL
- a reverse proxy that can forward the public Host header to `127.0.0.1:8100`
- Stripe account and webhook secret
- Twilio parent/master account

## Recommended Layout

- app root: `/opt/twinevia-saas`
- app user/group: `twinevia`
- env file: `/opt/twinevia-saas/.env`
- logs: `/var/log/twinevia-saas`
- gunicorn bind: `127.0.0.1:8100`

For upgraded hosts that still run the SaaS checkout as `smsadmin`, the install and deploy scripts keep working when `APP_USER=smsadmin` and `APP_GROUP=smsadmin` are set explicitly.

## 1. Create The App User And Checkout

```bash
sudo adduser --system --group --home /opt/twinevia-saas --shell /bin/bash twinevia
sudo install -d -o twinevia -g twinevia /opt/twinevia-saas
sudo install -d -o twinevia -g twinevia /var/log/twinevia-saas

sudo -u twinevia git clone <repo-url> /opt/twinevia-saas
```

## 2. Prepare PostgreSQL And Redis

Create a dedicated PostgreSQL role and database, then ensure Redis is active.

Example:

```bash
sudo systemctl enable --now postgresql redis-server
```

The repo does not assume managed infrastructure here; local services on the VPS are fine as long as `DATABASE_URL` and `REDIS_URL` are correct.

## 3. Create `/opt/twinevia-saas/.env`

Minimum production shape:

```env
FLASK_ENV=production
FLASK_DEBUG=0
TRUST_PROXY=1
TRUSTED_HOSTS=app.example.com
SAAS_MODE=1
SCHEDULER_ENABLED=0
DATABASE_URL=postgresql+psycopg://user:password@127.0.0.1:5432/twinevia
REDIS_URL=redis://localhost:6379/0
RQ_QUEUE_NAME=twinevia-saas
SAAS_BASE_URL=https://app.example.com
SECRET_KEY=replace-me
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-me
STRIPE_SECRET_KEY=sk_live_replace_me
STRIPE_WEBHOOK_SECRET=whsec_replace_me
STRIPE_PRICE_ID=price_replace_me
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=replace_me
TWILIO_CREDENTIAL_ENCRYPTION_KEY=replace_me
STRIPE_FAKE_CHECKOUT_ENABLED=0
TWILIO_BROWSER_FAKE_SENDS=0
TWILIO_A2P_FAKE_QUEUE=0
```

Add these when applicable:

- `TWILIO_API_KEY_SID` and `TWILIO_API_KEY_SECRET`
- `TWILIO_A2P_ONBOARDING_ENABLED=1`
- `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID=BU...`
- `PLATFORM_SERVICE_RESTART_ENABLED=1`

The SaaS install/deploy scripts now refuse to continue when the runtime still
looks like local development. In practice that means:

- `FLASK_ENV` must be `production`
- `FLASK_DEBUG` must be unset or `0`
- `DATABASE_URL` must use PostgreSQL, not SQLite
- `TRUSTED_HOSTS` must contain real public hostnames, not only `localhost`
- `TRUST_PROXY=1`
- `SESSION_COOKIE_SECURE=1`
- `REMEMBER_COOKIE_SECURE=1`
- `SESSION_COOKIE_SAMESITE` must be `Lax` or `Strict`
- fake/test flags must stay off: `STRIPE_FAKE_CHECKOUT_ENABLED=0`, `TWILIO_BROWSER_FAKE_SENDS=0`, `TWILIO_A2P_FAKE_QUEUE=0`

File permissions should be:

```bash
sudo chown root:twinevia /opt/twinevia-saas/.env
sudo chmod 660 /opt/twinevia-saas/.env
```

## 4. Run The SaaS Installer

```bash
cd /opt/twinevia-saas
sudo ./deploy/install_saas.sh
```

What `install_saas.sh` does:

- ensures the Python 3.11 virtualenv exists
- installs `twinevia-saas-dbdoctor`
- installs the `saas-dbdoctor` compatibility alias
- installs `restart-twinevia-saas-services`
- installs the matching sudoers rule
- creates or normalizes the env file permissions
- appends key SaaS defaults when missing
- installs Python dependencies
- applies SaaS schema migrations
- ensures the first platform admin exists
- installs and enables `twinevia-saas*` services and timers
- validates restart-helper access
- runs a local health check against `127.0.0.1:8100/health`

## 5. Runtime Validation

The deploy flow validates config before restarting:

```bash
cd /opt/twinevia-saas
sudo -u twinevia bash -lc 'set -a; source .env; set +a; ./venv/bin/python - <<'"'"'PY'"'"'
from app import create_app
create_app(run_startup_tasks=False, start_scheduler=False)
print("SaaS app config validation ok")
PY'
```

This is the same validation path used by the deploy scripts and is the right first check when a config change fails.

## 6. SaaS Systemd Units

Installed runtime units:

- `twinevia-saas.service`
- `twinevia-saas-worker.service`
- `twinevia-saas-scheduler.service`
- `twinevia-saas-scheduler.timer`
- `twinevia-saas-billing-reconcile.service`
- `twinevia-saas-billing-reconcile.timer`
- `twinevia-saas-platform-restart-queue.service`
- `twinevia-saas-platform-restart-queue.timer`
- `twinevia-saas-a2p-reconcile.service`
- `twinevia-saas-a2p-reconcile.timer`

Enabled runtime set:

- `twinevia-saas`
- `twinevia-saas-worker`
- `twinevia-saas-scheduler.timer`
- `twinevia-saas-billing-reconcile.timer`
- `twinevia-saas-platform-restart-queue.timer`
- `twinevia-saas-a2p-reconcile.timer`

## 7. Health And Service Checks

Basic checks:

```bash
sudo systemctl status twinevia-saas twinevia-saas-worker twinevia-saas-scheduler.timer
sudo systemctl status twinevia-saas-billing-reconcile.timer twinevia-saas-platform-restart-queue.timer twinevia-saas-a2p-reconcile.timer
curl -fsS -H "Host: app.example.com" http://127.0.0.1:8100/health
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --doctor'
```

Important detail:

- when `TRUSTED_HOSTS` is configured, local health checks must send an allowed `Host` header
- direct `twinevia-saas-dbdoctor` wrapper calls should run from `/opt/twinevia-saas` with `.env` sourced first

## 8. Deploy Updates

Routine SaaS deploys should use:

```bash
sudo ./deploy/deploy_twinevia_saas.sh
```

That script:

- pulls latest code
- syncs restart helper and systemd artifacts
- installs Python dependencies
- applies SaaS migrations
- ensures the first platform admin exists
- validates config
- enables and restarts the runtime units
- validates the restart helper
- retries the health check on `127.0.0.1:8100`

## 9. Reverse Proxy

This repo does not currently ship a dedicated SaaS nginx config file.

Your reverse proxy must:

- forward the original `Host` header
- forward `X-Forwarded-*` headers when `TRUST_PROXY=1`
- proxy the app to `127.0.0.1:8100`
- allow `/health` through for monitoring

If you reuse the legacy nginx sample, update:

- server name
- upstream port
- auth requirements
- static path assumptions

Do not proxy SaaS traffic to the legacy `sms.service` port.

## 10. Stripe And Twilio Production Notes

### Stripe

- production webhook path: `/webhooks/stripe`
- configure `STRIPE_WEBHOOK_SECRET` from the live endpoint
- do not reuse CLI or staging webhook secrets

### Twilio

- `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` should point at the parent/master account in platform-managed SaaS
- if `TWILIO_A2P_ONBOARDING_ENABLED=1`, `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID` must be a primary `BU...` profile
- customer-managed org secrets are stored encrypted in the database, not in `.env`

## 11. Backup Expectations

At minimum, back up:

- PostgreSQL
- Redis persistence files or managed Redis snapshots
- `/opt/twinevia-saas/.env`
- `/var/log/twinevia-saas` if your incident process relies on local log retention

See [saas-operations.md](saas-operations.md) for day-2 backup and restore guidance.

## Legacy Compatibility Appendix

Use the legacy line only when you intentionally need the older single-tenant deployment.

### Legacy deploy roots

- app root: `/opt/sms-admin`
- direct health target: `127.0.0.1:8000`
- units: `sms.service`, `sms-worker.service`, `sms-scheduler.timer`
- CLI: `dbdoctor`

### Legacy install/update commands

```bash
sudo ./deploy/install.sh
sudo ./deploy/deploy_sms_admin.sh
```

### Legacy-specific notes

- SQLite permissions on `/opt/sms-admin/instance` matter
- `TWILIO_FROM_NUMBER` is a direct runtime dependency there
- the repo ships a legacy nginx sample at `deploy/nginx.conf`
