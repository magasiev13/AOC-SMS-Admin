from sqlalchemy import text


def apply(connection, logger) -> None:
    duplicate_indexes = [
        "ix_stripe_webhook_events_object_id",
        "ix_stripe_webhook_events_customer_id",
        "ix_stripe_webhook_events_subscription_id",
    ]

    for index_name in duplicate_indexes:
        connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))

    logger.info(
        "Migration 015_drop_duplicate_stripe_webhook_indexes: dropped legacy duplicate stripe webhook indexes."
    )
