"""Add permission evidence without reclassifying any existing contact or booking."""
from sqlalchemy import inspect


def apply(connection, logger) -> None:
    if 'organizations' not in inspect(connection).get_table_names():
        raise RuntimeError('SaaS migration 020 requires the organizations table.')

    from app.models import RecipientPermissionEvidence

    table = RecipientPermissionEvidence.__table__
    table.create(connection, checkfirst=True)
    for index in table.indexes:
        index.create(connection, checkfirst=True)
    logger.info(
        'SaaS migration 020: retained permission evidence is available; '
        'existing contacts and bookings were not reclassified.'
    )
