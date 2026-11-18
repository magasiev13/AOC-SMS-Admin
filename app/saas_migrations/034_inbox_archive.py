"""Keep archived conversations and their history in the workspace."""

from sqlalchemy import inspect, text


def apply(connection, logger):
    if 'archived_at' not in {column['name'] for column in inspect(connection).get_columns('inbox_threads')}:
        connection.execute(text('ALTER TABLE inbox_threads ADD COLUMN archived_at TIMESTAMP'))
