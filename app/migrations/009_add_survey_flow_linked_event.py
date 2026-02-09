from sqlalchemy import text


def apply(connection, logger) -> None:
    result = connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='survey_flows'")
    )
    if result.first() is None:
        logger.info(
            "Skipping migration 009_add_survey_flow_linked_event: table survey_flows does not exist."
        )
        return

    columns = connection.execute(text("PRAGMA table_info(survey_flows)"))
    column_names = {row._mapping["name"] for row in columns}
    if "linked_event_id" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE survey_flows "
                "ADD COLUMN linked_event_id INTEGER REFERENCES events(id)"
            )
        )
        logger.info(
            "Migration 009_add_survey_flow_linked_event: added linked_event_id column to survey_flows."
        )

    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_survey_flows_linked_event_id "
            "ON survey_flows (linked_event_id)"
        )
    )
    logger.info(
        "Migration 009_add_survey_flow_linked_event: ensured index ix_survey_flows_linked_event_id exists."
    )
