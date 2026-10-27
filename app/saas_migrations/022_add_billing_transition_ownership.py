"""Persist one organization-owned Checkout request before provider operations."""
from sqlalchemy import inspect, text


def apply(connection, logger) -> None:
    definitions = {
        "organization_subscriptions": {
            "checkout_request_key": "VARCHAR(96)",
            "checkout_request_payload_json": "TEXT",
            "checkout_request_started_at": "TIMESTAMP",
            "checkout_request_status": "VARCHAR(24)",
            "checkout_request_session_id": "VARCHAR(80)",
            "billing_incident_code": "VARCHAR(64)",
            "billing_incident_detail": "TEXT",
        },
        "stripe_checkout_sessions": {"transition_key": "VARCHAR(96)"},
    }
    for table, columns in definitions.items():
        if table not in inspect(connection).get_table_names():
            raise RuntimeError(f"SaaS migration 022 requires {table}.")
        present = {column["name"] for column in inspect(connection).get_columns(table)}
        for name, sql_type in columns.items():
            if name not in present:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_stripe_checkout_sessions_transition_key "
        "ON stripe_checkout_sessions (transition_key)"
    ))
    logger.info("SaaS migration 022: durable Checkout ownership available; prior billing and payment records retained.")
