from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Flask
from redis.exceptions import RedisError
from rq import Worker
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import Organization, ScheduledMessage
from app.queue import get_queue, get_redis_connection
from app.saas_migrations.runner import inspect_saas_migrations


SYSTEMD_UNIT_PATTERN = re.compile(r"^[a-zA-Z0-9_.@-]+\.timer$")


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ready: bool
    detail: str


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    checked_at: str
    release_id: str
    checks: tuple[ReadinessCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checked_at": self.checked_at,
            "release_id": self.release_id,
            "checks": [asdict(check) for check in self.checks],
        }


def _database_check() -> ReadinessCheck:
    driver_name = str(db.engine.url.drivername)
    check_name = "postgresql" if driver_name.startswith("postgresql") else "database"
    try:
        value = db.session.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError as exc:
        db.session.rollback()
        return ReadinessCheck(check_name, False, f"database query failed: {exc}")
    return ReadinessCheck(check_name, value == 1, f"{driver_name} query completed")


def _migration_check() -> ReadinessCheck:
    try:
        report = inspect_saas_migrations(db.engine)
    except (SQLAlchemyError, RuntimeError, OSError) as exc:
        return ReadinessCheck("migrations", False, f"migration inspection failed: {exc}")
    pending = list(report.get("pending", []))
    missing_tables = list(report.get("missing_tables", []))
    if pending or missing_tables:
        detail = f"pending={pending or 'none'} missing_tables={missing_tables or 'none'}"
        return ReadinessCheck("migrations", False, detail)
    return ReadinessCheck("migrations", True, "schema is current")


def _redis_and_queue_checks(app: Flask) -> tuple[ReadinessCheck, ReadinessCheck]:
    connection = get_redis_connection(app)
    try:
        ping_ready = bool(connection.ping())
    except (RedisError, OSError) as exc:
        detail = f"Redis ping failed: {exc}"
        return (
            ReadinessCheck("redis", False, detail),
            ReadinessCheck("queue_operation", False, "queue operation skipped because Redis is unavailable"),
        )

    redis_check = ReadinessCheck("redis", ping_ready, "Redis ping completed")
    queue_name = str(app.config.get("RQ_QUEUE_NAME") or "").strip()
    readiness_key = f"twinevia:readiness:{queue_name}:{uuid4().hex}"
    readiness_value = uuid4().hex
    try:
        connection.rpush(readiness_key, readiness_value)
        connection.expire(readiness_key, 60)
        popped_value = connection.lpop(readiness_key)
        connection.delete(readiness_key)
    except (RedisError, OSError) as exc:
        return redis_check, ReadinessCheck("queue_operation", False, f"Redis queue round-trip failed: {exc}")

    decoded_value = popped_value.decode("utf-8") if isinstance(popped_value, bytes) else str(popped_value or "")
    queue_ready = decoded_value == readiness_value
    return redis_check, ReadinessCheck(
        "queue_operation",
        queue_ready,
        "Redis queue round-trip completed" if queue_ready else "Redis queue round-trip returned the wrong value",
    )


def _worker_check(app: Flask) -> ReadinessCheck:
    queue = get_queue(app)
    max_age_seconds = int(app.config.get("READINESS_WORKER_MAX_AGE_SECONDS") or 120)
    try:
        workers = Worker.all(queue=queue)
    except (RedisError, OSError, ValueError) as exc:
        return ReadinessCheck("rq_worker", False, f"worker discovery failed: {exc}")

    now = datetime.now(timezone.utc)
    active_workers: list[str] = []
    for worker in workers:
        heartbeat = getattr(worker, "last_heartbeat", None)
        if heartbeat is None:
            continue
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        age_seconds = (now - heartbeat).total_seconds()
        if age_seconds <= max_age_seconds:
            active_workers.append(str(getattr(worker, "name", "unknown")))

    if not active_workers:
        return ReadinessCheck(
            "rq_worker",
            False,
            f"no worker heartbeat for queue {queue.name!r} within {max_age_seconds} seconds",
        )
    return ReadinessCheck(
        "rq_worker",
        True,
        f"active worker heartbeats: {', '.join(sorted(active_workers))}",
    )


