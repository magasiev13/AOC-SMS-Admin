import json
import re
import secrets
from datetime import datetime as dt, timezone

from flask_login import UserMixin
from sqlalchemy.orm import validates
from werkzeug.security import check_password_hash, generate_password_hash

from app import db
from app.utils import normalize_keyword, normalize_phone, validate_phone


def utc_now():
    return dt.now(timezone.utc)


def new_session_nonce() -> str:
    return secrets.token_hex(16)


def new_invitation_token() -> str:
    return secrets.token_urlsafe(24)


def slugify_organization_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return normalized[:64]


class AppUser(UserMixin, db.Model):
    """Application users with role-based access."""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, unique=True)
    email = db.Column(db.String(255), nullable=True, index=True)
    full_name = db.Column(db.String(120), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(20), nullable=True, index=True)
    role = db.Column(db.String(30), nullable=False, default='admin')
    is_platform_admin = db.Column(db.Boolean, default=False, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    session_nonce = db.Column(db.String(64), nullable=False, default=new_session_nonce)
    created_at = db.Column(db.DateTime, default=utc_now)
    memberships = db.relationship(
        'OrganizationMembership',
        back_populates='user',
        cascade='all, delete-orphan',
    )
    __table_args__ = (
        db.Index('ux_users_username_lower', db.func.lower(username), unique=True),
        db.Index('ux_users_email_lower', db.func.lower(email), unique=True),
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def get_id(self) -> str:
        nonce = self.session_nonce or ""
        return f"{self.id}:{nonce}"

    def rotate_session_nonce(self) -> None:
        self.session_nonce = new_session_nonce()

    @validates("username")
    def _normalize_username(self, key, value):
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("Username is required.")
        return normalized

    @validates("email")
    def _normalize_email(self, key, value):
        normalized = (value or "").strip().lower()
        return normalized or None

    @validates("phone")
    def _normalize_user_phone(self, key, value):
        if value is None:
            return None
        normalized = normalize_phone(value)
        return normalized or None

    @property
    def primary_membership(self):
        return self.memberships[0] if self.memberships else None

    @property
    def organization_membership(self):
        return self.primary_membership

    @property
    def organization(self):
        membership = self.primary_membership
        return membership.organization if membership is not None else None

    @property
    def organization_id(self):
        membership = self.primary_membership
        return membership.organization_id if membership is not None else None

    @property
    def organization_role(self):
        membership = self.primary_membership
        return membership.role if membership is not None else None

    @property
    def effective_role(self) -> str:
        if self.is_platform_admin:
            return 'admin'
        if self.organization_role == 'owner':
            return 'admin'
        if self.organization_role == 'staff':
            return 'social_manager'
        return self.role

    @property
    def is_admin(self) -> bool:
        return self.effective_role == 'admin'

    @property
    def is_social_manager(self) -> bool:
        return self.effective_role == 'social_manager'

    def __repr__(self):
        return f'<AppUser {self.username} role={self.effective_role}>'


class Organization(db.Model):
    """Business account boundary for multi-tenant SaaS mode."""
    __tablename__ = 'organizations'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(64), nullable=False, unique=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='active')
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    memberships = db.relationship(
        'OrganizationMembership',
        back_populates='organization',
        cascade='all, delete-orphan',
    )
    invitations = db.relationship(
        'OrganizationInvitation',
        back_populates='organization',
        cascade='all, delete-orphan',
    )
    subscription = db.relationship(
        'OrganizationSubscription',
        back_populates='organization',
        uselist=False,
        cascade='all, delete-orphan',
    )
    messaging_profile = db.relationship(
        'OrganizationMessagingProfile',
        back_populates='organization',
        uselist=False,
        cascade='all, delete-orphan',
    )

    @validates("name")
    def _normalize_name(self, key, value):
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("Organization name is required.")
        return normalized[:120]

    @validates("slug")
    def _normalize_slug(self, key, value):
        normalized = slugify_organization_name(value)
        if not normalized:
            raise ValueError("Organization slug is required.")
        return normalized

    def __repr__(self):
        return f'<Organization {self.slug}>'


class OrganizationMembership(db.Model):
    """Pilot-v1 single-org memberships with owner/staff roles."""
    __tablename__ = 'organization_memberships'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default='staff')
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    organization = db.relationship('Organization', back_populates='memberships')
    user = db.relationship('AppUser', back_populates='memberships')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'user_id', name='ux_org_memberships_org_user'),
    )

    @validates("role")
    def _normalize_role(self, key, value):
        normalized = (value or "").strip().lower()
        if normalized not in {'owner', 'staff'}:
            raise ValueError("Organization membership role must be owner or staff.")
        return normalized

    def __repr__(self):
        return f'<OrganizationMembership org={self.organization_id} user={self.user_id} role={self.role}>'


