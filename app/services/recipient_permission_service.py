"""Recipient permission with explicit organization sign-up contracts.

Most organizations use recorded purpose grants. AOC's confirmed sign-up
contract authorizes community communications across its newsletter, event and
in-person lists. Missing per-person records are not evidence of no consent.
Explicit opt-outs always take precedence over either affirmative source.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.exc import IntegrityError
from flask import current_app

from app import db
from app.models import CommunityMember, EventRegistration, Organization, RecipientPermissionEvidence, utc_now
from app.tenant import get_current_organization_id, organization_context, saas_mode_enabled, tenant_scope_is_active
from app.utils import normalize_phone, validate_phone


PERMISSION_PURPOSES = {'community', 'event', 'all'}
PERMISSION_DECISIONS = {'granted', 'denied', 'unknown'}
_message_permission: ContextVar[tuple[str, int | None]] = ContextVar(
    'message_permission', default=('community', None)
)


@dataclass(frozen=True)
class RecipientPermissionStatus:
    allowed: bool
    decision: str
    reason: str | None
    evidence_id: int | None = None
    source_label: str | None = None


def organization_has_confirmed_signup_consent(organization_id: int | None) -> bool:
    """Use an explicitly configured customer contract, within tenant scope."""
    if not organization_id or not saas_mode_enabled():
        return False
    configured = current_app.config.get('COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS') or []
    if isinstance(configured, str):
        configured = configured.split(',')
    slugs = {str(slug).strip() for slug in configured if str(slug).strip()}
    if not slugs:
        return False
    with _organization_scope(organization_id):
        organization = db.session.get(Organization, organization_id)
        return bool(organization and organization.slug in slugs)


def _signup_audience_phones(organization_id, phones, purpose, event_id):
    if not phones:
        return set()
    if purpose == 'event':
        return {row[0] for row in EventRegistration.query.with_entities(EventRegistration.phone).filter(
            EventRegistration.organization_id == organization_id,
            EventRegistration.event_id == event_id, EventRegistration.phone.in_(phones),
        ).all()}
    community = {row[0] for row in CommunityMember.query.with_entities(CommunityMember.phone).filter(
        CommunityMember.organization_id == organization_id, CommunityMember.phone.in_(phones),
    ).all()}
    community.update(row[0] for row in EventRegistration.query.with_entities(EventRegistration.phone).filter(
        EventRegistration.organization_id == organization_id, EventRegistration.phone.in_(phones),
    ).all())
    return community


def _confirmed_signup_status(entries, *, purpose, scope_key):
    transport = [entry for entry in entries if entry.purpose == 'all']
    latest_transport = max(transport, key=_evidence_order, default=None)
    if latest_transport is not None and latest_transport.decision == 'denied':
        return RecipientPermissionStatus(False, 'denied', 'permission_revoked', latest_transport.id)
    # A missing/false booking field never proved withdrawal from AOC's broad
    # community opt-in. Keep genuine owner withdrawals and address corrections.
    meaningful = []
    for entry in entries:
        if entry.purpose == 'all':
            continue
        if entry.source.startswith('event_sync:'):
            try:
                metadata = json.loads(entry.metadata_json or '{}')
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict) or not (
                metadata.get('reason') == 'booking_phone_changed'
                or (metadata.get('consent_basis') == 'confirmed_community_signup' and entry.decision == 'granted')
            ):
                continue
        meaningful.append(entry)
    recorded = _evaluate_permission(meaningful, purpose=purpose, scope_key=scope_key)
    if recorded.decision == 'denied':
        return recorded
    return RecipientPermissionStatus(
        True, 'granted', None, source_label='Community sign-up consent confirmed by the organization',
    )


def _utc_naive(value: datetime | None) -> datetime:
    value = value or utc_now()
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _scope_key(purpose: str, event_id: int | None) -> str:
    if purpose not in PERMISSION_PURPOSES:
        raise ValueError('Permission purpose must be community, event, or all.')
    if purpose == 'event':
        if not event_id or int(event_id) <= 0:
            raise ValueError('Event permission requires an event id.')
        return f'event:{int(event_id)}'
    if event_id is not None:
        raise ValueError('Only event permission accepts an event id.')
    return ''


def _bounded_identity(value: str, maximum: int) -> str:
    value = str(value or '').strip()
    if not value:
        raise ValueError('Permission source and evidence key are required.')
    if len(value) > maximum:
        return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()
    return value


@contextmanager
def _organization_scope(organization_id: int):
    organization_id = int(organization_id)
    if organization_id <= 0:
        raise ValueError('Permission evidence requires an organization id.')
    if tenant_scope_is_active() and get_current_organization_id() != organization_id:
        raise ValueError('Permission evidence belongs to another organization.')
    with organization_context(organization_id):
        yield


@contextmanager
def permission_context(purpose: str = 'community', event_id: int | None = None):
    """Carry a worker's reviewed audience purpose through the provider boundary."""
    _scope_key(purpose, event_id)
    if purpose == 'all':
        raise ValueError('All is a consent-revocation scope, not a sending purpose.')
    token = _message_permission.set((purpose, int(event_id) if event_id is not None else None))
    try:
        yield
    finally:
        _message_permission.reset(token)


