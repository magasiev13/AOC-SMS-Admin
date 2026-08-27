import os
from datetime import timedelta
from pathlib import Path


def _env_bool(name: str, default: str) -> bool:
    raw_value = str(os.environ.get(name, default)).strip().lower()
    if raw_value in {'1', 'true', 't', 'yes', 'y', 'on'}:
        return True
    if raw_value in {'0', 'false', 'f', 'no', 'n', 'off'}:
        return False
    raise RuntimeError(
        f"{name} must be a boolean value (1/0, true/false, yes/no, on/off), got {raw_value!r}."
    )


def _env_int(name: str, default: str) -> int:
    raw_value = os.environ.get(name, default)
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer value, got {raw_value!r}.") from exc


def _env_csv(name: str, default: str = '') -> list[str]:
    raw_value = os.environ.get(name, default)
    return [part.strip() for part in raw_value.split(',') if part.strip()]


DEFAULT_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "font-src 'self' data: https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "form-action 'self'"
)


def _env_cookie_samesite(name: str, default: str = 'Lax') -> str:
    raw_value = str(os.environ.get(name, default)).strip()
    normalized = raw_value.lower()
    allowed_values = {
        'lax': 'Lax',
        'strict': 'Strict',
        'none': 'None',
    }
    if normalized in allowed_values:
        return allowed_values[normalized]
    raise RuntimeError(
        f"{name} must be one of Lax, Strict, or None, got {raw_value!r}."
    )