class OrganizationInvitation(db.Model):
    """Invite-only pilot onboarding for owner/staff users."""
    __tablename__ = 'organization_invitations'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default='staff')
    token = db.Column(db.String(128), nullable=False, unique=True, default=new_invitation_token)
    status = db.Column(db.String(20), nullable=False, default='pending')
    invited_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    accepted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    organization = db.relationship('Organization', back_populates='invitations')
    invited_by_user = db.relationship('AppUser')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'email', 'status', name='ux_org_invitations_org_email_status'),
    )

    @validates("email")
    def _normalize_invitation_email(self, key, value):
        normalized = (value or "").strip().lower()
        if not normalized:
            raise ValueError("Invitation email is required.")
        return normalized

    @validates("role")
    def _normalize_invitation_role(self, key, value):
        normalized = (value or "").strip().lower()
        if normalized not in {'owner', 'staff'}:
            raise ValueError("Invitation role must be owner or staff.")
        return normalized

    def __repr__(self):
        return f'<OrganizationInvitation org={self.organization_id} email={self.email} status={self.status}>'


class OrganizationSubscription(db.Model):
    """Stripe subscription state for one organization."""
    __tablename__ = 'organization_subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, unique=True, index=True)
    stripe_customer_id = db.Column(db.String(80), nullable=True, unique=True)
    stripe_subscription_id = db.Column(db.String(80), nullable=True, unique=True)
    stripe_price_id = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(30), nullable=False, default='incomplete')
    current_period_end = db.Column(db.DateTime, nullable=True)
    cancel_at_period_end = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    organization = db.relationship('Organization', back_populates='subscription')

    def __repr__(self):
        return f'<OrganizationSubscription org={self.organization_id} status={self.status}>'


class StripeWebhookEvent(db.Model):
    """Minimal Stripe webhook ledger for idempotency and audit."""
    __tablename__ = 'stripe_webhook_events'

    id = db.Column(db.Integer, primary_key=True)
    stripe_event_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    event_type = db.Column(db.String(80), nullable=False, index=True)
    stripe_object_id = db.Column(db.String(80), nullable=True, index=True)
    stripe_customer_id = db.Column(db.String(80), nullable=True, index=True)
    stripe_subscription_id = db.Column(db.String(80), nullable=True, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    signature_verified = db.Column(db.Boolean, nullable=False, default=False)
    event_created_at = db.Column(db.DateTime, nullable=True)
    received_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=utc_now, onupdate=utc_now)
    processed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='processing', index=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=1)
    last_error = db.Column(db.Text, nullable=True)

    organization = db.relationship('Organization')

    def __repr__(self):
        return f'<StripeWebhookEvent {self.stripe_event_id} status={self.status}>'


