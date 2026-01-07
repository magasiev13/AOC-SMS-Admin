import argparse
import logging

from app import create_app, db
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
    app = create_app(run_startup_tasks=False, start_scheduler=False)

    with app.app_context():
        if args.apply:
            db.create_all()
            run_pending_migrations(db.engine, app.logger)

        if args.print_only:
            report = inspect_migrations(db.engine)
            _print_report(report)


if __name__ == "__main__":
    main()
