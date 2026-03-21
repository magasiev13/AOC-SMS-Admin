# Ubuntu VPS SaaS Pilot Checklist

Step-by-step checklist for bringing up the SaaS pilot branch on a separate Ubuntu VPS before putting any public proxy in front of it.

This assumes:

- Ubuntu with `python3.11` available through `apt`
- a fresh separate checkout at `/opt/sms-saas`
- PostgreSQL and Redis running on the same VPS
- the SaaS branch is `codex/saas-pilot-v2`
- you want the app reachable locally on the VPS at `127.0.0.1:8100` first

Do not point this deployment at the legacy SQLite database or the legacy `sms` systemd services.

## 1. Install Base Packages

SSH into the VPS and install the runtime packages:

```bash
sudo apt update
sudo apt install -y \
  git \
  curl \
  build-essential \
  python3.11 \
  python3.11-venv \
  python3-pip \
  postgresql \
  postgresql-contrib \
  redis-server
```

Start and enable PostgreSQL and Redis:

```bash
sudo systemctl enable --now postgresql redis-server
```

Confirm Python 3.11 exists before going further:

```bash
python3.11 --version
```

If `python3.11` is not available, stop here and fix the OS image first. This branch is pinned to Python 3.11.

## 2. Create The App User And Directories

Create the dedicated service account and directories:

```bash
sudo adduser --system --group --home /opt/sms-saas --shell /bin/bash smsadmin
sudo install -d -o smsadmin -g smsadmin /opt/sms-saas
sudo install -d -o smsadmin -g smsadmin /var/log/sms-saas
```

## 3. Clone The SaaS Branch

Clone the branch directly into the SaaS path:

```bash
sudo -u smsadmin git clone \
  --branch codex/saas-pilot-v2 \
  https://github.com/magasiev13/AOC-SMS-Admin.git \
  /opt/sms-saas
```

Verify the checkout:

```bash
sudo -u smsadmin bash -lc 'cd /opt/sms-saas && git branch --show-current && git rev-parse --short HEAD'
```

## 4. Create The PostgreSQL Database

Create a dedicated PostgreSQL role and database for the SaaS app:

```bash
sudo -u postgres psql <<'SQL'
CREATE USER sms_saas WITH PASSWORD 'REPLACE_WITH_STRONG_DB_PASSWORD';
CREATE DATABASE sms_saas OWNER sms_saas;
ALTER ROLE sms_saas SET client_encoding TO 'UTF8';
ALTER ROLE sms_saas SET timezone TO 'UTC';
SQL
```

Test the connection:

```bash
PGPASSWORD='REPLACE_WITH_STRONG_DB_PASSWORD' psql \
  -h 127.0.0.1 \
  -U sms_saas \
  -d sms_saas \
  -c 'select current_database(), current_user, now();'
```

## 5. Create The SaaS `.env`

Create `/opt/sms-saas/.env` with the required SaaS settings:

```bash
sudo tee /opt/sms-saas/.env >/dev/null <<'EOF'
FLASK_ENV=production
SAAS_MODE=1
TRUST_PROXY=1
TRUSTED_HOSTS=beta.example.com
SCHEDULER_ENABLED=0
RQ_QUEUE_NAME=sms-saas
REDIS_URL=redis://localhost:6379/0
DATABASE_URL=postgresql+psycopg://sms_saas:REPLACE_WITH_STRONG_DB_PASSWORD@127.0.0.1:5432/sms_saas
SAAS_BASE_URL=https://beta.example.com
SECRET_KEY=REPLACE_WITH_LONG_RANDOM_SECRET
ADMIN_USERNAME=admin
ADMIN_PASSWORD=REPLACE_WITH_STRONG_ADMIN_PASSWORD
ADMIN_EMAIL=admin@example.com
STRIPE_SECRET_KEY=sk_test_or_live_replace_me
STRIPE_WEBHOOK_SECRET=whsec_replace_me
STRIPE_PRICE_ID=price_replace_me
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=replace_me
TWILIO_CREDENTIAL_ENCRYPTION_KEY=REPLACE_WITH_VALID_FERNET_KEY
APP_TIMEZONE=America/Denver
AUTH_PASSWORD_POLICY_ENFORCE=1
AUTH_PASSWORD_MIN_LENGTH=12
AUTH_ATTEMPT_WINDOW_SECONDS=300
AUTH_LOCKOUT_SECONDS=900
AUTH_MAX_ATTEMPTS_IP_ACCOUNT=5
AUTH_MAX_ATTEMPTS_ACCOUNT=8
AUTH_MAX_ATTEMPTS_IP=30
SESSION_IDLE_TIMEOUT_MINUTES=30
REMEMBER_COOKIE_DURATION_DAYS=7
SESSION_COOKIE_SAMESITE=Lax
SESSION_COOKIE_SECURE=1
EOF
sudo chown root:smsadmin /opt/sms-saas/.env
sudo chmod 660 /opt/sms-saas/.env
```

Adjust at least these placeholders before continuing:

- `beta.example.com`
- PostgreSQL password
- `SECRET_KEY`
- `ADMIN_PASSWORD`
- Stripe values
- Twilio values
- `TWILIO_CREDENTIAL_ENCRYPTION_KEY`

