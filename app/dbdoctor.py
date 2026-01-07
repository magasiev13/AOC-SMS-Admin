import argparse
import logging
import os
import stat
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from app import db
from app.config import Config
from app.migrations.runner import (
    _migration_files,
    _sqlite_db_path,
    inspect_migrations,
    run_pending_migrations,
)


def _configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _print_report(report: dict[str, list[str] | str]) -> None:
    db_path = report["db_path"]
    migrations = report["migrations"]
    applied = set(report["applied"])

    print(f"Database file: {db_path}")
    if not migrations:
        print("Migrations: none")
        return

    print("Migrations:")
    for version in migrations:
        status = "applied" if version in applied else "pending"
        print(f"  - {version}: {status}")


def _build_engine() -> Engine:
    return create_engine(
        Config.SQLALCHEMY_DATABASE_URI,
        **Config.SQLALCHEMY_ENGINE_OPTIONS,
    )


def _describe_permissions(db_path: str) -> tuple[str, list[str]]:
    issues: list[str] = []
    if db_path == "sqlite://":
        return "in-memory", issues
    if not os.path.exists(db_path):
        issues.append(
            f"Database file not found at {db_path}. Run `python -m app.dbdoctor --apply` or start the app to create it."
        )
        return "missing", issues
    if not os.path.isfile(db_path):
        issues.append(f"Database path {db_path} is not a file. Update DATABASE_URL to a file path.")
        return "not-a-file", issues
    permissions = stat.filemode(os.stat(db_path).st_mode)
    if not os.access(db_path, os.R_OK):
        issues.append(f"Database file {db_path} is not readable. Fix file permissions or ownership.")
    if not os.access(db_path, os.W_OK):
        issues.append(f"Database file {db_path} is not writable. Fix file permissions or ownership.")
    return permissions, issues


def _check_message_logs(connection) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    expected_columns = {
        "id",
        "created_at",
        "message_body",
        "target",
        "event_id",
        "status",
        "total_recipients",
        "success_count",
        "failure_count",
        "details",
    }
    inspector = inspect(connection)
    columns_info = inspector.get_columns("message_logs")
    if not columns_info:
        issues.append("Table message_logs is missing. Run `python -m app.dbdoctor --apply` to create it.")
        return [], issues
    columns = [column["name"] for column in columns_info]
    missing = sorted(expected_columns - set(columns))
    if missing:
        issues.append(
            "message_logs is missing columns: "
            + ", ".join(missing)
            + ". Run `python -m app.dbdoctor --apply` to apply migrations."
        )
    return columns, issues


def _doctor(engine: Engine) -> int:
    db_path = _sqlite_db_path(engine)
    migrations = [migration.version for migration in _migration_files()]
    applied: set[str] = set()
    pending: list[str] = []
    unexpected: list[str] = []
    issues: list[str] = []

    can_connect = True
    if engine.url.drivername.startswith("sqlite"):
        permissions, permission_issues = _describe_permissions(db_path)
        issues.extend(permission_issues)
        if permissions in {"missing", "not-a-file"}:
            can_connect = False
    else:
        permissions = "n/a (non-sqlite database)"

    sqlite_version = "n/a"
    message_log_columns: list[str] = []
    if can_connect:
        report = inspect_migrations(engine)
        applied = set(report["applied"])
        pending = [version for version in migrations if version not in applied]
        unexpected = sorted(applied - set(migrations))
        with engine.connect() as connection:
            if engine.url.drivername.startswith("sqlite"):
                sqlite_version = connection.execute(text("select sqlite_version()")).scalar_one()
            message_log_columns, message_log_issues = _check_message_logs(connection)
            issues.extend(message_log_issues)
    else:
        pending = migrations
        sqlite_version = "unknown (database missing)"
        issues.append("Unable to inspect tables until the database file exists.")

    if pending:
        issues.append(
            "Pending migrations detected: "
            + ", ".join(pending)
            + ". Run `python -m app.dbdoctor --apply` to apply them."
        )
    if unexpected:
        issues.append(
            "Unexpected applied migrations not found on disk: "
            + ", ".join(unexpected)
            + ". Ensure the migrations directory matches the database state."
        )

    print(f"Database file: {db_path}")
    print(f"File perms: {permissions}")
    print(f"SQLite version: {sqlite_version}")
    if migrations:
        print(
            "Schema migrations: "
            f"{len(applied)}/{len(migrations)} applied"
            + (f", pending: {', '.join(pending)}" if pending else ", pending: none")
        )
    else:
        print("Schema migrations: none")
    if message_log_columns:
        print("message_logs columns: " + ", ".join(message_log_columns))
    else:
        print("message_logs columns: missing")

    if issues:
        for issue in issues:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or apply database migrations.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print migration status and database path.",
    )
    action.add_argument(
        "--apply",
        action="store_true",
        help="Apply any pending migrations.",
    )
    action.add_argument(
        "--doctor",
        action="store_true",
        help="Check database health and exit non-zero if issues are detected.",
    )
    args = parser.parse_args()

    _configure_logging()
    engine = _build_engine()
    logger = logging.getLogger(__name__)

    if args.apply:
        from app import models  # noqa: F401

        db.metadata.create_all(bind=engine)
        run_pending_migrations(engine, logger)

    if args.print_only:
        report = inspect_migrations(engine)
        _print_report(report)

    if args.doctor:
        sys.exit(_doctor(engine))


if __name__ == "__main__":
    main()
