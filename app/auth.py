from functools import wraps

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import func

from app import db
from app.models import (
    AppUser,
    Organization,
    OrganizationA2POnboarding,
    OrganizationMembership,
    OrganizationMessagingProfile,
    OrganizationSubscription,
    slugify_organization_name,
)
from app.tenant import clear_current_organization_id, set_current_organization_id
from app.utils import is_safe_url, normalize_phone, validate_phone
from app.services.auth_security_service import (
    check_login_limited,
    clear_failed_logins,
    normalize_login_username,
    password_policy_errors,
    record_auth_event,
    record_failed_login,
)
from app.services.billing_service import organization_can_send
from app.services.security_alert_service import send_security_alert


login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = None
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"

bp = Blueprint("auth", __name__)
SUSPENDED_ORGANIZATION_MESSAGE = "Your organization is currently suspended. Contact your platform admin."

TENANT_ENDPOINT_PREFIXES = (
    "main.dashboard",
    "main.setup",
    "main.billing_",
    "main.community_",
    "main.events_",
    "main.logs_",
    "main.scheduled_",
    "main.unsubscribed_",
    "main.inbox_",
    "main.keyword_rule",
    "main.keyword_rules",
    "main.survey_flow",
    "main.survey_flows",
    "main.team_",
)

OWNER_SETUP_ALLOWED_ENDPOINTS = {
    "auth.login",
    "auth.platform_login",
    "auth.logout",
    "main.change_password",
    "main.security_contact",
    "main.setup",
    "main.setup_status",
    "main.setup_billing_checkout",
    "main.billing_overview",
    "main.billing_checkout",
    "main.billing_portal",
}

STAFF_SETUP_ALLOWED_ENDPOINTS = {
    "auth.login",
    "auth.platform_login",
    "auth.logout",
    "main.change_password",
    "main.security_contact",
    "main.setup_pending",
    "main.setup_status",
    "main.billing_overview",
}


def _get_client_ip() -> str:
    return request.remote_addr or "unknown"


def home_endpoint_for_user(user) -> str:
    if not current_app.config.get("SAAS_MODE"):
        return "main.dashboard"
    return _setup_endpoint_for_user(user)


