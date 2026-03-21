from sqlalchemy import inspect, text


PROFILE_COLUMNS = (
    "provider_mode VARCHAR(30) NOT NULL DEFAULT 'platform_managed'",
    "twilio_auth_token_encrypted TEXT",
    "credential_reference VARCHAR(255)",
    "phone_number_sid VARCHAR(64)",
    "provider_status VARCHAR(20) NOT NULL DEFAULT 'pending'",
    "business_type VARCHAR(80)",
    "use_case VARCHAR(120)",
    "consent_acknowledged_at TIMESTAMP",
    "sender_review_status VARCHAR(20) NOT NULL DEFAULT 'pending'",
    "provisioning_started_at TIMESTAMP",
    "provisioned_at TIMESTAMP",
    "suspended_at TIMESTAMP",
    "provider_last_checked_at TIMESTAMP",
    "last_provision_error TEXT",
)

TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS organization_provider_audit_logs (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        actor_user_id INTEGER,
        action VARCHAR(40) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'success',
        message TEXT,
        metadata_json TEXT,
        created_at TIMESTAMP NOT NULL,
        FOREIGN KEY(organization_id) REFERENCES organizations (id),
        FOREIGN KEY(actor_user_id) REFERENCES users (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messaging_usage_records (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        message_sid VARCHAR(64) NOT NULL,
        direction VARCHAR(10) NOT NULL DEFAULT 'outbound',
        source VARCHAR(20) NOT NULL DEFAULT 'blast',
        twilio_subaccount_sid VARCHAR(64),
        twilio_message_status VARCHAR(30),
        provider_currency VARCHAR(8) NOT NULL DEFAULT 'usd',
        provider_cost NUMERIC(12, 4) NOT NULL DEFAULT 0,
        sell_rate NUMERIC(12, 4) NOT NULL DEFAULT 0,
        sell_amount NUMERIC(12, 4) NOT NULL DEFAULT 0,
        margin NUMERIC(12, 4) NOT NULL DEFAULT 0,
        billable_units INTEGER NOT NULL DEFAULT 0,
        billable BOOLEAN NOT NULL DEFAULT FALSE,
        reconciliation_status VARCHAR(20) NOT NULL DEFAULT 'pending',
        billing_period_start TIMESTAMP,
        billing_period_end TIMESTAMP,
        last_error TEXT,
        created_at TIMESTAMP NOT NULL,
        reconciled_at TIMESTAMP,
        FOREIGN KEY(organization_id) REFERENCES organizations (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS organization_usage_billing_periods (
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL,
        period_start TIMESTAMP NOT NULL,
        period_end TIMESTAMP NOT NULL,
        included_units INTEGER NOT NULL DEFAULT 0,
        used_units INTEGER NOT NULL DEFAULT 0,
        overage_units INTEGER NOT NULL DEFAULT 0,
        sell_amount NUMERIC(12, 4) NOT NULL DEFAULT 0,
        currency VARCHAR(8) NOT NULL DEFAULT 'usd',
        stripe_invoice_item_id VARCHAR(80),
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        posted_at TIMESTAMP,
        FOREIGN KEY(organization_id) REFERENCES organizations (id)
    )
    """,
)

INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_messaging_profiles_phone_number_sid ON organization_messaging_profiles (phone_number_sid)",
    "CREATE INDEX IF NOT EXISTS ix_org_provider_audit_logs_organization_id ON organization_provider_audit_logs (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_provider_audit_logs_actor_user_id ON organization_provider_audit_logs (actor_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_provider_audit_logs_action ON organization_provider_audit_logs (action)",
    "CREATE INDEX IF NOT EXISTS ix_org_provider_audit_logs_status ON organization_provider_audit_logs (status)",
    "CREATE INDEX IF NOT EXISTS ix_org_provider_audit_logs_created_at ON organization_provider_audit_logs (created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_messaging_usage_records_message_sid ON messaging_usage_records (message_sid)",
    "CREATE INDEX IF NOT EXISTS ix_messaging_usage_records_organization_id ON messaging_usage_records (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_messaging_usage_records_direction ON messaging_usage_records (direction)",
    "CREATE INDEX IF NOT EXISTS ix_messaging_usage_records_twilio_subaccount_sid ON messaging_usage_records (twilio_subaccount_sid)",
    "CREATE INDEX IF NOT EXISTS ix_messaging_usage_records_twilio_message_status ON messaging_usage_records (twilio_message_status)",
    "CREATE INDEX IF NOT EXISTS ix_messaging_usage_records_reconciliation_status ON messaging_usage_records (reconciliation_status)",
    "CREATE INDEX IF NOT EXISTS ix_messaging_usage_records_billing_period_start ON messaging_usage_records (billing_period_start)",
    "CREATE INDEX IF NOT EXISTS ix_messaging_usage_records_billing_period_end ON messaging_usage_records (billing_period_end)",
    "CREATE INDEX IF NOT EXISTS ix_org_usage_billing_periods_organization_id ON organization_usage_billing_periods (organization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_usage_period ON organization_usage_billing_periods (organization_id, period_start, period_end)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_usage_billing_periods_invoice_item_id ON organization_usage_billing_periods (stripe_invoice_item_id)",
    "CREATE INDEX IF NOT EXISTS ix_org_usage_billing_periods_status ON organization_usage_billing_periods (status)",
)


def _table_exists(connection, table_name: str) -> bool:
    return table_name in inspect(connection).get_table_names()


def _column_names(connection, table_name: str) -> set[str]:
    if not _table_exists(connection, table_name):
        return set()
    return {
        column["name"]
        for column in inspect(connection).get_columns(table_name)
    }


def _ensure_column(connection, table_name: str, column_sql: str, logger) -> None:
    column_name = column_sql.split()[0]
    columns = _column_names(connection, table_name)
    if not columns:
        logger.info("SaaS migration 003: table %s does not exist; skipping column %s.", table_name, column_name)
        return
    if column_name in columns:
        logger.info("SaaS migration 003: %s.%s already present.", table_name, column_name)
        return
    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
    logger.info("SaaS migration 003: added %s.%s.", table_name, column_name)


def apply(connection, logger) -> None:
    for column_sql in PROFILE_COLUMNS:
        _ensure_column(connection, "organization_messaging_profiles", column_sql, logger)

    connection.execute(
        text(
            """
            UPDATE organization_messaging_profiles
            SET provider_mode = COALESCE(NULLIF(provider_mode, ''), 'platform_managed'),
                provider_status = COALESCE(NULLIF(provider_status, ''), status, 'pending'),
                sender_review_status = COALESCE(NULLIF(sender_review_status, ''), 'pending')
            """
        )
    )
    logger.info("SaaS migration 003: backfilled provider defaults for organization messaging profiles.")

    for statement in TABLE_STATEMENTS:
        connection.execute(text(statement))
    logger.info("SaaS migration 003: ensured provider audit and usage billing tables.")

    for statement in INDEX_STATEMENTS:
        connection.execute(text(statement))
    logger.info("SaaS migration 003: ensured provider audit and usage billing indexes.")
