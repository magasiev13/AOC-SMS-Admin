import time
from urllib.parse import urljoin, urlparse
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required
from app.utils import verify_admin_password

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
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

class User(UserMixin):
    """Simple user class for single-admin authentication."""
    
    def __init__(self, id):
        self.id = id
    
    @staticmethod
    def validate(username, password):
        """Validate credentials against configured admin user."""
        admin_username = current_app.config.get('ADMIN_USERNAME')
        admin_password = current_app.config.get('ADMIN_PASSWORD')
        
        if not admin_password:
            return None
        
        if username == admin_username and verify_admin_password(admin_password, password):
            return User(id=username)
        return None


@login_manager.user_loader
def load_user(user_id):
    """Load user by ID (username)."""
    admin_username = current_app.config.get('ADMIN_USERNAME')
    if user_id == admin_username:
        return User(id=user_id)
    return None


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
        
        user = User.validate(username, password)
        
        if user:
            login_user(user, remember=remember)
            _FAILED_LOGINS.pop(_get_client_ip(), None)
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
