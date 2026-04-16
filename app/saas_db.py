from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from sqlalchemy import create_engine, func, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

from app.config import Config
from app.saas_migrations.runner import ensure_saas_schema_ready, inspect_saas_migrations, run_pending_saas_migrations
from app.services.legacy_import_service import (
    import_legacy_sqlite_snapshot,
    import_legacy_sqlite_snapshot_into_new_org,
    sync_legacy_sqlite_snapshot_into_existing_org,
)


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
    warnings: list[str] = []
    if report["pending"]:
        issues.append("Pending SaaS migrations: " + ", ".join(report["pending"]))
    if report["missing_tables"]:
        issues.append("Missing required tables: " + ", ".join(report["missing_tables"]))

    if not inspect(engine).get_table_names():
        issues.append("Target database is empty. Run `python -m app.saas_db --apply` first.")

    inspector = inspect(engine)
    if "organization_a2p_onboardings" in inspector.get_table_names():
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                """
                SELECT onboarding_status, COUNT(*)
                FROM organization_a2p_onboardings
                GROUP BY onboarding_status
                """
            ).fetchall()
        if rows:
            status_summary = ", ".join(f"{status}={count}" for status, count in rows)
            print(f"A2P onboarding: {status_summary}")
        problematic_statuses = {"error", "needs_action", "rejected"}
        if any(status in problematic_statuses for status, _count in rows):
            warnings.append(
                "A2P onboarding has records requiring attention: "
                + ", ".join(f"{status}={count}" for status, count in rows if status in problematic_statuses)
            )

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    if issues:
        for issue in issues:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    return 0


