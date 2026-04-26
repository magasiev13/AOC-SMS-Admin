# deploy/ — Deployment Infrastructure

This directory contains the primary SaaS deployment family (`twinevia-saas*`, `/opt/twinevia-saas`).

## PRIMARY SaaS FILES

| File | Purpose |
|---|---|
| `install_saas.sh` | Primary install/bootstrap flow for SaaS |
| `deploy_twinevia_saas.sh` | Primary update/restart flow for SaaS |
| `twinevia-saas.service` | Gunicorn web app on `127.0.0.1:8100` |
| `twinevia-saas-worker.service` | RQ worker |
| `twinevia-saas-scheduler.*` | Scheduled send timer/service |
| `twinevia-saas-billing-reconcile.*` | Billing reconciliation timer/service |
| `twinevia-saas-platform-restart-queue.*` | Restart queue timer/service |
| `twinevia-saas-a2p-reconcile.*` | A2P reconciliation timer/service |
| `restart_twinevia_saas_services.sh` | Host restart helper |
| `twinevia-saas-restart.sudoers` | `sudo -n` rule for the restart helper |

## CONVENTIONS

- SaaS deploys should use `twinevia-saas-dbdoctor`, not `dbdoctor`.
- SaaS timers are the production scheduler/reconciliation mechanism; do not rely on in-process scheduling there.
- Health checks must use an allowed `Host` header when `TRUSTED_HOSTS` is enforced.
- Do not reintroduce the retired legacy host/deploy path.

## ANTI-PATTERNS

- **DO NOT** copy legacy unit assumptions into the SaaS deploy flow.
- **DO NOT** enable the oneshot services directly when the timer is the intended long-lived control point.
- **DO NOT** forget the restart-helper sudoers validation when using platform restart controls.
