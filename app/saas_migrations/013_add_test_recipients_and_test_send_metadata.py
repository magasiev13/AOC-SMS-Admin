from datetime import datetime, timezone

from sqlalchemy import inspect, text


MESSAGE_LOG_COLUMNS = (
    (
        "test_mode",
        "ALTER TABLE message_logs ADD COLUMN test_mode BOOLEAN NOT NULL DEFAULT FALSE",
    ),
)

SCHEDULED_MESSAGE_COLUMNS = (
    (
        "test_recipient_selection_mode",
        "ALTER TABLE scheduled_messages ADD COLUMN test_recipient_selection_mode VARCHAR(20)",
    ),
    (
        "test_recipient_snapshot_json",
        "ALTER TABLE scheduled_messages ADD COLUMN test_recipient_snapshot_json TEXT",
    ),
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


def _table_exists(connection, table_name: str) -> bool:
    return table_name in inspect(connection).get_table_names()


def _column_names(connection, table_name: str) -> set[str]:
    if not _table_exists(connection, table_name):
        return set()
    return {column["name"] for column in inspect(connection).get_columns(table_name)}


def _ensure_column(connection, table_name: str, column_name: str, statement: str, logger) -> None:
    columns = _column_names(connection, table_name)
    if not columns:
        logger.info("SaaS migration 013: table %s missing, skipping column %s.", table_name, column_name)
        return
    if column_name in columns:
        logger.info("SaaS migration 013: %s.%s already exists.", table_name, column_name)
        return
    connection.execute(text(statement))
    logger.info("SaaS migration 013: added %s.%s.", table_name, column_name)


def _create_table(connection, table_name: str, statement_sqlite: str, statement_postgres: str, logger) -> None:
    if _table_exists(connection, table_name):
        logger.info("SaaS migration 013: table %s already exists.", table_name)
        return
    if connection.dialect.name == "postgresql":
        connection.execute(text(statement_postgres))
    else:
        connection.execute(text(statement_sqlite))
    logger.info("SaaS migration 013: created table %s.", table_name)


def _backfill_owner_phones(connection, logger) -> None:
    required_tables = {
        "organization_test_recipients",
        "organization_memberships",
        "users",
    }
    if not required_tables.issubset(set(inspect(connection).get_table_names())):
        logger.info("SaaS migration 013: owner backfill skipped because required tables are missing.")
        return

    existing_rows = connection.execute(
        text("SELECT organization_id, phone FROM organization_test_recipients")
    )
    existing_keys = {
        (row._mapping["organization_id"], row._mapping["phone"])
        for row in existing_rows
        if row._mapping["organization_id"] is not None and row._mapping["phone"]
    }

    owner_rows = connection.execute(
        text(
            """
            SELECT organization_memberships.organization_id AS organization_id,
                   users.phone AS phone,
                   COALESCE(NULLIF(users.full_name, ''), NULLIF(users.username, '')) AS label
            FROM organization_memberships
            JOIN users ON users.id = organization_memberships.user_id
            WHERE organization_memberships.role = 'owner'
              AND users.phone IS NOT NULL
              AND users.phone <> ''
            ORDER BY organization_memberships.organization_id ASC, users.id ASC
            """
        )
    )

    inserted = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in owner_rows:
        key = (row._mapping["organization_id"], row._mapping["phone"])
        if key in existing_keys:
            continue
        connection.execute(
            text(
                """
                INSERT INTO organization_test_recipients (
                    organization_id,
                    phone,
                    label,
                    created_at,
                    updated_at
                ) VALUES (
                    :organization_id,
                    :phone,
                    :label,
                    :created_at,
                    :updated_at
                )
                """
            ),
            {
                "organization_id": row._mapping["organization_id"],
                "phone": row._mapping["phone"],
                "label": row._mapping["label"],
                "created_at": now,
                "updated_at": now,
            },
        )
        existing_keys.add(key)
        inserted += 1

    logger.info("SaaS migration 013: backfilled %d owner test recipient row(s).", inserted)


def apply(connection, logger) -> None:
    _create_table(
        connection,
        "organization_test_recipients",
        """
        CREATE TABLE organization_test_recipients (
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
        CREATE TABLE organization_test_recipients (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations (id),
            phone VARCHAR(20) NOT NULL,
            label VARCHAR(120),
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """,
        logger,
    )
    _create_table(
        connection,
        "organization_settings_audit_logs",
        """
        CREATE TABLE organization_settings_audit_logs (
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
        """
        CREATE TABLE organization_settings_audit_logs (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations (id),
            actor_user_id INTEGER REFERENCES users (id),
            category VARCHAR(40) NOT NULL,
            action VARCHAR(40) NOT NULL,
            metadata_json TEXT,
            created_at TIMESTAMP NOT NULL
        )
        """,
        logger,
    )

    for column_name, statement in MESSAGE_LOG_COLUMNS:
        _ensure_column(connection, "message_logs", column_name, statement, logger)
    for column_name, statement in SCHEDULED_MESSAGE_COLUMNS:
        _ensure_column(connection, "scheduled_messages", column_name, statement, logger)

    for statement in INDEX_STATEMENTS:
        connection.execute(text(statement))
    logger.info("SaaS migration 013: ensured test-recipient indexes.")

    _backfill_owner_phones(connection, logger)
