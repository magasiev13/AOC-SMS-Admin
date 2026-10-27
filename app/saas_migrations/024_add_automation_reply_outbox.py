"""Persist only new automatic replies; never recreate historical responses."""
from sqlalchemy import inspect


def apply(connection, logger):
    if 'inbox_messages' not in inspect(connection).get_table_names():
        raise RuntimeError('SaaS migration 024 requires the inbox schema.')
    from app.models import AutomationReplyOutbox

    table = AutomationReplyOutbox.__table__
    table.create(connection, checkfirst=True)
    for index in table.indexes:
        index.create(connection, checkfirst=True)
    logger.info('SaaS migration 024: automation outbox available; no historical messages backfilled or enqueued.')
