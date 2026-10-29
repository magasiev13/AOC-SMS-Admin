"""Read-only event communication views built from established send identities."""
from __future__ import annotations

import json

from sqlalchemy import and_, or_

from app.models import FeedbackInvitation, MessageLog, ScheduledMessage
from app.services.aoc_event_sync_service import (
    EVENT_REMINDER_KINDS, _automation_key, _automation_source_for_event,
)
from app.services.delivery_status_service import project_log_delivery


REMINDER_LABELS = {
    'two_week': 'Two weeks before', 'seven_day': 'One week before',
    'one_day': 'One day before', 'day_of': 'Event day',
}


def submission_outcomes(log):
    """Acceptance, receipt evidence and uncertainty have different denominators.

    Historical logs with aggregate successes but no receipt SID stay unresolved.
    No missing receipt is treated as a successful delivery.
    """
    if log is None:
        return None
    try:
        details = json.loads(log.details or '[]')
    except (TypeError, ValueError):
        details = []
    if not isinstance(details, list):
        details = []
    details = [row for row in details if isinstance(row, dict)]
    projected = project_log_delivery(log.organization_id, details)
    # A duplicate result row is not a second receipt.
    receipts = {row['sid']: row for row in projected if row.get('sid')}
    delivered = sum(row.get('delivery_status') in {'delivered', 'read'} for row in receipts.values())
    delivery_failed = sum(row.get('delivery_status') in {'undelivered', 'failed', 'canceled'} for row in receipts.values())
    accepted = int(log.success_count or 0)
    # Some older scheduled finalizers included uncertain acceptance in their
    # aggregate failure count. Preserve those rows as unresolved evidence.
    uncertain = {row.get('logical_send_key') or row.get('phone') or ('detail', index)
                 for index, row in enumerate(details)
                 if row.get('status') in {'sending', 'ambiguous'} and row.get('success') is not True}
    known_failed = sum(row.get('success') is False and row.get('status') not in {'sending', 'ambiguous', 'queued'}
                       for row in details)
    submission_failed = max(known_failed, int(log.failure_count or 0) - len(uncertain), 0)
    total = int(log.total_recipients or 0)
    return {
        'total': total, 'accepted': accepted, 'delivered': delivered,
        'submission_failed': submission_failed, 'delivery_failed': delivery_failed,
        'unresolved': max(0, total - submission_failed - delivered - delivery_failed),
        'acceptance_rate': round(accepted / total * 100, 1) if total else None,
    }


def event_communications(event):
    """Associate community reminders by the exact validated automation identity.

    Those schedules intentionally have event_id=NULL. Never infer an association
    from the message title, substring matching, or an unrelated automation key.
    """
    keys = {_automation_key(event, kind): kind for kind in EVENT_REMINDER_KINDS}
    invitations = FeedbackInvitation.query.filter_by(
        organization_id=event.organization_id, event_id=event.id,
    ).all()
    invitation_by_schedule = {row.scheduled_message_id: row for row in invitations}
    reminder_identity = or_(*(and_(
        ScheduledMessage.automation_key == key,
        ScheduledMessage.automation_kind == kind,
    ) for key, kind in keys.items()))
    schedules = ScheduledMessage.query.filter(
        ScheduledMessage.organization_id == event.organization_id,
        or_(
            ScheduledMessage.event_id == event.id,
            (ScheduledMessage.automation_source == _automation_source_for_event(event))
            & reminder_identity,
            ScheduledMessage.id.in_(invitation_by_schedule),
        ),
    ).order_by(ScheduledMessage.scheduled_at, ScheduledMessage.id).all()
    log_ids = {row.message_log_id for row in schedules if row.message_log_id}
    logs = MessageLog.query.filter(
        MessageLog.organization_id == event.organization_id,
        or_(MessageLog.event_id == event.id, MessageLog.id.in_(log_ids)),
    ).order_by(MessageLog.created_at.desc(), MessageLog.id.desc()).limit(30).all()
    log_by_id = {log.id: log for log in logs}
    rows = []
    for row in schedules:
        candidate_kind = keys.get(row.automation_key) if row.automation_source == _automation_source_for_event(event) else None
        kind = candidate_kind if candidate_kind == row.automation_kind else None
        invitation = invitation_by_schedule.get(row.id)
        rows.append({
            'schedule': row, 'label': 'Feedback invitation' if invitation is not None else (REMINDER_LABELS[kind] if kind else 'Scheduled message'),
            'invitation': invitation,
            'audience': 'Contactable community' if row.target == 'community' else 'Registered contacts',
            'outcome': submission_outcomes(log_by_id.get(row.message_log_id)),
        })
    next_message = next((row for row in rows if row['schedule'].status in {'pending', 'processing'}), None)
    attention = [row for row in rows if row['schedule'].status in {'held', 'failed'}]
    return {'scheduled': rows, 'next_message': next_message,
            'attention': attention,
            'logs': [{'log': log, 'outcome': submission_outcomes(log)} for log in logs]}
