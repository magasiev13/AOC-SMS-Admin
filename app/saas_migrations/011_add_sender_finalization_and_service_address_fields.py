from sqlalchemy import inspect, text


PROFILE_COLUMNS = (
    (
        "service_address_country",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_country VARCHAR(2)",
    ),
    (
        "service_address_line1",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_line1 VARCHAR(255)",
    ),
    (
        "service_address_line2",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_line2 VARCHAR(255)",
    ),
    (
        "service_address_city",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_city VARCHAR(120)",
    ),
    (
        "service_address_region",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_region VARCHAR(120)",
    ),
    (
        "service_address_postal_code",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_postal_code VARCHAR(32)",
    ),
    (
        "twilio_address_sid",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN twilio_address_sid VARCHAR(64)",
    ),
    (
        "twilio_address_json",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN twilio_address_json TEXT",
    ),
    (
        "emergency_address_sid",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN emergency_address_sid VARCHAR(64)",
    ),
    (
        "emergency_address_status",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN emergency_address_status VARCHAR(20)",
    ),
    (
        "emergency_address_last_error",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN emergency_address_last_error TEXT",
    ),
    (
        "emergency_address_last_synced_at",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN emergency_address_last_synced_at TIMESTAMP",
    ),
    (
        "sender_finalization_status",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN sender_finalization_status VARCHAR(40)",
    ),
    (
        "sender_finalization_error",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN sender_finalization_error TEXT",
    ),
    (
        "sender_finalized_at",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN sender_finalized_at TIMESTAMP",
    ),
)


def apply(connection, logger) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())

    if "organization_messaging_profiles" not in table_names:
        logger.info("SaaS migration 011: organization_messaging_profiles missing, skipping sender finalization fields.")
        return

    profile_columns = {row["name"] for row in inspector.get_columns("organization_messaging_profiles")}
    for column_name, statement in PROFILE_COLUMNS:
        if column_name in profile_columns:
            logger.info("SaaS migration 011: organization_messaging_profiles.%s already exists.", column_name)
            continue
        connection.execute(text(statement))
        logger.info("SaaS migration 011: added organization_messaging_profiles.%s.", column_name)

    connection.execute(
        text(
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
    )
    logger.info("SaaS migration 011: backfilled sender finalization status for organization messaging profiles.")
