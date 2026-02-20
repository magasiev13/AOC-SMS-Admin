# Configuration Guide

SMS Admin uses environment variables for configuration. All settings are loaded via `app/config.py`.

## Environment File

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

## Required Variables

### Twilio Credentials

| Variable | Description | Example |
|----------|-------------|---------|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID | `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token | `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `TWILIO_FROM_NUMBER` | Twilio phone number (E.164) | `+18005551234` |

### Flask Security

| Variable | Description | Example |
|----------|-------------|---------|
| `SECRET_KEY` | Flask secret key for sessions | Generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_PASSWORD` | Initial admin password | Required in production |

## Optional Variables

### Flask

| Variable | Default | Description |
|----------|---------|-------------|
| `FLASK_ENV` | `production` | Set to `development` for debug mode |
| `FLASK_DEBUG` | `0` | Set to `1` to enable debug mode |

### Proxy / Reverse Proxy

| Variable | Default | Description |
|----------|---------|-------------|
| `TRUST_PROXY` | `0` | Set to `1` to enable `ProxyFix` and trust forwarded headers from a known reverse proxy |

### Admin

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_USERNAME` | `admin` | Initial admin username |
| `ADMIN_TEST_PHONE` | - | Phone number for test mode sends |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///instance/sms.db` | SQLAlchemy database URI |
| `SQLITE_TIMEOUT` | `30` | SQLite lock timeout in seconds |

If `DATABASE_URL` is unset, the app defaults to `instance/sms.db` under the project root.

### Redis / Background Jobs

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `RQ_QUEUE_NAME` | `sms` | RQ queue name |

### Scheduler

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHEDULER_ENABLED` | `0` (prod), `1` (dev) | Enable APScheduler background thread |
| `SCHEDULER_RUNNER` | - | Set to `1` in scheduler service |
| `SCHEDULED_MESSAGE_MAX_LAG` | `1440` | Minutes before scheduled message expires |

### Timezone

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_TIMEZONE` | `UTC` | Default timezone for display |

### Session Security

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_COOKIE_SAMESITE` | `Lax` | Cookie SameSite policy |
| `SESSION_COOKIE_SECURE` | `1` (prod), `0` (dev) | Require HTTPS for cookies |

### Login Hardening (Recommended for Production)

| Variable | Recommended | Non-technical description |
|----------|-------------|---------------------------|
| `AUTH_ATTEMPT_WINDOW_SECONDS` | `300` | Time window (in seconds) used to count failed sign-ins. |
| `AUTH_LOCKOUT_SECONDS` | `900` | How long a lockout lasts after too many failed sign-ins. |
| `AUTH_MAX_ATTEMPTS_IP_ACCOUNT` | `5` | Failed sign-ins allowed for one username from one IP before lockout starts. |
| `AUTH_MAX_ATTEMPTS_ACCOUNT` | `8` | Failed sign-ins allowed for one username across all IPs before lockout starts. |
| `AUTH_MAX_ATTEMPTS_IP` | `30` | Failed sign-ins allowed from one IP across all usernames before lockout starts. |
| `SESSION_IDLE_TIMEOUT_MINUTES` | `30` | Maximum idle time before a session expires. |
| `REMEMBER_COOKIE_DURATION_DAYS` | `7` | How long “Remember me” keeps a user logged in. |
| `AUTH_PASSWORD_MIN_LENGTH` | `12` | Minimum password length accepted in user forms. |
| `AUTH_PASSWORD_POLICY_ENFORCE` | `1` | Turns password policy checks on (`1`) or off (`0`). |
| `TRUSTED_HOSTS` | `sms.theitwingman.com` | Comma-separated hostnames the app should trust in production requests. |

### Production Deploy Behavior For Security Keys

- Deploy appends missing hardening keys to existing `/opt/sms-admin/.env`.
- Existing values are never overwritten automatically.
- If an existing value is not the recommended value, deploy prints a warning for manual review.

## Configuration Class

`app/config.py` defines the `Config` class:

```python
class Config:
    # Flask
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    DEBUG = os.environ.get('FLASK_DEBUG') == '1' or os.environ.get('FLASK_ENV') == 'development'
    TRUST_PROXY = os.environ.get('TRUST_PROXY', '0') == '1'

    # Security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax')
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', '1' if not DEBUG else '0') == '1'
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = SESSION_COOKIE_SAMESITE
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    SESSION_IDLE_TIMEOUT_MINUTES = int(os.environ.get('SESSION_IDLE_TIMEOUT_MINUTES', '30'))
    REMEMBER_COOKIE_DURATION_DAYS = int(os.environ.get('REMEMBER_COOKIE_DURATION_DAYS', '7'))

    AUTH_ATTEMPT_WINDOW_SECONDS = int(os.environ.get('AUTH_ATTEMPT_WINDOW_SECONDS', '300'))
    AUTH_LOCKOUT_SECONDS = int(os.environ.get('AUTH_LOCKOUT_SECONDS', '900'))
    AUTH_MAX_ATTEMPTS_IP_ACCOUNT = int(os.environ.get('AUTH_MAX_ATTEMPTS_IP_ACCOUNT', '5'))
    AUTH_MAX_ATTEMPTS_ACCOUNT = int(os.environ.get('AUTH_MAX_ATTEMPTS_ACCOUNT', '8'))
    AUTH_MAX_ATTEMPTS_IP = int(os.environ.get('AUTH_MAX_ATTEMPTS_IP', '30'))

    AUTH_PASSWORD_MIN_LENGTH = int(os.environ.get('AUTH_PASSWORD_MIN_LENGTH', '12'))
    AUTH_PASSWORD_POLICY_ENFORCE = os.environ.get('AUTH_PASSWORD_POLICY_ENFORCE', '1') == '1'
    TRUSTED_HOSTS = [h.strip() for h in os.environ.get('TRUSTED_HOSTS', '').split(',') if h.strip()]

    # Scheduler
    SCHEDULER_ENABLED = os.environ.get('SCHEDULER_ENABLED', '1' if DEBUG else '0') == '1'
    SCHEDULED_MESSAGE_MAX_LAG = int(os.environ.get('SCHEDULED_MESSAGE_MAX_LAG', '1440'))

    APP_TIMEZONE = os.environ.get('APP_TIMEZONE', 'UTC')

    # Redis / RQ
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    RQ_QUEUE_NAME = os.environ.get('RQ_QUEUE_NAME', 'sms')

    # Database
    BASE_DIR = Path(__file__).resolve().parent.parent
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        f"sqlite:///{BASE_DIR / 'instance' / 'sms.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
    }

    # Twilio
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
    TWILIO_FROM_NUMBER = os.environ.get('TWILIO_FROM_NUMBER')

    # Admin
    ADMIN_TEST_PHONE = os.environ.get('ADMIN_TEST_PHONE')
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
```

## Production vs Development

| Setting | Development | Production |
|---------|-------------|------------|
| `DEBUG` | `True` | `False` |
| `SECRET_KEY` | Defaults allowed | **Required** |
| `ADMIN_PASSWORD` | Optional | **Required** |
| `SESSION_COOKIE_SECURE` | `False` | `True` |
| `AUTH_PASSWORD_POLICY_ENFORCE` | Optional | `True` |
| `TRUSTED_HOSTS` | Optional | **Required** |
| `SCHEDULER_ENABLED` | `True` | `False` (use systemd timer) |

## Security Checks

On startup in production (`DEBUG=False`):

1. **SECRET_KEY validation** - App refuses to start with default dev key
2. **ADMIN_PASSWORD validation** - Required to create initial admin user
3. **Security hardening validation** - Critical auth/session values must be in safe ranges
4. **TRUSTED_HOSTS validation** - Must be set to at least one hostname

## Example Production .env

```bash
# Twilio (required)
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM_NUMBER=+18005551234

