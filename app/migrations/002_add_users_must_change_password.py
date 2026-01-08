from sqlalchemy import text


def apply(connection, logger) -> None:
    result = connection.execute(text("PRAGMA table_info(users)"))
    columns = {row._mapping["name"] for row in result}

    if not columns:
        logger.info("Skipping migration 002_add_users_must_change_password: table users does not exist.")
        return

    if "must_change_password" not in columns:
        connection.execute(
            text(
                "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
            )
        )
        logger.info(
            "Migration 002_add_users_must_change_password: added missing column 'must_change_password'."
        )

    connection.execute(
        text("UPDATE users SET must_change_password = 0 WHERE must_change_password IS NULL")
    )
    logger.info(
        "Migration 002_add_users_must_change_password: backfilled must_change_password to false."
    )
