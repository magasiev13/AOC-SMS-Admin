"""Match an affirmative hosted signup to a signed inbound text from the same number."""
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlsplit

from flask import current_app

from app import db
from app.models import CommunityMember, ConsentSource, RecipientPermissionEvidence
from app.services.recipient_permission_service import record_permission_evidence
from app.services.recipient_service import get_suppressed_phone_set, get_unsubscribed_phone_set
from app.services.twilio_a2p_service import hosted_a2p_compliance_urls
from app.utils import normalize_phone, validate_phone


JOIN_COMMAND = re.compile(r'JOIN TV([1-9][0-9]{0,9})-([0-9A-F]{8})-([0-9A-F]{12})-([0-9A-F]{24})', re.I)
JOIN_VALID_SECONDS = 15 * 60
STANDARD_TERMS = 'Message frequency varies. Message and data rates may apply. Reply STOP to opt out or HELP for help.'


def public_details(organization, help_contact):
    profile = organization.messaging_profile
    contact = (help_contact or '').strip()
    if (organization.status != 'active' or not profile or not profile.can_send
            or not validate_phone(profile.from_number) or not 3 <= len(contact) <= 120):
        return None
    urls = hosted_a2p_compliance_urls(organization)
    if any(urlsplit(urls[key]).scheme != 'https' or not urlsplit(urls[key]).hostname
           for key in ('privacy_policy_url', 'terms_and_conditions_url')):
        return None
    onboarding = organization.a2p_onboarding
    sender = (onboarding.public_brand_name if onboarding else None) or organization.name
    return {'sender': sender, 'number': profile.from_number, 'help_contact': contact,
            'terms': STANDARD_TERMS, 'privacy_url': urls['privacy_policy_url'],
            'terms_url': urls['terms_and_conditions_url']}


def is_published(source, organization):
    details = source.public_details or {}
    return (source.kind == 'hosted' and source.status == 'approved' and bool(details)
            and details == public_details(organization, details.get('help_contact')))


def _signature(source, phone, prefix):
    key = current_app.config['SECRET_KEY']
    data = f'twinevia-hosted-signup-v1|{source.organization_id}|{phone}|{prefix}'.encode()
    return hmac.new(key if isinstance(key, bytes) else key.encode(), data, hashlib.sha256).hexdigest()[:24].upper()


def confirmation_command(source, phone):
    prefix = f'TV{source.id}-{int(time.time()):08X}-{secrets.token_hex(6).upper()}'
    return f'JOIN {prefix}-{_signature(source, phone, prefix)}'


def confirm_inbound(match, organization, phone, destination, message_sid):
    """Use the existing authenticated webhook transaction and its locked inbox thread."""
    source_id, issued, nonce, signature = (value.upper() for value in match.groups())
    age = int(time.time()) - int(issued, 16)
    if not 0 <= age <= JOIN_VALID_SECONDS or not message_sid:
        return 'signup_unconfirmed'
    source = ConsentSource.query.filter_by(id=int(source_id), organization_id=organization.id,
                                           kind='hosted', status='approved').with_for_update().first()
    if source is None or not is_published(source, organization):
        return 'signup_unconfirmed'
    if normalize_phone(destination) != source.public_details['number']:
        return 'signup_unconfirmed'
    prefix = f'TV{source_id}-{issued}-{nonce}'
    if not hmac.compare_digest(signature, _signature(source, phone, prefix)):
        return 'signup_unconfirmed'
    if get_unsubscribed_phone_set([phone]) or get_suppressed_phone_set([phone]):
        return 'signup_blocked'

    source_key = f'hosted_signup:{source.id}'
    if RecipientPermissionEvidence.query.filter_by(organization_id=organization.id, source=source_key,
                                                  evidence_key=prefix).first():
        return 'signup_already_confirmed'
    record_permission_evidence(
        organization.id, phone, purpose=source.purpose, event_id=source.event_id, decision='granted',
        source=source_key, evidence_key=prefix, verified=True,
        metadata={'proof': message_sid, 'method': 'hosted form and inbound JOIN', 'disclosure': source.disclosure,
                  'public_terms': source.public_details, 'source_id': source.id},
    )
    if CommunityMember.query.filter_by(organization_id=organization.id, phone=phone).first() is None:
        db.session.add(CommunityMember(organization_id=organization.id, phone=phone, individual_consent_required=True))
    return 'signup_confirmed'