def _configuration_check(app: Flask) -> ReadinessCheck:
    required_names: list[str] = [
        "APP_RELEASE_ID",
        "PUBLIC_BASE_URL",
        "APP_BASE_URL",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_WEBHOOK_ENDPOINT_ID",
        "STRIPE_PORTAL_CONFIGURATION_ID",
        "TWILIO_CREDENTIAL_ENCRYPTION_KEY",
        "READINESS_TOKEN",
        "READINESS_REQUIRED_SYSTEMD_TIMERS",
        "OPERATIONS_MONITORING_MODE",
        "BACKUP_OFFSITE_MODE",
        "BACKUP_ENCRYPTION_PASSPHRASE_FILE",
        "BACKUP_STATUS_FILE",
        "RESTORE_DRILL_STATUS_FILE",
        "RESTORE_DRILL_DATABASE_URL",
        "RESTORE_DRILL_DATABASE_NAME",
        "AOC_SCHEDULED_CANCELLATION_RECORD_FILE",
    ]
    if str(app.config.get("OPERATIONS_MONITORING_MODE") or "") == "github_actions":
        required_names.append("OPERATIONS_GITHUB_REPOSITORY")
    else:
        required_names.extend(("ALERT_WEBHOOK_URL", "UPTIME_MONITOR_HEARTBEAT_URL"))
    if str(app.config.get("BACKUP_OFFSITE_MODE") or "") == "mounted":
        required_names.append("BACKUP_OFFSITE_DESTINATION")
    missing = [name for name in required_names if not str(app.config.get(name) or "").strip()]
    if missing:
        return ReadinessCheck("required_configuration", False, "missing: " + ", ".join(missing))
    return ReadinessCheck("required_configuration", True, "required live configuration is present")


def _systemd_timers_check(app: Flask) -> ReadinessCheck:
    configured_units = str(app.config.get("READINESS_REQUIRED_SYSTEMD_TIMERS") or "")
    units = tuple(unit.strip() for unit in configured_units.split(",") if unit.strip())
    if not units:
        return ReadinessCheck("scheduler_timers", False, "no required systemd timers are configured")
    invalid_units = tuple(unit for unit in units if SYSTEMD_UNIT_PATTERN.fullmatch(unit) is None)
    if invalid_units:
        return ReadinessCheck(
            "scheduler_timers",
            False,
            "invalid systemd timer names: " + ", ".join(invalid_units),
        )

    timeout_seconds = int(app.config.get("READINESS_SYSTEMCTL_TIMEOUT_SECONDS") or 5)
    inactive_units: list[str] = []
    for unit in units:
        try:
            result = subprocess.run(
                ("systemctl", "is-active", unit),
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            return ReadinessCheck("scheduler_timers", False, "systemctl is not installed")
        except subprocess.TimeoutExpired:
            return ReadinessCheck(
                "scheduler_timers",
                False,
                f"systemctl timed out while checking {unit}",
            )
        except OSError as exc:
            return ReadinessCheck(
                "scheduler_timers",
                False,
                f"systemctl failed while checking {unit}: {exc}",
            )
        state = str(result.stdout or "").strip() or "unknown"
        if result.returncode != 0 or state != "active":
            inactive_units.append(f"{unit}={state}")

    if inactive_units:
        return ReadinessCheck(
            "scheduler_timers",
            False,
            "inactive required timers: " + ", ".join(inactive_units),
        )
    return ReadinessCheck(
        "scheduler_timers",
        True,
        "active required timers: " + ", ".join(units),
    )


def _backup_check(app: Flask) -> ReadinessCheck:
    status_path = Path(str(app.config.get("BACKUP_STATUS_FILE") or ""))
    max_age_hours = int(app.config.get("BACKUP_MAX_AGE_HOURS") or 30)
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        completed_at = datetime.fromisoformat(str(payload["completed_at"]))
        offsite_reference = str(
            payload.get("offsite_reference") or payload.get("offsite_path") or ""
        ).strip()
        offsite_verified = payload.get("offsite_verified", bool(payload.get("offsite_path"))) is True
        encrypted_sha256 = str(payload["encrypted_sha256"] or "").strip()
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return ReadinessCheck("encrypted_backup", False, f"backup status is unavailable or invalid: {exc}")

    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600
    if age_hours < 0 or age_hours > max_age_hours:
        return ReadinessCheck(
            "encrypted_backup",
            False,
            f"latest off-host backup is {age_hours:.1f} hours old; maximum is {max_age_hours}",
        )
    if not offsite_verified or not offsite_reference or len(encrypted_sha256) != 64:
        return ReadinessCheck("encrypted_backup", False, "backup status lacks off-host proof or SHA-256")
    return ReadinessCheck("encrypted_backup", True, f"latest off-host backup is {age_hours:.1f} hours old")


def _restore_drill_check(app: Flask) -> ReadinessCheck:
    status_path = Path(str(app.config.get("RESTORE_DRILL_STATUS_FILE") or ""))
    max_age_days = int(app.config.get("RESTORE_DRILL_MAX_AGE_DAYS") or 90)
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        completed_at = datetime.fromisoformat(str(payload["completed_at"]))
        archive_sha256 = str(payload["archive_sha256"] or "").strip()
        schema_ready = payload["schema_ready"] is True
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return ReadinessCheck("restore_drill", False, f"restore-drill status is unavailable or invalid: {exc}")

    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - completed_at).total_seconds() / 86400
    if age_days < 0 or age_days > max_age_days:
        return ReadinessCheck(
            "restore_drill",
            False,
            f"latest isolated restore drill is {age_days:.1f} days old; maximum is {max_age_days}",
        )
    if not schema_ready or len(archive_sha256) != 64:
        return ReadinessCheck("restore_drill", False, "restore-drill status lacks schema or archive proof")
    return ReadinessCheck("restore_drill", True, f"latest isolated restore drill is {age_days:.1f} days old")


