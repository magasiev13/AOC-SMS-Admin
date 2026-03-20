from sqlalchemy import Column, DateTime, Index, Integer, MetaData, String, Table, Text


def apply(connection, logger) -> None:
    metadata = MetaData()
    Table(
        "saas_import_runs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("source_db_path", Text, nullable=False),
        Column("organization_id", Integer, nullable=True),
        Column("status", String(20), nullable=False, default="processing"),
        Column("row_counts_json", Text, nullable=True),
        Column("started_at", DateTime, nullable=False),
        Column("completed_at", DateTime, nullable=True),
        Column("error_message", Text, nullable=True),
        Index("ix_saas_import_runs_organization_id", "organization_id"),
        Index("ix_saas_import_runs_status", "status"),
    )
    metadata.create_all(bind=connection)
    logger.info("SaaS migration 002: ensured saas_import_runs audit table.")
