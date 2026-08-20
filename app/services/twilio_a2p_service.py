from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from flask import current_app
from twilio.base import serialize, values
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
from app.services.public_https_service import PublicHttpsFetchError, fetch_public_https_text
from app.services.twilio_service import (
    PlatformSubaccountAuthRequiredError,
    ProviderProvisioningError,
    _build_subaccount_client,
    _build_subaccount_client_context,
    _client_for_profile,
    _configure_service_webhooks,
    _master_client,
    _record_provider_audit,
    _twilio_inbound_webhook_url,
    customer_managed_activation_complete,
    customer_managed_activation_state,
    ensure_messaging_profile,
    finalize_sender_setup,
    provision_org,
    require_chargeable_provider_entitlement,
    seed_service_address_from_onboarding,
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
    ("platform_assign", "Assign the first sender later from platform support"),
    ("auto_buy", "Buy a new number automatically"),
    ("existing_subaccount_number", "Use an existing subaccount number"),
    ("transfer_parent_number", "Transfer an existing parent-account number"),
)

A2P_BUSINESS_TYPE_CHOICES = (
    ("Co-operative", "Co-operative"),
    ("Corporation", "Corporation"),
    ("Limited Liability Corporation", "Limited Liability Corporation"),
    ("Non-profit Corporation", "Non-profit Corporation"),
    ("Partnership", "Partnership"),
    ("Sole Proprietor", "Sole Proprietor"),
)

A2P_CAMPAIGN_USE_CASES = (
    ("MIXED", "Mixed"),
    ("ACCOUNT_NOTIFICATION", "Account Notification"),
    ("CUSTOMER_CARE", "Customer Care"),
    ("MARKETING", "Marketing"),
    ("SOLE_PROPRIETOR", "Sole Proprietor"),
)

A2P_BUSINESS_INDUSTRY_CHOICES = (
    ("EDUCATION", "Education"),
    ("FINANCIAL_SERVICES", "Financial Services"),
    ("GOVERNMENT", "Government"),
    ("HEALTHCARE", "Healthcare"),
    ("HOSPITALITY", "Hospitality"),
    ("INSURANCE", "Insurance"),
    ("NONPROFIT", "Nonprofit"),
    ("REAL_ESTATE", "Real Estate"),
    ("RETAIL", "Retail"),
    ("TECHNOLOGY", "Technology"),
    ("TRANSPORTATION", "Transportation"),
    ("OTHER", "Other"),
)

A2P_BUSINESS_REGION_CHOICES = (
    ("AFRICA", "Africa"),
    ("ASIA", "Asia"),
    ("AUSTRALIA", "Australia"),
    ("EUROPE", "Europe"),
    ("LATIN_AMERICA", "Latin America"),
    ("USA_AND_CANADA", "USA and Canada"),
)

A2P_JOB_POSITION_CHOICES = (
    ("Director", "Director"),
    ("GM", "GM"),
    ("VP", "VP"),
    ("CEO", "CEO"),
    ("CFO", "CFO"),
    ("General Counsel", "General Counsel"),
    ("Other", "Other"),
)

A2P_REGISTRATION_IDENTIFIER_CHOICES = (
    ("EIN", "USA: Employer Identification Number (EIN)"),
)

DEFAULT_OPT_IN_KEYWORDS = ["START", "SUBSCRIBE", "YES"]
DEFAULT_OPT_OUT_KEYWORDS = ["STOP", "UNSUBSCRIBE", "END"]
DEFAULT_HELP_KEYWORDS = ["HELP", "INFO"]
DEFAULT_A2P_CAMPAIGN_USE_CASE = "ACCOUNT_NOTIFICATION"
A2P_EVENT_STREAM_SUBSCRIPTION_TYPES = (
    "com.twilio.messaging.compliance.brand-registration.brand-registered",
    "com.twilio.messaging.compliance.brand-registration.brand-failure",
    "com.twilio.messaging.compliance.brand-registration.brand-verified",
    "com.twilio.messaging.compliance.brand-registration.brand-unverified",
    "com.twilio.messaging.compliance.brand-registration.brand-vetted-verified",
    "com.twilio.messaging.compliance.brand-registration.brand-secondary-vetting-failure",
    "com.twilio.messaging.compliance.campaign-registration.campaign-submitted",
    "com.twilio.messaging.compliance.campaign-registration.campaign-failure",
    "com.twilio.messaging.compliance.campaign-registration.campaign-approved",
    "com.twilio.messaging.compliance.number-registration.failed",
    "com.twilio.messaging.compliance.number-registration.pending",
    "com.twilio.messaging.compliance.number-registration.successful",
    "com.twilio.messaging.compliance.number-deregistration.failed",
    "com.twilio.messaging.compliance.number-deregistration.pending",
    "com.twilio.messaging.compliance.number-deregistration.successful",
)
A2P_REGISTRATION_PATH_VALUES = {value for value, _ in A2P_REGISTRATION_PATHS}
A2P_NUMBER_STRATEGY_VALUES = {value for value, _ in A2P_NUMBER_STRATEGIES}
A2P_ALLOWED_BUSINESS_TYPES = {value for value, _ in A2P_BUSINESS_TYPE_CHOICES}
A2P_ALLOWED_BUSINESS_INDUSTRIES = {value for value, _ in A2P_BUSINESS_INDUSTRY_CHOICES}
A2P_ALLOWED_BUSINESS_REGIONS = {value for value, _ in A2P_BUSINESS_REGION_CHOICES}
A2P_ALLOWED_JOB_POSITIONS = {value for value, _ in A2P_JOB_POSITION_CHOICES}
A2P_BUSINESS_TYPE_ALIASES = {
    "co operative": "Co-operative",
    "co operative society": "Co-operative",
    "co operative corporation": "Co-operative",
    "co operative company": "Co-operative",
    "cooperative": "Co-operative",
    "cooperative society": "Co-operative",
    "corporation": "Corporation",
    "corp": "Corporation",
    "limited liability company": "Limited Liability Corporation",
    "limited liability corporation": "Limited Liability Corporation",
    "llc": "Limited Liability Corporation",
    "single member limited liability company": "Limited Liability Corporation",
    "single member llc": "Limited Liability Corporation",
    "non profit": "Non-profit Corporation",
    "non profit corporation": "Non-profit Corporation",
    "nonprofit": "Non-profit Corporation",
    "nonprofit corporation": "Non-profit Corporation",
    "partnership": "Partnership",
    "sole proprietor": "Sole Proprietor",
}
A2P_BRAND_FAILURE_STATUSES = {"failed", "rejected", "registration_failed", "secondary_vetting_failed"}
A2P_CAMPAIGN_FAILURE_STATUSES = {"failed", "rejected", "deleted"}
A2P_NUMBER_FAILURE_STATUSES = {"failed", "rejected", "registration_failed"}
A2P_BRAND_APPROVED_STATUSES = {"approved", "registered", "verified", "vetting_verified"}
A2P_BRAND_READY_FOR_CAMPAIGN_STATUSES = set(A2P_BRAND_APPROVED_STATUSES)
A2P_CAMPAIGN_APPROVED_STATUSES = {"approved", "active", "verified"}
A2P_REVIEWING_STATUSES = {"pending", "pending-review", "processing", "queued", "registered", "submitted", "unverified", "in_progress"}
A2P_EVENT_STREAM_RECENT_EVENT_LIMIT = 20
A2P_CAMPAIGN_RECREATE_DELAY_SECONDS = 5
A2P_CAMPAIGN_ASSOCIATION_CONFLICT_FRAGMENT = "already a campaign associated with this messaging service"
A2P_RECOVERY_STATE_KEY = "recovery_state"
A2P_RECONCILED_PROFILE_APPROVED_STATUSES = {"approved", "twilio-approved", "in_review", "pending-review"}
A2P_RECONCILED_TRUST_PRODUCT_APPROVED_STATUSES = {"approved", "twilio-approved", "in_review", "pending-review"}
A2P_TRANSIENT_PROVIDER_ERROR_FRAGMENTS = (
    "failed to resolve",
    "temporary failure in name resolution",
    "name resolution",
    "max retries exceeded",
    "timed out",
    "read timed out",
    "connection aborted",
    "connection reset",
    "connection refused",
    "connecttimeouterror",
    "readtimeouterror",
    "httpsconnectionpool",
    "api.twilio.com",
    "messaging.twilio.com",
)


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
    legal_business_name: str
    public_brand_name: str
    business_type: str | None
    business_industry: str | None
    has_business_tax_id: bool
    brand_registration_mode: str
    business_regions: list[str]
    has_public_website: bool
    submission_source_mode: str
    submission_source_reason: str | None
    external_website_url: str | None
    external_privacy_policy_url: str | None
    external_terms_and_conditions_url: str | None
    external_cta_proof_url: str | None
    external_url_validation: dict[str, Any]
    website_url: str | None
    social_profile_url: str | None
    privacy_policy_url: str | None
    terms_and_conditions_url: str | None
    cta_proof_url: str | None
    email: str
    notification_email: str
    phone_number: str | None
    mobile_number: str | None
    first_name: str
    last_name: str
    business_title: str | None
    job_position: str | None
    business_registration_identifier: str | None
    business_registration_number: str | None
    address_country: str | None
    address_line1: str | None
    address_line2: str | None
    address_city: str | None
    address_region: str | None
    address_postal_code: str | None
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
    upgrade_recommended_reason: str | None
    upgrade_requested: bool
    declaration_accepted: bool


def a2p_onboarding_enabled() -> bool:
    return bool(current_app.config.get("TWILIO_A2P_ONBOARDING_ENABLED"))


def ensure_a2p_onboarding(organization: Organization) -> OrganizationA2POnboarding:
    onboarding = organization.a2p_onboarding
    if onboarding is not None:
        return onboarding

    onboarding = OrganizationA2POnboarding(
        organization=organization,
        business_name=organization.name,
        legal_business_name=organization.name,
        public_brand_name=organization.name,
        brand_registration_mode="low_volume_standard",
    )
    db.session.add(onboarding)
    db.session.flush()
    return onboarding


def a2p_registration_path_choices() -> tuple[tuple[str, str], ...]:
    return A2P_REGISTRATION_PATHS


def a2p_number_strategy_choices() -> tuple[tuple[str, str], ...]:
    return A2P_NUMBER_STRATEGIES


def a2p_business_type_choices() -> tuple[tuple[str, str], ...]:
    return A2P_BUSINESS_TYPE_CHOICES


def a2p_campaign_use_case_choices() -> tuple[tuple[str, str], ...]:
    return A2P_CAMPAIGN_USE_CASES


def a2p_business_industry_choices() -> tuple[tuple[str, str], ...]:
    return A2P_BUSINESS_INDUSTRY_CHOICES


def a2p_business_region_choices() -> tuple[tuple[str, str], ...]:
    return A2P_BUSINESS_REGION_CHOICES


def a2p_job_position_choices() -> tuple[tuple[str, str], ...]:
    return A2P_JOB_POSITION_CHOICES


def a2p_registration_identifier_choices() -> tuple[tuple[str, str], ...]:
    return A2P_REGISTRATION_IDENTIFIER_CHOICES


def _public_base_url() -> str | None:
    base_url = (
        current_app.config.get("APP_BASE_URL")
        or current_app.config.get("SAAS_BASE_URL")
        or ""
    ).strip().rstrip("/")
    return base_url or None


def a2p_event_streams_enabled() -> bool:
    return bool(current_app.config.get("TWILIO_A2P_EVENT_STREAMS_ENABLED"))


def a2p_event_stream_destination_url(organization: Organization) -> str | None:
    base_url = _public_base_url()
    if not base_url:
        return None
    return f"{base_url}/webhooks/twilio/a2p-events?organization_id={organization.id}"


def hosted_a2p_compliance_urls(
    organization: Organization,
    onboarding: OrganizationA2POnboarding | None = None,
) -> dict[str, str]:
    base_url = _public_base_url()
    hosted_cta_url = f"{base_url}/compliance/{organization.slug}/sms/opt-in" if base_url else ""
    hosted_privacy_url = f"{base_url}/compliance/{organization.slug}/sms/privacy" if base_url else ""
    hosted_terms_url = f"{base_url}/compliance/{organization.slug}/sms/terms" if base_url else ""
    return {
        "website_url": hosted_cta_url,
        "privacy_policy_url": hosted_privacy_url,
        "terms_and_conditions_url": hosted_terms_url,
        "cta_proof_url": hosted_cta_url,
    }


def _clean_text(raw_value: Any, *, lowercase: bool = False) -> str | None:
    value = str(raw_value or "").strip()
    if not value or value.lower() in {"none", "null"}:
        return None
    return value.lower() if lowercase else value


def _normalized_csv_list(raw_value: str | None, *, default: list[str] | None = None) -> list[str]:
    value = _clean_text(raw_value) or ""
    if not value:
        return list(default or [])
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def _normalize_public_url(raw_value: str | None, *, field_label: str) -> str | None:
    value = _clean_text(raw_value)
    if not value:
        return None
    if not re.match(r"^https?://", value, re.IGNORECASE):
        raise ProviderProvisioningError(f"{field_label} must be a public http:// or https:// URL.")
    return value[:255]


def _normalize_external_public_url(raw_value: str | None) -> tuple[str | None, str | None]:
    value = _clean_text(raw_value)
    if not value:
        return None, None
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return None, "must use a public https:// URL"
    if parsed.username or parsed.password:
        return None, "must not contain user information"
    try:
        port = parsed.port or 443
    except ValueError:
        return None, "contains an invalid port"
    if port != 443:
        return None, "must use the standard HTTPS port 443"
    if _is_reserved_test_host(value) and not current_app.testing:
        return None, "must not use a reserved test or local domain"
    return value[:255], None


def _http_fetch_text(url: str) -> tuple[int | None, str, str | None]:
    timeout = float(current_app.config.get("TWILIO_A2P_URL_VALIDATION_TIMEOUT", 5))
    max_bytes = int(current_app.config.get("TWILIO_A2P_URL_VALIDATION_MAX_BYTES", 131072))
    max_redirects = int(current_app.config.get("TWILIO_A2P_URL_VALIDATION_MAX_REDIRECTS", 3))
    last_error: PublicHttpsFetchError | None = None
    for attempt in range(1, 3):
        try:
            response = fetch_public_https_text(
                url,
                timeout,
                max_bytes,
                max_redirects,
                "Mozilla/5.0 (compatible; TwineviaA2PValidator/1.0)",
            )
            return response.status_code, response.body, None
        except PublicHttpsFetchError as exc:
            last_error = exc
            if attempt == 1:
                current_app.logger.warning(
                    "Public compliance URL validation fetch failed; retrying.",
                    extra={"url": url, "attempt": attempt, "error": str(exc)},
                )
    return None, "", str(last_error or "Public URL validation failed.")


