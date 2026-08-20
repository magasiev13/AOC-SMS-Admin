from __future__ import annotations

import argparse
import json
import sys

from app import create_app
from app.services.readiness_service import run_readiness_checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Twinevia runtime readiness.")
    parser.add_argument(
        "--skip-worker",
        action="store_true",
        help="Skip the RQ worker heartbeat check for isolated diagnostics only.",
    )
    parser.add_argument(
        "--infrastructure-only",
        action="store_true",
        help="Check the database, migrations, isolated Redis, queue operation, and worker without launch artifacts.",
    )
    args = parser.parse_args()

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        report = run_readiness_checks(
            app,
            require_worker=not args.skip_worker,
            require_launch_artifacts=not args.infrastructure_only,
        )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    sys.exit(0 if report.ready else 1)


if __name__ == "__main__":
    main()
