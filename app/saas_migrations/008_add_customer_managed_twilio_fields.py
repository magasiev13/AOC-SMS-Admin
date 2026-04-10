from sqlalchemy import inspect, text


def apply(connection, logger) -> None:
    inspector = inspect(connection)
    if "organization_messaging_profiles" not in inspector.get_table_names():
        logger.info("SaaS migration 008: organization_messaging_profiles missing, skipping.")
        return

    existing_columns = {
        row["name"]
        for row in inspector.get_columns("organization_messaging_profiles")
    }

    if "twilio_account_sid" not in existing_columns:
        connection.execute(
            text(
                "ALTER TABLE organization_messaging_profiles "
                "ADD COLUMN twilio_account_sid VARCHAR(64)"
            )
        )
        logger.info("SaaS migration 008: added organization_messaging_profiles.twilio_account_sid.")
    else:
        logger.info("SaaS migration 008: organization_messaging_profiles.twilio_account_sid already exists.")

    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_org_messaging_profiles_twilio_account_sid "
            "ON organization_messaging_profiles (twilio_account_sid)"
        )
    )
    logger.info("SaaS migration 008: ensured customer-managed Twilio indexes.")
