from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from flask import current_app

from app.queue import get_redis_connection
from app.utils import normalize_phone


BLAST_IDEMPOTENCY_TTL_SECONDS = 120
DIRECT_SEND_IDEMPOTENCY_TTL_SECONDS = 30
BLAST_JOB_TIMEOUT_SECONDS = 1800
_PENDING_VALUE = "pending"


@dataclass(frozen=True)
class OutboundIdempotencyClaim:
    acquired: bool
    fingerprint: str
    redis_key: str | None
    existing_log_id: int | None = None
    existing_value: str | None = None
    storage_available: bool = True


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_outbound_fingerprint(payload: dict[str, Any]) -> str:
    serialized = _canonical_payload(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def recipient_fingerprint_phones(recipient_data: list[dict] | None) -> list[str]:
    phones = {
        normalize_phone(recipient.get("phone"))
        for recipient in (recipient_data or [])
        if isinstance(recipient, dict)
    }
    phones.discard("")
    return sorted(phones)


def build_blast_send_fingerprint(
    *,
    organization_id: int | None,
    target: str,
    event_id: int | None,
    test_mode: bool,
    final_message: str,
    recipient_data: list[dict],
) -> str:
    return build_outbound_fingerprint(
        {
            "kind": "blast",
            "organization_id": organization_id,
            "target": target,
            "event_id": event_id if target == "event" else None,
            "test_mode": bool(test_mode),
            "message_body": final_message,
            "phones": recipient_fingerprint_phones(recipient_data),
        }
    )


def _redis_key(namespace: str, fingerprint: str) -> str:
    return f"twinevia:outbound-idempotency:{namespace}:{fingerprint}"


def _decode_redis_value(value: Any) -> str | None:
    if value in {None, b"", ""}:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore").strip() or None
    return str(value).strip() or None


def _parse_log_id(value: str | None) -> int | None:
    normalized = (value or "").strip().lower()
    if not normalized.startswith("log:"):
        return None
    try:
        return int(normalized.split(":", 1)[1])
    except (TypeError, ValueError):
        return None


def claim_outbound_idempotency(
    namespace: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int,
) -> OutboundIdempotencyClaim:
    fingerprint = build_outbound_fingerprint(payload)
    redis_key = _redis_key(namespace, fingerprint)
    ttl_seconds = max(1, int(ttl_seconds))
    try:
        redis = get_redis_connection()
        acquired = bool(redis.set(redis_key, _PENDING_VALUE, nx=True, ex=ttl_seconds))
        if acquired:
            return OutboundIdempotencyClaim(
                acquired=True,
                fingerprint=fingerprint,
                redis_key=redis_key,
            )

        existing_value = _decode_redis_value(redis.get(redis_key))
        return OutboundIdempotencyClaim(
            acquired=False,
            fingerprint=fingerprint,
            redis_key=redis_key,
            existing_log_id=_parse_log_id(existing_value),
            existing_value=existing_value,
        )
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception(
            "Outbound idempotency unavailable namespace=%s fingerprint=%s: %s",
            namespace,
            fingerprint,
            exc,
        )
        return OutboundIdempotencyClaim(
            acquired=True,
            fingerprint=fingerprint,
            redis_key=None,
            storage_available=False,
        )


def bind_idempotency_log_id(redis_key: str | None, log_id: int, *, ttl_seconds: int) -> None:
    if not redis_key:
        return
    ttl_seconds = max(1, int(ttl_seconds))
    try:
        redis = get_redis_connection()
        remaining_ttl = redis.ttl(redis_key)
        if not isinstance(remaining_ttl, int) or remaining_ttl <= 0:
            remaining_ttl = ttl_seconds
        redis.set(redis_key, f"log:{int(log_id)}", ex=remaining_ttl)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception(
            "Failed to bind outbound idempotency log redis_key=%s log_id=%s: %s",
            redis_key,
            log_id,
            exc,
        )


def release_outbound_idempotency(redis_key: str | None) -> None:
    if not redis_key:
        return
    try:
        redis = get_redis_connection()
        redis.delete(redis_key)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception(
            "Failed to release outbound idempotency redis_key=%s: %s",
            redis_key,
            exc,
        )
