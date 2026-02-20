# deploy/ — Deployment Infrastructure

Debian VPS deployment via systemd + Nginx + Gunicorn. CI via GitHub Actions.

## STRUCTURE

```
deploy/
├── install.sh              # Automated deploy: deps, env, migrations, systemd setup
├── deploy_sms_admin.sh     # Pull/update/migrate/restart deploy helper (+ security env append)
├── sms.service             # systemd: Gunicorn web app (ExecStartPre runs dbdoctor)
├── sms-worker.service      # systemd: RQ background worker
├── sms-scheduler.service   # systemd: Oneshot scheduler (triggered by timer)
├── sms-scheduler.timer     # systemd: Fires scheduler every 30s
├── run_scheduler_once.sh   # Wrapper for scheduler oneshot execution
├── run_worker.sh           # Wrapper for RQ worker startup
└── nginx.conf              # Reverse proxy + SSL + HTTP basic auth
```

## WHERE TO LOOK

| Task | File | Notes |
|------|------|-------|
| Add systemd service | Copy pattern from `sms.service` | `ExecStartPre=dbdoctor --apply` |
| Modify scheduler interval | `sms-scheduler.timer` | `OnUnitActiveSec=30s` |
| Change Gunicorn workers | `sms.service` | `--workers N` in ExecStart |
| Nginx config changes | `nginx.conf` | SSL certs via Certbot |
| Automated deploy | `install.sh` | Idempotent; safe to re-run |

## CI PIPELINE (.github/workflows/deploy.yml)

- Triggers on push to `main` or manual dispatch
- SSH to VPS → install `deploy/deploy_sms_admin.sh` to `/usr/local/bin/deploy_sms_admin.sh` → run deploy script → verify services → health check
- Post-deploy assertions: services active, timer configured, scheduler runs, health endpoint 200

## CONVENTIONS

- **App path**: `/opt/sms-admin/` on server
- **App user**: `smsadmin`
- **Env file**: `/opt/sms-admin/.env` (root:smsadmin, mode 660)
- **Logs**: `/var/log/sms-admin/` + journald
- **dbdoctor CLI**: Installed to `/usr/local/bin/dbdoctor`
- **Migrations auto-run**: `ExecStartPre` in service units

## ANTI-PATTERNS

- **DO NOT** enable `sms-scheduler.service` directly. Enable `sms-scheduler.timer` only.
- **DO NOT** edit systemd units in `/etc/systemd/system/` directly. Copy from `deploy/`.
- **DO NOT** run Flask dev server in production. Always Gunicorn.
- **DO NOT** skip `dbdoctor --apply` before serving traffic.