## 6. Create The Virtualenv And Install Python Dependencies

```bash
sudo -u smsadmin bash -lc '
  cd /opt/sms-saas &&
  python3.11 -m venv venv &&
  ./venv/bin/pip install --upgrade pip &&
  ./venv/bin/pip install -r requirements.txt
'
```

## 7. Apply And Verify The SaaS Schema

Run the explicit SaaS schema workflow:

```bash
sudo -u smsadmin bash -lc '
  cd /opt/sms-saas &&
  set -a &&
  source .env &&
  set +a &&
  ./venv/bin/python -m app.saas_db --apply &&
  ./venv/bin/python -m app.saas_db --ensure-platform-admin &&
  ./venv/bin/python -m app.saas_db --doctor
'
```

You should get a clean doctor result before starting the app.

## 8. Validate App Config Without Starting Services

Run the same app config validation used by the deploy script:

```bash
sudo -u smsadmin bash -lc 'set -euo pipefail
cd /opt/sms-saas
set -a
source .env
set +a
./venv/bin/python - <<'"'"'"'"'"'"'"'"'PY'"'"'"'"'"'"'"'"'
from app import create_app

create_app(run_startup_tasks=False, start_scheduler=False)
print("SaaS app config validation ok")
PY'
```

If this fails, fix `.env` before moving on.

## 9. Run The Web App Locally On The VPS

Start the app manually, bound only to localhost:

```bash
sudo -u smsadmin bash -lc 'set -euo pipefail
cd /opt/sms-saas
set -a
source .env
set +a
./venv/bin/gunicorn \
  --workers 2 \
  --bind 127.0.0.1:8100 \
  wsgi:app'
```

Leave that running in the first terminal. In a second terminal on the VPS, hit the health endpoint:

```bash
curl -fsS http://127.0.0.1:8100/health
```

Expected result:

```json
{"ok":true}
```

If you want to open the app from your laptop before setting up a public proxy, create an SSH tunnel:

```bash
ssh -L 8100:127.0.0.1:8100 ubuntu@YOUR_VPS_IP
```

Then open:

- `http://127.0.0.1:8100/login`

Stop the manual Gunicorn process after this smoke check.

## 10. Optional Manual Worker Smoke Check

Before installing services, you can verify the worker starts cleanly:

```bash
sudo -u smsadmin bash -lc 'set -euo pipefail
cd /opt/sms-saas
set -a
source .env
set +a
./venv/bin/rq worker --url "$REDIS_URL" "$RQ_QUEUE_NAME"'
```

If it boots without import or Redis errors, stop it with `Ctrl+C`.

## 11. Install The SaaS Systemd Services

Run the branch’s install script:

```bash
cd /opt/sms-saas
sudo APP_ROOT=/opt/sms-saas APP_USER=smsadmin APP_GROUP=smsadmin ./deploy/install_saas.sh
```

This script will:

- install `saas-dbdoctor` to `/usr/local/bin/saas-dbdoctor`
- ensure the `.env` has the required SaaS keys
- install Python dependencies
- apply SaaS migrations
- install the `sms-saas*` systemd units
- start the web service, worker, scheduler timer, and billing reconcile timer

## 12. Verify The Running Services

Check service state:

```bash
sudo systemctl status sms-saas sms-saas-worker sms-saas-scheduler.timer sms-saas-billing-reconcile.timer --no-pager
```

Check the health endpoint again:

```bash
curl -fsS http://127.0.0.1:8100/health
```

Tail logs if anything is off:

```bash
sudo journalctl -u sms-saas -n 100 --no-pager
sudo journalctl -u sms-saas-worker -n 100 --no-pager
sudo journalctl -u sms-saas-scheduler.service -n 100 --no-pager
sudo journalctl -u sms-saas-billing-reconcile.service -n 100 --no-pager
```

## 13. Update The Branch Later

When you want to pull the latest changes on the same branch:

```bash
cd /opt/sms-saas
sudo ./deploy/deploy_sms_saas.sh
```

That script does:

- `git pull --ff-only`
- `pip install -r requirements.txt`
- `saas-dbdoctor --apply`
- `saas-dbdoctor --ensure-platform-admin`
- `saas-dbdoctor --doctor`
- app config validation
- service restart
- post-restart health check

## 14. First SaaS Acceptance Checks

After the service is running locally on the VPS, confirm:

1. `curl http://127.0.0.1:8100/health` returns success.
2. Platform admin login works.
3. `/platform/organizations` loads.
4. You can create an organization.
5. Owner invite flow renders.
6. `/billing` loads for owner and returns `403` for staff.
7. The worker is idle but healthy.
8. The scheduler timer is active.

## 15. OLS Panel Note

Do not remove OLS Panel first. Get the SaaS app healthy on `127.0.0.1:8100` first, verify the local login and onboarding flow, and only then replace the public-facing proxy layer.

If OLS Panel is currently the thing occupying ports `80` and `443`, that does not block this checklist because the SaaS app binds to `127.0.0.1:8100`.
