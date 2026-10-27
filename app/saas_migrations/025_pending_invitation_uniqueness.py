"""Allow retained invitation history while keeping one pending invite per email."""
from sqlalchemy import inspect, text


def apply(connection, logger):
    inspector = inspect(connection)
    if 'organization_invitations' not in inspector.get_table_names():
        raise RuntimeError('Migration 025 requires organization_invitations.')
    old_name = 'ux_org_invitations_org_email_status'
    if connection.dialect.name == 'postgresql':
        for constraint in inspector.get_unique_constraints('organization_invitations'):
            if set(constraint.get('column_names') or []) == {'organization_id', 'email', 'status'} and constraint.get('name') != old_name:
                raise RuntimeError('Invitation history has an unexpected uniqueness constraint; review it before migration 025.')
        if any(index.get('name') == old_name and not index.get('duplicates_constraint') for index in inspector.get_indexes('organization_invitations')):
            raise RuntimeError('Invitation history uses a standalone legacy uniqueness index; review it before migration 025.')
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_invitations_pending_email "
        "ON organization_invitations (organization_id, email) WHERE status = 'pending'"
    ))
    if connection.dialect.name == 'postgresql':
        connection.execute(text(
            'ALTER TABLE organization_invitations DROP CONSTRAINT IF EXISTS ux_org_invitations_org_email_status'
        ))
        logger.info('Migration 025: pending invitation uniqueness preserved; all historical rows, tokens and references retained.')
    elif connection.dialect.name == 'sqlite':
        logger.warning('Migration 025 SQLite compatibility: added pending index; any existing org/email/status uniqueness restriction is retained. PostgreSQL is required to relax historical invitation uniqueness without rebuilding this legacy database.')
    else:
        raise RuntimeError('Migration 025 supports PostgreSQL and SQLite compatibility only.')
