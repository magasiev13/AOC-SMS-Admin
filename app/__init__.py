import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from pathlib import Path
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash

db = SQLAlchemy()
csrf = CSRFProtect()


def create_app():
    app = Flask(__name__)

    @app.context_processor
    def inject_app_version():
        return {"app_version": os.environ.get("APP_VERSION", "dev")}
    
    # Load configuration
    from app.config import Config
    app.config.from_object(Config)

    if not app.config.get('DEBUG'):
        if app.config.get('SECRET_KEY') == 'dev-secret-key-change-in-production':
            raise RuntimeError('SECRET_KEY must be set in production')

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    
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
    
    # Create database tables
    with app.app_context():
        db.create_all()
        from app.schema import ensure_message_log_columns

        ensure_message_log_columns(db, app.logger)

        from app.models import AppUser

        if AppUser.query.count() == 0:
            admin_password = app.config.get('ADMIN_PASSWORD')
            if not admin_password:
                if not app.config.get('DEBUG'):
                    raise RuntimeError('ADMIN_PASSWORD must be set in production to create the first admin user')
            else:
                admin_username = app.config.get('ADMIN_USERNAME', 'admin')
                password_hash = admin_password
                if not admin_password.startswith(('pbkdf2:', 'scrypt:')):
                    password_hash = generate_password_hash(admin_password)

                admin_user = AppUser(
                    username=admin_username,
                    role='admin',
                    password_hash=password_hash
                )
                db.session.add(admin_user)
                db.session.commit()
    
    # Start background scheduler
    if app.config.get('SCHEDULER_ENABLED'):
        from app.services.scheduler_service import init_scheduler
        init_scheduler(app)
    
    return app
