from sqlalchemy import text


def _index_exists(connection, index_name: str) -> bool:
    result = connection.execute(
        text(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'index' AND name = :index_name
            """
        ),
        {"index_name": index_name},
    )
    return result.first() is not None


def apply(connection, logger) -> None:
    if _index_exists(connection, "uq_users_phone_nonempty"):
        connection.execute(text("DROP INDEX uq_users_phone_nonempty"))
        logger.info("Migration 013: dropped legacy unique users.phone index.")
    else:
        logger.info("Migration 013: legacy unique users.phone index already absent.")

    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))
    logger.info("Migration 013: ensured non-unique users.phone index.")
