"""Remember a missing canceled Stripe resource without changing billing authority."""
from sqlalchemy import inspect, text


def apply(connection, logger):
    table = "organization_subscriptions"
    if table not in inspect(connection).get_table_names():
        raise RuntimeError("SaaS migration 032 requires organization subscriptions.")
    columns = {column["name"] for column in inspect(connection).get_columns(table)}
    for name, kind in (
        ("stripe_missing_subscription_id", "VARCHAR(80)"),
        ("stripe_missing_observed_at", "TIMESTAMP"),
    ):
        if name not in columns:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {kind}"))
    logger.info("SaaS migration 032: missing-resource observations available; existing billing state unchanged.")
