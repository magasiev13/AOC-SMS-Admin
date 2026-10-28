"""Short-lived signed review of an exact audience; no persistence or provider work."""
import hashlib
import json
import base64
import hmac

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from cryptography.fernet import Fernet, InvalidToken


class SendReviewInputError(ValueError):
    """An explicitly authored validation message safe for the composer."""

    def __init__(self, public_message):
        self.public_message = public_message
        super().__init__(public_message)


def _signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='twinevia-send-review-v1')


def _actor(user, organization_id):
    return [organization_id, user.id, hashlib.sha256(str(user.session_nonce).encode()).hexdigest()]


def _review_cipher():
    secret = current_app.secret_key
    secret = secret.encode() if isinstance(secret, str) else secret
    key = hmac.new(secret, b'twinevia-private-send-review-v2', hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def freeze_review(user, organization_id, form_key, intent, recipients):
    limit = int(current_app.config.get('RECIPIENT_SNAPSHOT_MAX_BYTES', 1024 * 1024) or 1024 * 1024)
    if len(json.dumps(recipients, sort_keys=True).encode()) > limit:
        raise SendReviewInputError('The reviewed recipient snapshot exceeds the configured size limit.')
    # Signing authenticates a snapshot; it does not hide its contact numbers
    # from the browser. Keep the entire audience/intent encrypted for all roles.
    payload = {'actor': _actor(user, organization_id), 'key': form_key, 'intent': intent, 'recipients': recipients}
    encrypted = _review_cipher().encrypt(json.dumps(payload, separators=(',', ':')).encode()).decode()
    return _signer().dumps({'version': 2, 'encrypted': encrypted})


def reviewed_recipients(token, user, organization_id, form_key, intent):
    try:
        payload = _signer().loads(token, max_age=900)
    except SignatureExpired as exc:
        raise ValueError('This review expired after 15 minutes. Review the audience again before sending.') from exc
    except BadSignature as exc:
        raise ValueError('The send review could not be verified. Review the audience again.') from exc
    if isinstance(payload, dict) and payload.get('version') == 2:
        try:
            payload = json.loads(_review_cipher().decrypt(payload['encrypted'].encode()))
        except (KeyError, AttributeError, InvalidToken, ValueError, TypeError) as exc:
            raise ValueError('The send review could not be verified. Review the audience again.') from exc
    else:
        from app.services.contact_privacy_service import can_view_phone_numbers
        if not can_view_phone_numbers(user):
            raise ValueError('This review must be refreshed. Review the audience again.')
    if not isinstance(payload, dict) or payload.get('actor') != _actor(user, organization_id) or payload.get('key') != form_key or payload.get('intent') != intent:
        raise ValueError('The message, audience, timing or signed-in account changed. Review this send again.')
    recipients = payload.get('recipients')
    if not isinstance(recipients, list) or not recipients or any(not isinstance(row, dict) or not row.get('phone') for row in recipients):
        raise ValueError('The reviewed audience is unavailable. Review the audience again.')
    return recipients
