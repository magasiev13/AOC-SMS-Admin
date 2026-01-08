#!/usr/bin/env python3
import argparse

from app import create_app
from app.services.suppression_backfill import backfill_suppressions


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
