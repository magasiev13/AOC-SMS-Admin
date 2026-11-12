"""Persist only a notification fingerprint and time, across readiness runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time


REPEAT_SECONDS = 3600


def notification_due(path: Path, message: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        state = json.loads(path.read_text())
        notified_at = float(state['notified_at'])
        fingerprint = state['fingerprint']
    except (OSError, ValueError, TypeError, KeyError):
        return True
    return (fingerprint != hashlib.sha256(message.encode()).hexdigest()
            or not math.isfinite(notified_at) or now < notified_at
            or now - notified_at >= REPEAT_SECONDS)


def record_notification(path: Path, message: str, *, now: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.readiness-alert-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump({'fingerprint': hashlib.sha256(message.encode()).hexdigest(),
                       'notified_at': time.time() if now is None else now}, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('due', 'record', 'clear'))
    parser.add_argument('path', type=Path)
    parser.add_argument('message', nargs='?', default='')
    args = parser.parse_args()
    try:
        if args.action == 'due':
            return 0 if notification_due(args.path, args.message) else 1
        if args.action == 'record':
            record_notification(args.path, args.message)
        else:
            args.path.unlink(missing_ok=True)
    except OSError:
        # No paths, message contents, credentials or destinations in errors.
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
