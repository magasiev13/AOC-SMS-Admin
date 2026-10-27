"""Retain the start of a financial attempt before provider idempotency can age out."""
from sqlalchemy import inspect, text


def apply(connection, logger):
    if 'organization_usage_billing_periods' not in inspect(connection).get_table_names():
        raise RuntimeError('SaaS migration 026 requires the usage billing ledger.')
    columns = {column['name'] for column in inspect(connection).get_columns('organization_usage_billing_periods')}
    if 'settlement_started_at' not in columns:
        connection.execute(text('ALTER TABLE organization_usage_billing_periods ADD COLUMN settlement_started_at TIMESTAMP'))
    # A legacy attempt has no trustworthy first-operation timestamp. Do not invent
    # one from its mutable updated_at or its potentially much older period dates.
    logger.info('SaaS migration 026: nullable settlement start retained; existing invoice/payment identities unchanged.')
