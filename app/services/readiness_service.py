from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Flask
from redis.exceptions import RedisError
from rq import Worker
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.queue import get_queue, get_redis_connection
from app.saas_migrations.runner import inspect_saas_migrations
from deploy.runtime_control import (
    DEFAULT_MAINTENANCE_FILE,
    RUNTIME_UNITS,
    SERVICE_UNITS,
    TIMER_UNITS,
    RuntimeControlError,
    read_maintenance_receipt,
)


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
    state: str | None = None
    release_sha: str = ""
    units: dict[str, dict[str, Any]] = field(default_factory=dict)
    maintenance: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "state": self.state or ("ready" if self.ready else "not_ready"),
            "checked_at": self.checked_at,
            "release_id": self.release_id,
            "release_sha": self.release_sha,
            "checks": [asdict(check) for check in self.checks],
            "units": {name: dict(facts) for name, facts in self.units.items()},
            "maintenance": self.maintenance,
            "warnings": list(self.warnings),
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
        "APP_RELEASE_SHA",
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
    if re.fullmatch(r"[0-9a-f]{40}", str(app.config.get("APP_RELEASE_SHA") or "")) is None:
        return ReadinessCheck("required_configuration", False, "APP_RELEASE_SHA must identify the immutable deployed source")
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


def _runtime_unit_facts(app: Flask) -> dict[str, dict[str, Any]]:
    """Read the complete canonical runtime in one bounded, read-only operation."""
    properties = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState")
    facts = {unit: {"active_state": "unknown", "load_state": "unknown",
                    "sub_state": "unknown", "unit_file_state": "unknown"} for unit in RUNTIME_UNITS}
    command = ("systemctl", "show", "--no-pager", *(f"--property={name}" for name in properties), *RUNTIME_UNITS)
    try:
        result = subprocess.run(command, capture_output=True, check=False, text=True,
                                timeout=int(app.config.get("READINESS_SYSTEMCTL_TIMEOUT_SECONDS") or 5))
    except (OSError, subprocess.TimeoutExpired) as exc:
        for value in facts.values():
            value["error"] = f"systemctl runtime inspection failed: {exc}"
        return facts
    if result.returncode != 0:
        for value in facts.values():
            value["error"] = "systemctl could not inspect the complete required runtime"
        return facts
    names = {"LoadState": "load_state", "ActiveState": "active_state",
             "SubState": "sub_state", "UnitFileState": "unit_file_state"}
    seen = set()
    for block in re.split(r"\n\s*\n", str(result.stdout or "").strip()):
        raw = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = raw.get("Id")
        if unit not in facts:
            continue
        if unit in seen:
            facts[unit]["error"] = "systemctl returned duplicate unit facts"
            continue
        seen.add(unit)
        for source, target in names.items():
            facts[unit][target] = raw.get(source) or "unknown"
    for unit in facts:
        if unit not in seen or facts[unit]["active_state"] == "unknown" or facts[unit]["load_state"] == "unknown" or facts[unit]["unit_file_state"] == "unknown":
            facts[unit]["error"] = "required unit facts are incomplete"
    return facts


