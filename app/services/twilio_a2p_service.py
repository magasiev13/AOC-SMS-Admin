from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from flask import current_app
from twilio.base.exceptions import TwilioRestException

from app import db
from app.models import (
    Organization,
    OrganizationA2POnboarding,
    OrganizationMessagingProfile,
    utc_now,
)
from app.queue import get_queue
from app.services.provider_secret_service import decrypt_provider_secret, encrypt_provider_secret
from app.services.twilio_service import (
    ProviderProvisioningError,
    _build_subaccount_client,
    _configure_service_webhooks,
    _master_client,
    _record_provider_audit,
    _twilio_inbound_webhook_url,
    ensure_messaging_profile,
    provision_org,
    sync_sender_assignment,
)


STANDARD_CUSTOMER_PROFILE_POLICY_SID = "RNdfbf3fae0e1107f8aded0e7cead80bf5"
STANDARD_TRUST_PRODUCT_POLICY_SID = "RNb0d4771c2c98518d916a3d4cd70a8f8b"
SOLE_PROPRIETOR_CUSTOMER_PROFILE_POLICY_SID = "RN806dd6cd175f314e1f96a9727ee271f4"
SOLE_PROPRIETOR_TRUST_PRODUCT_POLICY_SID = "RN670d5d2e282a6130ae063b234b6019c8"

A2P_REGISTRATION_PATHS = (
    ("standard", "Standard"),
    ("low_volume_standard", "Low-Volume Standard"),
    ("nonprofit", "Nonprofit"),
    ("government", "Government"),
    ("sole_proprietor", "Sole Proprietor"),
)

A2P_NUMBER_STRATEGIES = (
    ("auto_buy", "Buy a new number automatically"),
    ("existing_subaccount_number", "Use an existing subaccount number"),
    ("transfer_parent_number", "Transfer an existing parent-account number"),
)

A2P_CAMPAIGN_USE_CASES = (
    ("MIXED", "Mixed"),
    ("ACCOUNT_NOTIFICATION", "Account Notification"),
    ("CUSTOMER_CARE", "Customer Care"),
    ("MARKETING", "Marketing"),
    ("SOLE_PROPRIETOR", "Sole Proprietor"),
)

DEFAULT_OPT_IN_KEYWORDS = ["START", "SUBSCRIBE", "YES"]
DEFAULT_OPT_OUT_KEYWORDS = ["STOP", "UNSUBSCRIBE", "END"]
DEFAULT_HELP_KEYWORDS = ["HELP", "INFO"]
A2P_REGISTRATION_PATH_VALUES = {value for value, _ in A2P_REGISTRATION_PATHS}
A2P_NUMBER_STRATEGY_VALUES = {value for value, _ in A2P_NUMBER_STRATEGIES}


@dataclass(frozen=True)
class QueuedA2PJobStub:
    job_name: str
    organization_id: int
    actor_user_id: int | None


@dataclass(frozen=True)
class A2PFormData:
    registration_path: str
    number_strategy: str
    business_name: str
    business_type: str | None
    website_url: str | None
    social_profile_url: str | None
    email: str
    phone_number: str | None
    mobile_number: str | None
    first_name: str
    last_name: str
    job_position: str | None
    business_registration_identifier: str | None
    business_registration_number: str | None
    address_sid: str | None
    supporting_document_sid: str | None
    campaign_use_case: str
    campaign_description: str
    message_flow: str
    message_samples: list[str]
    opt_in_message: str | None
    opt_out_message: str | None
    help_message: str | None
    opt_in_keywords: list[str]
    opt_out_keywords: list[str]
    help_keywords: list[str]
    has_embedded_links: bool
    has_embedded_phone: bool
    desired_phone_number: str | None
    desired_phone_number_sid: str | None
    campaign_verify_token: str | None


def a2p_onboarding_enabled() -> bool:
    return bool(current_app.config.get("TWILIO_A2P_ONBOARDING_ENABLED"))


def ensure_a2p_onboarding(organization: Organization) -> OrganizationA2POnboarding:
    onboarding = organization.a2p_onboarding
    if onboarding is not None:
        return onboarding

    onboarding = OrganizationA2POnboarding(
        organization=organization,
        business_name=organization.name,
    )
    db.session.add(onboarding)
    db.session.flush()
    return onboarding


def a2p_registration_path_choices() -> tuple[tuple[str, str], ...]:
    return A2P_REGISTRATION_PATHS


def a2p_number_strategy_choices() -> tuple[tuple[str, str], ...]:
    return A2P_NUMBER_STRATEGIES