def current_permission_context() -> tuple[str, int | None]:
    return _message_permission.get()


def record_permission_evidence(
    organization_id: int,
    phone: str,
    *,
    purpose: str,
    decision: str,
    source: str,
    evidence_key: str,
    occurred_at: datetime | None = None,
    event_id: int | None = None,
    metadata: dict | None = None,
    verified: bool = False,
) -> RecipientPermissionEvidence:
    """Append idempotent evidence in the caller's transaction; never commit it.

    A positive grant requires both verified=True and retained proof. This API is
    for trusted services/authorized owner actions, not raw request form binding.
    """
    normalized_phone = normalize_phone(phone)
    if not validate_phone(normalized_phone):
        raise ValueError('Permission evidence requires a valid phone number.')
    if decision not in PERMISSION_DECISIONS:
        raise ValueError('Permission decision must be granted, denied, or unknown.')
    scope_key = _scope_key(purpose, event_id)
    if purpose == 'all' and decision == 'granted':
        raise ValueError('A global grant cannot replace purpose-specific permission.')
    metadata = dict(metadata or {})
    proof = metadata.get('proof')
    if decision == 'granted' and (verified is not True or not isinstance(proof, str) or not proof.strip()):
        raise ValueError('An affirmative grant requires verified permission proof.')
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    if len(metadata_json.encode('utf-8')) > 20000:
        raise ValueError('Permission proof metadata is too large.')
    source = _bounded_identity(source, 80)
    evidence_key = _bounded_identity(evidence_key, 128)
    occurred_at_was_provided = occurred_at is not None
    occurred_at = _utc_naive(occurred_at)

    with _organization_scope(organization_id):
        query = RecipientPermissionEvidence.query.filter_by(
            organization_id=organization_id, source=source, evidence_key=evidence_key,
            purpose=purpose, scope_key=scope_key,
        )
        existing = query.first()
        if existing is not None:
            if (existing.phone, existing.decision, existing.verified) != (normalized_phone, decision, bool(verified)) or (
                occurred_at_was_provided and _utc_naive(existing.occurred_at) != occurred_at
            ):
                raise ValueError('Permission evidence identity conflicts with its original content.')
            return existing

        evidence = RecipientPermissionEvidence(
            organization_id=organization_id, phone=normalized_phone,
            purpose=purpose, scope_key=scope_key, decision=decision,
            source=source, evidence_key=evidence_key, verified=bool(verified),
            metadata_json=metadata_json, occurred_at=occurred_at,
            recorded_at=_utc_naive(utc_now()),
        )
        try:
            with db.session.begin_nested():
                db.session.add(evidence)
                db.session.flush()
        except IntegrityError:
            existing = query.first()
            if existing is None:
                raise
            if (existing.phone, existing.decision, existing.verified) != (normalized_phone, decision, bool(verified)) or (
                occurred_at_was_provided and _utc_naive(existing.occurred_at) != occurred_at
            ):
                raise ValueError('Permission evidence identity conflicts with its original content.')
            return existing
        return evidence


def _evidence_order(evidence: RecipientPermissionEvidence) -> tuple[datetime, int]:
    return (_utc_naive(evidence.occurred_at), int(evidence.id or 0))


def _evaluate_permission(
    evidence: list[RecipientPermissionEvidence], *, purpose: str, scope_key: str,
) -> RecipientPermissionStatus:
    global_entries = [entry for entry in evidence if entry.purpose == 'all']
    latest_global = max(global_entries, key=_evidence_order, default=None)
    if latest_global is not None and latest_global.decision == 'denied':
        return RecipientPermissionStatus(False, 'denied', 'permission_revoked', latest_global.id)
    last_revocation = max(
        (entry for entry in global_entries if entry.decision == 'denied'),
        key=_evidence_order, default=None,
    )

    relevant = [entry for entry in evidence if entry.purpose == purpose and entry.scope_key == scope_key]
    latest_by_source: dict[str, RecipientPermissionEvidence] = {}
    for entry in relevant:
        if entry.source not in latest_by_source or _evidence_order(entry) > _evidence_order(latest_by_source[entry.source]):
            latest_by_source[entry.source] = entry
    current = list(latest_by_source.values())
    # Unknown/new source revisions must not resurrect a grant withdrawn earlier.
    denials = [entry for entry in relevant if entry.decision == 'denied']
    latest_denial = max(denials, key=_evidence_order, default=None)
    def has_verified_proof(entry):
        try:
            metadata = json.loads(entry.metadata_json or '{}')
        except (TypeError, ValueError):
            return False
        return isinstance(metadata, dict) and isinstance(metadata.get('proof'), str) and bool(metadata['proof'].strip())

    grants = [entry for entry in current if entry.decision == 'granted' and entry.verified and has_verified_proof(entry)]
    latest_grant = max(grants, key=_evidence_order, default=None)
    if latest_grant is not None and (
        latest_denial is None or _utc_naive(latest_grant.occurred_at) > _utc_naive(latest_denial.occurred_at)
    ) and (
        last_revocation is None or _utc_naive(latest_grant.occurred_at) > _utc_naive(last_revocation.occurred_at)
    ):
        return RecipientPermissionStatus(True, 'granted', None, latest_grant.id)
    if last_revocation is not None and (
        latest_grant is None or _utc_naive(latest_grant.occurred_at) <= _utc_naive(last_revocation.occurred_at)
    ):
        return RecipientPermissionStatus(False, 'denied', 'permission_revoked', last_revocation.id)
    if latest_denial is not None:
        return RecipientPermissionStatus(False, 'denied', 'permission_denied', latest_denial.id)
    latest = max(current, key=_evidence_order, default=None)
    return RecipientPermissionStatus(False, 'unknown', 'permission_unknown', latest.id if latest else None)


