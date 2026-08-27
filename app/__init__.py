import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from flask_wtf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

db = SQLAlchemy()
csrf = CSRFProtect()


def _validate_production_security_config(app: Flask) -> None:
    errors: list[str] = []

    def expect_int_range(name: str, minimum: int, maximum: int) -> None:
        value = app.config.get(name)
        if not isinstance(value, int):
            errors.append(f"{name} must be an integer.")
            return
        if value < minimum or value > maximum:
            errors.append(f"{name} must be between {minimum} and {maximum}. Current value: {value}.")

    if app.config.get("SESSION_COOKIE_SECURE") is not True:
        errors.append("SESSION_COOKIE_SECURE must be enabled (1) in production.")
    if app.config.get("REMEMBER_COOKIE_SECURE") is not True:
        errors.append("REMEMBER_COOKIE_SECURE must be enabled (1) in production.")
    if app.config.get("SESSION_COOKIE_HTTPONLY") is not True:
        errors.append("SESSION_COOKIE_HTTPONLY must remain enabled.")
    if app.config.get("REMEMBER_COOKIE_HTTPONLY") is not True:
        errors.append("REMEMBER_COOKIE_HTTPONLY must remain enabled.")
    if app.config.get("SESSION_COOKIE_SAMESITE") not in {"Lax", "Strict"}:
        errors.append("SESSION_COOKIE_SAMESITE must be Lax or Strict in production.")

    expect_int_range("AUTH_ATTEMPT_WINDOW_SECONDS", 60, 86400)
    expect_int_range("AUTH_LOCKOUT_SECONDS", 60, 86400)
    expect_int_range("AUTH_MAX_ATTEMPTS_IP_ACCOUNT", 1, 100)
    expect_int_range("AUTH_MAX_ATTEMPTS_ACCOUNT", 1, 200)
    expect_int_range("AUTH_MAX_ATTEMPTS_IP", 1, 500)
    expect_int_range("AUTH_TRUSTED_BROWSER_MAX_AGE_SECONDS", 3600, 90 * 24 * 60 * 60)
    expect_int_range("AUTH_ALERT_COOLDOWN_SECONDS", 60, 86400)
    expect_int_range("SESSION_IDLE_TIMEOUT_MINUTES", 5, 1440)
    expect_int_range("REMEMBER_COOKIE_DURATION_DAYS", 1, 30)
    expect_int_range("AUTH_PASSWORD_MIN_LENGTH", 12, 128)
    expect_int_range("SECURITY_HSTS_MAX_AGE", 300, 63072000)
    expect_int_range("MAX_CONTENT_LENGTH", 65536, 10 * 1024 * 1024)
    expect_int_range("MAX_FORM_MEMORY_SIZE", 16384, 2 * 1024 * 1024)
    expect_int_range("MAX_FORM_PARTS", 10, 500)
    expect_int_range("WEBHOOK_MAX_BYTES", 4096, 1024 * 1024)
    expect_int_range("CSV_IMPORT_MAX_BYTES", 1024, 5 * 1024 * 1024)
    expect_int_range("CSV_IMPORT_MAX_ROWS", 1, 25000)
    expect_int_range("CSV_IMPORT_MAX_COLUMNS", 1, 100)
    expect_int_range("CSV_IMPORT_MAX_CELL_CHARS", 1, 10000)
    expect_int_range("CSV_EXPORT_MAX_ROWS", 1, 100000)
    expect_int_range("SEND_MAX_RECIPIENTS", 1, 25000)
    expect_int_range("SEND_MAX_SEGMENTS", 1, 100000)
    expect_int_range("RECIPIENT_SNAPSHOT_MAX_BYTES", 4096, 5 * 1024 * 1024)
    expect_int_range("TENANT_MAX_PROCESSING_MESSAGE_LOGS", 1, 100)
    expect_int_range("SCHEDULED_MAX_PENDING_PER_ORGANIZATION", 1, 1000)

    if app.config.get("AUTH_PASSWORD_POLICY_ENFORCE") is not True:
        errors.append("AUTH_PASSWORD_POLICY_ENFORCE must be enabled (1) in production.")
    if app.config.get("TWILIO_VALIDATE_INBOUND_SIGNATURE") is not True:
        errors.append("TWILIO_VALIDATE_INBOUND_SIGNATURE must be enabled (1) in production.")
    if app.config.get("SECURITY_HEADERS_ENABLED") is not True:
        errors.append("SECURITY_HEADERS_ENABLED must be enabled (1) in production.")
    if app.config.get("SECURITY_HSTS_ENABLED") is not True:
        errors.append("SECURITY_HSTS_ENABLED must be enabled (1) in production.")
    if not str(app.config.get("SECURITY_CONTENT_SECURITY_POLICY") or "").strip():
        errors.append("SECURITY_CONTENT_SECURITY_POLICY must not be empty in production.")

    trusted_browser_cookie_name = str(
        app.config.get("AUTH_TRUSTED_BROWSER_COOKIE_NAME") or ""
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", trusted_browser_cookie_name):
        errors.append(
            "AUTH_TRUSTED_BROWSER_COOKIE_NAME must contain 1-64 letters, digits, underscores, or hyphens."
        )

    ip_account_limit = app.config.get("AUTH_MAX_ATTEMPTS_IP_ACCOUNT")
    account_limit = app.config.get("AUTH_MAX_ATTEMPTS_ACCOUNT")
    ip_limit = app.config.get("AUTH_MAX_ATTEMPTS_IP")
    if isinstance(ip_account_limit, int) and isinstance(account_limit, int):
        if ip_account_limit > account_limit:
            errors.append(
                "AUTH_MAX_ATTEMPTS_IP_ACCOUNT should not be greater than AUTH_MAX_ATTEMPTS_ACCOUNT."
            )
    if isinstance(account_limit, int) and isinstance(ip_limit, int):
        if account_limit > ip_limit:
            errors.append("AUTH_MAX_ATTEMPTS_ACCOUNT should not be greater than AUTH_MAX_ATTEMPTS_IP.")

    trusted_hosts = app.config.get("TRUSTED_HOSTS") or []
    if not trusted_hosts:
        errors.append("TRUSTED_HOSTS must include your production hostnames.")

    if errors:
        details = "\n - ".join(errors)
        raise RuntimeError(f"Production security configuration is invalid:\n - {details}")


def _validate_saas_billing_config(app: Flask) -> None:
    if not app.config.get("SAAS_MODE"):
        return

    required_values = {
        "STRIPE_SECRET_KEY": app.config.get("STRIPE_SECRET_KEY"),
        "STRIPE_WEBHOOK_SECRET": app.config.get("STRIPE_WEBHOOK_SECRET"),
        "STRIPE_ANNUAL_PRICE_ID": app.config.get("STRIPE_ANNUAL_PRICE_ID"),
        "STRIPE_ACTIVATION_PRICE_ID": app.config.get("STRIPE_ACTIVATION_PRICE_ID"),
        "PUBLIC_BASE_URL": app.config.get("PUBLIC_BASE_URL"),
        "APP_BASE_URL": app.config.get("APP_BASE_URL"),
        "TWILIO_CREDENTIAL_ENCRYPTION_KEY": app.config.get("TWILIO_CREDENTIAL_ENCRYPTION_KEY"),
    }
    missing = [name for name, value in required_values.items() if not str(value or "").strip()]
    if not str(app.config.get("STRIPE_MONTHLY_PRICE_ID") or app.config.get("STRIPE_PRICE_ID") or "").strip():
        missing.append("STRIPE_MONTHLY_PRICE_ID or STRIPE_PRICE_ID")
    if missing:
        details = "\n - ".join(f"{name} must be configured for SaaS billing." for name in missing)
        raise RuntimeError(f"SaaS billing configuration is invalid:\n - {details}")

    url_errors: list[str] = []
    public_https_required = (
        os.environ.get("FLASK_ENV", "").lower() == "production"
        or bool(app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED"))
    )
    for name in ("PUBLIC_BASE_URL", "APP_BASE_URL"):
        raw_value = str(app.config.get(name) or "").strip()
        parsed = urlsplit(raw_value)
        hostname = (parsed.hostname or "").lower()
        local_http_allowed = (
            not public_https_required
            and parsed.scheme == "http"
            and hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or (parsed.scheme != "https" and not local_http_allowed)
        ):
            url_errors.append(f"{name} must be an absolute public HTTPS URL without user information.")
    if url_errors:
        details = "\n - ".join(url_errors)
        raise RuntimeError(f"SaaS billing configuration is invalid:\n - {details}")

    if app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED"):
        mismatched = [
            f"{name} must be configured for live billing."
            for name in ("STRIPE_WEBHOOK_ENDPOINT_ID", "STRIPE_PORTAL_CONFIGURATION_ID")
            if not str(app.config.get(name) or "").strip()
        ]
        expected_price_ids = {
            "STRIPE_ACTIVATION_PRICE_ID": app.config.get("STRIPE_EXPECTED_ACTIVATION_PRICE_ID"),
            "STRIPE_MONTHLY_PRICE_ID": app.config.get("STRIPE_EXPECTED_MONTHLY_PRICE_ID"),
            "STRIPE_ANNUAL_PRICE_ID": app.config.get("STRIPE_EXPECTED_ANNUAL_PRICE_ID"),
        }
        staged_expected_price_id = str(
            app.config.get("STRIPE_EXPECTED_STAGED_ACTIVATION_PRICE_ID") or ""
        ).strip()
        if staged_expected_price_id:
            expected_price_ids["STRIPE_STAGED_ACTIVATION_PRICE_ID"] = staged_expected_price_id
        mismatched.extend(
            f"{name} must equal {expected_value}."
            for name, expected_value in expected_price_ids.items()
            if str(app.config.get(name) or "").strip() != str(expected_value or "").strip()
        )
        secret_key = str(app.config.get("STRIPE_SECRET_KEY") or "").strip()
        if not secret_key.startswith(("sk_live_", "rk_live_")):
            mismatched.append("STRIPE_SECRET_KEY must be a live-mode secret or restricted key.")
        webhook_secret = str(app.config.get("STRIPE_WEBHOOK_SECRET") or "").strip()
        if not webhook_secret.startswith("whsec_"):
            mismatched.append("STRIPE_WEBHOOK_SECRET must be a Stripe endpoint signing secret.")
        if mismatched:
            details = "\n - ".join(mismatched)
            raise RuntimeError(f"SaaS live billing configuration is invalid:\n - {details}")

    commercial_values = {
        "BILLING_ACTIVATION_FEE_USD": "149.00",
        "BILLING_STAGED_ACTIVATION_FEE_USD": "150.00",
        "BILLING_MONTHLY_PRICE_USD": "59.99",
        "BILLING_ANNUAL_PRICE_USD": "600.00",
        "BILLING_OUTBOUND_SEGMENT_RATE_USD": "0.0300",
    }
    commercial_errors = [
        f"{name} must equal {expected_value}."
        for name, expected_value in commercial_values.items()
        if str(app.config.get(name) or "").strip() != expected_value
    ]
    if int(app.config.get("BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS", 0) or 0) != 1000:
        commercial_errors.append("BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS must equal 1000.")
    if int(app.config.get("BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS", 0) or 0) != 1000:
        commercial_errors.append("BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS must equal 1000.")
    if commercial_errors:
        details = "\n - ".join(commercial_errors)
        raise RuntimeError(f"SaaS commercial configuration is invalid:\n - {details}")

    api_key_sid = (app.config.get("TWILIO_API_KEY_SID") or "").strip()
    api_key_secret = (app.config.get("TWILIO_API_KEY_SECRET") or "").strip()
    if bool(api_key_sid) != bool(api_key_secret):
        raise RuntimeError(
            "SaaS billing configuration is invalid:\n - TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET must be configured together."
        )

    if app.config.get("TWILIO_A2P_ONBOARDING_ENABLED"):
        primary_customer_profile_sid = (app.config.get("TWILIO_PRIMARY_CUSTOMER_PROFILE_SID") or "").strip()
        if not primary_customer_profile_sid:
            raise RuntimeError(
                "SaaS billing configuration is invalid:\n - TWILIO_PRIMARY_CUSTOMER_PROFILE_SID must be configured when TWILIO_A2P_ONBOARDING_ENABLED=1."
            )


def _validate_aoc_event_sync_config(app: Flask) -> None:
    if not app.config.get("AOC_EVENTS_WEBHOOK_ENABLED"):
        return

    errors: list[str] = []
    if not str(app.config.get("AOC_EVENTS_WEBHOOK_SECRET") or "").strip():
        errors.append("AOC_EVENTS_WEBHOOK_SECRET must be configured when AOC_EVENTS_WEBHOOK_ENABLED=1.")
    if not str(app.config.get("AOC_EVENTS_ORGANIZATION_SLUG") or "").strip():
        errors.append("AOC_EVENTS_ORGANIZATION_SLUG must be configured when AOC_EVENTS_WEBHOOK_ENABLED=1.")
    tolerance_seconds = app.config.get("AOC_EVENTS_WEBHOOK_TOLERANCE_SECONDS")
    if not isinstance(tolerance_seconds, int) or tolerance_seconds < 60 or tolerance_seconds > 3600:
        errors.append("AOC_EVENTS_WEBHOOK_TOLERANCE_SECONDS must be between 60 and 3600.")

    if errors:
        details = "\n - ".join(errors)
        raise RuntimeError(f"AOC event sync configuration is invalid:\n - {details}")


def _validate_explicit_production_runtime(app: Flask) -> None:
    if os.environ.get("FLASK_ENV", "").lower() != "production":
        return

    errors: list[str] = []
    if app.config.get("DEBUG"):
        errors.append("FLASK_DEBUG must be unset or 0 when FLASK_ENV=production.")

    if app.config.get("SAAS_MODE"):
        database_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip().lower()
        if not database_uri.startswith("postgresql"):
            errors.append("Production SaaS requires PostgreSQL DATABASE_URL; SQLite is not supported for live deploys.")

        if app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED") is not True:
            errors.append(
                "STRIPE_LIVE_CONFIGURATION_REQUIRED must be enabled (1) when FLASK_ENV=production."
            )

        for flag_name in (
            "STRIPE_FAKE_CHECKOUT_ENABLED",
            "TWILIO_BROWSER_FAKE_SENDS",
            "TWILIO_A2P_FAKE_QUEUE",
        ):
            if app.config.get(flag_name):
                errors.append(f"{flag_name} must be disabled (0) when FLASK_ENV=production.")

    if errors:
        details = "\n - ".join(errors)
        raise RuntimeError(f"Production runtime configuration is invalid:\n - {details}")


def _validate_production_operations_config(app: Flask) -> None:
    if os.environ.get("FLASK_ENV", "").lower() != "production" or not app.config.get("SAAS_MODE"):
        return

    errors: list[str] = []
    explicit_environment_names = (
        "PUBLIC_BASE_URL",
        "APP_BASE_URL",
        "MANAGED_PILOT_ENABLED",
        "PILOT_APPLICATION_RATE_LIMIT_COUNT",
        "PILOT_APPLICATION_RATE_LIMIT_WINDOW_SECONDS",
        "CUSTOMER_POLICY_VERSION",
        "TERMS_POLICY_VERSION",
        "PRIVACY_POLICY_VERSION",
        "ACCEPTABLE_USE_POLICY_VERSION",
        "SMS_POLICY_VERSION",
        "BILLING_POLICY_VERSION",
        "MAX_CONTENT_LENGTH",
        "MAX_FORM_MEMORY_SIZE",
        "MAX_FORM_PARTS",
        "WEBHOOK_MAX_BYTES",
        "CSV_IMPORT_MAX_BYTES",
        "CSV_IMPORT_MAX_ROWS",
        "CSV_IMPORT_MAX_COLUMNS",
        "CSV_IMPORT_MAX_CELL_CHARS",
        "CSV_EXPORT_MAX_ROWS",
        "SEND_MAX_RECIPIENTS",
        "SEND_MAX_SEGMENTS",
        "RECIPIENT_SNAPSHOT_MAX_BYTES",
        "TENANT_MAX_PROCESSING_MESSAGE_LOGS",
        "SCHEDULED_MAX_PENDING_PER_ORGANIZATION",
        "STRIPE_EXPECTED_ACCOUNT_ID",
        "STRIPE_ACTIVATION_PRICE_ID",
        "STRIPE_STAGED_ACTIVATION_PRICE_ID",
        "STRIPE_MONTHLY_PRICE_ID",
        "STRIPE_ANNUAL_PRICE_ID",
        "STRIPE_WEBHOOK_ENDPOINT_ID",
        "STRIPE_PORTAL_CONFIGURATION_ID",
        "BILLING_OFFER_VERSION",
        "BILLING_ACTIVATION_FEE_USD",
        "BILLING_STAGED_ACTIVATION_FEE_USD",
        "BILLING_MONTHLY_PRICE_USD",
        "BILLING_ANNUAL_PRICE_USD",
        "BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS",
        "BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS",
        "BILLING_OUTBOUND_SEGMENT_RATE_USD",
        "READINESS_TOKEN",
        "READINESS_WORKER_MAX_AGE_SECONDS",
        "READINESS_SYSTEMCTL_TIMEOUT_SECONDS",
        "READINESS_REQUIRED_SYSTEMD_TIMERS",
        "OPERATIONS_MONITORING_MODE",
        "BACKUP_LOCAL_DIR",
        "BACKUP_OFFSITE_MODE",
        "BACKUP_ENCRYPTION_PASSPHRASE_FILE",
        "BACKUP_RETENTION_DAYS",
        "BACKUP_STATUS_FILE",
        "BACKUP_MAX_AGE_HOURS",
        "RESTORE_DRILL_STATUS_FILE",
        "RESTORE_DRILL_DATABASE_URL",
        "RESTORE_DRILL_DATABASE_NAME",
        "RESTORE_DRILL_MAX_AGE_DAYS",
        "AOC_SCHEDULED_CANCELLATION_RECORD_FILE",
    )
    missing_explicit_names = [
        name
        for name in explicit_environment_names
        if not str(os.environ.get(name) or "").strip()
    ]
    if missing_explicit_names:
        errors.append(
            "Production launch configuration must explicitly define: "
            + ", ".join(missing_explicit_names)
            + "."
        )
    required_values = {
        "APP_RELEASE_ID": app.config.get("APP_RELEASE_ID"),
        "READINESS_TOKEN": app.config.get("READINESS_TOKEN"),
        "OPERATIONS_MONITORING_MODE": app.config.get("OPERATIONS_MONITORING_MODE"),
        "BACKUP_LOCAL_DIR": app.config.get("BACKUP_LOCAL_DIR"),
        "BACKUP_OFFSITE_MODE": app.config.get("BACKUP_OFFSITE_MODE"),
        "BACKUP_ENCRYPTION_PASSPHRASE_FILE": app.config.get("BACKUP_ENCRYPTION_PASSPHRASE_FILE"),
        "BACKUP_STATUS_FILE": app.config.get("BACKUP_STATUS_FILE"),
        "RESTORE_DRILL_STATUS_FILE": app.config.get("RESTORE_DRILL_STATUS_FILE"),
        "RESTORE_DRILL_DATABASE_URL": app.config.get("RESTORE_DRILL_DATABASE_URL"),
        "RESTORE_DRILL_DATABASE_NAME": app.config.get("RESTORE_DRILL_DATABASE_NAME"),
        "AOC_SCHEDULED_CANCELLATION_RECORD_FILE": app.config.get("AOC_SCHEDULED_CANCELLATION_RECORD_FILE"),
    }
    errors.extend(
        f"{name} must be configured for production operations."
        for name, value in required_values.items()
        if not str(value or "").strip()
    )

    if app.config.get("MANAGED_PILOT_ENABLED") is not True:
        errors.append("MANAGED_PILOT_ENABLED must remain enabled for the managed-pilot launch.")

    release_id = str(app.config.get("APP_RELEASE_ID") or "").strip().lower()
    if release_id in {"", "dev", "unknown"}:
        errors.append("APP_RELEASE_ID must identify the immutable deployed release.")

    readiness_token = str(app.config.get("READINESS_TOKEN") or "")
    if readiness_token and len(readiness_token) < 32:
        errors.append("READINESS_TOKEN must contain at least 32 characters.")

    monitoring_mode = str(app.config.get("OPERATIONS_MONITORING_MODE") or "").strip()
    github_repository = str(app.config.get("OPERATIONS_GITHUB_REPOSITORY") or "").strip()
    if monitoring_mode not in {"github_actions", "webhook"}:
        errors.append("OPERATIONS_MONITORING_MODE must be github_actions or webhook.")
    if monitoring_mode == "github_actions" and re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        github_repository,
    ) is None:
        errors.append("OPERATIONS_GITHUB_REPOSITORY must use the owner/repository format.")
    if monitoring_mode == "webhook":
        for name in ("ALERT_WEBHOOK_URL", "UPTIME_MONITOR_HEARTBEAT_URL"):
            if not str(app.config.get(name) or "").strip():
                errors.append(f"{name} must be configured when OPERATIONS_MONITORING_MODE=webhook.")

    for name in ("ALERT_WEBHOOK_URL", "UPTIME_MONITOR_HEARTBEAT_URL"):
        raw_value = str(app.config.get(name) or "").strip()
        if raw_value:
            parsed = urlsplit(raw_value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                errors.append(f"{name} must be an absolute HTTPS URL without user information.")

    local_backup_dir = str(app.config.get("BACKUP_LOCAL_DIR") or "").strip()
    if local_backup_dir and not Path(local_backup_dir).is_absolute():
        errors.append("BACKUP_LOCAL_DIR must be an absolute path.")
    backup_offsite_mode = str(app.config.get("BACKUP_OFFSITE_MODE") or "").strip()
    if backup_offsite_mode not in {"github_actions", "mounted"}:
        errors.append("BACKUP_OFFSITE_MODE must be github_actions or mounted.")
    offsite_destination = str(app.config.get("BACKUP_OFFSITE_DESTINATION") or "").strip()
    if backup_offsite_mode == "mounted":
        if not offsite_destination:
            errors.append("BACKUP_OFFSITE_DESTINATION is required when BACKUP_OFFSITE_MODE=mounted.")
        if local_backup_dir and offsite_destination == local_backup_dir:
            errors.append("BACKUP_OFFSITE_DESTINATION must not be the local backup directory.")
        if offsite_destination and not Path(offsite_destination).is_absolute():
            errors.append("BACKUP_OFFSITE_DESTINATION must be an absolute path to an off-host mounted filesystem.")
    elif offsite_destination:
        errors.append("BACKUP_OFFSITE_DESTINATION must be empty when BACKUP_OFFSITE_MODE=github_actions.")

    passphrase_path = str(app.config.get("BACKUP_ENCRYPTION_PASSPHRASE_FILE") or "").strip()
    if passphrase_path and not Path(passphrase_path).is_absolute():
        errors.append("BACKUP_ENCRYPTION_PASSPHRASE_FILE must be an absolute path.")
    if passphrase_path and not Path(passphrase_path).is_file():
        errors.append("BACKUP_ENCRYPTION_PASSPHRASE_FILE must reference an existing readable file.")

    status_path = str(app.config.get("BACKUP_STATUS_FILE") or "").strip()
    if status_path and not Path(status_path).is_absolute():
        errors.append("BACKUP_STATUS_FILE must be an absolute path.")
    restore_status_path = str(app.config.get("RESTORE_DRILL_STATUS_FILE") or "").strip()
    if restore_status_path and not Path(restore_status_path).is_absolute():
        errors.append("RESTORE_DRILL_STATUS_FILE must be an absolute path.")
    restore_database_url = str(app.config.get("RESTORE_DRILL_DATABASE_URL") or "").strip()
    if restore_database_url:
        parsed_restore_database = urlsplit(restore_database_url)
        restore_database_name = parsed_restore_database.path.strip("/")
        if not parsed_restore_database.scheme.startswith("postgresql") or not restore_database_name:
            errors.append("RESTORE_DRILL_DATABASE_URL must reference a named PostgreSQL database.")
        if restore_database_url == str(app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip():
            errors.append("RESTORE_DRILL_DATABASE_URL must not equal the production DATABASE_URL.")
        if restore_database_name != str(app.config.get("RESTORE_DRILL_DATABASE_NAME") or "").strip():
            errors.append("RESTORE_DRILL_DATABASE_NAME must match RESTORE_DRILL_DATABASE_URL.")
    aoc_cancellation_path = str(app.config.get("AOC_SCHEDULED_CANCELLATION_RECORD_FILE") or "").strip()
    if aoc_cancellation_path and not Path(aoc_cancellation_path).is_absolute():
        errors.append("AOC_SCHEDULED_CANCELLATION_RECORD_FILE must be an absolute path.")

    retention_days = app.config.get("BACKUP_RETENTION_DAYS")
    if not isinstance(retention_days, int) or retention_days < 7 or retention_days > 365:
        errors.append("BACKUP_RETENTION_DAYS must be between 7 and 365.")
    backup_max_age = app.config.get("BACKUP_MAX_AGE_HOURS")
    if not isinstance(backup_max_age, int) or backup_max_age < 1 or backup_max_age > 168:
        errors.append("BACKUP_MAX_AGE_HOURS must be between 1 and 168.")
    restore_max_age = app.config.get("RESTORE_DRILL_MAX_AGE_DAYS")
    if not isinstance(restore_max_age, int) or restore_max_age < 1 or restore_max_age > 365:
        errors.append("RESTORE_DRILL_MAX_AGE_DAYS must be between 1 and 365.")
    worker_age = app.config.get("READINESS_WORKER_MAX_AGE_SECONDS")
    if not isinstance(worker_age, int) or worker_age < 30 or worker_age > 600:
        errors.append("READINESS_WORKER_MAX_AGE_SECONDS must be between 30 and 600.")
    systemctl_timeout = app.config.get("READINESS_SYSTEMCTL_TIMEOUT_SECONDS")
    if not isinstance(systemctl_timeout, int) or systemctl_timeout < 1 or systemctl_timeout > 30:
        errors.append("READINESS_SYSTEMCTL_TIMEOUT_SECONDS must be between 1 and 30.")
    required_timer_names = {
        "twinevia-saas-scheduler.timer",
        "twinevia-saas-billing-reconcile.timer",
        "twinevia-saas-a2p-reconcile.timer",
        "twinevia-saas-backup.timer",
        "twinevia-saas-readiness.timer",
    }
    configured_timer_names = {
        timer.strip()
        for timer in str(app.config.get("READINESS_REQUIRED_SYSTEMD_TIMERS") or "").split(",")
        if timer.strip()
    }
    missing_timer_names = sorted(required_timer_names - configured_timer_names)
    if missing_timer_names:
        errors.append(
            "READINESS_REQUIRED_SYSTEMD_TIMERS must include "
            + ", ".join(missing_timer_names)
            + "."
        )

    public_host = (urlsplit(str(app.config.get("PUBLIC_BASE_URL") or "")).hostname or "").lower()
    app_host = (urlsplit(str(app.config.get("APP_BASE_URL") or "")).hostname or "").lower()
    if public_host != "twinevia.com":
        errors.append("PUBLIC_BASE_URL must use https://twinevia.com for the managed-pilot launch.")
    if app_host != "app.twinevia.com":
        errors.append("APP_BASE_URL must use https://app.twinevia.com for the managed-pilot launch.")
    trusted_hosts = {str(host).strip().lower() for host in app.config.get("TRUSTED_HOSTS", [])}
    required_hosts = {"twinevia.com", "www.twinevia.com", "app.twinevia.com"}
    missing_hosts = sorted(required_hosts - trusted_hosts)
    if missing_hosts:
        errors.append("TRUSTED_HOSTS must include " + ", ".join(missing_hosts) + ".")

    if errors:
        details = "\n - ".join(errors)
        raise RuntimeError(f"Production operations configuration is invalid:\n - {details}")


def _configured_host_redirect_url(base_url: str, path: str) -> str | None:
    parsed_base_url = urlsplit(base_url)
    decoded_path = unquote(path)
    if (
        parsed_base_url.scheme not in {"http", "https"}
        or not parsed_base_url.netloc
        or not path.startswith("/")
        or decoded_path.startswith("//")
        or "\\" in decoded_path
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
    ):
        return None
    normalized_query = urlencode(list(request.args.items(multi=True)), doseq=True)
    return urlunsplit(
        (
            parsed_base_url.scheme,
            parsed_base_url.netloc,
            path,
            normalized_query,
            "",
        )
    )


def _is_public_marketing_path(path: str) -> bool:
    public_paths = {
        "/",
        "/features",
        "/pricing",
        "/security",
        "/request-a-pilot",
        "/contact",
        "/privacy",
        "/terms",
        "/acceptable-use",
        "/sms-a2p-policy",
        "/billing-cancellation-refund-policy",
    }
    return path in public_paths


def _is_legacy_public_callback_path(path: str) -> bool:
    preserved_prefixes = (
        "/webhooks/",
        "/invites/",
        "/compliance/",
        "/billing",
        "/setup",
    )
    infrastructure_paths = {"/health", "/ready", "/favicon.ico"}
    return path in infrastructure_paths or path.startswith("/static/") or path.startswith(preserved_prefixes)


def _host_redirect_target(app: Flask, host: str, path: str) -> str | None:
    public_base_url = str(app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    app_base_url = str(app.config.get("APP_BASE_URL") or "").strip().rstrip("/")
    public_host = (urlsplit(public_base_url).hostname or "").lower()
    app_host = (urlsplit(app_base_url).hostname or "").lower()
    www_host = f"www.{public_host}" if public_host else ""

    if not public_host or not app_host:
        return None
    if host == www_host and _is_public_marketing_path(path):
        return _configured_host_redirect_url(public_base_url, path)
    if host in {public_host, www_host}:
        if _is_public_marketing_path(path) or _is_legacy_public_callback_path(path):
            return None
        return _configured_host_redirect_url(app_base_url, path)
    if host == app_host and path != "/" and _is_public_marketing_path(path):
        return _configured_host_redirect_url(public_base_url, path)
    return None


def _run_startup_tasks(app: Flask) -> None:
    with app.app_context():
        if app.config.get("SAAS_MODE"):
            from app.saas_migrations.runner import ensure_saas_schema_ready
            from app.services.billing_service import validate_live_stripe_configuration

            saas_report = ensure_saas_schema_ready(db.engine)
            app.logger.info(
                "SaaS schema ready for %s; applied=%s pending=%s.",
                saas_report["db_label"],
                len(saas_report["applied"]),
                len(saas_report["pending"]),
            )
            if app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED"):
                validate_live_stripe_configuration()
            return

        from app.migrations.runner import (
            check_migrations_compatibility,
            inspect_migrations,
            run_pending_migrations,
        )

        if db.engine.url.drivername.startswith("sqlite"):
            db.create_all()
            check_migrations_compatibility(db.engine, app.logger)
            run_pending_migrations(db.engine, app.logger)
            migration_report = inspect_migrations(db.engine)
            migration_total = len(migration_report["migrations"])
            applied = set(migration_report["applied"])
            pending = [
                version
                for version in migration_report["migrations"]
                if version not in applied
            ]
            app.logger.info("Database file in use: %s", migration_report["db_path"])
            if migration_total:
                app.logger.info(
                    "Schema migrations: %s/%s applied; pending: %s",
                    len(applied),
                    migration_total,
                    ", ".join(pending) if pending else "none",
                )
            else:
                app.logger.info("Schema migrations: none")
        else:
            db.create_all()
            check_migrations_compatibility(db.engine, app.logger)

        _ensure_bootstrap_admin_user(app)


def _ensure_bootstrap_admin_user(app: Flask) -> None:
    from app.models import AppUser

    if AppUser.query.count() != 0:
        return

    admin_password = app.config.get("ADMIN_PASSWORD")
    if not admin_password:
        if not app.config.get("DEBUG"):
            raise RuntimeError(
                "ADMIN_PASSWORD must be set in production to create the first admin user"
            )
        return

    admin_username = (app.config.get("ADMIN_USERNAME") or "admin").strip() or "admin"
    admin_email = app.config.get("ADMIN_EMAIL") or f"{admin_username}@example.com"
    password_hash = admin_password
    if not admin_password.startswith(("pbkdf2:", "scrypt:")):
        password_hash = generate_password_hash(admin_password, method="pbkdf2:sha256")

    admin_user = AppUser(
        username=admin_username,
        email=admin_email,
        role="admin",
        is_platform_admin=bool(app.config.get("SAAS_MODE")),
        password_hash=password_hash,
    )
    db.session.add(admin_user)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing_user = AppUser.query.filter_by(username=admin_username).first()
        if existing_user is None:
            raise
        app.logger.info(
            "Bootstrap admin user %s already exists; another startup worker created it.",
            admin_username,
        )


def _configure_scheduler(app: Flask, *, start_scheduler: bool) -> None:
    scheduler_setting = app.config.get("SCHEDULER_ENABLED")

    if start_scheduler and scheduler_setting:
        app.logger.info(
            "Scheduler enabled (SCHEDULER_ENABLED=%s) via explicit runtime entrypoint; starting background scheduler.",
            scheduler_setting,
        )
        from app.services.scheduler_service import init_scheduler

        init_scheduler(app)
    elif start_scheduler and not scheduler_setting:
        app.logger.info(
            "Scheduler start was requested by the runtime entrypoint, but SCHEDULER_ENABLED=%s; not starting.",
            scheduler_setting,
        )
    else:
        if scheduler_setting:
            app.logger.warning(
                "Scheduler enabled (SCHEDULER_ENABLED=%s) but not started (explicit runtime entrypoint not requested).",
                scheduler_setting,
            )
        else:
            app.logger.info(
                "Scheduler disabled (SCHEDULER_ENABLED=%s); running web app only.",
                scheduler_setting,
            )


def _configure_security_headers(app: Flask) -> None:
    @app.after_request
    def apply_security_headers(response):
        if not app.config.get("SECURITY_HEADERS_ENABLED", True):
            return response

        if "X-Content-Type-Options" not in response.headers:
            response.headers["X-Content-Type-Options"] = "nosniff"
        if "X-Frame-Options" not in response.headers:
            response.headers["X-Frame-Options"] = "DENY"

        referrer_policy = str(app.config.get("SECURITY_REFERRER_POLICY") or "").strip()
        if referrer_policy and "Referrer-Policy" not in response.headers:
            response.headers["Referrer-Policy"] = referrer_policy

        permissions_policy = str(app.config.get("SECURITY_PERMISSIONS_POLICY") or "").strip()
        if permissions_policy and "Permissions-Policy" not in response.headers:
            response.headers["Permissions-Policy"] = permissions_policy

        content_security_policy = str(app.config.get("SECURITY_CONTENT_SECURITY_POLICY") or "").strip()
        if content_security_policy and "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = content_security_policy

        if request.is_secure and app.config.get("SECURITY_HSTS_ENABLED"):
            max_age = int(app.config.get("SECURITY_HSTS_MAX_AGE", 31536000) or 31536000)
            if "Strict-Transport-Security" not in response.headers:
                response.headers["Strict-Transport-Security"] = f"max-age={max_age}"

        return response


def _build_app() -> Flask:
    app = Flask(__name__)

    @app.context_processor
    def inject_app_version():
        return {
            "app_version": os.environ.get("APP_VERSION", "dev"),
            "current_year": datetime.now(timezone.utc).year,
            "product_name": app.config.get("PRODUCT_NAME", "Twinevia"),
            "product_descriptor": app.config.get("PRODUCT_DESCRIPTOR", "Messaging Workspace"),
            "password_min_length": int(app.config.get("AUTH_PASSWORD_MIN_LENGTH", 12)),
        }

    @app.template_filter("localtime")
    def localtime_filter(utc_dt, fmt="%Y-%m-%d %H:%M"):
        """Convert UTC datetime to user's local timezone."""
        if not utc_dt:
            return "-"

        app_timezone = app.config.get("APP_TIMEZONE", "UTC")
        client_tz_raw = request.cookies.get("client_timezone", "")
        client_tz = unquote(client_tz_raw).strip() if client_tz_raw else ""
        display_tz = client_tz or app_timezone

        try:
            tz = ZoneInfo(display_tz)
        except Exception:
            tz = timezone.utc

        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)

        return utc_dt.astimezone(tz).strftime(fmt)

    from app.config import Config

    app.config.from_object(Config)

    is_explicit_production = os.environ.get("FLASK_ENV", "").lower() == "production"
    if is_explicit_production:
        _validate_explicit_production_runtime(app)

    if not app.config.get("DEBUG"):
        if app.config.get("SECRET_KEY") == "dev-secret-key-change-in-production":
            raise RuntimeError("SECRET_KEY must be set in production")
        if is_explicit_production:
            _validate_production_security_config(app)
        _validate_saas_billing_config(app)
        _validate_aoc_event_sync_config(app)
        if is_explicit_production:
            _validate_production_operations_config(app)

    if is_explicit_production and app.config.get("TRUSTED_HOSTS"):
        trusted_hosts = {
            host.strip().lower()
            for host in app.config.get("TRUSTED_HOSTS", [])
            if host.strip()
        }
        @app.before_request
        def enforce_trusted_hosts():
            host = (request.host or "").split(":", 1)[0].strip().lower()
            if host not in trusted_hosts:
                abort(400)
            redirect_target = _host_redirect_target(app, host, request.path)
            if redirect_target is not None:
                return redirect(redirect_target, code=308)
            return None

    if app.config.get("TRUST_PROXY"):
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_port=1,
            x_prefix=1,
        )

    _configure_security_headers(app)

    instance_path = Path(app.instance_path)
    instance_path.mkdir(exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)

    from app.tenant import init_tenant_scoping

    init_tenant_scoping()

    from app.auth import bp as auth_bp, login_manager

    login_manager.init_app(app)
    app.register_blueprint(auth_bp)

    from app import routes

    app.register_blueprint(routes.bp)

    return app


def create_app(run_startup_tasks: bool = False, start_scheduler: bool = False) -> Flask:
    app = _build_app()
    if run_startup_tasks:
        _run_startup_tasks(app)

    _configure_scheduler(app, start_scheduler=start_scheduler)
    return app


def create_runtime_app(*, start_scheduler: bool = False) -> Flask:
    """Create the runtime app and perform explicit bootstrap side effects."""
    app = _build_app()
    _run_startup_tasks(app)
    _configure_scheduler(app, start_scheduler=start_scheduler)
    return app
