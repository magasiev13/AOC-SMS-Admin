import json
from typing import Iterable

from app.utils import normalize_phone, phone_digits_sql, phone_lookup_variants, validate_phone


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

    variants = {variant for phone in normalized_phones for variant in phone_lookup_variants(phone)}
    if not variants:
        return set()

    unsubscribed = UnsubscribedContact.query.filter(
        phone_digits_sql(UnsubscribedContact.phone).in_(variants)
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

    variants = {variant for phone in normalized_phones for variant in phone_lookup_variants(phone)}
    if not variants:
        return set()

    suppressed = SuppressedContact.query.filter(
        phone_digits_sql(SuppressedContact.phone).in_(variants)
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


def dedupe_recipients_by_phone(recipients: list[dict]) -> tuple[list[dict], list[dict], set[str]]:
    deduped: list[dict] = []
    duplicates: list[dict] = []
    duplicate_phones: set[str] = set()
    seen_phones: set[str] = set()

    for recipient in recipients:
        normalized_recipient, normalized_phone = _normalize_recipient(recipient)
        if normalized_phone and normalized_phone in seen_phones:
            duplicates.append(normalized_recipient)
            duplicate_phones.add(normalized_phone)
            continue
        if normalized_phone:
            seen_phones.add(normalized_phone)
        deduped.append(normalized_recipient)

    return deduped, duplicates, duplicate_phones


def load_recipient_snapshot(
    snapshot_json: str | None,
    *,
    missing_message: str = "This scheduled message predates recipient snapshots. Recreate it to continue.",
    invalid_message: str = "This scheduled message has an invalid recipient snapshot. Recreate it to continue.",
    unusable_message: str = "This scheduled message does not have a usable recipient snapshot. Recreate it to continue.",
) -> list[dict[str, str]]:
    normalized = (snapshot_json or "").strip()
    if not normalized:
        raise ValueError(missing_message)

    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError(invalid_message) from exc

    if not isinstance(payload, list) or not payload:
        raise ValueError(unusable_message)

    normalized_rows: list[dict[str, str]] = []
    seen_phones: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        normalized_phone = normalize_phone(item.get("phone"))
        if not validate_phone(normalized_phone) or normalized_phone in seen_phones:
            continue
        normalized_rows.append(
            {
                "phone": normalized_phone,
                "name": (item.get("name") or item.get("label") or "").strip(),
            }
        )
        seen_phones.add(normalized_phone)

    if not normalized_rows:
        raise ValueError(unusable_message)
    return normalized_rows
