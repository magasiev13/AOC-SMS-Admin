from typing import Iterable


def get_unsubscribed_phone_set(phones: Iterable[str]) -> set[str]:
    phones = {phone for phone in phones if phone}
    if not phones:
        return set()

    from app.models import UnsubscribedContact

    unsubscribed = UnsubscribedContact.query.filter(UnsubscribedContact.phone.in_(phones)).all()
    return {entry.phone for entry in unsubscribed}


def filter_unsubscribed_recipients(recipients: list[dict]) -> tuple[list[dict], list[dict], set[str]]:
    phones = [recipient.get('phone') for recipient in recipients if recipient.get('phone')]
    unsubscribed_phones = get_unsubscribed_phone_set(phones)
    if not unsubscribed_phones:
        return recipients, [], set()

    filtered = [recipient for recipient in recipients if recipient.get('phone') not in unsubscribed_phones]
    skipped = [recipient for recipient in recipients if recipient.get('phone') in unsubscribed_phones]
    return filtered, skipped, unsubscribed_phones


def get_suppressed_phone_set(phones: Iterable[str]) -> set[str]:
    phones = {phone for phone in phones if phone}
    if not phones:
        return set()

    from app.models import SuppressedContact

    suppressed = SuppressedContact.query.filter(SuppressedContact.phone.in_(phones)).all()
    return {entry.phone for entry in suppressed}


def filter_suppressed_recipients(recipients: list[dict]) -> tuple[list[dict], list[dict], set[str]]:
    phones = [recipient.get('phone') for recipient in recipients if recipient.get('phone')]
    suppressed_phones = get_suppressed_phone_set(phones)
    if not suppressed_phones:
        return recipients, [], set()

    filtered = [recipient for recipient in recipients if recipient.get('phone') not in suppressed_phones]
    skipped = [recipient for recipient in recipients if recipient.get('phone') in suppressed_phones]
    return filtered, skipped, suppressed_phones