class PlatformServiceRestartRequest(db.Model):
    """Durable platform restart requests processed outside the web request path."""
    __tablename__ = 'platform_service_restart_requests'

    id = db.Column(db.Integer, primary_key=True)
    requested_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    requested_username = db.Column(db.String(80), nullable=True, index=True)
    client_ip = db.Column(db.String(45), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    transient_unit = db.Column(db.String(120), nullable=True, unique=True)
    summary = db.Column(db.Text, nullable=True)
    detail = db.Column(db.Text, nullable=True)
    requested_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    last_checked_at = db.Column(db.DateTime, nullable=True, index=True)

    requested_by_user = db.relationship('AppUser')

    @validates("status")
    def _normalize_status(self, key, value):
        normalized = (value or "").strip().lower()
        if normalized not in {"pending", "queued", "succeeded", "failed"}:
            raise ValueError("Platform restart request status must be pending, queued, succeeded, or failed.")
        return normalized

    def __repr__(self):
        return f'<PlatformServiceRestartRequest {self.id} status={self.status}>'


class OrganizationMessagingProfile(db.Model):
    """Provider-managed messaging resources for one organization."""
    __tablename__ = 'organization_messaging_profiles'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, unique=True, index=True)
    provider_mode = db.Column(db.String(30), nullable=False, default='platform_managed')
    twilio_subaccount_sid = db.Column(db.String(64), nullable=True, unique=True)
    twilio_auth_token_encrypted = db.Column(db.Text, nullable=True)
    credential_reference = db.Column(db.String(255), nullable=True)
    messaging_service_sid = db.Column(db.String(64), nullable=True, unique=True)
    phone_number_sid = db.Column(db.String(64), nullable=True, unique=True)
    from_number = db.Column(db.String(20), nullable=True, unique=True)
    inbound_identity = db.Column(db.String(64), nullable=True, unique=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    provider_status = db.Column(db.String(20), nullable=False, default='pending')
    business_type = db.Column(db.String(80), nullable=True)
    use_case = db.Column(db.String(120), nullable=True)
    consent_acknowledged_at = db.Column(db.DateTime, nullable=True)
    sender_review_status = db.Column(db.String(20), nullable=False, default='pending')
    provisioning_started_at = db.Column(db.DateTime, nullable=True)
    provisioned_at = db.Column(db.DateTime, nullable=True)
    suspended_at = db.Column(db.DateTime, nullable=True)
    provider_last_checked_at = db.Column(db.DateTime, nullable=True)
    last_provision_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    organization = db.relationship('Organization', back_populates='messaging_profile')

    @validates("provider_mode")
    def _normalize_provider_mode(self, key, value):
        normalized = (value or "").strip().lower()
        if normalized not in {"platform_managed", "customer_managed"}:
            raise ValueError("Provider mode must be platform_managed or customer_managed.")
        return normalized

    @validates("status", "provider_status")
    def _normalize_provider_status(self, key, value):
        normalized = (value or "").strip().lower()
        if normalized not in {"pending", "provisioning", "active", "suspended", "error"}:
            raise ValueError("Provider status must be pending, provisioning, active, suspended, or error.")
        return normalized

    @validates("sender_review_status")
    def _normalize_sender_review_status(self, key, value):
        normalized = (value or "").strip().lower()
        if normalized not in {"pending", "approved", "rejected"}:
            raise ValueError("Sender review status must be pending, approved, or rejected.")
        return normalized

    @validates("from_number")
    def _normalize_from_number(self, key, value):
        if value is None:
            return None
        normalized = normalize_phone(value)
        return normalized or None

    @validates("inbound_identity")
    def _normalize_inbound_identity(self, key, value):
        normalized = (value or "").strip()
        return normalized or None

    @property
    def active_sender_identity(self) -> str | None:
        return self.from_number or self.messaging_service_sid

    @property
    def can_send(self) -> bool:
        return self.provider_status == 'active' and bool(self.active_sender_identity)

    def set_provider_status(self, value: str) -> None:
        normalized = self._normalize_provider_status("provider_status", value)
        self.provider_status = normalized
        self.status = normalized

    def __repr__(self):
        return f'<OrganizationMessagingProfile org={self.organization_id} status={self.provider_status}>'


class OrganizationProviderAuditLog(db.Model):
    """Audit trail for provider lifecycle actions."""
    __tablename__ = 'organization_provider_audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    action = db.Column(db.String(40), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='success', index=True)
    message = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)

    organization = db.relationship('Organization')
    actor_user = db.relationship('AppUser')

    def __repr__(self):
        return f'<OrganizationProviderAuditLog org={self.organization_id} action={self.action}>'


