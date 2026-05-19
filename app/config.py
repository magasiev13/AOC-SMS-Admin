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
    TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN = os.environ.get('TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN')
    TWILIO_ALLOW_LIVE_SENDS_IN_TESTING = _env_bool('TWILIO_ALLOW_LIVE_SENDS_IN_TESTING', '0')
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
    BILLING_ACTIVATION_FEE_USD = os.environ.get('BILLING_ACTIVATION_FEE_USD', '150.00').strip() or '150.00'
    BILLING_ANNUAL_ONLY_ORG_SLUGS = os.environ.get('BILLING_ANNUAL_ONLY_ORG_SLUGS', '').strip()
    BILLING_ANNUAL_ONLY_ORG_IDS = os.environ.get('BILLING_ANNUAL_ONLY_ORG_IDS', '').strip()
    STRIPE_FAKE_CHECKOUT_ENABLED = _env_bool('STRIPE_FAKE_CHECKOUT_ENABLED', '0')
    PLAYWRIGHT_ARTIFACT_DIR = os.environ.get('PLAYWRIGHT_ARTIFACT_DIR', '').strip()

    # Stripe / billing
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
    STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
    STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
    STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID')
    STRIPE_MONTHLY_PRICE_ID = os.environ.get('STRIPE_MONTHLY_PRICE_ID') or STRIPE_PRICE_ID
    STRIPE_ANNUAL_PRICE_ID = os.environ.get('STRIPE_ANNUAL_PRICE_ID')
    STRIPE_ACTIVATION_PRICE_ID = os.environ.get('STRIPE_ACTIVATION_PRICE_ID')
    STRIPE_GROWTH_PRICE_ID = os.environ.get('STRIPE_GROWTH_PRICE_ID')
    STRIPE_SCALE_PRICE_ID = os.environ.get('STRIPE_SCALE_PRICE_ID')
    
    # Admin login credentials
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
