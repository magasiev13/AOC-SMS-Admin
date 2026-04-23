# Twinevia SaaS Operations

Day-2 operational runbook for the primary SaaS deployment line.

## Deployment Topology

Canonical SaaS deployment:

- app root: `/opt/twinevia-saas`
- app user: `twinevia`
- env file: `/opt/twinevia-saas/.env`
- web bind: `127.0.0.1:8100`
- queue name: `twinevia-saas`
- service family: `twinevia-saas*`

If an older host still runs `/opt/sms-saas` as `smsadmin`, treat that as a transitional layout. Canonicalize it once with `./run/production_cutover.sh --canonicalize-host`; do not keep documenting it as the default production shape.

Keep this deployment isolated from the legacy line:

- do not share `/opt/sms-admin`
- do not share the legacy SQLite DB
- do not point the SaaS worker at queue `sms`

## Required Runtime State

Minimum expected env:

- `SAAS_MODE=1`
- `DATABASE_URL=postgresql+psycopg://...`
- `REDIS_URL=redis://localhost:6379/0`
- `RQ_QUEUE_NAME=twinevia-saas`
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
cd /opt/twinevia-saas
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --print'
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --apply'
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --doctor'
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --ensure-platform-admin'
```

### Service checks

```bash
sudo systemctl status twinevia-saas twinevia-saas-worker twinevia-saas-scheduler.timer --no-pager
sudo systemctl status twinevia-saas-billing-reconcile.timer twinevia-saas-platform-restart-queue.timer twinevia-saas-a2p-reconcile.timer --no-pager
```

### Health check

```bash
curl -fsS -H "Host: app.example.com" http://127.0.0.1:8100/health
```

After the first platform admin exists, `ADMIN_PASSWORD` is no longer required for deploys or runtime startup.
The canonical installed schema wrapper is `twinevia-saas-dbdoctor`; `saas-dbdoctor` remains available as a compatibility alias on upgraded hosts.
Additional platform admins can be created from `/users` while signed into the platform control plane.

Platform-admin accounts are control-plane only. Use a separate email for each organization owner or staff user.

If you change Twilio or other runtime values in `/opt/twinevia-saas/.env`, restart the SaaS services before testing provisioning or outbound messaging. The `/platform` restart control stays hidden until `PLATFORM_SERVICE_RESTART_ENABLED=1`.

If you enable `TWILIO_A2P_EVENT_STREAMS_ENABLED=1`, the app provisions org-specific Twilio Event Streams webhook destinations at `/webhooks/twilio/a2p-events?organization_id=<id>`. Twilio signature validation over the raw JSON body is the primary trust check. `TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN` is only an optional secondary bearer fallback.

Organizations without their own public website can use the tenant-hosted compliance pages generated under `/compliance/<organization-slug>/sms/privacy`, `/terms`, and `/opt-in`. The SaaS onboarding flow now creates this hosted package for every org and automatically falls back to it whenever tenant-supplied public website/privacy/terms/CTA URLs are incomplete or fail validation.

For eligible EIN-backed businesses, the default A2P registration path is now `low_volume_standard`. Treat `standard` as the upgrade path when low-volume limits no longer fit the org's throughput or campaign posture.

For platform-managed A2P, background sync is now non-destructive. It can detect provider drift, a missing campaign, or transient Twilio connectivity failures, but it does not auto-create a new campaign in the background. Platform admins must explicitly use `Reconcile Twilio State` when the app is bound to stale provider identifiers, and must explicitly use `Create Campaign` when the approved Twilio packet exists but the Messaging Service has no attached campaign.

For platform-managed sender activation, the org service address is now the source of truth for sender provisioning. After A2P approval, use the organization messaging page to save or update the service address, choose the number strategy, and run `Finalize Sender Setup`. That workflow validates or updates the Twilio address, buys or reuses the sender number inside the org subaccount, binds inbound webhooks, syncs emergency-address registration, and only then flips the provider active.

For platform-managed A2P, recurring status refresh remains non-destructive and can keep polling already-submitted records even when `TWILIO_A2P_ONBOARDING_ENABLED` is off. That flag only gates automated submission/provisioning work. If you expect Twilio Event Streams callbacks to update org state in real time, production must also set `TWILIO_A2P_EVENT_STREAMS_ENABLED=1`.

## A2P Review To Go-Live

While Twilio review is in progress, do not mutate the packet again unless the product explicitly shows a rejected or needs-action state.

Use the built-in product surfaces during review:

- owner `/setup` launch step
- platform organization messaging page
- platform A2P onboarding page

Those pages now show:

- a launch-readiness checklist
- recent Twilio/provider lifecycle activity from `organization_provider_audit_logs`
- live vs stored Twilio resource comparisons when provider drift is detected
- explicit post-approval sender guidance
- explicit `Reconcile Twilio State` and `Create Campaign` actions when the app must not mutate Twilio automatically
- retry guidance when a failed campaign should use an in-place Twilio edit/retry path instead of delete-and-recreate

### Drift recovery

If Twilio still shows an approved profile, trust product, and brand but the app says the org needs action:

1. Open `/platform/organizations/<id>/messaging/onboarding`.
2. Review the stored vs live Twilio SIDs shown in the recovery panel.
3. Use `Reconcile Twilio State` to bind the org to the current live Messaging Service, Customer Profile, Trust Product, and Brand Registration in the same Twilio subaccount.
4. If the selected Messaging Service has no attached campaign, use `Create Campaign` only after confirming the fee warning.
5. Refresh the page again and confirm the app now shows the live Twilio identifiers and the expected review stage.

Identifier notes:

- `BU...` values are Trust Hub resources such as Customer Profile or Trust Product
- `BN...` is the Brand Registration SID
- `QE...` is the Messaging Service campaign association SID stored by the app
- `CM...` is the console campaign ID Twilio may expose separately

### Production repair for `it-wingman-llc`

The current production incident should follow that same flow:

1. Reconcile org `it-wingman-llc` to the live approved Twilio resources that already exist in its current subaccount.
2. Do not rebuild the approved profile, trust product, or brand.
3. Leave campaign creation as a separate explicit operator step after the rebind.
4. Keep the pre-repair production snapshot until post-repair verification confirms the org state is stable.

### First Controlled Send

After Twilio approves the campaign, keep customer traffic paused until this manual runbook is complete:

1. Confirm the approval has synced into the app and the launch-readiness checklist shows the campaign as approved.
2. Save or verify the org service address on the messaging page.
3. Run `Finalize Sender Setup` using the configured number strategy.
4. Verify the provider status turns `active` and emergency-address sync is complete.
5. Send one controlled internal test message.
6. Confirm inbound `STOP` and `HELP` handling still works as expected.
7. Only then allow live customer traffic.

If the campaign is approved but no sender is attached yet, the platform messaging page will show the exact next operator action based on the stored number strategy.

## Deploy Updates

Canonical update flow:

```bash
cd /opt/twinevia-saas
sudo ./deploy/deploy_twinevia_saas.sh
```

What it refreshes:

- git checkout contents
- Python dependencies
- SaaS schema migrations
- platform-admin bootstrap state
- restart helper and sudoers
- systemd unit files
- active SaaS services and timers

### Production cutover wrapper

For the live public host, use the repo wrapper from your operator machine instead of improvising the sequence by hand:

```bash
./run/production_cutover.sh \
  --org-slug it-wingman-llc \
  --canonicalize-host \
  --freeze-note "Pause org edits, invites, billing mutations, sender changes, and outbound sends." \
  --deploy