class MessagingUsageRecord(db.Model):
    """Per-message usage ledger for outbound billing reconciliation."""
    __tablename__ = 'messaging_usage_records'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    message_sid = db.Column(db.String(64), nullable=False, unique=True, index=True)
    direction = db.Column(db.String(10), nullable=False, default='outbound', index=True)
    source = db.Column(db.String(20), nullable=False, default='blast')
    twilio_subaccount_sid = db.Column(db.String(64), nullable=True, index=True)
    twilio_message_status = db.Column(db.String(30), nullable=True, index=True)
    provider_currency = db.Column(db.String(8), nullable=False, default='usd')
    provider_cost = db.Column(db.Numeric(12, 4), nullable=False, default=0)
    sell_rate = db.Column(db.Numeric(12, 4), nullable=False, default=0)
    sell_amount = db.Column(db.Numeric(12, 4), nullable=False, default=0)
    margin = db.Column(db.Numeric(12, 4), nullable=False, default=0)
    billable_units = db.Column(db.Integer, nullable=False, default=0)
    billable = db.Column(db.Boolean, nullable=False, default=False)
    reconciliation_status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    billing_period_start = db.Column(db.DateTime, nullable=True, index=True)
    billing_period_end = db.Column(db.DateTime, nullable=True, index=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    reconciled_at = db.Column(db.DateTime, nullable=True)

    organization = db.relationship('Organization')

    def __repr__(self):
        return f'<MessagingUsageRecord org={self.organization_id} sid={self.message_sid}>'


class OrganizationUsageBillingPeriod(db.Model):
    """Closed-period usage summary for overage posting."""
    __tablename__ = 'organization_usage_billing_periods'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=False, index=True)
    period_start = db.Column(db.DateTime, nullable=False)
    period_end = db.Column(db.DateTime, nullable=False)
    included_units = db.Column(db.Integer, nullable=False, default=0)
    used_units = db.Column(db.Integer, nullable=False, default=0)
    overage_units = db.Column(db.Integer, nullable=False, default=0)
    sell_amount = db.Column(db.Numeric(12, 4), nullable=False, default=0)
    currency = db.Column(db.String(8), nullable=False, default='usd')
    stripe_invoice_item_id = db.Column(db.String(80), nullable=True, unique=True)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    posted_at = db.Column(db.DateTime, nullable=True)

    organization = db.relationship('Organization')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'period_start', 'period_end', name='ux_org_usage_period'),
    )

    def __repr__(self):
        return f'<OrganizationUsageBillingPeriod org={self.organization_id} start={self.period_start}>'


class CommunityMember(db.Model):
    """Recipients for community-wide SMS blasts."""
    __tablename__ = 'community_members'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)
    organization = db.relationship('Organization')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'phone', name='ux_community_members_org_phone'),
    )

    @validates("phone")
    def _normalize_member_phone(self, key, value):
        normalized = normalize_phone(value)
        if not validate_phone(normalized):
            raise ValueError("Community member phone must be a valid E.164 number.")
        return normalized

    def __repr__(self):
        return f'<CommunityMember {self.phone}>'


class UnsubscribedContact(db.Model):
    """Phone numbers that should not receive messages."""
    __tablename__ = 'unsubscribed_contacts'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(50), nullable=False, default='manual')
    created_at = db.Column(db.DateTime, default=utc_now)
    organization = db.relationship('Organization')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'phone', name='ux_unsubscribed_contacts_org_phone'),
    )

    @validates("phone")
    def _normalize_unsubscribed_phone(self, key, value):
        normalized = normalize_phone(value)
        if not validate_phone(normalized):
            raise ValueError("Unsubscribed contact phone must be a valid E.164 number.")
        return normalized

    def __repr__(self):
        return f'<UnsubscribedContact {self.phone}>'


class SuppressedContact(db.Model):
    """Phone numbers that should not receive messages for specific reasons."""
    __tablename__ = 'suppressed_contacts'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    reason = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(20), nullable=False)
    source = db.Column(db.String(50), nullable=True)
    source_type = db.Column(db.String(50), nullable=True)
    source_message_log_id = db.Column(db.Integer, db.ForeignKey('message_logs.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    message_log = db.relationship('MessageLog')
    organization = db.relationship('Organization')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'phone', name='ux_suppressed_contacts_org_phone'),
    )

    @validates('phone')
    def _normalize_phone(self, key, value):
        return normalize_phone(value)

    def __repr__(self):
        return f'<SuppressedContact {self.phone} category={self.category}>'


class Event(db.Model):
    """Event definitions."""
    __tablename__ = 'events'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    organization = db.relationship('Organization')
    registrations = db.relationship('EventRegistration', back_populates='event', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Event {self.title}>'


class EventRegistration(db.Model):
    """Recipients registered for a specific event (separate from community members)."""
    __tablename__ = 'event_registrations'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)
    name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    event = db.relationship('Event', back_populates='registrations')
    organization = db.relationship('Organization')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'event_id', 'phone', name='unique_org_event_phone'),
    )

    @validates("phone")
    def _normalize_registration_phone(self, key, value):
        normalized = normalize_phone(value)
        if not validate_phone(normalized):
            raise ValueError("Event registration phone must be a valid E.164 number.")
        return normalized

    def __repr__(self):
        return f'<EventRegistration event={self.event_id} phone={self.phone}>'


