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
    if _table_exists(connection, "organization_a2p_onboardings"):
        onboarding_columns = _table_columns(connection, "organization_a2p_onboardings")
        for column_name, statement in (
            (
                "privacy_policy_url",
                "ALTER TABLE organization_a2p_onboardings ADD COLUMN privacy_policy_url VARCHAR(255)",
            ),
            (
                "terms_and_conditions_url",
                "ALTER TABLE organization_a2p_onboardings ADD COLUMN terms_and_conditions_url VARCHAR(255)",
            ),
            (
                "cta_proof_url",
                "ALTER TABLE organization_a2p_onboardings ADD COLUMN cta_proof_url VARCHAR(255)",
            ),
        ):
            if column_name in onboarding_columns:
                logger.info("Migration 022: organization_a2p_onboardings.%s already exists.", column_name)
                continue
            connection.exec_driver_sql(statement)
            logger.info("Migration 022: added organization_a2p_onboardings.%s.", column_name)
    else:
        logger.info("Migration 022: organization_a2p_onboardings missing, skipping hosted compliance fields.")

    if _table_exists(connection, "organization_messaging_profiles"):
        profile_columns = _table_columns(connection, "organization_messaging_profiles")
        for column_name, statement in (
            (
                "event_stream_sink_sid",
                "ALTER TABLE organization_messaging_profiles ADD COLUMN event_stream_sink_sid VARCHAR(64)",
            ),
            (
                "event_stream_subscription_sid",
                "ALTER TABLE organization_messaging_profiles ADD COLUMN event_stream_subscription_sid VARCHAR(64)",
            ),
            (
                "event_stream_status",
                "ALTER TABLE organization_messaging_profiles ADD COLUMN event_stream_status VARCHAR(30)",
            ),
            (
                "event_stream_error",
                "ALTER TABLE organization_messaging_profiles ADD COLUMN event_stream_error TEXT",
            ),
        ):
            if column_name in profile_columns:
                logger.info("Migration 022: organization_messaging_profiles.%s already exists.", column_name)
                continue
            connection.exec_driver_sql(statement)
            logger.info("Migration 022: added organization_messaging_profiles.%s.", column_name)
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_org_messaging_profiles_event_stream_status "
            "ON organization_messaging_profiles (event_stream_status)"
        )
        logger.info("Migration 022: ensured organization_messaging_profiles.event_stream_status index.")
    else:
        logger.info("Migration 022: organization_messaging_profiles missing, skipping Event Streams fields.")
