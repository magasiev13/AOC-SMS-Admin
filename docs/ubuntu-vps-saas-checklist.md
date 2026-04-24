# Ubuntu VPS SaaS Checklist

Step-by-step checklist for bringing up the SaaS deployment on a fresh Ubuntu VPS before placing a public proxy in front of it.

This assumes:

- Ubuntu with `python3.11`
- a separate checkout at `/opt/twinevia-saas`
- PostgreSQL and Redis on the same VPS
- local health validation first at `127.0.0.1:8100`

Do not point this deployment at the legacy SQLite database or the legacy `sms` services.

## 1. Install Base Packages

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

```bash
sudo systemctl enable --now postgresql redis-server
python3.11 --version
```

## 2. Create The App User And Directories

```bash
sudo adduser --system --group --home /opt/twinevia-saas --shell /bin/bash twinevia
sudo install -d -o twinevia -g twinevia /opt/twinevia-saas
sudo install -d -o twinevia -g twinevia /var/log/twinevia-saas
```

## 3. Clone The Repo

```bash
sudo -u twinevia git clone <repo-url> /opt/twinevia-saas
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && git branch --show-current && git rev-parse --short HEAD'
```

## 4. Create PostgreSQL Role And Database

```bash
sudo -u postgres psql <<'SQL'
CREATE USER twinevia_saas WITH PASSWORD 'REPLACE_WITH_STRONG_DB_PASSWORD';
CREATE DATABASE twinevia_saas OWNER twinevia_saas;
ALTER ROLE twinevia_saas SET client_encoding TO 'UTF8';
ALTER ROLE twinevia_saas SET timezone TO 'UTC';
SQL
```

Connectivity check:

```bash
PGPASSWORD='REPLACE_WITH_STRONG_DB_PASSWORD' psql \
  -h 127.0.0.1 \
  -U twinevia_saas \
  -d twinevia_saas \
  -c 'select current_database(), current_user, now();'
```

## 5. Create `/opt/twinevia-saas/.env`

```bash
sudo tee /opt/twinevia-saas/.env >/dev/null <<'EOF'
FLASK_ENV=production
FLASK_DEBUG=0
TRUST_PROXY=1
TRUSTED_HOSTS=app.example.com
SAAS_MODE=1
SCHEDULER_ENABLED=0
RQ_QUEUE_NAME=twinevia-saas
REDIS_URL=redis://localhost:6379/0
DATABASE_URL=postgresql+psycopg://twinevia_saas:REPLACE_WITH_STRONG_DB_PASSWORD@127.0.0.1:5432/twinevia_saas
SAAS_BASE_URL=https://app.example.com
SECRET_KEY=REPLACE_WITH_LONG_RANDOM_SECRET
ADMIN_USERNAME=admin
ADMIN_PASSWORD=REPLACE_WITH_STRONG_ADMIN_PASSWORD
ADMIN_EMAIL=admin@example.com
STRIPE_SECRET_KEY=sk_live_replace_me
STRIPE_WEBHOOK_SECRET=whsec_replace_me
STRIPE_PRICE_ID=price_replace_me
STRIPE_ACTIVATION_PRICE_ID=price_activation_replace_me
BILLING_TRIAL_DAYS=0
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=replace_me
TWILIO_API_KEY_SID=
TWILIO_API_KEY_SECRET=
TWILIO_CREDENTIAL_ENCRYPTION_KEY=REPLACE_WITH_VALID_FERNET_KEY
TWILIO_A2P_ONBOARDING_ENABLED=0
TWILIO_PRIMARY_CUSTOMER_PROFILE_SID=
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
REMEMBER_COOKIE_SECURE=1
STRIPE_FAKE_CHECKOUT_ENABLED=0
TWILIO_BROWSER_FAKE_SENDS=0
TWILIO_A2P_FAKE_QUEUE=0
EOF
sudo chown root:twinevia /opt/twinevia-saas/.env
sudo chmod 660 /opt/twinevia-saas/.env
```

Replace at least:

- domain names
- database password
- `SECRET_KEY`
- `ADMIN_PASSWORD`
- Stripe values
- Twilio values
- `TWILIO_CREDENTIAL_ENCRYPTION_KEY`

The SaaS installer and deployer now hard-fail if this file still looks like a
local/dev setup. Keep PostgreSQL, production mode, real hostnames, secure
cookies, and the fake Stripe/Twilio flags exactly as shown above unless you have
an intentional equivalent.

## 6. Install Python Dependencies

You can let the installer handle this, but a manual precheck is fine:

```bash
sudo -u twinevia bash -lc '
  cd /opt/twinevia-saas &&
  python3.11 -m venv venv &&
  ./venv/bin/pip install --upgrade pip &&
  ./venv/bin/pip install -r requirements.txt
'
```

## 7. Apply And Verify The SaaS Schema

```bash
sudo -u twinevia bash -lc '
  cd /opt/twinevia-saas &&
  set -a &&
  source .env &&
  set +a &&
  ./venv/bin/python -m app.saas_db --apply &&
  ./venv/bin/python -m app.saas_db --ensure-platform-admin &&
  ./venv/bin/python -m app.saas_db --doctor
'
```

## 8. Validate App Config Before Starting Services

```bash
sudo -u twinevia bash -lc 'set -euo pipefail
cd /opt/twinevia-saas
set -a
source .env
set +a
./venv/bin/python - <<'"'"'PY'"'"'
from app import create_app

create_app(run_startup_tasks=False, start_scheduler=False)
print("SaaS app config validation ok")
PY'
```

## 9. Smoke Test Gunicorn Locally

```bash
sudo -u twinevia bash -lc 'set -euo pipefail
cd /opt/twinevia-saas
set -a
source .env
set +a
./venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8100 wsgi:app'
```

In a second terminal:

```bash
curl -fsS -H "Host: app.example.com" http://127.0.0.1:8100/health
```

Expected result:

```text
OK
```

## 10. Optional Manual Worker Check

```bash
sudo -u twinevia bash -lc 'set -euo pipefail
cd /opt/twinevia-saas
set -a
source .env
set +a
./venv/bin/rq worker --url "$REDIS_URL" "$RQ_QUEUE_NAME"'
```

## 11. Install The SaaS Services

```bash
cd /opt/twinevia-saas
sudo ./deploy/install_saas.sh
```

## 12. Verify The Running Services

```bash
sudo systemctl status twinevia-saas twinevia-saas-worker twinevia-saas-scheduler.timer --no-pager
sudo systemctl status twinevia-saas-billing-reconcile.timer twinevia-saas-platform-restart-queue.timer twinevia-saas-a2p-reconcile.timer --no-pager
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --doctor'
curl -fsS -H "Host: app.example.com" http://127.0.0.1:8100/health
```

## 13. Configure Your Public Proxy

The repo does not ship a dedicated SaaS nginx config. Whatever proxy you use must:

- forward the original `Host` header
- forward `X-Forwarded-*` headers when `TRUST_PROXY=1`
- proxy to `127.0.0.1:8100`
- preserve `/health`

## 14. Update Later

```bash
cd /opt/twinevia-saas
sudo ./deploy/deploy_twinevia_saas.sh
```

## 15. First Acceptance Checks

- platform admin can log in
- owner invite acceptance lands on `/setup`
- Stripe checkout returns correctly
- workspace remains blocked until provider readiness is satisfied
- staff user gets `403` on billing and platform surfaces