def a2p_campaign_use_case_choices() -> tuple[tuple[str, str], ...]:
    return A2P_CAMPAIGN_USE_CASES


def _normalized_csv_list(raw_value: str | None, *, default: list[str] | None = None) -> list[str]:
    value = (raw_value or "").strip()
    if not value:
        return list(default or [])
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def _normalized_multiline_list(raw_value: str | None) -> list[str]:
    value = (raw_value or "").strip()
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _coerce_bool(raw_value: Any) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_use_case(registration_path: str, raw_value: str | None) -> str:
    candidate = (raw_value or "").strip().upper() or "MIXED"
    allowed = {value for value, _ in A2P_CAMPAIGN_USE_CASES}
    if registration_path == "sole_proprietor":
        return "SOLE_PROPRIETOR"
    if candidate not in allowed:
        return "MIXED"
    if candidate == "SOLE_PROPRIETOR":
        return "MIXED"
    return candidate


def _business_identity(registration_path: str) -> str:
    if registration_path == "government":
        return "government"
    if registration_path == "nonprofit":
        return "non_profit"
    return "direct_customer"


def _policy_sids(registration_path: str) -> tuple[str, str]:
    if registration_path == "sole_proprietor":
        return SOLE_PROPRIETOR_CUSTOMER_PROFILE_POLICY_SID, SOLE_PROPRIETOR_TRUST_PRODUCT_POLICY_SID
    return STANDARD_CUSTOMER_PROFILE_POLICY_SID, STANDARD_TRUST_PRODUCT_POLICY_SID


def _load_status_payload(onboarding: OrganizationA2POnboarding) -> dict[str, Any]:
    raw_value = (onboarding.raw_status_json or "").strip()
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _store_status_payload(onboarding: OrganizationA2POnboarding, payload: dict[str, Any]) -> None:
    onboarding.raw_status_json = json.dumps(payload, sort_keys=True)


def _status_value(raw_value: Any) -> str | None:
    normalized = str(raw_value or "").strip()
    return normalized.lower() or None


def _build_form_data(payload: dict[str, Any], organization: Organization) -> A2PFormData:
    registration_path = (payload.get("registration_path") or "standard").strip().lower()
    number_strategy = (payload.get("number_strategy") or "auto_buy").strip().lower()
    if registration_path not in A2P_REGISTRATION_PATH_VALUES:
        raise ProviderProvisioningError("Choose a valid Twilio A2P registration path.")
    if number_strategy not in A2P_NUMBER_STRATEGY_VALUES:
        raise ProviderProvisioningError("Choose a valid Twilio A2P number strategy.")
    business_name = (payload.get("business_name") or organization.name or "").strip()
    email = (payload.get("email") or "").strip().lower()
    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    campaign_description = (payload.get("campaign_description") or "").strip()
    message_flow = (payload.get("message_flow") or "").strip()
    if not business_name:
        raise ProviderProvisioningError("Legal business name is required for A2P onboarding.")
    if not email:
        raise ProviderProvisioningError("Business email is required for A2P onboarding.")
    if not first_name or not last_name:
        raise ProviderProvisioningError("Authorized representative first and last name are required.")
    if not campaign_description:
        raise ProviderProvisioningError("Campaign description is required.")
    if not message_flow:
        raise ProviderProvisioningError("Message flow is required.")

    message_samples = _normalized_multiline_list(payload.get("message_samples"))
    if not message_samples:
        raise ProviderProvisioningError("At least one message sample is required.")

    mobile_number = (payload.get("mobile_number") or "").strip() or None
    desired_phone_number_sid = (payload.get("desired_phone_number_sid") or "").strip() or None
    if registration_path == "sole_proprietor" and not mobile_number:
        raise ProviderProvisioningError("A mobile number is required for sole proprietor onboarding.")
    if number_strategy in {"existing_subaccount_number", "transfer_parent_number"} and not desired_phone_number_sid:
        raise ProviderProvisioningError("A Twilio phone number SID is required for the selected number strategy.")

    return A2PFormData(
        registration_path=registration_path,
        number_strategy=number_strategy,
        business_name=business_name,
        business_type=(payload.get("business_type") or "").strip() or None,
        website_url=(payload.get("website_url") or "").strip() or None,
        social_profile_url=(payload.get("social_profile_url") or "").strip() or None,
        email=email,
        phone_number=(payload.get("phone_number") or "").strip() or None,
        mobile_number=mobile_number,
        first_name=first_name,
        last_name=last_name,
        job_position=(payload.get("job_position") or "").strip() or None,
        business_registration_identifier=(payload.get("business_registration_identifier") or "").strip() or None,
        business_registration_number=(payload.get("business_registration_number") or "").strip() or None,
        address_sid=(payload.get("address_sid") or "").strip() or None,
        supporting_document_sid=(payload.get("supporting_document_sid") or "").strip() or None,
        campaign_use_case=_normalize_use_case(registration_path, payload.get("campaign_use_case")),
        campaign_description=campaign_description,
        message_flow=message_flow,
        message_samples=message_samples,
        opt_in_message=(payload.get("opt_in_message") or "").strip() or None,
        opt_out_message=(payload.get("opt_out_message") or "").strip() or None,
        help_message=(payload.get("help_message") or "").strip() or None,
        opt_in_keywords=_normalized_csv_list(payload.get("opt_in_keywords"), default=DEFAULT_OPT_IN_KEYWORDS),
        opt_out_keywords=_normalized_csv_list(payload.get("opt_out_keywords"), default=DEFAULT_OPT_OUT_KEYWORDS),
        help_keywords=_normalized_csv_list(payload.get("help_keywords"), default=DEFAULT_HELP_KEYWORDS),
        has_embedded_links=_coerce_bool(payload.get("has_embedded_links")),
        has_embedded_phone=_coerce_bool(payload.get("has_embedded_phone")),
        desired_phone_number=(payload.get("desired_phone_number") or "").strip() or None,
        desired_phone_number_sid=desired_phone_number_sid,
        campaign_verify_token=(payload.get("campaign_verify_token") or "").strip() or None,
    )


