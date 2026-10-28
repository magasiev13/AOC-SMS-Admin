"""Contact presentation policy for workspace staff.

Authorization and server-side omission are the primary boundary.  The text/JSON
helpers are display defenses, never changes to stored content or send payloads.
They must not be applied to serialized signed tokens, HTML, or provider requests.
"""
from __future__ import annotations

from functools import wraps
import re
from collections.abc import Mapping

from flask import abort, current_app, has_app_context, has_request_context
from flask_login import current_user

from app.tenant import get_current_organization_id


HIDDEN_PHONE_LABEL = '[number hidden]'

# Short local numbers need word boundaries. Long phone-like sequences are also
# hidden inside freeform names/errors ("Call3035550123now"). Machine values must
# use their explicit DTO keys instead of this display-only helper.
_DATE = (
    r'\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])'
    r'(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?\b'
    r'|\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:19|20)\d{2}\b'
)
_PHONE = (
    r'(?<![\w])(?P<phone_open>\()?(?:\+|00)?\d(?:[\s\u200b\u2060\ufeff()./-]*\d){6,14}(?![\w])(?(phone_open)\)?)(?:\s*(?:ext\.?|x|#)\s*\d{1,6})?'
    r'|(?<!\d)(?:\+|00)?\d(?:[\s\u200b\u2060\ufeff()./-]*\d){9,14}(?!\d)'
)
_DISPLAY_PATTERN = re.compile(r'(?P<date>' + _DATE + r')|(?P<phone>' + _PHONE + r')', re.IGNORECASE)

_PHONE_KEYS = frozenset({
    'phone', 'phones', 'phone_number', 'phone_numbers', 'normalized_phone',
    'from_number', 'to_number', 'sender_number', 'recipient_phone',
    'recipient_phones', 'phone_e164', 'e164', 'contact_phone',
})
_MACHINE_KEYS = frozenset({
    'id', 'ids', 'record_id', 'record_ids', 'event_id', 'survey_id', 'session_id',
    'thread_id', 'member_id', 'registration_id', 'organization_id', 'user_id',
    'request_id', 'outbound_request_id', 'request_key', 'submission_key',
    'form_key', 'request_idempotency_key', 'acknowledged_reply_key', 'reply_draft_context',
    'job_id', 'message_id', 'logical_send_key', 'body_digest', 'payload_digest',
    'fingerprint', 'idempotency_key',
    'csrf_token', 'review_token', 'token', 'signature', 'nonce', 'session_nonce',
    'created_at', 'updated_at', 'completed_at', 'started_at', 'scheduled_at',
    'message_sid', 'provider_sid', 'sid',
})


def _actor(user=None):
    if user is not None:
        return user
    return current_user if has_request_context() else None


def can_view_phone_numbers(user=None) -> bool:
    """Use the active SaaS membership, never a staff user's legacy admin role."""
    user = _actor(user)
    if user is None or not bool(getattr(user, 'is_authenticated', False)):
        return False
    # Platform accounts already have an intentional separate HTTP boundary.
    # This helper does not grant them permission to enter tenant routes.
    if getattr(user, 'is_platform_admin', False) is True:
        return True
    if not has_app_context():
        return False
    if current_app.config.get('SAAS_MODE'):
        membership = getattr(user, 'organization_membership', None)
        if membership is None or getattr(membership, 'role', None) != 'owner':
            return False
        scoped_id = get_current_organization_id()
        if scoped_id is not None and getattr(membership, 'organization_id', None) != scoped_id:
            return False
        return True
    return getattr(user, 'effective_role', getattr(user, 'role', None)) == 'admin'


def staff_phone_privacy_applies(user=None) -> bool:
    user = _actor(user)
    return bool(user is not None and getattr(user, 'is_authenticated', False)
                and not can_view_phone_numbers(user))


def redact_phone_numbers(value) -> str:
    """Redact phone-like sequences in one freeform display value, not tokens."""
    text = '' if value is None else str(value)
    return _DISPLAY_PATTERN.sub(
        lambda match: HIDDEN_PHONE_LABEL if match.group('phone') else match.group(0), text,
    )


def contact_display_label(name, record_id=None, kind='Contact', user=None) -> str:
    """A name or record label that never falls back to a stored phone number."""
    value = str(name or '').strip()
    if not can_view_phone_numbers(user):
        value = redact_phone_numbers(value).strip()
    if value and value != HIDDEN_PHONE_LABEL:
        return value
    label = redact_phone_numbers(str(kind or 'Contact')).strip() or 'Contact'
    if isinstance(record_id, int) and not isinstance(record_id, bool) and record_id > 0:
        return f'{label} #{record_id}'
    return label


def require_contact_export_permission(view):
    """Deny a direct export request before executing a query or streaming rows."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return current_app.login_manager.unauthorized()
        if not can_view_phone_numbers():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def redact_private_json(payload):
    """Copy a staff-facing JSON value, omitting phone fields and redacting text.

    Semantic IDs/counts, timestamps and opaque tokens remain exact.  Any token
    must already be confidential: preserving an unencrypted signed token that
    embeds recipients would still disclose them.  Call only on response DTOs,
    never the server's actual dispatch intent.
    """
    aliases = {}

    def walk(value, key=None):
        if key in _MACHINE_KEYS and (value is None or isinstance(value, (str, int, float, bool))):
            return value
        if isinstance(value, Mapping):
            result = {}
            for field, item in value.items():
                normalized = str(field).lower()
                if normalized in _PHONE_KEYS:
                    continue
                output_field = field
                if isinstance(field, str) and redact_phone_numbers(field) != field:
                    output_field = aliases.setdefault(field, f'contact_{len(aliases) + 1}')
                result[output_field] = walk(item, normalized)
            return result
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, tuple):
            return [walk(item) for item in value]
        if isinstance(value, str):
            return redact_phone_numbers(value)
        return value

    return walk(payload)


def build_thread_display_previews(threads):
    """Read complete latest bodies before cutting staff's bounded sidebar text.

    The persisted rollup is already truncated and can contain a phone prefix.
    Keep that cache untouched; fetch full sources in one tenant-scoped query,
    rather than loading every thread's message relationship separately.
    """
    threads = list(threads)
    if can_view_phone_numbers():
        return {thread.id: thread.last_message_preview for thread in threads}
    if not threads:
        return {}
    from sqlalchemy import inspect
    from app.models import InboxMessage, InboxThread
    # Read persisted identities without refreshing one expired ORM object per
    # thread after a read-status commit.
    thread_ids = [inspect(thread).identity[0] for thread in threads]
    latest_body = (
        InboxMessage.query.with_entities(InboxMessage.body)
        .filter(InboxMessage.thread_id == InboxThread.id)
        .order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc())
        .limit(1).correlate(InboxThread).scalar_subquery()
    )
    rows = (InboxThread.query.with_entities(InboxThread.id, latest_body.label('body'))
            .filter(InboxThread.id.in_(thread_ids)).all())
    return {thread_id: (redact_phone_numbers(body)[:180] if body is not None
                        else 'Message preview unavailable') for thread_id, body in rows}
