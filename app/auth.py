from functools import wraps
from urllib.parse import urljoin, urlparse

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user

from app import db
from app.models import AppUser
from app.services.auth_security_service import (
    check_login_limited,
    clear_failed_logins,
    normalize_login_username,
    record_auth_event,
    record_failed_login,
)
from app.services.security_alert_service import send_security_alert


login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = None
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"

bp = Blueprint("auth", __name__)


def _get_client_ip() -> str:
    return request.remote_addr or "unknown"


def _is_safe_url(target: str | None) -> bool:
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ("http", "https") and host_url.netloc == redirect_url.netloc


def require_roles(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if roles and current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


@bp.before_app_request
def enforce_account_security():
    if not current_user.is_authenticated:
        return None

    # Keep idle timeout enforcement active by using permanent sessions.
    session.permanent = True

    endpoint = request.endpoint or ""
    if endpoint.startswith("static"):
        return None

    if current_user.must_change_password:
        allowed_endpoints = {
            "auth.login",
            "auth.logout",
            "main.change_password",
            "main.security_contact",
        }
        if endpoint not in allowed_endpoints:
            return redirect(url_for("main.change_password"))

    if not current_user.phone:
        allowed_endpoints = {
            "auth.login",
            "auth.logout",
            "main.security_contact",
        }
        if endpoint not in allowed_endpoints:
            return redirect(url_for("main.security_contact"))

    return None


@login_manager.user_loader
def load_user(user_id):
    """Load user by nonce-bound session identifier."""
    if not user_id or ":" not in user_id:
        return None

    user_id_raw, nonce = user_id.split(":", 1)
    try:
        user_id_int = int(user_id_raw)
    except (TypeError, ValueError):
        return None

    user = db.session.get(AppUser, user_id_int)
    if not user or not user.session_nonce:
        return None

    if user.session_nonce != nonce:
        return None

    return user


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username_input = request.form.get("username", "").strip()
        normalized_username = normalize_login_username(username_input)
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"
        client_ip = _get_client_ip()

        limited, remaining_seconds, scope = check_login_limited(client_ip, normalized_username)
        if limited:
            minutes = max(1, int(((remaining_seconds or 0) + 59) // 60))
            record_auth_event(
                "login_blocked",
                outcome="blocked",
                username=normalized_username or username_input,
                client_ip=client_ip,
                metadata={
                    "scope": scope,
                    "remaining_seconds": remaining_seconds,
                },
            )
            flash(f"Too many failed attempts. Try again in {minutes} minute(s).", "error")
            return render_template("auth/login.html")

        user = AppUser.query.filter_by(username=username_input).first()

        if user and user.check_password(password):
            # Clear existing client session before issuing a new authenticated session.
            session.clear()
            login_user(user, remember=remember)
            clear_failed_logins(client_ip, normalize_login_username(user.username))
            record_auth_event(
                "login_success",
                outcome="success",
                user=user,
                username=user.username,
                client_ip=client_ip,
                metadata={"remember": remember},
            )

            if user.must_change_password:
                return redirect(url_for("main.change_password"))

            next_page = request.args.get("next")
            if next_page and _is_safe_url(next_page):
                return redirect(next_page)
            return redirect(url_for("main.dashboard"))

        lock_result = record_failed_login(client_ip, normalized_username)
        record_auth_event(
            "login_failure",
            outcome="failed",
            username=normalized_username or username_input,
            client_ip=client_ip,
            metadata=lock_result,
        )

        if user and lock_result.get("account_locked_now"):
            alert_result = send_security_alert(user, "account_lockout")
            if not alert_result.get("success"):
                record_auth_event(
                    "alert_sms_failed",
                    outcome="failed",
                    user=user,
                    username=user.username,
                    client_ip=client_ip,
                    metadata={
                        "context": "account_lockout",
                        "reason": alert_result.get("reason"),
                        "skipped": alert_result.get("skipped", False),
                    },
                )

        flash("Invalid username or password.", "error")

    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))
