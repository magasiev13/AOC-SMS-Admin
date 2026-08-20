from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    AppUser,
    Organization,
    OrganizationA2POnboarding,
    OrganizationInvitation,
    OrganizationMessagingProfile,
    OrganizationSubscription,
    PilotApplication,
    PilotApplicationStatusHistory,
    slugify_organization_name,
    utc_now,
)
from app.utils import normalize_phone, validate_phone


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TWILIO_ACCOUNT_STATUSES = {
    "none",
    "existing_account",
    "existing_number",
    "needs_guidance",
}


class PilotApplicationError(ValueError):
    """Raised when a pilot application or review transition is invalid."""


class PilotApplicationRateLimitError(PilotApplicationError):
    """Raised before storage when one source exceeds the pilot intake limit."""


@dataclass(frozen=True)
class PilotApplicationSubmission:
    business_name: str
    contact_name: str
    email: str
    phone: str | None
    website_url: str | None
    use_case: str
    expected_monthly_segments: str | int | None
    twilio_account_status: str | None
    honeypot: str | None


@dataclass(frozen=True)
class PilotApprovalResult:
    application: PilotApplication
    organization: Organization
    invitation: OrganizationInvitation


def _bounded_text(raw_value: object, label: str, minimum: int, maximum: int) -> str:
    value = str(raw_value or "").strip()
    if len(value) < minimum:
        raise PilotApplicationError(f"{label} must be at least {minimum} characters.")
    if len(value) > maximum:
        raise PilotApplicationError(f"{label} must be no more than {maximum} characters.")
    return value


def _normalized_email(raw_value: str) -> str:
    email = _bounded_text(raw_value, "Email", 3, 255).lower()
    if EMAIL_PATTERN.fullmatch(email) is None:
        raise PilotApplicationError("Enter a valid business email address.")
    return email


def _normalized_phone(raw_value: str | None) -> str | None:
    if not str(raw_value or "").strip():
        return None
    phone = normalize_phone(str(raw_value))
    if not phone or not validate_phone(phone):
        raise PilotApplicationError("Phone number must be valid E.164 format.")
    return phone


def _normalized_website_url(raw_value: str | None) -> str | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    if len(value) > 255:
        raise PilotApplicationError("Website URL must be no more than 255 characters.")
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise PilotApplicationError("Website URL must be a valid public HTTPS URL.")
    if parsed.username or parsed.password:
        raise PilotApplicationError("Website URL must not contain user information.")
    return value


def _normalized_expected_segments(raw_value: str | int | None) -> int | None:
    if raw_value is None or str(raw_value).strip() == "":
        return None
    try:
        segments = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise PilotApplicationError("Expected monthly segments must be a whole number.") from exc
    if segments < 0 or segments > 1_000_000:
        raise PilotApplicationError("Expected monthly segments must be between 0 and 1,000,000.")
    return segments


def _normalized_twilio_status(raw_value: str | None) -> str | None:
    value = str(raw_value or "").strip().lower()
    if not value:
        return None
    if value not in TWILIO_ACCOUNT_STATUSES:
        raise PilotApplicationError("Choose a valid Twilio account status.")
    return value


