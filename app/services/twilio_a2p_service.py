from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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
from app.services.twilio_service import (
    ProviderProvisioningError,
    _build_subaccount_client,
    _client_for_profile,
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
    base_url = (current_app.config.get("SAAS_BASE_URL") or "").strip().rstrip("/")
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
    return value[:255], None


def _http_fetch_text(url: str) -> tuple[int | None, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ITWingmanA2PValidator/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    timeout = float(current_app.config.get("TWILIO_A2P_URL_VALIDATION_TIMEOUT", 5))
    try:
        with urlopen(request, timeout=timeout) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            body = response.read(131072).decode("utf-8", errors="ignore")
            return status_code, body
    except HTTPError as exc:
        body = exc.read(8192).decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
        return exc.code, body
    except URLError:
        return None, ""


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
        if _is_reserved_test_host(url):
            field_result = results["fields"][field_name]
            field_result["status_code"] = 200
            field_result["reachable"] = True
            field_result["valid"] = True
            continue
        status_code, body = _http_fetch_text(url)
        field_result = results["fields"][field_name]
        field_result["status_code"] = status_code
        field_result["reachable"] = status_code == 200
        if status_code != 200:
            field_result["error"] = f"returned HTTP {status_code or 'unreachable'}"
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


def _status_value(raw_value: Any) -> str | None:
    normalized = str(raw_value or "").strip()
    return normalized.lower() or None


def _friendly_provider_error_message(raw_message: str) -> str:
    message = (raw_message or "").strip()
    if "Secondary Customer Profile for direct_customer can only be created through Twilio console." in message:
        return (
            "Twilio rejected automated secondary profile creation because the parent account is still set up as a "
            "Direct Customer profile. Reclassify the primary Twilio Customer Profile to ISV Reseller or Partner "
            "in Twilio Trust Hub or through Twilio Support, then retry onboarding."
        )
    return message


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
    return {
        "sid": _clean_text(getattr(campaign, "sid", None)),
        "status": _status_value(getattr(campaign, "campaign_status", None) or getattr(campaign, "status", None)),
        "brand_registration_sid": _clean_text(getattr(campaign, "brand_registration_sid", None)),
        "use_case": _campaign_use_case_value(
            getattr(campaign, "us_app_to_person_usecase", None) or getattr(campaign, "campaign_usecase", None)
        ),
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

    destination = a2p_event_stream_destination_url(organization)
    if not destination:
        profile.event_stream_status = "error"
        profile.event_stream_error = "SAAS_BASE_URL must be configured before Twilio Event Streams can be enabled."
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


def _find_onboarding_for_event(event_type: str, data: dict[str, Any]) -> tuple[OrganizationA2POnboarding | None, OrganizationMessagingProfile | None]:
    topic = _event_stream_topic(event_type)
    if topic is None:
        return None, None

    if topic == "brand":
        brand_sid = _clean_text(data.get("brandsid"))
        if brand_sid:
            onboarding = OrganizationA2POnboarding.query.filter_by(brand_registration_sid=brand_sid).first()
            if onboarding is not None:
                organization = db.session.get(Organization, onboarding.organization_id)
                profile = organization.messaging_profile if organization is not None else None
                return onboarding, profile

    campaign_sid = _clean_text(data.get("campaignsid"))
    if campaign_sid:
        onboarding = OrganizationA2POnboarding.query.filter_by(campaign_sid=campaign_sid).first()
        if onboarding is not None:
            organization = db.session.get(Organization, onboarding.organization_id)
            profile = organization.messaging_profile if organization is not None else None
            return onboarding, profile

    phone_number_sid = _clean_text(data.get("phonenumbersid"))
    if phone_number_sid:
        profile = OrganizationMessagingProfile.query.filter_by(phone_number_sid=phone_number_sid).first()
        if profile is not None and profile.organization is not None:
            return profile.organization.a2p_onboarding, profile

    return None, None


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
            return

        if allow_number_setup:
            _complete_number_setup(onboarding, profile, actor_user_id)
            onboarding.onboarding_status = "approved"
            onboarding.approved_at = utc_now()
            if onboarding.brand_registration_mode == "standard":
                onboarding.upgraded_at = onboarding.upgraded_at or utc_now()
            onboarding.last_synced_at = utc_now()
            onboarding.verification_status = number_status or onboarding.verification_status
            onboarding.last_error = None
            profile.sender_review_status = "approved"
            profile.consent_acknowledged_at = profile.consent_acknowledged_at or utc_now()
            if profile.provider_status != "suspended":
                profile.set_provider_status("active")
            profile.last_provision_error = None
            return

        _set_status(
            onboarding,
            profile,
            onboarding_status="approved" if profile.can_send else "pending",
            brand_status=normalized_brand_status,
            campaign_status=normalized_campaign_status,
            verification_status=number_status or onboarding.verification_status,
        )
        if onboarding.brand_registration_mode == "standard" and onboarding.onboarding_status == "approved":
            onboarding.upgraded_at = onboarding.upgraded_at or utc_now()
        if profile.provider_status != "suspended":
            profile.set_provider_status("active" if profile.can_send else "pending")
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
    if queue_submission:
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
        },
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
    if not error_message:
        onboarding.failure_code = None
    profile.provider_last_checked_at = utc_now()
    if not error_message:
        profile.last_provision_error = None
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


def _status_failure_reason(onboarding: OrganizationA2POnboarding) -> str | None:
    status_payload = _load_status_payload(onboarding)
    return status_payload.get("campaign_failure_reason") or status_payload.get("brand_failure_reason")


def _resolved_campaign_status(onboarding: OrganizationA2POnboarding, campaign_status: str | None) -> str | None:
    if campaign_status:
        return campaign_status
    return "pending" if onboarding.campaign_sid else None


def _trusthub_status_callback_url() -> str:
    base_url = (current_app.config.get("SAAS_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        raise ProviderProvisioningError("SAAS_BASE_URL must be configured to receive Twilio Trust Hub status callbacks.")
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
    address = client.addresses.create(
        customer_name=onboarding.business_name,
        friendly_name=f"{onboarding.business_name[:48]} business address",
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
    client = _build_subaccount_client(profile)
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

    customer_profile_policy_sid, trust_product_policy_sid = _policy_sids(onboarding.registration_path)
    if not onboarding.customer_profile_sid:
        customer_profile = client.trusthub.v1.customer_profiles.create(
            policy_sid=customer_profile_policy_sid,
            friendly_name=f"{onboarding.business_name} Customer Profile",
            email=notification_email,
            status_callback=trusthub_status_callback,
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
                    "business_title": onboarding.business_title or "Owner",
                    "job_position": onboarding.job_position or "Other",
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
                    "business_title": onboarding.business_title or "Owner",
                    "job_position": onboarding.job_position or "Other",
                },
            )
            status_payload["authorized_representative_sid"] = authorized_rep.sid
        if address_sid and not status_payload.get("supporting_document_sid"):
            supporting_document = client.trusthub.v1.supporting_documents.create(
                friendly_name=onboarding.business_name[:64],
                type="customer_profile_address",
                attributes={"address_sids": address_sid},
            )
            status_payload["supporting_document_sid"] = supporting_document.sid
            onboarding.supporting_document_sid = supporting_document.sid

    if not onboarding.trust_product_sid:
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"{onboarding.business_name} A2P Trust Product",
            policy_sid=trust_product_policy_sid,
            email=notification_email,
            status_callback=trusthub_status_callback,
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
            attributes={"company_type": _messaging_profile_company_type(onboarding.registration_path)},
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

    _store_status_payload(onboarding, status_payload)


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
    client = _build_subaccount_client(profile)
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
    brand_failure_code = None
    campaign_failure_code = None
    status_payload = _load_status_payload(onboarding)

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
        status_payload["campaign_errors"] = getattr(campaign, "errors", None)
        status_payload["campaign_failure_reason"] = campaign_failure_reason
        status_payload["campaign_failure_code"] = campaign_failure_code

    onboarding.failure_code = campaign_failure_code or brand_failure_code or None

    _store_status_payload(onboarding, status_payload)
    return brand_status, campaign_status


def _complete_number_setup(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile, actor_user_id: int | None) -> None:
    if profile.from_number and profile.phone_number_sid:
        return
    if onboarding.number_strategy == "platform_assign":
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
        db.session.commit()
        ensure_a2p_event_stream_subscription(organization, profile)
        db.session.commit()
        brand_status, campaign_status = _sync_remote_status(onboarding, profile)
        onboarding.brand_status = brand_status
        onboarding.campaign_status = campaign_status
        profile.provider_last_checked_at = utc_now()
        normalized_brand_status = _status_value(brand_status) or "pending"
        brand_ready_for_campaign = normalized_brand_status in A2P_BRAND_READY_FOR_CAMPAIGN_STATUSES
        if brand_ready_for_campaign and not onboarding.campaign_sid:
            _create_a2p_campaign(onboarding, profile, actor_user_id=actor_user_id)
            db.session.commit()
            brand_status, campaign_status = _sync_remote_status(onboarding, profile)
            onboarding.brand_status = brand_status
            onboarding.campaign_status = campaign_status
            profile.provider_last_checked_at = utc_now()
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
        if can_send or (
            brand_status in A2P_BRAND_APPROVED_STATUSES
            and campaign_status in A2P_CAMPAIGN_APPROVED_STATUSES
        ):
            stage = "approved"
            badge = "success"
            title = "External A2P approved"
            summary = "This workspace uses a customer-managed Twilio account with an approved external brand and campaign."
            next_step = "Keep A2P and messaging compliance managed in the customer Twilio account."
            eta = "Ready immediately."
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
            "show_wait_state": stage in {"reviewing", "needs_action"} and not can_send,
            "event_streams_enabled": a2p_event_streams_enabled(),
            "external_managed": True,
            "campaign_sid": campaign_sid,
            "brand_registration_sid": (
                status_payload.get("brand_registration_sid")
                if status_payload.get("brand_registration_sid")
                else (onboarding.brand_registration_sid if onboarding is not None else None)
            ),
            "messaging_service_sid": messaging_service_sid,
            "phone_number_sid": phone_number_sid,
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
        }

    brand_status = _status_value(onboarding.brand_status)
    campaign_status = _status_value(onboarding.campaign_status)
    number_status = _latest_number_status(onboarding)
    failure_reason = _latest_failure_message(onboarding)
    failure_code = _latest_failure_code(onboarding)
    has_submission = onboarding.onboarding_status not in {"draft", "canceled"}
    can_send = bool(profile is not None and profile.can_send)

    if onboarding.onboarding_status == "canceled":
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
    }


def ingest_a2p_event_stream_payload(payload: Any) -> dict[str, int]:
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

        onboarding, profile = _find_onboarding_for_event(event_type, data)
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
