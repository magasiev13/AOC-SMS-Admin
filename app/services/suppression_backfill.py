import json
from typing import Optional

from flask import current_app

from app.models import MessageLog
from app.services.suppression_service import process_failure_details


def _load_details(log: MessageLog) -> list:
    if not log.details:
        return []
    try:
        details = json.loads(log.details)
    except json.JSONDecodeError:
        return []
    if isinstance(details, list):
        return details
    if isinstance(details, dict):
        candidate = details.get('details') or details.get('results')
        if isinstance(candidate, list):
            return candidate
    return []


def backfill_suppressions(batch_size: int = 500, logger: Optional[object] = None) -> dict:
    log = logger or current_app.logger
    last_id = 0
    batch_number = 0
    total_logs = 0
    total_calls = 0
    total_details = 0
    total_unsubscribed = 0
    total_suppressed = 0

    while True:
        batch = (
            MessageLog.query.filter(MessageLog.id > last_id)
            .order_by(MessageLog.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        batch_number += 1
        batch_logs = 0
        batch_calls = 0
        batch_details = 0
        batch_unsubscribed = 0
        batch_suppressed = 0

        for log_entry in batch:
            batch_logs += 1
            details = _load_details(log_entry)
            if not details:
                continue
            batch_details += len(details)
            result = process_failure_details(details, log_entry.id)
            batch_unsubscribed += result.get('unsubscribed_upserts', 0)
            batch_suppressed += result.get('suppressed_upserts', 0)
            batch_calls += 1

        last_id = batch[-1].id
        total_logs += batch_logs
        total_calls += batch_calls
        total_details += batch_details
        total_unsubscribed += batch_unsubscribed
        total_suppressed += batch_suppressed

        log.info(
            "Backfill suppressions batch=%s logs=%s calls=%s details=%s unsubscribed=%s suppressed=%s",
            batch_number,
            batch_logs,
            batch_calls,
            batch_details,
            batch_unsubscribed,
            batch_suppressed,
        )

    log.info(
        "Backfill suppressions complete batches=%s logs=%s calls=%s details=%s unsubscribed=%s suppressed=%s",
        batch_number,
        total_logs,
        total_calls,
        total_details,
        total_unsubscribed,
        total_suppressed,
    )

    return {
        'batches': batch_number,
        'logs': total_logs,
        'calls': total_calls,
        'details': total_details,
        'unsubscribed': total_unsubscribed,
        'suppressed': total_suppressed,
    }
