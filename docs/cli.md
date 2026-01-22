# CLI Tools

## dbdoctor

Database health check and migration tool.

### Location

```bash
# Development (from repo root)
python -m app.dbdoctor

# Production (after install.sh)
dbdoctor
```

### Commands

#### Print Migration Status

```bash
python -m app.dbdoctor --print
```

Output:
```
Database file: /opt/sms-admin/instance/sms.db
Migrations:
  - 001: applied
  - 002: applied
  - 003: pending
```

#### Apply Pending Migrations

```bash
python -m app.dbdoctor --apply
```

Actions:
1. Creates all SQLAlchemy tables if missing
2. Applies pending migrations in order
3. Records applied migrations in `schema_migrations` table

Output:
```
2024-01-15 10:30:00 INFO app.dbdoctor Database file in use: /opt/sms-admin/instance/sms.db
2024-01-15 10:30:00 INFO app.migrations.runner Applying migration 003 (003_add_suppressed_contacts).
2024-01-15 10:30:00 INFO app.migrations.runner Applied migrations: 003
```

#### Full Health Check

```bash
python -m app.dbdoctor --doctor
```

Checks:
- Database file exists and is readable/writable
- SQLite version
- Migration status
- `message_logs` table columns

Output (healthy):
```
Database file: /opt/sms-admin/instance/sms.db
File perms: -rw-r-----
SQLite version: 3.40.1
Schema migrations: 5/5 applied, pending: none
message_logs columns: id, created_at, message_body, target, event_id, status, total_recipients, success_count, failure_count, details
```

Output (issues):
```
Database file: /opt/sms-admin/instance/sms.db
File perms: -r--r-----
SQLite version: 3.40.1
Schema migrations: 3/5 applied, pending: 004, 005
message_logs columns: id, created_at, message_body, target, event_id
ERROR: Database file /opt/sms-admin/instance/sms.db is not writable. Fix file permissions or ownership.
ERROR: message_logs is missing columns: status, total_recipients, success_count, failure_count, details. Run `python -m app.dbdoctor --apply` to apply migrations.
ERROR: Pending migrations detected: 004, 005. Run `python -m app.dbdoctor --apply` to apply them.
```

Exit code: `0` if healthy, `1` if issues detected.

### Production Usage

The `install.sh` script installs `dbdoctor` to `/usr/local/bin/`:

```bash
# As smsadmin user
sudo -u smsadmin dbdoctor --doctor

# Check and apply
sudo -u smsadmin dbdoctor --apply
```

### systemd Integration

The `sms.service` and `sms-scheduler.service` run migrations automatically on startup via `ExecStartPre`:

```ini
[Service]
ExecStartPre=/usr/local/bin/dbdoctor --apply
ExecStart=...
```

---

## Flask CLI

Standard Flask commands are available:

### Run Development Server

```bash
flask --app wsgi:app run --debug
```

### Shell

```bash
flask --app wsgi:app shell
```

Access app context:
```python
>>> from app.models import CommunityMember
>>> CommunityMember.query.count()
42
```

### Routes

```bash
flask --app wsgi:app routes
```

Lists all registered routes.

---

## RQ Worker

Start background job worker:

```bash
# Development
rq worker sms --url redis://localhost:6379/0

# Production (via systemd)
sudo systemctl start sms-worker
```

Monitor jobs:
```bash
rq info --url redis://localhost:6379/0
```

---

## Scheduler (Development)

For development, enable APScheduler in `.env`:

```bash
SCHEDULER_ENABLED=1
```

Then start the app normally - scheduler runs in background thread.

---

## Scheduler (Production)

Use systemd timer instead of background thread:

```bash
# Check timer status
systemctl list-timers | grep sms-scheduler

# View scheduler logs
journalctl -u sms-scheduler.service -f

# Manually trigger
sudo systemctl start sms-scheduler.service
```

---

## Backfill Suppressions

Process historical message logs to extract suppression data:

### Via RQ Job

```bash
flask --app wsgi:app shell
>>> from app.queue import get_queue
>>> queue = get_queue()
>>> job = queue.enqueue('app.tasks.backfill_suppressions_job')
>>> print(job.id)
```

### Via UI

POST to `/unsubscribed/backfill` (admin only)

---

## Database Backup

```bash
# Simple file copy (while app is stopped)
sudo systemctl stop sms
sudo cp /opt/sms-admin/instance/sms.db /backup/sms-$(date +%Y%m%d).db
sudo systemctl start sms

# With SQLite backup command (while running)
sudo -u smsadmin sqlite3 /opt/sms-admin/instance/sms.db ".backup /backup/sms-$(date +%Y%m%d).db"
```

---

## Database Inspection

```bash
# Open SQLite CLI
sqlite3 /opt/sms-admin/instance/sms.db

# Common queries
.tables
.schema message_logs
SELECT COUNT(*) FROM community_members;
SELECT * FROM schema_migrations;

# Check integrity
PRAGMA integrity_check;
```