def _is_platform_admin_tenant_endpoint(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    return any(endpoint.startswith(prefix) for prefix in TENANT_ENDPOINT_PREFIXES)


def _organization_status_for_user(user) -> str | None:
    organization = getattr(user, "organization", None)
    if organization is None:
        return None
    normalized = (getattr(organization, "status", None) or "").strip().lower()
    return normalized or None


def _organization_setup_complete(user) -> bool:
    organization = getattr(user, "organization", None)
    if organization is None:
        return False
    messaging_profile = getattr(organization, "messaging_profile", None)
    return organization_can_send(organization) and bool(
        messaging_profile is not None and messaging_profile.can_send
    )


def _setup_endpoint_for_user(user) -> str:
    if not current_app.config.get("SAAS_MODE"):
        return "main.dashboard"
    if getattr(user, "is_platform_admin", False):
        return "main.platform_home"
    if getattr(user, "organization_id", None) is None:
        return "main.dashboard"
    if _organization_setup_complete(user):
        return "main.dashboard"
    if getattr(user, "organization_role", None) == "owner":
        return "main.setup"
    return "main.setup_pending"


def _is_owner_setup_allowed_endpoint(endpoint: str | None) -> bool:
    return bool(endpoint) and endpoint in OWNER_SETUP_ALLOWED_ENDPOINTS


def _is_staff_setup_allowed_endpoint(endpoint: str | None) -> bool:
    return bool(endpoint) and endpoint in STAFF_SETUP_ALLOWED_ENDPOINTS


def _unique_org_slug(name: str) -> str:
    base_slug = slugify_organization_name(name)
    candidate = base_slug
    counter = 2
    while Organization.query.filter_by(slug=candidate).first() is not None:
        suffix = f"-{counter}"
        candidate = f"{base_slug[: max(1, 64 - len(suffix))]}{suffix}"
        counter += 1
    return candidate


def _deny_suspended_organization_access():
    logout_user()
    session.clear()
    clear_current_organization_id()
    flash(SUSPENDED_ORGANIZATION_MESSAGE, "error")
    return redirect(url_for("auth.login"))


@login_manager.unauthorized_handler
def _handle_unauthorized():
    next_url = request.full_path if request.query_string else request.path
    if request.path.startswith("/platform"):
        return redirect(url_for("auth.platform_login", next=next_url))
    return redirect(url_for("auth.login", next=next_url))


def require_roles(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            effective_role = getattr(current_user, "effective_role", getattr(current_user, "role", None))
            if roles and not getattr(current_user, "is_platform_admin", False) and effective_role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


@bp.before_app_request
def enforce_account_security():
    clear_current_organization_id()
    if not current_user.is_authenticated:
        return None

    endpoint = request.endpoint or ""
    if endpoint.startswith("static"):
        return None

    if current_app.config.get("SAAS_MODE") and not getattr(current_user, "is_platform_admin", False):
        organization_id = getattr(current_user, "organization_id", None)
        if not organization_id:
            logout_user()
            session.clear()
            flash("Your account is not assigned to an organization.", "error")
            return redirect(url_for("auth.login"))
        set_current_organization_id(organization_id)

        if _organization_status_for_user(current_user) == "suspended":
            allowed_endpoints = {
                "auth.logout",
            }
            if endpoint not in allowed_endpoints:
                return _deny_suspended_organization_access()

    # Keep idle timeout enforcement active by using permanent sessions.
    session.permanent = True

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

    if current_app.config.get("SAAS_MODE") and getattr(current_user, "is_platform_admin", False):
        if _is_platform_admin_tenant_endpoint(endpoint):
            if request.method == "GET":
                flash(
                    "Platform admins use the platform home. Sign in with an organization owner or staff account for workspace activity.",
                    "info",
                )
                return redirect(url_for("main.platform_home"))
            abort(403)

    return None


@bp.after_app_request
def clear_tenant_context(response):
    clear_current_organization_id()
    return response


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


def _render_login(surface: str):
    return render_template("auth/login.html", auth_surface=surface)


def _lookup_login_user(username_input: str, normalized_username: str):
    user = AppUser.query.filter(func.lower(AppUser.email) == normalized_username).first()
    if not user:
        user = AppUser.query.filter_by(username=username_input).first()
    if not user and normalized_username:
        user = (
            AppUser.query
            .filter(func.lower(AppUser.username) == normalized_username)
            .order_by(AppUser.id.asc())
            .first()
        )
    return user


def _complete_login(user: AppUser, *, remember: bool, client_ip: str):
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
    if next_page and is_safe_url(next_page, request.host_url):
        return redirect(next_page)
    return redirect(url_for(home_endpoint_for_user(user)))


def _handle_login(surface: str):
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
            return _render_login(surface)

        user = _lookup_login_user(username_input, normalized_username)

        if user and user.check_password(password):
            if surface == "platform" and not getattr(user, "is_platform_admin", False):
                flash("Use the workspace login to access your organization account.", "error")
                return _render_login(surface)
            if current_app.config.get("SAAS_MODE") and not getattr(user, "is_platform_admin", False):
                if _organization_status_for_user(user) == "suspended":
                    record_auth_event(
                        "login_denied",
                        outcome="blocked",
                        user=user,
                        username=user.username,
                        client_ip=client_ip,
                        metadata={"reason": "organization_suspended"},
                    )
                    flash(SUSPENDED_ORGANIZATION_MESSAGE, "error")
                    return _render_login(surface)
            return _complete_login(user, remember=remember, client_ip=client_ip)

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

        flash("Invalid email, username, or password.", "error")

    return _render_login(surface)


@bp.route("/login", methods=["GET", "POST"])
def login():
    return _handle_login("tenant")


@bp.route("/platform/login", methods=["GET", "POST"])
def platform_login():
    return _handle_login("platform")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if not current_app.config.get("SAAS_MODE"):
        abort(404)
    if request.method == "POST":
        organization_name = request.form.get("organization_name", "").strip()
        full_name = request.form.get("full_name", "").strip() or None
        email = request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip()
        phone_input = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not organization_name:
            flash("Business name is required.", "error")
            return render_template("auth/signup.html")
        if not email:
            flash("Business email is required.", "error")
            return render_template("auth/signup.html")
        if not username:
            flash("Username is required.", "error")
            return render_template("auth/signup.html")
        if not phone_input:
            flash("Phone number is required.", "error")
            return render_template("auth/signup.html")
        if password != confirm_password:
            flash("Password confirmation does not match.", "error")
            return render_template("auth/signup.html")
        policy_errors = password_policy_errors(password, username=username)
        if policy_errors:
            for error in policy_errors:
                flash(error, "error")
            return render_template("auth/signup.html")

        normalized_phone = normalize_phone(phone_input)
        if not validate_phone(normalized_phone):
            flash("Phone number must be a valid E.164 number.", "error")
            return render_template("auth/signup.html")
        if AppUser.query.filter(func.lower(AppUser.email) == email).first():
            flash("That email is already in use.", "error")
            return render_template("auth/signup.html")
        if (
            AppUser.query.filter(func.lower(AppUser.username) == normalize_login_username(username)).first()
            or AppUser.query.filter_by(username=username).first()
        ):
            flash("That username is already taken.", "error")
            return render_template("auth/signup.html")

        organization = Organization(name=organization_name, slug=_unique_org_slug(organization_name), status="active")
        user = AppUser(
            username=username,
            email=email,
            full_name=full_name,
            phone=normalized_phone,
            role="admin",
            must_change_password=False,
        )
        user.set_password(password)
        membership = OrganizationMembership(organization=organization, user=user, role="owner")
        subscription = OrganizationSubscription(
            organization=organization,
            stripe_price_id=current_app.config.get("STRIPE_PRICE_ID"),
            status="incomplete",
        )
        messaging_profile = OrganizationMessagingProfile(
            organization=organization,
            provider_mode="platform_managed",
            status="pending",
            provider_status="pending",
            sender_review_status="pending",
        )
        onboarding = OrganizationA2POnboarding(
            organization=organization,
            business_name=organization_name,
            email=email,
            notification_email=email,
            number_strategy="auto_buy",
            onboarding_status="draft",
            business_regions_json='["USA_AND_CANADA"]',
        )
        db.session.add_all([organization, user, membership, subscription, messaging_profile, onboarding])
        db.session.commit()
        return _complete_login(user, remember=True, client_ip=_get_client_ip())

    return render_template("auth/signup.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))
