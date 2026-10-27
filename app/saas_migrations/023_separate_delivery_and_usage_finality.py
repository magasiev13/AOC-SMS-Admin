"""Reopen old incomplete facts without changing submission or billing evidence."""
from sqlalchemy import inspect, text


def apply(connection, logger):
    if 'messaging_usage_records' not in inspect(connection).get_table_names():
        raise RuntimeError('SaaS migration 023 requires the usage ledger.')
    existing = {column['name'] for column in inspect(connection).get_columns('messaging_usage_records')}
    columns = {
        'delivery_status': "VARCHAR(30) NOT NULL DEFAULT 'pending'",
        'delivery_error_code': 'VARCHAR(32)', 'delivery_error_message': 'TEXT',
        'delivery_finalized_at': 'TIMESTAMP', 'delivery_expires_at': 'TIMESTAMP',
        'delivery_updated_at': 'TIMESTAMP',
        'delivery_evidence_json': 'TEXT',
        'cost_status': "VARCHAR(20) NOT NULL DEFAULT 'pending'", 'cost_finalized_at': 'TIMESTAMP',
        'cost_expires_at': 'TIMESTAMP', 'units_status': "VARCHAR(20) NOT NULL DEFAULT 'pending'",
        'next_reconcile_at': 'TIMESTAMP', 'reconcile_attempt_count': 'INTEGER NOT NULL DEFAULT 0',
        'suppression_applied_at': 'TIMESTAMP',
        'billing_review_required': 'BOOLEAN NOT NULL DEFAULT FALSE',
    }
    newly_added = set(columns) - existing
    for name in newly_added:
        connection.execute(text(f'ALTER TABLE messaging_usage_records ADD COLUMN {name} {columns[name]}'))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_messaging_usage_records_next_reconcile_at ON messaging_usage_records (next_reconcile_at)'))
    # An unobserved row is recognizable even after an interrupted compatibility
    # schema upgrade. Mark it once; rerunning must not erase later review work.
    legacy = "delivery_evidence_json IS NULL AND delivery_expires_at IS NULL AND cost_expires_at IS NULL"
    connection.execute(text(f"UPDATE messaging_usage_records SET cost_status = 'known', cost_finalized_at = reconciled_at WHERE {legacy} AND provider_cost > 0 AND cost_status = 'pending'"))
    connection.execute(text(f"UPDATE messaging_usage_records SET units_status = 'known' WHERE {legacy} AND billable_units > 0 AND units_status = 'pending'"))
    connection.execute(text(f"UPDATE messaging_usage_records SET billing_review_required = TRUE WHERE {legacy} AND (cost_status <> 'known' OR units_status <> 'known')"))
    connection.execute(text(f"UPDATE messaging_usage_records SET delivery_status = twilio_message_status, delivery_finalized_at = reconciled_at WHERE {legacy} AND twilio_message_status IN ('delivered','read','undelivered','failed','canceled') AND delivery_status = 'pending'"))
    connection.execute(text(f"UPDATE messaging_usage_records SET reconciliation_status = 'pending', next_reconcile_at = NULL WHERE {legacy} AND reconciliation_status = 'finalized' AND (twilio_message_status = 'sent' OR cost_status <> 'known' OR units_status <> 'known')"))
    connection.execute(text(f"UPDATE messaging_usage_records SET delivery_evidence_json = '[]' WHERE {legacy}"))
    logger.info('SaaS migration 023: separate delivery, cost and units completeness; original attempts and customer billing evidence retained.')
