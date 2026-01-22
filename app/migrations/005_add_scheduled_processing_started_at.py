from sqlalchemy import text


def apply(connection, logger) -> None:
    result = connection.execute(text("PRAGMA table_info(scheduled_messages)"))
    columns = {row._mapping["name"] for row in result}

    if not columns:
        logger.info(
            "Skipping migration 005_add_scheduled_processing_started_at: table scheduled_messages does not exist."
        )
        return

    if "processing_started_at" not in columns:
        connection.execute(
            text(
                "ALTER TABLE scheduled_messages ADD COLUMN processing_started_at DATETIME"
            )
        )
        logger.info(
            "Migration 005_add_scheduled_processing_started_at: added missing column 'processing_started_at'."
        )

    connection.execute(
        text(
            "UPDATE scheduled_messages "
            "SET processing_started_at = scheduled_at "
            "WHERE processing_started_at IS NULL AND status = 'processing'"
        )
    )
    logger.info(
        "Migration 005_add_scheduled_processing_started_at: backfilled processing_started_at for processing messages."
    )