class MessageLog(db.Model):
    """Log of sent SMS blasts."""
    __tablename__ = 'message_logs'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    message_body = db.Column(db.Text, nullable=False)
    target = db.Column(db.String(20), nullable=False)  # 'community' or 'event'
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=True)
    status = db.Column(db.String(20), default='sent')  # 'processing', 'sent', 'failed'
    total_recipients = db.Column(db.Integer, default=0)
    success_count = db.Column(db.Integer, default=0)
    failure_count = db.Column(db.Integer, default=0)
    details = db.Column(db.Text, nullable=True)  # JSON string of per-recipient results

    event = db.relationship('Event')
    organization = db.relationship('Organization')

    def __repr__(self):
        return f'<MessageLog {self.id} target={self.target}>'


class InboxThread(db.Model):
    """Conversation threads grouped by phone number."""
    __tablename__ = 'inbox_threads'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    contact_name = db.Column(db.String(100), nullable=True)
    unread_count = db.Column(db.Integer, default=0, nullable=False)
    last_message_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)
    last_message_preview = db.Column(db.Text, nullable=True)
    last_direction = db.Column(db.String(10), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    organization = db.relationship('Organization')
    messages = db.relationship(
        'InboxMessage',
        back_populates='thread',
        cascade='all, delete-orphan',
        order_by='InboxMessage.created_at',
    )

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'phone', name='ux_inbox_threads_org_phone'),
    )

    def __repr__(self):
        return f'<InboxThread {self.phone}>'


class InboxMessage(db.Model):
    """Inbound and outbound messages shown in the shared inbox."""
    __tablename__ = 'inbox_messages'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('inbox_threads.id'), nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)  # inbound/outbound
    body = db.Column(db.Text, nullable=False)
    message_sid = db.Column(db.String(64), nullable=True, unique=True)
    automation_source = db.Column(db.String(30), nullable=True)
    automation_source_id = db.Column(db.Integer, nullable=True)
    matched_keyword = db.Column(db.String(40), nullable=True)
    delivery_status = db.Column(db.String(30), nullable=True)
    delivery_error = db.Column(db.Text, nullable=True)
    raw_payload = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)

    thread = db.relationship('InboxThread', back_populates='messages')
    organization = db.relationship('Organization')

    def __repr__(self):
        return f'<InboxMessage {self.id} {self.direction} {self.phone}>'


class KeywordAutomationRule(db.Model):
    """Keyword-based automated replies for inbound SMS."""
    __tablename__ = 'keyword_automation_rules'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    keyword = db.Column(db.String(40), nullable=False, index=True)
    response_body = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    match_count = db.Column(db.Integer, default=0, nullable=False)
    last_matched_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    organization = db.relationship('Organization')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'keyword', name='ux_keyword_rules_org_keyword'),
    )

    @validates('keyword')
    def _normalize_keyword(self, key, value):
        return normalize_keyword(value)

    def __repr__(self):
        return f'<KeywordAutomationRule {self.keyword}>'


class SurveyFlow(db.Model):
    """Multi-step inbound survey started by a keyword."""
    __tablename__ = 'survey_flows'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    trigger_keyword = db.Column(db.String(40), nullable=False, index=True)
    intro_message = db.Column(db.Text, nullable=True)
    questions_json = db.Column(db.Text, nullable=False, default='[]')
    completion_message = db.Column(db.Text, nullable=True)
    linked_event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    start_count = db.Column(db.Integer, default=0, nullable=False)
    completion_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    linked_event = db.relationship('Event')
    organization = db.relationship('Organization')
    sessions = db.relationship('SurveySession', back_populates='survey')
    responses = db.relationship('SurveyResponse', back_populates='survey')

    __table_args__ = (
        db.UniqueConstraint('organization_id', 'name', name='ux_survey_flows_org_name'),
        db.UniqueConstraint('organization_id', 'trigger_keyword', name='ux_survey_flows_org_trigger_keyword'),
    )

    @validates('trigger_keyword')
    def _normalize_trigger_keyword(self, key, value):
        return normalize_keyword(value)

    @property
    def questions(self) -> list[str]:
        try:
            payload = json.loads(self.questions_json or '[]')
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [str(item).strip() for item in payload if str(item).strip()]

    def set_questions(self, questions: list[str]) -> None:
        self.questions_json = json.dumps([question.strip() for question in questions if question and question.strip()])

    def __repr__(self):
        return f'<SurveyFlow {self.name} keyword={self.trigger_keyword}>'


