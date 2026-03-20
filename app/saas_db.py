from __future__ import annotations

import argparse
import json
import logging
import sys

from sqlalchemy import create_engine, inspect

from app.config import Config
from app.saas_migrations.runner import ensure_saas_schema_ready, inspect_saas_migrations, run_pending_saas_migrations
from app.services.legacy_import_service import import_legacy_sqlite_snapshot


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_engine():
    return create_engine(
        Config.SQLALCHEMY_DATABASE_URI,
        **Config.SQLALCHEMY_ENGINE_OPTIONS,
    )


def _print_report(report: dict[str, list[str] | str]) -> None:
    print(f"Database: {report['db_label']}")
    print("SaaS migrations:")
    for version in report["migrations"]:
        status = "applied" if version in set(report["applied"]) else "pending"
        print(f"  - {version}: {status}")
    missing_tables = report["missing_tables"]
    if missing_tables:
        print("Missing required tables: " + ", ".join(missing_tables))


def _doctor(engine) -> int:
    report = inspect_saas_migrations(engine)
    _print_report(report)
    issues: list[str] = []
    if report["pending"]:
        issues.append("Pending SaaS migrations: " + ", ".join(report["pending"]))
    if report["missing_tables"]:
        issues.append("Missing required tables: " + ", ".join(report["missing_tables"]))

    if not inspect(engine).get_table_names():
        issues.append("Target database is empty. Run `python -m app.saas_db --apply` first.")

    if issues:
        for issue in issues:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage explicit SaaS schema and import workflows.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--print", dest="print_only", action="store_true", help="Print SaaS migration status.")
    action.add_argument("--apply", action="store_true", help="Apply pending SaaS migrations.")
    action.add_argument("--doctor", action="store_true", help="Validate SaaS schema readiness.")
    action.add_argument(
        "--import-legacy",
        metavar="LEGACY_SQLITE_PATH",
        help="Import a legacy SQLite snapshot into the SaaS database.",
    )
    parser.add_argument("--organization-name", default="Legacy Production", help="Default imported organization name.")
    parser.add_argument("--organization-slug", default="legacy-production", help="Default imported organization slug.")
    args = parser.parse_args()

    _configure_logging()
    logger = logging.getLogger(__name__)
    engine = _build_engine()

    if args.apply:
        run_pending_saas_migrations(engine, logger)

    if args.print_only:
        _print_report(inspect_saas_migrations(engine))

    if args.doctor:
        sys.exit(_doctor(engine))

    if args.import_legacy:
        run_pending_saas_migrations(engine, logger)
        ensure_saas_schema_ready(engine)
        summary = import_legacy_sqlite_snapshot(
            legacy_db_path=args.import_legacy,
            organization_name=args.organization_name,
            organization_slug=args.organization_slug,
            logger=logger,
        )
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
