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
| `TWILIO_ACCOUNT_SID` | Twilio Account SID. In SaaS platform-managed mode this must be the parent/master account used to provision subaccounts. | `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token for the same parent/master account | `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `TWILIO_FROM_NUMBER` | Twilio phone number (E.164). Required for the legacy single-tenant runtime; optional in SaaS platform-managed mode. | `+18005551234` |

### Twilio A2P Automation

| Variable | Default | Description |
|----------|---------|-------------|
| `TWILIO_A2P_ONBOARDING_ENABLED` | `0` | Enables the platform-managed A2P onboarding flow. |
| `TWILIO_A2P_EVENT_STREAMS_ENABLED` | `0` | Enables the optional Twilio Event Streams webhook for push-based A2P status updates. The app provisions org-specific webhook destinations under `/webhooks/twilio/a2p-events?organization_id=<id>`. |
| `TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN` | unset | Optional secondary bearer token accepted by the Twilio Event Streams webhook. The primary trust check is Twilio signature validation over the raw JSON body. |
| `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID` | unset | Primary Twilio Trust Hub customer profile SID used when linking secondary A2P onboarding bundles. |

### Flask Security

| Variable | Description | Example |
|----------|-------------|---------|
| `SECRET_KEY` | Flask secret key for sessions | Generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_PASSWORD` | Initial admin password | Required for legacy production bootstrap; bootstrap-only for first SaaS platform admin provisioning |

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
| `ADMIN_USERNAME` | `admin` | Initial admin username or first SaaS platform admin username |
| `ADMIN_TEST_PHONE` | - | Phone number for test mode sends |

### SaaS Platform Operations

| Variable | Default | Description |
|----------|---------|-------------|
| `SAAS_MODE` | `0` | Enable the SaaS control plane and SaaS startup validation |
| `SAAS_BASE_URL` | empty | Public base URL used for Twilio inbound webhook binding and other absolute links |
| `PLATFORM_SERVICE_RESTART_ENABLED` | `0` | Enable the platform-admin-only restart control on `/platform` |
| `PLATFORM_SERVICE_RESTART_SCRIPT` | `/usr/local/bin/restart-sms-saas-services` | Absolute path to the fixed restart helper executed via `sudo -n` |

### SaaS Twilio Platform Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `TWILIO_API_KEY_SID` | empty | Optional Twilio API Key SID for production REST auth. Must be paired with `TWILIO_API_KEY_SECRET`. |
| `TWILIO_API_KEY_SECRET` | empty | Optional Twilio API Key secret for production REST auth. Must be paired with `TWILIO_API_KEY_SID`. |
| `TWILIO_CREDENTIAL_ENCRYPTION_KEY` | empty | Required in SaaS mode. Fernet key used to encrypt per-organization Twilio secrets at rest. |
| `TWILIO_A2P_ONBOARDING_ENABLED` | `0` | Enable automated Twilio Trust Hub / A2P onboarding flows for organizations. |
| `TWILIO_PRIMARY_CUSTOMER_PROFILE_SID` | empty | Required when `TWILIO_A2P_ONBOARDING_ENABLED=1`. Must be the primary Trust Hub customer-profile bundle (`BU...`), not an address or supporting-document bundle. |
| `TWILIO_A2P_NUMBER_COUNTRY` | `US` | Default country used when the app auto-buys an A2P sender number. |

There is no separate `TWILIO_PARENT_ACCOUNT_SID` setting. The app treats `TWILIO_ACCOUNT_SID` as the parent/master account for provisioning and parent-number transfer.

When `SAAS_BASE_URL` is set, the app also exposes tenant-hosted SMS compliance pages at `/compliance/<organization-slug>/sms/privacy`, `/terms`, and `/opt-in`. These URLs are generated for every org and act as the automatic A2P fallback package whenever the tenant does not have a public site or the supplied public website/privacy/terms/CTA URLs fail validation.

The self-serve A2P flow now defaults eligible EIN-backed businesses to `low_volume_standard` and defaults the campaign posture to `ACCOUNT_NOTIFICATION`. Organizations can still move to `standard` later when they need more throughput or explicitly request the upgrade.

When an org resubmits after a failed Twilio campaign review, the app now inspects the Messaging Service before creating a replacement campaign. It only auto-deletes the attached failed campaign when Twilio requires a new campaign, such as a use-case change or a brand-side correction. If the failed campaign is still the same use case and brand, the app refuses to recreate it automatically so operators do not accidentally trigger another paid vetting cycle.

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
| `REMEMBER_COOKIE_DURATION_DAYS` | `7` | Remember-me session lifetime |

### Auth Hardening

| Variable | Default | Description |
|----------|---------|-------------|
| `PASSWORD_HISTORY_COUNT` | `3` | Number of prior passwords blocked for reuse |
| `AUTH_ALERTS_ENABLED` | `1` | Enable SMS security alerts |
| `AUTH_EVENT_RETENTION_DAYS` | `180` | Auth event retention window |
| `AUTH_LOCKOUT_MAX_ATTEMPTS` | `5` | Failed login attempts before lockout |
| `AUTH_LOCKOUT_WINDOW_SECONDS` | `300` | Failure counting window |
| `AUTH_LOCKOUT_SECONDS` | `600` | Lockout duration |

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
    SAAS_MODE = os.environ.get('SAAS_MODE', '0') == '1'
    SAAS_BASE_URL = os.environ.get('SAAS_BASE_URL', '')

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
    PASSWORD_HISTORY_COUNT = int(os.environ.get('PASSWORD_HISTORY_COUNT', '3'))
    AUTH_ALERTS_ENABLED = os.environ.get('AUTH_ALERTS_ENABLED', '1') == '1'
    AUTH_EVENT_RETENTION_DAYS = int(os.environ.get('AUTH_EVENT_RETENTION_DAYS', '180'))
    AUTH_LOCKOUT_MAX_ATTEMPTS = int(os.environ.get('AUTH_LOCKOUT_MAX_ATTEMPTS', '5'))
    AUTH_LOCKOUT_WINDOW_SECONDS = int(os.environ.get('AUTH_LOCKOUT_WINDOW_SECONDS', '300'))

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
    TWILIO_API_KEY_SID = os.environ.get('TWILIO_API_KEY_SID')
    TWILIO_API_KEY_SECRET = os.environ.get('TWILIO_API_KEY_SECRET')
    TWILIO_FROM_NUMBER = os.environ.get('TWILIO_FROM_NUMBER')
    TWILIO_CREDENTIAL_ENCRYPTION_KEY = os.environ.get('TWILIO_CREDENTIAL_ENCRYPTION_KEY')
    TWILIO_VALIDATE_INBOUND_SIGNATURE = os.environ.get('TWILIO_VALIDATE_INBOUND_SIGNATURE', '1') == '1'
    TWILIO_A2P_ONBOARDING_ENABLED = os.environ.get('TWILIO_A2P_ONBOARDING_ENABLED', '0') == '1'
    TWILIO_PRIMARY_CUSTOMER_PROFILE_SID = os.environ.get('TWILIO_PRIMARY_CUSTOMER_PROFILE_SID')
    PLATFORM_SERVICE_RESTART_ENABLED = os.environ.get('PLATFORM_SERVICE_RESTART_ENABLED', '0') == '1'
    PLATFORM_SERVICE_RESTART_SCRIPT = os.environ.get(
        'PLATFORM_SERVICE_RESTART_SCRIPT',
        '/usr/local/bin/restart-sms-saas-services',
    )

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
| `ADMIN_PASSWORD` | Optional | **Required** for legacy runtime bootstrap; bootstrap-only for first SaaS platform admin provisioning |
| `SESSION_COOKIE_SECURE` | `False` | `True` |
| `AUTH_PASSWORD_POLICY_ENFORCE` | Optional | `True` |
| `TRUSTED_HOSTS` | Optional | **Required** |
| `SCHEDULER_ENABLED` | `True` | `False` (use systemd timer) |

## Security Checks

On startup in production (`DEBUG=False`):

1. **SECRET_KEY validation** - App refuses to start with default dev key
2. **ADMIN_PASSWORD validation** - Legacy runtime uses it to create the initial admin user; the SaaS line provisions the first platform admin explicitly with `python -m app.saas_db --ensure-platform-admin`
3. **Security hardening validation** - Critical auth/session values must be in safe ranges
4. **TRUSTED_HOSTS validation** - Must be set to at least one hostname

## Example Production .env

```bash
# Twilio (required)
# In SaaS mode, use the parent/master Twilio account here.
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Legacy single-tenant sender. Optional for SaaS platform-managed tenant testing.
TWILIO_FROM_NUMBER=+18005551234

