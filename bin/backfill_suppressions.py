#!/usr/bin/env python3
import argparse
import json

from flask import current_app

from app import create_app
from app.models import MessageLog
from app.services.suppression_service import process_failure_details


def _load_details(log: MessageLog) -> list:
    if not log.details:
        return []
    try:
        details = json.loads(log.details)
    except json.JSONDecodeError:
        return []
    if not isinstance(details, list):
        return []
    return details


def backfill_suppressions(batch_size: int) -> None:
    last_id = 0
    batch_number = 0
    total_logs = 0
    total_calls = 0
    total_details = 0

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

        for log in batch:
            batch_logs += 1
            details = _load_details(log)
            if not details:
                continue
            batch_details += len(details)
            process_failure_details(details, log.id)
            batch_calls += 1

        last_id = batch[-1].id
        total_logs += batch_logs
        total_calls += batch_calls
        total_details += batch_details

        current_app.logger.info(
            "Backfill suppressions batch=%s logs=%s calls=%s details=%s",
            batch_number,
            batch_logs,
            batch_calls,
            batch_details,
        )

    current_app.logger.info(
        "Backfill suppressions complete batches=%s logs=%s calls=%s details=%s",
        batch_number,
        total_logs,
        total_calls,
        total_details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill suppression records from message logs.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of MessageLog rows to process per batch.",
    )
    args = parser.parse_args()

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        backfill_suppressions(args.batch_size)


if __name__ == "__main__":
    main()
