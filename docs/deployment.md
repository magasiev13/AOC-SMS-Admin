# Deployment Guide

This guide covers deploying SMS Admin to a Debian/Ubuntu VPS.

## Prerequisites

- Debian 11+ or Ubuntu 20.04+
- Python 3.11 (supported/tested)
- Redis server
- Domain name with DNS configured
- Twilio account with phone number

## Quick Deploy

```bash
# Clone repository
sudo -u smsadmin git clone https://github.com/YOUR_REPO/AOC-SMS.git /opt/sms-admin
cd /opt/sms-admin

# Run automated installer
sudo ./deploy/install.sh
```

The installer handles:
- Installing `dbdoctor` CLI
- Creating `.env` file with correct permissions
- Running database migrations
- Installing and enabling systemd services
- Running smoke tests

## Manual Deployment Steps

### 1. System Preparation

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    python3.11 python3.11-venv python3-pip \
    nginx certbot python3-certbot-nginx \
    redis-server \
    git apache2-utils
```

### 2. Create Application User

```bash
sudo useradd -r -m -d /opt/sms-admin -s /bin/bash smsadmin
```

### 3. Clone Repository

```bash
sudo -u smsadmin git clone https://github.com/YOUR_REPO/AOC-SMS.git /opt/sms-admin
sudo chown -R smsadmin:smsadmin /opt/sms-admin
```

### 4. Setup Python Environment

```bash
sudo -u smsadmin bash -c 'cd /opt/sms-admin && python3.11 -m venv venv'
sudo -u smsadmin bash -c 'cd /opt/sms-admin && source venv/bin/activate && pip install -r requirements.txt'
sudo -u smsadmin /opt/sms-admin/venv/bin/python -c "import sys; print(f'Python {sys.version_info.major}.{sys.version_info.minor}')"
```

### 5. Configure Environment

```bash
# Create .env with restricted permissions
sudo install -m 660 -o root -g smsadmin /dev/null /opt/sms-admin/.env

# Edit configuration
sudo nano /opt/sms-admin/.env
```

Minimum required:
```bash
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM_NUMBER=+18005551234
SECRET_KEY=$(python3.11 -c "import secrets; print(secrets.token_hex(32))")
FLASK_ENV=production
ADMIN_PASSWORD=your-secure-password
REDIS_URL=redis://localhost:6379/0
```

### 6. Install dbdoctor

```bash
sudo install -m 0755 /opt/sms-admin/bin/dbdoctor /usr/local/bin/dbdoctor
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

### 9. Install systemd Services

```bash
# Copy service files
sudo cp /opt/sms-admin/deploy/sms.service /etc/systemd/system/
sudo cp /opt/sms-admin/deploy/sms-worker.service /etc/systemd/system/
sudo cp /opt/sms-admin/deploy/sms-scheduler.service /etc/systemd/system/
sudo cp /opt/sms-admin/deploy/sms-scheduler.timer /etc/systemd/system/

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable --now sms sms-worker
sudo systemctl enable --now sms-scheduler.timer
```

### 10. Configure Nginx

```bash
# Copy config
sudo cp /opt/sms-admin/deploy/nginx.conf /etc/nginx/sites-available/sms.example.com

# Edit domain name
sudo nano /etc/nginx/sites-available/sms.example.com

# Enable site
sudo ln -s /etc/nginx/sites-available/sms.example.com /etc/nginx/sites-enabled/

# Test and reload
sudo nginx -t
sudo systemctl reload nginx
```

### 11. Setup HTTP Basic Auth

```bash
sudo htpasswd -c /etc/nginx/.htpasswd admin
```

### 12. Setup SSL

```bash
sudo certbot --nginx -d sms.example.com
```

## systemd Services

### sms.service (Main Web App)

Runs Gunicorn WSGI server.

