from sqlalchemy import inspect, text


TABLE_NAME = "organization_event_sync_integrations"


CREATE_TABLE_STATEMENTS = {
    "postgresql": f"""
        CREATE TABLE {TABLE_NAME} (
            id SERIAL PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations(id),
            provider VARCHAR(40) NOT NULL DEFAULT 'wordpress',
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            webhook_secret_encrypted TEXT,
            last_event_synced_at TIMESTAMP,
            last_signup_synced_at TIMESTAMP,
            last_reconcile_synced_at TIMESTAMP,
            last_error_at TIMESTAMP,
            last_error_message TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ux_org_event_sync_org_provider UNIQUE (organization_id, provider)
        )
    """,
    "sqlite": f"""
        CREATE TABLE {TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL REFERENCES organizations(id),
            provider VARCHAR(40) NOT NULL DEFAULT 'wordpress',
            enabled BOOLEAN NOT NULL DEFAULT 0,
            webhook_secret_encrypted TEXT,
            last_event_synced_at TIMESTAMP,
            last_signup_synced_at TIMESTAMP,
            last_reconcile_synced_at TIMESTAMP,
            last_error_at TIMESTAMP,
            last_error_message TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ux_org_event_sync_org_provider UNIQUE (organization_id, provider)
        )
    """,
}


INDEX_STATEMENTS = (
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_organization_id ON {TABLE_NAME} (organization_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_provider ON {TABLE_NAME} (provider)",
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_enabled ON {TABLE_NAME} (enabled)",
)


def apply(connection, logger) -> None:
    inspector = inspect(connection)
    if TABLE_NAME not in inspector.get_table_names():
        dialect = connection.engine.dialect.name
        statement = CREATE_TABLE_STATEMENTS["sqlite"] if dialect == "sqlite" else CREATE_TABLE_STATEMENTS["postgresql"]
        connection.execute(text(statement))
        logger.info("SaaS migration 016: created %s.", TABLE_NAME)
    else:
        logger.info("SaaS migration 016: %s already exists.", TABLE_NAME)

    for statement in INDEX_STATEMENTS:
        connection.execute(text(statement))
    logger.info("SaaS migration 016: ensured event sync integration indexes.")
