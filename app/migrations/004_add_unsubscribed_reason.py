from sqlalchemy import text


def apply(connection, logger) -> None:
    result = connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='unsubscribed_contacts'")
    )
    if result.first() is None:
        logger.info(
            "Skipping migration 004_add_unsubscribed_reason: table unsubscribed_contacts does not exist."
        )
        return

    columns = connection.execute(text("PRAGMA table_info(unsubscribed_contacts)"))
    column_names = {row._mapping["name"] for row in columns}
    if "reason" in column_names:
        logger.info("Skipping migration 004_add_unsubscribed_reason: reason column already exists.")
        return

    connection.execute(text("ALTER TABLE unsubscribed_contacts ADD COLUMN reason TEXT"))
    logger.info("Migration 004_add_unsubscribed_reason: added reason column to unsubscribed_contacts.")
