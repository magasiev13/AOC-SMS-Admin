from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Optional

from flask import current_app
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app import db
from app.models import (
    MessagingUsageRecord,
    Organization,
    OrganizationMessagingProfile,
    OrganizationProviderAuditLog,
    OrganizationUsageBillingPeriod,
    utc_now,
)
from app.services.provider_secret_service import (
    decrypt_provider_secret,
    encrypt_provider_secret,
)
from app.utils import normalize_phone, render_message_template


TERMINAL_MESSAGE_STATUSES = {"delivered", "sent", "undelivered", "failed"}
CUSTOMER_MANAGED_APPROVED_CAMPAIGN_STATUSES = {"approved", "active", "verified"}
CUSTOMER_MANAGED_APPROVED_BRAND_STATUSES = {"approved", "registered", "verified", "vetting_verified"}


class TwilioTransientError(Exception):
    """Transient Twilio error that should trigger a retry."""

    def __init__(self, message: str, results: Optional[dict] = None, failed_index: Optional[int] = None):
        super().__init__(message)
        self.results = results
        self.failed_index = failed_index


class ProviderProvisioningError(RuntimeError):
    """Raised when provider lifecycle actions fail."""


@dataclass(frozen=True)
class InboundSignatureValidationResult:
    """Result payload for Twilio inbound signature validation."""

    is_valid: bool
    reason: str


@dataclass(frozen=True)
class CustomerManagedValidationResult:
    """Resolved provider metadata for a validated customer-managed Twilio profile."""

    account_sid: str
    phone_number_sid: str
    from_number: str
    messaging_service_sid: str | None
    campaign_sid: str | None
    campaign_status: str | None
    brand_registration_sid: str | None
    brand_status: str | None


def _decimal_value(value: object, default: str = "0") -> Decimal:
    try:
        if value in {None, ""}:
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _absolute_decimal(value: object) -> Decimal:
    return abs(_decimal_value(value))


def _currency_rate() -> Decimal:
    rate = _decimal_value(current_app.config.get("BILLING_OUTBOUND_SEGMENT_RATE_USD"), "0.0300")
    return max(rate, Decimal("0"))


def _usage_currency() -> str:
    return (current_app.config.get("BILLING_USAGE_CURRENCY") or "usd").strip().lower() or "usd"


def _included_outbound_segments() -> int:
    try:
        return max(0, int(current_app.config.get("BILLING_INCLUDED_OUTBOUND_SEGMENTS") or 0))
    except (TypeError, ValueError):
        return 0


def _organization_has_complimentary_billing(organization_id: int | None) -> bool:
    if not organization_id:
        return False
    organization = db.session.get(Organization, organization_id)
    if organization is None or organization.subscription is None:
        return False
    return (organization.subscription.status or "").strip().lower() == "complimentary"


def _period_window(value: datetime | None) -> tuple[datetime, datetime]:
    normalized = value or utc_now()
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    else:
        normalized = normalized.astimezone(timezone.utc)
    period_start = normalized.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period_start.month == 12:
        period_end = period_start.replace(year=period_start.year + 1, month=1)
    else:
        period_end = period_start.replace(month=period_start.month + 1)
    return period_start, period_end


def previous_billing_period_window(reference_time: datetime | None = None) -> tuple[datetime, datetime]:
    now = reference_time or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    current_start, _ = _period_window(now)
    previous_end = current_start
    previous_reference = previous_end - timedelta(seconds=1)
    previous_start, _ = _period_window(previous_reference)
    return previous_start, previous_end


def _record_provider_audit(
    organization_id: int,
    action: str,
    *,
    status: str = "success",
    actor_user_id: int | None = None,
    message: str | None = None,
    metadata: dict | None = None,
) -> None:
    db.session.add(
        OrganizationProviderAuditLog(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action=action,
            status=status,
            message=message,
            metadata_json=json.dumps(metadata or {}, sort_keys=True),
        )
    )