def _is_reserved_test_host(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname.endswith((".test", ".example", ".invalid", ".localhost"))


def _page_contains_all(text: str, phrases: tuple[str, ...]) -> bool:
    normalized = (text or "").lower()
    return all(phrase in normalized for phrase in phrases)


def _page_contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    normalized = (text or "").lower()
    return any(phrase in normalized for phrase in phrases)


def _validate_external_submission_urls(
    *,
    external_urls: dict[str, str | None],
) -> tuple[bool, dict[str, Any], str | None]:
    results: dict[str, Any] = {"overall_valid": False, "fields": {}}
    missing_labels: list[str] = []
    normalization_errors: list[str] = []

    required_labels = {
        "website_url": "website/contact",
        "privacy_policy_url": "privacy policy",
        "terms_and_conditions_url": "terms and conditions",
        "cta_proof_url": "CTA proof",
    }
    normalized_urls: dict[str, str] = {}

    for field_name, label in required_labels.items():
        normalized_url, error_message = _normalize_external_public_url(external_urls.get(field_name))
        field_result = {
            "provided": bool(_clean_text(external_urls.get(field_name))),
            "url": normalized_url or _clean_text(external_urls.get(field_name)),
            "reachable": False,
            "status_code": None,
            "valid": False,
            "error": error_message,
        }
        if not field_result["provided"]:
            missing_labels.append(label)
        elif error_message:
            normalization_errors.append(f"{label} {error_message}")
        else:
            normalized_urls[field_name] = normalized_url or ""
        results["fields"][field_name] = field_result

    if missing_labels or normalization_errors:
        reason_parts: list[str] = []
        if missing_labels:
            reason_parts.append(f"missing {', '.join(missing_labels)}")
        if normalization_errors:
            reason_parts.append("; ".join(normalization_errors))
        return False, results, "Hosted fallback selected because the tenant website package is incomplete: " + " ".join(reason_parts)

    page_checks = {
        "website_url": lambda body: True,
        "privacy_policy_url": lambda body: _page_contains_any(
            body,
            (
                "privacy",
                "mobile opt-in",
                "not sold",
                "not shared",
            ),
        ),
        "terms_and_conditions_url": lambda body: _page_contains_all(
            body,
            (
                "stop",
                "help",
            ),
        )
        and _page_contains_any(
            body,
            (
                "message and data rates may apply",
                "message frequency",
                "frequency varies",
            ),
        ),
        "cta_proof_url": lambda body: _page_contains_any(
            body,
            (
                "opt in",
                "opt-in",
                "consent",
                "subscribe",
                "sign up",
            ),
        )
        and _page_contains_any(
            body,
            (
                "stop",
                "help",
            ),
        ),
    }
    page_errors: list[str] = []
    for field_name, url in normalized_urls.items():
        if _is_reserved_test_host(url) and current_app.testing:
            field_result = results["fields"][field_name]
            field_result["status_code"] = 200
            field_result["reachable"] = True
            field_result["valid"] = True
            continue
        status_code, body, fetch_error = _http_fetch_text(url)
        field_result = results["fields"][field_name]
        field_result["status_code"] = status_code
        field_result["reachable"] = status_code == 200
        if status_code != 200:
            field_result["error"] = fetch_error or f"returned HTTP {status_code or 'unreachable'}"
            page_errors.append(f"{required_labels[field_name]} page {field_result['error']}")
            continue
        if not page_checks[field_name](body):
            field_result["error"] = "did not include the required public SMS disclosures"
            page_errors.append(f"{required_labels[field_name]} page {field_result['error']}")
            continue
        field_result["valid"] = True

    if page_errors:
        return False, results, "Hosted fallback selected because the tenant website package did not pass validation: " + "; ".join(page_errors)

    results["overall_valid"] = True
    return True, results, "Using the tenant-provided public website, privacy, terms, and CTA proof URLs."


def _derive_brand_registration_mode(
    registration_path: str,
) -> str:
    if registration_path == "sole_proprietor":
        return "sole_proprietor"
    if registration_path == "standard":
        return "standard"
    return "low_volume_standard"


def _determine_registration_path(
    payload: dict[str, Any],
    *,
    has_business_tax_id: bool,
) -> str:
    requested_path = _clean_text(payload.get("registration_path"), lowercase=True)
    if not requested_path:
        requested_path = _clean_text(payload.get("brand_registration_mode"), lowercase=True)
    requested_path = requested_path or "low_volume_standard"

    candidate_business_type = _canonical_business_type(payload.get("business_type"))
    if candidate_business_type == "Sole Proprietor" and not has_business_tax_id:
        return "sole_proprietor"
    if requested_path == "sole_proprietor" and has_business_tax_id:
        return "low_volume_standard"
    if requested_path not in A2P_REGISTRATION_PATH_VALUES:
        raise ProviderProvisioningError("Choose a valid Twilio A2P registration path.")
    return requested_path


def _resolve_submission_source(
    *,
    organization: Organization,
    has_public_website: bool,
    external_urls: dict[str, str | None],
) -> tuple[str, str, dict[str, str], dict[str, Any]]:
    hosted_urls = hosted_a2p_compliance_urls(organization)
    if not has_public_website:
        validation = {
            "overall_valid": False,
            "fields": {},
        }
        return (
            "hosted_fallback",
            "Hosted fallback selected because the organization did not provide a public website.",
            hosted_urls,
            validation,
        )

    external_valid, validation_payload, reason = _validate_external_submission_urls(external_urls=external_urls)
    if external_valid:
        return "external_site", reason or "Using tenant-provided website URLs.", {
            "website_url": validation_payload["fields"]["website_url"]["url"] or "",
            "privacy_policy_url": validation_payload["fields"]["privacy_policy_url"]["url"] or "",
            "terms_and_conditions_url": validation_payload["fields"]["terms_and_conditions_url"]["url"] or "",
            "cta_proof_url": validation_payload["fields"]["cta_proof_url"]["url"] or "",
        }, validation_payload
    return "hosted_fallback", reason or "Hosted fallback selected because external website validation failed.", hosted_urls, validation_payload


def _normalized_multiline_list(raw_value: str | None) -> list[str]:
    value = _clean_text(raw_value) or ""
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _coerce_bool(raw_value: Any) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


def _payload_text(
    payload: dict[str, Any],
    *keys: str,
    existing: str | None = None,
    lowercase: bool = False,
) -> str | None:
    for key in keys:
        if key in payload:
            return _clean_text(payload.get(key), lowercase=lowercase)
    return _clean_text(existing, lowercase=lowercase)


def _country_code(raw_value: str | None) -> str | None:
    candidate = _clean_text(raw_value, lowercase=True)
    if not candidate:
        return None
    aliases = {
        "us": "US",
        "usa": "US",
        "united states": "US",
        "united states of america": "US",
        "ca": "CA",
        "canada": "CA",
    }
    if candidate in aliases:
        return aliases[candidate]
    if len(candidate) == 2 and candidate.isalpha():
        return candidate.upper()
    return None


def _normalize_address_value(raw_value: str | None) -> str | None:
    value = _clean_text(raw_value)
    return value[:255] if value else None


def _normalize_business_industry(raw_value: str | None) -> str | None:
    candidate = (_clean_text(raw_value, lowercase=True) or "").replace("-", "_").replace(" ", "_").upper()
    if not candidate:
        raise ProviderProvisioningError("Business industry is required.")
    if candidate in A2P_ALLOWED_BUSINESS_INDUSTRIES:
        return candidate
    raise ProviderProvisioningError("Choose a valid business industry for Twilio A2P onboarding.")


def _normalize_business_regions(raw_value: Any) -> list[str]:
    if isinstance(raw_value, list):
        values = raw_value
    else:
        value = _clean_text(raw_value) or ""
        if not value:
            raise ProviderProvisioningError("Choose at least one business region of operation.")
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, list):
            values = parsed
        else:
            values = [part.strip() for part in re.split(r"[,\n]", value) if part.strip()]

    normalized: list[str] = []
    for item in values:
        candidate = str(item or "").strip().upper().replace(" ", "_")
        if candidate == "USA_CANADA":
            candidate = "USA_AND_CANADA"
        if candidate == "LATINAMERICA":
            candidate = "LATIN_AMERICA"
        if candidate in A2P_ALLOWED_BUSINESS_REGIONS and candidate not in normalized:
            normalized.append(candidate)
    if not normalized:
        raise ProviderProvisioningError("Choose at least one business region of operation.")
    return normalized


def _normalize_job_position(raw_value: str | None) -> str | None:
    value = _clean_text(raw_value)
    if not value:
        raise ProviderProvisioningError("Job position is required for the authorized representative.")
    if value in A2P_ALLOWED_JOB_POSITIONS:
        return value
    raise ProviderProvisioningError("Choose a valid job position for the authorized representative.")


def _normalize_business_title(raw_value: str | None, *, fallback: str | None = None) -> str | None:
    value = _clean_text(raw_value) or _clean_text(fallback)
    if not value:
        raise ProviderProvisioningError("Business title is required for the authorized representative.")
    return value[:120]


def _normalize_notification_email(raw_value: str | None, *, fallback: str | None = None) -> str:
    candidate = _clean_text(raw_value, lowercase=True) or _clean_text(fallback, lowercase=True) or ""
    if not candidate or "@" not in candidate:
        raise ProviderProvisioningError("Notification email is required.")
    return candidate


def _normalize_registration_identifier(raw_value: str | None) -> str | None:
    value = _clean_text(raw_value)
    if not value:
        return None
    if "ein" in value.lower():
        return "EIN"
    return value


def _normalize_business_registration_number(identifier: str | None, raw_value: str | None) -> str | None:
    value = _clean_text(raw_value)
    if not value:
        return None
    if identifier == "EIN":
        digits = re.sub(r"\D+", "", value)
        if len(digits) != 9:
            raise ProviderProvisioningError("Enter a valid EIN using 9 digits.")
        return f"{digits[:2]}-{digits[2:]}"
    return value


