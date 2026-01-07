import argparse
import logging

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app import db
from app.config import Config
from app.migrations.runner import inspect_migrations, run_pending_migrations


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or apply database migrations.")
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print migration status and database path.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply any pending migrations.",
    )
    args = parser.parse_args()

    if not args.print_only and not args.apply:
        parser.error("Specify --print or --apply")

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


if __name__ == "__main__":
    main()