# Flask (required)
SECRET_KEY=your-256-bit-random-hex-key
FLASK_ENV=production
# Reverse proxy (optional; set to 1 only behind a trusted proxy)
TRUST_PROXY=1
TRUSTED_HOSTS=sms.example.com

# Admin (required)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password

# Login hardening (recommended)
AUTH_ATTEMPT_WINDOW_SECONDS=300
AUTH_LOCKOUT_SECONDS=900
AUTH_MAX_ATTEMPTS_IP_ACCOUNT=5
AUTH_MAX_ATTEMPTS_ACCOUNT=8
AUTH_MAX_ATTEMPTS_IP=30
SESSION_IDLE_TIMEOUT_MINUTES=30
REMEMBER_COOKIE_DURATION_DAYS=7
AUTH_PASSWORD_MIN_LENGTH=12
AUTH_PASSWORD_POLICY_ENFORCE=1

# Optional: Test phone for test mode
ADMIN_TEST_PHONE=+1234567890

# Database (optional, defaults work)
DATABASE_URL=sqlite:///instance/sms.db

# Redis (required for background jobs)
REDIS_URL=redis://localhost:6379/0
RQ_QUEUE_NAME=sms

# Scheduler (disabled in prod, use systemd timer)
SCHEDULER_ENABLED=0

# Timezone
APP_TIMEZONE=America/Denver
```

## Example Development .env

```bash
# Twilio (required for actual SMS sending)
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM_NUMBER=+18005551234

# Flask
SECRET_KEY=dev-secret-key
FLASK_ENV=development

# Admin (optional in dev)
ADMIN_PASSWORD=admin

# Test phone
ADMIN_TEST_PHONE=+1234567890

# Scheduler (enabled for dev)
SCHEDULER_ENABLED=1

# Redis (if running locally)
REDIS_URL=redis://localhost:6379/0
```

## Generating SECRET_KEY

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## File Permissions

In production, protect the .env file:

```bash
# Create with restricted permissions
sudo install -m 660 -o root -g smsadmin /dev/null /opt/sms-admin/.env

# Or fix existing file
sudo chown root:smsadmin /opt/sms-admin/.env
sudo chmod 660 /opt/sms-admin/.env
```

This allows:
- Root to edit the file
- smsadmin group members to read/write
- No access for others
