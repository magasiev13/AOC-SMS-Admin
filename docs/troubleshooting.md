# Twinevia Troubleshooting

This guide is SaaS-first. Legacy SQLite-only problems are grouped near the end.

## SaaS Startup And Config Failures

### App fails during startup validation

Run the same config check the deploy scripts use:

```bash
cd /opt/twinevia-saas
sudo -u twinevia bash -lc 'set -a; source .env; set +a; ./venv/bin/python - <<'"'"'PY'"'"'
from app import create_app
create_app(run_startup_tasks=False, start_scheduler=False)
print("ok")
PY'
```

Common causes:

- `SECRET_KEY` still uses the development default
- `TRUSTED_HOSTS` is empty while `FLASK_ENV=production`
- cookie security values do not satisfy production validation
- SaaS billing prerequisites are missing
- only one of `TWILIO_API_KEY_SID` or `TWILIO_API_KEY_SECRET` is set
- A2P onboarding is enabled without `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID`

### Health check returns `400`

Cause:

- `TRUSTED_HOSTS` is enabled and the local health probe is missing an allowed `Host` header

Fix:

```bash
curl -fsS -H "Host: app.example.com" http://127.0.0.1:8100/health
```

### SaaS doctor reports schema issues

Run:

```bash
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --apply'
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --doctor'
```

If you accidentally used `dbdoctor` against the SaaS database, switch back to `twinevia-saas-dbdoctor` or `./venv/bin/python -m app.saas_db`.

## Services And Timers

### Web app is down after deploy

Check:

```bash
sudo systemctl status twinevia-saas --no-pager
sudo journalctl -u twinevia-saas -n 200 --no-pager
```

Common causes:

- failed config validation
- bad database credentials
- missing Python dependency after dependency sync
- reverse proxy or trusted-host mismatch hiding a healthy local app

### Worker is not processing jobs

Check:

```bash
sudo systemctl status twinevia-saas-worker --no-pager
sudo journalctl -u twinevia-saas-worker -n 200 --no-pager
```

Common causes:

- Redis unavailable
- wrong `RQ_QUEUE_NAME`
- schema drift causing worker import/startup failure
- env changes applied to the web service but not restarted for the worker

### Scheduled sends are not moving

Check the timer and oneshot logs:

```bash
sudo systemctl status twinevia-saas-scheduler.timer --no-pager
sudo journalctl -u twinevia-saas-scheduler.service -n 200 --no-pager
```

Also verify:

- `SCHEDULER_ENABLED=0` in production
- the timer is enabled
- due rows exist in `scheduled_messages`
- billing and provider readiness actually allow sending

### Billing reconciliation is not updating usage

Check:

```bash
sudo systemctl status twinevia-saas-billing-reconcile.timer --no-pager
sudo journalctl -u twinevia-saas-billing-reconcile.service -n 200 --no-pager
```

Common causes:

- Stripe configuration incomplete
- message usage rows never reconciled because outbound Twilio state is incomplete
- subscription state is not active enough to post usage

### Platform restart control is stuck

Check:

```bash
sudo systemctl status twinevia-saas-platform-restart-queue.timer --no-pager
sudo journalctl -u twinevia-saas-platform-restart-queue.service -n 200 --no-pager
sudo -u twinevia sudo -n /usr/local/bin/restart-twinevia-saas-services --check
```

Common causes:

- `PLATFORM_SERVICE_RESTART_ENABLED=0`
- helper script path is wrong
- sudoers rule was not installed or drifted
- queue processor timer is not active

### A2P status is stale

Check:

```bash
sudo systemctl status twinevia-saas-a2p-reconcile.timer --no-pager
sudo journalctl -u twinevia-saas-a2p-reconcile.service -n 200 --no-pager
```

Also verify:

- `TWILIO_A2P_ONBOARDING_ENABLED=1` if you expect automated flows
- `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID` is a primary `BU...` profile
- optional Event Streams auth token matches the webhook sender when using `/webhooks/twilio/a2p-events`

If the Twilio dashboard still shows approved resources but the app says the org needs action:

