"""Create consent sources when importing a database with SaaS organizations."""
from importlib import import_module

from sqlalchemy import inspect


def apply(connection, logger):
    if 'organizations' in inspect(connection).get_table_names():
        import_module('app.saas_migrations.035_consent_sources').apply(connection, logger)