def _source_ip_hash(source_ip: str) -> str:
    secret = str(current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    if not secret:
        raise PilotApplicationError("Pilot intake cannot run without an application secret.")
    return hmac.new(
        secret,
        f"pilot-intake:{source_ip}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _enforce_rate_limit(source_hash: str) -> None:
    limit = int(current_app.config["PILOT_APPLICATION_RATE_LIMIT_COUNT"])
    window_seconds = int(current_app.config["PILOT_APPLICATION_RATE_LIMIT_WINDOW_SECONDS"])
    cutoff = utc_now() - timedelta(seconds=window_seconds)
    recent_count = PilotApplication.query.filter(
        PilotApplication.source_ip_hash == source_hash,
        PilotApplication.created_at >= cutoff,
    ).count()
    if recent_count >= limit:
        raise PilotApplicationRateLimitError(
            "Too many pilot requests were submitted from this network. Please try again later."
        )


def _record_transition(
    application: PilotApplication,
    from_status: str | None,
    to_status: str,
    actor_user_id: int | None,
    note: str | None,
) -> None:
    db.session.add(
        PilotApplicationStatusHistory(
            pilot_application=application,
            from_status=from_status,
            to_status=to_status,
            actor_user_id=actor_user_id,
            note=note,
        )
    )


def create_pilot_application(
    submission: PilotApplicationSubmission,
    source_ip: str,
    user_agent: str | None,
) -> PilotApplication:
    if str(submission.honeypot or "").strip():
        raise PilotApplicationError("Unable to submit this pilot request.")

    source_hash = _source_ip_hash(source_ip)
    _enforce_rate_limit(source_hash)
    application = PilotApplication(
        business_name=_bounded_text(submission.business_name, "Business name", 2, 120),
        contact_name=_bounded_text(submission.contact_name, "Contact name", 2, 120),
        email=_normalized_email(submission.email),
        phone=_normalized_phone(submission.phone),
        website_url=_normalized_website_url(submission.website_url),
        use_case=_bounded_text(submission.use_case, "Use case", 30, 4000),
        expected_monthly_segments=_normalized_expected_segments(
            submission.expected_monthly_segments
        ),
        twilio_account_status=_normalized_twilio_status(submission.twilio_account_status),
        status="new",
        source_ip_hash=source_hash,
        user_agent=str(user_agent or "")[:255] or None,
    )
    db.session.add(application)
    db.session.flush()
    _record_transition(application, None, "new", None, "Pilot application submitted.")
    db.session.commit()
    return application


def _unique_organization_slug(name: str) -> str:
    base_slug = slugify_organization_name(name)
    if not base_slug:
        raise PilotApplicationError("Pilot application business name cannot produce an organization slug.")
    for suffix_number in range(1, 101):
        suffix = "" if suffix_number == 1 else f"-{suffix_number}"
        candidate = f"{base_slug[: max(1, 64 - len(suffix))]}{suffix}"
        if Organization.query.filter_by(slug=candidate).first() is None:
            return candidate
    raise PilotApplicationError("Could not allocate a unique organization slug.")


def _validate_approval_email(application: PilotApplication) -> None:
    email_user = AppUser.query.filter(func.lower(AppUser.email) == application.email).first()
    if email_user is not None and (email_user.is_platform_admin or email_user.memberships):
        raise PilotApplicationError("The applicant email is already assigned to an account.")
    username_user = AppUser.query.filter(func.lower(AppUser.username) == application.email).first()
    if username_user is not None and username_user is not email_user:
        raise PilotApplicationError("The applicant email conflicts with an existing login identifier.")


def approve_pilot_application(
    application_id: int,
    reviewer: AppUser,
    review_note: str | None,
) -> PilotApprovalResult:
    if not reviewer.is_platform_admin:
        raise PilotApplicationError("Only a platform administrator may approve pilot applications.")

    application = db.session.execute(
        select(PilotApplication)
        .where(PilotApplication.id == application_id)
        .with_for_update()
    ).scalar_one_or_none()
    if application is None:
        raise PilotApplicationError("Pilot application was not found.")
    if application.organization is not None and application.owner_invitation is not None:
        return PilotApprovalResult(
            application=application,
            organization=application.organization,
            invitation=application.owner_invitation,
        )
    if application.status == "declined":
        raise PilotApplicationError("A declined pilot application cannot be approved.")
    if application.status not in {"new", "under_review"}:
        raise PilotApplicationError("Pilot application is not in an approvable state.")

    _validate_approval_email(application)
    note = str(review_note or "").strip()[:4000] or None
    previous_status = application.status
    organization = Organization(
        name=application.business_name,
        slug=_unique_organization_slug(application.business_name),
        status="active",
        billing_offer="standard",
        billing_offer_version=str(current_app.config["BILLING_OFFER_VERSION"]),
    )
    subscription = OrganizationSubscription(
        organization=organization,
        stripe_price_id=current_app.config.get("STRIPE_MONTHLY_PRICE_ID")
        or current_app.config.get("STRIPE_PRICE_ID"),
        offer_version=str(current_app.config["BILLING_OFFER_VERSION"]),
        status="incomplete",
    )
    messaging_profile = OrganizationMessagingProfile(
        organization=organization,
        provider_mode="platform_managed",
        status="pending",
        provider_status="pending",
        sender_review_status="pending",
    )
    onboarding = OrganizationA2POnboarding(
        organization=organization,
        business_name=application.business_name,
        email=application.email,
        notification_email=application.email,
        number_strategy="auto_buy",
        onboarding_status="draft",
        business_regions_json='["USA_AND_CANADA"]',
    )
    invitation = OrganizationInvitation(
        organization=organization,
        email=application.email,
        role="owner",
        status="pending",
        invited_by_user_id=reviewer.id,
        expires_at=utc_now() + timedelta(days=7),
    )
    db.session.add_all(
        [organization, subscription, messaging_profile, onboarding, invitation]
    )
    db.session.flush()

    application.status = "approved"
    application.reviewed_by_user_id = reviewer.id
    application.reviewed_at = utc_now()
    application.review_note = note
    application.organization_id = organization.id
    application.owner_invitation_id = invitation.id
    _record_transition(
        application,
        previous_status,
        "approved",
        reviewer.id,
        note or "Pilot application approved and owner invitation issued.",
    )
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise PilotApplicationError(
            "Pilot approval conflicted with another organization, user, or invitation."
        ) from exc
    return PilotApprovalResult(
        application=application,
        organization=organization,
        invitation=invitation,
    )


def decline_pilot_application(
    application_id: int,
    reviewer: AppUser,
    review_note: str,
) -> PilotApplication:
    if not reviewer.is_platform_admin:
        raise PilotApplicationError("Only a platform administrator may decline pilot applications.")
    note = _bounded_text(review_note, "Decline reason", 3, 4000)
    application = db.session.execute(
        select(PilotApplication)
        .where(PilotApplication.id == application_id)
        .with_for_update()
    ).scalar_one_or_none()
    if application is None:
        raise PilotApplicationError("Pilot application was not found.")
    if application.status not in {"new", "under_review"}:
        raise PilotApplicationError("Pilot application is not in a declineable state.")
    previous_status = application.status
    application.status = "declined"
    application.reviewed_by_user_id = reviewer.id
    application.reviewed_at = utc_now()
    application.review_note = note
    _record_transition(application, previous_status, "declined", reviewer.id, note)
    db.session.commit()
    return application
