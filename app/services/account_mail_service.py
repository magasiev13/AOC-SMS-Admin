"""Authenticated SMTP delivery and durable single-use account mail."""
import hashlib
import hmac
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from urllib.parse import urlsplit

from flask import current_app

from app import db
from app.models import AccountMailDelivery, AppUser, OrganizationMembership, utc_now
from app.tenant import organization_context


def _valid_address(value):
    try:
        address = Address(addr_spec=value or '')
        return bool(address.username and address.domain and address.addr_spec == value)
    except (ValueError, IndexError, HeaderParseError):
        return False


def mail_ready():
    config = current_app.config
    base = urlsplit(config.get('APP_BASE_URL') or '')
    return bool(config.get('SAAS_MODE') and config.get('ACCOUNT_MAIL_ENABLED') and
                base.scheme == 'https' and base.hostname and not base.username and not base.password and
                not base.query and not base.fragment and config.get('ACCOUNT_MAIL_SMTP_PORT') in (465, 587) and
                _valid_address(config.get('ACCOUNT_MAIL_FROM')) and
                all(config.get(key) for key in ('ACCOUNT_MAIL_SMTP_HOST',
                                                'ACCOUNT_MAIL_SMTP_USER', 'ACCOUNT_MAIL_SMTP_PASSWORD')))


def _now():
    return utc_now().replace(tzinfo=None)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _identity(user):
    key = current_app.secret_key.encode()
    return hmac.new(key, f'{user.id}:{user.session_nonce}:{user.email}'.encode(), hashlib.sha256).hexdigest()


def _eligible(user, membership, kind):
    if (not user or not membership or user.is_platform_admin or not _valid_address(user.email) or
            membership.organization.status == 'suspended' or membership.user_id != user.id):
        return False
    if kind == 'verify':
        return user.verified_recovery_email != user.email
    return user.verified_recovery_email == user.email and (kind == 'recover' or
            (kind == 'reply' and membership.reply_notice_email == user.email))


def request_account_link(user_id, kind):
    if not mail_ready() or kind not in ('recover', 'verify'):
        return
    user = AppUser.query.filter_by(id=user_id).populate_existing().with_for_update().first()
    if not user or not _eligible(user, user.primary_membership, kind):
        return
    now = _now()
    if AccountMailDelivery.query.filter_by(user_id=user.id, kind=kind).filter(
            AccountMailDelivery.created_at > now - timedelta(minutes=2)).first():
        return
    db.session.add(AccountMailDelivery(organization_id=user.primary_membership.organization_id, user_id=user.id,
        kind=kind, identity_digest=_identity(user), dedupe_key=secrets.token_hex(24), expires_at=now + timedelta(minutes=15)))
    db.session.commit()


def save_reply_notices(inbound):
    """Called once inside the same transaction as the new unmatched inbound message."""
    if not mail_ready():
        return
    memberships = OrganizationMembership.query.filter(OrganizationMembership.organization_id == inbound.organization_id,
                                                        OrganizationMembership.reply_notice_email.is_not(None)).all()
    for membership in memberships:
        user = membership.user
        if _eligible(user, membership, 'reply'):
            db.session.add(AccountMailDelivery(organization_id=inbound.organization_id, user_id=user.id, kind='reply',
                identity_digest=_identity(user), dedupe_key=f'reply-{inbound.id}-{membership.id}',
                expires_at=_now() + timedelta(hours=1)))


class BeforeSubmissionError(Exception):
    """SMTP did not reach the message submission step. Retrying is safe."""


def _send(email):
    config = current_app.config
    port = config['ACCOUNT_MAIL_SMTP_PORT']
    options = dict(host=config['ACCOUNT_MAIL_SMTP_HOST'], port=port, timeout=8)
    context = ssl.create_default_context()
    client = None
    try:
        client = smtplib.SMTP_SSL(**options, context=context) if port == 465 else smtplib.SMTP(**options)
        if port == 587:
            client.starttls(context=context)
        client.login(config['ACCOUNT_MAIL_SMTP_USER'], config['ACCOUNT_MAIL_SMTP_PASSWORD'])
    except (OSError, smtplib.SMTPException) as exc:
        if client:
            client.close()
        raise BeforeSubmissionError() from exc
    try:
        client.send_message(email)
    finally:
        client.close()