class Config:
    # Flask
    # This secret signs login cookies. Use a random value in production.
    # If this is weak or shared, attackers can forge sessions.
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    PRODUCT_NAME = os.environ.get('PRODUCT_NAME', 'Twinevia').strip() or 'Twinevia'
    PRODUCT_DESCRIPTOR = os.environ.get('PRODUCT_DESCRIPTOR', 'Messaging Workspace').strip() or 'Messaging Workspace'

    # Debug should only be enabled for local development.
    # Leaving debug on in production can expose sensitive internals.
    DEBUG = os.environ.get('FLASK_DEBUG') == '1' or os.environ.get('FLASK_ENV') == 'development'
    # Set this to 1 only when traffic comes through your own reverse proxy.
    # If enabled on public traffic, client IP and scheme can be spoofed.
    TRUST_PROXY = _env_bool('TRUST_PROXY', '0')
    SAAS_MODE = _env_bool('SAAS_MODE', '0')
    SAAS_BASE_URL = os.environ.get('SAAS_BASE_URL', '')
    PUBLIC_BASE_URL = (os.environ.get('PUBLIC_BASE_URL') or SAAS_BASE_URL).strip().rstrip('/')
    APP_BASE_URL = (os.environ.get('APP_BASE_URL') or SAAS_BASE_URL).strip().rstrip('/')
    APP_RELEASE_ID = os.environ.get('APP_RELEASE_ID', '').strip()
    MANAGED_PILOT_ENABLED = _env_bool('MANAGED_PILOT_ENABLED', '1')
    PILOT_APPLICATION_RATE_LIMIT_COUNT = _env_int('PILOT_APPLICATION_RATE_LIMIT_COUNT', '5')
    PILOT_APPLICATION_RATE_LIMIT_WINDOW_SECONDS = _env_int(
        'PILOT_APPLICATION_RATE_LIMIT_WINDOW_SECONDS',
        '3600',
    )
    CUSTOMER_POLICY_VERSION = (
        os.environ.get('CUSTOMER_POLICY_VERSION', '2026-08-18-managed-pilot-v1').strip()
        or '2026-08-18-managed-pilot-v1'
    )
    TERMS_POLICY_VERSION = (
        os.environ.get('TERMS_POLICY_VERSION', CUSTOMER_POLICY_VERSION).strip()
        or CUSTOMER_POLICY_VERSION
    )
    PRIVACY_POLICY_VERSION = (
        os.environ.get('PRIVACY_POLICY_VERSION', CUSTOMER_POLICY_VERSION).strip()
        or CUSTOMER_POLICY_VERSION
    )
    ACCEPTABLE_USE_POLICY_VERSION = (
        os.environ.get('ACCEPTABLE_USE_POLICY_VERSION', CUSTOMER_POLICY_VERSION).strip()
        or CUSTOMER_POLICY_VERSION
    )
    SMS_POLICY_VERSION = (
        os.environ.get('SMS_POLICY_VERSION', CUSTOMER_POLICY_VERSION).strip()
        or CUSTOMER_POLICY_VERSION
    )
    BILLING_POLICY_VERSION = (
        os.environ.get('BILLING_POLICY_VERSION', CUSTOMER_POLICY_VERSION).strip()
        or CUSTOMER_POLICY_VERSION
    )
    PLATFORM_SERVICE_RESTART_ENABLED = _env_bool('PLATFORM_SERVICE_RESTART_ENABLED', '0')
    PLATFORM_SERVICE_RESTART_SCRIPT = os.environ.get(
        'PLATFORM_SERVICE_RESTART_SCRIPT',
        '/usr/local/bin/restart-twinevia-saas-services',
    )
    PLATFORM_SERVICE_RESTART_TIMEOUT_SECONDS = _env_int('PLATFORM_SERVICE_RESTART_TIMEOUT_SECONDS', '15')
    PLATFORM_SERVICE_RESTART_STALE_AFTER_SECONDS = _env_int('PLATFORM_SERVICE_RESTART_STALE_AFTER_SECONDS', '300')

    # Session Security
    # Keep session cookies inaccessible to browser JavaScript.
    # Turning this off increases account takeover risk from injected scripts.
    SESSION_COOKIE_HTTPONLY = True
    # Recommended: Lax for admin apps. Strict is also valid if your UX allows it.
    # A weaker setting can make cross-site request abuse easier. Accepted input is case-insensitive.
    SESSION_COOKIE_SAMESITE = _env_cookie_samesite('SESSION_COOKIE_SAMESITE', 'Lax')
    # Recommended in production: 1. This ensures cookies are sent only over HTTPS.
    # Setting this to 0 in production can leak session cookies over insecure transport.
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', '1' if not DEBUG else '0')

    # Keep remember-me cookies inaccessible to browser JavaScript.
    # Turning this off increases account takeover risk from injected scripts.
    REMEMBER_COOKIE_HTTPONLY = True
    # Keep remember-me cookie policy aligned with the main session cookie.
    REMEMBER_COOKIE_SAMESITE = SESSION_COOKIE_SAMESITE
    # Recommended in production: 1, so persistent login cookies require HTTPS.
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE

    # Recommended: 30 minutes. This is idle timeout for signed-in sessions.
    # If this is too long, unattended sessions remain usable for longer.
    SESSION_IDLE_TIMEOUT_MINUTES = _env_int('SESSION_IDLE_TIMEOUT_MINUTES', '30')
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES)

    # Recommended: 7 days. This controls "Remember me" duration.
    # If this is too long, stolen persistent cookies stay valid longer.
    REMEMBER_COOKIE_DURATION_DAYS = _env_int('REMEMBER_COOKIE_DURATION_DAYS', '7')
    REMEMBER_COOKIE_DURATION = timedelta(days=REMEMBER_COOKIE_DURATION_DAYS)

    # Login Hardening
    # Recommended: 300 seconds (5 minutes) to measure failed login bursts.
    # Too large means old failures keep counting for too long.
    AUTH_ATTEMPT_WINDOW_SECONDS = _env_int('AUTH_ATTEMPT_WINDOW_SECONDS', '300')
    # Recommended: 900 seconds (15 minutes) lockout after repeated failures.
    # Too short weakens brute-force protection; too long may block real users.
    AUTH_LOCKOUT_SECONDS = _env_int('AUTH_LOCKOUT_SECONDS', '900')
    # Recommended: 5 failures per username+IP before lockout logic triggers.
    # Too high allows rapid password guessing from one source.
    AUTH_MAX_ATTEMPTS_IP_ACCOUNT = _env_int('AUTH_MAX_ATTEMPTS_IP_ACCOUNT', '5')
    # Recommended: 8 failures per username across all IPs.
    # Too high allows distributed attacks against one account.
    AUTH_MAX_ATTEMPTS_ACCOUNT = _env_int('AUTH_MAX_ATTEMPTS_ACCOUNT', '8')
    # Recommended: 30 failures per IP across all accounts.
    # Too high allows broad credential-stuffing from one host.
    AUTH_MAX_ATTEMPTS_IP = _env_int('AUTH_MAX_ATTEMPTS_IP', '30')
    # A signed, HttpOnly trusted-browser token lets a legitimate user prove
    # possession of a previously authenticated browser during an account-wide
    # distributed-guess lock. Rotating the user's session nonce invalidates it.
    AUTH_TRUSTED_BROWSER_COOKIE_NAME = (
        os.environ.get('AUTH_TRUSTED_BROWSER_COOKIE_NAME', 'twinevia_trusted_browser').strip()
        or 'twinevia_trusted_browser'
    )
    AUTH_TRUSTED_BROWSER_MAX_AGE_SECONDS = _env_int(
        'AUTH_TRUSTED_BROWSER_MAX_AGE_SECONDS',
        str(30 * 24 * 60 * 60),
    )
    AUTH_ALERT_COOLDOWN_SECONDS = _env_int('AUTH_ALERT_COOLDOWN_SECONDS', '900')

    # Password Policy
    # Recommended: minimum 12 characters for new/updated passwords.
    # A smaller minimum makes guessed passwords much easier.
    AUTH_PASSWORD_MIN_LENGTH = _env_int('AUTH_PASSWORD_MIN_LENGTH', '12')
    # Recommended in production: 1 to enforce password policy checks.
    # Setting this to 0 allows weak passwords to be created in the UI.
    AUTH_PASSWORD_POLICY_ENFORCE = _env_bool('AUTH_PASSWORD_POLICY_ENFORCE', '1')

    # Auth Event / Password Hardening
    PASSWORD_HISTORY_COUNT = _env_int('PASSWORD_HISTORY_COUNT', '3')
    AUTH_ALERTS_ENABLED = _env_bool('AUTH_ALERTS_ENABLED', '1')
    AUTH_EVENT_RETENTION_DAYS = _env_int('AUTH_EVENT_RETENTION_DAYS', '180')
    # Alias settings used by the auth security service.
    AUTH_LOCKOUT_MAX_ATTEMPTS = _env_int('AUTH_LOCKOUT_MAX_ATTEMPTS', str(AUTH_MAX_ATTEMPTS_IP_ACCOUNT))
    AUTH_LOCKOUT_WINDOW_SECONDS = _env_int('AUTH_LOCKOUT_WINDOW_SECONDS', str(AUTH_ATTEMPT_WINDOW_SECONDS))

    # Proxy / Host
    # Set your allowed production hostnames (comma-separated), e.g. sms.example.com.
    # Leaving this empty in production can allow unsafe Host header usage.
    TRUSTED_HOSTS = _env_csv('TRUSTED_HOSTS', '')

    # Request and shared-capacity limits. These are intentionally conservative for
    # the managed pilot and can be raised only through explicit configuration.
    MAX_CONTENT_LENGTH = _env_int('MAX_CONTENT_LENGTH', str(2 * 1024 * 1024))
    MAX_FORM_MEMORY_SIZE = _env_int('MAX_FORM_MEMORY_SIZE', str(256 * 1024))
    MAX_FORM_PARTS = _env_int('MAX_FORM_PARTS', '100')
    WEBHOOK_MAX_BYTES = _env_int('WEBHOOK_MAX_BYTES', str(256 * 1024))
    CSV_IMPORT_MAX_BYTES = _env_int('CSV_IMPORT_MAX_BYTES', str(1024 * 1024))
    CSV_IMPORT_MAX_ROWS = _env_int('CSV_IMPORT_MAX_ROWS', '5000')
    CSV_IMPORT_MAX_COLUMNS = _env_int('CSV_IMPORT_MAX_COLUMNS', '25')
    CSV_IMPORT_MAX_CELL_CHARS = _env_int('CSV_IMPORT_MAX_CELL_CHARS', '2000')
    CSV_EXPORT_MAX_ROWS = _env_int('CSV_EXPORT_MAX_ROWS', '25000')
    SEND_MAX_RECIPIENTS = _env_int('SEND_MAX_RECIPIENTS', '5000')
    SEND_MAX_SEGMENTS = _env_int('SEND_MAX_SEGMENTS', '15000')
    RECIPIENT_SNAPSHOT_MAX_BYTES = _env_int('RECIPIENT_SNAPSHOT_MAX_BYTES', str(1024 * 1024))
    TENANT_MAX_PROCESSING_MESSAGE_LOGS = _env_int('TENANT_MAX_PROCESSING_MESSAGE_LOGS', '5')
    SCHEDULED_MAX_PENDING_PER_ORGANIZATION = _env_int(
        'SCHEDULED_MAX_PENDING_PER_ORGANIZATION',
        '25',
    )

    # Keep browser security headers centralized and enabled for every response.
    # Turning this off removes the app-level clickjacking, MIME sniffing, and CSP controls.
    SECURITY_HEADERS_ENABLED = _env_bool('SECURITY_HEADERS_ENABLED', '1')
    # Recommended in production over HTTPS. HSTS tells browsers to keep using HTTPS.
    SECURITY_HSTS_ENABLED = _env_bool(
        'SECURITY_HSTS_ENABLED',
        '1' if os.environ.get('FLASK_ENV', '').lower() == 'production' else '0',
    )
    # Recommended: one year. This is only emitted on secure requests.
    SECURITY_HSTS_MAX_AGE = _env_int('SECURITY_HSTS_MAX_AGE', '31536000')
    # Keep cross-site referrers from leaking full workspace URLs.
    SECURITY_REFERRER_POLICY = (
        os.environ.get('SECURITY_REFERRER_POLICY', 'strict-origin-when-cross-origin').strip()
        or 'strict-origin-when-cross-origin'
    )
    # Disable high-risk browser features Twinevia does not use.
    SECURITY_PERMISSIONS_POLICY = (
        os.environ.get('SECURITY_PERMISSIONS_POLICY', 'camera=(), microphone=(), geolocation=(), payment=()').strip()
    )
    # The default policy allows the existing Bootstrap/Chart.js CDN and current inline scripts.
    SECURITY_CONTENT_SECURITY_POLICY = (
        os.environ.get('SECURITY_CONTENT_SECURITY_POLICY', DEFAULT_CONTENT_SECURITY_POLICY).strip()
    )

    # Scheduler (disable by default in production; run as a separate service)
    SCHEDULER_ENABLED = _env_bool('SCHEDULER_ENABLED', '1' if DEBUG else '0')
    SCHEDULED_MESSAGE_MAX_LAG = _env_int('SCHEDULED_MESSAGE_MAX_LAG', '1440')
    SCHEDULED_PROCESSING_TIMEOUT_MINUTES = _env_int('SCHEDULED_PROCESSING_TIMEOUT_MINUTES', '10')
    SCHEDULED_SEND_MAX_RETRIES = _env_int('SCHEDULED_SEND_MAX_RETRIES', '3')
    SCHEDULED_SEND_RETRY_BACKOFF_SECONDS = _env_int('SCHEDULED_SEND_RETRY_BACKOFF_SECONDS', '60')
    SCHEDULED_SEND_RETRY_MAX_BACKOFF_SECONDS = _env_int(
        'SCHEDULED_SEND_RETRY_MAX_BACKOFF_SECONDS',
        '900',
    )

    APP_TIMEZONE = os.environ.get('APP_TIMEZONE', 'UTC')

    # Redis / RQ
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    RQ_QUEUE_NAME = os.environ.get('RQ_QUEUE_NAME', 'twinevia-saas' if SAAS_MODE else 'sms')
    READINESS_TOKEN = os.environ.get('READINESS_TOKEN', '').strip()
    READINESS_WORKER_MAX_AGE_SECONDS = _env_int('READINESS_WORKER_MAX_AGE_SECONDS', '120')
    READINESS_SYSTEMCTL_TIMEOUT_SECONDS = _env_int('READINESS_SYSTEMCTL_TIMEOUT_SECONDS', '5')
    READINESS_REQUIRED_SYSTEMD_TIMERS = os.environ.get(
        'READINESS_REQUIRED_SYSTEMD_TIMERS',
        ','.join(
            (
                'twinevia-saas-scheduler.timer',
                'twinevia-saas-billing-reconcile.timer',
                'twinevia-saas-a2p-reconcile.timer',
                'twinevia-saas-backup.timer',
                'twinevia-saas-readiness.timer',
            )
        ),
    ).strip()
    ALERT_WEBHOOK_URL = os.environ.get('ALERT_WEBHOOK_URL', '').strip()
    UPTIME_MONITOR_HEARTBEAT_URL = os.environ.get('UPTIME_MONITOR_HEARTBEAT_URL', '').strip()
    OPERATIONS_MONITORING_MODE = os.environ.get(
        'OPERATIONS_MONITORING_MODE',
        'webhook',
    ).strip().lower()
    OPERATIONS_GITHUB_REPOSITORY = os.environ.get(
        'OPERATIONS_GITHUB_REPOSITORY',
        '',
    ).strip()

    # Encrypted, off-host PostgreSQL backup controls.
    BACKUP_LOCAL_DIR = os.environ.get('BACKUP_LOCAL_DIR', '/var/backups/twinevia-saas').strip()
    BACKUP_OFFSITE_MODE = os.environ.get('BACKUP_OFFSITE_MODE', 'mounted').strip().lower()
    BACKUP_OFFSITE_DESTINATION = os.environ.get('BACKUP_OFFSITE_DESTINATION', '').strip()
    BACKUP_ENCRYPTION_PASSPHRASE_FILE = os.environ.get(
        'BACKUP_ENCRYPTION_PASSPHRASE_FILE',
        '',
    ).strip()
    BACKUP_RETENTION_DAYS = _env_int('BACKUP_RETENTION_DAYS', '35')
    BACKUP_STATUS_FILE = os.environ.get(
        'BACKUP_STATUS_FILE',
        '/var/lib/twinevia-saas/backup-status.json',
    ).strip()
    BACKUP_MAX_AGE_HOURS = _env_int('BACKUP_MAX_AGE_HOURS', '30')
    RESTORE_DRILL_STATUS_FILE = os.environ.get(
        'RESTORE_DRILL_STATUS_FILE',
        '/var/lib/twinevia-saas/restore-drill-status.json',
    ).strip()
    RESTORE_DRILL_DATABASE_URL = os.environ.get('RESTORE_DRILL_DATABASE_URL', '').strip()
    RESTORE_DRILL_DATABASE_NAME = os.environ.get('RESTORE_DRILL_DATABASE_NAME', '').strip()
    RESTORE_DRILL_MAX_AGE_DAYS = _env_int('RESTORE_DRILL_MAX_AGE_DAYS', '90')
    
    # Database
    BASE_DIR = Path(__file__).resolve().parent.parent
    DEFAULT_SQLITE_DB_NAME = 'twinevia.db' if SAAS_MODE else 'sms.db'
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        f"sqlite:///{BASE_DIR / 'instance' / DEFAULT_SQLITE_DB_NAME}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
    }
    if str(SQLALCHEMY_DATABASE_URI).startswith('sqlite'):
        SQLALCHEMY_ENGINE_OPTIONS['connect_args'] = {
            'timeout': _env_int('SQLITE_TIMEOUT', '30'),
        }
    
    # Twilio
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
    TWILIO_API_KEY_SID = os.environ.get('TWILIO_API_KEY_SID')
    TWILIO_API_KEY_SECRET = os.environ.get('TWILIO_API_KEY_SECRET')
    TWILIO_FROM_NUMBER = os.environ.get('TWILIO_FROM_NUMBER')
    TWILIO_PLATFORM_FRIENDLY_NAME = os.environ.get('TWILIO_PLATFORM_FRIENDLY_NAME', PRODUCT_NAME)
    TWILIO_CREDENTIAL_ENCRYPTION_KEY = os.environ.get('TWILIO_CREDENTIAL_ENCRYPTION_KEY')
    TWILIO_VALIDATE_INBOUND_SIGNATURE = _env_bool('TWILIO_VALIDATE_INBOUND_SIGNATURE', '1')
    TWILIO_A2P_ONBOARDING_ENABLED = _env_bool('TWILIO_A2P_ONBOARDING_ENABLED', '0')
    TWILIO_PRIMARY_CUSTOMER_PROFILE_SID = os.environ.get('TWILIO_PRIMARY_CUSTOMER_PROFILE_SID')
    TWILIO_A2P_NUMBER_COUNTRY = (os.environ.get('TWILIO_A2P_NUMBER_COUNTRY') or 'US').strip().upper() or 'US'
    TWILIO_A2P_FAKE_QUEUE = _env_bool('TWILIO_A2P_FAKE_QUEUE', '0')
    TWILIO_BROWSER_FAKE_SENDS = _env_bool('TWILIO_BROWSER_FAKE_SENDS', '0')
    TWILIO_A2P_EVENT_STREAMS_ENABLED = _env_bool('TWILIO_A2P_EVENT_STREAMS_ENABLED', '0')
    TWILIO_A2P_URL_VALIDATION_TIMEOUT = _env_int('TWILIO_A2P_URL_VALIDATION_TIMEOUT', '5')
    TWILIO_A2P_URL_VALIDATION_MAX_BYTES = _env_int(
        'TWILIO_A2P_URL_VALIDATION_MAX_BYTES',
        str(128 * 1024),
    )
    TWILIO_A2P_URL_VALIDATION_MAX_REDIRECTS = _env_int(
        'TWILIO_A2P_URL_VALIDATION_MAX_REDIRECTS',
        '3',
    )
    TWILIO_ALLOW_LIVE_SENDS_IN_TESTING = _env_bool('TWILIO_ALLOW_LIVE_SENDS_IN_TESTING', '0')
    AOC_EVENTS_WEBHOOK_ENABLED = _env_bool('AOC_EVENTS_WEBHOOK_ENABLED', '0')
    AOC_EVENTS_WEBHOOK_SECRET = os.environ.get('AOC_EVENTS_WEBHOOK_SECRET', '').strip()
    AOC_EVENTS_WEBHOOK_TOLERANCE_SECONDS = _env_int('AOC_EVENTS_WEBHOOK_TOLERANCE_SECONDS', '300')
    AOC_EVENTS_ORGANIZATION_SLUG = os.environ.get('AOC_EVENTS_ORGANIZATION_SLUG', 'armenians-of-colorado').strip()
    AOC_SCHEDULED_CANCELLATION_RECORD_FILE = os.environ.get(
        'AOC_SCHEDULED_CANCELLATION_RECORD_FILE',
        '',
    ).strip()
    INBOUND_AUTO_REPLY_ENABLED = _env_bool('INBOUND_AUTO_REPLY_ENABLED', '1')
    SURVEY_AMBIGUOUS_DUPLICATE_WINDOW_SECONDS = _env_int('SURVEY_AMBIGUOUS_DUPLICATE_WINDOW_SECONDS', '3')
    BILLING_TRIAL_DAYS = _env_int('BILLING_TRIAL_DAYS', '0')
    BILLING_INCLUDED_OUTBOUND_SEGMENTS = _env_int('BILLING_INCLUDED_OUTBOUND_SEGMENTS', '1000')
    BILLING_STARTER_INCLUDED_OUTBOUND_SEGMENTS = _env_int(
        'BILLING_STARTER_INCLUDED_OUTBOUND_SEGMENTS',
        os.environ.get('BILLING_INCLUDED_OUTBOUND_SEGMENTS', '1000'),
    )
    BILLING_GROWTH_INCLUDED_OUTBOUND_SEGMENTS = _env_int('BILLING_GROWTH_INCLUDED_OUTBOUND_SEGMENTS', '3000')
    BILLING_SCALE_INCLUDED_OUTBOUND_SEGMENTS = _env_int('BILLING_SCALE_INCLUDED_OUTBOUND_SEGMENTS', '10000')
    BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS = _env_int(
        'BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS',
        os.environ.get('BILLING_STARTER_INCLUDED_OUTBOUND_SEGMENTS', os.environ.get('BILLING_INCLUDED_OUTBOUND_SEGMENTS', '1000')),
    )
    BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS = _env_int(
        'BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS',
        os.environ.get('BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS', os.environ.get('BILLING_INCLUDED_OUTBOUND_SEGMENTS', '1000')),
    )
    BILLING_USAGE_CURRENCY = os.environ.get('BILLING_USAGE_CURRENCY', 'usd').strip().lower() or 'usd'
    BILLING_OUTBOUND_SEGMENT_RATE_USD = os.environ.get('BILLING_OUTBOUND_SEGMENT_RATE_USD', '0.0300').strip() or '0.0300'
    BILLING_MONTHLY_PRICE_USD = os.environ.get('BILLING_MONTHLY_PRICE_USD', '59.99').strip() or '59.99'
    BILLING_ANNUAL_PRICE_USD = os.environ.get('BILLING_ANNUAL_PRICE_USD', '600.00').strip() or '600.00'
    BILLING_ACTIVATION_FEE_USD = os.environ.get('BILLING_ACTIVATION_FEE_USD', '149.00').strip() or '149.00'
    BILLING_STAGED_ACTIVATION_FEE_USD = (
        os.environ.get('BILLING_STAGED_ACTIVATION_FEE_USD', '150.00').strip()
        or '150.00'
    )
    BILLING_OFFER_VERSION = (
        os.environ.get('BILLING_OFFER_VERSION', '2026-08-managed-pilot-v1').strip()
        or '2026-08-managed-pilot-v1'
    )
    BILLING_USAGE_SETTLEMENT_GRACE_HOURS = _env_int(
        'BILLING_USAGE_SETTLEMENT_GRACE_HOURS',
        '72',
    )
    BILLING_ANNUAL_ONLY_ORG_SLUGS = os.environ.get('BILLING_ANNUAL_ONLY_ORG_SLUGS', '').strip()
    BILLING_ANNUAL_ONLY_ORG_IDS = os.environ.get('BILLING_ANNUAL_ONLY_ORG_IDS', '').strip()
    STRIPE_FAKE_CHECKOUT_ENABLED = _env_bool('STRIPE_FAKE_CHECKOUT_ENABLED', '0')
    PLAYWRIGHT_ARTIFACT_DIR = os.environ.get('PLAYWRIGHT_ARTIFACT_DIR', '').strip()

    # Stripe / billing
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
    STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
    STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
    STRIPE_WEBHOOK_ENDPOINT_ID = os.environ.get('STRIPE_WEBHOOK_ENDPOINT_ID')
    STRIPE_PORTAL_CONFIGURATION_ID = os.environ.get('STRIPE_PORTAL_CONFIGURATION_ID')
    STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID')
    STRIPE_MONTHLY_PRICE_ID = os.environ.get('STRIPE_MONTHLY_PRICE_ID') or STRIPE_PRICE_ID
    STRIPE_ANNUAL_PRICE_ID = os.environ.get('STRIPE_ANNUAL_PRICE_ID')
    STRIPE_ACTIVATION_PRICE_ID = os.environ.get('STRIPE_ACTIVATION_PRICE_ID')
    STRIPE_STAGED_ACTIVATION_PRICE_ID = os.environ.get('STRIPE_STAGED_ACTIVATION_PRICE_ID')
    STRIPE_GROWTH_PRICE_ID = os.environ.get('STRIPE_GROWTH_PRICE_ID')
    STRIPE_SCALE_PRICE_ID = os.environ.get('STRIPE_SCALE_PRICE_ID')
    STRIPE_EXPECTED_ACCOUNT_ID = (
        os.environ.get('STRIPE_EXPECTED_ACCOUNT_ID', 'acct_1TCY8xEksbf3Q3Fg').strip()
        or 'acct_1TCY8xEksbf3Q3Fg'
    )
    STRIPE_EXPECTED_ACTIVATION_PRICE_ID = (
        os.environ.get('STRIPE_EXPECTED_ACTIVATION_PRICE_ID', 'price_1TPq4KEksbf3Q3FgwATaTJ7h').strip()
        or 'price_1TPq4KEksbf3Q3FgwATaTJ7h'
    )
    STRIPE_EXPECTED_STAGED_ACTIVATION_PRICE_ID = os.environ.get(
        'STRIPE_EXPECTED_STAGED_ACTIVATION_PRICE_ID',
        '',
    ).strip()
    STRIPE_EXPECTED_MONTHLY_PRICE_ID = (
        os.environ.get('STRIPE_EXPECTED_MONTHLY_PRICE_ID', 'price_1TYtNuEksbf3Q3FgN2B1VqGN').strip()
        or 'price_1TYtNuEksbf3Q3FgN2B1VqGN'
    )
    STRIPE_EXPECTED_ANNUAL_PRICE_ID = (
        os.environ.get('STRIPE_EXPECTED_ANNUAL_PRICE_ID', 'price_1TYtO4Eksbf3Q3FgHzXB9S5b').strip()
        or 'price_1TYtO4Eksbf3Q3FgHzXB9S5b'
    )
    STRIPE_LIVE_CONFIGURATION_REQUIRED = _env_bool(
        'STRIPE_LIVE_CONFIGURATION_REQUIRED',
        '1' if os.environ.get('FLASK_ENV', '').lower() == 'production' else '0',
    )
    STRIPE_CONFIGURATION_VALIDATION_ATTEMPTS = _env_int(
        'STRIPE_CONFIGURATION_VALIDATION_ATTEMPTS',
        '3',
    )
    
    # Admin login credentials
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
