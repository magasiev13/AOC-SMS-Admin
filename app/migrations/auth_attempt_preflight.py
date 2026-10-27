"""Preserve duplicate legacy lockout evidence before pending migration 010.

This is a runner preflight, not a modification to an applied migration. The
runner owns the SQLite BEGIN IMMEDIATE transaction, including the evidence
archive, merge, unique-index creation and migration-version insert.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json

from sqlalchemy import text


EVIDENCE_TABLE = "legacy_login_attempt_merge_evidence"


def _timestamp(value, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Legacy login-attempt {field} is invalid; review the retained database before migration 010.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_value(value):
    if isinstance(value, bytes):
        return {"sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
    return value


def prepare_pending_auth_hardening(connection, logger) -> None:
    """Merge only duplicate keys that pending 010 would make unique.

    Keep every original row, including unknown extra columns, in an append-only
    archive. Summed failure counts, the latest window start and longest lock
    conservatively retain protection; individual original windows remain in
    the archive. No application credential, network or provider is involved.
    """
    if connection.dialect.name != "sqlite":
        raise RuntimeError("Legacy auth preflight requires the SQLite migration runner.")
    columns = {row._mapping["name"] for row in connection.execute(text("PRAGMA table_info(login_attempts)"))}
    users_columns = {row._mapping["name"] for row in connection.execute(text("PRAGMA table_info(users)"))}
    if not columns or not users_columns:
        return
    if not {"id", "client_ip"}.issubset(columns):
        raise RuntimeError("Legacy login-attempt key columns are unsupported; migration 010 requires manual review.")
    username_expression = "COALESCE(username, '')" if "username" in columns else "''"
    groups = connection.execute(text(
        f"SELECT client_ip, {username_expression} AS normalized_username "
        f"FROM login_attempts GROUP BY client_ip, {username_expression} "
        "HAVING COUNT(*) > 1 ORDER BY client_ip, normalized_username"
    )).mappings().all()
    if not groups:
        return
    if not {"attempt_count", "first_attempt_at", "locked_until"}.issubset(columns):
        raise RuntimeError("Duplicate legacy login-attempt evidence is incomplete; review it before migration 010.")

    connection.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {EVIDENCE_TABLE} (
            id INTEGER PRIMARY KEY,
            source_attempt_id INTEGER NOT NULL UNIQUE,
            retained_attempt_id INTEGER NOT NULL,
            source_record_json TEXT NOT NULL,
            archived_at TEXT NOT NULL
        )
    """))
    archived_at = datetime.now(timezone.utc).isoformat()
    archived_count = 0
    for group in groups:
        if group["client_ip"] is None:
            raise RuntimeError("A duplicate legacy login-attempt has no client IP; review it before migration 010.")
        rows = connection.execute(text(
            f"SELECT * FROM login_attempts WHERE client_ip = :client_ip "
            f"AND {username_expression} = :username ORDER BY id"
        ), {"client_ip": group["client_ip"], "username": group["normalized_username"]}).mappings().all()
        counts = [row["attempt_count"] for row in rows]
        if any(not isinstance(count, int) or count < 0 for count in counts) or sum(counts) > 2**63 - 1:
            raise RuntimeError("Duplicate legacy login-attempt counts are invalid; review them before migration 010.")
        first_attempt = max(_timestamp(row["first_attempt_at"], "first_attempt_at") for row in rows)
        locks = [_timestamp(row["locked_until"], "locked_until") for row in rows if row["locked_until"] is not None]
        locked_until = max(locks) if locks else None
        retained_id = rows[0]["id"]
        for row in rows:
            snapshot = json.dumps({key: _json_value(value) for key, value in row.items()}, sort_keys=True, allow_nan=False)
            connection.execute(text(f"""
                INSERT INTO {EVIDENCE_TABLE}
                    (source_attempt_id, retained_attempt_id, source_record_json, archived_at)
                VALUES (:source_id, :retained_id, :snapshot, :archived_at)
            """), {"source_id": row["id"], "retained_id": retained_id, "snapshot": snapshot, "archived_at": archived_at})
            archived_count += 1
        connection.execute(text("""
            UPDATE login_attempts SET attempt_count = :attempt_count,
                first_attempt_at = :first_attempt_at, locked_until = :locked_until
            WHERE id = :retained_id
        """), {
            "attempt_count": sum(counts), "retained_id": retained_id,
            "first_attempt_at": first_attempt.replace(tzinfo=None).isoformat(sep=" "),
            "locked_until": locked_until.replace(tzinfo=None).isoformat(sep=" ") if locked_until else None,
        })
        for row in rows[1:]:
            connection.execute(text("DELETE FROM login_attempts WHERE id = :id"), {"id": row["id"]})
    logger.info("Preserved %d original login-attempt rows while merging %d duplicate buckets before pending migration 010.", archived_count, len(groups))
