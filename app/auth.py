from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'warning'

bp = Blueprint('auth', __name__)


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
        
        if username == admin_username and password == admin_password:
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
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'
        
        user = User.validate(username, password)
        
        if user:
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('main.dashboard'))
        
        flash('Invalid username or password.', 'error')
    
    return render_template('auth/login.html')


@bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('auth.login'))