def _master_credentials() -> tuple[str, str]:
    account_sid = (current_app.config.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (current_app.config.get("TWILIO_AUTH_TOKEN") or "").strip()
    if not account_sid or not auth_token:
        raise ValueError("Twilio platform credentials are not configured.")
    return account_sid, auth_token


def _master_rest_credentials() -> tuple[str, str, str]:
    account_sid, auth_token = _master_credentials()
    api_key_sid = (current_app.config.get("TWILIO_API_KEY_SID") or "").strip()
    api_key_secret = (current_app.config.get("TWILIO_API_KEY_SECRET") or "").strip()
    if api_key_sid and api_key_secret:
        return account_sid, api_key_sid, api_key_secret
    return account_sid, account_sid, auth_token


def _master_client() -> Client:
    account_sid, username, password = _master_rest_credentials()
    return Client(username, password, account_sid)


def _twilio_inbound_webhook_url() -> str:
    base_url = (current_app.config.get("SAAS_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        raise ProviderProvisioningError("SAAS_BASE_URL must be configured to bind Twilio inbound webhooks.")
    return f"{base_url}/webhooks/twilio/inbound"


def _messaging_profile_for_org(organization_id: int | None) -> OrganizationMessagingProfile | None:
    if not organization_id:
        return None
    return OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).first()


def _provider_ready(profile: OrganizationMessagingProfile | None) -> bool:
    return profile is not None and profile.can_send


def _build_subaccount_client(profile: OrganizationMessagingProfile) -> Client:
    if not profile.twilio_subaccount_sid:
        raise ValueError("Twilio subaccount is not provisioned for this organization.")

    encrypted_token = (profile.twilio_auth_token_encrypted or "").strip()
    if encrypted_token:
        auth_token = decrypt_provider_secret(encrypted_token)
        if not auth_token:
            raise ValueError("Stored subaccount auth token is empty.")
        return Client(profile.twilio_subaccount_sid, auth_token)

    master_account_sid, username, password = _master_rest_credentials()
    return Client(username, password, profile.twilio_subaccount_sid)


def _build_customer_managed_client(
    *,
    account_sid: str,
    auth_token: str,
) -> Client:
    normalized_account_sid = (account_sid or "").strip().upper()
    normalized_auth_token = (auth_token or "").strip()
    if not normalized_account_sid or not normalized_account_sid.startswith("AC"):
        raise ValueError("Customer-managed Twilio account SID must start with AC.")
    if not normalized_auth_token:
        raise ValueError("Customer-managed Twilio auth token is required.")
    return Client(normalized_account_sid, normalized_auth_token)


def _client_for_profile(profile: OrganizationMessagingProfile) -> Client:
    if profile.provider_mode == "platform_managed":
        return _build_subaccount_client(profile)

    if profile.provider_mode == "customer_managed":
        auth_token = decrypt_provider_secret(profile.twilio_auth_token_encrypted)
        if not auth_token:
            raise ValueError("Stored customer-managed auth token is empty.")
        return _build_customer_managed_client(
            account_sid=profile.twilio_account_sid or "",
            auth_token=auth_token,
        )

    raise ValueError(f"Unsupported provider mode {profile.provider_mode!r}.")


def _client_for_usage_reconciliation(organization_id: int | None) -> Client:
    profile = _messaging_profile_for_org(organization_id)
    if profile is not None and profile.provider_status != "error":
        if profile.provider_mode == "platform_managed" and profile.twilio_subaccount_sid:
            return _build_subaccount_client(profile)
        if profile.provider_mode == "customer_managed" and profile.twilio_account_sid:
            return _client_for_profile(profile)
    return _master_client()


def _service_context(profile: OrganizationMessagingProfile, client: Client | None = None):
    if not profile.messaging_service_sid:
        raise ProviderProvisioningError("Messaging service is not provisioned for this organization.")
    provider_client = client or _client_for_profile(profile)
    return provider_client.messaging.v1.services(profile.messaging_service_sid)


def _configure_service_webhooks(profile: OrganizationMessagingProfile, *, client: Client | None = None) -> None:
    service = _service_context(profile, client=client)
    service.update(
        inbound_request_url=_twilio_inbound_webhook_url(),
        inbound_method="POST",
        use_inbound_webhook_on_number=False,
    )


def _configure_phone_number_webhook(
    client: Client,
    phone_number_sid: str,
) -> None:
    if not phone_number_sid:
        raise ProviderProvisioningError("A phone number SID is required to bind the inbound webhook.")
    client.incoming_phone_numbers(phone_number_sid).update(
        sms_url=_twilio_inbound_webhook_url(),
        sms_method="POST",
    )


def _normalized_twilio_status(value: object) -> str | None:
    normalized = (str(value or "")).strip().lower()
    return normalized or None


def _resolve_customer_managed_phone_number(
    client: Client,
    from_number: str,
) -> SimpleNamespace:
    matches = client.incoming_phone_numbers.list(phone_number=from_number, limit=20)
    for match in matches:
        if normalize_phone(getattr(match, "phone_number", None)) == from_number:
            return SimpleNamespace(
                sid=getattr(match, "sid", None),
                phone_number=normalize_phone(getattr(match, "phone_number", None)),
            )
    raise ProviderProvisioningError(
        f"Twilio account does not contain the sender number {from_number}."
    )


def _resolve_customer_managed_campaign(service_context) -> tuple[str | None, str | None, str | None]:
    campaigns = service_context.us_app_to_person.list(limit=20)
    if not campaigns:
        return None, None, None

    preferred = None
    for campaign in campaigns:
        status = _normalized_twilio_status(getattr(campaign, "status", None))
        if status in CUSTOMER_MANAGED_APPROVED_CAMPAIGN_STATUSES:
            preferred = campaign
            break
    campaign = preferred or campaigns[0]
    return (
        getattr(campaign, "sid", None),
        _normalized_twilio_status(getattr(campaign, "status", None)),
        getattr(campaign, "brand_registration_sid", None),
    )


def _fetch_customer_managed_brand_status(
    client: Client,
    brand_registration_sid: str | None,
) -> str | None:
    if not brand_registration_sid:
        return None
    try:
        brand = client.messaging.v1.brand_registrations(brand_registration_sid).fetch()
    except TwilioRestException:
        return None
    return _normalized_twilio_status(getattr(brand, "status", None))


def _sender_sync_error_message(exc: Exception, profile: OrganizationMessagingProfile) -> str:
    if isinstance(exc, TwilioRestException):
        detail = (getattr(exc, "msg", "") or str(exc)).strip()
        status = getattr(exc, "status", None)
        if status == 404:
            return (
                f"Twilio could not attach phone number SID {profile.phone_number_sid} to messaging "
                f"service {profile.messaging_service_sid} under subaccount {profile.twilio_subaccount_sid}. "
                "Make sure the PN SID already belongs to this organization's Twilio subaccount. "
                "A phone number from the platform master account or another subaccount will not attach here."
            )
        return (
            f"Twilio error {status or 'unknown'} while attaching phone number SID "
            f"{profile.phone_number_sid} to messaging service {profile.messaging_service_sid}: {detail}"
        )
    return str(exc)


def _sync_service_sender(profile: OrganizationMessagingProfile, *, actor_user_id: int | None = None) -> None:
    if profile.provider_mode != "platform_managed":
        raise ProviderProvisioningError("Sender sync is only available for platform-managed providers.")
    if not profile.from_number or not profile.phone_number_sid:
        raise ProviderProvisioningError("Both sender number and phone number SID are required.")

    provider_client = _build_subaccount_client(profile)
    service = _service_context(profile, client=provider_client)
    attached = False
    detached_count = 0

    for sender in service.phone_numbers.list():
        if sender.phone_number == profile.from_number:
            attached = True
            continue
        sender.delete()
        detached_count += 1

    if not attached:
        service.phone_numbers.create(phone_number_sid=profile.phone_number_sid)

    _configure_service_webhooks(profile, client=provider_client)
    profile.inbound_identity = profile.from_number
    profile.provider_last_checked_at = utc_now()
    profile.last_provision_error = None
    _record_provider_audit(
        profile.organization_id,
        "sender_sync",
        actor_user_id=actor_user_id,
        message="Attached sender to the organization messaging service and configured inbound webhook.",
        metadata={
            "from_number": profile.from_number,
            "phone_number_sid": profile.phone_number_sid,
            "messaging_service_sid": profile.messaging_service_sid,
            "detached_count": detached_count,
        },
    )


def _detach_service_senders(profile: OrganizationMessagingProfile, *, actor_user_id: int | None = None) -> None:
    if profile.provider_mode != "platform_managed":
        raise ProviderProvisioningError("Sender detach is only available for platform-managed providers.")
    if not profile.twilio_subaccount_sid or not profile.messaging_service_sid:
        return

    provider_client = _build_subaccount_client(profile)
    service = _service_context(profile, client=provider_client)
    detached_count = 0
    for sender in service.phone_numbers.list():
        sender.delete()
        detached_count += 1

    _configure_service_webhooks(profile, client=provider_client)
    profile.provider_last_checked_at = utc_now()
    profile.last_provision_error = None
    _record_provider_audit(
        profile.organization_id,
        "sender_detach",
        actor_user_id=actor_user_id,
        message="Detached sender resources from the organization messaging service.",
        metadata={
            "messaging_service_sid": profile.messaging_service_sid,
            "detached_count": detached_count,
        },
    )


def _message_status_is_terminal(status: str | None) -> bool:
    return (status or "").strip().lower() in TERMINAL_MESSAGE_STATUSES


class TwilioService:
    def __init__(self, organization_id: int | None = None):
        self.organization_id = organization_id
        self.profile = _messaging_profile_for_org(organization_id)
        self.account_sid, self.auth_token = _master_credentials()
        self.from_number = current_app.config.get("TWILIO_FROM_NUMBER")
        self.messaging_service_sid = None
        self.phone_number_sid = None

        if self.profile is not None:
            if self.profile.provider_status == "suspended":
                raise ValueError("Messaging provider is suspended for this organization.")
            if self.profile.provider_status in {"provisioning", "error"}:
                raise ValueError("Messaging provider is not ready for this organization.")
            if self.profile.provider_mode == "platform_managed" and self.profile.twilio_subaccount_sid:
                self.client = _build_subaccount_client(self.profile)
                self.account_sid = self.profile.twilio_subaccount_sid
            elif self.profile.provider_mode == "customer_managed" and self.profile.twilio_account_sid:
                self.client = _client_for_profile(self.profile)
                self.account_sid = self.profile.twilio_account_sid
                self.auth_token = decrypt_provider_secret(self.profile.twilio_auth_token_encrypted) or self.auth_token
            else:
                self.client = Client(self.account_sid, self.auth_token)
            self.from_number = self.profile.from_number or self.from_number
            self.messaging_service_sid = self.profile.messaging_service_sid
            self.phone_number_sid = self.profile.phone_number_sid
        else:
            self.client = Client(self.account_sid, self.auth_token)

        if organization_id and not _provider_ready(self.profile):
            raise ValueError("Messaging provider is not active for this organization.")

        if not (self.from_number or self.messaging_service_sid):
            raise ValueError("Twilio sender is not configured.")

    def _is_transient_error(self, error: TwilioRestException) -> bool:
        status = getattr(error, "status", None)
        return status in {429} or (isinstance(status, int) and status >= 500)

    def send_message(self, to_number: str, body: str, raise_on_transient: bool = False) -> dict:
        create_params = {
            "body": body,
            "to": to_number,
        }
        if self.messaging_service_sid:
            create_params["messaging_service_sid"] = self.messaging_service_sid
        else:
            create_params["from_"] = self.from_number
        try:
            message = self.client.messages.create(**create_params)
            return {
                "success": True,
                "sid": message.sid,
                "status": message.status,
                "error": None,
                "account_sid": getattr(message, "account_sid", None),
                "num_segments": getattr(message, "num_segments", None),
                "provider_price": getattr(message, "price", None),
                "provider_currency": getattr(message, "price_unit", None),
            }
        except TwilioRestException as exc:
            if raise_on_transient and self._is_transient_error(exc):
                raise TwilioTransientError(str(exc)) from exc
            return {
                "success": False,
                "sid": None,
                "status": "failed",
                "error": str(exc.msg) if hasattr(exc, "msg") else str(exc),
                "account_sid": None,
            }
        except Exception as exc:
            if raise_on_transient:
                raise
            return {
                "success": False,
                "sid": None,
                "status": "failed",
                "error": str(exc),
                "account_sid": None,
            }

    def send_bulk(
        self,
        recipients: list,
        body: str,
        delay: float = 0.1,
        raise_on_transient: bool = False,
    ) -> dict:
        results = {
            "total": len(recipients),
            "success_count": 0,
            "failure_count": 0,
            "details": [],
        }

        for index, recipient in enumerate(recipients):
            phone = recipient.get("phone")
            name = recipient.get("name", "")
            personalized_body = render_message_template(body, recipient)

            try:
                result = self.send_message(phone, personalized_body, raise_on_transient=raise_on_transient)
            except TwilioTransientError as exc:
                raise TwilioTransientError(
                    str(exc),
                    results=results,
                    failed_index=index,
                ) from exc

            detail = {
                "phone": phone,
                "name": name,
                "success": result["success"],
                "error": result.get("error"),
                "sid": result.get("sid"),
                "status": result.get("status"),
                "account_sid": result.get("account_sid"),
            }
            results["details"].append(detail)

            if result["success"]:
                results["success_count"] += 1
            else:
                results["failure_count"] += 1

            if delay > 0:
                time.sleep(delay)

        return results


def get_messaging_provider(organization_id: int | None = None) -> TwilioService:
    return TwilioService(organization_id=organization_id)


def get_twilio_service(organization_id: int | None = None) -> TwilioService:
    return get_messaging_provider(organization_id=organization_id)


def ensure_messaging_profile(organization: Organization) -> OrganizationMessagingProfile:
    profile = organization.messaging_profile
    if profile is not None:
        return profile
    profile = OrganizationMessagingProfile(
        organization=organization,
        provider_mode="platform_managed",
        status="pending",
        provider_status="pending",
    )
    db.session.add(profile)
    db.session.flush()
    return profile


def save_customer_managed_profile(
    organization_id: int,
    *,
    twilio_account_sid: str,
    twilio_auth_token: str,
    from_number: str,
    messaging_service_sid: str | None = None,
    business_type: str | None = None,
    use_case: str | None = None,
    actor_user_id: int | None = None,
) -> tuple[OrganizationMessagingProfile, CustomerManagedValidationResult]:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")

    profile = ensure_messaging_profile(organization)
    normalized_account_sid = (twilio_account_sid or "").strip().upper()
    normalized_auth_token = (twilio_auth_token or "").strip()
    normalized_from_number = normalize_phone(from_number)
    normalized_service_sid = (messaging_service_sid or "").strip().upper() or None

    if not normalized_account_sid.startswith("AC"):
        raise ProviderProvisioningError("Customer-managed Twilio account SID must start with AC.")
    if not normalized_auth_token:
        raise ProviderProvisioningError("Customer-managed Twilio auth token is required.")
    if not normalized_from_number:
        raise ProviderProvisioningError("A valid E.164 sender number is required.")
    if normalized_service_sid and not normalized_service_sid.startswith("MG"):
        raise ProviderProvisioningError("Twilio Messaging Service SID must start with MG.")

    profile.set_provider_status("provisioning")
    profile.last_provision_error = None
    profile.provider_last_checked_at = utc_now()
    db.session.commit()

    try:
        customer_client = _build_customer_managed_client(
            account_sid=normalized_account_sid,
            auth_token=normalized_auth_token,
        )
        customer_client.api.v2010.accounts(normalized_account_sid).fetch()

        resolved_number = _resolve_customer_managed_phone_number(
            customer_client,
            normalized_from_number,
        )
        if not resolved_number.sid:
            raise ProviderProvisioningError(
                f"Twilio could not resolve a phone number SID for {normalized_from_number}."
            )

        campaign_sid = None
        campaign_status = None
        brand_registration_sid = None
        brand_status = None
        if normalized_service_sid:
            service_context = customer_client.messaging.v1.services(normalized_service_sid)
            service_context.fetch()
            campaign_sid, campaign_status, brand_registration_sid = _resolve_customer_managed_campaign(
                service_context
            )
            if not campaign_sid:
                raise ProviderProvisioningError(
                    "The customer-managed Messaging Service does not have an attached A2P campaign."
                )
            if campaign_status not in CUSTOMER_MANAGED_APPROVED_CAMPAIGN_STATUSES:
                raise ProviderProvisioningError(
                    "The customer-managed Messaging Service campaign is not approved for live sending yet."
                )
            brand_status = _fetch_customer_managed_brand_status(customer_client, brand_registration_sid)
            if brand_status is not None and brand_status not in CUSTOMER_MANAGED_APPROVED_BRAND_STATUSES:
                raise ProviderProvisioningError(
                    "The customer-managed Twilio brand is not approved for live sending yet."
                )
            service_context.update(use_inbound_webhook_on_number=True)

        _configure_phone_number_webhook(customer_client, resolved_number.sid)

        profile.provider_mode = "customer_managed"
        profile.twilio_account_sid = normalized_account_sid
        profile.twilio_subaccount_sid = None
        profile.twilio_auth_token_encrypted = encrypt_provider_secret(normalized_auth_token)
        profile.messaging_service_sid = normalized_service_sid
        profile.from_number = resolved_number.phone_number or normalized_from_number
        profile.phone_number_sid = resolved_number.sid
        profile.inbound_identity = resolved_number.phone_number or normalized_from_number
        profile.business_type = business_type
        profile.use_case = use_case
        profile.sender_review_status = "approved"
        profile.consent_acknowledged_at = utc_now()
        profile.provisioned_at = profile.provisioned_at or utc_now()
        profile.provider_last_checked_at = utc_now()
        profile.last_provision_error = None
        profile.set_provider_status("active")
        _record_provider_audit(
            organization.id,
            "customer_managed_validate",
            actor_user_id=actor_user_id,
            message="Validated customer-managed Twilio configuration and bound inbound webhook.",
            metadata={
                "twilio_account_sid": normalized_account_sid,
                "messaging_service_sid": normalized_service_sid,
                "from_number": profile.from_number,
                "phone_number_sid": profile.phone_number_sid,
                "campaign_sid": campaign_sid,
                "campaign_status": campaign_status,
                "brand_registration_sid": brand_registration_sid,
                "brand_status": brand_status,
            },
        )
        db.session.commit()
        return profile, CustomerManagedValidationResult(
            account_sid=normalized_account_sid,
            phone_number_sid=profile.phone_number_sid,
            from_number=profile.from_number,
            messaging_service_sid=normalized_service_sid,
            campaign_sid=campaign_sid,
            campaign_status=campaign_status,
            brand_registration_sid=brand_registration_sid,
            brand_status=brand_status,
        )
    except Exception as exc:
        db.session.rollback()
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            raise
        profile = ensure_messaging_profile(organization)
        profile.provider_mode = "customer_managed"
        profile.twilio_account_sid = normalized_account_sid or profile.twilio_account_sid
        profile.provider_last_checked_at = utc_now()
        profile.last_provision_error = str(exc)
        profile.set_provider_status("error")
        _record_provider_audit(
            organization.id,
            "customer_managed_validate",
            actor_user_id=actor_user_id,
            status="error",
            message=str(exc),
            metadata={
                "twilio_account_sid": normalized_account_sid,
                "messaging_service_sid": normalized_service_sid,
                "from_number": normalized_from_number,
            },
        )
        db.session.commit()
        raise ProviderProvisioningError(str(exc)) from exc


def provision_org(organization_id: int, *, actor_user_id: int | None = None) -> OrganizationMessagingProfile:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")

    profile = ensure_messaging_profile(organization)
    if profile.provider_mode != "platform_managed":
        raise ProviderProvisioningError("Only platform-managed providers can be provisioned automatically.")

    profile.set_provider_status("provisioning")
    profile.provisioning_started_at = utc_now()
    profile.last_provision_error = None
    _record_provider_audit(
        organization.id,
        "provision_start",
        actor_user_id=actor_user_id,
        message="Started Twilio provider provisioning.",
    )
    db.session.commit()

    try:
        master_client = _master_client()
        if not profile.twilio_subaccount_sid:
            subaccount = master_client.api.v2010.accounts.create(
                friendly_name=f"{current_app.config.get('TWILIO_PLATFORM_FRIENDLY_NAME')} - {organization.name}"
            )
            profile.twilio_subaccount_sid = subaccount.sid
            auth_token = getattr(subaccount, "auth_token", None)
            if auth_token:
                profile.twilio_auth_token_encrypted = encrypt_provider_secret(auth_token)
            db.session.commit()

        subaccount_client = _build_subaccount_client(profile)
        if not profile.messaging_service_sid:
            service = subaccount_client.messaging.v1.services.create(
                friendly_name=organization.name[:64]
            )
            profile.messaging_service_sid = service.sid
            db.session.commit()
        _configure_service_webhooks(profile, client=subaccount_client)

        if not profile.inbound_identity:
            profile.inbound_identity = profile.from_number or profile.messaging_service_sid

        profile.provisioned_at = utc_now()
        profile.provider_last_checked_at = utc_now()
        if profile.from_number and profile.sender_review_status == "approved":
            profile.set_provider_status("active")
        else:
            profile.set_provider_status("pending")

        _record_provider_audit(
            organization.id,
            "provision_complete",
            actor_user_id=actor_user_id,
            message="Twilio subaccount and messaging service are ready.",
            metadata={
                "twilio_subaccount_sid": profile.twilio_subaccount_sid,
                "messaging_service_sid": profile.messaging_service_sid,
            },
        )
        db.session.commit()
        return profile
    except Exception as exc:
        db.session.rollback()
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            raise
        profile = ensure_messaging_profile(organization)
        profile.set_provider_status("error")
        profile.last_provision_error = str(exc)
        profile.provider_last_checked_at = utc_now()
        _record_provider_audit(
            organization.id,
            "provision_failed",
            actor_user_id=actor_user_id,
            status="error",
            message=str(exc),
        )
        db.session.commit()
        raise ProviderProvisioningError(str(exc)) from exc


def suspend_org(organization_id: int, *, actor_user_id: int | None = None) -> OrganizationMessagingProfile:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    profile = ensure_messaging_profile(organization)
    if profile.twilio_subaccount_sid:
        _master_client().api.v2010.accounts(profile.twilio_subaccount_sid).update(status="suspended")
    profile.set_provider_status("suspended")
    profile.suspended_at = utc_now()
    profile.provider_last_checked_at = utc_now()
    _record_provider_audit(
        organization.id,
        "suspend",
        actor_user_id=actor_user_id,
        message="Twilio provider suspended.",
    )
    db.session.commit()
    return profile


def resume_org(organization_id: int, *, actor_user_id: int | None = None) -> OrganizationMessagingProfile:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    profile = ensure_messaging_profile(organization)
    if profile.twilio_subaccount_sid:
        _master_client().api.v2010.accounts(profile.twilio_subaccount_sid).update(status="active")
    profile.suspended_at = None
    profile.provider_last_checked_at = utc_now()
    if profile.from_number and profile.sender_review_status == "approved":
        profile.set_provider_status("active")
    else:
        profile.set_provider_status("pending")
    _record_provider_audit(
        organization.id,
        "resume",
        actor_user_id=actor_user_id,
        message="Twilio provider resumed.",
    )
    db.session.commit()
    return profile


def release_sender(organization_id: int, *, actor_user_id: int | None = None) -> OrganizationMessagingProfile:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    profile = ensure_messaging_profile(organization)
    try:
        _detach_service_senders(profile, actor_user_id=actor_user_id)
        profile.from_number = None
        profile.phone_number_sid = None
        profile.inbound_identity = profile.messaging_service_sid
        if profile.provider_status != "suspended":
            profile.set_provider_status("pending")
        _record_provider_audit(
            organization.id,
            "release_sender",
            actor_user_id=actor_user_id,
            message="Released sender assignment from organization.",
        )
        db.session.commit()
        return profile
    except Exception as exc:
        db.session.rollback()
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            raise
        profile = ensure_messaging_profile(organization)
        profile.set_provider_status("error")
        profile.last_provision_error = str(exc)
        profile.provider_last_checked_at = utc_now()
        _record_provider_audit(
            organization.id,
            "release_sender_failed",
            actor_user_id=actor_user_id,
            status="error",
            message=str(exc),
        )
        db.session.commit()
        raise ProviderProvisioningError(str(exc)) from exc


def sync_sender_assignment(organization_id: int, *, actor_user_id: int | None = None) -> OrganizationMessagingProfile:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    profile = ensure_messaging_profile(organization)
    if not profile.messaging_service_sid:
        raise ProviderProvisioningError("Provision the Twilio provider before assigning a sender.")
    if not profile.from_number or not profile.phone_number_sid:
        raise ProviderProvisioningError("Both sender number and phone number SID are required.")

    try:
        _sync_service_sender(profile, actor_user_id=actor_user_id)
        if profile.provider_status != "suspended":
            if profile.sender_review_status == "approved" and profile.consent_acknowledged_at is not None:
                profile.set_provider_status("active")
            else:
                profile.set_provider_status("pending")
        db.session.commit()
        return profile
    except Exception as exc:
        db.session.rollback()
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            raise
        profile = ensure_messaging_profile(organization)
        message = _sender_sync_error_message(exc, profile)
        profile.set_provider_status("error")
        profile.last_provision_error = message
        profile.provider_last_checked_at = utc_now()
        _record_provider_audit(
            organization.id,
            "sender_sync_failed",
            actor_user_id=actor_user_id,
            status="error",
            message=message,
            metadata={
                "from_number": profile.from_number,
                "phone_number_sid": profile.phone_number_sid,
                "messaging_service_sid": profile.messaging_service_sid,
            },
        )
        db.session.commit()
        raise ProviderProvisioningError(message) from exc


def resolve_messaging_profile(payload: dict) -> OrganizationMessagingProfile | None:
    to_number = normalize_phone((payload.get("To") or "").strip())
    messaging_service_sid = (payload.get("MessagingServiceSid") or "").strip()
    inbound_identity = (payload.get("To") or "").strip()

    if messaging_service_sid:
        profile = OrganizationMessagingProfile.query.filter_by(messaging_service_sid=messaging_service_sid).first()
        if profile is not None:
            return profile

    if to_number:
        profile = OrganizationMessagingProfile.query.filter(
            db.or_(
                OrganizationMessagingProfile.from_number == to_number,
                OrganizationMessagingProfile.inbound_identity == to_number,
            )
        ).first()
        if profile is not None:
            return profile

    if inbound_identity:
        return OrganizationMessagingProfile.query.filter_by(inbound_identity=inbound_identity).first()

    return None


def validate_inbound_signature_detailed(
    url: str,
    params: dict,
    signature: Optional[str],
    *,
    messaging_profile: OrganizationMessagingProfile | None = None,
) -> InboundSignatureValidationResult:
    auth_token = None
    if messaging_profile is not None:
        auth_token = decrypt_provider_secret(messaging_profile.twilio_auth_token_encrypted)
    if not auth_token:
        auth_token = current_app.config.get("TWILIO_AUTH_TOKEN")
    if not auth_token:
        return InboundSignatureValidationResult(
            is_valid=False,
            reason="missing_auth_token",
        )
    if not signature:
        return InboundSignatureValidationResult(
            is_valid=False,
            reason="missing_signature",
        )

    try:
        validator = RequestValidator(auth_token)
        if validator.validate(url, params, signature):
            return InboundSignatureValidationResult(is_valid=True, reason="valid")
        return InboundSignatureValidationResult(
            is_valid=False,
            reason="invalid_signature",
        )
    except Exception:
        current_app.logger.exception("Failed to validate Twilio inbound signature.")
        return InboundSignatureValidationResult(
            is_valid=False,
            reason="validator_exception",
        )


def validate_inbound_signature(
    url: str,
    params: dict,
    signature: Optional[str],
    *,
    messaging_profile: OrganizationMessagingProfile | None = None,
) -> bool:
    return validate_inbound_signature_detailed(
        url,
        params,
        signature,
        messaging_profile=messaging_profile,
    ).is_valid


def record_usage_candidates(
    organization_id: int | None,
    details: list[dict] | None,
    *,
    source: str = "blast",
) -> int:
    if not organization_id or not details:
        return 0

    created_count = 0
    for detail in details:
        if not isinstance(detail, dict):
            continue
        sid = (detail.get("sid") or "").strip()
        if not sid:
            continue
        record = MessagingUsageRecord.query.filter_by(message_sid=sid).first()
        if record is None:
            record = MessagingUsageRecord(
                organization_id=organization_id,
                message_sid=sid,
                direction="outbound",
                source=source,
                twilio_subaccount_sid=(detail.get("account_sid") or "").strip() or None,
                twilio_message_status=(detail.get("status") or "").strip() or None,
                provider_currency=_usage_currency(),
                reconciliation_status="pending",
            )
            db.session.add(record)
            created_count += 1
        else:
            record.twilio_subaccount_sid = record.twilio_subaccount_sid or ((detail.get("account_sid") or "").strip() or None)
            record.twilio_message_status = (detail.get("status") or "").strip() or record.twilio_message_status
    db.session.commit()
    return created_count


def reconcile_messaging_usage() -> dict[str, int]:
    summary = {
        "records_seen": 0,
        "records_finalized": 0,
        "records_pending": 0,
        "records_errored": 0,
        "periods_updated": 0,
    }
    pending_records = MessagingUsageRecord.query.filter(
        MessagingUsageRecord.reconciliation_status.in_(("pending", "error"))
    ).all()

    for record in pending_records:
        summary["records_seen"] += 1
        try:
            reconciliation_client = _client_for_usage_reconciliation(record.organization_id)
            message = reconciliation_client.messages(record.message_sid).fetch()
            status = (getattr(message, "status", None) or "").strip().lower() or None
            segments = getattr(message, "num_segments", None)
            price = getattr(message, "price", None)
            currency = (getattr(message, "price_unit", None) or _usage_currency()).strip().lower() or _usage_currency()
            date_created = getattr(message, "date_created", None) or record.created_at

            provider_cost = _absolute_decimal(price)
            billable_units = 0
            if segments not in {None, ""}:
                try:
                    billable_units = max(0, int(segments))
                except (TypeError, ValueError):
                    billable_units = 0
            if billable_units == 0 and provider_cost > 0:
                billable_units = 1
            billable = provider_cost > 0 or (status in {"sent", "delivered"} and billable_units > 0)
            complimentary_billing = _organization_has_complimentary_billing(record.organization_id)

            sell_rate = _currency_rate()
            sell_amount = Decimal("0")
            if complimentary_billing:
                sell_rate = Decimal("0")
            elif billable and billable_units > 0:
                sell_amount = Decimal(billable_units) * sell_rate

            period_start, period_end = _period_window(date_created)
            record.twilio_subaccount_sid = getattr(message, "account_sid", None) or record.twilio_subaccount_sid
            record.twilio_message_status = status
            record.provider_currency = currency
            record.provider_cost = provider_cost
            record.sell_rate = sell_rate
            record.billable_units = billable_units
            record.billable = billable
            record.sell_amount = sell_amount
            record.margin = sell_amount - provider_cost
            record.billing_period_start = period_start
            record.billing_period_end = period_end
            record.last_error = None
            record.reconciled_at = utc_now()
            if _message_status_is_terminal(status):
                record.reconciliation_status = "finalized"
                summary["records_finalized"] += 1
            else:
                record.reconciliation_status = "pending"
                summary["records_pending"] += 1
        except Exception as exc:
            record.reconciliation_status = "error"
            record.last_error = str(exc)
            record.reconciled_at = utc_now()
            summary["records_errored"] += 1

    db.session.commit()
    summary["periods_updated"] = upsert_closed_usage_billing_periods()
    return summary


def upsert_closed_usage_billing_periods() -> int:
    period_start, period_end = previous_billing_period_window()
    organization_ids = [
        row.organization_id
        for row in db.session.query(MessagingUsageRecord.organization_id)
        .filter(MessagingUsageRecord.billing_period_start == period_start)
        .filter(MessagingUsageRecord.billing_period_end == period_end)
        .filter(MessagingUsageRecord.reconciliation_status == "finalized")
        .group_by(MessagingUsageRecord.organization_id)
        .all()
    ]
    updated = 0
    for organization_id in organization_ids:
        used_units = (
            db.session.query(db.func.coalesce(db.func.sum(MessagingUsageRecord.billable_units), 0))
            .filter(MessagingUsageRecord.organization_id == organization_id)
            .filter(MessagingUsageRecord.billing_period_start == period_start)
            .filter(MessagingUsageRecord.billing_period_end == period_end)
            .filter(MessagingUsageRecord.reconciliation_status == "finalized")
            .filter(MessagingUsageRecord.billable.is_(True))
            .scalar()
        ) or 0
        included_units = _included_outbound_segments()
        complimentary_billing = _organization_has_complimentary_billing(organization_id)
        overage_units = max(0, int(used_units) - included_units)
        sell_amount = Decimal(overage_units) * _currency_rate()
        if complimentary_billing:
            overage_units = 0
            sell_amount = Decimal("0")
        period = OrganizationUsageBillingPeriod.query.filter_by(
            organization_id=organization_id,
            period_start=period_start,
            period_end=period_end,
        ).first()
        if period is None:
            period = OrganizationUsageBillingPeriod(
                organization_id=organization_id,
                period_start=period_start,
                period_end=period_end,
            )
            db.session.add(period)
        period.included_units = included_units
        period.used_units = int(used_units)
        period.overage_units = overage_units
        period.sell_amount = sell_amount
        period.currency = _usage_currency()
        if overage_units == 0:
            period.status = "included"
        elif not period.stripe_invoice_item_id:
            period.status = "pending"
        updated += 1
    db.session.commit()
    return updated
