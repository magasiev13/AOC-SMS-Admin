from sqlalchemy import text


MESSAGE_LOG_COLUMNS = [
    {
        "name": "status",
        "type": "VARCHAR(20)",
        "default": "'sent'",
    },
    {
        "name": "total_recipients",
        "type": "INTEGER",
        "default": "0",
    },
    {
        "name": "success_count",
        "type": "INTEGER",
        "default": "0",
    },
    {
        "name": "failure_count",
        "type": "INTEGER",
        "default": "0",
    },
    {
        "name": "details",
        "type": "TEXT",
        "default": None,
    },
]


def apply(connection, logger) -> None:
    result = connection.execute(text("PRAGMA table_info(message_logs)"))
    columns = {row._mapping["name"] for row in result}

    if not columns:
        logger.info("Skipping migration 001_add_message_logs_columns: table message_logs does not exist.")
        return

    for column in MESSAGE_LOG_COLUMNS:
        if column["name"] in columns:
            logger.info(
                "Migration 001_add_message_logs_columns: column '%s' already present.",
                column["name"],
            )
            continue

        statement = f"ALTER TABLE message_logs ADD COLUMN {column['name']} {column['type']}"
        if column["default"] is not None:
            statement += f" DEFAULT {column['default']}"

        connection.execute(text(statement))
        logger.info(
            "Migration 001_add_message_logs_columns: added missing column '%s'.",
            column["name"],
        )
