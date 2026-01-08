from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from app import db
from app.models import AppUser, LoginAttempt

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = None
login_manager.login_message_category = 'warning'

bp = Blueprint('auth', __name__)

_MAX_LOGIN_ATTEMPTS = 5
_ATTEMPT_WINDOW_SECONDS = 300
_LOCKOUT_SECONDS = 600


def _get_client_ip():
    return request.remote_addr or 'unknown'


def _is_safe_url(target):
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ('http', 'https') and host_url.netloc == redirect_url.netloc


def _login_rate_limited():
    client_ip = _get_client_ip()
    now = datetime.now(timezone.utc)

    record = LoginAttempt.query.filter_by(client_ip=client_ip).first()
    if not record:
        return False, None

    if record.locked_until:
        if now < record.locked_until.replace(tzinfo=timezone.utc):
            remaining = (record.locked_until.replace(tzinfo=timezone.utc) - now).total_seconds()
            return True, remaining
        else:
            db.session.delete(record)
            db.session.commit()
            return False, None

    first_attempt = record.first_attempt_at.replace(tzinfo=timezone.utc)
    if (now - first_attempt).total_seconds() > _ATTEMPT_WINDOW_SECONDS:
        db.session.delete(record)
        db.session.commit()
        return False, None

    if record.attempt_count >= _MAX_LOGIN_ATTEMPTS:
        record.locked_until = now + timedelta(seconds=_LOCKOUT_SECONDS)
        db.session.commit()
        return True, _LOCKOUT_SECONDS

    return False, None


def _record_failed_login():
    client_ip = _get_client_ip()
    now = datetime.now(timezone.utc)

    record = LoginAttempt.query.filter_by(client_ip=client_ip).first()
    if not record:
        record = LoginAttempt(client_ip=client_ip, attempt_count=1, first_attempt_at=now)
        db.session.add(record)
        db.session.commit()
        return

    first_attempt = record.first_attempt_at.replace(tzinfo=timezone.utc)
    if (now - first_attempt).total_seconds() > _ATTEMPT_WINDOW_SECONDS:
        record.attempt_count = 1
        record.first_attempt_at = now
        record.locked_until = None
    else:
        record.attempt_count += 1

    db.session.commit()


def _clear_failed_logins():
    client_ip = _get_client_ip()
    LoginAttempt.query.filter_by(client_ip=client_ip).delete()
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
    return AppUser.query.get(int(user_id))


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        limited, remaining = _login_rate_limited()
        if limited:
            minutes = max(1, int(remaining // 60))
            flash(f'Too many failed attempts. Try again in {minutes} minute(s).', 'error')
            return render_template('auth/login.html')

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'
        
        user = AppUser.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user, remember=remember)
            _clear_failed_logins()
            if user.must_change_password:
                return redirect(url_for('main.change_password'))
            next_page = request.args.get('next')
            if next_page and _is_safe_url(next_page):
                return redirect(next_page)
            return redirect(url_for('main.dashboard'))
        
        _record_failed_login()
        flash('Invalid username or password.', 'error')
    
    return render_template('auth/login.html')


@bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('auth.login'))