def _save_form_data(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    form_data: A2PFormData,
) -> None:
    onboarding.registration_path = form_data.registration_path
    onboarding.number_strategy = form_data.number_strategy
    onboarding.business_name = form_data.business_name
    onboarding.business_type = form_data.business_type
    onboarding.business_identity = _business_identity(form_data.registration_path)
    onboarding.business_registration_identifier = form_data.business_registration_identifier
    onboarding.business_registration_number_encrypted = (
        encrypt_provider_secret(form_data.business_registration_number)
        if form_data.business_registration_number
        else None
    )
    onboarding.website_url = form_data.website_url
    onboarding.social_profile_url = form_data.social_profile_url
    onboarding.email = form_data.email
    onboarding.phone_number = form_data.phone_number
    onboarding.mobile_number = form_data.mobile_number
    onboarding.first_name = form_data.first_name
    onboarding.last_name = form_data.last_name
    onboarding.job_position = form_data.job_position
    onboarding.address_sid = form_data.address_sid
    onboarding.supporting_document_sid = form_data.supporting_document_sid
    onboarding.campaign_use_case = form_data.campaign_use_case
    onboarding.campaign_description = form_data.campaign_description
    onboarding.message_flow = form_data.message_flow
    onboarding.message_samples_json = json.dumps(form_data.message_samples)
    onboarding.opt_in_message = form_data.opt_in_message
    onboarding.opt_out_message = form_data.opt_out_message
    onboarding.help_message = form_data.help_message
    onboarding.opt_in_keywords_json = json.dumps(form_data.opt_in_keywords)
    onboarding.opt_out_keywords_json = json.dumps(form_data.opt_out_keywords)
    onboarding.help_keywords_json = json.dumps(form_data.help_keywords)
    onboarding.campaign_verify_token_encrypted = (
        encrypt_provider_secret(form_data.campaign_verify_token)
        if form_data.campaign_verify_token
        else None
    )
    onboarding.desired_phone_number = form_data.desired_phone_number
    onboarding.desired_phone_number_sid = form_data.desired_phone_number_sid
    onboarding.onboarding_status = "queued"
    onboarding.brand_status = None
    onboarding.campaign_status = None
    onboarding.verification_status = None
    onboarding.submitted_at = utc_now()
    onboarding.canceled_at = None
    onboarding.approved_at = None
    onboarding.last_error = None
    onboarding.failure_code = None
    onboarding.raw_submission_json = json.dumps(
        {
            "has_embedded_links": form_data.has_embedded_links,
            "has_embedded_phone": form_data.has_embedded_phone,
            "opt_in_keywords": form_data.opt_in_keywords,
            "opt_out_keywords": form_data.opt_out_keywords,
            "help_keywords": form_data.help_keywords,
        },
        sort_keys=True,
    )
    profile.business_type = form_data.business_type
    profile.use_case = form_data.campaign_description[:120]
    if profile.provider_status != "suspended":
        profile.set_provider_status("pending")


