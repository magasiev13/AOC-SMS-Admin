from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url


MIGRATIONS_TABLE = "schema_migrations"
LOCK_TABLE = "schema_migration_lock"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path


def _migration_files() -> list[Migration]:
    migrations_dir = Path(__file__).resolve().parent
    migrations = []
    for path in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.py")):
        version = path.name.split("_", 1)[0]
        migrations.append(Migration(version=version, name=path.stem, path=path))
    return migrations


def _load_migration(migration: Migration):
    spec = importlib.util.spec_from_file_location(f"app.migrations.{migration.name}", migration.path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Unable to load migration module {migration.path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ensure_migrations_tables(connection) -> None:
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {LOCK_TABLE} (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                acquired_at TEXT NOT NULL
            )
            """
        )
    )


def _get_applied_versions(connection) -> set[str]:
    result = connection.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:table_name"
        ),
        {"table_name": MIGRATIONS_TABLE},
    )
    if result.first() is None:
        return set()

    rows = connection.execute(text(f"SELECT version FROM {MIGRATIONS_TABLE}"))
    return {row._mapping["version"] for row in rows}


def _sqlite_db_path(engine: Engine) -> str:
    url = make_url(str(engine.url))
    if url.drivername.startswith("sqlite"):
        if url.database:
            return str(Path(url.database).expanduser().resolve())
        return "sqlite://"
    return str(engine.url)


def inspect_migrations(engine: Engine) -> dict[str, list[str] | str]:
    migrations = _migration_files()
    applied = []
    with engine.connect() as connection:
        applied = sorted(_get_applied_versions(connection))
    return {
        "db_path": _sqlite_db_path(engine),
        "migrations": [migration.version for migration in migrations],
        "applied": applied,
    }


def run_pending_migrations(engine: Engine, logger) -> list[str]:
    migrations = _migration_files()
    db_path = _sqlite_db_path(engine)
    logger.info("Database file in use: %s", db_path)

    if not migrations:
        logger.info("No migrations found. Skipping migration runner.")
        return []

    applied_versions: set[str] = set()
    applied_now: list[str] = []

    with engine.connect() as connection:
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            _ensure_migrations_tables(connection)
            lock_time = datetime.now(timezone.utc).isoformat()
            connection.execute(
                text(
                    f"INSERT OR REPLACE INTO {LOCK_TABLE} (id, acquired_at) VALUES (1, :acquired_at)"
                ),
                {"acquired_at": lock_time},
            )

            applied_versions = _get_applied_versions(connection)

            for migration in migrations:
                if migration.version in applied_versions:
                    logger.info("Migration %s already applied. Skipping.", migration.version)
                    continue

                module = _load_migration(migration)
                logger.info("Applying migration %s (%s).", migration.version, migration.name)
                module.apply(connection, logger)
                connection.execute(
                    text(
                        f"INSERT INTO {MIGRATIONS_TABLE} (version, applied_at) VALUES (:version, :applied_at)"
                    ),
                    {
                        "version": migration.version,
                        "applied_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                applied_now.append(migration.version)

            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception(
                "Database migrations failed. Next steps: run `python -m app.dbdoctor --apply` after resolving the issue."
            )
            raise

    if applied_now:
        logger.info("Applied migrations: %s", ", ".join(applied_now))
    else:
        logger.info("No pending migrations to apply.")

    return applied_now
