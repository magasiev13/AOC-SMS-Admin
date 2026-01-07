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


def ensure_message_log_columns(db, logger) -> None:
    engine = db.engine
    if engine.name != 'sqlite':
        return

    with engine.begin() as connection:
        result = connection.execute(text("PRAGMA table_info(message_logs)"))
        columns = {row._mapping["name"] for row in result}

        if not columns:
            return

        for column in MESSAGE_LOG_COLUMNS:
            if column["name"] in columns:
                continue

            statement = f"ALTER TABLE message_logs ADD COLUMN {column['name']} {column['type']}"
            if column["default"] is not None:
                statement += f" DEFAULT {column['default']}"

            connection.execute(text(statement))
            logger.info("Added missing message_logs column '%s'", column["name"])
