import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from pathlib import Path
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf import CSRFProtect

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
        if not app.config.get('ADMIN_PASSWORD'):
            raise RuntimeError('ADMIN_PASSWORD must be set in production')

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
    
    # Start background scheduler
    if app.config.get('SCHEDULER_ENABLED'):
        from app.services.scheduler_service import init_scheduler
        init_scheduler(app)
    
    return app
