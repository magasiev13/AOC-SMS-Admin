from sqlalchemy import inspect, text


NEW_COLUMNS = (
    ("business_industry", "ALTER TABLE organization_a2p_onboardings ADD COLUMN business_industry VARCHAR(40)"),
    ("business_regions_json", "ALTER TABLE organization_a2p_onboardings ADD COLUMN business_regions_json TEXT"),
    ("notification_email", "ALTER TABLE organization_a2p_onboardings ADD COLUMN notification_email VARCHAR(255)"),
    ("business_title", "ALTER TABLE organization_a2p_onboardings ADD COLUMN business_title VARCHAR(120)"),
    ("address_country", "ALTER TABLE organization_a2p_onboardings ADD COLUMN address_country VARCHAR(2)"),
    ("address_line1", "ALTER TABLE organization_a2p_onboardings ADD COLUMN address_line1 VARCHAR(255)"),
    ("address_line2", "ALTER TABLE organization_a2p_onboardings ADD COLUMN address_line2 VARCHAR(255)"),
    ("address_city", "ALTER TABLE organization_a2p_onboardings ADD COLUMN address_city VARCHAR(120)"),
    ("address_region", "ALTER TABLE organization_a2p_onboardings ADD COLUMN address_region VARCHAR(120)"),
    ("address_postal_code", "ALTER TABLE organization_a2p_onboardings ADD COLUMN address_postal_code VARCHAR(32)"),
    ("declaration_accepted_at", "ALTER TABLE organization_a2p_onboardings ADD COLUMN declaration_accepted_at TIMESTAMP"),
)


def apply(connection, logger) -> None:
    inspector = inspect(connection)
    if "organization_a2p_onboardings" not in inspector.get_table_names():
        logger.info("SaaS migration 007: organization_a2p_onboardings missing, skipping.")
        return

    existing_columns = {
        row["name"]
        for row in inspector.get_columns("organization_a2p_onboardings")
    }

    for column_name, statement in NEW_COLUMNS:
        if column_name in existing_columns:
            logger.info("SaaS migration 007: %s already exists.", column_name)
            continue
        connection.execute(text(statement))
        logger.info("SaaS migration 007: added %s.", column_name)

    connection.execute(
        text(
            """
            UPDATE organization_a2p_onboardings
            SET notification_email = COALESCE(NULLIF(notification_email, ''), email)
            WHERE COALESCE(NULLIF(notification_email, ''), '') = ''
            """
        )
    )
    connection.execute(
        text(
            """
            UPDATE organization_a2p_onboardings
            SET business_title = COALESCE(NULLIF(business_title, ''), job_position)
            WHERE COALESCE(NULLIF(business_title, ''), '') = ''
            """
        )
    )
    connection.execute(
        text(
            """
            UPDATE organization_a2p_onboardings
            SET business_regions_json = '["USA_AND_CANADA"]'
            WHERE COALESCE(NULLIF(business_regions_json, ''), '') = ''
            """
        )
    )
    logger.info("SaaS migration 007: backfilled notification email, business title, and business regions.")