```ini
[Unit]
Description=SMS Admin Web Application
After=network.target

[Service]
User=smsadmin
Group=smsadmin
WorkingDirectory=/opt/sms-admin
EnvironmentFile=/opt/sms-admin/.env
ExecStartPre=/opt/sms-admin/deploy/check_python_runtime.sh
ExecStartPre=/usr/local/bin/dbdoctor --apply
ExecStart=/opt/sms-admin/venv/bin/gunicorn \
    --workers 2 \
    --bind unix:/opt/sms-admin/sms.sock \
    --access-logfile /var/log/sms-admin/access.log \
    --error-logfile /var/log/sms-admin/error.log \
    wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

### sms-worker.service (Background Jobs)

Runs RQ worker for async SMS sending.

```ini
[Unit]
Description=SMS Admin RQ Worker
After=network.target redis.service

[Service]
User=smsadmin
Group=smsadmin
WorkingDirectory=/opt/sms-admin
EnvironmentFile=/opt/sms-admin/.env
ExecStartPre=/opt/sms-admin/deploy/check_python_runtime.sh
ExecStart=/opt/sms-admin/venv/bin/rq worker sms
Restart=always

[Install]
WantedBy=multi-user.target
```

### sms-scheduler.timer (Scheduler Timer)

Triggers scheduler every 30 seconds.

```ini
[Unit]
Description=SMS Admin Scheduler Timer

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
AccuracySec=1s

[Install]
WantedBy=timers.target
```

### sms-scheduler.service (Scheduler Oneshot)

Processes pending scheduled messages.

```ini
[Unit]
Description=SMS Admin Scheduler (oneshot)
After=network.target

[Service]
Type=oneshot
User=smsadmin
Group=smsadmin
WorkingDirectory=/opt/sms-admin
EnvironmentFile=/opt/sms-admin/.env
ExecStartPre=/opt/sms-admin/deploy/check_python_runtime.sh
ExecStart=/bin/bash /opt/sms-admin/deploy/run_scheduler_once.sh
```

## Verification

### Check Service Status

```bash
sudo systemctl status sms sms-worker
systemctl list-timers | grep sms-scheduler
```

### Check Health Endpoint

```bash
curl https://sms.example.com/health
# Should return: OK
```

### View Logs

```bash
# Web app logs
sudo journalctl -u sms -f

# Worker logs
sudo journalctl -u sms-worker -f

# Scheduler logs
sudo journalctl -u sms-scheduler.service -f

# Gunicorn logs
sudo tail -f /var/log/sms-admin/error.log
```

### Database Health

```bash
sudo -u smsadmin dbdoctor --doctor
```

## Updating

```bash
cd /opt/sms-admin

# Pull latest code
sudo -u smsadmin git pull

# Update dependencies
sudo -u smsadmin bash -c 'source venv/bin/activate && pip install -r requirements.txt'

# Apply migrations
sudo -u smsadmin dbdoctor --apply

# Restart services
sudo systemctl restart sms sms-worker
```

## Backup

### Database

```bash
# Stop for consistent backup
sudo systemctl stop sms sms-worker
sudo -u smsadmin sqlite3 /opt/sms-admin/instance/sms.db ".backup /backup/sms-$(date +%Y%m%d).db"
sudo systemctl start sms sms-worker
```

### Full Application

```bash
sudo tar -czf /backup/sms-admin-$(date +%Y%m%d).tar.gz \
    --exclude='venv' \
    --exclude='__pycache__' \
    /opt/sms-admin
```

## Restore

```bash
# Stop services
sudo systemctl stop sms sms-worker

# Restore database
sudo -u smsadmin cp /backup/sms-20240115.db /opt/sms-admin/instance/sms.db

# Start services
sudo systemctl start sms sms-worker
```

## Scaling Considerations

### Multiple Workers

Increase Gunicorn workers in `sms.service`:
```bash
--workers 4
```

### Multiple RQ Workers

```bash
sudo systemctl enable sms-worker@{1..3}
sudo systemctl start sms-worker@{1..3}
```

(Requires parameterized service file)

### External Database

For high-concurrency, consider PostgreSQL:
```bash
DATABASE_URL=postgresql://user:pass@host:5432/smsdb
```

Note: SQLite migrations are SQLite-specific; use Alembic for PostgreSQL.
