# SMS Admin

A production-ready, single-user SMS admin web application for sending community and event SMS blasts via Twilio.

## Features

- **Send SMS Blasts**: Community-wide or event-specific messaging with personalization
- **Scheduled Messages**: Schedule messages for future delivery with timezone support
- **Recipients Management**: Add, edit, delete, and import recipients via CSV
- **Events Management**: Create events and manage registrations
- **Message Logs**: Track all sent messages with per-recipient success/failure details
- **Automatic Suppression**: Auto-detect and suppress invalid numbers and opt-outs
- **Unsubscribe Management**: Track and respect opt-outs across all sends
- **Role-Based Access**: Admin and social manager roles with appropriate permissions
- **Secure**: Environment-based secrets, Flask-Login authentication, CSRF protection, HTTPS

## Documentation

For detailed documentation, see the [`docs/`](docs/) folder:

- [Architecture Overview](docs/architecture.md) - System design and components
- [Database Schema](docs/database.md) - Data models and migrations
- [API Reference](docs/api.md) - All HTTP routes and endpoints
- [Services](docs/services.md) - Twilio, Scheduler, Suppression services
- [Configuration](docs/configuration.md) - Environment variables guide
- [Deployment](docs/deployment.md) - Production deployment instructions
- [CLI Tools](docs/cli.md) - dbdoctor and other utilities
- [Troubleshooting](docs/troubleshooting.md) - Common issues and solutions

## Tech Stack

- **Backend**: Python 3.11 (supported/tested), Flask, SQLAlchemy
- **Database**: SQLite
- **Queue**: Redis + RQ worker (background sends)
- **Scheduler**: APScheduler (dev) + systemd timer (prod)
- **SMS Provider**: Twilio
- **Production**: Gunicorn + Nginx + systemd (sms, sms-worker, sms-scheduler.timer)

## Project Structure

```
├── app/
│   ├── __init__.py          # App factory with extensions, migrations, scheduler
│   ├── config.py            # Environment-based configuration
│   ├── models.py            # SQLAlchemy ORM models
│   ├── routes.py            # Flask HTTP routes (40+ endpoints)
│   ├── auth.py              # Authentication, login, rate limiting
│   ├── utils.py             # Phone normalization, CSV parsing, templating
│   ├── queue.py             # Redis/RQ connection utilities
│   ├── tasks.py             # Background job definitions
│   ├── dbdoctor.py          # Database health check CLI
│   ├── sort_utils.py        # Sort parameter validation
│   ├── services/
│   │   ├── twilio_service.py      # SMS sending via Twilio API
│   │   ├── scheduler_service.py   # Scheduled message processing
│   │   ├── recipient_service.py   # Recipient filtering utilities
│   │   ├── suppression_service.py # Failure classification & suppression
│   │   └── suppression_backfill.py # Historical suppression backfill
│   ├── migrations/          # SQLite schema migrations
│   ├── templates/           # Jinja2 HTML templates
│   └── static/              # CSS, JavaScript assets
├── bin/
│   └── dbdoctor             # CLI wrapper script
├── deploy/
│   ├── install.sh           # Automated deployment script
│   ├── nginx.conf           # Nginx reverse proxy config
│   ├── sms.service          # systemd unit: web app
│   ├── sms-worker.service   # systemd unit: RQ worker
│   ├── sms-scheduler.service # systemd unit: scheduler (oneshot)
│   ├── sms-scheduler.timer  # systemd timer: runs scheduler every 30s
│   └── run_scheduler_once.sh # Scheduler wrapper script
├── docs/                    # Documentation (see below)
├── tests/                   # pytest test suite
├── wsgi.py                  # WSGI entry point
├── requirements.txt
├── .env.example
├── AGENTS.md                # AI agent guidelines
└── README.md
```

## Data Model

```
users                 → Application users with roles (admin, social_manager)
community_members     → People who receive community blasts
events                → Event definitions
event_registrations   → People registered for specific events
scheduled_messages    → Messages scheduled for future delivery
message_logs          → Send history with per-recipient results
unsubscribed_contacts → Opted-out phone numbers (STOP, manual)
suppressed_contacts   → Invalid/failed phone numbers (landlines, etc.)
login_attempts        → Failed login tracking for rate limiting
```

**Key concept**: Community members and event registrations are **separate pools**:
- **Community blast** → sends to everyone in `community_members`
- **Event blast** → sends only to people in `event_registrations` for that event

For detailed schema documentation, see [docs/database.md](docs/database.md).

## How to Run Locally

### 1. Clone and Setup

```bash
cd /path/to/AOC-SMS
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Optional helper (Codex-friendly; preserves existing `.env`):

```bash
./run/setup.sh
```

The helper script creates/uses the local `venv`, installs Python dependencies, and only copies
`.env.example` to `.env` if `.env` does not already exist.
It fails fast if your interpreter or existing `venv` is not Python 3.11.

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your Twilio credentials
```

