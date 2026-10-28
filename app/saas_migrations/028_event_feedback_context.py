"""Separate event feedback purpose and freeze response-session context."""
from sqlalchemy import inspect, text


COLUMNS = {
    'survey_flows': (
        ('purpose', "VARCHAR(24) NOT NULL DEFAULT 'survey'"),
        ('feedback_event_id', 'INTEGER REFERENCES events(id) ON DELETE SET NULL'),
    ),
    'survey_sessions': (
        ('purpose', "VARCHAR(24) NOT NULL DEFAULT 'survey'"),
        ('feedback_event_id', 'INTEGER'),
        ('feedback_event_title', 'VARCHAR(200)'),
        ('feedback_questions_json', 'TEXT'),
        ('feedback_completion_message', 'TEXT'),
    ),
}


def apply(connection, logger):
    tables = set(inspect(connection).get_table_names())
    if not set(COLUMNS) <= tables:
        raise RuntimeError('Event feedback migration requires the existing survey tables.')
    for table, additions in COLUMNS.items():
        columns = {column['name'] for column in inspect(connection).get_columns(table)}
        for name, definition in additions:
            if name not in columns:
                connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
        connection.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_{table}_feedback_event_id ON {table} (feedback_event_id)'
        ))
    logger.info('Event feedback context added. Existing surveys/sessions retain survey purpose; registrations, names and stored responses were not rewritten.')
