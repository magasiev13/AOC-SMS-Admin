from sqlalchemy import text


TENANT_COLUMNS = {
    "community_members": ["organization_id INTEGER"],
    "unsubscribed_contacts": ["organization_id INTEGER"],
    "suppressed_contacts": ["organization_id INTEGER"],
    "events": ["organization_id INTEGER"],
    "event_registrations": ["organization_id INTEGER"],
    "message_logs": ["organization_id INTEGER"],
    "inbox_threads": ["organization_id INTEGER"],
    "inbox_messages": ["organization_id INTEGER"],
    "keyword_automation_rules": ["organization_id INTEGER"],
    "survey_flows": ["organization_id INTEGER"],
    "survey_sessions": ["organization_id INTEGER"],
    "survey_responses": ["organization_id INTEGER"],
    "scheduled_messages": ["organization_id INTEGER"],
    "auth_events": ["organization_id INTEGER"],
}

ORGANIZATION_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS organizations (
        id INTEGER PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        slug VARCHAR(64) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS organization_memberships (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        role VARCHAR(20) NOT NULL DEFAULT 'staff',
        created_at DATETIME NOT NULL,
        FOREIGN KEY(organization_id) REFERENCES organizations (id),
        FOREIGN KEY(user_id) REFERENCES users (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS organization_invitations (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        email VARCHAR(255) NOT NULL,
        role VARCHAR(20) NOT NULL DEFAULT 'staff',
        token VARCHAR(128) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        invited_by_user_id INTEGER,
        expires_at DATETIME,
        accepted_at DATETIME,
        created_at DATETIME NOT NULL,
        FOREIGN KEY(organization_id) REFERENCES organizations (id),
        FOREIGN KEY(invited_by_user_id) REFERENCES users (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS organization_subscriptions (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        stripe_customer_id VARCHAR(80),
        stripe_subscription_id VARCHAR(80),
        stripe_price_id VARCHAR(80),
        status VARCHAR(30) NOT NULL DEFAULT 'incomplete',
        current_period_end DATETIME,
        cancel_at_period_end BOOLEAN NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        FOREIGN KEY(organization_id) REFERENCES organizations (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS organization_messaging_profiles (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        twilio_subaccount_sid VARCHAR(64),
        messaging_service_sid VARCHAR(64),
        from_number VARCHAR(20),
        inbound_identity VARCHAR(64),
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        FOREIGN KEY(organization_id) REFERENCES organizations (id)
    )
    """,
)

INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_organizations_slug ON organizations (slug)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_memberships_org_user ON organization_memberships (organization_id, user_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_memberships_organization_id ON organization_memberships (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_memberships_user_id ON organization_memberships (user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_org_invitations_token ON organization_invitations (token)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_invitations_org_email_status ON organization_invitations (organization_id, email, status)",
    "CREATE INDEX IF NOT EXISTS ix_org_invitations_organization_id ON organization_invitations (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_invitations_email ON organization_invitations (email)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_org_subscriptions_organization_id ON organization_subscriptions (organization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_subscriptions_customer_id ON organization_subscriptions (stripe_customer_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_subscriptions_subscription_id ON organization_subscriptions (stripe_subscription_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_org_messaging_profiles_organization_id ON organization_messaging_profiles (organization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_messaging_profiles_subaccount_sid ON organization_messaging_profiles (twilio_subaccount_sid)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_messaging_profiles_service_sid ON organization_messaging_profiles (messaging_service_sid)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_messaging_profiles_from_number ON organization_messaging_profiles (from_number)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_messaging_profiles_inbound_identity ON organization_messaging_profiles (inbound_identity)",
    "CREATE INDEX IF NOT EXISTS ix_org_messaging_profiles_inbound_identity ON organization_messaging_profiles (inbound_identity)",
    "CREATE INDEX IF NOT EXISTS ix_users_is_platform_admin ON users (is_platform_admin)",
    "CREATE INDEX IF NOT EXISTS ix_community_members_organization_id ON community_members (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_unsubscribed_contacts_organization_id ON unsubscribed_contacts (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_suppressed_contacts_organization_id ON suppressed_contacts (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_organization_id ON events (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_event_registrations_organization_id ON event_registrations (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_message_logs_organization_id ON message_logs (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_inbox_threads_organization_id ON inbox_threads (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_inbox_messages_organization_id ON inbox_messages (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_keyword_automation_rules_organization_id ON keyword_automation_rules (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_survey_flows_organization_id ON survey_flows (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_survey_sessions_organization_id ON survey_sessions (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_survey_responses_organization_id ON survey_responses (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_scheduled_messages_organization_id ON scheduled_messages (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_auth_events_organization_id ON auth_events (organization_id)",
)


def _table_exists(connection, table_name: str) -> bool:
    result = connection.execute(
        text(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return result.first() is not None


def _column_names(connection, table_name: str) -> set[str]:
    if not _table_exists(connection, table_name):
        return set()
    return {
        row._mapping["name"]
        for row in connection.execute(text(f"PRAGMA table_info({table_name})"))
    }


def _ensure_column(connection, table_name: str, column_sql: str, logger) -> None:
    column_name = column_sql.split()[0]
    columns = _column_names(connection, table_name)
    if not columns:
        logger.info("Migration 017: table %s does not exist; skipping column %s.", table_name, column_name)
        return
    if column_name in columns:
        logger.info("Migration 017: %s.%s already present.", table_name, column_name)
        return
    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
    logger.info("Migration 017: added %s.%s.", table_name, column_name)


def apply(connection, logger) -> None:
    _ensure_column(connection, "users", "is_platform_admin BOOLEAN NOT NULL DEFAULT 0", logger)

    for table_name, columns in TENANT_COLUMNS.items():
        for column_sql in columns:
            _ensure_column(connection, table_name, column_sql, logger)

    for statement in ORGANIZATION_TABLE_STATEMENTS:
        connection.execute(text(statement))
    logger.info("Migration 017: ensured SaaS organization tables.")

    for statement in INDEX_STATEMENTS:
        connection.execute(text(statement))
    logger.info("Migration 017: ensured SaaS tenant indexes.")
