from sqlalchemy import inspect, text


ONBOARDING_COLUMNS = (
    ("privacy_policy_url", "ALTER TABLE organization_a2p_onboardings ADD COLUMN privacy_policy_url VARCHAR(255)"),
    (
        "terms_and_conditions_url",
        "ALTER TABLE organization_a2p_onboardings ADD COLUMN terms_and_conditions_url VARCHAR(255)",
    ),
    ("cta_proof_url", "ALTER TABLE organization_a2p_onboardings ADD COLUMN cta_proof_url VARCHAR(255)"),
)

PROFILE_COLUMNS = (
    ("event_stream_sink_sid", "ALTER TABLE organization_messaging_profiles ADD COLUMN event_stream_sink_sid VARCHAR(64)"),
    (
        "event_stream_subscription_sid",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN event_stream_subscription_sid VARCHAR(64)",
    ),
    ("event_stream_status", "ALTER TABLE organization_messaging_profiles ADD COLUMN event_stream_status VARCHAR(30)"),
    ("event_stream_error", "ALTER TABLE organization_messaging_profiles ADD COLUMN event_stream_error TEXT"),
)


def apply(connection, logger) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())

    if "organization_a2p_onboardings" in table_names:
        onboarding_columns = {row["name"] for row in inspector.get_columns("organization_a2p_onboardings")}
        for column_name, statement in ONBOARDING_COLUMNS:
            if column_name in onboarding_columns:
                logger.info("SaaS migration 009: organization_a2p_onboardings.%s already exists.", column_name)
                continue
            connection.execute(text(statement))
            logger.info("SaaS migration 009: added organization_a2p_onboardings.%s.", column_name)
    else:
        logger.info("SaaS migration 009: organization_a2p_onboardings missing, skipping hosted compliance fields.")

    if "organization_messaging_profiles" in table_names:
        profile_columns = {row["name"] for row in inspector.get_columns("organization_messaging_profiles")}
        for column_name, statement in PROFILE_COLUMNS:
            if column_name in profile_columns:
                logger.info("SaaS migration 009: organization_messaging_profiles.%s already exists.", column_name)
                continue
            connection.execute(text(statement))
            logger.info("SaaS migration 009: added organization_messaging_profiles.%s.", column_name)
        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_org_messaging_profiles_event_stream_status
                ON organization_messaging_profiles (event_stream_status)
                """
            )
        )
        logger.info("SaaS migration 009: ensured organization_messaging_profiles.event_stream_status index.")
    else:
        logger.info("SaaS migration 009: organization_messaging_profiles missing, skipping Event Streams fields.")
