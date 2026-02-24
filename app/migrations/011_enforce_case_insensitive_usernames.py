from sqlalchemy import text


def _table_columns(connection, table_name: str) -> set[str]:
    result = connection.execute(text(f"PRAGMA table_info({table_name})"))
    return {row._mapping["name"] for row in result}


def apply(connection, logger) -> None:
    users_columns = _table_columns(connection, "users")
    if not users_columns:
        logger.info("Skipping migration 011_enforce_case_insensitive_usernames: table users does not exist.")
        return

    connection.execute(
        text(
            """
            UPDATE users
            SET username = TRIM(username)
            WHERE username IS NOT NULL
            """
        )
    )

    duplicates = connection.execute(
        text(
            """
            SELECT lower(username) AS normalized_username, COUNT(*) AS total
            FROM users
            WHERE username IS NOT NULL
            GROUP BY lower(username)
            HAVING COUNT(*) > 1
            ORDER BY normalized_username
            LIMIT 5
            """
        )
    ).fetchall()
    if duplicates:
        duplicate_preview = ", ".join(
            f"{row._mapping['normalized_username']} ({row._mapping['total']})"
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot enforce case-insensitive username uniqueness because duplicates already exist: "
            f"{duplicate_preview}. Resolve duplicates first and rerun migrations."
        )

    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username_lower
            ON users (lower(username))
            """
        )
    )
    logger.info("Migration 011: ensured unique lower(username) index for users.")