- open the platform A2P onboarding page and review the stored vs live Twilio identifiers
- confirm the page is reading the org's own Twilio subaccount state, not the parent account
- use `Reconcile Twilio State` to bind the org back to the current live subaccount resources
- if the only missing piece is the campaign, use `Create Campaign` explicitly after reviewing the fee warning
- do not reset the approved Twilio profile/trust product/brand just because the app stored stale SIDs

## Stripe And Billing

### Stripe webhook failures

Symptoms:

- checkout completes in Stripe but workspace state does not update
- `billing_overview` remains stale

Check:

```bash
sudo journalctl -u twinevia-saas -n 200 --no-pager | rg Stripe
```

Common causes:

- wrong `STRIPE_WEBHOOK_SECRET`
- webhook sent to the wrong path
- `STRIPE_SECRET_KEY` missing
- event object does not map to the expected organization metadata

### Fake checkout route returns 404

Cause:

- `STRIPE_FAKE_CHECKOUT_ENABLED` is disabled, or the session ID is not a fake test session

This is expected in production.

## Twilio And Messaging

### Inbound webhook fails signature validation

Check:

- `TWILIO_VALIDATE_INBOUND_SIGNATURE`
- public base URL and reverse proxy host/scheme forwarding
- Twilio webhook URL configured on the correct sender identity

### Org cannot send even though billing is active

Sending requires both:

- subscription readiness from billing
- active provider readiness from `OrganizationMessagingProfile`

Check the org’s:

- subscription status
- provider status
- sender review status
- sender identity (`from_number` or `messaging_service_sid`)

### Customer-managed workspace fails validation

Common causes:

- bad external Twilio account SID/auth token
- sender number or Messaging Service SID already assigned elsewhere
- phone number SID provided without a matching sender number

### STOP/START behavior looks inconsistent

Remember:

- inbound STOP-like messages update `unsubscribed_contacts`
- manual inbox replies are blocked while the contact is unsubscribed
- the contact must send a START-style message to be re-enabled

## Auth And Account Access

### User keeps getting redirected to `/account/security-contact`

Cause:

- authenticated user has no phone on record

### User cannot reach the dashboard after login

In SaaS mode, home routing depends on:

- platform admin vs workspace user
- owner vs staff
- whether billing/provider setup is complete
- whether the org is suspended

Expected destinations:

- platform admin -> `/platform`
- owner with incomplete setup -> `/setup`
- staff with incomplete setup -> `/setup/pending`
- ready workspace user -> `/dashboard`

### Too many failed login attempts

The lockout counters are DB-backed. Review:

- `AUTH_ATTEMPT_WINDOW_SECONDS`
- `AUTH_LOCKOUT_SECONDS`
- `AUTH_MAX_ATTEMPTS_IP_ACCOUNT`
- `AUTH_MAX_ATTEMPTS_ACCOUNT`
- `AUTH_MAX_ATTEMPTS_IP`

### Need to bootstrap the first platform admin

Use:

```bash
cd /opt/twinevia-saas
sudo -u twinevia bash -lc 'cd /opt/twinevia-saas && set -a && source .env && set +a && twinevia-saas-dbdoctor --ensure-platform-admin'
```

## Local Tooling Issues

### `./run/local_saas_stack.sh` fails immediately

Check:

- `stripe` CLI is installed
- Redis is reachable
- `.env` contains SaaS-ready values
- the virtualenv exists and uses Python 3.11

### `./run/test_browser.sh` fails before running specs

Check:

- `npm` is installed
- `node_modules` exists or can be installed
- Playwright Chromium can be installed in the local cache

## Legacy Compatibility Appendix

### `attempt to write a readonly database`

This is a legacy SQLite problem. Fix permissions on:

- `/opt/sms-admin/instance`
- `/opt/sms-admin/instance/sms.db`
- any `sms.db-wal` and `sms.db-shm` files

### `database is locked`

Also a legacy SQLite problem. Check for long-running transactions, stale processes, or too-small `SQLITE_TIMEOUT`.

### Legacy health check succeeds locally but fails behind nginx

Make sure:

- `TRUSTED_HOSTS` matches the public domain
- nginx forwards the `Host` header
- the upstream points to `127.0.0.1:8000`, not the SaaS port
