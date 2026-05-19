from sqlalchemy import inspect, text


def apply(connection, logger) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    if "organizations" not in table_names:
        logger.info("SaaS migration 014: organizations missing, skipping billing offer.")
        return

    organization_columns = {row["name"] for row in inspector.get_columns("organizations")}
    if "billing_offer" in organization_columns:
        logger.info("SaaS migration 014: organizations.billing_offer already exists.")
    else:
        connection.execute(
            text(
                "ALTER TABLE organizations "
                "ADD COLUMN billing_offer VARCHAR(30) NOT NULL DEFAULT 'standard'"
            )
        )
        logger.info("SaaS migration 014: added organizations.billing_offer.")

    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_organizations_billing_offer "
            "ON organizations (billing_offer)"
        )
    )
    logger.info("SaaS migration 014: ensured organizations.billing_offer index.")
