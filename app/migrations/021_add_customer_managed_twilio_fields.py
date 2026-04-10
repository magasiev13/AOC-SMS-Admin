def _table_exists(connection, table_name: str) -> bool:
    cursor = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _table_columns(connection, table_name: str) -> set[str]:
    cursor = connection.exec_driver_sql(f"PRAGMA table_info('{table_name}')")
    return {row[1] for row in cursor.fetchall()}


def apply(connection, logger):
    if not _table_exists(connection, "organization_messaging_profiles"):
        logger.info("Migration 021: organization_messaging_profiles missing, skipping.")
        return

    columns = _table_columns(connection, "organization_messaging_profiles")
    if "twilio_account_sid" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE organization_messaging_profiles ADD COLUMN twilio_account_sid VARCHAR(64)"
        )
        logger.info("Migration 021: added organization_messaging_profiles.twilio_account_sid.")
    else:
        logger.info("Migration 021: organization_messaging_profiles.twilio_account_sid already exists.")

    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_org_messaging_profiles_twilio_account_sid "
        "ON organization_messaging_profiles (twilio_account_sid)"
    )
    logger.info("Migration 021: ensured customer-managed Twilio indexes.")
