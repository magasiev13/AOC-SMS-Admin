from sqlalchemy import text


def apply(connection, logger) -> None:
    result = connection.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='suppressed_contacts'"))
    if result.first() is not None:
        logger.info("Skipping migration 003_add_suppressed_contacts: table suppressed_contacts already exists.")
        return

    connection.execute(
        text(
            """
            CREATE TABLE suppressed_contacts (
                id INTEGER PRIMARY KEY,
                phone VARCHAR(20) NOT NULL UNIQUE,
                reason TEXT,
                category VARCHAR(20) NOT NULL,
                source VARCHAR(50),
                source_type VARCHAR(50),
                source_message_log_id INTEGER,
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY(source_message_log_id) REFERENCES message_logs (id)
            )
            """
        )
    )
    logger.info("Migration 003_add_suppressed_contacts: created suppressed_contacts table.")