def _queue_job(job_name: str, organization_id: int, actor_user_id: int | None = None):
    if current_app.config.get("TWILIO_A2P_FAKE_QUEUE"):
        return QueuedA2PJobStub(
            job_name=job_name,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
        )
    queue = get_queue()
    return queue.enqueue(job_name, organization_id, actor_user_id)


def _mark_queue_failure(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    organization_id: int,
    *,
    actor_user_id: int | None = None,
) -> None:
    message = "Twilio A2P onboarding could not be queued. Check Redis/RQ and retry."
    _set_status(
        onboarding,
        profile,
        onboarding_status="error",
        brand_status=onboarding.brand_status,
        campaign_status=onboarding.campaign_status,
        verification_status=onboarding.verification_status,
        error_message=message,
    )
    _record_provider_audit(
        organization_id,
        "a2p_queue_failed",
        actor_user_id=actor_user_id,
        status="error",
        message=message,
    )
    db.session.commit()
    raise ProviderProvisioningError(message)


def submit_a2p_onboarding(
    organization_id: int,
    payload: dict[str, Any],
    *,
    actor_user_id: int | None = None,
) -> OrganizationA2POnboarding:
    if not a2p_onboarding_enabled():
        raise ProviderProvisioningError("Twilio A2P onboarding automation is not enabled.")

    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")

    profile = organization.messaging_profile or ensure_messaging_profile(organization)
    onboarding = ensure_a2p_onboarding(organization)
    form_data = _build_form_data(payload, organization)
    _save_form_data(onboarding, profile, form_data)
    db.session.commit()
    _record_provider_audit(
        organization.id,
        "a2p_submit",
        actor_user_id=actor_user_id,
        message=f"Queued Twilio A2P onboarding ({onboarding.registration_path}).",
        metadata={
            "registration_path": onboarding.registration_path,
            "number_strategy": onboarding.number_strategy,
            "campaign_use_case": onboarding.campaign_use_case,
        },
    )
    db.session.commit()
    try:
        _queue_job("app.tasks.process_a2p_onboarding_job", organization.id, actor_user_id)
    except Exception as exc:
        current_app.logger.exception(
            "Failed to queue Twilio A2P onboarding for organization_id=%s: %s",
            organization.id,
            exc,
        )
        _mark_queue_failure(onboarding, profile, organization.id, actor_user_id=actor_user_id)
    return onboarding


def refresh_a2p_onboarding(organization_id: int, *, actor_user_id: int | None = None) -> OrganizationA2POnboarding:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    if onboarding.onboarding_status == "draft":
        raise ProviderProvisioningError("Submit onboarding details before requesting a refresh.")
    if onboarding.onboarding_status == "canceled":
        raise ProviderProvisioningError("Canceled Twilio A2P onboarding cannot be refreshed.")
    onboarding.onboarding_status = "queued"
    onboarding.last_error = None
    db.session.commit()
    profile = organization.messaging_profile or ensure_messaging_profile(organization)
    try:
        _queue_job("app.tasks.process_a2p_onboarding_job", organization.id, actor_user_id)
    except Exception as exc:
        current_app.logger.exception(
            "Failed to queue Twilio A2P refresh for organization_id=%s: %s",
            organization.id,
            exc,
        )
        _mark_queue_failure(onboarding, profile, organization.id, actor_user_id=actor_user_id)
    return onboarding


def cancel_a2p_onboarding(organization_id: int, *, actor_user_id: int | None = None) -> OrganizationA2POnboarding:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    if onboarding.onboarding_status == "draft":
        raise ProviderProvisioningError("Twilio A2P onboarding has not been submitted yet.")
    if onboarding.onboarding_status == "approved":
        raise ProviderProvisioningError("Approved Twilio A2P onboarding cannot be canceled.")
    if onboarding.onboarding_status == "canceled":
        raise ProviderProvisioningError("Twilio A2P onboarding is already canceled.")
    onboarding.onboarding_status = "canceled"
    onboarding.canceled_at = utc_now()
    db.session.commit()
    _record_provider_audit(
        organization.id,
        "a2p_cancel",
        actor_user_id=actor_user_id,
        message="Canceled Twilio A2P onboarding.",
    )
    db.session.commit()
    return onboarding


def _ensure_provider_resources(organization: Organization) -> OrganizationMessagingProfile:
    profile = organization.messaging_profile or ensure_messaging_profile(organization)
    if not profile.twilio_subaccount_sid or not profile.messaging_service_sid:
        profile = provision_org(organization.id)
    return profile