def _aoc_scheduled_cancellation_check(app: Flask) -> ReadinessCheck:
    status_path = Path(str(app.config.get("AOC_SCHEDULED_CANCELLATION_RECORD_FILE") or ""))
    organization_slug = str(app.config.get("AOC_EVENTS_ORGANIZATION_SLUG") or "").strip()
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        cancellation_state = str(payload["cancellation_state"])
        expected_count = int(payload["expected_count"])
        recorded_slug = str(payload["organization"]["slug"])
        recorded_messages = list(payload["scheduled_messages"])
        recorded_remaining = int(payload["dispatchable_count_after_cancellation"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return ReadinessCheck(
            "aoc_scheduled_cancellation",
            False,
            f"AOC cancellation record is unavailable or invalid: {exc}",
        )

    if (
        cancellation_state != "confirmed"
        or expected_count < 1
        or expected_count > 100
        or len(recorded_messages) != expected_count
        or recorded_remaining != 0
        or recorded_slug != organization_slug
    ):
        return ReadinessCheck(
            "aoc_scheduled_cancellation",
            False,
            "AOC cancellation record does not prove the expected sends were captured and cancelled",
        )

    try:
        organization = db.session.execute(
            select(Organization).where(Organization.slug == organization_slug)
        ).scalar_one_or_none()
        if organization is None:
            return ReadinessCheck(
                "aoc_scheduled_cancellation",
                False,
                "configured AOC organization was not found",
            )
        dispatchable_count = db.session.scalar(
            select(func.count(ScheduledMessage.id)).where(
                ScheduledMessage.organization_id == organization.id,
                ScheduledMessage.status.in_(("pending", "processing")),
            )
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        return ReadinessCheck(
            "aoc_scheduled_cancellation",
            False,
            f"AOC scheduled-message verification failed: {exc}",
        )

    if int(dispatchable_count or 0) != 0:
        return ReadinessCheck(
            "aoc_scheduled_cancellation",
            False,
            f"AOC still has {int(dispatchable_count or 0)} dispatchable scheduled messages",
        )
    return ReadinessCheck(
        "aoc_scheduled_cancellation",
        True,
        f"{expected_count} launch send(s) are recorded and no AOC scheduled message is dispatchable",
    )


def run_readiness_checks(
    app: Flask,
    require_worker: bool,
    require_launch_artifacts: bool,
) -> ReadinessReport:
    checks: list[ReadinessCheck] = [_database_check(), _migration_check()]
    if require_launch_artifacts:
        checks.extend(
            (
                _configuration_check(app),
                _systemd_timers_check(app),
                _backup_check(app),
                _restore_drill_check(app),
                _aoc_scheduled_cancellation_check(app),
            )
        )
    redis_check, queue_check = _redis_and_queue_checks(app)
    checks.extend((redis_check, queue_check))
    if require_worker:
        checks.append(_worker_check(app))
    return ReadinessReport(
        ready=all(check.ready for check in checks),
        checked_at=datetime.now(timezone.utc).isoformat(),
        release_id=str(app.config.get("APP_RELEASE_ID") or "dev"),
        checks=tuple(checks),
    )
