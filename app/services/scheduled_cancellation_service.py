"""Linearize scheduled cancellation with individual provider submissions.

PostgreSQL callers hold the schedule row lock from the last eligibility check
through the provider outcome commit. A cancellation therefore acknowledges only
after an in-flight submission finishes, and before any following submission.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from app import db
from app.models import ScheduledMessage


_scheduled_dispatch: ContextVar[tuple[int, int | None] | None] = ContextVar(
    "scheduled_dispatch", default=None
)


@contextmanager
def scheduled_dispatch_context(scheduled_id: int, organization_id: int | None):
    token = _scheduled_dispatch.set((int(scheduled_id), organization_id))
    try:
        yield
    finally:
        _scheduled_dispatch.reset(token)


def current_scheduled_dispatch() -> tuple[int, int | None] | None:
    return _scheduled_dispatch.get()


def lock_scheduled_message(scheduled_id: int, organization_id: int | None):
    """Refresh persisted state under a row lock; never flush a stale status first."""
    with db.session.no_autoflush:
        query = ScheduledMessage.query.filter_by(
            id=int(scheduled_id), organization_id=organization_id,
        )
        if db.session.get_bind().dialect.name == 'sqlite':
            # Compatibility-only SQLite has no FOR UPDATE. Acquire its writer
            # lock without changing content so an acknowledged cancellation
            # cannot slip between this check and a legacy provider call.
            query.update({ScheduledMessage.id: ScheduledMessage.id}, synchronize_session=False)
        return (
            query
            .populate_existing()
            .with_for_update()
            .first()
        )


def cancellation_result(*, status: str | None, account_sid: str | None, send_kind: str) -> dict:
    cancelled = status == "cancelled"
    return {
        "success": False,
        "sid": None,
        "status": "cancelled" if cancelled else "skipped",
        "error": (
            "Scheduled message was cancelled before this recipient was submitted."
            if cancelled
            else "Scheduled message is no longer processing; no provider submission was made."
        ),
        "account_sid": account_sid,
        "send_kind": send_kind,
        "dispatch_halted": True,
        "cancelled": cancelled,
        "halted_status": status or "missing",
    }


def apply_scheduled_cancellation(scheduled) -> bool:
    """Apply only while locked, without committing the caller's transaction."""
    if scheduled.status not in {"pending", "processing", "held"}:
        return False
    was_processing = scheduled.status == "processing"
    scheduled.status = "cancelled"
    scheduled.processing_started_at = None
    scheduled.next_retry_at = None
    scheduled.error_message = (
        "Cancellation accepted. Already submitted messages may complete; remaining recipients will not be submitted."
        if was_processing else None
    )
    return True
