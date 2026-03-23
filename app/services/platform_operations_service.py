from __future__ import annotations

import subprocess
from pathlib import Path

from flask import current_app


class PlatformServiceRestartError(RuntimeError):
    """Raised when the SaaS restart helper cannot be queued successfully."""


def _restart_script_path() -> str:
    configured_path = (current_app.config.get("PLATFORM_SERVICE_RESTART_SCRIPT") or "").strip()
    if not configured_path:
        raise PlatformServiceRestartError("Platform service restart script is not configured.")

    path = Path(configured_path)
    if not path.is_absolute():
        raise PlatformServiceRestartError("Platform service restart script must use an absolute path.")
    return str(path)


def _summarize_process_output(*, stdout: str, stderr: str) -> str | None:
    for raw_value in (stderr, stdout):
        text = (raw_value or "").strip()
        if not text:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1][:280]
    return None


def request_platform_service_restart(*, timeout_seconds: int = 15) -> dict[str, str | bool | None]:
    script_path = _restart_script_path()
    command = ["sudo", "-n", script_path]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlatformServiceRestartError(
            "Timed out while requesting the SaaS service restart."
        ) from exc
    except FileNotFoundError as exc:
        raise PlatformServiceRestartError(
            f"Platform restart helper is unavailable: {exc.filename or 'missing executable'}."
        ) from exc
    except OSError as exc:
        raise PlatformServiceRestartError(
            f"Failed to execute the platform restart helper: {exc}."
        ) from exc

    detail = _summarize_process_output(stdout=completed.stdout, stderr=completed.stderr)
    if completed.returncode != 0:
        raise PlatformServiceRestartError(
            detail or f"Platform restart helper exited with status {completed.returncode}."
        )

    return {
        "success": True,
        "summary": "Restart queued. The SaaS services will recycle shortly.",
        "detail": detail,
        "script_path": script_path,
    }
