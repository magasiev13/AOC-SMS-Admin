import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
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
    expect_int_range("SESSION_IDLE_TIMEOUT_MINUTES", 5, 1440)
    expect_int_range("REMEMBER_COOKIE_DURATION_DAYS", 1, 30)
    expect_int_range("AUTH_PASSWORD_MIN_LENGTH", 12, 128)
    expect_int_range("SECURITY_HSTS_MAX_AGE", 300, 63072000)

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
        "SAAS_BASE_URL": app.config.get("SAAS_BASE_URL"),
        "TWILIO_CREDENTIAL_ENCRYPTION_KEY": app.config.get("TWILIO_CREDENTIAL_ENCRYPTION_KEY"),
    }
    missing = [name for name, value in required_values.items() if not str(value or "").strip()]
    if not str(app.config.get("STRIPE_MONTHLY_PRICE_ID") or app.config.get("STRIPE_PRICE_ID") or "").strip():
        missing.append("STRIPE_MONTHLY_PRICE_ID or STRIPE_PRICE_ID")
    if missing:
        details = "\n - ".join(f"{name} must be configured for SaaS billing." for name in missing)
        raise RuntimeError(f"SaaS billing configuration is invalid:\n - {details}")

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


def _run_startup_tasks(app: Flask) -> None:
    with app.app_context():
        if app.config.get("SAAS_MODE"):
            from app.saas_migrations.runner import ensure_saas_schema_ready

            saas_report = ensure_saas_schema_ready(db.engine)
            app.logger.info(
                "SaaS schema ready for %s; applied=%s pending=%s.",
                saas_report["db_label"],
                len(saas_report["applied"]),
                len(saas_report["pending"]),
            )
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


def _canonical_public_host(app: Flask, trusted_hosts: set[str]) -> tuple[str, str] | None:
    configured_base_url = str(app.config.get("SAAS_BASE_URL") or "").strip().rstrip("/")
    if not configured_base_url:
        return None

    parsed = urlsplit(configured_base_url)
    canonical_host = (parsed.hostname or "").strip().lower()
    if not parsed.scheme or not parsed.netloc or not canonical_host:
        return None
    if canonical_host not in trusted_hosts:
        return None
    return canonical_host, configured_base_url


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
        _validate_saas_billing_config(app)
        _validate_aoc_event_sync_config(app)
        if is_explicit_production:
            _validate_production_security_config(app)

    if is_explicit_production and app.config.get("TRUSTED_HOSTS"):
        trusted_hosts = {
            host.strip().lower()
            for host in app.config.get("TRUSTED_HOSTS", [])
            if host.strip()
        }
        canonical_public_host = _canonical_public_host(app, trusted_hosts)

        @app.before_request
        def enforce_trusted_hosts():
            host = (request.host or "").split(":", 1)[0].strip().lower()
            if host not in trusted_hosts:
                abort(400)
            if canonical_public_host and host != canonical_public_host[0]:
                request_path = request.full_path if request.query_string else request.path
                return redirect(f"{canonical_public_host[1]}{request_path}", code=308)
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