def permission_status_by_phone(
    organization_id: int | None,
    phones: Iterable[str],
    *,
    purpose: str = 'community',
    event_id: int | None = None,
) -> dict[str, RecipientPermissionStatus]:
    normalized_phones = {normalize_phone(phone) for phone in phones if phone}
    normalized_phones.discard('')
    scope_key = _scope_key(purpose, event_id)
    if not saas_mode_enabled():
        return {phone: RecipientPermissionStatus(True, 'legacy_compatibility', None) for phone in normalized_phones}
    if not organization_id:
        return {phone: RecipientPermissionStatus(False, 'unknown', 'permission_unknown') for phone in normalized_phones}
    if purpose == 'all':
        raise ValueError('All is not a sending purpose.')
    with _organization_scope(organization_id):
        entries = RecipientPermissionEvidence.query.filter(
            RecipientPermissionEvidence.organization_id == organization_id,
            RecipientPermissionEvidence.phone.in_(normalized_phones),
            RecipientPermissionEvidence.purpose.in_([purpose, 'all']),
        ).all() if normalized_phones else []
        signup_phones = (
            _signup_audience_phones(organization_id, normalized_phones, purpose, event_id)
            if organization_has_confirmed_signup_consent(organization_id) else set()
        )
    grouped: dict[str, list[RecipientPermissionEvidence]] = {}
    for entry in entries:
        grouped.setdefault(entry.phone, []).append(entry)
    return {
        phone: (
            _confirmed_signup_status(grouped.get(phone, []), purpose=purpose, scope_key=scope_key)
            if phone in signup_phones else
            _evaluate_permission(grouped.get(phone, []), purpose=purpose, scope_key=scope_key)
        )
        for phone in normalized_phones
    }


def recipient_permission_status(
    organization_id: int | None, phone: str, *, purpose: str = 'community', event_id: int | None = None,
) -> RecipientPermissionStatus:
    normalized = normalize_phone(phone)
    if not validate_phone(normalized):
        return RecipientPermissionStatus(False, 'unknown', 'invalid_phone')
    return permission_status_by_phone(organization_id, [normalized], purpose=purpose, event_id=event_id)[normalized]


def recipient_permission_block_reason(
    organization_id: int | None, phone: str, *, purpose: str = 'community', event_id: int | None = None,
) -> str | None:
    return recipient_permission_status(organization_id, phone, purpose=purpose, event_id=event_id).reason


def recipient_permission_revocation_reason(organization_id: int | None, phone: str) -> str | None:
    """Transport STOP applies to conversational, test and account messages too."""
    if not saas_mode_enabled() or not organization_id:
        return None
    with _organization_scope(organization_id):
        latest = RecipientPermissionEvidence.query.filter_by(
            organization_id=organization_id, phone=normalize_phone(phone), purpose='all',
        ).order_by(RecipientPermissionEvidence.occurred_at.desc(), RecipientPermissionEvidence.id.desc()).first()
    return 'permission_revoked' if latest is not None and latest.decision == 'denied' else None


def filter_permission_recipients(
    recipients: list[dict], *, organization_id: int | None = None,
    purpose: str = 'community', event_id: int | None = None,
) -> tuple[list[dict], list[dict], set[str]]:
    if organization_id is None:
        organization_id = get_current_organization_id()
    statuses = permission_status_by_phone(
        organization_id, [recipient.get('phone') for recipient in recipients], purpose=purpose, event_id=event_id,
    )
    allowed, skipped, blocked = [], [], set()
    for recipient in recipients:
        phone = normalize_phone(recipient.get('phone'))
        status = statuses.get(phone, RecipientPermissionStatus(False, 'unknown', 'invalid_phone'))
        if status.allowed:
            allowed.append(recipient)
        else:
            skipped.append({**recipient, 'permission_reason': status.reason})
            blocked.add(phone)
    return allowed, skipped, blocked
