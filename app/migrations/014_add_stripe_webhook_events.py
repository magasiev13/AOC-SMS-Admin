from sqlalchemy import text


def apply(connection, logger) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS stripe_webhook_events (
                id INTEGER PRIMARY KEY,
                stripe_event_id VARCHAR(80) NOT NULL UNIQUE,
                event_type VARCHAR(80) NOT NULL,
                stripe_object_id VARCHAR(80),
                stripe_customer_id VARCHAR(80),
                stripe_subscription_id VARCHAR(80),
                organization_id INTEGER,
                signature_verified BOOLEAN NOT NULL DEFAULT 0,
                event_created_at DATETIME,
                received_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL,
                processed_at DATETIME,
                status VARCHAR(20) NOT NULL DEFAULT 'processing',
                attempt_count INTEGER NOT NULL DEFAULT 1,
                last_error TEXT,
                FOREIGN KEY(organization_id) REFERENCES organizations (id)
            )
            """
        )
    )
    logger.info(
        "Migration 014_add_stripe_webhook_events: ensured stripe webhook ledger table exists."
    )
