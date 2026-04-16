def _table_exists(connection, table_name: str) -> bool:
    cursor = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _column_exists(connection, table_name: str, column_name: str) -> bool:
    cursor = connection.exec_driver_sql(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cursor.fetchall())


PROFILE_COLUMNS = (
    (
        "service_address_source_mode",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_source_mode VARCHAR(20)",
    ),
)


def apply(connection, logger):
    if not _table_exists(connection, "organization_messaging_profiles"):
        logger.info(
            "Migration 025_add_service_address_source_mode: organization_messaging_profiles missing, skipping."
        )
        return

    for column_name, statement in PROFILE_COLUMNS:
        if _column_exists(connection, "organization_messaging_profiles", column_name):
            logger.info("Migration 025: organization_messaging_profiles.%s already exists.", column_name)
            continue
        connection.exec_driver_sql(statement)
        logger.info("Migration 025: added organization_messaging_profiles.%s.", column_name)
