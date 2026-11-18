from importlib import import_module
from sqlalchemy import inspect, text


def apply(connection, logger):
    tables = inspect(connection).get_table_names()
    if {'organizations', 'organization_memberships', 'users'} <= set(tables):
        import_module('app.saas_migrations.036_account_mail').apply(connection, logger)
    elif 'users' in tables and 'verified_recovery_email' not in {c['name'] for c in inspect(connection).get_columns('users')}:
        connection.execute(text('ALTER TABLE users ADD COLUMN verified_recovery_email VARCHAR(255)'))
