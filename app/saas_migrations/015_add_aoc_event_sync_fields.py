from sqlalchemy import inspect, text


EVENT_COLUMNS = (
    ("external_source", "ALTER TABLE events ADD COLUMN external_source VARCHAR(50)"),
    ("external_event_id", "ALTER TABLE events ADD COLUMN external_event_id VARCHAR(80)"),
    ("external_post_id", "ALTER TABLE events ADD COLUMN external_post_id VARCHAR(80)"),
    ("external_slug", "ALTER TABLE events ADD COLUMN external_slug VARCHAR(200)"),
    ("external_url", "ALTER TABLE events ADD COLUMN external_url TEXT"),
    ("external_status", "ALTER TABLE events ADD COLUMN external_status VARCHAR(30)"),
    ("external_start_at", "ALTER TABLE events ADD COLUMN external_start_at TIMESTAMP"),
    ("external_end_at", "ALTER TABLE events ADD COLUMN external_end_at TIMESTAMP"),
    ("external_timezone", "ALTER TABLE events ADD COLUMN external_timezone VARCHAR(80)"),
    ("external_modified_at", "ALTER TABLE events ADD COLUMN external_modified_at TIMESTAMP"),
    ("location_name", "ALTER TABLE events ADD COLUMN location_name VARCHAR(200)"),
    ("location_address", "ALTER TABLE events ADD COLUMN location_address VARCHAR(200)"),
    ("location_town", "ALTER TABLE events ADD COLUMN location_town VARCHAR(120)"),
    ("location_state", "ALTER TABLE events ADD COLUMN location_state VARCHAR(80)"),
    ("location_postcode", "ALTER TABLE events ADD COLUMN location_postcode VARCHAR(20)"),
    ("location_country", "ALTER TABLE events ADD COLUMN location_country VARCHAR(2)"),
    ("rsvp_enabled", "ALTER TABLE events ADD COLUMN rsvp_enabled BOOLEAN"),
    ("capacity", "ALTER TABLE events ADD COLUMN capacity INTEGER"),
    ("synced_at", "ALTER TABLE events ADD COLUMN synced_at TIMESTAMP"),
)

EVENT_REGISTRATION_COLUMNS = (
    ("external_source", "ALTER TABLE event_registrations ADD COLUMN external_source VARCHAR(50)"),
    ("external_booking_id", "ALTER TABLE event_registrations ADD COLUMN external_booking_id VARCHAR(80)"),
    ("external_person_id", "ALTER TABLE event_registrations ADD COLUMN external_person_id VARCHAR(80)"),
    ("external_booking_status", "ALTER TABLE event_registrations ADD COLUMN external_booking_status VARCHAR(30)"),
    ("booking_spaces", "ALTER TABLE event_registrations ADD COLUMN booking_spaces INTEGER"),
    ("external_updated_at", "ALTER TABLE event_registrations ADD COLUMN external_updated_at TIMESTAMP"),
    ("synced_at", "ALTER TABLE event_registrations ADD COLUMN synced_at TIMESTAMP"),
)

SCHEDULED_MESSAGE_COLUMNS = (
    ("automation_source", "ALTER TABLE scheduled_messages ADD COLUMN automation_source VARCHAR(50)"),
    ("automation_key", "ALTER TABLE scheduled_messages ADD COLUMN automation_key VARCHAR(160)"),
    ("automation_kind", "ALTER TABLE scheduled_messages ADD COLUMN automation_kind VARCHAR(40)"),
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS ix_events_external_source ON events (external_source)",
    "CREATE INDEX IF NOT EXISTS ix_events_external_event_id ON events (external_event_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_external_post_id ON events (external_post_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_external_status ON events (external_status)",
    "CREATE INDEX IF NOT EXISTS ix_events_external_start_at ON events (external_start_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_events_org_external_source_event ON events (organization_id, external_source, external_event_id)",
    "CREATE INDEX IF NOT EXISTS ix_event_registrations_external_source ON event_registrations (external_source)",
    "CREATE INDEX IF NOT EXISTS ix_event_registrations_external_booking_id ON event_registrations (external_booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_event_registrations_external_person_id ON event_registrations (external_person_id)",
    "CREATE INDEX IF NOT EXISTS ix_event_registrations_external_booking_status ON event_registrations (external_booking_status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_event_registrations_org_external_booking ON event_registrations (organization_id, external_source, external_booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_scheduled_messages_automation_source ON scheduled_messages (automation_source)",
    "CREATE INDEX IF NOT EXISTS ix_scheduled_messages_automation_key ON scheduled_messages (automation_key)",
    "CREATE INDEX IF NOT EXISTS ix_scheduled_messages_automation_kind ON scheduled_messages (automation_kind)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_scheduled_messages_org_automation_key ON scheduled_messages (organization_id, automation_source, automation_key)",
)


def _table_exists(connection, table_name: str) -> bool:
    return table_name in inspect(connection).get_table_names()


def _column_names(connection, table_name: str) -> set[str]:
    if not _table_exists(connection, table_name):
        return set()
    return {column["name"] for column in inspect(connection).get_columns(table_name)}


def _ensure_column(connection, table_name: str, column_name: str, statement: str, logger) -> None:
    columns = _column_names(connection, table_name)
    if not columns:
        logger.info("SaaS migration 015: table %s missing, skipping column %s.", table_name, column_name)
        return
    if column_name in columns:
        logger.info("SaaS migration 015: %s.%s already exists.", table_name, column_name)
        return
    connection.execute(text(statement))
    logger.info("SaaS migration 015: added %s.%s.", table_name, column_name)


def apply(connection, logger) -> None:
    for column_name, statement in EVENT_COLUMNS:
        _ensure_column(connection, "events", column_name, statement, logger)
    for column_name, statement in EVENT_REGISTRATION_COLUMNS:
        _ensure_column(connection, "event_registrations", column_name, statement, logger)
    for column_name, statement in SCHEDULED_MESSAGE_COLUMNS:
        _ensure_column(connection, "scheduled_messages", column_name, statement, logger)

    existing_tables = set(inspect(connection).get_table_names())
    if {"events", "event_registrations", "scheduled_messages"}.issubset(existing_tables):
        for statement in INDEX_STATEMENTS:
            connection.execute(text(statement))
        logger.info("SaaS migration 015: ensured AOC event sync indexes.")
    else:
        logger.info("SaaS migration 015: skipped indexes because required tables are missing.")
