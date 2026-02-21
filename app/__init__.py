import os
from flask import Flask, request, abort
from flask_sqlalchemy import SQLAlchemy
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
from urllib.parse import unquote
from zoneinfo import ZoneInfo
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf import CSRFProtect
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

    if app.config.get("AUTH_PASSWORD_POLICY_ENFORCE") is not True:
        errors.append("AUTH_PASSWORD_POLICY_ENFORCE must be enabled (1) in production.")

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


def create_app(run_startup_tasks: bool = True, start_scheduler: Optional[bool] = None):
    app = Flask(__name__)

    @app.context_processor
    def inject_app_version():
        return {
            "app_version": os.environ.get("APP_VERSION", "dev"),
            "current_year": datetime.now(timezone.utc).year,
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

    # Load configuration
    from app.config import Config

    app.config.from_object(Config)

    is_explicit_production = os.environ.get("FLASK_ENV", "").lower() == "production"
    if not app.config.get("DEBUG"):
        if app.config.get("SECRET_KEY") == "dev-secret-key-change-in-production":
            raise RuntimeError("SECRET_KEY must be set in production")
        if is_explicit_production:
            _validate_production_security_config(app)

    if is_explicit_production and app.config.get("TRUSTED_HOSTS"):
        trusted_hosts = {host.strip().lower() for host in app.config.get("TRUSTED_HOSTS", []) if host.strip()}

        @app.before_request
        def enforce_trusted_hosts():
            host = (request.host or "").split(":", 1)[0].strip().lower()
            if host not in trusted_hosts:
                abort(400)
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

    # Ensure instance folder exists
    instance_path = Path(app.instance_path)
    instance_path.mkdir(exist_ok=True)

    # Initialize extensions
    db.init_app(app)
    csrf.init_app(app)

    # Initialize Flask-Login
    from app.auth import login_manager, bp as auth_bp

    login_manager.init_app(app)
    app.register_blueprint(auth_bp)

    # Register routes
    from app import routes

    app.register_blueprint(routes.bp)

    if run_startup_tasks:
        # Create database tables and run migrations
        with app.app_context():
            from app.migrations.runner import (
                check_migrations_compatibility,
                inspect_migrations,
                run_pending_migrations,
            )

            check_migrations_compatibility(db.engine, app.logger)
            db.create_all()

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

            from app.models import AppUser

            if AppUser.query.count() == 0:
                admin_password = app.config.get("ADMIN_PASSWORD")
                if not admin_password:
                    if not app.config.get("DEBUG"):
                        raise RuntimeError(
                            "ADMIN_PASSWORD must be set in production to create the first admin user"
                        )
                else:
                    admin_username = app.config.get("ADMIN_USERNAME", "admin")
                    password_hash = admin_password
                    if not admin_password.startswith(("pbkdf2:", "scrypt:")):
                        password_hash = generate_password_hash(
                            admin_password, method="pbkdf2:sha256"
                        )

                    admin_user = AppUser(
                        username=admin_username,
                        role="admin",
                        password_hash=password_hash,
                    )
                    db.session.add(admin_user)
                    db.session.commit()

    # Start background scheduler
    scheduler_setting = app.config.get("SCHEDULER_ENABLED")
    if start_scheduler is None:
        start_scheduler = os.environ.get("SCHEDULER_RUNNER") == "1"
        scheduler_reason = "SCHEDULER_RUNNER flag"
    else:
        scheduler_reason = "explicit override"

    if start_scheduler and scheduler_setting:
        app.logger.info(
            "Scheduler enabled (SCHEDULER_ENABLED=%s) via %s; starting background scheduler.",
            scheduler_setting,
            scheduler_reason,
        )
        from app.services.scheduler_service import init_scheduler

        init_scheduler(app)
    elif start_scheduler and not scheduler_setting:
        app.logger.info(
            "Scheduler runner requested via %s, but SCHEDULER_ENABLED=%s; not starting.",
            scheduler_reason,
            scheduler_setting,
        )
    else:
        if scheduler_setting:
            app.logger.warning(
                "Scheduler enabled (SCHEDULER_ENABLED=%s) but not started (%s).",
                scheduler_setting,
                scheduler_reason,
            )
        else:
            app.logger.info(
                "Scheduler disabled (SCHEDULER_ENABLED=%s); running web app only (%s).",
                scheduler_setting,
                scheduler_reason,
            )

    return app
