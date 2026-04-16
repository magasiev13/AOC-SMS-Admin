from sqlalchemy import inspect, text


PROFILE_COLUMNS = (
    (
        "service_address_source_mode",
        "ALTER TABLE organization_messaging_profiles ADD COLUMN service_address_source_mode VARCHAR(20)",
    ),
)


def apply(connection, logger) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())

    if "organization_messaging_profiles" not in table_names:
        logger.info("SaaS migration 012: organization_messaging_profiles missing, skipping service address source mode.")
        return

    profile_columns = {row["name"] for row in inspector.get_columns("organization_messaging_profiles")}
    for column_name, statement in PROFILE_COLUMNS:
        if column_name in profile_columns:
            logger.info("SaaS migration 012: organization_messaging_profiles.%s already exists.", column_name)
            continue
        connection.execute(text(statement))
        logger.info("SaaS migration 012: added organization_messaging_profiles.%s.", column_name)
