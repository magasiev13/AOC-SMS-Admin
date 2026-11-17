"""Workspace clock and event reminder preferences."""
from sqlalchemy import inspect, text


def apply(connection, logger):
    columns = {c['name'] for c in inspect(connection).get_columns('organizations')}
    if 'timezone_name' not in columns:
        connection.execute(text('ALTER TABLE organizations ADD COLUMN timezone_name VARCHAR(64)'))
        connection.execute(text("UPDATE organizations SET timezone_name = 'America/Denver' "
                                "WHERE slug = 'armenians-of-colorado' AND timezone_name IS NULL"))
    if 'reminder_preferences' not in columns:
        connection.execute(text('ALTER TABLE organizations ADD COLUMN reminder_preferences JSON'))
