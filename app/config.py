import os
from datetime import timedelta
from pathlib import Path


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default) == '1'


def _env_int(name: str, default: str) -> int:
    raw_value = os.environ.get(name, default)
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer value, got {raw_value!r}.") from exc


def _env_csv(name: str, default: str = '') -> list[str]:
    raw_value = os.environ.get(name, default)
    return [part.strip() for part in raw_value.split(',') if part.strip()]


class Config:
    # Flask
    # This secret signs login cookies. Use a random value in production.
    # If this is weak or shared, attackers can forge sessions.
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

    # Debug should only be enabled for local development.
    # Leaving debug on in production can expose sensitive internals.
    DEBUG = os.environ.get('FLASK_DEBUG') == '1' or os.environ.get('FLASK_ENV') == 'development'
    # Set this to 1 only when traffic comes through your own reverse proxy.
    # If enabled on public traffic, client IP and scheme can be spoofed.
    TRUST_PROXY = _env_bool('TRUST_PROXY', '0')

    # Session Security
    # Keep session cookies inaccessible to browser JavaScript.
    # Turning this off increases account takeover risk from injected scripts.
    SESSION_COOKIE_HTTPONLY = True
    # Recommended: Lax for admin apps. Strict is also valid if your UX allows it.
    # A weaker setting can make cross-site request abuse easier.
    SESSION_COOKIE_SAMESITE = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax')
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

    # Proxy / Host
    # Set your allowed production hostnames (comma-separated), e.g. sms.example.com.
    # Leaving this empty in production can allow unsafe Host header usage.
    TRUSTED_HOSTS = _env_csv('TRUSTED_HOSTS', '')

    # Scheduler (disable by default in production; run as a separate service)
    SCHEDULER_ENABLED = _env_bool('SCHEDULER_ENABLED', '1' if DEBUG else '0')
    SCHEDULED_MESSAGE_MAX_LAG = _env_int('SCHEDULED_MESSAGE_MAX_LAG', '1440')

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
    if str(SQLALCHEMY_DATABASE_URI).startswith('sqlite'):
        SQLALCHEMY_ENGINE_OPTIONS['connect_args'] = {
            'timeout': _env_int('SQLITE_TIMEOUT', '30'),
        }
    
    # Twilio
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
    TWILIO_FROM_NUMBER = os.environ.get('TWILIO_FROM_NUMBER')
    TWILIO_VALIDATE_INBOUND_SIGNATURE = os.environ.get('TWILIO_VALIDATE_INBOUND_SIGNATURE', '1') == '1'
    INBOUND_AUTO_REPLY_ENABLED = os.environ.get('INBOUND_AUTO_REPLY_ENABLED', '1') == '1'
    
    # Admin test phone for testing messages before full blast
    ADMIN_TEST_PHONE = os.environ.get('ADMIN_TEST_PHONE')
    
    # Admin login credentials
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
