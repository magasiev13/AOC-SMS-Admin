"""Legacy compatibility for explicit feedback invitation records."""
from importlib import import_module
from sqlalchemy import inspect


def apply(connection, logger):
    if not {'organizations', 'events', 'survey_flows', 'scheduled_messages', 'users'} <= set(inspect(connection).get_table_names()):
        logger.info('Feedback invitation compatibility migration skipped: required tables absent.')
        return
    import_module('app.saas_migrations.031_feedback_invitations').apply(connection, logger)
