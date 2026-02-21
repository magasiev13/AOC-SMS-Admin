from sqlalchemy import text


def _table_columns(connection, table_name: str) -> set[str]:
    result = connection.execute(text(f"PRAGMA table_info({table_name})"))
    return {row._mapping["name"] for row in result}


def apply(connection, logger) -> None:
    users_columns = _table_columns(connection, "users")
    if not users_columns:
        logger.info("Skipping migration 010_add_auth_hardening_tables_and_columns: table users does not exist.")
        return

    if "phone" not in users_columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(20)"))
        logger.info("Migration 010: added users.phone.")

    if "session_nonce" not in users_columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN session_nonce VARCHAR(64) NOT NULL DEFAULT ''"))
        logger.info("Migration 010: added users.session_nonce.")

    connection.execute(
        text(
            """
            UPDATE users
            SET session_nonce = lower(hex(randomblob(16)))
            WHERE session_nonce IS NULL OR TRIM(session_nonce) = ''
            """
        )
    )
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_users_phone_nonempty
            ON users (phone)
            WHERE phone IS NOT NULL AND TRIM(phone) <> ''
            """
        )
    )
    logger.info("Migration 010: backfilled users.session_nonce and ensured users phone indexes.")

    login_attempt_columns = _table_columns(connection, "login_attempts")
    if login_attempt_columns:
        if "username" not in login_attempt_columns:
            connection.execute(
                text("ALTER TABLE login_attempts ADD COLUMN username VARCHAR(80) NOT NULL DEFAULT ''")
            )
            logger.info("Migration 010: added login_attempts.username.")

        connection.execute(
            text("UPDATE login_attempts SET username = '' WHERE username IS NULL")
        )
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_login_attempts_client_ip_username
                ON login_attempts (client_ip, username)
                """
            )
        )
        logger.info("Migration 010: normalized login_attempts.username and ensured composite index.")

    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_password_history (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_user_password_history_user_created
            ON user_password_history (user_id, created_at DESC)
            """
        )
    )

    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY,
                event_type VARCHAR(50) NOT NULL,
                outcome VARCHAR(20) NOT NULL DEFAULT 'success',
                user_id INTEGER,
                username VARCHAR(80),
                client_ip VARCHAR(45),
                metadata_json TEXT,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
            """
        )
    )
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_auth_events_created_at ON auth_events (created_at)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_auth_events_user_id ON auth_events (user_id)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_auth_events_event_type ON auth_events (event_type)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_auth_events_username ON auth_events (username)"))

    logger.info("Migration 010_add_auth_hardening_tables_and_columns: schema updates complete.")
