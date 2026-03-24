from sqlalchemy import text


TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS platform_service_restart_requests (
    id INTEGER PRIMARY KEY,
    requested_by_user_id INTEGER,
    requested_username VARCHAR(80),
    client_ip VARCHAR(45),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    transient_unit VARCHAR(120),
    summary TEXT,
    detail TEXT,
    requested_at TIMESTAMP NOT NULL,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    last_checked_at TIMESTAMP,
    FOREIGN KEY(requested_by_user_id) REFERENCES users (id)
)
"""

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS ix_platform_service_restart_requests_requested_by_user_id ON platform_service_restart_requests (requested_by_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_platform_service_restart_requests_requested_username ON platform_service_restart_requests (requested_username)",
    "CREATE INDEX IF NOT EXISTS ix_platform_service_restart_requests_status ON platform_service_restart_requests (status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_platform_service_restart_requests_transient_unit ON platform_service_restart_requests (transient_unit)",
    "CREATE INDEX IF NOT EXISTS ix_platform_service_restart_requests_requested_at ON platform_service_restart_requests (requested_at)",
    "CREATE INDEX IF NOT EXISTS ix_platform_service_restart_requests_last_checked_at ON platform_service_restart_requests (last_checked_at)",
)


def apply(connection, logger) -> None:
    connection.execute(text(TABLE_STATEMENT))
    logger.info("SaaS migration 004: ensured platform service restart request table exists.")

    for statement in INDEX_STATEMENTS:
        connection.execute(text(statement))
    logger.info("SaaS migration 004: ensured platform service restart request indexes exist.")