# Flask (required)
SECRET_KEY=your-256-bit-random-hex-key
FLASK_ENV=production
# Reverse proxy (optional; set to 1 only behind a trusted proxy)
TRUST_PROXY=1
TRUSTED_HOSTS=sms.example.com

# Admin bootstrap
# Legacy runtime uses these directly. SaaS uses them for first-time
# platform-admin provisioning via `python -m app.saas_db --ensure-platform-admin`.
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

# SaaS platform runtime
# Required when running the SaaS control plane
SAAS_MODE=1
SAAS_BASE_URL=https://beta.example.com
TWILIO_API_KEY_SID=SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_API_KEY_SECRET=replace_me
TWILIO_CREDENTIAL_ENCRYPTION_KEY=REPLACE_WITH_VALID_FERNET_KEY
TWILIO_A2P_ONBOARDING_ENABLED=1
# Must be the primary Trust Hub customer-profile bundle, not an address/supporting-doc bundle
TWILIO_PRIMARY_CUSTOMER_PROFILE_SID=BUxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Database (optional, defaults work)
DATABASE_URL=sqlite:///instance/sms.db

# Redis (required for background jobs)
REDIS_URL=redis://localhost:6379/0
RQ_QUEUE_NAME=sms

# Optional SaaS-only platform restart control
PLATFORM_SERVICE_RESTART_ENABLED=0
PLATFORM_SERVICE_RESTART_SCRIPT=/usr/local/bin/restart-sms-saas-services

# Scheduler (disabled in prod, use systemd timer)
SCHEDULER_ENABLED=0

# Timezone
APP_TIMEZONE=America/Denver
```

## Example Development .env

```bash
# Twilio (required for actual SMS sending)
# In SaaS mode, use the parent/master Twilio account here.
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

# Optional SaaS settings for local platform-managed Twilio testing
# SAAS_MODE=1
# SAAS_BASE_URL=http://127.0.0.1:5000
# TWILIO_CREDENTIAL_ENCRYPTION_KEY=REPLACE_WITH_VALID_FERNET_KEY
# TWILIO_A2P_ONBOARDING_ENABLED=1
# TWILIO_PRIMARY_CUSTOMER_PROFILE_SID=BUxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

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