Required environment variables:
- `TWILIO_ACCOUNT_SID` - Your Twilio Account SID
- `TWILIO_AUTH_TOKEN` - Your Twilio Auth Token
- `TWILIO_FROM_NUMBER` - Your Twilio phone number (E.164 format, e.g., +1234567890)
- `SECRET_KEY` - Flask secret key (generate with `python3.11 -c "import secrets; print(secrets.token_hex(32))"`)

### 3. Run Development Server

```bash
# With python-dotenv installed, .env is loaded automatically
export FLASK_ENV=development
flask --app wsgi:app run --debug
```

Or, without manually activating the venv:

```bash
./run/dev.sh
```

Recommended for full local functionality (starts Redis-backed RQ worker + web server):

```bash
./run/up.sh
```

Visit http://127.0.0.1:5000

---

## How to Deploy to Debian VPS

### 1. Server Preparation

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install required packages
sudo apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx git apache2-utils
```

### 2. Create Application User

```bash
sudo useradd -r -m -d /opt/sms-admin -s /bin/bash smsadmin
```

### 3. Clone Repository

```bash
sudo -u smsadmin git clone https://github.com/YOUR_REPO/AOC-SMS.git /opt/sms-admin
# Or copy files manually:
# sudo cp -r /path/to/AOC-SMS/* /opt/sms-admin/
sudo chown -R smsadmin:smsadmin /opt/sms-admin
```

### 4. Setup Python Environment

```bash
sudo -u smsadmin bash -c 'cd /opt/sms-admin && python3.11 -m venv venv'
sudo -u smsadmin bash -c 'cd /opt/sms-admin && source venv/bin/activate && pip install -r requirements.txt'
```

### 5. Install dbdoctor Command

```bash
sudo /opt/sms-admin/deploy/install.sh
```

> **Note:** The installer uses `/usr/local/bin/dbdoctor` by default. Override with `DBDOCTOR_DEST=/custom/path/dbdoctor`.
> The dbdoctor wrapper uses `/opt/sms-admin/venv/bin/python` by default; override with `SMS_ADMIN_PYTHON=/custom/venv/bin/python`.

### 6. Configure Environment Variables

The installer creates `/opt/sms-admin/.env` as `root:smsadmin` with `660` permissions so
members of the `smsadmin` group can update it. If your deploy user is not in that group,
either add them or edit the file via `sudo`.

```bash
# Create .env file (root:smsadmin, group-writable)
sudo install -m 660 -o root -g smsadmin /dev/null /opt/sms-admin/.env
sudo tee /opt/sms-admin/.env >/dev/null << EOF
TWILIO_ACCOUNT_SID=your_account_sid_here
TWILIO_AUTH_TOKEN=your_auth_token_here
TWILIO_FROM_NUMBER=+1234567890
SECRET_KEY=$(python3.11 -c "import secrets; print(secrets.token_hex(32))")
FLASK_ENV=production
EOF
```

### 7. Initialize Database

```bash
sudo -u smsadmin dbdoctor --apply
```

### 8. Create Log Directory

```bash
sudo mkdir -p /var/log/sms-admin
sudo chown smsadmin:smsadmin /var/log/sms-admin
```

### 9. Setup systemd Service

```bash
sudo cp /opt/sms-admin/deploy/sms.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sms
sudo systemctl start sms

# Check status
sudo systemctl status sms
```

### 10. Setup Scheduler Timer (for scheduled messages)

The scheduler uses a **systemd timer + oneshot service** (not a long-running daemon) to reliably process scheduled messages every **30 seconds**. This approach is more robust because:
- Each invocation is independent — no background threads that can die silently
- If the scheduler crashes, systemd invokes it again on the next tick
- Clear, auditable logs per run via journald
- The old long-running `sms-scheduler` process was unreliable (would start/stop without processing due messages)

The `deploy/install.sh` script handles all of this automatically, but for manual setup:

```bash
# Copy service and timer files
sudo cp /opt/sms-admin/deploy/sms-scheduler.service /etc/systemd/system/
sudo cp /opt/sms-admin/deploy/sms-scheduler.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable and start the timer (NOT the service directly)
sudo systemctl enable --now sms-scheduler.timer

# Verify timer is active
systemctl list-timers | grep sms-scheduler
```

**Verifying scheduled message processing:**

```bash
# Watch scheduler logs in real-time (shows message processing)
journalctl -u sms-scheduler.service -f

# Check timer status and next run time
systemctl list-timers sms-scheduler.timer

# Manually trigger the scheduler (for testing)
sudo systemctl start sms-scheduler.service

# Check if timer is enabled
systemctl is-enabled sms-scheduler.timer
```

**Common failure: Read-only SQLite database**

If the scheduler fails with "attempt to write a readonly database", the `instance/` directory or `sms.db` file has incorrect permissions. The install script fixes this automatically, but to fix manually:

```bash
# Fix instance directory permissions
sudo chown -R smsadmin:smsadmin /opt/sms-admin/instance
sudo chmod 750 /opt/sms-admin/instance
sudo chmod 660 /opt/sms-admin/instance/sms.db

