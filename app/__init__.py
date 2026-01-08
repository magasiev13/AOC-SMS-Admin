import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from pathlib import Path
from typing import Optional
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash

db = SQLAlchemy()
csrf = CSRFProtect()


def create_app(run_startup_tasks: bool = True, start_scheduler: Optional[bool] = None):
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
    
    if run_startup_tasks:
        # Create database tables and run migrations
        with app.app_context():
            db.create_all()
            from app.migrations.runner import inspect_migrations, run_pending_migrations

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
    scheduler_setting = app.config.get('SCHEDULER_ENABLED')
    scheduler_reason = "explicit override" if start_scheduler is not None else "configuration"
    if start_scheduler is None:
        start_scheduler = scheduler_setting

    if start_scheduler:
        app.logger.info(
            "Scheduler enabled (SCHEDULER_ENABLED=%s) via %s; starting background scheduler.",
            scheduler_setting,
            scheduler_reason,
        )
        from app.services.scheduler_service import init_scheduler
        init_scheduler(app)
    else:
        app.logger.info(
            "Scheduler disabled (SCHEDULER_ENABLED=%s) via %s; running web app only.",
            scheduler_setting,
            scheduler_reason,
        )
    
    return app
