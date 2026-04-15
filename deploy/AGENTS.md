# deploy/ — Deployment Infrastructure

This directory contains both deployment families:

- primary SaaS deployment (`sms-saas*`, `/opt/sms-saas`)
- secondary legacy deployment (`sms*`, `/opt/sms-admin`)

## PRIMARY SaaS FILES

| File | Purpose |
|---|---|
| `install_saas.sh` | Primary install/bootstrap flow for SaaS |
| `deploy_sms_saas.sh` | Primary update/restart flow for SaaS |
| `sms-saas.service` | Gunicorn web app on `127.0.0.1:8100` |
| `sms-saas-worker.service` | RQ worker |
| `sms-saas-scheduler.*` | Scheduled send timer/service |
| `sms-saas-billing-reconcile.*` | Billing reconciliation timer/service |
| `sms-saas-platform-restart-queue.*` | Restart queue timer/service |
| `sms-saas-a2p-reconcile.*` | A2P reconciliation timer/service |
| `restart_sms_saas_services.sh` | Host restart helper |
| `sms-saas-restart.sudoers` | `sudo -n` rule for the restart helper |

## SECONDARY LEGACY FILES

| File | Purpose |
|---|---|
| `install.sh` | Legacy SQLite install flow |
| `deploy_sms_admin.sh` | Legacy update flow |
| `sms.service` | Legacy gunicorn web app on `127.0.0.1:8000` |
| `sms-worker.service` | Legacy RQ worker |
| `sms-scheduler.*` | Legacy scheduled send timer/service |
| `nginx.conf` | Legacy nginx sample |

## CONVENTIONS

- SaaS deploys should use `saas-dbdoctor`, not `dbdoctor`.
- SaaS timers are the production scheduler/reconciliation mechanism; do not rely on in-process scheduling there.
- Health checks must use an allowed `Host` header when `TRUSTED_HOSTS` is enforced.
- Keep `/opt/sms-admin` and `/opt/sms-saas` assets separate.

## ANTI-PATTERNS

- **DO NOT** copy legacy unit assumptions into the SaaS deploy flow.
- **DO NOT** enable the oneshot services directly when the timer is the intended long-lived control point.
- **DO NOT** forget the restart-helper sudoers validation when using platform restart controls.