```

This wrapper preserves the current production `DATABASE_URL`, `REDIS_URL`, and live `.env`, captures pre/post snapshots, writes a backup bundle, and only then performs the in-place deploy. When `--canonicalize-host` is supplied on a legacy `/opt/sms-saas` host, it first migrates the runtime to `/opt/twinevia-saas` and `twinevia`.

### Live smoke wrapper

Use the authenticated live smoke wrapper from your operator machine before and after the cutover:

```bash
TWINEVIA_OWNER_USERNAME=owner@example.com \
TWINEVIA_OWNER_PASSWORD=... \
TWINEVIA_PLATFORM_USERNAME=platform@example.com \
TWINEVIA_PLATFORM_PASSWORD=... \
./run/public_readiness_live_smoke.sh
```

The live smoke is intentionally read-only. It covers public auth surfaces, owner readiness/billing views, and platform inspection pages without starting checkout, sending invites, or mutating A2P/provider state.

For new organizations, the app-saved sender service address is the source of truth. Imported Twilio address state may be observed for diagnostics, but it must not silently override app-entered service-address fields.

## Timers And Background Jobs

### Scheduler

- timer: `twinevia-saas-scheduler.timer`
- service: `twinevia-saas-scheduler.service`
- role: due scheduled sends and retry processing

### Billing reconciliation

- timer: `twinevia-saas-billing-reconcile.timer`
- service: `twinevia-saas-billing-reconcile.service`
- role: subscription/usage reconciliation and overage posting

### Platform restart queue

- timer: `twinevia-saas-platform-restart-queue.timer`
- service: `twinevia-saas-platform-restart-queue.service`
- role: queued restart dispatch and status refresh

### A2P reconciliation

- timer: `twinevia-saas-a2p-reconcile.timer`
- service: `twinevia-saas-a2p-reconcile.service`
- role: Twilio A2P state refresh and recovery

## Restart Helper Operations

When platform restart control is enabled:

- web requests only queue `PlatformServiceRestartRequest` rows
- host restarts happen out-of-band through `restart-twinevia-saas-services`
- the helper must be runnable by `twinevia` via `sudo -n`

Validation command:

```bash
sudo -u twinevia sudo -n /usr/local/bin/restart-twinevia-saas-services --check
```

## Backup And Restore

### PostgreSQL

Backup:

```bash
sudo -u twinevia bash -lc '
  cd /opt/twinevia-saas &&
  set -a &&
  source .env &&
  set +a &&
  pg_dump "$DATABASE_URL" > /var/backups/twinevia-$(date +%Y%m%d-%H%M%S).sql
'
```

Restore into a fresh target:

```bash
TARGET_DATABASE_URL='postgresql://user:password@127.0.0.1:5432/twinevia_restore'
psql "$TARGET_DATABASE_URL" < /var/backups/twinevia-YYYYMMDD-HHMMSS.sql
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

- `/opt/twinevia-saas/.env`
- reverse-proxy config
- any deploy-specific secrets or CI metadata outside the repo

For production cutovers, the automation stores off-host copies of the env snapshot and reverse-proxy evidence under `output/signoff/<run-id>/production-cutover/` while leaving the full PostgreSQL and Redis backups on the live host until signoff completes.

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
