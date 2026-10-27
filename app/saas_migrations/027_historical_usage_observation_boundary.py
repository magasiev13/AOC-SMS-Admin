"""Quarantine pre-existing usage observations from current customer state."""
from sqlalchemy import inspect, text


def apply(connection, logger):
    if 'messaging_usage_records' not in inspect(connection).get_table_names():
        raise RuntimeError('SaaS migration 027 requires the usage ledger.')
    columns = {column['name'] for column in inspect(connection).get_columns('messaging_usage_records')}
    if 'historical_observation_only' not in columns:
        # Every pre-existing row is protected, including rows already polled by
        # 023 and known-price rows whose billing_review_required is false. The
        # default also protects unknown/manual writers and interrupted upgrades.
        # A rerun must never relabel later, explicitly live submission rows.
        connection.execute(text(
            'ALTER TABLE messaging_usage_records ADD COLUMN '
            'historical_observation_only BOOLEAN NOT NULL DEFAULT TRUE'
        ))
    logger.info('SaaS migration 027: all existing usage remains observational; no contact, consent, period or provider state is changed.')
