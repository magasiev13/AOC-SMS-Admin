"""Add reversible conversation archiving to the legacy inbox."""

from importlib import import_module

from sqlalchemy import inspect


def apply(connection, logger):
    if inspect(connection).has_table('inbox_threads'):
        import_module('app.saas_migrations.034_inbox_archive').apply(connection, logger)
