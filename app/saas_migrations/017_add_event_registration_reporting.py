from sqlalchemy import inspect, text


COLUMNS = (
    ("events", "sms_location_note", "ALTER TABLE events ADD COLUMN sms_location_note TEXT"),
    (
        "event_registrations",
        "selections_json",
        "ALTER TABLE event_registrations ADD COLUMN selections_json TEXT",
    ),
    (
        "event_registrations",
        "booking_comment",
        "ALTER TABLE event_registrations ADD COLUMN booking_comment TEXT",
    ),
)


def apply(connection, logger) -> None:
    tables = set(inspect(connection).get_table_names())
    for table_name, column_name, statement in COLUMNS:
        if table_name not in tables:
            logger.info(
                "SaaS migration 017: table %s missing, skipping column %s.",
                table_name,
                column_name,
            )
            continue
        columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
        if column_name in columns:
            logger.info("SaaS migration 017: %s.%s already exists.", table_name, column_name)
            continue
        connection.execute(text(statement))
        logger.info("SaaS migration 017: added %s.%s.", table_name, column_name)
