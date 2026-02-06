from sqlalchemy import text


def apply(connection, logger) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS inbox_threads (
                id INTEGER PRIMARY KEY,
                phone VARCHAR(20) NOT NULL UNIQUE,
                contact_name VARCHAR(100),
                unread_count INTEGER NOT NULL DEFAULT 0,
                last_message_at DATETIME NOT NULL,
                last_message_preview TEXT,
                last_direction VARCHAR(10),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS inbox_messages (
                id INTEGER PRIMARY KEY,
                thread_id INTEGER NOT NULL,
                phone VARCHAR(20) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                body TEXT NOT NULL,
                message_sid VARCHAR(64) UNIQUE,
                automation_source VARCHAR(30),
                automation_source_id INTEGER,
                matched_keyword VARCHAR(40),
                delivery_status VARCHAR(30),
                delivery_error TEXT,
                raw_payload TEXT,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES inbox_threads (id)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS keyword_automation_rules (
                id INTEGER PRIMARY KEY,
                keyword VARCHAR(40) NOT NULL UNIQUE,
                response_body TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                match_count INTEGER NOT NULL DEFAULT 0,
                last_matched_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS survey_flows (
                id INTEGER PRIMARY KEY,
                name VARCHAR(120) NOT NULL UNIQUE,
                trigger_keyword VARCHAR(40) NOT NULL UNIQUE,
                intro_message TEXT,
                questions_json TEXT NOT NULL DEFAULT '[]',
                completion_message TEXT,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                start_count INTEGER NOT NULL DEFAULT 0,
                completion_count INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS survey_sessions (
                id INTEGER PRIMARY KEY,
                survey_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                phone VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                current_question_index INTEGER NOT NULL DEFAULT 0,
                started_at DATETIME NOT NULL,
                last_activity_at DATETIME NOT NULL,
                completed_at DATETIME,
                FOREIGN KEY(survey_id) REFERENCES survey_flows (id),
                FOREIGN KEY(thread_id) REFERENCES inbox_threads (id)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS survey_responses (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                survey_id INTEGER NOT NULL,
                phone VARCHAR(20) NOT NULL,
                question_index INTEGER NOT NULL,
                question_prompt TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(session_id) REFERENCES survey_sessions (id),
                FOREIGN KEY(survey_id) REFERENCES survey_flows (id)
            )
            """
        )
    )

    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_inbox_messages_created_at ON inbox_messages (created_at)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_inbox_messages_thread_id ON inbox_messages (thread_id)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_inbox_messages_phone ON inbox_messages (phone)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_inbox_threads_last_message_at ON inbox_threads (last_message_at)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_keyword_automation_rules_keyword ON keyword_automation_rules (keyword)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_survey_flows_trigger_keyword ON survey_flows (trigger_keyword)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_survey_sessions_phone_status ON survey_sessions (phone, status)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_survey_responses_session_id ON survey_responses (session_id)"))

    logger.info("Migration 006_add_inbox_automation_tables: ensured inbox and automation tables exist.")
