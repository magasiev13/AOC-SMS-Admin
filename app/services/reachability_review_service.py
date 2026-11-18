"""Read-only owner review of repeated final unreachable-handset receipts."""
import json
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import and_

from app import db
from app.models import InboxMessage, MessageDispatchAttempt, MessageLog, MessagingUsageRecord


from app.services.delivery_status_service import utc_naive


def repeated_unreachable_contacts(organization_id, *, now=None):
    if organization_id is None:
        return []
    end = utc_naive(now)
    start = end - timedelta(days=30)
    ledger = MessagingUsageRecord
    attempt = MessageDispatchAttempt
    inbox = InboxMessage
    receipts = (
        db.session.query(ledger.message_sid, ledger.delivery_finalized_at, attempt.recipient_phone,
                         attempt.message_log_id, attempt.scheduled_message_id, inbox.phone, ledger.source)
        .outerjoin(attempt, and_(attempt.organization_id == ledger.organization_id,
                                attempt.provider_message_sid == ledger.message_sid))
        .outerjoin(inbox, and_(inbox.organization_id == ledger.organization_id,
                              inbox.message_sid == ledger.message_sid, inbox.direction == 'outbound'))
        .filter(ledger.organization_id == organization_id, ledger.direction == 'outbound',
                ledger.delivery_error_code == '30003', ledger.delivery_status.in_(('undelivered', 'failed')),
                ledger.delivery_finalized_at.between(start, end))
    )
    sends = defaultdict(dict)
    legacy = {}
    for sid, finalized, attempt_phone, log_id, schedule_id, inbox_phone, source in receipts:
        phone = attempt_phone or inbox_phone
        if log_id is not None or schedule_id is not None:
            key = ('log', log_id) if log_id is not None else ('schedule', schedule_id)
            sends[phone][key] = max(finalized, sends[phone].get(key, finalized))
        elif phone and source not in ('blast', 'scheduled'):
            sends[phone][('sid', sid)] = finalized
        else:
            legacy[sid] = (finalized, phone)
    if legacy:
        # Older message logs keep SID and contact in a JSON text column. Stream
        # tenant history once only when indexed dispatch records cannot identify
        # the send. Stop as soon as every missing SID has a matching record.
        logs = MessageLog.query.with_entities(MessageLog.id, MessageLog.details).filter(
            MessageLog.organization_id == organization_id, MessageLog.details.isnot(None),
        ).order_by(MessageLog.id.desc()).yield_per(100)
        for log_id, details in logs:
            try:
                rows = json.loads(details)
            except (TypeError, ValueError):
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('sid'), str) or row['sid'] not in legacy:
                    continue
                finalized, inbox_phone = legacy.pop(row['sid'])
                phone = inbox_phone or row.get('phone') or row.get('to') or row.get('recipient')
                if isinstance(phone, str) and phone:
                    key = ('log', log_id)
                    sends[phone][key] = max(finalized, sends[phone].get(key, finalized))
            if not legacy:
                break
    flagged = [{'phone': phone, 'count': len(events), 'latest': max(events.values())}
               for phone, events in sends.items() if phone and len(events) >= 3]
    return sorted(flagged, key=lambda row: (-row['count'], row['phone']))
