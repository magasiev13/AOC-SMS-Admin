from sqlalchemy import inspect, text


POSTGRES_PRIMARY_KEY_TABLES = (
    "organization_provider_audit_logs",
    "messaging_usage_records",
    "organization_usage_billing_periods",
    "platform_service_restart_requests",
    "organization_a2p_onboardings",
)


def _table_exists(connection, table_name: str) -> bool:
    return table_name in inspect(connection).get_table_names()


def _repair_postgres_primary_key_default(connection, table_name: str, logger) -> None:
    if not _table_exists(connection, table_name):
        logger.info("SaaS migration 006: table %s does not exist; skipping.", table_name)
        return

    sequence_name = f"{table_name}_id_seq"
    connection.execute(text(f"CREATE SEQUENCE IF NOT EXISTS {sequence_name}"))
    connection.execute(text(f"ALTER SEQUENCE {sequence_name} OWNED BY {table_name}.id"))
    connection.execute(text(f"ALTER TABLE {table_name} ALTER COLUMN id SET DEFAULT nextval('{sequence_name}')"))
    connection.execute(
        text(
            f"""
            SELECT setval(
                '{sequence_name}',
                COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1,
                false
            )
            """
        )
    )
    logger.info("SaaS migration 006: ensured Postgres id default for %s.", table_name)


def apply(connection, logger) -> None:
    if connection.dialect.name != "postgresql":
        logger.info("SaaS migration 006: dialect %s does not require Postgres id repairs.", connection.dialect.name)
        return

    for table_name in POSTGRES_PRIMARY_KEY_TABLES:
        _repair_postgres_primary_key_default(connection, table_name, logger)
