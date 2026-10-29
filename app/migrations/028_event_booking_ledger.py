"""Legacy SQLite compatibility for the additive booking ledger."""
from importlib import import_module
from sqlalchemy import inspect


def apply(connection, logger):
    if 'event_registrations' not in inspect(connection).get_table_names():
        logger.info('Legacy booking migration skipped: registration table is absent.')
        return
    import_module('app.saas_migrations.029_event_booking_ledger').apply(connection, logger)
