# Troubleshooting Guide

Common issues and their solutions.

## Database Issues

### "attempt to write a readonly database"

**Cause:** SQLite database file or directory not writable by smsadmin user.

**Solution:**
```bash
sudo chown -R smsadmin:smsadmin /opt/sms-admin/instance
sudo chmod 750 /opt/sms-admin/instance
sudo chmod 640 /opt/sms-admin/instance/sms.db

# Also fix WAL/SHM files if they exist
sudo chmod 640 /opt/sms-admin/instance/sms.db-wal 2>/dev/null || true
sudo chmod 640 /opt/sms-admin/instance/sms.db-shm 2>/dev/null || true
```

### "database is locked"

**Cause:** Long-running transaction or multiple processes writing simultaneously.

**Solutions:**
1. Increase SQLite timeout:
   ```bash
   SQLITE_TIMEOUT=60
   ```

2. Check for stuck processes:
   ```bash
   sudo fuser /opt/sms-admin/instance/sms.db
   ```

3. Restart services:
   ```bash
   sudo systemctl restart sms sms-worker
   ```

### Schema Mismatch / Missing Columns

**Cause:** Migrations not applied.

**Solution:**
```bash
sudo -u smsadmin dbdoctor --apply
sudo systemctl restart sms
```

### Database Corruption

**Diagnosis:**
```bash
sqlite3 /opt/sms-admin/instance/sms.db "PRAGMA integrity_check;"
```

**Recovery:**
```bash
# Attempt repair
sqlite3 /opt/sms-admin/instance/sms.db ".recover" | sqlite3 /opt/sms-admin/instance/sms-recovered.db

# Or restore from backup
sudo cp /backup/sms-latest.db /opt/sms-admin/instance/sms.db
```

---

## Scheduler Issues

### Scheduled Messages Not Sending

**Check timer status:**
```bash
systemctl list-timers | grep sms-scheduler
# Should show next trigger time
```

**Check scheduler logs:**
```bash
journalctl -u sms-scheduler.service -n 50
```

**Common causes:**
1. Timer not enabled:
   ```bash
   sudo systemctl enable --now sms-scheduler.timer
   ```

2. Database not writable (see above)

3. No pending messages:
   ```bash
   sqlite3 /opt/sms-admin/instance/sms.db "SELECT id, status, scheduled_at FROM scheduled_messages WHERE status='pending';"
   ```

### Messages Stuck in "processing"

**Cause:** Scheduler crashed mid-processing.

**Solution:** Messages stuck for >10 minutes are automatically marked as failed on next scheduler run.

**Manual fix:**
```bash
sqlite3 /opt/sms-admin/instance/sms.db "UPDATE scheduled_messages SET status='failed', error_message='Manual reset' WHERE status='processing';"
```

### Old Scheduler Still Running

If using the old long-running scheduler service:
```bash
sudo systemctl stop sms-scheduler
sudo systemctl disable sms-scheduler
sudo systemctl enable --now sms-scheduler.timer
```

---

## Twilio Issues

### "Twilio credentials not configured"

**Cause:** Missing environment variables.

**Check:**
```bash
sudo cat /opt/sms-admin/.env | grep TWILIO
```

**Required:**
```bash
TWILIO_ACCOUNT_SID=ACxxxx
TWILIO_AUTH_TOKEN=xxxx
TWILIO_FROM_NUMBER=+1xxxx
```

### Rate Limiting (Error 429)

**Cause:** Sending too fast.

**Solutions:**
1. Increase delay between sends (default 0.1s is usually fine)
2. Use Twilio Messaging Services for higher throughput
3. RQ will automatically retry with exponential backoff

### Invalid Phone Numbers

**Symptoms:** High failure rate, error codes 30003, 30005, 30007.

**Solution:** Enable automatic suppression (already enabled by default). Failed numbers are added to `suppressed_contacts` and skipped in future sends.

### Opt-Out Errors (21610, 30004)

**Cause:** Recipient replied STOP.

**Behavior:** Automatically added to `unsubscribed_contacts`.

---

## Authentication Issues

### "Too many failed attempts"

**Cause:** Rate limiting after 5 failed login attempts.

**Solution:** Wait 10 minutes or clear from database:
```bash
sqlite3 /opt/sms-admin/instance/sms.db "DELETE FROM login_attempts WHERE client_ip='x.x.x.x';"
```

If account-scoped lockouts are enabled, also clear username-scoped entries:
```bash
sqlite3 /opt/sms-admin/instance/sms.db "DELETE FROM login_attempts WHERE client_ip='__account__' AND username='admin';"
```

### Forgot Password