def deliver_mail(organization_id, delivery_id):
    if not mail_ready():
        return
    with organization_context(organization_id):
        row = AccountMailDelivery.query.filter_by(organization_id=organization_id, id=delivery_id,
                                                  status='pending').with_for_update().first()
        if row is None:
            return
        user = db.session.get(AppUser, row.user_id)
        member = OrganizationMembership.query.filter_by(organization_id=organization_id, user_id=row.user_id).first()
        if row.expires_at <= _now() or not _eligible(user, member, row.kind) or row.identity_digest != _identity(user):
            row.status = 'expired'
            db.session.commit()
            return
        message = EmailMessage()
        message['From'] = current_app.config['ACCOUNT_MAIL_FROM']
        message['To'] = user.email
        base = current_app.config['APP_BASE_URL'].rstrip('/')
        token_digest = None
        if row.kind == 'reply':
            message['Subject'] = 'New reply in your Twinevia inbox'
            message.set_content(f'An incoming reply is ready to review. Sign in to open your workspace inbox.\n\n{base}/inbox')
        else:
            token = secrets.token_urlsafe(32)
            token_digest = _digest(token)
            action = 'Verify your email' if row.kind == 'verify' else 'Reset your password'
            message['Subject'] = f'{action} for Twinevia'
            message.set_content(f'{action} using this single-use link. It expires 15 minutes after the request.\n\n'
                                f'{base}/account/email/{row.kind}#{token}\n\nCode: {token}')
        # Persist the claim before SMTP. An interrupted submission is never automatically replayed.
        claimed = AccountMailDelivery.query.filter_by(id=delivery_id, status='pending').update(
            {'status': 'sending', 'token_digest': token_digest}, synchronize_session=False,
        )
        db.session.commit()
        if not claimed:
            return
        # Recheck after the durable claim. Hold the account and preference rows through
        # SMTP so an acknowledged opt-out or email change wins before a later submission.
        user = AppUser.query.filter_by(id=row.user_id).populate_existing().with_for_update().first()
        member = OrganizationMembership.query.filter_by(organization_id=organization_id, user_id=row.user_id
                                                        ).populate_existing().with_for_update().first()
        if not _eligible(user, member, row.kind) or row.identity_digest != _identity(user) or row.expires_at <= _now():
            row.status = 'expired'
            db.session.commit()
            return
        try:
            _send(message)
        except BeforeSubmissionError:
            AccountMailDelivery.query.filter_by(id=delivery_id, status='sending').update(
                {'status': 'pending', 'token_digest': None, 'next_enqueue_at': _now() + timedelta(minutes=2)})
            db.session.commit()
            current_app.logger.warning('Account mail connection unavailable for delivery %d', delivery_id)
            return
        except Exception:
            AccountMailDelivery.query.filter_by(id=delivery_id, status='sending').update({'status': 'unconfirmed'})
            db.session.commit()
            current_app.logger.warning('Account mail acceptance unconfirmed for delivery %d', delivery_id)
            return
        AccountMailDelivery.query.filter_by(id=delivery_id, status='sending').update({'status': 'accepted'})
        db.session.commit()


def recover_mail():
    """The scheduler queues pending mail. Workers take the final delivery claim."""
    if not mail_ready():
        return
    from app.queue import get_queue
    now = _now()
    rows = AccountMailDelivery.query.filter(AccountMailDelivery.status == 'pending',
        db.or_(AccountMailDelivery.next_enqueue_at.is_(None), AccountMailDelivery.next_enqueue_at <= now),
    ).order_by(AccountMailDelivery.id).limit(100).all()
    for row in rows:
        job_args = (row.organization_id, row.id)
        row.next_enqueue_at = now + timedelta(minutes=1)
        db.session.commit()
        try:
            get_queue().enqueue('app.services.account_mail_service.mail_job', *job_args, result_ttl=0)
        except Exception:
            current_app.logger.warning('Account mail enqueue delayed for delivery %d', job_args[1])
            break


def mail_job(organization_id, delivery_id):
    from app import create_app
    with create_app(run_startup_tasks=False, start_scheduler=False).app_context():
        deliver_mail(organization_id, delivery_id)


def lock_link(token, kind):
    if not mail_ready() or len(token) != 43:
        return None, None
    # Lock the user before the delivery so concurrent different recovery links
    # cannot both pass the session identity check.
    digest = _digest(token)
    owner = AccountMailDelivery.query.with_entities(AccountMailDelivery.user_id).filter_by(token_digest=digest, kind=kind).scalar()
    if owner is None:
        return None, None
    user = AppUser.query.filter_by(id=owner).populate_existing().with_for_update().first()
    row = AccountMailDelivery.query.filter_by(token_digest=digest, kind=kind).populate_existing().with_for_update().one()
    member = OrganizationMembership.query.filter_by(organization_id=row.organization_id, user_id=owner).first()
    if (row.status not in ('sending', 'accepted', 'unconfirmed') or row.expires_at <= _now() or
            not _eligible(user, member, kind) or row.identity_digest != _identity(user)):
        return None, None
    return row, user
