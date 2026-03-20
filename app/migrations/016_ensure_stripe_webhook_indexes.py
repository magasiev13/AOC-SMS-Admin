from sqlalchemy import text


def apply(connection, logger) -> None:
    statements = [
        """
        CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_event_type
        ON stripe_webhook_events (event_type)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_organization_id
        ON stripe_webhook_events (organization_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_status
        ON stripe_webhook_events (status)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_stripe_object_id
        ON stripe_webhook_events (stripe_object_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_stripe_customer_id
        ON stripe_webhook_events (stripe_customer_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_stripe_subscription_id
        ON stripe_webhook_events (stripe_subscription_id)
        """,
    ]

    for statement in statements:
        connection.execute(text(statement))

    logger.info(
        "Migration 016_ensure_stripe_webhook_indexes: ensured canonical stripe webhook indexes exist."
    )