def _twilio_business_registration_number(identifier: str | None, raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    return raw_value


def _require_address(
    *,
    country: str | None,
    line1: str | None,
    city: str | None,
    region: str | None,
    postal_code: str | None,
) -> tuple[str, str, str, str, str]:
    if not country:
        raise ProviderProvisioningError("Business country is required.")
    if not line1:
        raise ProviderProvisioningError("Address line 1 is required.")
    if not city:
        raise ProviderProvisioningError("Business city is required.")
    if not region:
        raise ProviderProvisioningError("Business state or province is required.")
    if not postal_code:
        raise ProviderProvisioningError("Business postal code is required.")
    return country, line1, city, region, postal_code


def _normalize_business_type_key(raw_value: str | None) -> str | None:
    candidate = _clean_text(raw_value, lowercase=True)
    if not candidate:
        return None
    return re.sub(r"[^a-z0-9]+", " ", candidate).strip() or None


def _canonical_business_type(raw_value: str | None) -> str | None:
    key = _normalize_business_type_key(raw_value)
    if not key:
        return None
    return A2P_BUSINESS_TYPE_ALIASES.get(key)


def _default_campaign_use_case(registration_path: str) -> str:
    if registration_path == "sole_proprietor":
        return "SOLE_PROPRIETOR"
    return DEFAULT_A2P_CAMPAIGN_USE_CASE


def _normalize_use_case(registration_path: str, raw_value: str | None) -> str:
    candidate = (raw_value or "").strip().upper() or _default_campaign_use_case(registration_path)
    allowed = {value for value, _ in A2P_CAMPAIGN_USE_CASES}
    if registration_path == "sole_proprietor":
        return "SOLE_PROPRIETOR"
    if candidate not in allowed:
        return _default_campaign_use_case(registration_path)
    if candidate == "SOLE_PROPRIETOR":
        return _default_campaign_use_case(registration_path)
    return candidate


def _validate_message_samples(campaign_use_case: str, message_samples: list[str]) -> list[str]:
    if len(message_samples) < 2:
        raise ProviderProvisioningError("Provide at least two real message samples for carrier review.")
    return message_samples


def _normalize_business_type(registration_path: str, raw_value: str | None) -> str | None:
    if registration_path == "sole_proprietor":
        return "Sole Proprietor"
    if registration_path == "nonprofit":
        return "Non-profit Corporation"
    if registration_path == "government":
        return "Non-profit Corporation"
    candidate = _canonical_business_type(raw_value)
    if candidate == "Sole Proprietor":
        raise ProviderProvisioningError("Sole proprietor business type is only valid for sole proprietor A2P onboarding.")
    if candidate in A2P_ALLOWED_BUSINESS_TYPES:
        return candidate
    if _clean_text(raw_value):
        raise ProviderProvisioningError("Choose a valid Twilio legal business type for A2P onboarding.")
    raise ProviderProvisioningError("Business type is required for standard or low-volume A2P onboarding.")


def _business_identity(registration_path: str) -> str:
    return "direct_customer"

def _messaging_profile_company_type(registration_path: str) -> str:
    if registration_path == "government":
        return "government"
    if registration_path == "nonprofit":
        return "non_profit"
    return "private"


def _requires_business_registration_details(registration_path: str) -> bool:
    return registration_path != "sole_proprietor"


def _validate_business_registration_details(
    registration_path: str,
    identifier: str | None,
    number: str | None,
) -> tuple[str | None, str | None]:
    if not _requires_business_registration_details(registration_path):
        return identifier, number
    if not identifier:
        raise ProviderProvisioningError(
            "Business registration identifier is required for non-sole-proprietor A2P onboarding."
        )
    if not number:
        raise ProviderProvisioningError(
            "Business registration number is required for non-sole-proprietor A2P onboarding."
        )
    return identifier, number


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


def _load_provisioning_state(onboarding: OrganizationA2POnboarding) -> dict[str, Any]:
    raw_value = (onboarding.provisioning_state_json or "").strip()
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _checkpoint_a2p_provisioning(
    onboarding: OrganizationA2POnboarding,
    status_payload: dict[str, Any],
    phase: str,
) -> None:
    resources = {
        "address_sid": onboarding.address_sid,
        "supporting_document_sid": onboarding.supporting_document_sid,
        "customer_profile_sid": onboarding.customer_profile_sid,
        "trust_product_sid": onboarding.trust_product_sid,
        "brand_registration_sid": onboarding.brand_registration_sid,
        "sole_proprietor_end_user_sid": status_payload.get("sole_proprietor_end_user_sid"),
        "business_information_end_user_sid": status_payload.get("business_information_end_user_sid"),
        "authorized_representative_sid": status_payload.get("authorized_representative_sid"),
        "messaging_profile_end_user_sid": status_payload.get("messaging_profile_end_user_sid"),
    }
    state = _load_provisioning_state(onboarding)
    state.update(
        {
            "version": 1,
            "flow": "a2p_registration",
            "phase": phase,
            "resources": {key: value for key, value in resources.items() if value},
            "updated_at": utc_now().isoformat(),
        }
    )
    onboarding.provisioning_state_json = json.dumps(state, sort_keys=True)
    _store_status_payload(onboarding, status_payload)
    db.session.commit()


def _single_remote_resource(resources: list[Any], label: str) -> Any | None:
    if len(resources) > 1:
        raise ProviderProvisioningError(
            f"Multiple Twilio {label} resources match this onboarding record; reconcile them before continuing."
        )
    return resources[0] if resources else None


def _resource_text(resource: Any, attribute: str) -> str:
    return str(getattr(resource, attribute, "") or "").strip()


def _required_remote_sid(resource: Any, label: str) -> str:
    sid = _resource_text(resource, "sid")
    if not sid:
        raise ProviderProvisioningError(f"Twilio returned a {label} without a SID.")
    return sid


def _find_or_create_end_user(
    client: Any,
    friendly_name: str,
    type_name: str,
    attributes: dict[str, Any],
    label: str,
) -> Any:
    matches = [
        end_user
        for end_user in client.trusthub.v1.end_users.list(limit=100)
        if _resource_text(end_user, "friendly_name") == friendly_name
        and _resource_text(end_user, "type") == type_name
    ]
    existing = _single_remote_resource(matches, label)
    if existing is not None:
        return existing
    return _create_end_user(
        client,
        friendly_name=friendly_name,
        type_name=type_name,
        attributes=attributes,
    )


def _ensure_remote_assignment(assignment_context: Any, object_sid: str, label: str) -> None:
    matches = [
        assignment
        for assignment in assignment_context.list(limit=100)
        if _resource_text(assignment, "object_sid") == object_sid
    ]
    _single_remote_resource(matches, label)
    if not matches:
        assignment_context.create(object_sid=object_sid)


def _existing_evaluation(evaluation_context: Any, policy_sid: str, label: str) -> Any | None:
    matches = [
        evaluation
        for evaluation in evaluation_context.list(limit=100)
        if _resource_text(evaluation, "policy_sid") == policy_sid
    ]
    return _single_remote_resource(matches, label)


def _status_value(raw_value: Any) -> str | None:
    normalized = str(raw_value or "").strip()
    return normalized.lower() or None


def _sender_activation_ready(profile: OrganizationMessagingProfile) -> bool:
    emergency_ready = profile.emergency_address_status in {"synced", "not_required"} or (
        profile.effective_sender_finalization_status == "active"
    )
    return bool(
        profile.from_number
        and profile.phone_number_sid
        and profile.sender_review_status == "approved"
        and profile.consent_acknowledged_at is not None
        and emergency_ready
    )


def _friendly_provider_error_message(raw_message: str) -> str:
    message = (raw_message or "").strip()
    if _looks_like_transient_provider_error(message):
        return (
            "Twilio could not be reached during the latest status sync. Preserve the current approved state, "
            "then retry the refresh after connectivity stabilizes."
        )
    if "Secondary Customer Profile for direct_customer can only be created through Twilio console." in message:
        return (
            "Twilio rejected automated secondary profile creation because the parent account is still set up as a "
            "Direct Customer profile. Reclassify the primary Twilio Customer Profile to ISV Reseller or Partner "
            "in Twilio Trust Hub or through Twilio Support, then retry onboarding."
        )
    return message


def _twilio_rest_error_code(exc: TwilioRestException) -> str | None:
    for attribute in ("code", "error_code"):
        value = getattr(exc, attribute, None)
        normalized = _clean_text(value)
        if normalized:
            return normalized
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        normalized = _clean_text(details.get("code"))
        if normalized:
            return normalized
    return None


def _is_twilio_not_found_error(exc: TwilioRestException) -> bool:
    if getattr(exc, "status", None) == 404:
        return True
    if _twilio_rest_error_code(exc) == "20404":
        return True
    message = (_clean_text(getattr(exc, "msg", None)) or _clean_text(str(exc)) or "").lower()
    return "not found" in message


def _looks_like_transient_provider_error(raw_message: str | None) -> bool:
    message = (raw_message or "").strip().lower()
    return any(fragment in message for fragment in A2P_TRANSIENT_PROVIDER_ERROR_FRAGMENTS)


def _is_transient_provider_exception(exc: Exception) -> bool:
    return _looks_like_transient_provider_error(str(exc))


def _service_summary(service: Any) -> dict[str, Any]:
    return {
        "sid": _clean_text(getattr(service, "sid", None)),
        "friendly_name": _clean_text(getattr(service, "friendly_name", None)),
        "status": _status_value(getattr(service, "status", None)),
    }


def _customer_profile_summary(customer_profile: Any) -> dict[str, Any]:
    return {
        "sid": _clean_text(getattr(customer_profile, "sid", None)),
        "friendly_name": _clean_text(getattr(customer_profile, "friendly_name", None)),
        "status": _status_value(getattr(customer_profile, "status", None)),
    }


def _trust_product_summary(trust_product: Any) -> dict[str, Any]:
    return {
        "sid": _clean_text(getattr(trust_product, "sid", None)),
        "friendly_name": _clean_text(getattr(trust_product, "friendly_name", None)),
        "status": _status_value(getattr(trust_product, "status", None)),
    }


def _brand_summary(brand: Any) -> dict[str, Any]:
    return {
        "sid": _clean_text(getattr(brand, "sid", None)),
        "status": _status_value(getattr(brand, "status", None)),
        "identity_status": _status_value(getattr(brand, "identity_status", None)),
        "tcr_id": _clean_text(getattr(brand, "tcr_id", None)),
    }


def _matching_item(items: list[dict[str, Any]], sid: str | None) -> dict[str, Any] | None:
    if not sid:
        return None
    normalized_sid = sid.strip()
    for item in items:
        if item.get("sid") == normalized_sid:
            return item
    return None


def _preferred_item(
    items: list[dict[str, Any]],
    *,
    current_sid: str | None = None,
    approved_statuses: set[str] | None = None,
    reviewing_statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    current_match = _matching_item(items, current_sid)
    if current_match is not None:
        return current_match

    approved_statuses = approved_statuses or set()
    reviewing_statuses = reviewing_statuses or set()
    for item in items:
        status = _status_value(item.get("status")) or _status_value(item.get("identity_status"))
        if status in approved_statuses:
            return item
    for item in items:
        status = _status_value(item.get("status")) or _status_value(item.get("identity_status"))
        if status in reviewing_statuses:
            return item
    return items[0] if items else None


def _inventory_subaccount_resources(profile: OrganizationMessagingProfile) -> dict[str, Any]:
    client, read_context = _build_subaccount_client_context(
        profile,
        require_stored_auth_token=True,
    )
    services: list[dict[str, Any]] = []
    for service in client.messaging.v1.services.list(limit=20):
        summary = _service_summary(service)
        campaigns = [
            _existing_campaign_summary(campaign)
            for campaign in client.messaging.v1.services(summary["sid"]).us_app_to_person.list(limit=20)
        ] if summary.get("sid") else []
        summary["campaigns"] = campaigns
        summary["campaign_count"] = len(campaigns)
        services.append(summary)

    customer_profiles = [
        _customer_profile_summary(customer_profile)
        for customer_profile in client.trusthub.v1.customer_profiles.list(limit=20)
    ]
    trust_products = [
        _trust_product_summary(trust_product)
        for trust_product in client.trusthub.v1.trust_products.list(limit=20)
    ]
    brands = [
        _brand_summary(brand)
        for brand in client.messaging.v1.brand_registrations.list(limit=20)
    ]
    return {
        "subaccount_sid": profile.twilio_subaccount_sid,
        "read_context": read_context,
        "services": services,
        "customer_profiles": customer_profiles,
        "trust_products": trust_products,
        "brands": brands,
    }


def _stored_a2p_identifiers(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
) -> dict[str, Any]:
    return {
        "subaccount_sid": profile.twilio_subaccount_sid,
        "messaging_service_sid": profile.messaging_service_sid,
        "customer_profile_sid": onboarding.customer_profile_sid,
        "trust_product_sid": onboarding.trust_product_sid,
        "brand_registration_sid": onboarding.brand_registration_sid,
        "campaign_sid": onboarding.campaign_sid,
        "phone_number_sid": profile.phone_number_sid,
    }


def _resource_sid_options(items: list[dict[str, Any]]) -> set[str]:
    return {item.get("sid") for item in items if item.get("sid")}


def _console_campaign_id(value: Any) -> str | None:
    return _clean_text(value)


def _twilio_read_context(onboarding: OrganizationA2POnboarding) -> dict[str, Any]:
    status_payload = _load_status_payload(onboarding)
    read_context = status_payload.get("twilio_read_context")
    return read_context if isinstance(read_context, dict) else {}


def _store_twilio_read_context(
    onboarding: OrganizationA2POnboarding,
    *,
    read_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status_payload = _load_status_payload(onboarding)
    if read_context:
        status_payload["twilio_read_context"] = read_context
    else:
        status_payload.pop("twilio_read_context", None)
    return status_payload


def _store_console_campaign_id(
    onboarding: OrganizationA2POnboarding,
    console_campaign_id: str | None,
) -> dict[str, Any]:
    status_payload = _load_status_payload(onboarding)
    normalized_console_campaign_id = _console_campaign_id(console_campaign_id)
    if normalized_console_campaign_id:
        status_payload["console_campaign_id"] = normalized_console_campaign_id
    else:
        status_payload.pop("console_campaign_id", None)
    return status_payload


def _recovery_state(onboarding: OrganizationA2POnboarding) -> dict[str, Any]:
    status_payload = _load_status_payload(onboarding)
    recovery_state = status_payload.get(A2P_RECOVERY_STATE_KEY)
    return recovery_state if isinstance(recovery_state, dict) else {}


def _set_recovery_state(onboarding: OrganizationA2POnboarding, recovery_state: dict[str, Any]) -> None:
    status_payload = _load_status_payload(onboarding)
    status_payload[A2P_RECOVERY_STATE_KEY] = recovery_state
    _store_status_payload(onboarding, status_payload)


def _clear_recovery_state(onboarding: OrganizationA2POnboarding) -> None:
    status_payload = _load_status_payload(onboarding)
    if A2P_RECOVERY_STATE_KEY in status_payload:
        status_payload.pop(A2P_RECOVERY_STATE_KEY, None)
        _store_status_payload(onboarding, status_payload)


def _recovery_state_metadata(recovery_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "recovery_type": recovery_state.get("type"),
        "recommended_action": recovery_state.get("recommended_action"),
        "stored": recovery_state.get("stored"),
        "selected": recovery_state.get("selected"),
    }


def _build_recovery_state(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    recovery_type: str,
    inventory: dict[str, Any],
    summary: str,
    observed_ids: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stored = _stored_a2p_identifiers(onboarding, profile)
    selected_service = _preferred_item(
        inventory.get("services", []),
        current_sid=profile.messaging_service_sid,
    )
    selected_customer_profile = _preferred_item(
        inventory.get("customer_profiles", []),
        current_sid=onboarding.customer_profile_sid,
        approved_statuses=A2P_RECONCILED_PROFILE_APPROVED_STATUSES,
        reviewing_statuses=A2P_REVIEWING_STATUSES,
    )
    selected_trust_product = _preferred_item(
        inventory.get("trust_products", []),
        current_sid=onboarding.trust_product_sid,
        approved_statuses=A2P_RECONCILED_TRUST_PRODUCT_APPROVED_STATUSES,
        reviewing_statuses=A2P_REVIEWING_STATUSES,
    )
    selected_brand = _preferred_item(
        inventory.get("brands", []),
        current_sid=onboarding.brand_registration_sid,
        approved_statuses=A2P_BRAND_READY_FOR_CAMPAIGN_STATUSES,
        reviewing_statuses=A2P_REVIEWING_STATUSES,
    )
    selected_campaign = _preferred_item(
        list(selected_service.get("campaigns", [])) if selected_service is not None else [],
        current_sid=onboarding.campaign_sid,
        approved_statuses=A2P_CAMPAIGN_APPROVED_STATUSES,
        reviewing_statuses=A2P_REVIEWING_STATUSES,
    )

    missing = {
        "messaging_service_sid": bool(profile.messaging_service_sid) and profile.messaging_service_sid not in _resource_sid_options(inventory.get("services", [])),
        "customer_profile_sid": bool(onboarding.customer_profile_sid) and onboarding.customer_profile_sid not in _resource_sid_options(inventory.get("customer_profiles", [])),
        "trust_product_sid": bool(onboarding.trust_product_sid) and onboarding.trust_product_sid not in _resource_sid_options(inventory.get("trust_products", [])),
        "brand_registration_sid": bool(onboarding.brand_registration_sid) and onboarding.brand_registration_sid not in _resource_sid_options(inventory.get("brands", [])),
        "campaign_sid": bool(onboarding.campaign_sid) and onboarding.campaign_sid not in _resource_sid_options(list(selected_service.get("campaigns", [])) if selected_service is not None else []),
    }
    only_missing_campaign = bool(
        selected_service
        and selected_customer_profile
        and selected_trust_product
        and selected_brand
        and not selected_campaign
    )
    recommended_action = "create_campaign" if recovery_type == "missing_campaign" or only_missing_campaign else "reconcile"

    return {
        "type": recovery_type,
        "recommended_action": recommended_action,
        "summary": summary,
        "detected_at": utc_now().isoformat(),
        "stored": stored,
        "live": inventory,
        "selected": {
            "messaging_service_sid": selected_service.get("sid") if selected_service else None,
            "customer_profile_sid": selected_customer_profile.get("sid") if selected_customer_profile else None,
            "trust_product_sid": selected_trust_product.get("sid") if selected_trust_product else None,
            "brand_registration_sid": selected_brand.get("sid") if selected_brand else None,
            "campaign_sid": selected_campaign.get("sid") if selected_campaign else None,
        },
        "missing": missing,
        "only_missing_campaign": only_missing_campaign,
        "observed_ids": observed_ids or {},
    }


def _humanize_status(value: str | None, *, fallback: str) -> str:
    normalized = _status_value(value)
    if not normalized:
        return fallback
    return normalized.replace("_", " ").replace("-", " ")


def _failure_details_from_errors(errors: Any) -> tuple[str | None, str | None]:
    if not isinstance(errors, list):
        return None, None
    for item in errors:
        if not isinstance(item, dict):
            continue
        description = (
            _clean_text(item.get("registrationerrordescription"))
            or _clean_text(item.get("description"))
            or _clean_text(item.get("message"))
        )
        code = (
            _clean_text(item.get("registrationerrorcode"))
            or _clean_text(item.get("error_code"))
            or _clean_text(item.get("code"))
        )
        if description or code:
            return description, code
    return None, None


def _campaign_use_case_value(raw_value: Any) -> str | None:
    value = _clean_text(raw_value)
    return value.upper() if value else None


def _existing_campaign_summary(campaign: Any) -> dict[str, Any]:
    failure_reason, failure_code = _failure_details_from_errors(getattr(campaign, "errors", None))
    if not failure_reason:
        failure_reason = _clean_text(getattr(campaign, "failure_reason", None))
    console_campaign_id = (
        _console_campaign_id(getattr(campaign, "campaign_id", None))
        or _console_campaign_id(getattr(campaign, "external_campaign_id", None))
    )
    return {
        "sid": _clean_text(getattr(campaign, "sid", None)),
        "status": _status_value(getattr(campaign, "campaign_status", None) or getattr(campaign, "status", None)),
        "brand_registration_sid": _clean_text(getattr(campaign, "brand_registration_sid", None)),
        "use_case": _campaign_use_case_value(
            getattr(campaign, "us_app_to_person_usecase", None) or getattr(campaign, "campaign_usecase", None)
        ),
        "console_campaign_id": console_campaign_id,
        "failure_reason": failure_reason,
        "failure_code": failure_code,
        "errors": getattr(campaign, "errors", None),
    }


def _store_existing_campaign_snapshot(
    onboarding: OrganizationA2POnboarding,
    summary: dict[str, Any],
    *,
    preserve_failure: bool,
) -> None:
    status_payload = _load_status_payload(onboarding)
    status_payload["campaign_status"] = summary.get("status")
    status_payload["campaign_use_case"] = summary.get("use_case")
    if summary.get("console_campaign_id"):
        status_payload["console_campaign_id"] = summary.get("console_campaign_id")
    status_payload["campaign_errors"] = summary.get("errors")
    if preserve_failure:
        status_payload["campaign_failure_reason"] = summary.get("failure_reason")
        status_payload["campaign_failure_code"] = summary.get("failure_code")
    else:
        status_payload.pop("campaign_failure_reason", None)
        status_payload.pop("campaign_failure_code", None)
        status_payload.pop("campaign_errors", None)
    _store_status_payload(onboarding, status_payload)
    onboarding.campaign_sid = summary.get("sid")
    onboarding.campaign_status = summary.get("status")
    if preserve_failure:
        onboarding.failure_code = summary.get("failure_code")


def _delete_campaign_context(campaign_context: Any) -> None:
    delete_method = getattr(campaign_context, "delete", None) or getattr(campaign_context, "remove", None)
    if delete_method is None:
        raise ProviderProvisioningError("Twilio SDK does not expose campaign deletion for this Messaging Service.")
    delete_method()


def _conflicting_campaign_requires_recreation(
    onboarding: OrganizationA2POnboarding,
    summary: dict[str, Any],
) -> tuple[bool, str | None]:
    status = summary.get("status")
    if status not in A2P_CAMPAIGN_FAILURE_STATUSES:
        return False, None

    desired_use_case = _normalize_use_case(onboarding.registration_path, onboarding.campaign_use_case)
    existing_use_case = summary.get("use_case")
    if existing_use_case and existing_use_case != desired_use_case:
        return True, (
            "Twilio requires a new campaign when the Messaging Service still has a failed campaign attached "
            "for a different use case."
        )

    brand_status = _status_value(onboarding.brand_status)
    if brand_status in A2P_BRAND_FAILURE_STATUSES:
        return True, (
            "Twilio requires deleting the failed campaign association before recreating it after brand-side corrections."
        )

    if summary.get("brand_registration_sid") and onboarding.brand_registration_sid:
        if summary["brand_registration_sid"] != onboarding.brand_registration_sid:
            return True, (
                "Twilio requires a new campaign when the failed campaign is tied to a different brand registration."
            )

    return False, None


def _prepare_service_for_campaign_create(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    service_context: Any,
    *,
    actor_user_id: int | None = None,
) -> bool:
    existing_campaigns = list(service_context.us_app_to_person.list(limit=20))
    if not existing_campaigns:
        return False

    summaries = [_existing_campaign_summary(campaign) for campaign in existing_campaigns]
    attached_campaign = next(
        (
            summary
            for summary in summaries
            if summary.get("status") in A2P_CAMPAIGN_APPROVED_STATUSES or summary.get("status") in A2P_REVIEWING_STATUSES
        ),
        None,
    ) or next(
        (summary for summary in summaries if summary.get("status") in A2P_CAMPAIGN_FAILURE_STATUSES),
        None,
    ) or next(
        (summary for summary in summaries if summary.get("status") != "deleted"),
        None,
    ) or summaries[0]
    
    _store_existing_campaign_snapshot(
        onboarding,
        attached_campaign,
        preserve_failure=attached_campaign.get("status") in A2P_CAMPAIGN_FAILURE_STATUSES,
    )

    status = attached_campaign.get("status")
    if status == "deleted":
        time.sleep(A2P_CAMPAIGN_RECREATE_DELAY_SECONDS)
        return True

    if status in A2P_CAMPAIGN_APPROVED_STATUSES or status in A2P_REVIEWING_STATUSES:
        raise ProviderProvisioningError(
            "Twilio already has an active A2P campaign attached to this Messaging Service. "
            "Wait for that campaign to finish review or approval before replacing it."
        )

    should_recreate, recreate_reason = _conflicting_campaign_requires_recreation(onboarding, attached_campaign)
    if not should_recreate:
        raise ProviderProvisioningError(
            "Twilio still has a failed A2P campaign attached to this Messaging Service. "
            "Because the failed campaign still matches the same use case and brand, the app will not auto-delete it. "
            "Use Twilio's campaign edit and retry flow instead of recreating the campaign, or change the packet in a "
            "way that requires a new campaign."
        )

    campaign_sid = attached_campaign.get("sid")
    if not campaign_sid:
        raise ProviderProvisioningError("Twilio reported an attached failed campaign without a campaign SID.")

    _delete_campaign_context(service_context.us_app_to_person(campaign_sid))
    status_payload = _load_status_payload(onboarding)
    status_payload["last_deleted_campaign"] = {
        "sid": campaign_sid,
        "status": status,
        "use_case": attached_campaign.get("use_case"),
        "deleted_at": utc_now().isoformat(),
        "reason": recreate_reason,
    }
    status_payload.pop("campaign_failure_reason", None)
    status_payload.pop("campaign_failure_code", None)
    status_payload.pop("campaign_errors", None)
    status_payload["campaign_status"] = None
    _store_status_payload(onboarding, status_payload)
    onboarding.campaign_sid = None
    onboarding.campaign_status = None
    onboarding.failure_code = None
    onboarding.last_error = None
    _record_provider_audit(
        onboarding.organization_id,
        "a2p_campaign_recreated",
        actor_user_id=actor_user_id,
        status="pending",
        message=f"Deleted failed Twilio campaign {campaign_sid} before recreating it.",
        metadata={
            "deleted_campaign_sid": campaign_sid,
            "deleted_campaign_status": status,
            "deleted_campaign_use_case": attached_campaign.get("use_case"),
            "reason": recreate_reason,
        },
    )
    time.sleep(A2P_CAMPAIGN_RECREATE_DELAY_SECONDS)
    return True


def _is_campaign_association_conflict(exc: TwilioRestException) -> bool:
    if getattr(exc, "status", None) != 409:
        return False
    message = _clean_text(getattr(exc, "msg", None)) or _clean_text(str(exc)) or ""
    return A2P_CAMPAIGN_ASSOCIATION_CONFLICT_FRAGMENT in message.lower()


def _latest_failure_message(onboarding: OrganizationA2POnboarding) -> str | None:
    status_payload = _load_status_payload(onboarding)
    return (
        _clean_text(status_payload.get("number_failure_reason"))
        or _clean_text(status_payload.get("campaign_failure_reason"))
        or _clean_text(status_payload.get("brand_failure_reason"))
        or _clean_text(onboarding.last_error)
    )


def _latest_failure_code(onboarding: OrganizationA2POnboarding) -> str | None:
    status_payload = _load_status_payload(onboarding)
    return (
        _clean_text(status_payload.get("number_failure_code"))
        or _clean_text(status_payload.get("campaign_failure_code"))
        or _clean_text(status_payload.get("brand_failure_code"))
        or _clean_text(onboarding.failure_code)
    )


def _latest_number_status(onboarding: OrganizationA2POnboarding) -> str | None:
    status_payload = _load_status_payload(onboarding)
    return _status_value(status_payload.get("number_status"))


def _a2p_audit_metadata(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
) -> dict[str, Any]:
    status_payload = _load_status_payload(onboarding)
    read_context = _twilio_read_context(onboarding)
    return {
        "brand_registration_sid": onboarding.brand_registration_sid,
        "campaign_sid": onboarding.campaign_sid,
        "campaign_use_case": onboarding.campaign_use_case,
        "console_campaign_id": _console_campaign_id(status_payload.get("console_campaign_id")),
        "customer_profile_sid": onboarding.customer_profile_sid,
        "failure_code": onboarding.failure_code,
        "messaging_service_sid": profile.messaging_service_sid,
        "phone_number_sid": profile.phone_number_sid,
        "provider_status": profile.provider_status,
        "submission_source_mode": onboarding.submission_source_mode,
        "trust_product_sid": onboarding.trust_product_sid,
        "twilio_read_account_sid": _clean_text(read_context.get("twilio_read_account_sid")),
        "twilio_subaccount_sid": _clean_text(read_context.get("twilio_subaccount_sid")) or profile.twilio_subaccount_sid,
        "used_subaccount_auth_token": read_context.get("used_subaccount_auth_token"),
    }


def _set_non_destructive_error_state(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    onboarding_status: str,
    brand_status: str | None,
    campaign_status: str | None,
    error_message: str | None,
) -> None:
    _set_status(
        onboarding,
        profile,
        onboarding_status=onboarding_status,
        brand_status=brand_status,
        campaign_status=campaign_status,
        verification_status=onboarding.verification_status,
        error_message=error_message,
        provider_status_on_error=None,
    )
    if profile.provider_status != "suspended" and not profile.can_send:
        profile.set_provider_status("pending")


def _record_recovery_detection(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    recovery_type: str,
    summary: str,
    recovery_state: dict[str, Any],
    actor_user_id: int | None = None,
) -> None:
    action = {
        "provider_drift": "a2p_drift_detected",
        "missing_campaign": "a2p_missing_campaign_detected",
        "transient_connectivity": "a2p_transient_provider_failure",
    }.get(recovery_type, "a2p_drift_detected")
    _record_provider_audit(
        onboarding.organization_id,
        action,
        actor_user_id=actor_user_id,
        status="pending" if recovery_type != "transient_connectivity" else "error",
        message=summary,
        metadata={
            **_a2p_audit_metadata(onboarding, profile),
            **_recovery_state_metadata(recovery_state),
        },
    )


def _mark_recovery_required(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    recovery_type: str,
    summary: str,
    inventory: dict[str, Any],
    brand_status: str | None = None,
    campaign_status: str | None = None,
    observed_ids: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
) -> None:
    recovery_state = _build_recovery_state(
        onboarding,
        profile,
        recovery_type=recovery_type,
        inventory=inventory,
        summary=summary,
        observed_ids=observed_ids,
    )
    _set_recovery_state(onboarding, recovery_state)
    _set_non_destructive_error_state(
        onboarding,
        profile,
        onboarding_status="needs_action",
        brand_status=brand_status or onboarding.brand_status,
        campaign_status=campaign_status or onboarding.campaign_status,
        error_message=summary,
    )
    _record_recovery_detection(
        onboarding,
        profile,
        recovery_type=recovery_type,
        summary=summary,
        recovery_state=recovery_state,
        actor_user_id=actor_user_id,
    )


def _mark_transient_connectivity_issue(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    summary: str,
    actor_user_id: int | None = None,
) -> None:
    if onboarding.onboarding_status == "processing":
        onboarding.onboarding_status = "pending" if onboarding.submitted_at else "queued"
    recovery_state = {
        "type": "transient_connectivity",
        "recommended_action": "refresh",
        "summary": summary,
        "detected_at": utc_now().isoformat(),
        "stored": _stored_a2p_identifiers(onboarding, profile),
        "live": {},
        "selected": {},
        "missing": {},
        "only_missing_campaign": False,
        "observed_ids": {},
    }
    _set_recovery_state(onboarding, recovery_state)
    onboarding.last_error = summary
    onboarding.last_synced_at = utc_now()
    profile.provider_last_checked_at = utc_now()
    profile.last_provision_error = summary
    _record_recovery_detection(
        onboarding,
        profile,
        recovery_type="transient_connectivity",
        summary=summary,
        recovery_state=recovery_state,
        actor_user_id=actor_user_id,
    )


def _mark_subaccount_auth_required_issue(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    summary: str,
    actor_user_id: int | None = None,
) -> None:
    _clear_recovery_state(onboarding)
    _set_non_destructive_error_state(
        onboarding,
        profile,
        onboarding_status="needs_action",
        brand_status=onboarding.brand_status,
        campaign_status=onboarding.campaign_status,
        error_message=summary,
    )
    _record_provider_audit(
        onboarding.organization_id,
        "a2p_subaccount_auth_required",
        actor_user_id=actor_user_id,
        status="error",
        message=summary,
        metadata=_a2p_audit_metadata(onboarding, profile),
    )


def _record_review_transition_audit(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    actor_user_id: int | None,
    previous_onboarding_status: str | None,
    previous_failure_code: str | None,
    previous_error_message: str | None,
) -> None:
    current_status = (onboarding.onboarding_status or "").strip().lower()
    if current_status == "approved" and previous_onboarding_status != "approved":
        _record_provider_audit(
            onboarding.organization_id,
            "a2p_review_approved",
            actor_user_id=actor_user_id,
            message="Twilio approved the A2P registration.",
            metadata=_a2p_audit_metadata(onboarding, profile),
        )
        return

    if current_status not in {"rejected", "needs_action"}:
        return

    current_failure_code = (onboarding.failure_code or "").strip() or None
    current_error_message = (onboarding.last_error or "").strip() or None
    should_record = (
        previous_onboarding_status not in {"rejected", "needs_action"}
        or current_failure_code != previous_failure_code
        or current_error_message != previous_error_message
    )
    if not should_record:
        return

    metadata = _a2p_audit_metadata(onboarding, profile)
    metadata["failure_reason"] = current_error_message
    _record_provider_audit(
        onboarding.organization_id,
        "a2p_review_rejected",
        actor_user_id=actor_user_id,
        status="error",
        message=current_error_message or "Twilio flagged the A2P registration for correction.",
        metadata=metadata,
    )


def _event_stream_state(onboarding: OrganizationA2POnboarding) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    status_payload = _load_status_payload(onboarding)
    event_stream = status_payload.get("event_stream")
    if not isinstance(event_stream, dict):
        event_stream = {}
        status_payload["event_stream"] = event_stream
    recent_event_ids = event_stream.get("recent_event_ids")
    if not isinstance(recent_event_ids, list):
        recent_event_ids = []
        event_stream["recent_event_ids"] = recent_event_ids
    return status_payload, event_stream, recent_event_ids


def _record_recent_event_id(recent_event_ids: list[str], event_id: str | None) -> None:
    if not event_id:
        return
    recent_event_ids.append(event_id)
    if len(recent_event_ids) > A2P_EVENT_STREAM_RECENT_EVENT_LIMIT:
        del recent_event_ids[:-A2P_EVENT_STREAM_RECENT_EVENT_LIMIT]


def _event_stream_topic(event_type: str) -> str | None:
    normalized = (event_type or "").strip().lower()
    if "brand-registration" in normalized:
        return "brand"
    if "campaign-registration" in normalized:
        return "campaign"
    if "number-registration" in normalized:
        return "number"
    return None


def _event_stream_status(topic: str, event_type: str, data: dict[str, Any]) -> str | None:
    if topic == "brand":
        status = _status_value(data.get("brandstatus"))
        if status:
            return status
        suffix = (event_type or "").rsplit(".", 1)[-1].replace("-", "_")
        brand_suffix_map = {
            "brand_failure": "registration_failed",
            "brand_registered": "registered",
            "brand_unverified": "unverified",
            "brand_verified": "verified",
            "secondary_vetting_failed": "secondary_vetting_failed",
            "brand_secondary_vetting_failure": "secondary_vetting_failed",
            "vetting_verified": "vetting_verified",
            "brand_vetted_verified": "vetting_verified",
        }
        return brand_suffix_map.get(suffix)
    if topic == "campaign":
        status = _status_value(data.get("campaignregistrationstatus"))
        if status:
            return status
        suffix = (event_type or "").rsplit(".", 1)[-1].replace("-", "_")
        campaign_suffix_map = {
            "campaign_approved": "approved",
            "campaign_deleted": "deleted",
            "campaign_failure": "failed",
            "campaign_submitted": "submitted",
        }
        return campaign_suffix_map.get(suffix)
    if topic == "number":
        status = _status_value(data.get("externalstatus"))
        if status:
            return status
        suffix = (event_type or "").rsplit(".", 1)[-1].replace("-", "_")
        number_suffix_map = {
            "failed": "failed",
            "pending": "pending",
            "successful": "successful",
        }
        return number_suffix_map.get(suffix)
    return None


def _event_stream_failure(topic: str, data: dict[str, Any]) -> tuple[str | None, str | None]:
    if topic == "brand":
        message, code = _failure_details_from_errors(data.get("brandregistrationerrors"))
        return message, code
    if topic == "campaign":
        message, code = _failure_details_from_errors(data.get("campaignregistrationerrors"))
        if not code:
            code = _clean_text(data.get("errorcode"))
        return message, code
    if topic == "number":
        return _clean_text(data.get("failurereason")), _clean_text(data.get("errorcode"))
    return None, None


def _event_stream_subscription_payload() -> list[dict[str, Any]]:
    return [{"type": event_type, "schema_version": 1} for event_type in A2P_EVENT_STREAM_SUBSCRIPTION_TYPES]


def ensure_a2p_event_stream_subscription(
    organization: Organization,
    profile: OrganizationMessagingProfile,
) -> None:
    if not a2p_event_streams_enabled():
        return
    require_chargeable_provider_entitlement(organization, "a2p_event_stream_subscription")

    destination = a2p_event_stream_destination_url(organization)
    if not destination:
        profile.event_stream_status = "error"
        profile.event_stream_error = "APP_BASE_URL must be configured before Twilio Event Streams can be enabled."
        return

    try:
        client = _client_for_profile(profile)
        desired_types = {item["type"] for item in _event_stream_subscription_payload()}
        sink = None

        if profile.event_stream_sink_sid:
            try:
                existing_sink = client.events.v1.sinks(profile.event_stream_sink_sid).fetch()
            except TwilioRestException:
                profile.event_stream_sink_sid = None
            else:
                sink_configuration = getattr(existing_sink, "sink_configuration", None) or {}
                existing_destination = _clean_text(sink_configuration.get("destination"))
                existing_sink_type = _status_value(getattr(existing_sink, "sink_type", None))
                if existing_sink_type != "webhook" or existing_destination != destination:
                    client.events.v1.sinks(existing_sink.sid).delete()
                    profile.event_stream_sink_sid = None
                    profile.event_stream_subscription_sid = None
                else:
                    sink = existing_sink

        if sink is None:
            sink = client.events.v1.sinks.create(
                description=f"A2P status sync for {organization.slug}",
                sink_configuration={
                    "destination": destination,
                    "method": "POST",
                },
                sink_type="webhook",
            )
            profile.event_stream_sink_sid = sink.sid

        subscription = None
        if profile.event_stream_subscription_sid:
            try:
                existing_subscription = client.events.v1.subscriptions(profile.event_stream_subscription_sid).fetch()
            except TwilioRestException:
                profile.event_stream_subscription_sid = None
            else:
                existing_types = {
                    _clean_text(item.type)
                    for item in client.events.v1.subscriptions(existing_subscription.sid).subscribed_events.list(limit=100)
                    if _clean_text(getattr(item, "type", None))
                }
                if existing_subscription.sink_sid != sink.sid or existing_types != desired_types:
                    client.events.v1.subscriptions(existing_subscription.sid).delete()
                    profile.event_stream_subscription_sid = None
                else:
                    subscription = existing_subscription

        if subscription is None:
            subscription = client.events.v1.subscriptions.create(
                description=f"A2P registration updates for {organization.slug}",
                sink_sid=sink.sid,
                types=_event_stream_subscription_payload(),
            )
            profile.event_stream_subscription_sid = subscription.sid

        profile.event_stream_status = _status_value(getattr(sink, "status", None)) or "configured"
        profile.event_stream_error = None
    except Exception as exc:
        profile.event_stream_status = "error"
        profile.event_stream_error = str(exc)
        current_app.logger.warning(
            "Could not configure Twilio Event Streams for organization_id=%s provider_mode=%s.",
            organization.id,
            profile.provider_mode,
            exc_info=True,
        )


def _event_stream_timestamp(data: dict[str, Any]) -> int:
    for key in ("updateddate", "timestamp", "createddate"):
        raw_value = data.get(key)
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, str) and raw_value.isdigit():
            return int(raw_value)
    return 0


def _event_value(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _clean_text(data.get(key))
        if value:
            return value
    return None


def _find_onboarding_by_subaccount_sid(account_sid: str | None) -> tuple[OrganizationA2POnboarding | None, OrganizationMessagingProfile | None]:
    if not account_sid:
        return None, None
    profile = OrganizationMessagingProfile.query.filter_by(twilio_subaccount_sid=account_sid).first()
    if profile is not None and profile.organization is not None:
        return profile.organization.a2p_onboarding, profile
    return None, None


def _find_onboarding_by_brand_tcr_id(
    brand_tcr_id: str | None,
    *,
    account_sid: str | None = None,
) -> tuple[OrganizationA2POnboarding | None, OrganizationMessagingProfile | None]:
    if not brand_tcr_id:
        return None, None

    onboardings = OrganizationA2POnboarding.query.filter(
        OrganizationA2POnboarding.raw_status_json.contains(brand_tcr_id)
    ).all()
    for onboarding in onboardings:
        organization = db.session.get(Organization, onboarding.organization_id)
        profile = organization.messaging_profile if organization is not None else None
        if profile is None:
            continue
        if account_sid and profile.twilio_subaccount_sid != account_sid:
            continue
        return onboarding, profile
    return None, None


def _record_observed_identifier_drift(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    observed_ids: dict[str, Any],
    actor_user_id: int | None = None,
) -> None:
    normalized_observed_ids = {key: value for key, value in observed_ids.items() if _clean_text(value)}
    if not normalized_observed_ids:
        return

    console_campaign_id = _console_campaign_id(normalized_observed_ids.get("console_campaign_id"))
    if console_campaign_id:
        status_payload = _store_console_campaign_id(onboarding, console_campaign_id)
        _store_status_payload(onboarding, status_payload)

    stored = _stored_a2p_identifiers(onboarding, profile)
    stored_brand_tcr_id = _clean_text(_load_status_payload(onboarding).get("brand_tcr_id"))
    differences = {}
    for key, value in normalized_observed_ids.items():
        if key == "console_campaign_id":
            continue
        if key == "brand_tcr_id":
            if value != stored_brand_tcr_id:
                differences[key] = value
            continue
        stored_value = stored.get(key)
        if stored_value and stored_value != value:
            differences[key] = value

    if not differences:
        return

    recovery_state = _recovery_state(onboarding)
    existing_observed_ids = recovery_state.get("observed_ids", {}) if recovery_state else {}
    merged_observed_ids = {**existing_observed_ids, **normalized_observed_ids}
    if existing_observed_ids == merged_observed_ids and recovery_state:
        return

    if not recovery_state:
        recovery_state = {
            "type": "provider_drift",
            "recommended_action": "reconcile",
            "summary": "Twilio callbacks reported provider identifiers that differ from the stored app state.",
            "detected_at": utc_now().isoformat(),
            "stored": stored,
            "live": {},
            "selected": {},
            "missing": {},
            "only_missing_campaign": False,
            "observed_ids": merged_observed_ids,
        }
    else:
        recovery_state["observed_ids"] = merged_observed_ids
        recovery_state["summary"] = (
            recovery_state.get("summary")
            or "Twilio callbacks reported provider identifiers that differ from the stored app state."
        )
        recovery_state["type"] = recovery_state.get("type") or "provider_drift"
        recovery_state["recommended_action"] = recovery_state.get("recommended_action") or "reconcile"
    _set_recovery_state(onboarding, recovery_state)
    _record_provider_audit(
        onboarding.organization_id,
        "a2p_drift_detected",
        actor_user_id=actor_user_id,
        status="pending",
        message="Twilio callbacks reported live identifiers that differ from the stored onboarding state.",
        metadata={
            **_a2p_audit_metadata(onboarding, profile),
            "observed_ids": merged_observed_ids,
        },
    )


def _event_identifier_belongs_to_other_organization(
    authenticated_organization_id: int,
    data: dict[str, Any],
) -> bool:
    identifier_queries = (
        (
            OrganizationA2POnboarding,
            OrganizationA2POnboarding.brand_registration_sid,
            _clean_text(data.get("brandsid")),
        ),
        (
            OrganizationA2POnboarding,
            OrganizationA2POnboarding.campaign_sid,
            _clean_text(data.get("campaignsid")),
        ),
        (
            OrganizationMessagingProfile,
            OrganizationMessagingProfile.phone_number_sid,
            _clean_text(data.get("phonenumbersid")),
        ),
        (
            OrganizationMessagingProfile,
            OrganizationMessagingProfile.messaging_service_sid,
            _event_value(data, "messagingservicesid", "messageservicesid", "service_sid"),
        ),
        (
            OrganizationMessagingProfile,
            OrganizationMessagingProfile.twilio_subaccount_sid,
            _event_value(data, "accountsid", "account_sid"),
        ),
    )
    for model, column, identifier in identifier_queries:
        if not identifier:
            continue
        record = model.query.filter(column == identifier).first()
        if record is not None and record.organization_id != authenticated_organization_id:
            return True
    return False


def _find_onboarding_for_event(
    event_type: str,
    data: dict[str, Any],
    authenticated_organization_id: int,
) -> tuple[OrganizationA2POnboarding | None, OrganizationMessagingProfile | None]:
    topic = _event_stream_topic(event_type)
    if topic is None:
        return None, None

    organization = db.session.get(Organization, authenticated_organization_id)
    if organization is None:
        return None, None
    profile = organization.messaging_profile
    onboarding = organization.a2p_onboarding
    if profile is None or onboarding is None:
        return None, None

    account_sid = _event_value(data, "accountsid", "account_sid")
    if account_sid and profile.twilio_subaccount_sid and account_sid != profile.twilio_subaccount_sid:
        return None, None
    if _event_identifier_belongs_to_other_organization(authenticated_organization_id, data):
        return None, None
    return onboarding, profile


def _validate_message_flow_requirements(message_flow: str) -> None:
    normalized = message_flow.lower()
    if len(message_flow) < 24:
        raise ProviderProvisioningError(
            "Message flow must explain how users opt in and what the messaging program is for."
        )
    if not any(token in normalized for token in ("opt in", "opt-in", "consent", "subscribe", "sign up")):
        raise ProviderProvisioningError(
            "Message flow must explain how the customer captures opt-in or consent before messaging."
        )
    if not any(token in normalized for token in ("stop", "unsubscribe")):
        raise ProviderProvisioningError(
            "Message flow must mention STOP or unsubscribe instructions."
        )


def _validate_custom_keyword_flow(
    *,
    message: str | None,
    keywords: list[str],
    label: str,
) -> None:
    uses_custom_flow = bool(message or keywords)
    if not uses_custom_flow:
        return
    if not message:
        raise ProviderProvisioningError(f"{label} message is required when you configure custom {label.lower()} keywords.")
    if not keywords:
        raise ProviderProvisioningError(f"{label} keywords are required when you configure a custom {label.lower()} message.")


def _validate_form_data(form_data: A2PFormData) -> None:
    if len(form_data.campaign_description) < 12 or len(form_data.campaign_description.split()) < 2:
        raise ProviderProvisioningError(
            "Campaign description must clearly describe the traffic in at least two words."
        )
    _validate_message_flow_requirements(form_data.message_flow)
    if not form_data.privacy_policy_url:
        raise ProviderProvisioningError("Privacy policy URL is required for A2P onboarding.")
    if not form_data.terms_and_conditions_url:
        raise ProviderProvisioningError("Terms and conditions URL is required for A2P onboarding.")
    if not form_data.cta_proof_url:
        raise ProviderProvisioningError("CTA proof URL is required for A2P onboarding.")
    _validate_custom_keyword_flow(
        message=form_data.opt_in_message,
        keywords=form_data.opt_in_keywords,
        label="Opt-in",
    )
    _validate_custom_keyword_flow(
        message=form_data.opt_out_message,
        keywords=form_data.opt_out_keywords,
        label="Opt-out",
    )
    _validate_custom_keyword_flow(
        message=form_data.help_message,
        keywords=form_data.help_keywords,
        label="Help",
    )

    if form_data.registration_path in {"nonprofit", "government"}:
        if not (form_data.website_url or form_data.social_profile_url):
            raise ProviderProvisioningError(
                "Provide a website URL or social profile URL for nonprofit or government onboarding."
            )
    elif not form_data.website_url:
        raise ProviderProvisioningError("Business website is required for A2P onboarding.")


def _apply_status_snapshot(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    actor_user_id: int | None = None,
    allow_number_setup: bool,
) -> None:
    previous_onboarding_status = (onboarding.onboarding_status or "").strip().lower() or None
    previous_failure_code = (onboarding.failure_code or "").strip() or None
    previous_error_message = (onboarding.last_error or "").strip() or None
    normalized_brand_status = _status_value(onboarding.brand_status) or "pending"
    normalized_campaign_status = _status_value(onboarding.campaign_status) or "pending"
    number_status = _latest_number_status(onboarding)
    failure_message = _latest_failure_message(onboarding)

    if normalized_brand_status in A2P_BRAND_FAILURE_STATUSES or normalized_campaign_status in A2P_CAMPAIGN_FAILURE_STATUSES:
        _set_status(
            onboarding,
            profile,
            onboarding_status="rejected",
            brand_status=normalized_brand_status,
            campaign_status=normalized_campaign_status,
            error_message=failure_message or "Twilio rejected the A2P registration.",
        )
        _record_review_transition_audit(
            onboarding,
            profile,
            actor_user_id=actor_user_id,
            previous_onboarding_status=previous_onboarding_status,
            previous_failure_code=previous_failure_code,
            previous_error_message=previous_error_message,
        )
        return

    if number_status in A2P_NUMBER_FAILURE_STATUSES:
        _set_status(
            onboarding,
            profile,
            onboarding_status="needs_action",
            brand_status=normalized_brand_status,
            campaign_status=normalized_campaign_status,
            verification_status=number_status,
            error_message=failure_message or "Twilio could not finish the phone number registration.",
        )
        _record_review_transition_audit(
            onboarding,
            profile,
            actor_user_id=actor_user_id,
            previous_onboarding_status=previous_onboarding_status,
            previous_failure_code=previous_failure_code,
            previous_error_message=previous_error_message,
        )
        return

    if normalized_brand_status in A2P_BRAND_APPROVED_STATUSES and normalized_campaign_status in A2P_CAMPAIGN_APPROVED_STATUSES:
        if onboarding.number_strategy == "platform_assign":
            _set_status(
                onboarding,
                profile,
                onboarding_status="approved",
                brand_status=normalized_brand_status,
                campaign_status=normalized_campaign_status,
                verification_status=number_status or onboarding.verification_status,
            )
            onboarding.approved_at = onboarding.approved_at or utc_now()
            if onboarding.brand_registration_mode == "standard":
                onboarding.upgraded_at = onboarding.upgraded_at or utc_now()
            onboarding.last_synced_at = utc_now()
            onboarding.last_error = None
            profile.sender_review_status = "pending"
            profile.last_provision_error = None
            if profile.provider_status != "suspended":
                profile.set_provider_status("pending")
            _record_review_transition_audit(
                onboarding,
                profile,
                actor_user_id=actor_user_id,
                previous_onboarding_status=previous_onboarding_status,
                previous_failure_code=previous_failure_code,
                previous_error_message=previous_error_message,
            )
            return

        if allow_number_setup:
            _complete_number_setup(onboarding, profile, actor_user_id)
            sender_ready = _sender_activation_ready(profile)
            onboarding.onboarding_status = "approved"
            onboarding.approved_at = onboarding.approved_at or utc_now()
            if onboarding.brand_registration_mode == "standard":
                onboarding.upgraded_at = onboarding.upgraded_at or utc_now()
            onboarding.last_synced_at = utc_now()
            onboarding.verification_status = number_status or onboarding.verification_status
            onboarding.last_error = None
            if profile.provider_status != "suspended":
                profile.set_provider_status("active" if sender_ready else "pending")
            if sender_ready:
                profile.last_provision_error = None
            _record_review_transition_audit(
                onboarding,
                profile,
                actor_user_id=actor_user_id,
                previous_onboarding_status=previous_onboarding_status,
                previous_failure_code=previous_failure_code,
                previous_error_message=previous_error_message,
            )
            return

        sender_ready = _sender_activation_ready(profile)
        _set_status(
            onboarding,
            profile,
            onboarding_status="approved" if sender_ready else "pending",
            brand_status=normalized_brand_status,
            campaign_status=normalized_campaign_status,
            verification_status=number_status or onboarding.verification_status,
        )
        if onboarding.brand_registration_mode == "standard" and onboarding.onboarding_status == "approved":
            onboarding.upgraded_at = onboarding.upgraded_at or utc_now()
        if profile.provider_status != "suspended":
            profile.set_provider_status("active" if sender_ready else "pending")
        _record_review_transition_audit(
            onboarding,
            profile,
            actor_user_id=actor_user_id,
            previous_onboarding_status=previous_onboarding_status,
            previous_failure_code=previous_failure_code,
            previous_error_message=previous_error_message,
        )
        return

    _set_status(
        onboarding,
        profile,
        onboarding_status="pending",
        brand_status=normalized_brand_status,
        campaign_status=normalized_campaign_status,
        verification_status=number_status or ("pending" if onboarding.registration_path == "sole_proprietor" else None),
    )
    if profile.provider_status != "suspended":
        profile.set_provider_status("pending")
    _record_review_transition_audit(
        onboarding,
        profile,
        actor_user_id=actor_user_id,
        previous_onboarding_status=previous_onboarding_status,
        previous_failure_code=previous_failure_code,
        previous_error_message=previous_error_message,
    )


def _build_form_data(payload: dict[str, Any], organization: Organization, *, require_declaration: bool) -> A2PFormData:
    existing_onboarding = organization.a2p_onboarding
    if "has_business_tax_id" in payload:
        has_business_tax_id = _coerce_bool(payload.get("has_business_tax_id"))
    elif existing_onboarding is not None and existing_onboarding.has_business_tax_id is not None:
        has_business_tax_id = bool(existing_onboarding.has_business_tax_id)
    else:
        has_business_tax_id = bool(
            _clean_text(payload.get("business_registration_number"))
            or _clean_text(payload.get("business_registration_identifier"))
        )

    registration_path = _determine_registration_path(payload, has_business_tax_id=has_business_tax_id)
    brand_registration_mode = _derive_brand_registration_mode(registration_path)
    number_strategy = (payload.get("number_strategy") or "auto_buy").strip().lower()
    if number_strategy not in A2P_NUMBER_STRATEGY_VALUES:
        raise ProviderProvisioningError("Choose a valid Twilio A2P number strategy.")
    legal_business_name = (
        _payload_text(
            payload,
            "legal_business_name",
            "business_name",
            existing=getattr(existing_onboarding, "legal_business_name", None)
            or getattr(existing_onboarding, "business_name", None),
        )
        or organization.name
        or ""
    )
    business_name = legal_business_name
    public_brand_name = (
        _payload_text(
            payload,
            "public_brand_name",
            existing=getattr(existing_onboarding, "public_brand_name", None),
        )
        or organization.name
        or business_name
    )
    email = _clean_text(payload.get("email"), lowercase=True) or ""
    notification_email = _normalize_notification_email(payload.get("notification_email"), fallback=email)
    first_name = _clean_text(payload.get("first_name")) or ""
    last_name = _clean_text(payload.get("last_name")) or ""
    job_position = _normalize_job_position(payload.get("job_position"))
    business_title = _normalize_business_title(payload.get("business_title"), fallback=job_position)
    campaign_description = _clean_text(payload.get("campaign_description")) or ""
    message_flow = _clean_text(payload.get("message_flow")) or ""
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

    if "has_public_website" in payload:
        has_public_website = _coerce_bool(payload.get("has_public_website"))
    elif existing_onboarding is not None and existing_onboarding.has_public_website is not None:
        has_public_website = bool(existing_onboarding.has_public_website)
    else:
        has_public_website = any(
            _clean_text(payload.get(key))
            for key in (
                "external_website_url",
                "external_privacy_policy_url",
                "external_terms_and_conditions_url",
                "external_cta_proof_url",
                "website_url",
                "privacy_policy_url",
                "terms_and_conditions_url",
                "cta_proof_url",
            )
        )

    external_website_url = _payload_text(
        payload,
        "external_website_url",
        "website_url",
        existing=getattr(existing_onboarding, "external_website_url", None),
    )
    external_privacy_policy_url = _payload_text(
        payload,
        "external_privacy_policy_url",
        "privacy_policy_url",
        existing=getattr(existing_onboarding, "external_privacy_policy_url", None),
    )
    external_terms_and_conditions_url = _payload_text(
        payload,
        "external_terms_and_conditions_url",
        "terms_and_conditions_url",
        existing=getattr(existing_onboarding, "external_terms_and_conditions_url", None),
    )
    external_cta_proof_url = _payload_text(
        payload,
        "external_cta_proof_url",
        "cta_proof_url",
        existing=getattr(existing_onboarding, "external_cta_proof_url", None),
    )
    submission_source_mode, submission_source_reason, active_urls, external_url_validation = _resolve_submission_source(
        organization=organization,
        has_public_website=has_public_website,
        external_urls={
            "website_url": external_website_url,
            "privacy_policy_url": external_privacy_policy_url,
            "terms_and_conditions_url": external_terms_and_conditions_url,
            "cta_proof_url": external_cta_proof_url,
        },
    )
    mobile_number = _clean_text(payload.get("mobile_number"))
    privacy_policy_url = active_urls.get("privacy_policy_url") or None
    terms_and_conditions_url = active_urls.get("terms_and_conditions_url") or None
    cta_proof_url = active_urls.get("cta_proof_url") or None
    website_url = active_urls.get("website_url") or cta_proof_url or None
    social_profile_url = _normalize_public_url(payload.get("social_profile_url"), field_label="Social profile URL")
    desired_phone_number_sid = _clean_text(payload.get("desired_phone_number_sid"))
    if registration_path == "sole_proprietor" and not mobile_number:
        raise ProviderProvisioningError("A mobile number is required for sole proprietor onboarding.")
    if number_strategy in {"existing_subaccount_number", "transfer_parent_number"} and not desired_phone_number_sid:
        raise ProviderProvisioningError("A Twilio phone number SID is required for the selected number strategy.")
    campaign_use_case = _normalize_use_case(registration_path, payload.get("campaign_use_case"))
    message_samples = _validate_message_samples(
        campaign_use_case,
        _normalized_multiline_list(payload.get("message_samples")),
    )
    business_type = _normalize_business_type(registration_path, payload.get("business_type"))
    business_industry = _normalize_business_industry(payload.get("business_industry"))
    business_regions = _normalize_business_regions(payload.get("business_regions"))
    address_country = _country_code(payload.get("address_country"))
    address_line1 = _normalize_address_value(payload.get("address_line1"))
    address_line2 = _normalize_address_value(payload.get("address_line2"))
    address_city = _normalize_address_value(payload.get("address_city"))
    address_region = _normalize_address_value(payload.get("address_region"))
    address_postal_code = _normalize_address_value(payload.get("address_postal_code"))
    _require_address(
        country=address_country,
        line1=address_line1,
        city=address_city,
        region=address_region,
        postal_code=address_postal_code,
    )
    business_registration_identifier, business_registration_number = _validate_business_registration_details(
        registration_path,
        _normalize_registration_identifier(payload.get("business_registration_identifier")),
        _normalize_business_registration_number(
            _normalize_registration_identifier(payload.get("business_registration_identifier")),
            payload.get("business_registration_number"),
        ),
    )
    if registration_path != "sole_proprietor" and not has_business_tax_id:
        raise ProviderProvisioningError(
            "Non-sole-proprietor onboarding requires a business EIN or tax ID. Use sole proprietor only for true sole proprietors without a business tax ID."
        )
    declaration_accepted = _coerce_bool(payload.get("declaration_accepted"))
    if require_declaration and not declaration_accepted:
        raise ProviderProvisioningError("You must confirm the business declaration before submitting A2P onboarding.")

    previous_brand_mode = _clean_text(
        getattr(existing_onboarding, "brand_registration_mode", None),
        lowercase=True,
    )
    upgrade_requested = brand_registration_mode == "standard" and previous_brand_mode == "low_volume_standard"
    upgrade_recommended_reason = (
        "Requested migration from low-volume standard to standard registration."
        if upgrade_requested
        else None
    )

    form_data = A2PFormData(
        registration_path=registration_path,
        number_strategy=number_strategy,
        business_name=business_name,
        legal_business_name=legal_business_name,
        public_brand_name=public_brand_name,
        business_type=business_type,
        business_industry=business_industry,
        has_business_tax_id=has_business_tax_id,
        brand_registration_mode=brand_registration_mode,
        business_regions=business_regions,
        has_public_website=has_public_website,
        submission_source_mode=submission_source_mode,
        submission_source_reason=submission_source_reason,
        external_website_url=external_website_url,
        external_privacy_policy_url=external_privacy_policy_url,
        external_terms_and_conditions_url=external_terms_and_conditions_url,
        external_cta_proof_url=external_cta_proof_url,
        external_url_validation=external_url_validation,
        website_url=website_url,
        social_profile_url=social_profile_url,
        privacy_policy_url=privacy_policy_url,
        terms_and_conditions_url=terms_and_conditions_url,
        cta_proof_url=cta_proof_url,
        email=email,
        notification_email=notification_email,
        phone_number=_clean_text(payload.get("phone_number")),
        mobile_number=mobile_number,
        first_name=first_name,
        last_name=last_name,
        business_title=business_title,
        job_position=job_position,
        business_registration_identifier=business_registration_identifier,
        business_registration_number=business_registration_number,
        address_country=address_country,
        address_line1=address_line1,
        address_line2=address_line2,
        address_city=address_city,
        address_region=address_region,
        address_postal_code=address_postal_code,
        campaign_use_case=campaign_use_case,
        campaign_description=campaign_description,
        message_flow=message_flow,
        message_samples=message_samples,
        opt_in_message=_clean_text(payload.get("opt_in_message")),
        opt_out_message=_clean_text(payload.get("opt_out_message")),
        help_message=_clean_text(payload.get("help_message")),
        opt_in_keywords=_normalized_csv_list(payload.get("opt_in_keywords")),
        opt_out_keywords=_normalized_csv_list(payload.get("opt_out_keywords")),
        help_keywords=_normalized_csv_list(payload.get("help_keywords")),
        has_embedded_links=_coerce_bool(payload.get("has_embedded_links")),
        has_embedded_phone=_coerce_bool(payload.get("has_embedded_phone")),
        desired_phone_number=_clean_text(payload.get("desired_phone_number")),
        desired_phone_number_sid=desired_phone_number_sid,
        campaign_verify_token=_clean_text(payload.get("campaign_verify_token")),
        upgrade_recommended_reason=upgrade_recommended_reason,
        upgrade_requested=upgrade_requested,
        declaration_accepted=declaration_accepted,
    )
    _validate_form_data(form_data)
    return form_data


def _save_form_data(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    form_data: A2PFormData,
    *,
    queue_submission: bool,
) -> None:
    onboarding.registration_path = form_data.registration_path
    onboarding.number_strategy = form_data.number_strategy
    onboarding.business_name = form_data.business_name
    onboarding.legal_business_name = form_data.legal_business_name
    onboarding.public_brand_name = form_data.public_brand_name
    onboarding.business_type = form_data.business_type
    onboarding.business_identity = _business_identity(form_data.registration_path)
    onboarding.business_industry = form_data.business_industry
    onboarding.has_business_tax_id = form_data.has_business_tax_id
    onboarding.brand_registration_mode = form_data.brand_registration_mode
    onboarding.business_registration_identifier = form_data.business_registration_identifier
    onboarding.business_registration_number_encrypted = (
        encrypt_provider_secret(form_data.business_registration_number)
        if form_data.business_registration_number
        else None
    )
    onboarding.business_regions_json = json.dumps(form_data.business_regions)
    onboarding.has_public_website = form_data.has_public_website
    onboarding.submission_source_mode = form_data.submission_source_mode
    onboarding.submission_source_reason = form_data.submission_source_reason
    onboarding.external_website_url = form_data.external_website_url
    onboarding.external_privacy_policy_url = form_data.external_privacy_policy_url
    onboarding.external_terms_and_conditions_url = form_data.external_terms_and_conditions_url
    onboarding.external_cta_proof_url = form_data.external_cta_proof_url
    onboarding.external_url_validation_json = json.dumps(form_data.external_url_validation, sort_keys=True)
    onboarding.external_urls_last_checked_at = utc_now()
    onboarding.website_url = form_data.website_url
    onboarding.social_profile_url = form_data.social_profile_url
    onboarding.privacy_policy_url = form_data.privacy_policy_url
    onboarding.terms_and_conditions_url = form_data.terms_and_conditions_url
    onboarding.cta_proof_url = form_data.cta_proof_url
    onboarding.email = form_data.email
    onboarding.notification_email = form_data.notification_email
    onboarding.phone_number = form_data.phone_number
    onboarding.mobile_number = form_data.mobile_number
    onboarding.first_name = form_data.first_name
    onboarding.last_name = form_data.last_name
    onboarding.business_title = form_data.business_title
    onboarding.job_position = form_data.job_position
    onboarding.address_country = form_data.address_country
    onboarding.address_line1 = form_data.address_line1
    onboarding.address_line2 = form_data.address_line2
    onboarding.address_city = form_data.address_city
    onboarding.address_region = form_data.address_region
    onboarding.address_postal_code = form_data.address_postal_code
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
    onboarding.declaration_accepted_at = utc_now() if form_data.declaration_accepted else onboarding.declaration_accepted_at
    onboarding.raw_submission_json = json.dumps(
        {
            "has_embedded_links": form_data.has_embedded_links,
            "has_embedded_phone": form_data.has_embedded_phone,
            "opt_in_keywords": form_data.opt_in_keywords,
            "opt_out_keywords": form_data.opt_out_keywords,
            "help_keywords": form_data.help_keywords,
            "business_regions": form_data.business_regions,
            "has_business_tax_id": form_data.has_business_tax_id,
            "brand_registration_mode": form_data.brand_registration_mode,
            "has_public_website": form_data.has_public_website,
            "submission_source_mode": form_data.submission_source_mode,
            "submission_source_reason": form_data.submission_source_reason,
            "privacy_policy_url": form_data.privacy_policy_url,
            "terms_and_conditions_url": form_data.terms_and_conditions_url,
            "cta_proof_url": form_data.cta_proof_url,
            "external_website_url": form_data.external_website_url,
            "external_privacy_policy_url": form_data.external_privacy_policy_url,
            "external_terms_and_conditions_url": form_data.external_terms_and_conditions_url,
            "external_cta_proof_url": form_data.external_cta_proof_url,
            "external_url_validation": form_data.external_url_validation,
        },
        sort_keys=True,
    )
    profile.business_type = form_data.business_type
    profile.use_case = form_data.campaign_description[:120]
    seed_service_address_from_onboarding(profile, onboarding, overwrite=False)
    if queue_submission:
        _clear_recovery_state(onboarding)
        should_reset_campaign = (
            onboarding.onboarding_status in {"rejected", "error", "canceled"}
            or _status_value(onboarding.campaign_status) in A2P_CAMPAIGN_FAILURE_STATUSES
        )
        if should_reset_campaign:
            onboarding.campaign_sid = None
        onboarding.onboarding_status = "queued"
        onboarding.brand_status = None
        onboarding.campaign_status = None
        onboarding.verification_status = None
        onboarding.submitted_at = utc_now()
        onboarding.canceled_at = None
        onboarding.approved_at = None
        onboarding.last_error = None
        onboarding.failure_code = None
        if form_data.upgrade_requested:
            onboarding.upgrade_requested_at = onboarding.upgrade_requested_at or utc_now()
        if form_data.brand_registration_mode != "standard":
            onboarding.upgrade_requested_at = None
            onboarding.upgraded_at = None
        onboarding.upgrade_recommended_reason = form_data.upgrade_recommended_reason
        onboarding.upgrade_recommended_at = utc_now() if form_data.upgrade_recommended_reason else None
        profile.last_provision_error = None
        if profile.provider_status != "suspended":
            profile.set_provider_status("pending")
    elif onboarding.onboarding_status not in {"pending", "processing", "queued", "approved"}:
        onboarding.onboarding_status = "draft"
        onboarding.upgrade_recommended_reason = form_data.upgrade_recommended_reason
        onboarding.upgrade_recommended_at = utc_now() if form_data.upgrade_recommended_reason else None


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
    require_chargeable_provider_entitlement(organization, "a2p_submission")

    profile = organization.messaging_profile or ensure_messaging_profile(organization)
    onboarding = ensure_a2p_onboarding(organization)
    form_data = _build_form_data(payload, organization, require_declaration=False)
    _save_form_data(onboarding, profile, form_data, queue_submission=True)
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
            "submission_source_mode": onboarding.submission_source_mode,
            "messaging_service_sid": profile.messaging_service_sid,
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


def save_a2p_onboarding_draft(
    organization_id: int,
    payload: dict[str, Any],
    *,
    actor_user_id: int | None = None,
) -> OrganizationA2POnboarding:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")

    profile = organization.messaging_profile or ensure_messaging_profile(organization)
    onboarding = ensure_a2p_onboarding(organization)
    form_data = _build_form_data(payload, organization, require_declaration=False)
    _save_form_data(onboarding, profile, form_data, queue_submission=False)
    _record_provider_audit(
        organization.id,
        "a2p_save_draft",
        actor_user_id=actor_user_id,
        message="Saved Twilio A2P onboarding draft.",
        metadata={
            "registration_path": onboarding.registration_path,
            "number_strategy": onboarding.number_strategy,
            "submission_source_mode": onboarding.submission_source_mode,
        },
    )
    db.session.commit()
    return onboarding


def sync_a2p_onboarding_status(
    organization_id: int,
    *,
    actor_user_id: int | None = None,
) -> OrganizationA2POnboarding:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")

    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    profile = organization.messaging_profile or ensure_messaging_profile(organization)

    try:
        brand_status, campaign_status = _sync_remote_status(
            onboarding,
            profile,
            actor_user_id=actor_user_id,
        )
        onboarding.brand_status = brand_status
        onboarding.campaign_status = campaign_status
        profile.provider_last_checked_at = utc_now()
        if _recovery_state(onboarding):
            db.session.commit()
            return onboarding
        _apply_status_snapshot(
            onboarding,
            profile,
            actor_user_id=actor_user_id,
            allow_number_setup=False,
        )
        db.session.commit()
        return onboarding
    except Exception as exc:
        db.session.rollback()
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            raise
        onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
        profile = organization.messaging_profile or ensure_messaging_profile(organization)

        if isinstance(exc, PlatformSubaccountAuthRequiredError):
            _mark_subaccount_auth_required_issue(
                onboarding,
                profile,
                summary=str(exc),
                actor_user_id=actor_user_id,
            )
            db.session.commit()
            return onboarding

        if _is_transient_provider_exception(exc):
            _mark_transient_connectivity_issue(
                onboarding,
                profile,
                summary=_friendly_provider_error_message(str(exc)),
                actor_user_id=actor_user_id,
            )
            db.session.commit()
            return onboarding

        error_message = _friendly_provider_error_message(str(exc))
        _set_non_destructive_error_state(
            onboarding,
            profile,
            onboarding_status="needs_action",
            brand_status=onboarding.brand_status,
            campaign_status=onboarding.campaign_status,
            error_message=error_message,
        )
        _record_provider_audit(
            organization.id,
            "a2p_refresh_failed",
            actor_user_id=actor_user_id,
            status="error",
            message=error_message,
            metadata=_a2p_audit_metadata(onboarding, profile),
        )
        db.session.commit()
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
    _clear_recovery_state(onboarding)
    db.session.commit()
    profile = organization.messaging_profile or ensure_messaging_profile(organization)
    _record_provider_audit(
        organization.id,
        "a2p_refresh",
        actor_user_id=actor_user_id,
        status="pending",
        message="Queued a Twilio A2P status refresh.",
        metadata={
            "campaign_sid": onboarding.campaign_sid,
            "messaging_service_sid": profile.messaging_service_sid,
            "submission_source_mode": onboarding.submission_source_mode,
        },
    )
    db.session.commit()
    try:
        _queue_job("app.tasks.sync_a2p_onboarding_status_job", organization.id, actor_user_id)
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


def _required_inventory_item(
    items: list[dict[str, Any]],
    sid: str | None,
    *,
    label: str,
) -> dict[str, Any]:
    match = _matching_item(items, sid)
    if match is None:
        raise ProviderProvisioningError(f"Select a valid live Twilio {label} before reconciling state.")
    return match


def reconcile_a2p_twilio_state(
    organization_id: int,
    *,
    messaging_service_sid: str,
    customer_profile_sid: str,
    trust_product_sid: str,
    brand_registration_sid: str,
    actor_user_id: int | None = None,
) -> OrganizationA2POnboarding:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")

    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    profile = organization.messaging_profile or ensure_messaging_profile(organization)
    if profile.provider_mode == "customer_managed":
        raise ProviderProvisioningError("Customer-managed Twilio workspaces do not use platform A2P reconciliation.")
    if not profile.twilio_subaccount_sid:
        raise ProviderProvisioningError("Twilio subaccount must exist before A2P state can be reconciled.")

    inventory = _inventory_subaccount_resources(profile)
    service = _required_inventory_item(inventory.get("services", []), messaging_service_sid, label="Messaging Service")
    customer_profile = _required_inventory_item(
        inventory.get("customer_profiles", []),
        customer_profile_sid,
        label="Customer Profile",
    )
    trust_product = _required_inventory_item(
        inventory.get("trust_products", []),
        trust_product_sid,
        label="Trust Product",
    )
    brand = _required_inventory_item(
        inventory.get("brands", []),
        brand_registration_sid,
        label="Brand Registration",
    )
    live_campaign = _preferred_item(
        list(service.get("campaigns", [])),
        current_sid=onboarding.campaign_sid,
        approved_statuses=A2P_CAMPAIGN_APPROVED_STATUSES,
        reviewing_statuses=A2P_REVIEWING_STATUSES,
    )

    profile.messaging_service_sid = service["sid"]
    profile.inbound_identity = profile.from_number or service["sid"]
    onboarding.customer_profile_sid = customer_profile["sid"]
    onboarding.trust_product_sid = trust_product["sid"]
    onboarding.brand_registration_sid = brand["sid"]
    onboarding.brand_status = _status_value(brand.get("status")) or _status_value(brand.get("identity_status"))
    onboarding.failure_code = None
    onboarding.last_error = None

    status_payload = _load_status_payload(onboarding)
    status_payload["brand_status"] = onboarding.brand_status
    status_payload["brand_tcr_id"] = brand.get("tcr_id")
    status_payload["messaging_service_sid"] = service["sid"]
    status_payload.pop("campaign_failure_reason", None)
    status_payload.pop("campaign_failure_code", None)
    status_payload.pop("campaign_errors", None)

    if live_campaign is not None:
        onboarding.campaign_sid = live_campaign.get("sid")
        onboarding.campaign_status = _status_value(live_campaign.get("status"))
        status_payload["campaign_status"] = onboarding.campaign_status
        status_payload["campaign_use_case"] = live_campaign.get("use_case")
        status_payload.pop(A2P_RECOVERY_STATE_KEY, None)
        _clear_recovery_state(onboarding)
        _store_status_payload(onboarding, status_payload)
        _apply_status_snapshot(
            onboarding,
            profile,
            actor_user_id=actor_user_id,
            allow_number_setup=False,
        )
    else:
        onboarding.campaign_sid = None
        onboarding.campaign_status = None
        status_payload["campaign_status"] = None
        _store_status_payload(onboarding, status_payload)
        _mark_recovery_required(
            onboarding,
            profile,
            recovery_type="missing_campaign",
            summary=(
                "Twilio state is now aligned to the current approved resources. The only remaining step is to create a campaign explicitly."
            ),
            inventory=inventory,
            brand_status=onboarding.brand_status,
            campaign_status=None,
            actor_user_id=actor_user_id,
        )

    _record_provider_audit(
        organization.id,
        "a2p_reconcile_confirmed",
        actor_user_id=actor_user_id,
        message="Rebound the org to the current live Twilio A2P resources.",
        metadata={
            **_a2p_audit_metadata(onboarding, profile),
            "customer_profile_sid": onboarding.customer_profile_sid,
            "trust_product_sid": onboarding.trust_product_sid,
            "brand_registration_sid": onboarding.brand_registration_sid,
        },
    )
    db.session.commit()
    return onboarding


def create_missing_a2p_campaign(
    organization_id: int,
    *,
    actor_user_id: int | None = None,
) -> OrganizationA2POnboarding:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    require_chargeable_provider_entitlement(organization, "a2p_campaign_creation")

    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    profile = organization.messaging_profile or ensure_messaging_profile(organization)
    if profile.provider_mode == "customer_managed":
        raise ProviderProvisioningError("Customer-managed Twilio workspaces do not use platform campaign creation.")
    if not onboarding.brand_registration_sid or not profile.messaging_service_sid:
        raise ProviderProvisioningError("Reconcile Twilio state before creating a campaign.")
    recovery_state = _recovery_state(onboarding)
    if recovery_state and recovery_state.get("recommended_action") == "reconcile":
        raise ProviderProvisioningError("Reconcile Twilio state before creating a new campaign.")
    effective_brand_status = _status_value(onboarding.brand_status) or _status_value(_load_status_payload(onboarding).get("brand_status"))
    if effective_brand_status not in A2P_BRAND_READY_FOR_CAMPAIGN_STATUSES:
        raise ProviderProvisioningError("Twilio brand approval must complete before a campaign can be created.")
    if onboarding.campaign_sid and _status_value(onboarding.campaign_status) not in A2P_CAMPAIGN_FAILURE_STATUSES:
        raise ProviderProvisioningError("This org already has a Twilio campaign attached.")

    _create_a2p_campaign(onboarding, profile, actor_user_id=actor_user_id)
    brand_status, campaign_status = _sync_remote_status(onboarding, profile, actor_user_id=actor_user_id)
    onboarding.brand_status = brand_status
    onboarding.campaign_status = campaign_status
    _apply_status_snapshot(
        onboarding,
        profile,
        actor_user_id=actor_user_id,
        allow_number_setup=False,
    )
    _record_provider_audit(
        organization.id,
        "a2p_campaign_create_confirmed",
        actor_user_id=actor_user_id,
        status="pending",
        message="Created a new Twilio A2P campaign after explicit platform confirmation.",
        metadata=_a2p_audit_metadata(onboarding, profile),
    )
    db.session.commit()
    return onboarding


def _ensure_provider_resources(organization: Organization) -> OrganizationMessagingProfile:
    require_chargeable_provider_entitlement(organization, "a2p_provider_resources")
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
    provider_status_on_error: str | None = "error",
) -> None:
    onboarding.onboarding_status = onboarding_status
    onboarding.brand_status = brand_status
    onboarding.campaign_status = campaign_status
    onboarding.verification_status = verification_status
    onboarding.last_synced_at = utc_now()
    onboarding.last_error = error_message
    if not error_message:
        onboarding.failure_code = None
    profile.provider_last_checked_at = utc_now()
    if not error_message:
        profile.last_provision_error = None
    if error_message:
        profile.last_provision_error = error_message
    if error_message and profile.provider_status != "suspended" and provider_status_on_error:
        profile.set_provider_status(provider_status_on_error)


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


def _status_failure_reason(onboarding: OrganizationA2POnboarding) -> str | None:
    status_payload = _load_status_payload(onboarding)
    return status_payload.get("campaign_failure_reason") or status_payload.get("brand_failure_reason")


def _resolved_campaign_status(onboarding: OrganizationA2POnboarding, campaign_status: str | None) -> str | None:
    if campaign_status:
        return campaign_status
    return "pending" if onboarding.campaign_sid else None


def _trusthub_status_callback_url() -> str:
    base_url = (
        current_app.config.get("APP_BASE_URL")
        or current_app.config.get("SAAS_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if not base_url:
        raise ProviderProvisioningError("APP_BASE_URL must be configured to receive Twilio Trust Hub status callbacks.")
    return f"{base_url}/webhooks/twilio/trusthub-status"


def _business_regions_value(onboarding: OrganizationA2POnboarding) -> str:
    raw_value = (onboarding.business_regions_json or "").strip()
    if not raw_value:
        return "USA_AND_CANADA"
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        normalized = [str(item).strip() for item in parsed if str(item).strip()]
        return ",".join(normalized) or "USA_AND_CANADA"
    return raw_value


def _ensure_trusthub_address(onboarding: OrganizationA2POnboarding, client) -> str | None:
    if onboarding.address_sid:
        return onboarding.address_sid
    country, line1, city, region, postal_code = _require_address(
        country=onboarding.address_country,
        line1=onboarding.address_line1,
        city=onboarding.address_city,
        region=onboarding.address_region,
        postal_code=onboarding.address_postal_code,
    )
    friendly_name = f"{onboarding.business_name[:48]} business address"
    matches = [
        address
        for address in client.addresses.list(
            customer_name=onboarding.business_name,
            friendly_name=friendly_name,
            iso_country=country,
            limit=20,
        )
        if _resource_text(address, "street") == line1
        and _resource_text(address, "city") == city
        and _resource_text(address, "region") == region
        and _resource_text(address, "postal_code") == postal_code
    ]
    address = _single_remote_resource(matches, "business address")
    if address is None:
        address = client.addresses.create(
            customer_name=onboarding.business_name,
            friendly_name=friendly_name,
            street=line1,
            street_secondary=onboarding.address_line2 or None,
            city=city,
            region=region,
            postal_code=postal_code,
            iso_country=country,
        )
    onboarding.address_sid = address.sid
    return address.sid


def _upsert_a2p_resources(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile) -> None:
    client = _build_subaccount_client(profile, require_stored_auth_token=True)
    status_payload = _load_status_payload(onboarding)
    primary_customer_profile_sid = (current_app.config.get("TWILIO_PRIMARY_CUSTOMER_PROFILE_SID") or "").strip()
    business_registration_number = (
        decrypt_provider_secret(onboarding.business_registration_number_encrypted)
        if onboarding.business_registration_number_encrypted
        else None
    )
    business_registration_identifier, business_registration_number = _validate_business_registration_details(
        onboarding.registration_path,
        _clean_text(onboarding.business_registration_identifier),
        _clean_text(business_registration_number),
    )
    trusthub_status_callback = _trusthub_status_callback_url()
    notification_email = _normalize_notification_email(onboarding.notification_email, fallback=onboarding.email)
    address_sid = _ensure_trusthub_address(onboarding, client)
    status_payload["address_sid"] = address_sid
    _checkpoint_a2p_provisioning(onboarding, status_payload, "address_ready")

    customer_profile_policy_sid, trust_product_policy_sid = _policy_sids(onboarding.registration_path)
    if not onboarding.customer_profile_sid:
        customer_profile_name = f"{onboarding.business_name} Customer Profile"
        matches = list(
            client.trusthub.v1.customer_profiles.list(
                friendly_name=customer_profile_name,
                policy_sid=customer_profile_policy_sid,
                limit=20,
            )
        )
        customer_profile = _single_remote_resource(matches, "Customer Profile")
        if customer_profile is None:
            customer_profile = client.trusthub.v1.customer_profiles.create(
                policy_sid=customer_profile_policy_sid,
                friendly_name=customer_profile_name,
                email=notification_email,
                status_callback=trusthub_status_callback,
            )
        onboarding.customer_profile_sid = _required_remote_sid(customer_profile, "Customer Profile")
        _checkpoint_a2p_provisioning(onboarding, status_payload, "customer_profile_ready")

    if onboarding.registration_path == "sole_proprietor":
        if not status_payload.get("sole_proprietor_end_user_sid"):
            sole_prop = _find_or_create_end_user(
                client,
                f"{onboarding.business_name} Sole Proprietor",
                "sole_proprietor_information",
                {
                    "first_name": onboarding.first_name,
                    "last_name": onboarding.last_name,
                    "email": onboarding.email,
                    "phone_number": onboarding.mobile_number or onboarding.phone_number,
                    "business_title": onboarding.business_title or "Owner",
                    "job_position": onboarding.job_position or "Other",
                },
                "sole proprietor End User",
            )
            status_payload["sole_proprietor_end_user_sid"] = _required_remote_sid(
                sole_prop,
                "sole proprietor End User",
            )
            _checkpoint_a2p_provisioning(onboarding, status_payload, "sole_proprietor_end_user_ready")
    else:
        if not status_payload.get("business_information_end_user_sid"):
            business_info = _find_or_create_end_user(
                client,
                f"{onboarding.business_name} Business Information",
                "customer_profile_business_information",
                {
                    "business_name": onboarding.business_name,
                    "social_media_profile_urls": onboarding.social_profile_url or "",
                    "website_url": onboarding.website_url or "",
                    "business_regions_of_operation": _business_regions_value(onboarding),
                    "business_type": _normalize_business_type(onboarding.registration_path, onboarding.business_type) or "",
                    "business_registration_identifier": business_registration_identifier or "EIN",
                    "business_identity": onboarding.business_identity or "direct_customer",
                    "business_industry": _normalize_business_industry(onboarding.business_industry),
                    "business_registration_number": _twilio_business_registration_number(
                        business_registration_identifier,
                        business_registration_number,
                    ),
                },
                "business-information End User",
            )
            status_payload["business_information_end_user_sid"] = _required_remote_sid(
                business_info,
                "business-information End User",
            )
            _checkpoint_a2p_provisioning(onboarding, status_payload, "business_information_end_user_ready")
        if not status_payload.get("authorized_representative_sid"):
            authorized_rep = _find_or_create_end_user(
                client,
                f"{onboarding.business_name} Authorized Representative",
                "authorized_representative_1",
                {
                    "first_name": onboarding.first_name,
                    "last_name": onboarding.last_name,
                    "email": onboarding.email,
                    "phone_number": onboarding.mobile_number or onboarding.phone_number,
                    "business_title": onboarding.business_title or "Owner",
                    "job_position": onboarding.job_position or "Other",
                },
                "authorized-representative End User",
            )
            status_payload["authorized_representative_sid"] = _required_remote_sid(
                authorized_rep,
                "authorized-representative End User",
            )
            _checkpoint_a2p_provisioning(onboarding, status_payload, "authorized_representative_ready")
        if address_sid and not status_payload.get("supporting_document_sid"):
            document_name = onboarding.business_name[:64]
            matches = []
            for document in client.trusthub.v1.supporting_documents.list(limit=100):
                attributes = getattr(document, "attributes", {}) or {}
                address_sids = attributes.get("address_sids", []) if isinstance(attributes, dict) else []
                if isinstance(address_sids, str):
                    address_sids = [address_sids]
                if (
                    _resource_text(document, "friendly_name") == document_name
                    and _resource_text(document, "type") == "customer_profile_address"
                    and address_sid in address_sids
                ):
                    matches.append(document)
            supporting_document = _single_remote_resource(matches, "address Supporting Document")
            if supporting_document is None:
                supporting_document = client.trusthub.v1.supporting_documents.create(
                    friendly_name=document_name,
                    type="customer_profile_address",
                    attributes={"address_sids": address_sid},
                )
            supporting_document_sid = _required_remote_sid(supporting_document, "address Supporting Document")
            status_payload["supporting_document_sid"] = supporting_document_sid
            onboarding.supporting_document_sid = supporting_document_sid
            _checkpoint_a2p_provisioning(onboarding, status_payload, "supporting_document_ready")

    if not onboarding.trust_product_sid:
        trust_product_name = f"{onboarding.business_name} A2P Trust Product"
        matches = list(
            client.trusthub.v1.trust_products.list(
                friendly_name=trust_product_name,
                policy_sid=trust_product_policy_sid,
                limit=20,
            )
        )
        trust_product = _single_remote_resource(matches, "A2P Trust Product")
        if trust_product is None:
            trust_product = client.trusthub.v1.trust_products.create(
                friendly_name=trust_product_name,
                policy_sid=trust_product_policy_sid,
                email=notification_email,
                status_callback=trusthub_status_callback,
            )
        onboarding.trust_product_sid = _required_remote_sid(trust_product, "A2P Trust Product")
        _checkpoint_a2p_provisioning(onboarding, status_payload, "trust_product_ready")

    if onboarding.registration_path == "sole_proprietor":
        object_sid = status_payload.get("sole_proprietor_end_user_sid")
        if object_sid and status_payload.get("sole_prop_assigned") != object_sid:
            _ensure_remote_assignment(
                client.trusthub.v1.trust_products(onboarding.trust_product_sid).trust_products_entity_assignments,
                object_sid,
                "sole proprietor assignment",
            )
            status_payload["sole_prop_assigned"] = object_sid
            _checkpoint_a2p_provisioning(onboarding, status_payload, "sole_proprietor_assigned")
    else:
        business_sid = status_payload.get("business_information_end_user_sid")
        if business_sid and status_payload.get("business_info_assigned") != business_sid:
            _ensure_remote_assignment(
                client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_entity_assignments,
                business_sid,
                "business-information assignment",
            )
            status_payload["business_info_assigned"] = business_sid
            _checkpoint_a2p_provisioning(onboarding, status_payload, "business_information_assigned")
        authorized_sid = status_payload.get("authorized_representative_sid")
        if authorized_sid and status_payload.get("authorized_rep_assigned") != authorized_sid:
            _ensure_remote_assignment(
                client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_entity_assignments,
                authorized_sid,
                "authorized-representative assignment",
            )
            status_payload["authorized_rep_assigned"] = authorized_sid
            _checkpoint_a2p_provisioning(onboarding, status_payload, "authorized_representative_assigned")
        supporting_sid = status_payload.get("supporting_document_sid")
        if supporting_sid and status_payload.get("supporting_doc_assigned") != supporting_sid:
            _ensure_remote_assignment(
                client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_entity_assignments,
                supporting_sid,
                "supporting-document assignment",
            )
            status_payload["supporting_doc_assigned"] = supporting_sid
            _checkpoint_a2p_provisioning(onboarding, status_payload, "supporting_document_assigned")
        if primary_customer_profile_sid and status_payload.get("primary_profile_assigned") != primary_customer_profile_sid:
            _ensure_remote_assignment(
                client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).customer_profiles_entity_assignments,
                primary_customer_profile_sid,
                "primary-profile assignment",
            )
            status_payload["primary_profile_assigned"] = primary_customer_profile_sid
            _checkpoint_a2p_provisioning(onboarding, status_payload, "primary_profile_assigned")

    if not status_payload.get("messaging_profile_end_user_sid"):
        messaging_profile = _find_or_create_end_user(
            client,
            f"{onboarding.business_name} Messaging Profile",
            "us_a2p_messaging_profile_information",
            {"company_type": _messaging_profile_company_type(onboarding.registration_path)},
            "A2P Messaging Profile End User",
        )
        status_payload["messaging_profile_end_user_sid"] = _required_remote_sid(
            messaging_profile,
            "A2P Messaging Profile End User",
        )
        _checkpoint_a2p_provisioning(onboarding, status_payload, "messaging_profile_end_user_ready")

    messaging_profile_sid = status_payload.get("messaging_profile_end_user_sid")
    if messaging_profile_sid and status_payload.get("messaging_profile_assigned") != messaging_profile_sid:
        _ensure_remote_assignment(
            client.trusthub.v1.trust_products(onboarding.trust_product_sid).trust_products_entity_assignments,
            messaging_profile_sid,
            "messaging-profile assignment",
        )
        status_payload["messaging_profile_assigned"] = messaging_profile_sid
        _checkpoint_a2p_provisioning(onboarding, status_payload, "messaging_profile_assigned")

    if status_payload.get("customer_profile_assigned") != onboarding.customer_profile_sid:
        _ensure_remote_assignment(
            client.trusthub.v1.trust_products(onboarding.trust_product_sid).trust_products_entity_assignments,
            onboarding.customer_profile_sid,
            "Customer Profile assignment",
        )
        status_payload["customer_profile_assigned"] = onboarding.customer_profile_sid
        _checkpoint_a2p_provisioning(onboarding, status_payload, "customer_profile_assigned")

    if not status_payload.get("customer_profile_evaluated"):
        evaluation_context = client.trusthub.v1.customer_profiles(
            onboarding.customer_profile_sid
        ).customer_profiles_evaluations
        evaluation = _existing_evaluation(
            evaluation_context,
            customer_profile_policy_sid,
            "Customer Profile evaluation",
        )
        if evaluation is None:
            evaluation = evaluation_context.create(policy_sid=customer_profile_policy_sid)
        status_payload["customer_profile_evaluation_sid"] = getattr(evaluation, "sid", None)
        status_payload["customer_profile_evaluated"] = True
        _checkpoint_a2p_provisioning(onboarding, status_payload, "customer_profile_evaluated")
    if not status_payload.get("customer_profile_submitted"):
        client.trusthub.v1.customer_profiles(onboarding.customer_profile_sid).update(status="pending-review")
        status_payload["customer_profile_submitted"] = True
        _checkpoint_a2p_provisioning(onboarding, status_payload, "customer_profile_submitted")

    if not status_payload.get("trust_product_evaluated"):
        evaluation_context = client.trusthub.v1.trust_products(
            onboarding.trust_product_sid
        ).trust_products_evaluations
        evaluation = _existing_evaluation(
            evaluation_context,
            trust_product_policy_sid,
            "Trust Product evaluation",
        )
        if evaluation is None:
            evaluation = evaluation_context.create(policy_sid=trust_product_policy_sid)
        status_payload["trust_product_evaluation_sid"] = getattr(evaluation, "sid", None)
        status_payload["trust_product_evaluated"] = True
        _checkpoint_a2p_provisioning(onboarding, status_payload, "trust_product_evaluated")
    if not status_payload.get("trust_product_submitted"):
        client.trusthub.v1.trust_products(onboarding.trust_product_sid).update(status="pending-review")
        status_payload["trust_product_submitted"] = True
        _checkpoint_a2p_provisioning(onboarding, status_payload, "trust_product_submitted")

    if not onboarding.brand_registration_sid:
        brand_type = "SOLE_PROPRIETOR" if onboarding.registration_path == "sole_proprietor" else "STANDARD"
        matches = [
            brand
            for brand in client.messaging.v1.brand_registrations.list(limit=100)
            if _resource_text(brand, "customer_profile_bundle_sid") == onboarding.customer_profile_sid
            and _resource_text(brand, "a2p_profile_bundle_sid") == onboarding.trust_product_sid
            and _resource_text(brand, "brand_type").upper() == brand_type
            and _resource_text(brand, "status").upper() != "DELETED"
        ]
        brand_registration = _single_remote_resource(matches, "Brand Registration")
        if brand_registration is None:
            brand_registration = client.messaging.v1.brand_registrations.create(
                customer_profile_bundle_sid=onboarding.customer_profile_sid,
                a2p_profile_bundle_sid=onboarding.trust_product_sid,
                brand_type=brand_type,
            )
        onboarding.brand_registration_sid = _required_remote_sid(brand_registration, "Brand Registration")
        _checkpoint_a2p_provisioning(onboarding, status_payload, "brand_registration_ready")

    _checkpoint_a2p_provisioning(onboarding, status_payload, "complete")


def _create_a2p_campaign(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    actor_user_id: int | None = None,
) -> None:
    if onboarding.campaign_sid and _status_value(onboarding.campaign_status) not in A2P_CAMPAIGN_FAILURE_STATUSES:
        return
    if not onboarding.brand_registration_sid:
        raise ProviderProvisioningError("Twilio brand registration must exist before campaign creation can begin.")
    if not profile.messaging_service_sid:
        raise ProviderProvisioningError("Twilio messaging service must exist before campaign creation can begin.")

    submission_payload = json.loads(onboarding.raw_submission_json or "{}")
    campaign_use_case = _normalize_use_case(onboarding.registration_path, onboarding.campaign_use_case)
    message_samples = _validate_message_samples(
        campaign_use_case,
        _json_loads_list(onboarding.message_samples_json) or [onboarding.campaign_description or onboarding.business_name],
    )
    client = _build_subaccount_client(profile, require_stored_auth_token=True)
    service_context = client.messaging.v1.services(profile.messaging_service_sid)
    us_app_to_person = service_context.us_app_to_person
    _prepare_service_for_campaign_create(
        onboarding,
        profile,
        service_context,
        actor_user_id=actor_user_id,
    )
    opt_in_keywords = _json_loads_list(onboarding.opt_in_keywords_json)
    opt_out_keywords = _json_loads_list(onboarding.opt_out_keywords_json)
    help_keywords = _json_loads_list(onboarding.help_keywords_json)
    request_data = values.of(
        {
            "BrandRegistrationSid": onboarding.brand_registration_sid,
            "Description": onboarding.campaign_description or onboarding.business_name,
            "MessageFlow": onboarding.message_flow or onboarding.campaign_description or onboarding.business_name,
            "MessageSamples": serialize.map(message_samples, lambda item: item),
            "UsAppToPersonUsecase": campaign_use_case,
            "HasEmbeddedLinks": bool(submission_payload.get("has_embedded_links")),
            "HasEmbeddedPhone": bool(submission_payload.get("has_embedded_phone")),
            "PrivacyPolicyUrl": onboarding.privacy_policy_url,
            "TermsAndConditionsUrl": onboarding.terms_and_conditions_url,
            "OptInMessage": onboarding.opt_in_message or values.unset,
            "OptOutMessage": onboarding.opt_out_message or values.unset,
            "HelpMessage": onboarding.help_message or values.unset,
            "OptInKeywords": serialize.map(opt_in_keywords, lambda item: item) if opt_in_keywords else values.unset,
            "OptOutKeywords": serialize.map(opt_out_keywords, lambda item: item) if opt_out_keywords else values.unset,
            "HelpKeywords": serialize.map(help_keywords, lambda item: item) if help_keywords else values.unset,
        }
    )
    try:
        campaign_payload = us_app_to_person._version.create(
            method="POST",
            uri=us_app_to_person._uri,
            data=request_data,
        )
    except TwilioRestException as exc:
        if not _is_campaign_association_conflict(exc):
            raise
        _prepare_service_for_campaign_create(
            onboarding,
            profile,
            service_context,
            actor_user_id=actor_user_id,
        )
        campaign_payload = us_app_to_person._version.create(
            method="POST",
            uri=us_app_to_person._uri,
            data=request_data,
        )
    onboarding.campaign_sid = campaign_payload.get("sid")


def _buy_phone_number(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile):
    require_chargeable_provider_entitlement(profile.organization, "phone_number_purchase")
    subaccount_client = _build_subaccount_client(profile, require_stored_auth_token=True)
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
    subaccount_client = _build_subaccount_client(profile, require_stored_auth_token=True)
    existing_number = subaccount_client.incoming_phone_numbers(onboarding.desired_phone_number_sid).fetch()
    return existing_number.sid, existing_number.phone_number


def _sync_remote_status(
    onboarding: OrganizationA2POnboarding,
    profile: OrganizationMessagingProfile,
    *,
    actor_user_id: int | None = None,
) -> tuple[str | None, str | None]:
    client, read_context = _build_subaccount_client_context(
        profile,
        require_stored_auth_token=True,
    )
    brand_status = None
    campaign_status = None
    brand_failure_code = None
    campaign_failure_code = None
    status_payload = _store_twilio_read_context(onboarding, read_context=read_context)
    _store_status_payload(onboarding, status_payload)

    try:
        if onboarding.brand_registration_sid:
            brand = client.messaging.v1.brand_registrations(onboarding.brand_registration_sid).fetch()
            brand_status = _status_value(getattr(brand, "status", None))
            brand_failure_reason, brand_failure_code = _failure_details_from_errors(getattr(brand, "errors", None))
            if not brand_failure_reason:
                brand_failure_reason = _clean_text(getattr(brand, "failure_reason", None))
            status_payload["brand_status"] = brand_status
            status_payload["brand_failure_reason"] = brand_failure_reason
            status_payload["brand_failure_code"] = brand_failure_code
            status_payload["brand_tcr_id"] = getattr(brand, "tcr_id", None)
        if onboarding.campaign_sid and profile.messaging_service_sid:
            campaign = client.messaging.v1.services(profile.messaging_service_sid).us_app_to_person(onboarding.campaign_sid).fetch()
            campaign_status = _status_value(getattr(campaign, "campaign_status", None) or getattr(campaign, "status", None))
            campaign_failure_reason, campaign_failure_code = _failure_details_from_errors(getattr(campaign, "errors", None))
            if not campaign_failure_reason:
                campaign_failure_reason = _clean_text(getattr(campaign, "failure_reason", None))
            status_payload["campaign_status"] = campaign_status
            status_payload["campaign_use_case"] = _campaign_use_case_value(
                getattr(campaign, "us_app_to_person_usecase", None) or getattr(campaign, "campaign_usecase", None)
            )
            console_campaign_id = (
                _console_campaign_id(getattr(campaign, "campaign_id", None))
                or _console_campaign_id(getattr(campaign, "external_campaign_id", None))
            )
            if console_campaign_id:
                status_payload["console_campaign_id"] = console_campaign_id
            status_payload["campaign_errors"] = getattr(campaign, "errors", None)
            status_payload["campaign_failure_reason"] = campaign_failure_reason
            status_payload["campaign_failure_code"] = campaign_failure_code
    except TwilioRestException as exc:
        if not _is_twilio_not_found_error(exc):
            raise

        inventory = _inventory_subaccount_resources(profile)
        recovery_state = _build_recovery_state(
            onboarding,
            profile,
            recovery_type="provider_drift",
            inventory=inventory,
            summary="Twilio provider state no longer matches the identifiers stored in the app.",
        )
        selected_brand_sid = recovery_state.get("selected", {}).get("brand_registration_sid")
        selected_campaign_sid = recovery_state.get("selected", {}).get("campaign_sid")
        selected_brand = _matching_item(inventory.get("brands", []), selected_brand_sid)
        selected_service = _matching_item(inventory.get("services", []), recovery_state.get("selected", {}).get("messaging_service_sid"))
        selected_campaign = _matching_item(
            list(selected_service.get("campaigns", [])) if selected_service is not None else [],
            selected_campaign_sid,
        )
        brand_status = _status_value((selected_brand or {}).get("status")) or _status_value((selected_brand or {}).get("identity_status")) or brand_status
        campaign_status = _status_value((selected_campaign or {}).get("status")) or campaign_status
        recovery_type = "missing_campaign" if recovery_state.get("only_missing_campaign") else "provider_drift"
        summary = (
            "Twilio still has approved A2P resources in the subaccount, but the app is bound to stale identifiers. "
            "Review the live resources and reconcile the Twilio state from the platform."
        )
        if recovery_type == "missing_campaign":
            summary = (
                "Twilio still has the approved brand package in the subaccount, but the selected Messaging Service has no attached campaign. "
                "Reconcile the live resources first, then create the campaign explicitly."
            )
        _mark_recovery_required(
            onboarding,
            profile,
            recovery_type=recovery_type,
            summary=summary,
            inventory=inventory,
            brand_status=brand_status,
            campaign_status=campaign_status,
            actor_user_id=actor_user_id,
        )
        onboarding.failure_code = None
        return brand_status, campaign_status

    onboarding.failure_code = campaign_failure_code or brand_failure_code or None
    status_payload.pop(A2P_RECOVERY_STATE_KEY, None)
    _clear_recovery_state(onboarding)
    _store_status_payload(onboarding, status_payload)
    return brand_status, campaign_status


def _complete_number_setup(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile, actor_user_id: int | None) -> None:
    if onboarding.number_strategy == "platform_assign":
        return
    finalize_sender_setup(profile.organization_id, actor_user_id=actor_user_id)


def process_a2p_onboarding(organization_id: int, actor_user_id: int | None = None) -> OrganizationA2POnboarding:
    if not a2p_onboarding_enabled():
        raise ProviderProvisioningError("Twilio A2P onboarding automation is not enabled.")

    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ProviderProvisioningError(f"Organization {organization_id} not found.")
    require_chargeable_provider_entitlement(organization, "a2p_processing")
    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    profile = _ensure_provider_resources(organization)

    if onboarding.onboarding_status in {"canceled", "approved"}:
        return onboarding

    onboarding.onboarding_status = "processing"
    onboarding.last_error = None
    db.session.commit()

    try:
        _upsert_a2p_resources(onboarding, profile)
        db.session.commit()
        ensure_a2p_event_stream_subscription(organization, profile)
        db.session.commit()
        brand_status, campaign_status = _sync_remote_status(onboarding, profile, actor_user_id=actor_user_id)
        onboarding.brand_status = brand_status
        onboarding.campaign_status = campaign_status
        profile.provider_last_checked_at = utc_now()
        if _recovery_state(onboarding):
            db.session.commit()
            return onboarding
        normalized_brand_status = _status_value(brand_status) or "pending"
        brand_ready_for_campaign = normalized_brand_status in A2P_BRAND_READY_FOR_CAMPAIGN_STATUSES
        if brand_ready_for_campaign and not onboarding.campaign_sid:
            inventory = _inventory_subaccount_resources(profile)
            recovery_type = "missing_campaign"
            summary = (
                "Twilio approved the brand package, but the Messaging Service still needs an explicit campaign create step. "
                "Review the live resources, then confirm campaign creation from the platform."
            )
            recovery_state = _build_recovery_state(
                onboarding,
                profile,
                recovery_type=recovery_type,
                inventory=inventory,
                summary=summary,
            )
            selected_service_sid = recovery_state.get("selected", {}).get("messaging_service_sid")
            selected_service = _matching_item(inventory.get("services", []), selected_service_sid)
            selected_campaign = _matching_item(
                list(selected_service.get("campaigns", [])) if selected_service is not None else [],
                recovery_state.get("selected", {}).get("campaign_sid"),
            )
            if selected_campaign is not None:
                recovery_type = "provider_drift"
                summary = (
                    "Twilio already has a live A2P campaign attached to the selected Messaging Service, but the app is not bound to it. "
                    "Reconcile the Twilio state from the platform before continuing."
                )
            _mark_recovery_required(
                onboarding,
                profile,
                recovery_type=recovery_type,
                summary=summary,
                inventory=inventory,
                brand_status=brand_status,
                campaign_status=_status_value((selected_campaign or {}).get("status")),
                actor_user_id=actor_user_id,
            )
            onboarding.failure_code = None
            db.session.commit()
            return onboarding
        _apply_status_snapshot(
            onboarding,
            profile,
            actor_user_id=actor_user_id,
            allow_number_setup=True,
        )
        db.session.commit()
        return onboarding
    except Exception as exc:
        db.session.rollback()
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            raise
        onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
        profile = organization.messaging_profile or ensure_messaging_profile(organization)
        if isinstance(exc, PlatformSubaccountAuthRequiredError):
            _mark_subaccount_auth_required_issue(
                onboarding,
                profile,
                summary=str(exc),
                actor_user_id=actor_user_id,
            )
            db.session.commit()
            return onboarding
        if _is_transient_provider_exception(exc):
            _mark_transient_connectivity_issue(
                onboarding,
                profile,
                summary=_friendly_provider_error_message(str(exc)),
                actor_user_id=actor_user_id,
            )
            db.session.commit()
            return onboarding
        error_message = _friendly_provider_error_message(str(exc))
        _set_status(
            onboarding,
            profile,
            onboarding_status="error",
            brand_status=onboarding.brand_status,
            campaign_status=onboarding.campaign_status,
            error_message=error_message,
        )
        _record_provider_audit(
            organization.id,
            "a2p_failed",
            actor_user_id=actor_user_id,
            status="error",
            message=error_message,
        )
        db.session.commit()
        raise ProviderProvisioningError(error_message) from exc


def describe_a2p_onboarding(
    onboarding: OrganizationA2POnboarding | None,
    profile: OrganizationMessagingProfile | None = None,
) -> dict[str, Any]:
    if profile is not None and profile.provider_mode == "customer_managed":
        status_payload = _load_status_payload(onboarding) if onboarding is not None else {}
        brand_status = _status_value(
            status_payload.get("brand_status") if status_payload else (onboarding.brand_status if onboarding else None)
        )
        campaign_status = _status_value(
            status_payload.get("campaign_status") if status_payload else (onboarding.campaign_status if onboarding else None)
        )
        failure_reason = (
            _clean_text(status_payload.get("campaign_failure_reason"))
            or _clean_text(status_payload.get("brand_failure_reason"))
            or _clean_text(onboarding.last_error if onboarding is not None else None)
        )
        failure_code = (
            _clean_text(status_payload.get("campaign_failure_code"))
            or _clean_text(status_payload.get("brand_failure_code"))
            or _clean_text(onboarding.failure_code if onboarding is not None else None)
        )
        can_send = bool(profile.can_send)
        campaign_sid = (
            status_payload.get("campaign_sid")
            if status_payload.get("campaign_sid")
            else (onboarding.campaign_sid if onboarding is not None else None)
        )
        console_campaign_id = _console_campaign_id(status_payload.get("console_campaign_id"))
        messaging_service_sid = (
            status_payload.get("messaging_service_sid")
            if status_payload.get("messaging_service_sid")
            else profile.messaging_service_sid
        )
        phone_number_sid = (
            status_payload.get("phone_number_sid")
            if status_payload.get("phone_number_sid")
            else profile.phone_number_sid
        )
        activation_complete = customer_managed_activation_complete(onboarding, profile=profile)
        activation_state = customer_managed_activation_state(onboarding, profile=profile)
        approved_externally = (
            brand_status in A2P_BRAND_APPROVED_STATUSES
            and campaign_status in A2P_CAMPAIGN_APPROVED_STATUSES
        )
        if can_send or activation_complete:
            stage = "approved"
            badge = "success"
            title = "External Twilio active"
            summary = "This workspace uses a customer-managed Twilio account and Twinevia is already the live inbound destination."
            next_step = "Keep A2P, sender compliance, and Messaging Service ownership managed in the customer Twilio account."
            eta = "Ready immediately."
        elif approved_externally:
            stage = "activation_pending"
            badge = "warning text-dark"
            title = "External Twilio approved, activation pending"
            summary = (
                "Twilio shows an approved external brand and campaign, but Twinevia has not taken over inbound routing yet."
            )
            next_step = "Run Activate External Twilio when you are ready to move the inbound webhook and event-stream sync into Twinevia."
            eta = "Ready after activation."
        elif failure_reason or (
            brand_status in A2P_BRAND_FAILURE_STATUSES
            or campaign_status in A2P_CAMPAIGN_FAILURE_STATUSES
        ):
            stage = "needs_action"
            badge = "danger"
            title = "External A2P needs action"
            summary = failure_reason or "The customer-managed Twilio campaign or brand needs correction before live sending can be approved."
            next_step = "Correct the customer-managed Twilio brand or campaign, then re-save messaging settings to sync the latest status."
            eta = "Live SMS stays blocked until the external registration is approved."
        elif campaign_sid or messaging_service_sid or phone_number_sid:
            stage = "reviewing"
            badge = "warning text-dark"
            title = "External A2P under review"
            summary = "Twilio is still reviewing the customer-managed brand or campaign."
            next_step = "Keep the external Messaging Service, sender, and campaign aligned while Twilio completes review."
            eta = "Timing depends on the customer-managed Twilio account."
        else:
            stage = "external"
            badge = "info"
            title = "External A2P managed"
            summary = "A2P registration is owned in the customer Twilio account instead of the platform."
            next_step = (
                "Confirm the external brand, campaign, messaging service, and sender remain approved in Twilio."
            )
            eta = "Status depends on the customer-managed Twilio account."
        return {
            "stage": stage,
            "badge": badge,
            "title": title,
            "summary": summary,
            "next_step": next_step,
            "eta": eta,
            "failure_reason": failure_reason,
            "failure_code": failure_code,
            "brand_status": _humanize_status(brand_status, fallback="externally managed"),
            "campaign_status": _humanize_status(campaign_status, fallback="externally managed"),
            "number_status": "configured" if profile.from_number else "pending",
            "last_checked_at": onboarding.last_synced_at if onboarding is not None else profile.provider_last_checked_at,
            "has_submission": bool(campaign_sid or messaging_service_sid or phone_number_sid),
            "is_waiting": stage == "reviewing",
            "show_wait_state": stage in {"reviewing", "needs_action", "activation_pending"} and not can_send,
            "event_streams_enabled": a2p_event_streams_enabled(),
            "external_managed": True,
            "campaign_sid": campaign_sid,
            "console_campaign_id": console_campaign_id,
            "activation_state": activation_state,
            "brand_registration_sid": (
                status_payload.get("brand_registration_sid")
                if status_payload.get("brand_registration_sid")
                else (onboarding.brand_registration_sid if onboarding is not None else None)
            ),
            "messaging_service_sid": messaging_service_sid,
            "phone_number_sid": phone_number_sid,
            "recovery_state": None,
            "can_reconcile": False,
            "can_create_campaign": False,
            "campaign_fee_warning": None,
            "twilio_read_context": _twilio_read_context(onboarding) if onboarding is not None else {},
        }

    if onboarding is None or onboarding.onboarding_status == "draft":
        return {
            "stage": "draft",
            "badge": "secondary",
            "title": "Not submitted yet",
            "summary": "The A2P registration packet has not been submitted.",
            "next_step": "Submit the business details to start Twilio review.",
            "eta": "Nothing is under review yet.",
            "failure_reason": None,
            "failure_code": None,
            "brand_status": "not submitted",
            "campaign_status": "not submitted",
            "number_status": "not started",
            "last_checked_at": None,
            "has_submission": False,
            "is_waiting": False,
            "show_wait_state": False,
            "event_streams_enabled": bool(current_app.config.get("TWILIO_A2P_EVENT_STREAMS_ENABLED")),
            "external_managed": False,
            "recovery_state": None,
            "can_reconcile": False,
            "can_create_campaign": False,
            "campaign_fee_warning": None,
            "console_campaign_id": None,
            "twilio_read_context": {},
        }

    brand_status = _status_value(onboarding.brand_status)
    campaign_status = _status_value(onboarding.campaign_status)
    number_status = _latest_number_status(onboarding)
    failure_reason = _latest_failure_message(onboarding)
    failure_code = _latest_failure_code(onboarding)
    has_submission = onboarding.onboarding_status not in {"draft", "canceled"}
    can_send = bool(profile is not None and profile.can_send)
    recovery_state = _recovery_state(onboarding)
    recovery_type = _clean_text(recovery_state.get("type"), lowercase=True) if recovery_state else None
    synthetic_missing_campaign = (
        recovery_type is None
        and
        brand_status in A2P_BRAND_READY_FOR_CAMPAIGN_STATUSES
        and not onboarding.campaign_sid
        and campaign_status not in A2P_CAMPAIGN_APPROVED_STATUSES
    )

    if recovery_type == "provider_drift":
        stage = "needs_action"
        badge = "danger"
        title = "Twilio state needs reconciliation"
        summary = (
            recovery_state.get("summary")
            if recovery_state
            else "Twilio still has live resources, but the app is bound to stale identifiers."
        )
        next_step = "Review the live Twilio resources and use Reconcile Twilio state before making more A2P changes."
        eta = "Live SMS stays paused until the app is rebound to the correct Twilio resources."
    elif recovery_type == "missing_campaign" or synthetic_missing_campaign:
        stage = "needs_action"
        badge = "danger"
        title = "Campaign creation required"
        summary = (
            recovery_state.get("summary")
            if recovery_state
            else "Twilio approved the brand package, but the Messaging Service does not have a campaign attached yet."
        )
        next_step = "Confirm the live Twilio resources, then create the campaign explicitly from the platform."
        eta = "Live SMS stays paused until the new campaign enters review."
    elif recovery_type == "transient_connectivity":
        stage = "reviewing"
        badge = "warning text-dark"
        title = "Temporary Twilio sync issue"
        summary = (
            recovery_state.get("summary")
            if recovery_state
            else "Twilio could not be reached during the latest status refresh."
        )
        next_step = "Retry Refresh Status after Twilio connectivity stabilizes. Do not reset approved resources."
        eta = "Retry after connectivity stabilizes."
    elif onboarding.onboarding_status == "canceled":
        stage = "canceled"
        badge = "secondary"
        title = "Canceled"
        summary = "The A2P submission was canceled."
        next_step = "Submit a fresh packet when the business is ready."
        eta = "No carrier review is active."
    elif can_send or onboarding.onboarding_status == "approved":
        stage = "approved"
        badge = "success"
        title = "Live SMS approved"
        summary = "Carrier approval and sender setup are complete for this workspace."
        next_step = "You can start controlled live sends now."
        eta = "Ready immediately."
    elif onboarding.onboarding_status in {"queued", "processing"}:
        stage = "submitted"
        badge = "info"
        title = "Submitted to Twilio"
        summary = "The A2P packet has been queued and is moving into review."
        next_step = "No manual action is needed yet. The background worker will keep syncing status."
        eta = "Usually moves into review on the next sync."
    elif failure_reason or onboarding.onboarding_status in {"error", "rejected", "needs_action"}:
        stage = "needs_action"
        badge = "danger"
        title = "Needs action"
        summary = failure_reason or "Twilio flagged the registration details for correction."
        next_step = "Correct the packet, then resubmit or refresh the onboarding record."
        eta = "Live SMS stays paused until the corrected packet is approved."
    elif brand_status in A2P_BRAND_APPROVED_STATUSES and campaign_status in A2P_CAMPAIGN_APPROVED_STATUSES:
        stage = "reviewing"
        badge = "warning text-dark"
        title = "Final sender setup"
        summary = "Brand and campaign are approved. Sender assignment is still finishing."
        next_step = "Finish workspace setup now. Live SMS unlocks after the sender sync completes."
        eta = "Usually finishes shortly after approval sync."
    elif onboarding.onboarding_status in {"pending"} or brand_status in A2P_REVIEWING_STATUSES or campaign_status in A2P_REVIEWING_STATUSES:
        stage = "reviewing"
        badge = "warning text-dark"
        title = "Carrier review in progress"
        summary = "Twilio and downstream carriers are reviewing the registration package."
        next_step = "Keep onboarding the workspace while you wait for approval."
        eta = "Reviews can take from hours to several business days."
    else:
        stage = "draft"
        badge = "secondary"
        title = "Not submitted yet"
        summary = "The A2P registration packet has not been submitted."
        next_step = "Submit the business details to start Twilio review."
        eta = "Nothing is under review yet."

    return {
        "stage": stage,
        "badge": badge,
        "title": title,
        "summary": summary,
        "next_step": next_step,
        "eta": eta,
        "failure_reason": failure_reason,
        "failure_code": failure_code,
        "brand_status": _humanize_status(brand_status, fallback="not submitted"),
        "campaign_status": _humanize_status(campaign_status, fallback="not submitted"),
        "number_status": _humanize_status(number_status, fallback="not started"),
        "last_checked_at": onboarding.last_synced_at,
        "has_submission": has_submission,
        "is_waiting": stage in {"submitted", "reviewing"},
        "show_wait_state": stage in {"submitted", "reviewing", "needs_action"} and not can_send,
        "event_streams_enabled": a2p_event_streams_enabled(),
        "external_managed": False,
        "recovery_state": recovery_state or None,
        "can_reconcile": bool(recovery_state and recovery_state.get("recommended_action") == "reconcile"),
        "can_create_campaign": bool(recovery_state and recovery_state.get("recommended_action") == "create_campaign") or synthetic_missing_campaign,
        "campaign_fee_warning": (
            "Creating a new Twilio campaign may trigger another campaign vetting fee. Confirm the approved packet is correct before continuing."
            if ((recovery_state and recovery_state.get("recommended_action") == "create_campaign") or synthetic_missing_campaign)
            else None
        ),
        "console_campaign_id": _console_campaign_id(_load_status_payload(onboarding).get("console_campaign_id")),
        "twilio_read_context": _twilio_read_context(onboarding),
    }


def ingest_a2p_event_stream_payload(payload: Any, authenticated_organization_id: int) -> dict[str, int]:
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        events = payload["events"]
    elif isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        events = [payload]
    else:
        raise ProviderProvisioningError("Twilio Event Streams payload must be a JSON object or list.")

    summary = {
        "events_seen": 0,
        "events_applied": 0,
        "events_ignored": 0,
        "events_duplicate": 0,
        "events_out_of_order": 0,
    }
    should_queue = set()

    for event in events:
        summary["events_seen"] += 1
        if not isinstance(event, dict):
            summary["events_ignored"] += 1
            continue

        event_type = _clean_text(event.get("type"), lowercase=True) or ""
        data = event.get("data")
        if not event_type or not isinstance(data, dict):
            summary["events_ignored"] += 1
            continue

        onboarding, profile = _find_onboarding_for_event(
            event_type,
            data,
            authenticated_organization_id,
        )
        if onboarding is None:
            summary["events_ignored"] += 1
            continue
        if profile is None and onboarding.organization is not None:
            profile = onboarding.organization.messaging_profile or ensure_messaging_profile(onboarding.organization)
        if profile is None:
            summary["events_ignored"] += 1
            continue

        topic = _event_stream_topic(event_type)
        if topic is None:
            summary["events_ignored"] += 1
            continue

        _record_observed_identifier_drift(
            onboarding,
            profile,
            observed_ids={
                "subaccount_sid": _event_value(data, "accountsid", "account_sid"),
                "messaging_service_sid": _event_value(data, "messagingservicesid", "messageservicesid", "service_sid"),
                "brand_registration_sid": _clean_text(data.get("brandsid")),
                "campaign_sid": _clean_text(data.get("campaignsid")),
                "console_campaign_id": _event_value(data, "campaignid", "campaign_id", "externalcampaignid", "external_campaign_id"),
                "phone_number_sid": _clean_text(data.get("phonenumbersid")),
                "brand_tcr_id": _event_value(data, "brandtcrid", "brand_tcr_id", "tcrid", "brandtcr_id"),
            },
        )
        status_payload, event_stream, recent_event_ids = _event_stream_state(onboarding)
        event_id = _clean_text(event.get("id"))
        if event_id and event_id in recent_event_ids:
            summary["events_duplicate"] += 1
            continue

        event_timestamp = _event_stream_timestamp(data)
        topic_state = event_stream.get(topic)
        if not isinstance(topic_state, dict):
            topic_state = {}
            event_stream[topic] = topic_state
        previous_timestamp = topic_state.get("timestamp")
        if isinstance(previous_timestamp, str) and previous_timestamp.isdigit():
            previous_timestamp = int(previous_timestamp)
        if isinstance(previous_timestamp, int) and previous_timestamp and event_timestamp and event_timestamp < previous_timestamp:
            _record_recent_event_id(recent_event_ids, event_id)
            _store_status_payload(onboarding, status_payload)
            summary["events_out_of_order"] += 1
            continue

        status = _event_stream_status(topic, event_type, data)
        failure_reason, failure_code = _event_stream_failure(topic, data)
        topic_state.update(
            {
                "timestamp": event_timestamp or previous_timestamp or 0,
                "event_id": event_id,
                "event_type": event_type,
                "status": status,
                "failure_reason": failure_reason,
            }
        )
        if topic == "brand":
            onboarding.brand_status = status
            status_payload["brand_failure_reason"] = failure_reason
            if failure_code:
                status_payload["brand_failure_code"] = failure_code
        elif topic == "campaign":
            onboarding.campaign_status = status
            status_payload["campaign_failure_reason"] = failure_reason
            if failure_code:
                status_payload["campaign_failure_code"] = failure_code
        else:
            status_payload["number_status"] = status
            status_payload["number_failure_reason"] = failure_reason
            if failure_code:
                status_payload["number_failure_code"] = failure_code
        onboarding.failure_code = (
            _clean_text(status_payload.get("number_failure_code"))
            or _clean_text(status_payload.get("campaign_failure_code"))
            or _clean_text(status_payload.get("brand_failure_code"))
            or None
        )

        _record_recent_event_id(recent_event_ids, event_id)
        _store_status_payload(onboarding, status_payload)
        onboarding.last_synced_at = utc_now()
        profile.provider_last_checked_at = utc_now()
        _apply_status_snapshot(onboarding, profile, allow_number_setup=False)
        if onboarding.brand_status in A2P_BRAND_APPROVED_STATUSES and onboarding.campaign_status in A2P_CAMPAIGN_APPROVED_STATUSES:
            should_queue.add(onboarding.organization_id)
        summary["events_applied"] += 1

    for organization_id in should_queue:
        try:
            _queue_job("app.tasks.process_a2p_onboarding_job", organization_id, None)
        except Exception:
            current_app.logger.warning(
                "Could not queue A2P reconcile after Event Streams update for organization_id=%s.",
                organization_id,
                exc_info=True,
            )

    return summary


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
            status = (onboarding.onboarding_status or "").strip().lower()
            if status in {"queued", "processing"}:
                process_a2p_onboarding(onboarding.organization_id)
            else:
                sync_a2p_onboarding_status(onboarding.organization_id)
            summary["records_processed"] += 1
        except ProviderProvisioningError:
            current_app.logger.exception(
                "A2P reconcile failed for organization_id=%s onboarding_id=%s",
                onboarding.organization_id,
                onboarding.id,
            )
            summary["records_failed"] += 1
    return summary