def _maintenance_receipt(app: Flask):
    path = str(app.config.get("RUNTIME_MAINTENANCE_FILE") or DEFAULT_MAINTENANCE_FILE)
    release_sha = str(app.config.get("APP_RELEASE_SHA") or "").strip()
    try:
        receipt = read_maintenance_receipt(path, release_sha=release_sha or None)
        if receipt is not None and re.fullmatch(r"[0-9a-f]{40}", release_sha) is None:
            raise RuntimeControlError("Maintenance requires the current immutable release SHA.")
        return receipt, None
    except (RuntimeControlError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return None, str(exc)


def _maintenance_summary(receipt):
    if receipt is None:
        return None
    keys = ("held_units", "reason", "actor", "declared_at", "updated_at", "expires_at",
            "release_sha", "release_id", "transaction_id")
    return {key: receipt.get(key) for key in keys}


def _runtime_readiness(app: Flask):
    facts = _runtime_unit_facts(app)
    receipt, receipt_error = _maintenance_receipt(app)
    held = set(receipt.get("held_units") or ()) if receipt else set()
    expected = receipt.get("states") or {} if receipt else {}
    mismatch = []
    for unit in held:
        actual, original = facts.get(unit) or {}, expected.get(unit) or {}
        if unit not in TIMER_UNITS or not original or original.get("active_state") != "inactive" or actual.get("error"):
            mismatch.append(unit)
        elif any(actual.get(key) != value for key, value in original.items() if key in {"active_state", "load_state", "sub_state", "unit_file_state"}):
            mismatch.append(unit)
    if receipt and (not held or mismatch):
        receipt_error = "Declared maintenance does not match current unit state: " + ", ".join(sorted(mismatch or held))
    warnings = []
    for unit, value in facts.items():
        value["held"] = unit in held
        if value["active_state"] == "active" and value["unit_file_state"] in {"disabled", "masked", "masked-runtime"}:
            warnings.append(f"{unit} is active but {value['unit_file_state']}; it may not start after a reboot.")
    inactive_timers = [unit for unit in TIMER_UNITS if facts[unit].get("error") or facts[unit]["active_state"] != "active" or facts[unit]["load_state"] != "loaded"]
    inactive_services = [unit for unit in SERVICE_UNITS if facts[unit].get("error") or facts[unit]["active_state"] != "active" or facts[unit]["load_state"] != "loaded"]
    timer_check = ReadinessCheck("scheduler_timers", not inactive_timers,
        "inactive required timers: " + ", ".join(f"{unit}={facts[unit]['active_state']} ({facts[unit]['unit_file_state']})" for unit in inactive_timers)
        if inactive_timers else "all required timers are active")
    service_check = ReadinessCheck("runtime_services", not inactive_services,
        "required services are not active: " + ", ".join(inactive_services) if inactive_services else "web and worker services are active")
    maintenance_check = ReadinessCheck("runtime_maintenance", not receipt and not receipt_error,
        "Maintenance state cannot be verified: " + receipt_error if receipt_error else
        "Reviewed maintenance holds: " + ", ".join(sorted(held)) if receipt else "no maintenance is declared")
    configured = {unit.strip() for unit in str(app.config.get("READINESS_REQUIRED_SYSTEMD_TIMERS") or "").split(",") if unit.strip()}
    unknown = configured - set(TIMER_UNITS)
    if unknown:
        timer_check = ReadinessCheck("scheduler_timers", False, "unsupported required timer names: " + ", ".join(sorted(unknown)))
    maintenance_matches = bool(receipt and not receipt_error and not unknown and set(inactive_timers) == held)
    return (service_check, timer_check, maintenance_check), facts, _maintenance_summary(receipt), tuple(warnings), maintenance_matches


def platform_maintenance_view(app: Flask) -> dict[str, Any] | None:
    """Show the trusted operator declaration without running full readiness on a page."""
    receipt, error = _maintenance_receipt(app)
    if error:
        return {"state": "unverified", "title": "Maintenance state cannot be verified", "detail": error}
    if receipt is None:
        return None
    summary = _maintenance_summary(receipt)
    def display_date(value):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%b %d, %Y at %H:%M UTC")
    held = summary.get("held_units") or []
    labels = {
        "twinevia-saas-billing-reconcile.timer": "Billing reconciliation",
        "twinevia-saas-scheduler.timer": "Scheduled message processing",
        "twinevia-saas-platform-restart-queue.timer": "Platform restart processing",
        "twinevia-saas-a2p-reconcile.timer": "Provider approval reconciliation",
        "twinevia-saas-backup.timer": "Scheduled backups",
        "twinevia-saas-readiness.timer": "Host readiness monitoring",
    }
    return {**summary, "state": "declared", "title": "Maintenance declared",
            "unit_labels": [labels.get(unit, unit) for unit in held],
            "declared_at_display": display_date(summary['declared_at']),
            "expires_at_display": display_date(summary['expires_at']),
            "billing_reconciliation_held": "twinevia-saas-billing-reconcile.timer" in held}


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


def run_readiness_checks(
    app: Flask,
    require_worker: bool,
    require_launch_artifacts: bool,
) -> ReadinessReport:
    checks: list[ReadinessCheck] = [_database_check(), _migration_check()]
    units = {}
    maintenance = None
    warnings = ()
    maintenance_matches = False
    if require_launch_artifacts:
        runtime_checks, units, maintenance, warnings, maintenance_matches = _runtime_readiness(app)
        checks.extend(
            (
                _configuration_check(app),
                *runtime_checks,
                _backup_check(app),
                _restore_drill_check(app),
            )
        )
    redis_check, queue_check = _redis_and_queue_checks(app)
    checks.extend((redis_check, queue_check))
    if require_worker:
        checks.append(_worker_check(app))
    ready = all(check.ready for check in checks)
    state = "ready" if ready else "not_ready"
    if not ready and maintenance_matches and all(
        check.ready for check in checks if check.name not in {"scheduler_timers", "runtime_maintenance"}
    ):
        state = "maintenance"
    return ReadinessReport(
        ready=ready,
        state=state,
        checked_at=datetime.now(timezone.utc).isoformat(),
        release_id=str(app.config.get("APP_RELEASE_ID") or "dev"),
        release_sha=str(app.config.get("APP_RELEASE_SHA") or ""),
        checks=tuple(checks),
        units=units,
        maintenance=maintenance,
        warnings=warnings,
    )
