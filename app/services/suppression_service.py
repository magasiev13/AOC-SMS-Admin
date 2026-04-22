from typing import Literal

from flask import current_app

from app import db
from app.models import CommunityMember, EventRegistration, MessageLog, SuppressedContact, UnsubscribedContact, utc_now
from app.utils import normalize_phone, validate_phone


OptOutCategory = Literal['opt_out', 'hard_fail', 'soft_fail']
HARD_FAIL_ERROR_CODES = {'21610', '30003', '30004', '30005', '30006', '30007'}


def classify_failure(
    error_text: str,
    *,
    error_code: object | None = None,
    status: str | None = None,
) -> OptOutCategory:
    normalized_error_code = str(error_code or '').strip()
    normalized_status = (status or '').strip().lower()

    if normalized_error_code == '21610':
        return 'opt_out'
    if normalized_error_code in {'30003', '30004', '30005', '30006', '30007'}:
        return 'hard_fail'

    if not error_text:
        if normalized_status in {'failed', 'undelivered'}:
            return 'soft_fail'
        return 'soft_fail'

    message = error_text.lower()

    opt_out_phrases = [
        'unsubscribed',
        'opted out',
        'opted-out',
        'opt-out',
        'opt out',
        'reply stop',
        'unsubscribe',
        'recipient has opted out',
    ]
    # Keep opt-out detection strict to avoid suppressing valid contacts on
    # generic carrier blocks/transient failures.
    hard_fail_patterns = [
        'invalid',
        'not a valid',
        'does not exist',
        'unknown subscriber',
        'unreachable',
        'landline',
        'not a mobile',
        'no route',
        'unassigned',
        'number is not valid',
        'phone number is not',
        'carrier violation',
        '30003',
        '30004',
        '30005',
        '30006',
        '30007',
    ]
    soft_fail_patterns = [
        'temporarily',
        'timeout',
        'timed out',
        'rate limit',
        'throttle',
        'too many requests',
        'network',
        'connection',
        'service unavailable',
        'server error',
        'unavailable',
        'gateway',
        '429',
        '500',
        '502',
        '503',
        '504',
    ]

    if any(pattern in message for pattern in opt_out_phrases):
        return 'opt_out'
    if normalized_error_code == '21610' or '21610' in message:
        return 'opt_out'
    if any(pattern in message for pattern in hard_fail_patterns):
        return 'hard_fail'
    if any(pattern in message for pattern in soft_fail_patterns):
        return 'soft_fail'

    return 'soft_fail'


def apply_failure_suppression(
    *,
    organization_id: int | None,
    phone: str | None,
    name: str | None = None,
    error_text: str | None = None,
    error_code: object | None = None,
    status: str | None = None,
    source: str = 'message_failure',
    source_type: str | None = 'message_log',
    source_message_log_id: int | None = None,
    purge_related_rows: bool = True,
    commit: bool = False,
) -> dict:
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        return {'applied': False, 'category': 'soft_fail', 'reason': None}
    if not validate_phone(normalized_phone):
        return {'applied': False, 'category': 'soft_fail', 'reason': None}

    reason = str(error_text or error_code or status or '').strip() or None
    category = classify_failure(reason or '', error_code=error_code, status=status)
    applied = False

    if category == 'opt_out':
        existing = UnsubscribedContact.query.filter_by(
            phone=normalized_phone,
            organization_id=organization_id,
        ).first()
        if existing:
            existing.source = source
            if reason:
                existing.reason = reason
            if name and not existing.name:
                existing.name = name
        else:
            db.session.add(
                UnsubscribedContact(
                    organization_id=organization_id,
                    name=name,
                    phone=normalized_phone,
                    reason=reason,
                    source=source,
                )
            )
        applied = True
    elif category == 'hard_fail':
        existing = SuppressedContact.query.filter_by(
            phone=normalized_phone,
            organization_id=organization_id,
        ).first()
        if existing:
            existing.reason = reason
            existing.category = category
            existing.source = source
            existing.source_type = source_type
            existing.source_message_log_id = source_message_log_id
            existing.updated_at = utc_now()
        else:
            db.session.add(
                SuppressedContact(
                    organization_id=organization_id,
                    phone=normalized_phone,
                    reason=reason,
                    category=category,
                    source=source,
                    source_type=source_type,
                    source_message_log_id=source_message_log_id,
                )
            )
        applied = True

    if not applied:
        return {'applied': False, 'category': category, 'reason': reason}

    if purge_related_rows:
        CommunityMember.query.filter(
            CommunityMember.organization_id == organization_id,
            CommunityMember.phone == normalized_phone,
        ).delete(synchronize_session=False)
        EventRegistration.query.filter(
            EventRegistration.organization_id == organization_id,
            EventRegistration.phone == normalized_phone,
        ).delete(synchronize_session=False)

    if commit:
        db.session.commit()

    return {'applied': True, 'category': category, 'reason': reason}


