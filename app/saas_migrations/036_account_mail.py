"""Create account-mail delivery evidence. Existing addresses require verification."""
from sqlalchemy import inspect, text
from app.models import AccountMailDelivery


def apply(connection, logger):
    for table, column in [('users', 'verified_recovery_email'), ('organization_memberships', 'reply_notice_email')]:
        if column not in {c['name'] for c in inspect(connection).get_columns(table)}:
            connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} VARCHAR(255)'))
    AccountMailDelivery.__table__.create(connection, checkfirst=True)
