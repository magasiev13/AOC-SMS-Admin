"""Legacy SQLite compatibility for the additive feedback context columns."""
from importlib import import_module

from sqlalchemy import inspect


def apply(connection, logger):
    tables = set(inspect(connection).get_table_names())
    if not {'survey_flows', 'survey_sessions'} <= tables:
        logger.info('Legacy feedback migration skipped: survey tables are absent.')
        return
    import_module('app.saas_migrations.028_event_feedback_context').apply(connection, logger)