def process_failure_details(details: list, source_message_log_id: int) -> dict:
    source_log = MessageLog.query.filter_by(id=source_message_log_id).first()
    organization_id = source_log.organization_id if source_log is not None else None
    counts = {
        'total': len(details),
        'failed': 0,
        'opt_out': 0,
        'hard_fail': 0,
        'soft_fail': 0,
        'unsubscribed_upserts': 0,
        'suppressed_upserts': 0,
        'community_member_deletes': 0,
        'event_registration_deletes': 0,
        'skipped_no_phone': 0,
        'skipped_invalid': 0,
    }

    def get_phone(entry: dict) -> str:
        return entry.get('phone') or entry.get('to') or entry.get('recipient') or ''

    suppressed_phones = set()

    try:
        for detail in details:
            if not isinstance(detail, dict):
                counts['skipped_invalid'] += 1
                continue
            success = detail.get('success')
            status = detail.get('status')
            error_text = detail.get('error') or detail.get('message') or ''

            if success is True:
                continue
            if success is None and not error_text and status not in {'failed', 'undelivered'}:
                continue

            counts['failed'] += 1
            normalized_phone = normalize_phone(get_phone(detail))
            if not normalized_phone:
                counts['skipped_no_phone'] += 1
                continue
            if not validate_phone(normalized_phone):
                counts['skipped_invalid'] += 1
                continue

            category = classify_failure(
                error_text,
                error_code=detail.get('error_code'),
                status=status,
            )
            counts[category] += 1

            if category == 'opt_out':
                apply_failure_suppression(
                    organization_id=organization_id,
                    phone=normalized_phone,
                    name=detail.get('name'),
                    error_text=error_text,
                    error_code=detail.get('error_code'),
                    status=status,
                    source='message_failure',
                    source_type='message_log',
                    source_message_log_id=source_message_log_id,
                    purge_related_rows=False,
                    commit=False,
                )
                counts['unsubscribed_upserts'] += 1
                suppressed_phones.add(normalized_phone)
            elif category == 'hard_fail':
                apply_failure_suppression(
                    organization_id=organization_id,
                    phone=normalized_phone,
                    name=detail.get('name'),
                    error_text=error_text,
                    error_code=detail.get('error_code'),
                    status=status,
                    source='message_failure',
                    source_type='message_log',
                    source_message_log_id=source_message_log_id,
                    purge_related_rows=False,
                    commit=False,
                )
                counts['suppressed_upserts'] += 1
                suppressed_phones.add(normalized_phone)

        if suppressed_phones:
            counts['community_member_deletes'] = CommunityMember.query.filter(
                CommunityMember.organization_id == organization_id,
                CommunityMember.phone.in_(suppressed_phones)
            ).delete(synchronize_session=False)
            counts['event_registration_deletes'] = EventRegistration.query.filter(
                EventRegistration.organization_id == organization_id,
                EventRegistration.phone.in_(suppressed_phones)
            ).delete(synchronize_session=False)

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    current_app.logger.info(
        "Processed failure details: total=%s failed=%s opt_out=%s hard_fail=%s soft_fail=%s "
        "unsubscribed_upserts=%s suppressed_upserts=%s community_member_deletes=%s "
        "event_registration_deletes=%s skipped_no_phone=%s skipped_invalid=%s",
        counts['total'],
        counts['failed'],
        counts['opt_out'],
        counts['hard_fail'],
        counts['soft_fail'],
        counts['unsubscribed_upserts'],
        counts['suppressed_upserts'],
        counts['community_member_deletes'],
        counts['event_registration_deletes'],
        counts['skipped_no_phone'],
        counts['skipped_invalid'],
    )

    return counts
