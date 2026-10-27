"""Persist send intent before queue/provider operations. No existing work changes."""
from sqlalchemy import inspect


def apply(connection, logger) -> None:
    if 'organizations' not in inspect(connection).get_table_names():
        raise RuntimeError('SaaS migration 021 requires the organizations table.')
    from app.models import OutboundRequest
    table = OutboundRequest.__table__
    table.create(connection, checkfirst=True)
    for index in table.indexes:
        index.create(connection, checkfirst=True)
    logger.info('SaaS migration 021: durable outbound requests available; existing jobs/schedules retained.')