def _set_status(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    onboarding_status: str,
    brand_status: str | None = None,
    campaign_status: str | None = None,
    verification_status: str | None = None,
    error_message: str | None = None,
) -> None:
    onboarding.onboarding_status = onboarding_status
    onboarding.brand_status = brand_status
    onboarding.campaign_status = campaign_status
    onboarding.verification_status = verification_status
    onboarding.last_synced_at = utc_now()
    onboarding.last_error = error_message
    profile.provider_last_checked_at = utc_now()
    if error_message and profile.provider_status != "suspended":
        profile.set_provider_status("error")
        profile.last_provision_error = error_message


def _create_end_user(client, *, friendly_name: str, type_name: str, attributes: dict[str, Any]):
    return client.trusthub.v1.end_users.create(
        friendly_name=friendly_name,
        type=type_name,
        attributes=attributes,
    )


def _json_loads_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _upsert_a2p_resources(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile) -> None:
    client = _build_subaccount_client(profile)
    status_payload = _load_status_payload(onboarding)
    primary_customer_profile_sid = (current_app.config.get("TWILIO_PRIMARY_CUSTOMER_PROFILE_SID") or "").strip()

    customer_profile_policy_sid, trust_product_policy_sid = _policy_sids(onboarding.registration_path)
    if not onboarding.customer_profile_sid:
        customer_profile = client.trusthub.v1.customer_profiles.create(
            policy_sid=customer_profile_policy_sid,
            friendly_name=f"{onboarding.business_name} Customer Profile",
            email=onboarding.email,
        )
        onboarding.customer_profile_sid = customer_profile.sid

    if onboarding.registration_path == "sole_proprietor":
        if not status_payload.get("sole_proprietor_end_user_sid"):
            sole_prop = _create_end_user(
                client,
                friendly_name=f"{onboarding.business_name} Sole Proprietor",
                type_name="sole_proprietor_information",
                attributes={
                    "first_name": onboarding.first_name,
                    "last_name": onboarding.last_name,
                    "email": onboarding.email,
                    "phone_number": onboarding.mobile_number or onboarding.phone_number,
                    "business_title": onboarding.job_position or "Owner",
                    "job_position": onboarding.job_position or "Owner",
                },
            )
            status_payload["sole_proprietor_end_user_sid"] = sole_prop.sid
    else:
        if not status_payload.get("business_information_end_user_sid"):
            business_info = _create_end_user(
                client,
                friendly_name=f"{onboarding.business_name} Business Information",
                type_name="customer_profile_business_information",
                attributes={
                    "business_name": onboarding.business_name,
                    "social_media_profile_urls": onboarding.social_profile_url or "",
                    "website_url": onboarding.website_url or "",
                    "business_regions_of_operation": "USA_AND_CANADA",
                    "business_type": onboarding.business_type or "Nonprofit",
                    "business_registration_identifier": onboarding.business_registration_identifier or "EIN",
                    "business_identity": onboarding.business_identity or "direct_customer",
                    "business_industry": "OTHER",
                    "business_registration_number": decrypt_provider_secret(onboarding.business_registration_number_encrypted)
                    if onboarding.business_registration_number_encrypted
                    else "",
                },
            )
            status_payload["business_information_end_user_sid"] = business_info.sid
        if not status_payload.get("authorized_representative_sid"):
            authorized_rep = _create_end_user(
                client,
                friendly_name=f"{onboarding.business_name} Authorized Representative",
                type_name="authorized_representative_1",
                attributes={
                    "first_name": onboarding.first_name,
                    "last_name": onboarding.last_name,
                    "email": onboarding.email,
                    "phone_number": onboarding.mobile_number or onboarding.phone_number,
                    "business_title": onboarding.job_position or "Owner",
                    "job_position": onboarding.job_position or "Owner",
                },
            )
            status_payload["authorized_representative_sid"] = authorized_rep.sid
        if onboarding.address_sid and not status_payload.get("supporting_document_sid"):
            supporting_document = client.trusthub.v1.supporting_documents.create(
                friendly_name=onboarding.business_name[:64],
                type="customer_profile_address",
                attributes={"address_sids": onboarding.address_sid},
            )
            status_payload["supporting_document_sid"] = supporting_document.sid
            onboarding.supporting_document_sid = supporting_document.sid

    if not onboarding.trust_product_sid:
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"{onboarding.business_name} A2P Trust Product",
            policy_sid=trust_product_policy_sid,
            email=onboarding.email,
        )
        onboarding.trust_product_sid = trust_product.sid

    if onboarding.registration_path == "sole_proprietor":
        object_sid = status_payload.get("sole_proprietor_end_user_sid")
        if object_sid and status_payload.get("sole_prop_assigned") != object_sid:
            client.trusthub.v1.trust_products(onboarding.trust_product_sid).trust_products_entity_assignments.create(
                object_sid=object_sid
            )
            status_payload["sole_prop_assigned"] = object_sid
    else:
        business_sid = status_payload.get("business_information_end_user_sid")
        if business_sid and status_payload.get("business_info_assigned") != business_sid:
            client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_entity_assignments.create(
                object_sid=business_sid
            )
            status_payload["business_info_assigned"] = business_sid
        authorized_sid = status_payload.get("authorized_representative_sid")
        if authorized_sid and status_payload.get("authorized_rep_assigned") != authorized_sid:
            client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_entity_assignments.create(
                object_sid=authorized_sid
            )
            status_payload["authorized_rep_assigned"] = authorized_sid
        supporting_sid = status_payload.get("supporting_document_sid")
        if supporting_sid and status_payload.get("supporting_doc_assigned") != supporting_sid:
            client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_entity_assignments.create(
                object_sid=supporting_sid
            )
            status_payload["supporting_doc_assigned"] = supporting_sid
        if primary_customer_profile_sid and status_payload.get("primary_profile_assigned") != primary_customer_profile_sid:
            client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_entity_assignments.create(
                object_sid=primary_customer_profile_sid
            )
            status_payload["primary_profile_assigned"] = primary_customer_profile_sid

    if not status_payload.get("messaging_profile_end_user_sid"):
        messaging_profile = _create_end_user(
            client,
            friendly_name=f"{onboarding.business_name} Messaging Profile",
            type_name="us_a2p_messaging_profile_information",
            attributes={"company_type": "private"},
        )
        status_payload["messaging_profile_end_user_sid"] = messaging_profile.sid

    messaging_profile_sid = status_payload.get("messaging_profile_end_user_sid")
    if messaging_profile_sid and status_payload.get("messaging_profile_assigned") != messaging_profile_sid:
        client.trusthub.v1.trust_products(onboarding.trust_product_sid).trust_products_entity_assignments.create(
            object_sid=messaging_profile_sid
        )
        status_payload["messaging_profile_assigned"] = messaging_profile_sid

    if status_payload.get("customer_profile_assigned") != onboarding.customer_profile_sid:
        client.trusthub.v1.trust_products(onboarding.trust_product_sid).trust_products_entity_assignments.create(
            object_sid=onboarding.customer_profile_sid
        )
        status_payload["customer_profile_assigned"] = onboarding.customer_profile_sid

    if not status_payload.get("customer_profile_evaluated"):
        evaluation = client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_evaluations.create(
            policy_sid=customer_profile_policy_sid
        )
        status_payload["customer_profile_evaluation_sid"] = getattr(evaluation, "sid", None)
        status_payload["customer_profile_evaluated"] = True
    if not status_payload.get("customer_profile_submitted"):
        client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).update(status="pending-review")
        status_payload["customer_profile_submitted"] = True

    if not status_payload.get("trust_product_evaluated"):
        evaluation = client.trusthub.v1.trust_products(onboarding.trust_product_sid).trust_products_evaluations.create(
            policy_sid=trust_product_policy_sid
        )
        status_payload["trust_product_evaluation_sid"] = getattr(evaluation, "sid", None)
        status_payload["trust_product_evaluated"] = True
    if not status_payload.get("trust_product_submitted"):
        client.trusthub.v1.trust_products(onboarding.trust_product_sid).update(status="pending-review")
        status_payload["trust_product_submitted"] = True

    if not onboarding.brand_registration_sid:
        brand_registration = client.messaging.v1.brand_registrations.create(
            customer_profile_bundle_sid=onboarding.customer_profile_sid,
            a2p_profile_bundle_sid=onboarding.trust_product_sid,
            brand_type="SOLE_PROPRIETOR" if onboarding.registration_path == "sole_proprietor" else "STANDARD",
        )
        onboarding.brand_registration_sid = brand_registration.sid

    if not onboarding.campaign_sid:
        submission_payload = json.loads(onboarding.raw_submission_json or "{}")
        campaign = _build_subaccount_client(profile).messaging.v1.services(profile.messaging_service_sid).us_app_to_person.create(
            brand_registration_sid=onboarding.brand_registration_sid,
            description=onboarding.campaign_description or onboarding.business_name,
            message_flow=onboarding.message_flow or onboarding.campaign_description or onboarding.business_name,
            message_samples=_json_loads_list(onboarding.message_samples_json) or [onboarding.campaign_description or onboarding.business_name],
            us_app_to_person_usecase=onboarding.campaign_use_case,
            has_embedded_links=bool(submission_payload.get("has_embedded_links")),
            has_embedded_phone=bool(submission_payload.get("has_embedded_phone")),
            opt_in_message=onboarding.opt_in_message or None,
            opt_out_message=onboarding.opt_out_message or None,
            help_message=onboarding.help_message or None,
            opt_in_keywords=_json_loads_list(onboarding.opt_in_keywords_json),
            opt_out_keywords=_json_loads_list(onboarding.opt_out_keywords_json),
            help_keywords=_json_loads_list(onboarding.help_keywords_json),
        )
        onboarding.campaign_sid = campaign.sid

    _store_status_payload(onboarding, status_payload)


