from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from flask import current_app

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
    "non profit": "Non-profit Corporation",
    "non profit corporation": "Non-profit Corporation",
    "nonprofit": "Non-profit Corporation",
    "nonprofit corporation": "Non-profit Corporation",
    "partnership": "Partnership",
    "sole proprietor": "Sole Proprietor",
}


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
    business_industry: str | None
    business_regions: list[str]
    website_url: str | None
    social_profile_url: str | None
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


def _normalized_multiline_list(raw_value: str | None) -> list[str]:
    value = _clean_text(raw_value) or ""
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _coerce_bool(raw_value: Any) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


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


def _validate_message_samples(campaign_use_case: str, message_samples: list[str]) -> list[str]:
    if not message_samples:
        raise ProviderProvisioningError("At least one message sample is required.")
    if campaign_use_case == "MIXED" and len(message_samples) < 2:
        raise ProviderProvisioningError("Mixed-use campaigns require at least two message samples.")
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


def _build_form_data(payload: dict[str, Any], organization: Organization, *, require_declaration: bool) -> A2PFormData:
    registration_path = (payload.get("registration_path") or "standard").strip().lower()
    number_strategy = (payload.get("number_strategy") or "platform_assign").strip().lower()
    if registration_path not in A2P_REGISTRATION_PATH_VALUES:
        raise ProviderProvisioningError("Choose a valid Twilio A2P registration path.")
    if number_strategy not in A2P_NUMBER_STRATEGY_VALUES:
        raise ProviderProvisioningError("Choose a valid Twilio A2P number strategy.")
    business_name = _clean_text(payload.get("business_name")) or organization.name or ""
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
    if not _clean_text(payload.get("website_url")):
        raise ProviderProvisioningError("Business website is required for A2P onboarding.")
    if not campaign_description:
        raise ProviderProvisioningError("Campaign description is required.")
    if not message_flow:
        raise ProviderProvisioningError("Message flow is required.")

    mobile_number = _clean_text(payload.get("mobile_number"))
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
    declaration_accepted = _coerce_bool(payload.get("declaration_accepted"))
    if require_declaration and not declaration_accepted:
        raise ProviderProvisioningError("You must confirm the business declaration before submitting A2P onboarding.")

    return A2PFormData(
        registration_path=registration_path,
        number_strategy=number_strategy,
        business_name=business_name,
        business_type=business_type,
        business_industry=business_industry,
        business_regions=business_regions,
        website_url=_clean_text(payload.get("website_url")),
        social_profile_url=_clean_text(payload.get("social_profile_url")),
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
        opt_in_keywords=_normalized_csv_list(payload.get("opt_in_keywords"), default=DEFAULT_OPT_IN_KEYWORDS),
        opt_out_keywords=_normalized_csv_list(payload.get("opt_out_keywords"), default=DEFAULT_OPT_OUT_KEYWORDS),
        help_keywords=_normalized_csv_list(payload.get("help_keywords"), default=DEFAULT_HELP_KEYWORDS),
        has_embedded_links=_coerce_bool(payload.get("has_embedded_links")),
        has_embedded_phone=_coerce_bool(payload.get("has_embedded_phone")),
        desired_phone_number=_clean_text(payload.get("desired_phone_number")),
        desired_phone_number_sid=desired_phone_number_sid,
        campaign_verify_token=_clean_text(payload.get("campaign_verify_token")),
        declaration_accepted=declaration_accepted,
    )


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
    onboarding.business_type = form_data.business_type
    onboarding.business_identity = _business_identity(form_data.registration_path)
    onboarding.business_industry = form_data.business_industry
    onboarding.business_registration_identifier = form_data.business_registration_identifier
    onboarding.business_registration_number_encrypted = (
        encrypt_provider_secret(form_data.business_registration_number)
        if form_data.business_registration_number
        else None
    )
    onboarding.business_regions_json = json.dumps(form_data.business_regions)
    onboarding.website_url = form_data.website_url
    onboarding.social_profile_url = form_data.social_profile_url
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
        },
        sort_keys=True,
    )
    profile.business_type = form_data.business_type
    profile.use_case = form_data.campaign_description[:120]
    if queue_submission:
        onboarding.onboarding_status = "queued"
        onboarding.brand_status = None
        onboarding.campaign_status = None
        onboarding.verification_status = None
        onboarding.submitted_at = utc_now()
        onboarding.canceled_at = None
        onboarding.approved_at = None
        onboarding.last_error = None
        onboarding.failure_code = None
        profile.last_provision_error = None
        if profile.provider_status != "suspended":
            profile.set_provider_status("pending")
    elif onboarding.onboarding_status not in {"pending", "processing", "queued", "approved"}:
        onboarding.onboarding_status = "draft"


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


def _create_a2p_campaign(onboarding: OrganizationA2POnboarding, profile: OrganizationMessagingProfile) -> None:
    if onboarding.campaign_sid:
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
    campaign = _build_subaccount_client(profile).messaging.v1.services(profile.messaging_service_sid).us_app_to_person.create(
        brand_registration_sid=onboarding.brand_registration_sid,
        description=onboarding.campaign_description or onboarding.business_name,
        message_flow=onboarding.message_flow or onboarding.campaign_description or onboarding.business_name,
        message_samples=message_samples,
        us_app_to_person_usecase=campaign_use_case,
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
        brand_status, campaign_status = _sync_remote_status(onboarding, profile)
        onboarding.brand_status = brand_status
        onboarding.campaign_status = campaign_status
        profile.provider_last_checked_at = utc_now()

        normalized_brand_status = brand_status or "pending"
        normalized_campaign_status = _resolved_campaign_status(onboarding, campaign_status)
        brand_ready_for_campaign = normalized_brand_status in {"approved", "registered"}

        if brand_ready_for_campaign and not onboarding.campaign_sid:
            _create_a2p_campaign(onboarding, profile)
            db.session.commit()
            brand_status, campaign_status = _sync_remote_status(onboarding, profile)
            onboarding.brand_status = brand_status
            onboarding.campaign_status = campaign_status
            profile.provider_last_checked_at = utc_now()
            normalized_brand_status = brand_status or "pending"
            normalized_campaign_status = _resolved_campaign_status(onboarding, campaign_status)

        if normalized_brand_status in {"failed", "rejected"} or normalized_campaign_status in {"failed", "rejected"}:
            _set_status(
                onboarding,
                profile,
                onboarding_status="rejected",
                brand_status=normalized_brand_status,
                campaign_status=normalized_campaign_status,
                error_message=_status_failure_reason(onboarding) or "Twilio rejected the A2P registration.",
            )
        elif brand_ready_for_campaign and normalized_campaign_status in {"approved", "active"}:
            onboarding.approved_at = utc_now()
            onboarding.last_synced_at = utc_now()
            if onboarding.number_strategy == "platform_assign":
                _set_status(
                    onboarding,
                    profile,
                    onboarding_status="approved",
                    brand_status=normalized_brand_status,
                    campaign_status=normalized_campaign_status,
                )
                profile.sender_review_status = "pending"
                profile.last_provision_error = None
                if profile.provider_status != "suspended":
                    profile.set_provider_status("pending")
            else:
                _complete_number_setup(onboarding, profile, actor_user_id)
                onboarding.onboarding_status = "approved"
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
