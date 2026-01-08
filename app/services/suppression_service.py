from typing import Literal

from flask import current_app

from app import db
from app.models import CommunityMember, EventRegistration, SuppressedContact, UnsubscribedContact, utc_now
from app.utils import normalize_phone


OptOutCategory = Literal['opt_out', 'hard_fail', 'soft_fail']


def classify_failure(error_text: str) -> OptOutCategory:
    if not error_text:
        return 'soft_fail'

    message = error_text.lower()

    opt_out_patterns = [
        'unsubscribed',
        'opted out',
        'opt-out',
        'opt out',
        'stop',
        'reply stop',
        'unsubscribe',
        'cancel',
        'quit',
        'end',
        'blocked',
        'recipient has opted out',
        'opt-out',
        '21610',
        '30004',
    ]
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
        '30005',
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

    if any(pattern in message for pattern in opt_out_patterns):
        return 'opt_out'
    if any(pattern in message for pattern in hard_fail_patterns):
        return 'hard_fail'
    if any(pattern in message for pattern in soft_fail_patterns):
        return 'soft_fail'

    return 'soft_fail'


def process_failure_details(details: list, source_message_log_id: int) -> dict:
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
    }

    def get_phone(entry: dict) -> str:
        return entry.get('phone') or entry.get('to') or entry.get('recipient') or ''

    suppressed_phones = set()

    with db.session.begin():
        for detail in details:
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

            category = classify_failure(error_text)
            counts[category] += 1

            if category == 'opt_out':
                existing = UnsubscribedContact.query.filter_by(phone=normalized_phone).first()
                if existing:
                    existing.source = 'message_failure'
                    if error_text:
                        existing.reason = error_text
                    if detail.get('name') and not existing.name:
                        existing.name = detail.get('name')
                else:
                    db.session.add(
                        UnsubscribedContact(
                            name=detail.get('name'),
                            phone=normalized_phone,
                            reason=error_text or None,
                            source='message_failure',
                        )
                    )
                counts['unsubscribed_upserts'] += 1
            elif category == 'hard_fail':
                existing = SuppressedContact.query.filter_by(phone=normalized_phone).first()
                if existing:
                    existing.reason = error_text
                    existing.category = category
                    existing.source = 'message_failure'
                    existing.source_type = 'message_log'
                    existing.source_message_log_id = source_message_log_id
                    existing.updated_at = utc_now()
                else:
                    db.session.add(
                        SuppressedContact(
                            phone=normalized_phone,
                            reason=error_text,
                            category=category,
                            source='message_failure',
                            source_type='message_log',
                            source_message_log_id=source_message_log_id,
                        )
                    )
                counts['suppressed_upserts'] += 1
                suppressed_phones.add(normalized_phone)

        if suppressed_phones:
            counts['community_member_deletes'] = CommunityMember.query.filter(
                CommunityMember.phone.in_(suppressed_phones)
            ).delete(synchronize_session=False)
            counts['event_registration_deletes'] = EventRegistration.query.filter(
                EventRegistration.phone.in_(suppressed_phones)
            ).delete(synchronize_session=False)

    current_app.logger.info(
        "Processed failure details: total=%s failed=%s opt_out=%s hard_fail=%s soft_fail=%s "
        "unsubscribed_upserts=%s suppressed_upserts=%s community_member_deletes=%s "
        "event_registration_deletes=%s skipped_no_phone=%s",
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
    )

    return counts
