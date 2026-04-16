def _table_exists(connection, table_name: str) -> bool:
    cursor = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _column_exists(connection, table_name: str, column_name: str) -> bool:
    cursor = connection.exec_driver_sql(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cursor.fetchall())


TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS organization_test_recipients (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        phone VARCHAR(20) NOT NULL,
        label VARCHAR(120),
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        FOREIGN KEY(organization_id) REFERENCES organizations (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS organization_settings_audit_logs (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        actor_user_id INTEGER,
        category VARCHAR(40) NOT NULL,
        action VARCHAR(40) NOT NULL,
        metadata_json TEXT,
        created_at TIMESTAMP NOT NULL,
        FOREIGN KEY(organization_id) REFERENCES organizations (id),
        FOREIGN KEY(actor_user_id) REFERENCES users (id)
    )
    """,
)

INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_test_recipients_org_phone ON organization_test_recipients (organization_id, phone)",
    "CREATE INDEX IF NOT EXISTS ix_org_test_recipients_organization_id ON organization_test_recipients (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_test_recipients_phone ON organization_test_recipients (phone)",
    "CREATE INDEX IF NOT EXISTS ix_org_settings_audit_logs_organization_id ON organization_settings_audit_logs (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_settings_audit_logs_actor_user_id ON organization_settings_audit_logs (actor_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_settings_audit_logs_category ON organization_settings_audit_logs (category)",
    "CREATE INDEX IF NOT EXISTS ix_org_settings_audit_logs_action ON organization_settings_audit_logs (action)",
    "CREATE INDEX IF NOT EXISTS ix_org_settings_audit_logs_created_at ON organization_settings_audit_logs (created_at)",
)

COLUMN_STATEMENTS = (
    (
        "message_logs",
        "test_mode",
        "ALTER TABLE message_logs ADD COLUMN test_mode BOOLEAN NOT NULL DEFAULT 0",
    ),
    (
        "scheduled_messages",
        "test_recipient_selection_mode",
        "ALTER TABLE scheduled_messages ADD COLUMN test_recipient_selection_mode VARCHAR(20)",
    ),
    (
        "scheduled_messages",
        "test_recipient_snapshot_json",
        "ALTER TABLE scheduled_messages ADD COLUMN test_recipient_snapshot_json TEXT",
    ),
)


def apply(connection, logger):
    for statement in TABLE_STATEMENTS:
        connection.exec_driver_sql(statement)
    logger.info("Migration 026: ensured test-recipient tables.")

    for table_name, column_name, statement in COLUMN_STATEMENTS:
        if not _table_exists(connection, table_name):
            logger.info("Migration 026: table %s missing, skipping column %s.", table_name, column_name)
            continue
        if _column_exists(connection, table_name, column_name):
            logger.info("Migration 026: %s.%s already exists.", table_name, column_name)
            continue
        connection.exec_driver_sql(statement)
        logger.info("Migration 026: added %s.%s.", table_name, column_name)

    for statement in INDEX_STATEMENTS:
        connection.exec_driver_sql(statement)
    logger.info("Migration 026: ensured test-recipient indexes.")
