from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from app import db
from app.models import AppUser, LoginAttempt

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = None
login_manager.login_message_category = 'warning'

bp = Blueprint('auth', __name__)


def _get_client_ip():
    return request.remote_addr or 'unknown'


def _normalize_username(username: str | None) -> str:
    return (username or '').strip().lower()


def _attempt_window_seconds() -> int:
    return int(current_app.config.get('AUTH_ATTEMPT_WINDOW_SECONDS', 300))


def _lockout_seconds() -> int:
    return int(current_app.config.get('AUTH_LOCKOUT_SECONDS', 900))


def _keys_for_login_attempt(username: str | None, include_legacy_ip: bool = False) -> list[str]:
    client_ip = _get_client_ip()
    normalized_username = _normalize_username(username)

    keys = [f'ip:{client_ip}']
    if include_legacy_ip:
        keys.append(client_ip)
    if normalized_username:
        keys.append(f'account:{normalized_username}')
        keys.append(f'ip_account:{client_ip}:{normalized_username}')
    return keys


def _attempt_limit_for_key(key: str) -> int:
    if key.startswith('ip_account:'):
        return int(current_app.config.get('AUTH_MAX_ATTEMPTS_IP_ACCOUNT', 5))
    if key.startswith('account:'):
        return int(current_app.config.get('AUTH_MAX_ATTEMPTS_ACCOUNT', 8))
    return int(current_app.config.get('AUTH_MAX_ATTEMPTS_IP', 30))


def _is_safe_url(target):
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ('http', 'https') and host_url.netloc == redirect_url.netloc


def _login_rate_limited(username: str | None):
    now = datetime.now(timezone.utc)
    lockout_seconds = _lockout_seconds()
    attempt_window_seconds = _attempt_window_seconds()

    keys = _keys_for_login_attempt(username, include_legacy_ip=True)
    records = LoginAttempt.query.filter(LoginAttempt.client_ip.in_(keys)).all()

    records_to_delete = []
    record_changed = False
    max_remaining = 0.0

    for record in records:
        if record.locked_until:
            locked_until = record.locked_until.replace(tzinfo=timezone.utc)
            if now < locked_until:
                remaining = (locked_until - now).total_seconds()
                max_remaining = max(max_remaining, remaining)
                continue

            records_to_delete.append(record)
            continue

        first_attempt = record.first_attempt_at.replace(tzinfo=timezone.utc)
        if (now - first_attempt).total_seconds() > attempt_window_seconds:
            records_to_delete.append(record)
            continue

        max_attempts = _attempt_limit_for_key(record.client_ip)
        if record.attempt_count >= max_attempts:
            record.locked_until = now + timedelta(seconds=lockout_seconds)
            record_changed = True
            max_remaining = max(max_remaining, float(lockout_seconds))

    for record in records_to_delete:
        db.session.delete(record)
        record_changed = True

    if record_changed:
        db.session.commit()

    if max_remaining > 0:
        return True, max_remaining

    return False, None


def _record_failed_login(username: str | None):
    now = datetime.now(timezone.utc)
    attempt_window_seconds = _attempt_window_seconds()

    for key in _keys_for_login_attempt(username):
        record = LoginAttempt.query.filter_by(client_ip=key).first()
        if not record:
            record = LoginAttempt(client_ip=key, attempt_count=1, first_attempt_at=now)
            db.session.add(record)
            continue

        first_attempt = record.first_attempt_at.replace(tzinfo=timezone.utc)
        if (now - first_attempt).total_seconds() > attempt_window_seconds:
            record.attempt_count = 1
            record.first_attempt_at = now
            record.locked_until = None
        else:
            record.attempt_count += 1

    db.session.commit()


def _clear_failed_logins(username: str | None):
    keys = _keys_for_login_attempt(username, include_legacy_ip=True)
    LoginAttempt.query.filter(LoginAttempt.client_ip.in_(keys)).delete(synchronize_session=False)
    db.session.commit()


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
def enforce_password_change():
    if current_user.is_authenticated:
        session.permanent = True

    if not current_user.is_authenticated:
        return None
    if not current_user.must_change_password:
        return None

    endpoint = request.endpoint or ""
    if endpoint.startswith("static"):
        return None
    if endpoint in {"auth.login", "auth.logout", "main.change_password"}:
        return None

    return redirect(url_for("main.change_password"))


@login_manager.user_loader
def load_user(user_id):
    """Load user by ID."""
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        return None
    return db.session.get(AppUser, user_id_int)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'

        limited, remaining = _login_rate_limited(username)
        if limited:
            minutes = max(1, int(remaining // 60))
            flash(f'Too many failed attempts. Try again in {minutes} minute(s).', 'error')
            return render_template('auth/login.html')

        user = AppUser.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            _clear_failed_logins(username)
            if user.must_change_password:
                return redirect(url_for('main.change_password'))
            next_page = request.args.get('next')
            if next_page and _is_safe_url(next_page):
                return redirect(next_page)
            return redirect(url_for('main.dashboard'))

        _record_failed_login(username)
        flash('Invalid username or password.', 'error')
    
    return render_template('auth/login.html')


@bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('auth.login'))
