# SaaS Operations

Operational runbook for the separate SaaS deployment line.

## Install On A Separate VPS

Use a separate checkout such as `/opt/sms-saas` and keep it isolated from the legacy `sms` services.

```bash
sudo -u smsadmin git clone <repo> /opt/sms-saas
cd /opt/sms-saas
sudo ./deploy/install_saas.sh
```

This installs:

- `saas-dbdoctor`
- `restart-sms-saas-services`
- `sms-saas.service`
- `sms-saas-worker.service`
- `sms-saas-scheduler.timer`
- `sms-saas-billing-reconcile.timer`

## Required Env

Minimum required in `/opt/sms-saas/.env`:

```bash
SAAS_MODE=1
DATABASE_URL=postgresql+psycopg://...
REDIS_URL=redis://localhost:6379/0
RQ_QUEUE_NAME=sms-saas
SAAS_BASE_URL=https://beta.example.com
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
STRIPE_PRICE_ID=...
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_API_KEY_SID=...
TWILIO_API_KEY_SECRET=...
TWILIO_CREDENTIAL_ENCRYPTION_KEY=...
TWILIO_A2P_ONBOARDING_ENABLED=0
TWILIO_A2P_EVENT_STREAMS_ENABLED=0
TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN=
TWILIO_PRIMARY_CUSTOMER_PROFILE_SID=
SECRET_KEY=...
PLATFORM_SERVICE_RESTART_ENABLED=0
```

Bootstrap-only for the first platform admin:

```bash
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password
```

After the first platform admin exists, `ADMIN_PASSWORD` is no longer required for deploys or runtime startup.
Additional platform admins can be created from `/users` while signed into the platform control plane.

Platform-admin accounts are control-plane only. Use a separate email for each organization owner or staff user.

If you change Twilio or other runtime values in `/opt/sms-saas/.env`, restart the SaaS services before testing provisioning or outbound messaging. The `/platform` restart control stays hidden until `PLATFORM_SERVICE_RESTART_ENABLED=1`.

If you enable `TWILIO_A2P_EVENT_STREAMS_ENABLED=1`, point the Twilio Event Streams webhook sink at `/webhooks/twilio/a2p-events` and set `TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN` so the webhook can be authenticated with a bearer token.

## SaaS DB Commands

```bash
# Print schema status
python -m app.saas_db --print

# Apply pending SaaS migrations
python -m app.saas_db --apply

# Validate schema readiness
python -m app.saas_db --doctor

# Ensure the first platform admin exists
python -m app.saas_db --ensure-platform-admin

# Import a legacy SQLite snapshot into one default organization
python -m app.saas_db --import-legacy /path/to/legacy.db \
  --organization-name "Legacy Production" \
  --organization-slug legacy-production
```

`dbdoctor` remains for the legacy SQLite path. Use `saas-dbdoctor` or `python -m app.saas_db` for the SaaS line.

## Deploy Updates

```bash
sudo ./deploy/deploy_sms_saas.sh
```

That flow:

- pulls latest code
- installs Python dependencies
- applies SaaS schema migrations
- ensures the first platform admin exists when needed
- validates app startup config
- restarts SaaS web, worker, scheduler, and billing reconciliation timers
- restarts the A2P reconcile timer when present

## Backup And Restore

### PostgreSQL

```bash
# Backup
pg_dump "$DATABASE_URL" > /var/backups/sms-saas-$(date +%Y%m%d-%H%M%S).sql

# Restore to a fresh database
psql "$TARGET_DATABASE_URL" < /var/backups/sms-saas-YYYYMMDD-HHMMSS.sql
```

### Redis

Use Redis persistence plus periodic copies of the persistence files:

```bash
sudo systemctl stop redis
sudo cp /var/lib/redis/dump.rdb /var/backups/redis-dump-$(date +%Y%m%d-%H%M%S).rdb
sudo systemctl start redis
```

## Cutover From Legacy Production

Recommended cutover model:

1. Freeze the legacy app for writes during a short maintenance window.
2. Snapshot the legacy SQLite database.
3. Run `python -m app.saas_db --import-legacy ...` into the fresh SaaS database.
4. Verify imported counts, owner accounts, message logs, scheduled messages, and inbox data.
5. Switch traffic to the SaaS deployment.
6. Keep the legacy snapshot for rollback and audit.