def _buy_phone_number(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile):
    subaccount_client = _build_subaccount_client(profile)
    country = current_app.config.get("TWILIO_A2P_NUMBER_COUNTRY") or "US"
    if onboarding.desired_phone_number:
        purchased = subaccount_client.incoming_phone_numbers.create(
            phone_number=onboarding.desired_phone_number,
            bundle_sid=onboarding.supporting_document_sid or onboarding.address_sid or None,
        )
        return purchased.sid, purchased.phone_number

    near_number = onboarding.phone_number or onboarding.mobile_number
    search_results = subaccount_client.available_phone_numbers(country).local.list(
        sms_enabled=True,
        near_number=near_number or None,
        exclude_all_address_required=False,
        limit=1,
    )
    if not search_results:
        raise ProviderProvisioningError("Twilio could not find a purchasable local SMS number for this organization.")
    candidate = search_results[0]
    purchased = subaccount_client.incoming_phone_numbers.create(
        phone_number=candidate.phone_number,
        address_sid=onboarding.address_sid or None,
        bundle_sid=onboarding.supporting_document_sid or None,
    )
    return purchased.sid, purchased.phone_number


def _transfer_parent_number(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile) -> tuple[str, str]:
    if not onboarding.desired_phone_number_sid:
        raise ProviderProvisioningError("Phone number SID is required to transfer a parent-account number.")

    master_client = _master_client()
    phone_context = master_client.incoming_phone_numbers(onboarding.desired_phone_number_sid)
    existing_number = phone_context.fetch()
    master_account_sid = current_app.config.get("TWILIO_ACCOUNT_SID")
    if getattr(existing_number, "account_sid", None) != master_account_sid:
        raise ProviderProvisioningError("Only parent-account numbers owned by this platform can be transferred automatically.")

    transferred = phone_context.update(
        account_sid=profile.twilio_subaccount_sid,
        sms_url=_twilio_inbound_webhook_url(),
        sms_method="POST",
    )
    return transferred.sid, transferred.phone_number


