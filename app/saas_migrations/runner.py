from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url


SAAS_MIGRATIONS_TABLE = "saas_schema_migrations"
REQUIRED_TABLES = (
    "users",
    "organizations",
    "organization_memberships",
    "organization_subscriptions",
    "organization_messaging_profiles",
    "organization_provider_audit_logs",
    "messaging_usage_records",
    "organization_usage_billing_periods",
    "platform_service_restart_requests",
    "community_members",
    "event_registrations",
    "message_logs",
    "scheduled_messages",
    "stripe_webhook_events",
    "saas_import_runs",
)


@dataclass(frozen=True)
class SaaSMigration:
    version: str
    name: str
    path: Path


def _migration_files() -> list[SaaSMigration]:
    migrations_dir = Path(__file__).resolve().parent
    migrations = []
    for path in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.py")):
        version = path.name.split("_", 1)[0]
        migrations.append(SaaSMigration(version=version, name=path.stem, path=path))
    return migrations


def _load_migration(migration: SaaSMigration):
    spec = importlib.util.spec_from_file_location(f"app.saas_migrations.{migration.name}", migration.path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Unable to load SaaS migration module {migration.path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine_label(engine: Engine) -> str:
    url = make_url(str(engine.url))
    if url.drivername.startswith("sqlite"):
        if url.database:
            return str(Path(url.database).expanduser().resolve())
        return "sqlite://"
    return str(engine.url)


def _ensure_migrations_table(connection) -> None:
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {SAAS_MIGRATIONS_TABLE} (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
    )


def _applied_versions(connection) -> set[str]:
    inspector = inspect(connection)
    if SAAS_MIGRATIONS_TABLE not in inspector.get_table_names():
        return set()

    rows = connection.execute(text(f"SELECT version FROM {SAAS_MIGRATIONS_TABLE}"))
    return {row._mapping["version"] for row in rows}


def inspect_saas_migrations(engine: Engine) -> dict[str, list[str] | str]:
    migrations = _migration_files()
    with engine.connect() as connection:
        applied = sorted(_applied_versions(connection))
        tables = set(inspect(connection).get_table_names())

    pending = [migration.version for migration in migrations if migration.version not in applied]
    missing_tables = [table for table in REQUIRED_TABLES if table not in tables]
    return {
        "db_label": _engine_label(engine),
        "migrations": [migration.version for migration in migrations],
        "applied": applied,
        "pending": pending,
        "missing_tables": missing_tables,
    }


def run_pending_saas_migrations(
    engine: Engine,
    logger,
    *,
    target_version: str | None = None,
) -> list[str]:
    migrations = _migration_files()
    applied_now: list[str] = []

    with engine.begin() as connection:
        _ensure_migrations_table(connection)
        applied_versions = _applied_versions(connection)

        for migration in migrations:
            if target_version and migration.version > target_version:
                break
            if migration.version in applied_versions:
                logger.info("SaaS migration %s already applied. Skipping.", migration.version)
                continue

            module = _load_migration(migration)
            logger.info("Applying SaaS migration %s (%s).", migration.version, migration.name)
            module.apply(connection, logger)
            connection.execute(
                text(
                    f"""
                    INSERT INTO {SAAS_MIGRATIONS_TABLE} (version, applied_at)
                    VALUES (:version, :applied_at)
                    """
                ),
                {
                    "version": migration.version,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            applied_now.append(migration.version)

    if applied_now:
        logger.info("Applied SaaS migrations: %s", ", ".join(applied_now))
    else:
        logger.info("No pending SaaS migrations.")
    return applied_now


def ensure_saas_schema_ready(engine: Engine) -> dict[str, list[str] | str]:
    report = inspect_saas_migrations(engine)
    pending = report["pending"]
    missing_tables = report["missing_tables"]
    if pending or missing_tables:
        issues: list[str] = []
        if pending:
            issues.append(
                "pending SaaS migrations: " + ", ".join(pending)
            )
        if missing_tables:
            issues.append(
                "missing required tables: " + ", ".join(missing_tables)
            )
        detail = "; ".join(issues)
        raise RuntimeError(
            "SaaS schema is not ready for this database "
            f"({report['db_label']}): {detail}. "
            "Run `python -m app.saas_db --apply` before starting the app."
        )
    return report
