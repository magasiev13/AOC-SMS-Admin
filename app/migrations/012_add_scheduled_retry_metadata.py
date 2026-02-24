from sqlalchemy import text


def apply(connection, logger) -> None:
    result = connection.execute(text("PRAGMA table_info(scheduled_messages)"))
    columns = {row._mapping["name"] for row in result}

    if not columns:
        logger.info(
            "Skipping migration 012_add_scheduled_retry_metadata: table scheduled_messages does not exist."
        )
        return

    if "attempt_count" not in columns:
        connection.execute(
            text(
                "ALTER TABLE scheduled_messages "
                "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
            )
        )
        logger.info(
            "Migration 012_add_scheduled_retry_metadata: added column 'attempt_count'."
        )

    if "last_attempt_at" not in columns:
        connection.execute(
            text(
                "ALTER TABLE scheduled_messages "
                "ADD COLUMN last_attempt_at DATETIME"
            )
        )
        logger.info(
            "Migration 012_add_scheduled_retry_metadata: added column 'last_attempt_at'."
        )

    if "next_retry_at" not in columns:
        connection.execute(
            text(
                "ALTER TABLE scheduled_messages "
                "ADD COLUMN next_retry_at DATETIME"
            )
        )
        logger.info(
            "Migration 012_add_scheduled_retry_metadata: added column 'next_retry_at'."
        )

    connection.execute(
        text(
            "UPDATE scheduled_messages "
            "SET attempt_count = 1 "
            "WHERE attempt_count = 0 AND status IN ('processing', 'sent', 'failed', 'expired')"
        )
    )
    logger.info(
        "Migration 012_add_scheduled_retry_metadata: backfilled attempt_count for historical terminal/in-flight rows."
    )