def _attach_existing_number(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile) -> tuple[str, str]:
    if not onboarding.desired_phone_number_sid:
        raise ProviderProvisioningError("Phone number SID is required for the existing-number path.")
    subaccount_client = _build_subaccount_client(profile)
    existing_number = subaccount_client.incoming_phone_numbers(onboarding.desired_phone_number_sid).fetch()
    return existing_number.sid, existing_number.phone_number


def _sync_remote_status(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile) -> tuple[str | None, str | None]:
    client = _build_subaccount_client(profile)
    brand_status = None
    campaign_status = None
    status_payload = _load_status_payload(onboarding)

    if onboarding.brand_registration_sid:
        brand = client.messaging.v1.brand_registrations(onboarding.brand_registration_sid).fetch()
        brand_status = _status_value(getattr(brand, "status", None))
        status_payload["brand_failure_reason"] = getattr(brand, "failure_reason", None)
        status_payload["brand_tcr_id"] = getattr(brand, "tcr_id", None)
    if onboarding.campaign_sid and profile.messaging_service_sid:
        campaign = _build_subaccount_client(profile).messaging.v1.services(profile.messaging_service_sid).us_app_to_person(
            onboarding.campaign_sid
        ).fetch()
        campaign_status = _status_value(getattr(campaign, "status", None))
        status_payload["campaign_failure_reason"] = getattr(campaign, "failure_reason", None)

    _store_status_payload(onboarding, status_payload)
    return brand_status, campaign_status