# Also fix WAL/SHM files if they exist
sudo chmod 660 /opt/sms-admin/instance/sms.db-wal 2>/dev/null || true
sudo chmod 660 /opt/sms-admin/instance/sms.db-shm 2>/dev/null || true

# Verify by running scheduler manually
sudo -u smsadmin /opt/sms-admin/deploy/run_scheduler_once.sh
```

### 11. Database Migration Checks

```bash
# Print the current database path and migration status
sudo -u smsadmin dbdoctor --print

# Run the full database doctor report
sudo -u smsadmin dbdoctor --doctor

# Apply any pending migrations (also run automatically by systemd ExecStartPre)
sudo -u smsadmin dbdoctor --apply
```

Migrations run automatically on restart because the systemd units include an `ExecStartPre` step that calls `dbdoctor --apply`.

### 12. systemd Override Examples (ExecStartPre)

Use drop-in overrides if you need to enforce or customize the migration step for both services.
These examples explicitly run migrations *before* the main process starts, and the journal will
show the `ExecStartPre` step before the service `ExecStart` line.

```bash
sudo systemctl edit sms
```

```ini
[Service]
ExecStartPre=
ExecStartPre=/usr/local/bin/dbdoctor --apply
```

```bash
sudo systemctl edit sms-scheduler
```

```ini
[Service]
ExecStartPre=
ExecStartPre=/usr/local/bin/dbdoctor --apply
```

### 13. Verify systemd Migration Order and Logs

Run these commands after any unit edits to confirm migrations run before service startup:

```bash
sudo systemctl daemon-reload
sudo systemctl restart sms sms-scheduler
sudo journalctl -u sms -u sms-scheduler -b --no-pager
```

In the journal output, the `ExecStartPre` lines for `dbdoctor --apply` should appear before
the `ExecStart` lines for both `sms` and `sms-scheduler`.

### 14. Setup Nginx HTTP Basic Auth

```bash
# Create password file (replace 'admin' with your username)
sudo htpasswd -c /etc/nginx/.htpasswd admin
# Enter password when prompted
```

### 15. Configure Nginx

```bash
# Copy nginx config
sudo cp /opt/sms-admin/deploy/nginx.conf /etc/nginx/sites-available/sms.theitwingman.com

# Create symlink
sudo ln -s /etc/nginx/sites-available/sms.theitwingman.com /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# For initial setup without SSL, temporarily edit the config to only use HTTP:
sudo nano /etc/nginx/sites-available/sms.theitwingman.com
# Comment out the SSL server block and modify the HTTP block to proxy directly
```

### 16. Setup SSL with Let's Encrypt

```bash
# Get SSL certificate
sudo certbot --nginx -d sms.theitwingman.com

# Certbot will automatically update nginx config
# Reload nginx
sudo systemctl reload nginx
```

### 17. Verify Deployment

```bash
# Check health endpoint (no auth required)
curl https://sms.theitwingman.com/health

# Access the app in browser
# https://sms.theitwingman.com
# Enter HTTP Basic Auth credentials
```

---

## Management Commands

```bash
# View logs
sudo journalctl -u sms -f

# Restart service
sudo systemctl restart sms

# Check gunicorn logs
sudo tail -f /var/log/sms-admin/error.log

# Backup database
sudo cp /opt/sms-admin/instance/sms.db /backup/sms-$(date +%Y%m%d).db
```

## CSV Import Formats

### Recipients CSV

**Three columns (first name, last name, phone):**
```csv
Vardan,Hovsepyan,(323) 630-0201
Jane,Smith,720-383-2388
```

**Two columns (name, phone):**
```csv
name,phone
John Doe,+1234567890
Jane Smith,(303) 918-8410
```

**Phone only:**
```csv
720-383-2388
303-918-8410
(323) 630-0201
```

Phone formats accepted: `+1234567890`, `(323) 630-0201`, `720-383-2388`, `3236300201`

### Event Registrations CSV
```csv
720-383-2388
303-918-8410
```

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app

# Run specific test file
pytest tests/test_utils.py
```

## Assumptions

1. **Low Volume**: SMS sent individually with 100ms delay; suitable for <1000 recipients per blast
2. **US Phone Numbers**: Phone normalization assumes US (+1) if no country code provided
3. **UTC Timestamps**: All timestamps stored in UTC
4. **SQLite**: Suitable for single-server deployment; not for high-concurrency scenarios

## Security Notes

- Never commit `.env` file (it's gitignored)
- Twilio credentials loaded from environment only
- Flask debug mode disabled in production
- Flask-Login handles authentication with password hashing
- CSRF protection via Flask-WTF
- Login rate limiting (5 attempts / 5 min → 10 min lockout)
- HTTPS enforced via Nginx redirect
- Session cookies: HTTP-only, Secure, SameSite=Lax

For more details, see [docs/troubleshooting.md](docs/troubleshooting.md).
