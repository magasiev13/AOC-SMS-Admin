"""Add optional workspace preferences in the legacy importer schema."""
from importlib import import_module

from sqlalchemy import inspect


def apply(connection, logger):
    if 'organizations' in inspect(connection).get_table_names():
        import_module('app.saas_migrations.033_workspace_schedule_preferences').apply(connection, logger)