class SurveySession(db.Model):
    """Per-phone active/completed survey progress."""
    __tablename__ = 'survey_sessions'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey_flows.id'), nullable=False, index=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('inbox_threads.id'), nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    status = db.Column(db.String(20), default='active', nullable=False)  # active/completed/cancelled
    current_question_index = db.Column(db.Integer, default=0, nullable=False)
    started_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    last_activity_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('ix_survey_sessions_phone_status', 'phone', 'status'),
        db.Index('ix_survey_sessions_org_phone_status', 'organization_id', 'phone', 'status'),
    )

    survey = db.relationship('SurveyFlow', back_populates='sessions')
    organization = db.relationship('Organization')
    thread = db.relationship('InboxThread')
    responses = db.relationship(
        'SurveyResponse',
        back_populates='session',
        cascade='all, delete-orphan',
        order_by='SurveyResponse.question_index',
    )

    def __repr__(self):
        return f'<SurveySession survey={self.survey_id} phone={self.phone} status={self.status}>'


class SurveyResponse(db.Model):
    """Captured answer for one survey question."""
    __tablename__ = 'survey_responses'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('survey_sessions.id'), nullable=False, index=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey_flows.id'), nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    question_index = db.Column(db.Integer, nullable=False)
    question_prompt = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    session = db.relationship('SurveySession', back_populates='responses')
    survey = db.relationship('SurveyFlow', back_populates='responses')
    organization = db.relationship('Organization')

    def __repr__(self):
        return f'<SurveyResponse session={self.session_id} q={self.question_index}>'


class ScheduledMessage(db.Model):
    """Scheduled SMS blasts for future sending."""
    __tablename__ = 'scheduled_messages'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    message_body = db.Column(db.Text, nullable=False)
    target = db.Column(db.String(20), nullable=False)  # 'community' or 'event'
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=True)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'processing', 'sent', 'failed', 'expired', 'cancelled'
    test_mode = db.Column(db.Boolean, default=False)  # If true, send only to admin test phone
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    next_retry_at = db.Column(db.DateTime, nullable=True)
    processing_started_at = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    message_log_id = db.Column(db.Integer, db.ForeignKey('message_logs.id'), nullable=True)

    event = db.relationship('Event')
    message_log = db.relationship('MessageLog')
    organization = db.relationship('Organization')

    def __repr__(self):
        return f'<ScheduledMessage {self.id} scheduled={self.scheduled_at} status={self.status}>'


class UserPasswordHistory(db.Model):
    """Recent password hashes used to prevent password reuse."""
    __tablename__ = 'user_password_history'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)

    user = db.relationship('AppUser')

    def __repr__(self):
        return f'<UserPasswordHistory user_id={self.user_id} created_at={self.created_at}>'


class AuthEvent(db.Model):
    """Security-relevant auth events for incident review."""
    __tablename__ = 'auth_events'

    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(50), nullable=False, index=True)
    outcome = db.Column(db.String(20), nullable=False, default='success')
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    username = db.Column(db.String(80), nullable=True, index=True)
    client_ip = db.Column(db.String(45), nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)

    user = db.relationship('AppUser')
    organization = db.relationship('Organization')

    @property
    def metadata_payload(self) -> dict:
        if not self.metadata_json:
            return {}
        try:
            payload = json.loads(self.metadata_json)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def set_metadata(self, payload: dict | None) -> None:
        if not payload:
            self.metadata_json = None
            return
        self.metadata_json = json.dumps(payload)

    def __repr__(self):
        return f'<AuthEvent {self.event_type} outcome={self.outcome}>'


class LoginAttempt(db.Model):
    """Track failed login attempts for rate limiting across workers."""
    __tablename__ = 'login_attempts'

    id = db.Column(db.Integer, primary_key=True)
    client_ip = db.Column(db.String(45), nullable=False, index=True)
    username = db.Column(db.String(80), nullable=False, default='')
    attempt_count = db.Column(db.Integer, default=1, nullable=False)
    first_attempt_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index('ux_login_attempts_client_ip_username', 'client_ip', 'username', unique=True),
    )

    def __repr__(self):
        username = self.username or "<ip-only>"
        return f'<LoginAttempt {self.client_ip}/{username} count={self.attempt_count}>'