def ensure_platform_admin(engine, logger) -> bool:
    if not Config.SAAS_MODE:
        raise RuntimeError("SAAS_MODE must be set to 1 before provisioning a SaaS platform admin.")

    ensure_saas_schema_ready(engine)

    from app.models import AppUser

    session = sessionmaker(bind=engine)()
    try:
        existing_platform_admin = (
            session.query(AppUser)
            .filter_by(is_platform_admin=True)
            .order_by(AppUser.id.asc())
            .first()
        )
        if existing_platform_admin is not None:
            logger.info(
                "Platform admin user %s already exists; skipping bootstrap provisioning.",
                existing_platform_admin.username,
            )
            return False

        admin_username = (Config.ADMIN_USERNAME or "admin").strip() or "admin"
        admin_email = (Config.ADMIN_EMAIL or f"{admin_username}@example.com").strip().lower() or None
        admin_password = Config.ADMIN_PASSWORD
        if not admin_password:
            raise RuntimeError(
                "ADMIN_PASSWORD must be set to provision the first SaaS platform admin."
            )

        username_conflict = (
            session.query(AppUser)
            .filter(func.lower(AppUser.username) == admin_username.lower())
            .first()
        )
        if username_conflict is not None:
            if username_conflict.is_platform_admin:
                logger.info(
                    "Platform admin user %s already exists; skipping bootstrap provisioning.",
                    username_conflict.username,
                )
                return False
            raise RuntimeError(
                f"Configured ADMIN_USERNAME {admin_username!r} conflicts with an existing non-platform user."
            )

        if admin_email:
            email_conflict = (
                session.query(AppUser)
                .filter(func.lower(AppUser.email) == admin_email.lower())
                .first()
            )
            if email_conflict is not None:
                raise RuntimeError(
                    f"Configured ADMIN_EMAIL {admin_email!r} conflicts with an existing user."
                )

        password_hash = admin_password
        if not admin_password.startswith(("pbkdf2:", "scrypt:")):
            password_hash = generate_password_hash(admin_password, method="pbkdf2:sha256")

        session.add(
            AppUser(
                username=admin_username,
                email=admin_email,
                role="admin",
                is_platform_admin=True,
                password_hash=password_hash,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing_platform_admin = (
                session.query(AppUser)
                .filter_by(is_platform_admin=True)
                .order_by(AppUser.id.asc())
                .first()
            )
            if existing_platform_admin is None:
                raise
            logger.info(
                "Platform admin user %s already exists; another process created it.",
                existing_platform_admin.username,
            )
            return False

        logger.info("Created SaaS platform admin user %s.", admin_username)
        return True
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage explicit SaaS schema and import workflows.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--print", dest="print_only", action="store_true", help="Print SaaS migration status.")
    action.add_argument("--apply", action="store_true", help="Apply pending SaaS migrations.")
    action.add_argument("--doctor", action="store_true", help="Validate SaaS schema readiness.")
    action.add_argument(
        "--ensure-platform-admin",
        action="store_true",
        help="Create the first SaaS platform admin from ADMIN_* env values if needed.",
    )
    action.add_argument(
        "--import-legacy",
        metavar="LEGACY_SQLITE_PATH",
        help="Import a legacy SQLite snapshot into the SaaS database.",
    )
    action.add_argument(
        "--import-legacy-into-org",
        metavar="LEGACY_SQLITE_PATH",
        help="Import a legacy SQLite snapshot into a newly created org inside an existing SaaS database.",
    )
    action.add_argument(
        "--sync-legacy-into-org",
        metavar="LEGACY_SQLITE_PATH",
        help="Idempotently sync a legacy SQLite snapshot into an existing SaaS org.",
    )
    parser.add_argument("--organization-name", default="Legacy Production", help="Default imported organization name.")
    parser.add_argument("--organization-slug", default="legacy-production", help="Default imported organization slug.")
    parser.add_argument(
        "--subscription-status",
        default="incomplete",
        help="Subscription status to apply when importing into a new org.",
    )
    parser.add_argument(
        "--provider-mode",
        default="platform_managed",
        help="Messaging provider mode to apply when importing into a new org.",
    )
    parser.add_argument(
        "--username-remap",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Rename an imported legacy username to avoid conflicts. May be provided more than once.",
    )
    parser.add_argument(
        "--since",
        help="Baseline timestamp for legacy syncs in ISO-8601 format. Defaults to the latest completed import for the org.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the legacy sync result inside a transaction and roll it back before exiting.",
    )
    args = parser.parse_args()

    _configure_logging()
    logger = logging.getLogger(__name__)
    engine = _build_engine()

    try:
        if args.apply:
            run_pending_saas_migrations(engine, logger)

        if args.print_only:
            _print_report(inspect_saas_migrations(engine))

        if args.doctor:
            sys.exit(_doctor(engine))

        if args.ensure_platform_admin:
            ensure_platform_admin(engine, logger)
            return

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

        if args.import_legacy_into_org:
            run_pending_saas_migrations(engine, logger)
            ensure_saas_schema_ready(engine)
            username_remaps: dict[str, str] = {}
            for mapping in args.username_remap:
                source, separator, target = (mapping or "").partition("=")
                if not separator:
                    raise RuntimeError(
                        f"Invalid --username-remap value {mapping!r}. Use OLD=NEW."
                    )
                source = source.strip()
                target = target.strip()
                if not source or not target:
                    raise RuntimeError(
                        f"Invalid --username-remap value {mapping!r}. Use OLD=NEW."
                    )
                username_remaps[source] = target
            summary = import_legacy_sqlite_snapshot_into_new_org(
                legacy_db_path=args.import_legacy_into_org,
                organization_name=args.organization_name,
                organization_slug=args.organization_slug,
                subscription_status=args.subscription_status,
                provider_mode=args.provider_mode,
                username_remaps=username_remaps,
                logger=logger,
            )
            print(json.dumps(summary, indent=2, sort_keys=True, default=str))

        if args.sync_legacy_into_org:
            run_pending_saas_migrations(engine, logger)
            ensure_saas_schema_ready(engine)
            username_remaps: dict[str, str] = {}
            for mapping in args.username_remap:
                source, separator, target = (mapping or "").partition("=")
                if not separator:
                    raise RuntimeError(
                        f"Invalid --username-remap value {mapping!r}. Use OLD=NEW."
                    )
                source = source.strip()
                target = target.strip()
                if not source or not target:
                    raise RuntimeError(
                        f"Invalid --username-remap value {mapping!r}. Use OLD=NEW."
                    )
                username_remaps[source] = target
            since = None
            if args.since:
                normalized_since = args.since.strip()
                if not normalized_since:
                    raise RuntimeError("Invalid --since value. Use an ISO-8601 timestamp.")
                try:
                    since = datetime.fromisoformat(normalized_since.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise RuntimeError("Invalid --since value. Use an ISO-8601 timestamp.") from exc
            summary = sync_legacy_sqlite_snapshot_into_existing_org(
                legacy_db_path=args.sync_legacy_into_org,
                organization_slug=args.organization_slug,
                username_remaps=username_remaps,
                since=since,
                dry_run=args.dry_run,
                logger=logger,
            )
            print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
