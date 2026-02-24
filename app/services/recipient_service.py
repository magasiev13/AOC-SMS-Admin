from typing import Iterable

from sqlalchemy import func

from app.utils import normalize_phone


def _phone_digits_sql(column):
    normalized = func.replace(column, '+', '')
    for token in ('(', ')', '-', ' ', '.'):
        normalized = func.replace(normalized, token, '')
    return normalized


def _phone_lookup_variants(phone: str) -> list[str]:
    normalized_phone = normalize_phone(phone)
    digits = ''.join(char for char in normalized_phone if char.isdigit())
    if not digits:
        return []

    variants: list[str] = [digits]
    if len(digits) == 11 and digits.startswith('1'):
        variants.append(digits[1:])
    elif len(digits) == 10:
        variants.append(f'1{digits}')
    return list(dict.fromkeys(variants))


def _normalize_recipient(recipient: dict) -> tuple[dict, str]:
    phone = recipient.get('phone')
    normalized_phone = normalize_phone(phone) if phone else ''
    if not normalized_phone:
        return recipient, ''
    if phone == normalized_phone:
        return recipient, normalized_phone
    normalized_recipient = dict(recipient)
    normalized_recipient['phone'] = normalized_phone
    return normalized_recipient, normalized_phone


def get_unsubscribed_phone_set(phones: Iterable[str]) -> set[str]:
    normalized_phones = {normalize_phone(phone) for phone in phones if phone}
    normalized_phones.discard('')
    if not normalized_phones:
        return set()

    from app.models import UnsubscribedContact

    variants = {variant for phone in normalized_phones for variant in _phone_lookup_variants(phone)}
    if not variants:
        return set()

    unsubscribed = UnsubscribedContact.query.filter(
        _phone_digits_sql(UnsubscribedContact.phone).in_(variants)
    ).all()
    return {normalize_phone(entry.phone) for entry in unsubscribed if normalize_phone(entry.phone)}


def filter_unsubscribed_recipients(recipients: list[dict]) -> tuple[list[dict], list[dict], set[str]]:
    normalized_recipients: list[dict] = []
    phones: list[str] = []
    for recipient in recipients:
        normalized_recipient, normalized_phone = _normalize_recipient(recipient)
        normalized_recipients.append(normalized_recipient)
        if normalized_phone:
            phones.append(normalized_phone)

    unsubscribed_phones = get_unsubscribed_phone_set(phones)
    if not unsubscribed_phones:
        return normalized_recipients, [], set()

    filtered = [recipient for recipient in normalized_recipients if recipient.get('phone') not in unsubscribed_phones]
    skipped = [recipient for recipient in normalized_recipients if recipient.get('phone') in unsubscribed_phones]
    return filtered, skipped, unsubscribed_phones


def get_suppressed_phone_set(phones: Iterable[str]) -> set[str]:
    normalized_phones = {normalize_phone(phone) for phone in phones if phone}
    normalized_phones.discard('')
    if not normalized_phones:
        return set()

    from app.models import SuppressedContact

    variants = {variant for phone in normalized_phones for variant in _phone_lookup_variants(phone)}
    if not variants:
        return set()

    suppressed = SuppressedContact.query.filter(
        _phone_digits_sql(SuppressedContact.phone).in_(variants)
    ).all()
    return {normalize_phone(entry.phone) for entry in suppressed if normalize_phone(entry.phone)}


def filter_suppressed_recipients(recipients: list[dict]) -> tuple[list[dict], list[dict], set[str]]:
    normalized_recipients: list[dict] = []
    phones: list[str] = []
    for recipient in recipients:
        normalized_recipient, normalized_phone = _normalize_recipient(recipient)
        normalized_recipients.append(normalized_recipient)
        if normalized_phone:
            phones.append(normalized_phone)

    suppressed_phones = get_suppressed_phone_set(phones)
    if not suppressed_phones:
        return normalized_recipients, [], set()

    filtered = [recipient for recipient in normalized_recipients if recipient.get('phone') not in suppressed_phones]
    skipped = [recipient for recipient in normalized_recipients if recipient.get('phone') in suppressed_phones]
    return filtered, skipped, suppressed_phones
