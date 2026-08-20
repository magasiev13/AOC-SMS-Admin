from sqlalchemy import inspect, text


def apply(connection, logger) -> None:
    tables = set(inspect(connection).get_table_names())
    if "scheduled_messages" not in tables:
        logger.info(
            "SaaS migration 019: scheduled_messages missing, skipping request idempotency."
        )
        return

    columns = {
        column["name"]
        for column in inspect(connection).get_columns("scheduled_messages")
    }
    if "request_idempotency_key" not in columns:
        connection.execute(
            text(
                "ALTER TABLE scheduled_messages "
                "ADD COLUMN request_idempotency_key VARCHAR(64)"
            )
        )
        logger.info(
            "SaaS migration 019: added scheduled_messages.request_idempotency_key."
        )

    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ux_scheduled_messages_org_request_key "
            "ON scheduled_messages (organization_id, request_idempotency_key)"
        )
    )
    logger.info("SaaS migration 019: ensured scheduled-send request idempotency index.")
