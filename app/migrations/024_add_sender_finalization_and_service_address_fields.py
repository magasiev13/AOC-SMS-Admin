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
    ("service_address_country", "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_country VARCHAR(2)"),
    ("service_address_line1", "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_line1 VARCHAR(255)"),
    ("service_address_line2", "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_line2 VARCHAR(255)"),
    ("service_address_city", "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_city VARCHAR(120)"),
    ("service_address_region", "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_region VARCHAR(120)"),
    ("service_address_postal_code", "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_postal_code VARCHAR(32)"),
    ("twilio_address_sid", "ALTER TABLE organization_messaging_profiles ADD COLUMN twilio_address_sid VARCHAR(64)"),
    ("twilio_address_json", "ALTER TABLE organization_messaging_profiles ADD COLUMN twilio_address_json TEXT"),
    ("emergency_address_sid", "ALTER TABLE organization_messaging_profiles ADD COLUMN emergency_address_sid VARCHAR(64)"),
    ("emergency_address_status", "ALTER TABLE organization_messaging_profiles ADD COLUMN emergency_address_status VARCHAR(20)"),
    ("emergency_address_last_error", "ALTER TABLE organization_messaging_profiles ADD COLUMN emergency_address_last_error TEXT"),
    (
        "emergency_address_last_synced_at",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN emergency_address_last_synced_at DATETIME",
    ),
    (
        "sender_finalization_status",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN sender_finalization_status VARCHAR(40)",
    ),
    ("sender_finalization_error", "ALTER TABLE organization_messaging_profiles ADD COLUMN sender_finalization_error TEXT"),
    ("sender_finalized_at", "ALTER TABLE organization_messaging_profiles ADD COLUMN sender_finalized_at DATETIME"),
)


def apply(connection, logger):
    if not _table_exists(connection, "organization_messaging_profiles"):
        logger.info(
            "Migration 024_add_sender_finalization_and_service_address_fields: "
            "organization_messaging_profiles missing, skipping."
        )
        return

    for column_name, statement in PROFILE_COLUMNS:
        if _column_exists(connection, "organization_messaging_profiles", column_name):
            logger.info("Migration 024: organization_messaging_profiles.%s already exists.", column_name)
            continue
        connection.exec_driver_sql(statement)
        logger.info("Migration 024: added organization_messaging_profiles.%s.", column_name)

    connection.exec_driver_sql(
        """
        UPDATE organization_messaging_profiles
        SET sender_finalization_status = CASE
            WHEN provider_status = 'active' THEN 'active'
            WHEN phone_number_sid IS NOT NULL AND from_number IS NOT NULL THEN 'awaiting_emergency_address_sync'
            ELSE 'awaiting_a2p_approval'
        END
        WHERE COALESCE(NULLIF(sender_finalization_status, ''), '') = ''
        """
    )
    logger.info("Migration 024: backfilled sender finalization status for organization messaging profiles.")