**Solution:** Reset via database:
```bash
# Generate new hash
python3.11 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('newpassword'))"

# Update in database
sqlite3 /opt/sms-admin/instance/sms.db "UPDATE users SET password_hash='pbkdf2:sha256:...' WHERE username='admin';"
```

### No Admin User

**Cause:** `ADMIN_PASSWORD` not set on first startup.

**Solution:**
```bash
# Add to .env
echo "ADMIN_PASSWORD=your-password" | sudo tee -a /opt/sms-admin/.env

# Restart to create admin
sudo systemctl restart sms
```

---

## Service Issues

### "expected 3.11" / "python version mismatch"

**Cause:** The app venv was created with the wrong Python version.

**Check:**
```bash
/opt/sms-admin/venv/bin/python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
```

**Fix:**
```bash
sudo systemctl stop sms sms-worker sms-scheduler.timer
sudo rm -rf /opt/sms-admin/venv
sudo -u smsadmin bash -c 'cd /opt/sms-admin && python3.11 -m venv venv'
sudo -u smsadmin bash -c 'cd /opt/sms-admin && source venv/bin/activate && pip install -r requirements.txt'
sudo systemctl start sms sms-worker sms-scheduler.timer
```

### sms.service Won't Start

**Check logs:**
```bash
sudo journalctl -u sms -n 100
```

**Common causes:**
1. Missing SECRET_KEY in production
2. Database migrations failed
3. Port already in use

### sms-worker.service Not Processing Jobs

**Check:**
```bash
sudo systemctl status sms-worker
redis-cli ping  # Should return PONG
rq info --url redis://localhost:6379/0
```

**Common causes:**
1. Redis not running:
   ```bash
   sudo systemctl start redis
   ```

2. Wrong queue name:
   ```bash
   # Check .env
   RQ_QUEUE_NAME=sms
   ```

### Gunicorn Socket Permission Denied

**Cause:** Nginx can't read Gunicorn socket.

**Solution:**
```bash
# Add www-data to smsadmin group
sudo usermod -aG smsadmin www-data
sudo systemctl restart nginx
```

---

## Performance Issues

### Slow Dashboard

**Cause:** Large message_logs table.

**Solutions:**
1. Archive old logs
2. Add indexes (handled by migrations)
3. Clear logs periodically (admin feature)

### High Memory Usage

**Cause:** Too many Gunicorn workers.

**Solution:** Reduce workers in `sms.service`:
```bash
--workers 2
```

---

## Nginx Issues

### 502 Bad Gateway

**Cause:** Gunicorn not running or socket missing.

**Check:**
```bash
sudo systemctl status sms
ls -la /opt/sms-admin/sms.sock
```

### 403 Forbidden

**Cause:** HTTP Basic Auth failed or missing.

**Check:**
```bash
sudo cat /etc/nginx/.htpasswd
```

---

## Diagnostic Commands

### Full System Check

```bash
# Service status
sudo systemctl status sms sms-worker
systemctl list-timers | grep sms-scheduler

# Database health
sudo -u smsadmin dbdoctor --doctor

# Redis
redis-cli ping

# Recent logs
sudo journalctl -u sms -u sms-worker -u sms-scheduler.service -n 50 --no-pager
```

### Database Inspection

```bash
sqlite3 /opt/sms-admin/instance/sms.db <<EOF
.mode column
.headers on
SELECT 'Community Members' as table_name, COUNT(*) as count FROM community_members
UNION ALL SELECT 'Events', COUNT(*) FROM events
UNION ALL SELECT 'Event Registrations', COUNT(*) FROM event_registrations
UNION ALL SELECT 'Message Logs', COUNT(*) FROM message_logs
UNION ALL SELECT 'Scheduled Messages', COUNT(*) FROM scheduled_messages
UNION ALL SELECT 'Unsubscribed', COUNT(*) FROM unsubscribed_contacts
UNION ALL SELECT 'Suppressed', COUNT(*) FROM suppressed_contacts;
EOF
```

### Check Pending Work

```bash
# Pending scheduled messages
sqlite3 /opt/sms-admin/instance/sms.db "SELECT id, scheduled_at, status FROM scheduled_messages WHERE status='pending' ORDER BY scheduled_at;"

# Processing logs
sqlite3 /opt/sms-admin/instance/sms.db "SELECT id, created_at, status FROM message_logs WHERE status='processing';"

# RQ jobs
rq info --url redis://localhost:6379/0
```

---

## Getting Help

1. Check this troubleshooting guide
2. Review logs: `journalctl -u sms -u sms-worker -u sms-scheduler.service`
3. Run database doctor: `dbdoctor --doctor`
4. Check GitHub issues
5. Contact maintainers