def _complete_number_setup(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile, actor_user_id: int | None) -> None:
    if profile.from_number and profile.phone_number_sid:
        return

    if onboarding.number_strategy == "auto_buy":
        phone_number_sid, from_number = _buy_phone_number(onboarding, profile)
    elif onboarding.number_strategy == "transfer_parent_number":
        phone_number_sid, from_number = _transfer_parent_number(onboarding, profile)
    else:
        phone_number_sid, from_number = _attach_existing_number(onboarding, profile)

    profile.phone_number_sid = phone_number_sid
    profile.from_number = from_number
    profile.inbound_identity = from_number
    profile.sender_review_status = "approved"
    profile.consent_acknowledged_at = profile.consent_acknowledged_at or utc_now()
    _configure_service_webhooks(profile, client=_build_subaccount_client(profile))
    sync_sender_assignment(profile.organization_id, actor_user_id=actor_user_id)


def process_a2p_onboarding(organization_id: int, actor_user_id: int | None = None) -> OrganizationA2POnboarding:
    if not a2p_onboarding_enabled():
        raise ProviderProvisioningError("Twilio A2P onboarding automation is not enabled.")

    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    profile = _ensure_provider_resources(organization)

    if onboarding.onboarding_status in {"canceled", "approved"}:
        return onboarding

    onboarding.onboarding_status = "processing"
    onboarding.last_error = None
    db.session.commit()

    try:
        _upsert_a2p_resources(onboarding, profile)
        brand_status, campaign_status = _sync_remote_status(onboarding, profile)
        onboarding.brand_status = brand_status
        onboarding.campaign_status = campaign_status
        profile.provider_last_checked_at = utc_now()

        normalized_brand_status = brand_status or "pending"
        normalized_campaign_status = campaign_status or "pending"
        if normalized_brand_status in {"failed", "rejected"} or normalized_campaign_status in {"failed", "rejected"}:
            _set_status(
                onboarding,
                profile,
                onboarding_status="rejected",
                brand_status=normalized_brand_status,
                campaign_status=normalized_campaign_status,
                error_message=_load_status_payload(onboarding).get("campaign_failure_reason")
                or _load_status_payload(onboarding).get("brand_failure_reason")
                or "Twilio rejected the A2P registration.",
            )
        elif normalized_brand_status in {"approved"} and normalized_campaign_status in {"approved", "active"}:
            _complete_number_setup(onboarding, profile, actor_user_id)
            onboarding.onboarding_status = "approved"
            onboarding.approved_at = utc_now()
            onboarding.last_synced_at = utc_now()
            profile.sender_review_status = "approved"
            profile.consent_acknowledged_at = profile.consent_acknowledged_at or utc_now()
            if profile.provider_status != "suspended":
                profile.set_provider_status("active")
            profile.last_provision_error = None
        else:
            _set_status(
                onboarding,
                profile,
                onboarding_status="pending",
                brand_status=normalized_brand_status,
                campaign_status=normalized_campaign_status,
                verification_status="pending" if onboarding.registration_path == "sole_proprietor" else None,
            )
            if profile.provider_status != "suspended":
                profile.set_provider_status("pending")

        db.session.commit()
        return onboarding
    except Exception as exc:
        db.session.rollback()
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            raise
        onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
        profile = organization.messaging_profile or ensure_messaging_profile(organization)
        _set_status(
            onboarding,
            profile,
            onboarding_status="error",
            brand_status=onboarding.brand_status,
            campaign_status=onboarding.campaign_status,
            error_message=str(exc),
        )
        _record_provider_audit(
            organization.id,
            "a2p_failed",
            actor_user_id=actor_user_id,
            status="error",
            message=str(exc),
        )
        db.session.commit()
        raise ProviderProvisioningError(str(exc)) from exc


def reconcile_pending_a2p_onboardings() -> dict[str, int]:
    summary = {
        "records_seen": 0,
        "records_processed": 0,
        "records_failed": 0,
    }
    pending_records = OrganizationA2POnboarding.query.filter(
        OrganizationA2POnboarding.onboarding_status.in_(("queued", "processing", "pending", "needs_action"))
    ).all()
    for onboarding in pending_records:
        summary["records_seen"] += 1
        try:
            process_a2p_onboarding(onboarding.organization_id)
            summary["records_processed"] += 1
        except ProviderProvisioningError:
            current_app.logger.exception(
                "A2P reconcile failed for organization_id=%s onboarding_id=%s",
                onboarding.organization_id,
                onboarding.id,
            )
            summary["records_failed"] += 1
    return summary
