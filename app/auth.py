import time
from urllib.parse import urljoin, urlparse
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from app.models import AppUser

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = None
login_manager.login_message_category = 'warning'

bp = Blueprint('auth', __name__)

_FAILED_LOGINS = {}
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
    data = _FAILED_LOGINS.get(client_ip)
    if not data:
        return False, None

    now = time.time()
    locked_until = data.get('locked_until')
    if locked_until and now < locked_until:
        return True, locked_until - now

    if locked_until and now >= locked_until:
        _FAILED_LOGINS.pop(client_ip, None)
        return False, None

    first_attempt = data.get('first_attempt', now)
    if now - first_attempt > _ATTEMPT_WINDOW_SECONDS:
        _FAILED_LOGINS.pop(client_ip, None)
        return False, None

    if data.get('count', 0) >= _MAX_LOGIN_ATTEMPTS:
        _FAILED_LOGINS[client_ip]['locked_until'] = now + _LOCKOUT_SECONDS
        return True, _LOCKOUT_SECONDS

    return False, None


def _record_failed_login():
    client_ip = _get_client_ip()
    now = time.time()
    data = _FAILED_LOGINS.get(client_ip)
    if not data:
        _FAILED_LOGINS[client_ip] = {'count': 1, 'first_attempt': now}
        return

    if now - data.get('first_attempt', now) > _ATTEMPT_WINDOW_SECONDS:
        _FAILED_LOGINS[client_ip] = {'count': 1, 'first_attempt': now}
        return

    data['count'] = data.get('count', 0) + 1
    _FAILED_LOGINS[client_ip] = data


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
            _FAILED_LOGINS.pop(_get_client_ip(), None)
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
