"""Legacy SQLite compatibility for retained inventory and source evidence."""
from importlib import import_module

from sqlalchemy import inspect


def apply(connection, logger):
    if not {'organizations', 'events', 'users'} <= set(inspect(connection).get_table_names()):
        logger.info('Booking inventory compatibility migration skipped: required tables absent.')
        return
    import_module('app.saas_migrations.030_booking_inventories').apply(connection, logger)
