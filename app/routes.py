import csv
import io
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import DateTime, Integer, String, Text, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import csrf, db
from app.auth import home_endpoint_for_user, require_roles

from app.models import (
    AuthEvent,
    AppUser,
    CommunityMember,
    Event,
    EventRegistration,
    InboxMessage,
    InboxThread,
    KeywordAutomationRule,
    MessageLog,
    Organization,
    OrganizationA2POnboarding,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationMessagingProfile,
    OrganizationSubscription,
    ScheduledMessage,
    SuppressedContact,
    SurveyFlow,
    SurveyResponse,
    SurveySession,
    UnsubscribedContact,
    utc_now,
)
from app.services.auth_security_service import (
    is_password_reused,
    password_policy_errors,
    record_auth_event,
    store_password_history,
)
from app.services.billing_service import (
    clear_complimentary_subscription,
    create_billing_portal_session,
    create_checkout_session,
    is_fake_checkout_session_id,
    mark_subscription_complimentary,
    organization_can_send,
    process_stripe_webhook_event,
    refresh_subscription_from_stripe,
    subscription_status_allows_sending,
    subscription_status_is_complimentary,
    sync_checkout_session_by_id,
)
from app.services.provider_secret_service import decrypt_provider_secret
from app.services.inbox_service import (
    delete_survey_flow_with_dependencies,
    delete_messages_in_thread,
    delete_thread_with_dependencies,
    mark_thread_read,
    parse_survey_questions,
    process_inbound_sms,
    send_thread_reply,
    update_thread_contact_name,
)
from app.services.platform_operations_service import (
    enqueue_platform_service_restart_request,
    latest_platform_service_restart_request,
)
from app.services.recipient_service import (
    filter_suppressed_recipients,
    filter_unsubscribed_recipients,
    get_unsubscribed_phone_set,
)
from app.services.twilio_service import validate_inbound_signature_detailed
from app.services.twilio_a2p_service import (
    a2p_business_industry_choices,
    a2p_business_region_choices,
    a2p_business_type_choices,
    a2p_campaign_use_case_choices,
    a2p_job_position_choices,
    a2p_number_strategy_choices,
    a2p_registration_path_choices,
    a2p_registration_identifier_choices,
    cancel_a2p_onboarding,
    describe_a2p_onboarding,
    ensure_a2p_onboarding,
    ensure_a2p_event_stream_subscription,
    hosted_a2p_compliance_urls,
    ingest_a2p_event_stream_payload,
    refresh_a2p_onboarding,
    save_a2p_onboarding_draft,
    submit_a2p_onboarding,
)
from app.services.twilio_service import (
    ensure_messaging_profile,
    ProviderProvisioningError,
    provision_org,
    release_sender,
    resolve_messaging_profile,
    resume_org,
    save_customer_managed_profile,
    sync_sender_assignment,
    suspend_org,
)
from app.services.security_alert_service import send_security_alert
from app.sort_utils import normalize_sort_params
from app.tenant import organization_context, saas_mode_enabled, without_tenant_scope
from app.utils import (
    ALLOWED_TEMPLATE_TOKENS,
    as_utc_datetime,
    escape_like,
    find_invalid_template_tokens,
    is_safe_url,
    normalize_keyword,
    normalize_phone,
    parse_recipients_csv,
    phone_digits_sql,
    sanitize_csv_cell,
    validate_phone,
)

bp = Blueprint('main', __name__)
CSV_IMPORT_ERROR_FLASH = 'Could not process CSV file. Please verify the format and try again.'
BLAST_QUEUE_UNAVAILABLE_FLASH = (
    'Background queue is unavailable right now. The blast was not queued. Check Redis/worker health and try again.'
)
BACKFILL_QUEUE_UNAVAILABLE_FLASH = (
    'Background queue is unavailable right now. Backfill was not queued. Check Redis/worker health and try again.'
)


def _is_explicit_production() -> bool:
    return os.environ.get("FLASK_ENV", "").strip().lower() == "production"


def _normalize_org_messaging_values(
    sender_number: str | None,
    messaging_service_sid: str | None,
) -> tuple[str | None, str | None]:
    normalized_sender = normalize_phone(sender_number) if sender_number else None
    normalized_service_sid = messaging_service_sid.strip().upper() if messaging_service_sid else None
    return normalized_sender or None, normalized_service_sid or None

def _messaging_profile_status(
    sender_number: str | None,
    messaging_service_sid: str | None,
) -> str:
    return 'active' if (sender_number or messaging_service_sid) else 'pending'


def _a2p_form_defaults(onboarding) -> dict[str, str]:
    def _json_text(value: str | None, *, separator: str) -> str:
        if not value:
            return ""
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, list):
            return ""
        return separator.join(str(item) for item in parsed if str(item).strip())

    def _json_bool(value: str | None, key: str) -> bool:
        if not value:
            return False
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return False
        if not isinstance(parsed, dict):
            return False
        return bool(parsed.get(key))

    return {
        "message_samples": _json_text(getattr(onboarding, "message_samples_json", None), separator="\n"),
        "opt_in_keywords": _json_text(getattr(onboarding, "opt_in_keywords_json", None), separator=","),
        "opt_out_keywords": _json_text(getattr(onboarding, "opt_out_keywords_json", None), separator=","),
        "help_keywords": _json_text(getattr(onboarding, "help_keywords_json", None), separator=","),
        "business_regions": _json_text(getattr(onboarding, "business_regions_json", None), separator=","),
        "has_embedded_links": _json_bool(getattr(onboarding, "raw_submission_json", None), "has_embedded_links"),
        "has_embedded_phone": _json_bool(getattr(onboarding, "raw_submission_json", None), "has_embedded_phone"),
    }


def _json_dict(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _a2p_source_defaults(organization: Organization, onboarding: OrganizationA2POnboarding | None) -> dict[str, object]:
    hosted_urls = hosted_a2p_compliance_urls(organization)
    validation = _json_dict(getattr(onboarding, "external_url_validation_json", None))
    source_mode = (
        getattr(onboarding, "submission_source_mode", None)
        or ("external_site" if getattr(onboarding, "external_website_url", None) else "hosted_fallback")
    )
    return {
        "legal_business_name": (
            getattr(onboarding, "legal_business_name", None)
            or getattr(onboarding, "business_name", None)
            or organization.name
        ),
        "public_brand_name": getattr(onboarding, "public_brand_name", None) or organization.name,
        "has_business_tax_id": (
            bool(onboarding.has_business_tax_id)
            if onboarding and onboarding.has_business_tax_id is not None
            else bool(getattr(onboarding, "business_registration_identifier", None))
        ),
        "has_public_website": (
            bool(onboarding.has_public_website)
            if onboarding and onboarding.has_public_website is not None
            else bool(getattr(onboarding, "external_website_url", None))
        ),
        "brand_registration_mode": getattr(onboarding, "brand_registration_mode", None) or "low_volume_standard",
        "submission_source_mode": source_mode,
        "submission_source_reason": getattr(onboarding, "submission_source_reason", None),
        "external_website_url": (
            getattr(onboarding, "external_website_url", None)
            or (
                getattr(onboarding, "website_url", None)
                if source_mode == "external_site"
                else ""
            )
        ),
        "external_privacy_policy_url": (
            getattr(onboarding, "external_privacy_policy_url", None)
            or (
                getattr(onboarding, "privacy_policy_url", None)
                if source_mode == "external_site"
                else ""
            )
        ),
        "external_terms_and_conditions_url": (
            getattr(onboarding, "external_terms_and_conditions_url", None)
            or (
                getattr(onboarding, "terms_and_conditions_url", None)
                if source_mode == "external_site"
                else ""
            )
        ),
        "external_cta_proof_url": (
            getattr(onboarding, "external_cta_proof_url", None)
            or (
                getattr(onboarding, "cta_proof_url", None)
                if source_mode == "external_site"
                else ""
            )
        ),
        "hosted_urls": hosted_urls,
        "active_urls": {
            "website_url": getattr(onboarding, "website_url", None) or hosted_urls["website_url"],
            "privacy_policy_url": getattr(onboarding, "privacy_policy_url", None) or hosted_urls["privacy_policy_url"],
            "terms_and_conditions_url": (
                getattr(onboarding, "terms_and_conditions_url", None) or hosted_urls["terms_and_conditions_url"]
            ),
            "cta_proof_url": getattr(onboarding, "cta_proof_url", None) or hosted_urls["cta_proof_url"],
        },
        "external_validation": validation,
    }
def _validate_org_messaging_profile_input(
    sender_number: str | None,
    messaging_service_sid: str | None,
    *,
    organization_id: int | None = None,
) -> tuple[str | None, str | None, str | None]:
    normalized_sender, normalized_service_sid = _normalize_org_messaging_values(
        sender_number,
        messaging_service_sid,
    )
    existing_profile = (
        OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).first()
        if organization_id is not None
        else None
    )

    if sender_number and not validate_phone(normalized_sender):
        return 'Dedicated sender number must be a valid E.164 phone number.', None, None

    if normalized_service_sid and not normalized_service_sid.startswith('MG'):
        return 'Twilio Messaging Service SID must start with MG.', None, None

    if normalized_sender:
        duplicate_sender = OrganizationMessagingProfile.query.filter_by(from_number=normalized_sender).first()
        if duplicate_sender and (existing_profile is None or duplicate_sender.organization_id != existing_profile.organization_id):
            return 'That sender number is already assigned to another organization.', None, None

    if normalized_service_sid:
        duplicate_service = OrganizationMessagingProfile.query.filter_by(
            messaging_service_sid=normalized_service_sid
        ).first()
        if duplicate_service and (existing_profile is None or duplicate_service.organization_id != existing_profile.organization_id):
            return 'That Twilio Messaging Service SID is already assigned to another organization.', None, None

    inbound_identity = normalized_sender or normalized_service_sid
    if inbound_identity:
        duplicate_inbound_identity = OrganizationMessagingProfile.query.filter_by(
            inbound_identity=inbound_identity
        ).first()
        if duplicate_inbound_identity and (
            existing_profile is None
            or duplicate_inbound_identity.organization_id != existing_profile.organization_id
        ):
            return 'That inbound messaging identity is already assigned to another organization.', None, None

    return None, normalized_sender, normalized_service_sid


def _normalized_provider_mode(value: str | None, *, default: str = "platform_managed") -> str:
    normalized = (value or default).strip().lower()
    if normalized not in {"platform_managed", "customer_managed"}:
        return default
    return normalized


def _customer_managed_auth_token_for_save(
    messaging_profile: OrganizationMessagingProfile,
    *,
    requested_account_sid: str | None,
    raw_auth_token: str | None,
) -> str | None:
    normalized_auth_token = (raw_auth_token or "").strip()
    if normalized_auth_token:
        return normalized_auth_token

    existing_account_sid = (messaging_profile.twilio_account_sid or "").strip().upper()
    requested = (requested_account_sid or "").strip().upper()
    if requested and existing_account_sid and requested != existing_account_sid:
        return None

    return decrypt_provider_secret(messaging_profile.twilio_auth_token_encrypted)


def _sync_customer_managed_onboarding_state(
    organization: Organization,
    validation_result,
) -> None:
    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    campaign_status = (validation_result.campaign_status or "").strip().lower() or None
    brand_status = (validation_result.brand_status or "").strip().lower() or None
    failure_reason = (validation_result.campaign_failure_reason or "").strip() or None
    failure_code = (validation_result.campaign_failure_code or "").strip() or None

    onboarding.raw_status_json = json.dumps(
        {
            "external_managed": True,
            "provider_mode": "customer_managed",
            "twilio_account_sid": validation_result.account_sid,
            "messaging_service_sid": validation_result.messaging_service_sid,
            "phone_number_sid": validation_result.phone_number_sid,
            "from_number": validation_result.from_number,
            "campaign_sid": validation_result.campaign_sid,
            "campaign_status": validation_result.campaign_status,
            "campaign_failure_reason": failure_reason,
            "campaign_failure_code": failure_code,
            "brand_registration_sid": validation_result.brand_registration_sid,
            "brand_status": validation_result.brand_status,
        },
        sort_keys=True,
    )
    onboarding.campaign_sid = validation_result.campaign_sid or onboarding.campaign_sid
    onboarding.brand_registration_sid = (
        validation_result.brand_registration_sid or onboarding.brand_registration_sid
    )
    onboarding.campaign_status = campaign_status or onboarding.campaign_status or "approved"
    onboarding.brand_status = brand_status or onboarding.brand_status or "approved"
    onboarding.last_synced_at = utc_now()
    onboarding.last_error = failure_reason
    onboarding.failure_code = failure_code
    if campaign_status in {"approved", "active", "verified"} and brand_status in {None, "approved", "registered", "verified", "vetting_verified"}:
        onboarding.onboarding_status = "approved"
        onboarding.approved_at = onboarding.approved_at or utc_now()
        onboarding.last_error = None
        onboarding.failure_code = None
    elif failure_reason or campaign_status in {"failed", "rejected", "deleted"} or brand_status in {"failed", "rejected", "registration_failed", "secondary_vetting_failed"}:
        onboarding.onboarding_status = "needs_action"
    elif validation_result.messaging_service_sid:
        onboarding.onboarding_status = "pending"
        onboarding.last_error = None
        onboarding.failure_code = None


def _remove_env_key_in_place(env_path: str, key: str) -> bool | None:
    key_prefix = f"{key}="
    try:
        with open(env_path, "r+", encoding="utf-8") as env_file:
            lines = env_file.readlines()
            filtered_lines = [
                line
                for line in lines
                if not line.strip().startswith(key_prefix)
            ]
            if len(filtered_lines) == len(lines):
                return False

            env_file.seek(0)
            env_file.writelines(filtered_lines)
            env_file.truncate()
            env_file.flush()
            os.fsync(env_file.fileno())
            return True
    except OSError:
        return None


def _find_username_conflict(username: str, *, exclude_user_id: int | None = None) -> AppUser | None:
    normalized_username = (username or "").strip().lower()
    if not normalized_username:
        return None

    query = AppUser.query.filter(func.lower(AppUser.username) == normalized_username)
    if exclude_user_id is not None:
        query = query.filter(AppUser.id != exclude_user_id)
    return query.first()


def _find_phone_conflict(
    phone: str,
    *,
    exclude_user_id: int | None = None,
    organization_id: int | None = None,
) -> AppUser | None:
    normalized_phone = (phone or "").strip()
    if not normalized_phone:
        return None

    query = AppUser.query.filter(AppUser.phone == normalized_phone)
    if exclude_user_id is not None:
        query = query.filter(AppUser.id != exclude_user_id)
    if saas_mode_enabled() and organization_id is not None:
        query = query.join(OrganizationMembership, OrganizationMembership.user_id == AppUser.id)
        query = query.filter(OrganizationMembership.organization_id == organization_id)
    return query.first()


def _organization_email_account_status(email: str) -> tuple[AppUser | None, str | None]:
    normalized_email = (email or '').strip().lower()
    if not normalized_email:
        return None, None

    existing_user = AppUser.query.filter(func.lower(AppUser.email) == normalized_email).first()
    if existing_user is None:
        return None, None
    if existing_user.is_platform_admin:
        return existing_user, 'Platform admin accounts cannot be assigned to an organization. Use a separate owner or staff email.'
    if existing_user.memberships:
        return existing_user, 'That email is already attached to an organization.'
    return existing_user, None


def _platform_admin_count() -> int:
    return AppUser.query.filter_by(is_platform_admin=True).count()


def _can_manage_platform_access(user: AppUser | None = None) -> bool:
    if not saas_mode_enabled() or not current_user.is_platform_admin:
        return False
    if user is None:
        return True
    return not user.memberships or user.is_platform_admin


def _requested_platform_admin_access(user: AppUser | None = None) -> bool:
    if not _can_manage_platform_access(user):
        return bool(getattr(user, 'is_platform_admin', False)) if user is not None else False
    return request.form.get('is_platform_admin') == 'on'


def _platform_admin_access_error(
    *,
    requested_platform_admin: bool,
    user: AppUser | None = None,
) -> str | None:
    if not saas_mode_enabled() or not current_user.is_platform_admin:
        return None

    if user is None and not requested_platform_admin:
        return (
            'Users created from the platform in SaaS mode must have platform admin access. '
            'Create owners and staff from the organization workspace.'
        )

    if requested_platform_admin and user is not None and user.memberships:
        return 'Organization users cannot be granted platform admin access. Create a separate standalone account.'

    if user is not None and user.is_platform_admin and not requested_platform_admin:
        if current_user.id == user.id:
            return 'You cannot remove your own platform admin access. Ask another platform admin to do it.'
        if _platform_admin_count() <= 1:
            return 'At least one platform admin is required.'

    return None


def _membership_role_from_user_role(role: str) -> str:
    normalized_role = (role or '').strip().lower()
    return 'owner' if normalized_role in {'admin', 'owner'} else 'staff'


def _format_datetime_display(value):
    normalized = as_utc_datetime(value)
    if normalized is None:
        return None
    return normalized.strftime('%b %d, %Y %I:%M %p UTC')


def _a2p_status_view(
    onboarding,
    messaging_profile: OrganizationMessagingProfile | None,
) -> dict:
    view = describe_a2p_onboarding(onboarding, messaging_profile)
    view['last_checked_display'] = _format_datetime_display(view.get('last_checked_at'))
    return view


def _organization_provider_mode(organization: Organization | None) -> str:
    profile = organization.messaging_profile if organization is not None else None
    mode = (profile.provider_mode if profile is not None else "platform_managed") or "platform_managed"
    return mode.strip().lower()


def _organization_uses_customer_managed_messaging(organization: Organization | None) -> bool:
    return _organization_provider_mode(organization) == "customer_managed"


def _workspace_activation_tasks(
    *,
    subscription_view: dict,
    team_ready: bool,
    total_recipients: int,
    keyword_rule_count: int,
    survey_flow_count: int,
    event_count: int,
) -> list[dict]:
    intake_ready = survey_flow_count > 0 or event_count > 0
    intake_detail = (
        f'{survey_flow_count} survey flow(s) and {event_count} event(s) ready.'
        if intake_ready
        else 'Create a survey flow or event intake before live SMS approval lands.'
    )
    return [
        {
            'label': 'Billing active',
            'detail': subscription_view['title'],
            'complete': subscription_view['can_send'],
        },
        {
            'label': 'Invite the first staff member',
            'detail': 'Optional for owner-only testing. Add staff before team launch.' if not team_ready else 'Team access is already in place.',
            'complete': team_ready,
        },
        {
            'label': 'Prepare the audience',
            'detail': (
                f'{total_recipients} recipient(s) already loaded.'
                if total_recipients > 0
                else 'Import already-consented contacts or collect event registrations now.'
            ),
            'complete': total_recipients > 0,
        },
        {
            'label': 'Configure keyword automation',
            'detail': (
                f'{keyword_rule_count} keyword rule(s) ready.'
                if keyword_rule_count > 0
                else 'Add keyword replies so inbound opt-in and help traffic is ready on day one.'
            ),
            'complete': keyword_rule_count > 0,
        },
        {
            'label': 'Set up survey or event intake',
            'detail': intake_detail,
            'complete': intake_ready,
        },
    ]


def _current_organization_id() -> int | None:
    return getattr(current_user, 'organization_id', None)


def _current_organization() -> Organization | None:
    organization_id = _current_organization_id()
    if not organization_id:
        return None
    return db.session.get(Organization, organization_id)


def _current_subscription() -> OrganizationSubscription | None:
    organization = _current_organization()
    return organization.subscription if organization is not None else None


def _organization_setup_complete(organization: Organization | None) -> bool:
    return organization_can_send(organization) and _organization_has_active_messaging(organization)


def _a2p_profile_ready(onboarding: OrganizationA2POnboarding | None) -> bool:
    if onboarding is None:
        return False
    required_values = (
        onboarding.business_name,
        onboarding.email,
        onboarding.notification_email,
        onboarding.website_url,
        onboarding.privacy_policy_url,
        onboarding.terms_and_conditions_url,
        onboarding.cta_proof_url,
        onboarding.first_name,
        onboarding.last_name,
        onboarding.business_title,
        onboarding.job_position,
        onboarding.business_type,
        onboarding.business_industry,
        onboarding.business_registration_identifier,
        onboarding.business_regions_json,
        onboarding.address_country,
        onboarding.address_line1,
        onboarding.address_city,
        onboarding.address_region,
        onboarding.address_postal_code,
        onboarding.campaign_description,
        onboarding.message_flow,
        onboarding.message_samples_json,
    )
    return all(bool(value) for value in required_values) and bool(onboarding.business_registration_number_encrypted)


def _setup_current_step(organization: Organization) -> str:
    subscription_view = _subscription_view(organization.subscription)
    if _organization_setup_complete(organization):
        return "launch"
    if not subscription_view["can_send"]:
        return "billing"
    if _organization_uses_customer_managed_messaging(organization):
        return "provider"
    onboarding = organization.a2p_onboarding
    if onboarding is None or onboarding.onboarding_status in {"draft", "rejected", "error", "canceled"}:
        return "compliance" if not _a2p_profile_ready(onboarding) else "review"
    if onboarding.onboarding_status in {"queued", "processing", "pending", "approved"}:
        return "launch"
    return "compliance"


def _setup_steps_view(organization: Organization) -> list[dict]:
    onboarding = organization.a2p_onboarding
    subscription_view = _subscription_view(organization.subscription)
    messaging_profile = organization.messaging_profile
    current_step = _setup_current_step(organization)
    if _organization_uses_customer_managed_messaging(organization):
        a2p_status = _a2p_status_view(onboarding, messaging_profile)
        provider_detail = (
            messaging_profile.last_provision_error
            if messaging_profile and messaging_profile.last_provision_error
            else (
                a2p_status["summary"]
                if a2p_status.get("has_submission")
                else "Platform support is validating the customer-managed Twilio account, sender, and external A2P status."
            )
        )
        launch_label = "Live in workspace" if messaging_profile and messaging_profile.can_send else "Await external activation"
        step_rows = [
            {
                "key": "account",
                "label": "Workspace account",
                "detail": "Your owner workspace is active and ready to finish setup.",
                "complete": True,
            },
            {
                "key": "billing",
                "label": "Billing activation",
                "detail": subscription_view["title"],
                "complete": subscription_view["can_send"],
            },
            {
                "key": "provider",
                "label": "External Twilio activation",
                "detail": provider_detail,
                "complete": bool(
                    (messaging_profile and messaging_profile.provider_status in {"provisioning", "active"})
                    or a2p_status.get("has_submission")
                ),
            },
            {
                "key": "launch",
                "label": launch_label,
                "detail": (
                    messaging_profile.from_number
                    if messaging_profile and messaging_profile.from_number
                    else "The workspace unlocks as soon as the customer-managed sender is active."
                ),
                "complete": messaging_profile is not None and messaging_profile.can_send,
            },
        ]
        for step in step_rows:
            step["current"] = step["key"] == current_step
        return step_rows

    launch_label = "Live in workspace" if messaging_profile and messaging_profile.can_send else "Await number assignment"
    step_rows = [
        {
            "key": "account",
            "label": "Workspace account",
            "detail": "Your owner workspace is active and ready to finish setup.",
            "complete": True,
        },
        {
            "key": "billing",
            "label": "Billing activation",
            "detail": subscription_view["title"],
            "complete": subscription_view["can_send"],
        },
        {
            "key": "compliance",
            "label": "Business profile",
            "detail": "Complete the Twilio compliance profile once.",
            "complete": _a2p_profile_ready(onboarding),
        },
        {
            "key": "review",
            "label": "Review and submit",
            "detail": onboarding.onboarding_status if onboarding else "draft",
            "complete": onboarding is not None and onboarding.submitted_at is not None,
        },
        {
            "key": "launch",
            "label": launch_label,
            "detail": (
                messaging_profile.from_number
                if messaging_profile and messaging_profile.from_number
                else "Platform support will finish number assignment after approval."
            ),
            "complete": messaging_profile is not None and messaging_profile.can_send,
        },
    ]
    for step in step_rows:
        step["current"] = step["key"] == current_step
    return step_rows


def _setup_status_payload(organization: Organization) -> dict:
    onboarding = organization.a2p_onboarding
    messaging_profile = organization.messaging_profile
    subscription_view = _subscription_view(organization.subscription)
    a2p_status = _a2p_status_view(onboarding, messaging_profile)
    onboarding_status = (
        a2p_status["stage"]
        if _organization_uses_customer_managed_messaging(organization)
        else (onboarding.onboarding_status if onboarding else "draft")
    )
    return {
        "current_step": _setup_current_step(organization),
        "setup_complete": _organization_setup_complete(organization),
        "subscription": {
            "status": subscription_view["status"],
            "title": subscription_view["title"],
            "can_send": subscription_view["can_send"],
        },
        "onboarding": {
            "status": onboarding_status,
            "title": a2p_status["title"],
            "summary": a2p_status["summary"],
            "brand_status": a2p_status.get("brand_status"),
            "campaign_status": a2p_status.get("campaign_status"),
            "failure_code": a2p_status.get("failure_code"),
            "last_error": (
                onboarding.last_error
                if onboarding and onboarding.last_error
                else (messaging_profile.last_provision_error if messaging_profile else None)
            ),
            "submitted_at": onboarding.submitted_at.isoformat() if onboarding and onboarding.submitted_at else None,
            "external_managed": bool(a2p_status.get("external_managed")),
        },
        "messaging": {
            "provider_mode": messaging_profile.provider_mode if messaging_profile else "platform_managed",
            "provider_status": messaging_profile.provider_status if messaging_profile else "pending",
            "sender_review_status": messaging_profile.sender_review_status if messaging_profile else "pending",
            "from_number": messaging_profile.from_number if messaging_profile else None,
            "messaging_service_sid": messaging_profile.messaging_service_sid if messaging_profile else None,
            "can_send": messaging_profile.can_send if messaging_profile else False,
        },
    }


def _should_reconcile_subscription(organization: Organization | None, session_id: str = "") -> bool:
    if organization is None:
        return False
    normalized_session_id = (session_id or "").strip()
    if normalized_session_id:
        return True
    if current_app.config.get('STRIPE_FAKE_CHECKOUT_ENABLED'):
        return False
    subscription = organization.subscription
    if subscription is None:
        return False
    if subscription_status_is_complimentary(subscription.status):
        return False
    return bool(subscription.stripe_customer_id or subscription.stripe_subscription_id)


def _setup_submit_payload_from_onboarding(onboarding: OrganizationA2POnboarding) -> dict[str, str]:
    defaults = _a2p_form_defaults(onboarding)
    organization = onboarding.organization or db.session.get(Organization, onboarding.organization_id)
    source_defaults = _a2p_source_defaults(organization, onboarding) if organization is not None else {
        "legal_business_name": onboarding.business_name or "",
        "public_brand_name": onboarding.business_name or "",
        "has_business_tax_id": False,
        "has_public_website": False,
        "brand_registration_mode": "low_volume_standard",
        "submission_source_mode": "hosted_fallback",
        "submission_source_reason": "",
        "external_website_url": "",
        "external_privacy_policy_url": "",
        "external_terms_and_conditions_url": "",
        "external_cta_proof_url": "",
        "active_urls": {
            "website_url": "",
            "privacy_policy_url": "",
            "terms_and_conditions_url": "",
            "cta_proof_url": "",
        },
    }
    active_urls = source_defaults["active_urls"] if organization is not None else {
        "website_url": "",
        "privacy_policy_url": "",
        "terms_and_conditions_url": "",
        "cta_proof_url": "",
    }
    business_registration_number = (
        decrypt_provider_secret(onboarding.business_registration_number_encrypted)
        if onboarding.business_registration_number_encrypted
        else ""
    )
    campaign_verify_token = (
        decrypt_provider_secret(onboarding.campaign_verify_token_encrypted)
        if onboarding.campaign_verify_token_encrypted
        else ""
    )
    return {
        "registration_path": onboarding.registration_path or "low_volume_standard",
        "brand_registration_mode": onboarding.brand_registration_mode or str(source_defaults["brand_registration_mode"]),
        "number_strategy": onboarding.number_strategy or "platform_assign",
        "business_name": onboarding.business_name or "",
        "legal_business_name": str(source_defaults["legal_business_name"]),
        "public_brand_name": str(source_defaults["public_brand_name"]),
        "business_type": onboarding.business_type or "",
        "business_industry": onboarding.business_industry or "",
        "has_business_tax_id": "on" if bool(source_defaults["has_business_tax_id"]) else "",
        "business_registration_identifier": onboarding.business_registration_identifier or "",
        "business_registration_number": business_registration_number,
        "business_regions": defaults["business_regions"] or "USA_AND_CANADA",
        "has_public_website": "on" if bool(source_defaults["has_public_website"]) else "",
        "submission_source_mode": str(source_defaults["submission_source_mode"]),
        "website_url": onboarding.website_url or active_urls["website_url"],
        "external_website_url": str(source_defaults["external_website_url"] or ""),
        "social_profile_url": onboarding.social_profile_url or "",
        "privacy_policy_url": onboarding.privacy_policy_url or active_urls["privacy_policy_url"],
        "external_privacy_policy_url": str(source_defaults["external_privacy_policy_url"] or ""),
        "terms_and_conditions_url": onboarding.terms_and_conditions_url or active_urls["terms_and_conditions_url"],
        "external_terms_and_conditions_url": str(source_defaults["external_terms_and_conditions_url"] or ""),
        "cta_proof_url": onboarding.cta_proof_url or active_urls["cta_proof_url"],
        "external_cta_proof_url": str(source_defaults["external_cta_proof_url"] or ""),
        "email": onboarding.email or "",
        "notification_email": onboarding.notification_email or onboarding.email or "",
        "phone_number": onboarding.phone_number or "",
        "mobile_number": onboarding.mobile_number or "",
        "first_name": onboarding.first_name or "",
        "last_name": onboarding.last_name or "",
        "business_title": onboarding.business_title or "",
        "job_position": onboarding.job_position or "",
        "address_country": onboarding.address_country or "US",
        "address_line1": onboarding.address_line1 or "",
        "address_line2": onboarding.address_line2 or "",
        "address_city": onboarding.address_city or "",
        "address_region": onboarding.address_region or "",
        "address_postal_code": onboarding.address_postal_code or "",
        "campaign_use_case": onboarding.campaign_use_case or "ACCOUNT_NOTIFICATION",
        "campaign_description": onboarding.campaign_description or "",
        "message_flow": onboarding.message_flow or "",
        "message_samples": defaults["message_samples"] or "",
        "opt_in_message": onboarding.opt_in_message or "",
        "opt_out_message": onboarding.opt_out_message or "",
        "help_message": onboarding.help_message or "",
        "opt_in_keywords": defaults["opt_in_keywords"] or "",
        "opt_out_keywords": defaults["opt_out_keywords"] or "",
        "help_keywords": defaults["help_keywords"] or "",
        "has_embedded_links": "on" if defaults["has_embedded_links"] else "",
        "has_embedded_phone": "on" if defaults["has_embedded_phone"] else "",
        "desired_phone_number": onboarding.desired_phone_number or "",
        "desired_phone_number_sid": onboarding.desired_phone_number_sid or "",
        "campaign_verify_token": campaign_verify_token,
    }


def _public_organization_by_slug_or_404(organization_slug: str) -> Organization:
    with without_tenant_scope():
        organization = Organization.query.filter_by(slug=organization_slug).first()
    if organization is None:
        abort(404)
    return organization


def _tenant_get_or_404(model, entity_id: int):
    query = model.query.filter_by(id=entity_id)
    if (
        saas_mode_enabled()
        and not current_user.is_platform_admin
        and hasattr(model, 'organization_id')
        and _current_organization_id() is not None
    ):
        query = query.filter_by(organization_id=_current_organization_id())
    record = query.first()
    if record is None:
        abort(404)
    return record


def _organization_scoped_user_query():
    query = AppUser.query
    if not saas_mode_enabled() or current_user.is_platform_admin:
        return query
    organization_id = _current_organization_id()
    if not organization_id:
        return query.filter(db.text("1 = 0"))
    return (
        query.join(OrganizationMembership, OrganizationMembership.user_id == AppUser.id)
        .filter(OrganizationMembership.organization_id == organization_id)
    )


def _organization_scoped_user_get_or_404(user_id: int) -> AppUser:
    user = _organization_scoped_user_query().filter(AppUser.id == user_id).first()
    if user is None:
        abort(404)
    return user


def _can_manage_platform() -> bool:
    return bool(getattr(current_user, 'is_platform_admin', False))


def _tenant_bulk_filter(query, model):
    if saas_mode_enabled() and not current_user.is_platform_admin and hasattr(model, 'organization_id'):
        return query.filter(model.organization_id == _current_organization_id())
    return query


def _saas_base_url() -> str:
    configured = (current_app.config.get('SAAS_BASE_URL') or '').strip().rstrip('/')
    if configured:
        return configured
    return request.host_url.rstrip('/')


def _absolute_url(endpoint: str, **values) -> str:
    return f"{_saas_base_url()}{url_for(endpoint, **values)}"


def _invitation_absolute_url(invitation: OrganizationInvitation | None) -> str | None:
    if invitation is None or not invitation.token:
        return None
    return _absolute_url('main.invitation_accept', token=invitation.token)


def _primary_organization_invitation(organization: Organization) -> OrganizationInvitation | None:
    owner_invitation = (
        OrganizationInvitation.query
        .filter_by(organization_id=organization.id, role='owner')
        .order_by(OrganizationInvitation.created_at.desc(), OrganizationInvitation.id.desc())
        .first()
    )
    if owner_invitation is not None:
        return owner_invitation
    return (
        OrganizationInvitation.query
        .filter_by(organization_id=organization.id)
        .order_by(OrganizationInvitation.created_at.desc(), OrganizationInvitation.id.desc())
        .first()
    )


def _organization_owner_membership(organization: Organization) -> OrganizationMembership | None:
    return (
        OrganizationMembership.query
        .filter_by(organization_id=organization.id, role='owner')
        .order_by(OrganizationMembership.id.asc())
        .first()
    )


def _organization_membership_for_user(
    user: AppUser,
    *,
    organization_id: int | None = None,
) -> OrganizationMembership | None:
    target_organization_id = organization_id
    if target_organization_id is None and not getattr(current_user, 'is_platform_admin', False):
        target_organization_id = _current_organization_id()
    if target_organization_id is not None:
        return (
            OrganizationMembership.query
            .filter_by(user_id=user.id, organization_id=target_organization_id)
            .first()
        )
    return user.primary_membership


def _organization_owner_count(organization_id: int) -> int:
    return OrganizationMembership.query.filter_by(organization_id=organization_id, role='owner').count()


def _subscription_view(subscription: OrganizationSubscription | None) -> dict:
    status = (subscription.status if subscription else 'incomplete') or 'incomplete'
    normalized_status = status.strip().lower()
    view = {
        'status': status,
        'badge': 'secondary',
        'title': 'Billing setup needed',
        'summary': 'Finish checkout to unlock sending and team invites.',
        'next_step': 'Start your subscription to finish setting up the business account.',
        'period_label': None,
        'period_value': None,
        'can_send': False,
        'is_complimentary': False,
        'show_checkout': True,
        'show_portal': True,
    }

    if normalized_status == 'trialing':
        view.update(
            badge='success',
            title='Trial active',
            summary='Billing is active and sending is unlocked during the trial.',
            next_step='Keep onboarding your business and add a payment method before the trial ends.',
            can_send=True,
        )
    elif normalized_status == 'active':
        view.update(
            badge='success',
            title='Subscription active',
            summary='Billing is active and your business can keep sending messages.',
            next_step='Use the billing portal anytime to update payment details.',
            can_send=True,
        )
    elif normalized_status == 'complimentary':
        view.update(
            badge='info',
            title='Complimentary billing',
            summary='Platform billing is covered for this workspace.',
            next_step='No Stripe checkout is needed. Twilio billing stays on the customer-managed account.',
            can_send=True,
            is_complimentary=True,
            show_checkout=False,
            show_portal=False,
        )
    elif normalized_status in {'past_due', 'unpaid'}:
        view.update(
            badge='warning',
            title='Payment issue',
            summary='Your business can still sign in, but sending is paused until billing is fixed.',
            next_step='Open the billing portal and resolve the payment issue.',
        )
    elif normalized_status == 'canceled':
        view.update(
            badge='secondary',
            title='Subscription canceled',
            summary='Sending is disabled because the subscription was canceled.',
            next_step='Start a new subscription if you want to resume service.',
        )
    elif normalized_status == 'incomplete':
        view.update(
            badge='warning',
            title='Billing setup needed',
            summary='Your business is not active yet.',
            next_step='Complete checkout to unlock sending and staff invites.',
        )

    if subscription is not None and subscription.current_period_end:
        view['period_label'] = 'Trial ends' if normalized_status == 'trialing' else 'Current period ends'
        view['period_value'] = _format_datetime_display(subscription.current_period_end)

    if not subscription or not subscription.stripe_customer_id:
        view['show_portal'] = False

    return view


def _organization_onboarding_view(organization: Organization) -> dict:
    invitation = (
        OrganizationInvitation.query
        .filter_by(organization_id=organization.id, role='owner')
        .order_by(OrganizationInvitation.created_at.desc(), OrganizationInvitation.id.desc())
        .first()
    )
    owner_membership = _organization_owner_membership(organization)
    owner_invited = invitation is not None or owner_membership is not None
    staff_membership_count = (
        OrganizationMembership.query
        .filter_by(organization_id=organization.id, role='staff')
        .count()
    )
    pending_staff_invitation_count = (
        OrganizationInvitation.query
        .filter_by(organization_id=organization.id, role='staff', status='pending')
        .count()
    )
    subscription_view = _subscription_view(organization.subscription)
    messaging_profile = organization.messaging_profile
    messaging_ready = bool(messaging_profile is not None and messaging_profile.can_send)
    team_ready = staff_membership_count > 0 or pending_staff_invitation_count > 0
    a2p_status = _a2p_status_view(organization.a2p_onboarding, messaging_profile)
    a2p_packet_submitted = a2p_status['has_submission'] or messaging_ready

    steps = [
        {
            'label': 'Organization created',
            'detail': 'Your business account exists and can start onboarding.',
            'complete': True,
            'optional': False,
        },
        {
            'label': 'Owner invited',
            'detail': (
                invitation.email
                if invitation is not None
                else (
                    owner_membership.user.email
                    if owner_membership and owner_membership.user
                    else 'Create the organization to generate the first invite.'
                )
            ),
            'complete': owner_invited,
            'optional': False,
        },
        {
            'label': 'Owner joined',
            'detail': owner_membership.user.email if owner_membership and owner_membership.user else 'Waiting for the owner to accept the invite.',
            'complete': owner_membership is not None,
            'optional': False,
        },
        {
            'label': 'Billing active',
            'detail': subscription_view['title'],
            'complete': subscription_view['can_send'],
            'optional': False,
        },
        {
            'label': 'A2P packet submitted',
            'detail': (
                a2p_status['summary']
                if a2p_status['has_submission']
                else ('Live sender is already configured for this workspace.' if messaging_ready else 'Submit the A2P onboarding packet to start carrier review.')
            ),
            'complete': a2p_packet_submitted,
            'optional': False,
        },
        {
            'label': 'Live SMS approved',
            'detail': (
                messaging_profile.from_number
                if messaging_ready and messaging_profile and messaging_profile.from_number
                else a2p_status['next_step']
            ),
            'complete': messaging_ready,
            'optional': False,
        },
        {
            'label': 'Invite the first staff member',
            'detail': 'Optional for owner-only testing. Add a staff invite before team testing.',
            'complete': team_ready,
            'optional': True,
        },
    ]
    required_steps = [step for step in steps if not step['optional']]
    completed_required = sum(1 for step in required_steps if step['complete'])

    if completed_required == len(required_steps) and team_ready:
        headline = 'Ready for owner + staff testing'
    elif completed_required == len(required_steps):
        headline = 'Ready for owner testing'
    elif all(step['complete'] for step in required_steps[:-1]) and not required_steps[-1]['complete']:
        headline = 'Workspace ready while SMS approval is pending'
    else:
        headline = f'{completed_required}/{len(required_steps)} core steps complete'

    return {
        'headline': headline,
        'completed_required': completed_required,
        'required_total': len(required_steps),
        'steps': steps,
        'owner_invitation': invitation,
        'owner_invitation_url': _invitation_absolute_url(invitation) if invitation and invitation.status == 'pending' else None,
        'owner_joined': owner_membership is not None,
        'team_ready': team_ready,
        'a2p_status': a2p_status,
    }


def _organization_messaging_view(organization: Organization) -> dict:
    profile = organization.messaging_profile
    a2p_status = _a2p_status_view(organization.a2p_onboarding, profile)
    if profile is None:
        return {
            'badge': 'warning text-dark',
            'title': 'Pending',
            'summary': 'Provisioning still needed',
            'detail': 'Create the organization, then provision Twilio after billing.',
        }

    normalized_status = (profile.provider_status or '').strip().lower()
    if profile.can_send or normalized_status == 'active':
        return {
            'badge': 'success',
            'title': 'Ready',
            'summary': profile.from_number or profile.messaging_service_sid or 'Sender assigned',
            'detail': 'Messaging is configured and ready for owner testing.',
        }

    if normalized_status == 'suspended':
        return {
            'badge': 'secondary',
            'title': 'Suspended',
            'summary': profile.from_number or profile.messaging_service_sid or 'Provider suspended',
            'detail': 'Messaging is paused until the provider is resumed.',
        }

    if normalized_status == 'error':
        return {
            'badge': 'danger',
            'title': 'Error',
            'summary': profile.messaging_service_sid or 'Provisioning error',
            'detail': profile.last_provision_error or a2p_status['summary'] or 'Review the provider setup before enabling live SMS.',
        }

    if normalized_status == 'provisioning':
        detail = a2p_status['next_step'] if a2p_status['has_submission'] else (
            'Assign a reviewed sender to finish provisioning.'
            if profile.twilio_subaccount_sid
            else 'Twilio subaccount not provisioned yet.'
        )
        return {
            'badge': 'warning text-dark',
            'title': 'Provisioning',
            'summary': a2p_status['title'] if a2p_status['has_submission'] else (profile.messaging_service_sid or 'Provisioning still needed'),
            'detail': detail,
        }

    return {
        'badge': 'warning text-dark',
        'title': 'Pending',
        'summary': a2p_status['title'] if a2p_status['has_submission'] else (profile.messaging_service_sid or 'Provisioning still needed'),
        'detail': a2p_status['next_step'] if a2p_status['has_submission'] else 'Twilio subaccount not provisioned yet.',
    }


def _platform_organization_rows() -> list[dict]:
    organizations = (
        Organization.query
        .order_by(Organization.created_at.desc(), Organization.id.desc())
        .all()
    )
    return [
        {
            'organization': organization,
            'onboarding': _organization_onboarding_view(organization),
            'billing': _subscription_view(organization.subscription),
            'messaging': _organization_messaging_view(organization),
        }
        for organization in organizations
    ]


def _organization_joined_memberships(organization: Organization) -> list[OrganizationMembership]:
    memberships = (
        OrganizationMembership.query
        .filter_by(organization_id=organization.id)
        .join(AppUser, AppUser.id == OrganizationMembership.user_id)
        .all()
    )
    memberships.sort(
        key=lambda membership: (
            0 if membership.role == 'owner' else 1,
            (membership.user.full_name or membership.user.username or '').lower(),
            membership.id,
        )
    )
    return memberships


def _organization_pending_invitations(organization: Organization) -> list[OrganizationInvitation]:
    invitations = (
        OrganizationInvitation.query
        .filter_by(organization_id=organization.id, status='pending')
        .order_by(OrganizationInvitation.created_at.desc(), OrganizationInvitation.id.desc())
        .all()
    )
    invitations.sort(
        key=lambda invitation: (
            0 if invitation.role == 'owner' else 1,
            -(invitation.id or 0),
        )
    )
    return invitations


def _organization_pending_invitation_for_email(
    organization_id: int,
    email: str,
) -> OrganizationInvitation | None:
    normalized_email = (email or '').strip().lower()
    if not normalized_email:
        return None
    return (
        OrganizationInvitation.query
        .filter_by(
            organization_id=organization_id,
            email=normalized_email,
            status='pending',
        )
        .order_by(OrganizationInvitation.created_at.desc(), OrganizationInvitation.id.desc())
        .first()
    )


def _record_platform_organization_access_event(
    event_type: str,
    *,
    organization: Organization,
    target_email: str | None,
    outcome: str,
    reason: str | None = None,
    invitation: OrganizationInvitation | None = None,
    revoked_count: int | None = None,
) -> None:
    metadata: dict[str, object] = {
        'organization_id': organization.id,
        'organization_slug': organization.slug,
    }
    if target_email:
        metadata['target_email'] = target_email
    if reason:
        metadata['reason'] = reason
    if revoked_count is not None:
        metadata['revoked_count'] = revoked_count
    if invitation is not None:
        metadata['invitation_id'] = invitation.id
        metadata['invitation_role'] = invitation.role
        metadata['invitation_status'] = invitation.status
    record_auth_event(
        event_type,
        outcome=outcome,
        user=current_user,
        username=current_user.username,
        client_ip=request.remote_addr or 'unknown',
        metadata=metadata,
    )


def _platform_organization_access_context(
    organization: Organization,
    *,
    staff_invite_email: str | None = None,
    owner_reissue_email: str | None = None,
) -> dict:
    joined_memberships = _organization_joined_memberships(organization)
    members_missing_email = [
        membership
        for membership in joined_memberships
        if membership.user is not None and not (membership.user.email or '').strip()
    ]
    pending_invitations = _organization_pending_invitations(organization)
    pending_invitation_rows = [
        {
            'invitation': invitation,
            'accept_url': _invitation_absolute_url(invitation),
            'created_display': _format_datetime_display(invitation.created_at),
            'expires_display': _format_datetime_display(invitation.expires_at),
        }
        for invitation in pending_invitations
    ]
    owner_membership = next(
        (membership for membership in joined_memberships if membership.role == 'owner'),
        None,
    )
    pending_owner_invitation = next(
        (row['invitation'] for row in pending_invitation_rows if row['invitation'].role == 'owner'),
        None,
    )
    return {
        'organization': organization,
        'joined_memberships': joined_memberships,
        'pending_invitation_rows': pending_invitation_rows,
        'owner_membership': owner_membership,
        'pending_owner_invitation': pending_owner_invitation,
        'pending_owner_invitation_url': _invitation_absolute_url(pending_owner_invitation),
        'owner_recovery_available': owner_membership is None,
        'staff_invite_email': staff_invite_email or '',
        'owner_reissue_email': (
            owner_reissue_email
            if owner_reissue_email is not None
            else (pending_owner_invitation.email if pending_owner_invitation is not None else '')
        ),
        'members_missing_email': members_missing_email,
        'onboarding': _organization_onboarding_view(organization),
        'billing': _subscription_view(organization.subscription),
    }


def _render_platform_organization_access(
    organization: Organization,
    *,
    staff_invite_email: str | None = None,
    owner_reissue_email: str | None = None,
):
    return render_template(
        'platform/organization_access.html',
        **_platform_organization_access_context(
            organization,
            staff_invite_email=staff_invite_email,
            owner_reissue_email=owner_reissue_email,
        ),
    )


def _platform_restart_status_view() -> dict | None:
    restart_request = latest_platform_service_restart_request()
    if restart_request is None:
        return None

    status = (restart_request.status or '').strip().lower()
    if status in {'pending', 'queued'}:
        title = 'Queued'
        badge = 'warning text-dark'
        default_summary = (
            'Restart request queued. Waiting for the host processor.'
            if status == 'pending'
            else 'Restart queued. The SaaS services are restarting.'
        )
        timestamp = restart_request.started_at or restart_request.requested_at
    elif status == 'succeeded':
        title = 'Succeeded'
        badge = 'success'
        default_summary = 'Restart completed successfully.'
        timestamp = restart_request.completed_at or restart_request.last_checked_at or restart_request.requested_at
    else:
        title = 'Failed'
        badge = 'danger'
        default_summary = 'Restart failed.'
        timestamp = restart_request.completed_at or restart_request.last_checked_at or restart_request.requested_at

    return {
        'outcome': status,
        'title': title,
        'badge': badge,
        'summary': restart_request.summary or default_summary,
        'detail': restart_request.detail,
        'created_at_display': _format_datetime_display(timestamp),
    }


def _platform_home_context() -> dict:
    organization_rows = _platform_organization_rows()
    total_organizations = len(organization_rows)
    active_organizations = sum(
        1 for row in organization_rows
        if row['organization'].status == 'active'
    )
    suspended_organizations = total_organizations - active_organizations
    billing_ready = sum(
        1 for row in organization_rows
        if row['billing']['can_send']
    )
    onboarding_incomplete = sum(
        1 for row in organization_rows
        if row['onboarding']['completed_required'] < row['onboarding']['required_total']
    )
    missing_live_messaging = sum(
        1 for row in organization_rows
        if not (
            row['organization'].messaging_profile
            and row['organization'].messaging_profile.can_send
        )
    )
    attention_rows = [
        row for row in organization_rows
        if row['onboarding']['completed_required'] < row['onboarding']['required_total']
        or not row['billing']['can_send']
    ][:5]
    return {
        'summary': {
            'total_organizations': total_organizations,
            'active_organizations': active_organizations,
            'suspended_organizations': suspended_organizations,
            'billing_ready': billing_ready,
            'onboarding_incomplete': onboarding_incomplete,
            'missing_live_messaging': missing_live_messaging,
        },
        'attention_rows': attention_rows,
        'recent_rows': organization_rows[:5],
        'service_restart': {
            'enabled': bool(current_app.config.get('PLATFORM_SERVICE_RESTART_ENABLED')),
            'last_result': _platform_restart_status_view(),
        },
    }


def _billing_context(organization: Organization | None) -> dict:
    subscription = organization.subscription if organization is not None else None
    onboarding = _organization_onboarding_view(organization) if organization is not None else None
    return {
        'subscription_view': _subscription_view(subscription),
        'onboarding_view': onboarding,
        'a2p_status_view': onboarding['a2p_status'] if onboarding is not None else None,
    }


def _organization_has_active_messaging(organization: Organization | None) -> bool:
    profile = organization.messaging_profile if organization is not None else None
    return bool(profile is not None and profile.can_send)


def _organization_can_transmit_messages(organization: Organization | None) -> bool:
    return organization_can_send(organization) and _organization_has_active_messaging(organization)


def _send_access_denied_response(organization: Organization | None):
    if not organization_can_send(organization):
        flash('Finish workspace setup before sending messages.', 'error')
        return redirect(url_for('main.setup'))
    flash('Messaging is not provisioned for this organization yet. Return to setup to finish activation.', 'error')
    return redirect(url_for('main.setup'))


def _require_active_subscription():
    if not saas_mode_enabled() or current_user.is_platform_admin:
        return None
    organization = _current_organization()
    if _organization_can_transmit_messages(organization):
        return None
    return _send_access_denied_response(organization)


def _require_billing_access():
    if _can_manage_platform():
        abort(403)
    if saas_mode_enabled() and getattr(current_user, 'organization_role', None) != 'owner':
        abort(403)


def _get_queue_with_preflight():
    from app.queue import get_queue

    queue = get_queue()
    connection = getattr(queue, 'connection', None)
    ping = getattr(connection, 'ping', None)
    if callable(ping):
        ping()
    return queue


def _cleanup_bootstrap_admin_password_if_needed() -> None:
    if current_app.config.get("DEBUG"):
        return
    if not _is_explicit_production():
        return

    admin_username = (current_app.config.get("ADMIN_USERNAME") or "").strip()
    if not admin_username or current_user.username != admin_username:
        return

    env_path = os.environ.get("SMS_ADMIN_ENV_FILE", "/opt/sms-admin/.env")
    removed = _remove_env_key_in_place(env_path, "ADMIN_PASSWORD")
    if removed is None:
        current_app.logger.warning(
            "Could not remove ADMIN_PASSWORD from %s after password change.",
            env_path,
        )
        return

    os.environ.pop("ADMIN_PASSWORD", None)
    current_app.config["ADMIN_PASSWORD"] = None
    if removed:
        current_app.logger.info(
            "Removed bootstrap ADMIN_PASSWORD from %s after admin password change.",
            env_path,
        )


def _normalized_keyword_sql(column):
    """
    Normalize keyword-like values in SQL to mirror normalize_keyword().

    This keeps conflict checks in the database and avoids loading all rows
    into Python when legacy rows might contain non-canonical whitespace.
    """
    normalized = func.upper(func.trim(column))
    for token in ('\t', '\n', '\r', '\f', '\v'):
        normalized = func.replace(normalized, token, ' ')
    for _ in range(6):
        normalized = func.replace(normalized, '  ', ' ')
    return normalized


def _keyword_conflicts_with_survey(trigger_keyword: str, *, exclude_survey_id: int | None = None) -> bool:
    normalized_trigger = normalize_keyword(trigger_keyword)
    if not normalized_trigger:
        return False

    query = SurveyFlow.query.filter(_normalized_keyword_sql(SurveyFlow.trigger_keyword) == normalized_trigger)
    if exclude_survey_id is not None:
        query = query.filter(SurveyFlow.id != exclude_survey_id)
    return query.first() is not None


def _keyword_conflicts_with_rule(keyword: str, *, exclude_rule_id: int | None = None) -> bool:
    normalized_keyword = normalize_keyword(keyword)
    if not normalized_keyword:
        return False

    query = KeywordAutomationRule.query.filter(
        _normalized_keyword_sql(KeywordAutomationRule.keyword) == normalized_keyword
    )
    if exclude_rule_id is not None:
        query = query.filter(KeywordAutomationRule.id != exclude_rule_id)
    return query.first() is not None


def _is_active_trigger_keyword(column):
    active_rule_keywords = select(KeywordAutomationRule.keyword).where(
        KeywordAutomationRule.is_active.is_(True)
    )
    active_survey_keywords = select(SurveyFlow.trigger_keyword).where(
        SurveyFlow.is_active.is_(True)
    )
    return db.or_(
        column.in_(active_rule_keywords),
        column.in_(active_survey_keywords),
    )


def _active_trigger_keywords_set() -> set[str]:
    active_keywords_query = select(KeywordAutomationRule.keyword).where(
        KeywordAutomationRule.is_active.is_(True)
    ).union(
        select(SurveyFlow.trigger_keyword).where(SurveyFlow.is_active.is_(True))
    )
    return {
        keyword
        for keyword in db.session.execute(active_keywords_query).scalars().all()
        if keyword
    }


def _community_name_map_for_phones(phones: set[str]) -> dict[str, str]:
    if not phones:
        return {}

    members = CommunityMember.query.filter(CommunityMember.phone.in_(phones)).all()
    community_name_map = {}
    for member in members:
        name = (member.name or '').strip()
        if name:
            community_name_map[member.phone] = name
    return community_name_map


def _build_thread_display_names(
    threads: list[InboxThread],
    selected_thread: InboxThread | None = None,
) -> dict[int, str]:
    phones = {thread.phone for thread in threads if thread.phone}
    if selected_thread and selected_thread.phone:
        phones.add(selected_thread.phone)

    community_name_map = _community_name_map_for_phones(phones)
    display_names: dict[int, str] = {}

    for thread in threads:
        thread_name = (thread.contact_name or '').strip()
        display_names[thread.id] = community_name_map.get(thread.phone) or thread_name or thread.phone

    if selected_thread and selected_thread.id not in display_names:
        selected_name = (selected_thread.contact_name or '').strip()
        display_names[selected_thread.id] = (
            community_name_map.get(selected_thread.phone) or selected_name or selected_thread.phone
        )

    return display_names


def _parse_int_ids(raw_values: list[str]) -> list[int]:
    ids: list[int] = []
    for raw in raw_values:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return sorted(set(ids))


def _parse_survey_preview_indexes(raw_values: list[str], *, question_count: int) -> list[int]:
    indexes: list[int] = []
    for raw in raw_values:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= index < question_count:
            indexes.append(index)
    return sorted(set(indexes))


def _redirect_to_inbox(*, thread_id: int | None = None) -> object:
    search = request.form.get('search', '').strip()
    query_args = {}
    if thread_id:
        query_args['thread'] = thread_id
    if search:
        query_args['search'] = search
    return redirect(url_for('main.inbox_list', **query_args))


def _survey_submission_search_phones(survey_id: int, search: str) -> set[str]:
    search_text = search.strip()
    if not search_text:
        return set()

    escaped = escape_like(search_text)
    pattern = f'%{escaped}%'
    phones: set[str] = set()

    session_rows = (
        db.session.query(SurveySession.phone)
        .filter(
            SurveySession.survey_id == survey_id,
            SurveySession.status == 'completed',
            SurveySession.phone.ilike(pattern, escape='\\'),
        )
        .distinct()
        .all()
    )
    phones.update(phone for (phone,) in session_rows if phone)

    response_rows = (
        db.session.query(SurveyResponse.phone)
        .filter(
            SurveyResponse.survey_id == survey_id,
            SurveyResponse.answer.ilike(pattern, escape='\\'),
        )
        .distinct()
        .all()
    )
    phones.update(phone for (phone,) in response_rows if phone)

    community_rows = (
        db.session.query(CommunityMember.phone)
        .filter(CommunityMember.name.ilike(pattern, escape='\\'))
        .distinct()
        .all()
    )
    phones.update(phone for (phone,) in community_rows if phone)

    thread_rows = (
        db.session.query(InboxThread.phone)
        .filter(InboxThread.contact_name.ilike(pattern, escape='\\'))
        .distinct()
        .all()
    )
    phones.update(phone for (phone,) in thread_rows if phone)

    return phones


def _build_survey_submission_data(
    survey: SurveyFlow,
    *,
    search: str = '',
    page: int = 1,
    per_page: int = 50,
    preview_question_indexes: list[int] | None = None,
) -> dict[str, object]:
    questions = survey.questions
    selected_preview_indexes = _parse_survey_preview_indexes(
        [str(index) for index in (preview_question_indexes or [])],
        question_count=len(questions),
    )
    completed_filters = [
        SurveySession.survey_id == survey.id,
        SurveySession.status == 'completed',
    ]
    search_text = search.strip()
    search_phones = _survey_submission_search_phones(survey.id, search_text) if search_text else set()
    if search_text and not search_phones:
        return {
            'questions': questions,
            'latest_rows': [],
            'history_by_phone': {},
            'latest_session_by_phone': {},
            'unique_attendees': 0,
            'total_completed': 0,
            'repeat_submitters': 0,
            'page': 1,
            'total_pages': 0,
            'per_page': per_page,
            'preview_question_indexes': selected_preview_indexes,
        }

    if search_phones:
        completed_filters.append(SurveySession.phone.in_(search_phones))

    total_completed = (
        db.session.query(func.count(SurveySession.id))
        .filter(*completed_filters)
        .scalar()
        or 0
    )
    unique_attendees = (
        db.session.query(func.count(func.distinct(SurveySession.phone)))
        .filter(*completed_filters)
        .scalar()
        or 0
    )
    repeat_submitters = 0
    if unique_attendees:
        counts_subquery = (
            db.session.query(
                SurveySession.phone.label('phone'),
                func.count(SurveySession.id).label('submission_count'),
            )
            .filter(*completed_filters)
            .group_by(SurveySession.phone)
            .subquery()
        )
        repeat_submitters = (
            db.session.query(func.count())
            .select_from(counts_subquery)
            .filter(counts_subquery.c.submission_count > 1)
            .scalar()
            or 0
        )

    if not unique_attendees:
        return {
            'questions': questions,
            'latest_rows': [],
            'history_by_phone': {},
            'latest_session_by_phone': {},
            'unique_attendees': 0,
            'total_completed': 0,
            'repeat_submitters': 0,
            'page': 1,
            'total_pages': 0,
            'per_page': per_page,
            'preview_question_indexes': selected_preview_indexes,
        }

    total_pages = math.ceil(unique_attendees / per_page) if per_page else 1
    safe_page = max(1, min(page, total_pages))
    offset = (safe_page - 1) * per_page

    row_num = func.row_number().over(
        partition_by=SurveySession.phone,
        order_by=(SurveySession.completed_at.desc(), SurveySession.id.desc()),
    ).label('row_num')
    latest_subquery = (
        db.session.query(
            SurveySession.id.label('session_id'),
            SurveySession.phone.label('phone'),
            SurveySession.thread_id.label('thread_id'),
            SurveySession.completed_at.label('completed_at'),
            SurveySession.last_activity_at.label('last_activity_at'),
            SurveySession.started_at.label('started_at'),
            row_num,
        )
        .filter(*completed_filters)
        .subquery()
    )
    latest_sessions = (
        db.session.query(SurveySession)
        .join(latest_subquery, SurveySession.id == latest_subquery.c.session_id)
        .filter(latest_subquery.c.row_num == 1)
        .order_by(
            latest_subquery.c.completed_at.desc().nullslast(),
            latest_subquery.c.session_id.desc(),
        )
        .limit(per_page)
        .offset(offset)
        .all()
    )

    if not latest_sessions:
        return {
            'questions': questions,
            'latest_rows': [],
            'history_by_phone': {},
            'latest_session_by_phone': {},
            'unique_attendees': unique_attendees,
            'total_completed': total_completed,
            'repeat_submitters': repeat_submitters,
            'page': safe_page,
            'total_pages': total_pages,
            'per_page': per_page,
            'preview_question_indexes': selected_preview_indexes,
        }

    phones = {session.phone for session in latest_sessions if session.phone}
    completed_filter_for_phones = completed_filters + [SurveySession.phone.in_(phones)]
    submission_counts = {
        phone: count
        for phone, count in (
            db.session.query(SurveySession.phone, func.count(SurveySession.id))
            .filter(*completed_filter_for_phones)
            .group_by(SurveySession.phone)
            .all()
        )
    }

    session_rows = (
        SurveySession.query.filter(*completed_filter_for_phones)
        .order_by(SurveySession.completed_at.desc(), SurveySession.id.desc())
        .all()
    )
    session_ids = [session.id for session in session_rows]
    responses = (
        SurveyResponse.query.filter(SurveyResponse.session_id.in_(session_ids))
        .order_by(SurveyResponse.session_id.asc(), SurveyResponse.question_index.asc(), SurveyResponse.id.asc())
        .all()
    )

    answers_by_session: dict[int, dict[int, str]] = {}
    for response in responses:
        answer_map = answers_by_session.setdefault(response.session_id, {})
        if response.question_index not in answer_map:
            answer_map[response.question_index] = (response.answer or '').strip()

    community_name_map = _community_name_map_for_phones(phones)
    thread_name_map = {
        thread.phone: (thread.contact_name or '').strip()
        for thread in InboxThread.query.filter(InboxThread.phone.in_(phones)).all()
        if (thread.contact_name or '').strip()
    }

    latest_session_by_phone = {session.phone: session.id for session in latest_sessions if session.phone}
    latest_rows: list[dict[str, object]] = []
    history_by_phone: dict[str, list[dict[str, object]]] = {}

    for session in session_rows:
        answer_map = answers_by_session.get(session.id, {})
        answers = [answer_map.get(index, '') for index in range(len(questions))]
        first_answer = next((answer for answer in answers if answer), '')
        display_name = (
            community_name_map.get(session.phone)
            or thread_name_map.get(session.phone)
            or first_answer
            or session.phone
        )
        if selected_preview_indexes:
            answer_preview_items = [answers[index] for index in selected_preview_indexes if answers[index]]
        else:
            answer_preview_items = [answer for answer in answers if answer][:2]
        answers_preview = '; '.join(answer_preview_items)
        if len(answers_preview) > 120:
            answers_preview = f"{answers_preview[:117].rstrip()}..."

        submitted_at = session.completed_at or session.last_activity_at or session.started_at
        submitted_at_iso = submitted_at.isoformat() if submitted_at else ''

        row = {
            'session_id': session.id,
            'phone': session.phone,
            'display_name': display_name,
            'submitted_at': submitted_at,
            'submitted_at_iso': submitted_at_iso,
            'answers': answers,
            'qa_pairs': [
                {'prompt': question, 'answer': answers[index] if index < len(answers) else ''}
                for index, question in enumerate(questions)
            ],
            'answers_preview': answers_preview,
            'thread_id': session.thread_id,
        }
        if latest_session_by_phone.get(session.phone) == session.id:
            row['submission_count'] = submission_counts.get(session.phone, 0)
            row['phone_dom_id'] = re.sub(r'[^0-9]', '', str(session.phone)) or f"phone{session.id}"
            latest_rows.append(row)
        else:
            history_by_phone.setdefault(str(session.phone), []).append(row)

    return {
        'questions': questions,
        'latest_rows': latest_rows,
        'history_by_phone': history_by_phone,
        'latest_session_by_phone': latest_session_by_phone,
        'unique_attendees': unique_attendees,
        'total_completed': total_completed,
        'repeat_submitters': repeat_submitters,
        'page': safe_page,
        'total_pages': total_pages,
        'per_page': per_page,
        'preview_question_indexes': selected_preview_indexes,
    }


def _iter_survey_submission_export_rows(survey: SurveyFlow) -> object:
    questions = survey.questions
    question_headers = [question if question else f'question_{index + 1}' for index, question in enumerate(questions)]
    header_row = [
        'submission_id',
        'phone',
        'display_name',
        'submitted_at_utc',
        'is_latest_for_phone',
        *question_headers,
    ]

    def row_generator():
        yield header_row
        row_num = func.row_number().over(
            partition_by=SurveySession.phone,
            order_by=(SurveySession.completed_at.desc(), SurveySession.id.desc()),
        ).label('row_num')
        base_query = (
            db.session.query(SurveySession, row_num)
            .filter(
                SurveySession.survey_id == survey.id,
                SurveySession.status == 'completed',
            )
            .order_by(SurveySession.completed_at.desc(), SurveySession.id.desc())
        )
        batch_size = 500
        offset = 0
        while True:
            batch = base_query.limit(batch_size).offset(offset).all()
            if not batch:
                break
            sessions = [session for session, _ in batch]
            row_nums = {session.id: row_number for session, row_number in batch}
            phones = {session.phone for session in sessions if session.phone}
            community_name_map = _community_name_map_for_phones(phones)
            thread_name_map = {
                thread.phone: (thread.contact_name or '').strip()
                for thread in InboxThread.query.filter(InboxThread.phone.in_(phones)).all()
                if (thread.contact_name or '').strip()
            }

            session_ids = [session.id for session in sessions]
            responses = (
                SurveyResponse.query.filter(SurveyResponse.session_id.in_(session_ids))
                .order_by(SurveyResponse.session_id.asc(), SurveyResponse.question_index.asc(), SurveyResponse.id.asc())
                .all()
            )
            answers_by_session: dict[int, dict[int, str]] = {}
            for response in responses:
                answer_map = answers_by_session.setdefault(response.session_id, {})
                if response.question_index not in answer_map:
                    answer_map[response.question_index] = (response.answer or '').strip()

            for session in sessions:
                answer_map = answers_by_session.get(session.id, {})
                answers = [answer_map.get(index, '') for index in range(len(questions))]
                first_answer = next((answer for answer in answers if answer), '')
                display_name = (
                    community_name_map.get(session.phone)
                    or thread_name_map.get(session.phone)
                    or first_answer
                    or session.phone
                )
                submitted_at = session.completed_at or session.last_activity_at or session.started_at
                submitted_at_utc = submitted_at.isoformat() if submitted_at else ''
                is_latest_for_phone = row_nums.get(session.id) == 1
                yield [
                    session.id,
                    session.phone,
                    display_name,
                    submitted_at_utc,
                    'true' if is_latest_for_phone else 'false',
                    *answers,
                ]
            offset += batch_size

    return row_generator()


def _stream_csv_rows(rows: object) -> object:
    output = io.StringIO()
    writer = csv.writer(output)
    for row in rows:
        output.seek(0)
        output.truncate(0)
        writer.writerow([sanitize_csv_cell(cell) for cell in row])
        yield output.getvalue()


def _csv_import_error_response(
    *,
    template_name: str | None = None,
    redirect_endpoint: str | None = None,
    redirect_values: dict | None = None,
):
    if template_name is not None:
        return render_template(template_name)
    if redirect_endpoint is not None:
        return redirect(url_for(redirect_endpoint, **(redirect_values or {})))
    raise ValueError("CSV import error response requires a template or redirect endpoint.")


def _read_uploaded_csv_text(
    *,
    template_name: str | None = None,
    redirect_endpoint: str | None = None,
    redirect_values: dict | None = None,
):
    if 'file' not in request.files:
        flash('No file uploaded.', 'error')
        return None, None, _csv_import_error_response(
            template_name=template_name,
            redirect_endpoint=redirect_endpoint,
            redirect_values=redirect_values,
        )

    uploaded_file = request.files['file']
    if uploaded_file.filename == '':
        flash('No file selected.', 'error')
        return uploaded_file, None, _csv_import_error_response(
            template_name=template_name,
            redirect_endpoint=redirect_endpoint,
            redirect_values=redirect_values,
        )

    return uploaded_file, uploaded_file.read().decode('utf-8'), None


def _dedupe_recipients_by_phone(parsed: list[dict]) -> tuple[list[dict], int]:
    unique_recipients: list[dict] = []
    seen_phones: set[str] = set()
    duplicate_count = 0

    for recipient in parsed:
        phone = recipient['phone']
        if phone in seen_phones:
            duplicate_count += 1
            continue
        seen_phones.add(phone)
        unique_recipients.append(recipient)

    return unique_recipients, duplicate_count


def _csv_download_response(filename: str, rows: object) -> Response:
    response = Response(stream_with_context(_stream_csv_rows(rows)), mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


def _parse_message_log_details(raw_details: str | None) -> list[dict]:
    if not raw_details:
        return []

    try:
        payload = json.loads(raw_details)
    except json.JSONDecodeError:
        return []

    candidates = payload
    if isinstance(payload, dict):
        nested = payload.get('details') or payload.get('results')
        candidates = nested if isinstance(nested, list) else []

    if not isinstance(candidates, list):
        return []

    return [dict(item) for item in candidates if isinstance(item, dict)]


def _survey_form_events() -> list[Event]:
    return Event.query.order_by(Event.date.desc(), Event.title.asc()).all()


def _render_survey_form(*, survey: SurveyFlow | None, form_data: dict | None):
    return render_template(
        'inbox/survey_form.html',
        survey=survey,
        form_data=form_data,
        events=_survey_form_events(),
    )


# Health check endpoint
@bp.route('/health')
def health():
    return 'OK', 200


@bp.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='favicon.svg'), code=302)


# Redirect root to dashboard
@bp.route('/')
@login_required
def index():
    return redirect(url_for(home_endpoint_for_user(current_user)))


@bp.route('/platform')
@login_required
def platform_home():
    if not _can_manage_platform():
        return redirect(url_for('main.dashboard'))

    return render_template(
        'platform/home.html',
        platform_home=_platform_home_context(),
    )


@bp.route('/platform/operations/restart-services', methods=['POST'])
@login_required
def platform_restart_services():
    if not _can_manage_platform():
        abort(403)
    if not saas_mode_enabled() or not current_app.config.get('PLATFORM_SERVICE_RESTART_ENABLED'):
        abort(404)

    client_ip = request.remote_addr or 'unknown'

    try:
        restart_request, created = enqueue_platform_service_restart_request(
            requested_by_user=current_user,
            client_ip=client_ip,
        )
    except Exception as exc:
        current_app.logger.exception(
            'Failed to queue platform restart request user_id=%s client_ip=%s.',
            current_user.id,
            client_ip,
        )
        flash(str(exc), 'error')
    else:
        summary = str(restart_request.summary or 'Restart request queued.')
        if created:
            record_auth_event(
                'platform_service_restart',
                outcome='queued',
                user=current_user,
                username=current_user.username,
                client_ip=client_ip,
                metadata={
                    'request_id': restart_request.id,
                    'status': restart_request.status,
                    'summary': restart_request.summary,
                    'detail': restart_request.detail,
                },
            )
            flash(summary, 'success')
        else:
            flash(
                summary or 'A restart request is already in progress. The latest status will appear below shortly.',
                'info',
            )

    return redirect(url_for('main.platform_home'))


# Dashboard - Send Messages
@bp.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    from flask import current_app
    app_timezone = current_app.config.get('APP_TIMEZONE', 'UTC')
    client_timezone_raw = request.cookies.get('client_timezone', '')
    client_timezone = unquote(client_timezone_raw).strip() if client_timezone_raw else ''
    dashboard_timezone = client_timezone or app_timezone
    events = Event.query.order_by(Event.date.desc()).all()
    admin_test_phone = current_app.config.get('ADMIN_TEST_PHONE')

    def build_chart_data():
        """Build 7-day delivery trends data for the dashboard chart."""
        tz = None
        try:
            tz = ZoneInfo(dashboard_timezone)
        except Exception:
            if dashboard_timezone != app_timezone:
                try:
                    tz = ZoneInfo(app_timezone)
                except Exception:
                    tz = timezone.utc
            else:
                tz = timezone.utc

        today = datetime.now(tz).date()
        labels = []
        sent_data = []
        failed_data = []
        
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            labels.append(day.strftime('%b %d'))
            
            day_start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
            day_end_local = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
            day_start = day_start_local.astimezone(timezone.utc).replace(tzinfo=None)
            day_end = day_end_local.astimezone(timezone.utc).replace(tzinfo=None)
            
            try:
                logs = MessageLog.query.filter(
                    MessageLog.created_at >= day_start,
                    MessageLog.created_at < day_end
                ).all()
                
                day_sent = sum(log.success_count or 0 for log in logs)
                day_failed = sum(log.failure_count or 0 for log in logs)
            except OperationalError:
                day_sent = 0
                day_failed = 0
            
            sent_data.append(day_sent)
            failed_data.append(day_failed)
        
        if any(sent_data) or any(failed_data):
            return {
                'trends': {
                    'labels': labels,
                    'sent': sent_data,
                    'failed': failed_data
                }
            }
        return None

    def build_dashboard_context():
        organization = _current_organization()
        subscription = _current_subscription()
        subscription_view = _subscription_view(subscription)
        messaging_profile = organization.messaging_profile if organization is not None else None
        a2p_status_view = _a2p_status_view(organization.a2p_onboarding if organization is not None else None, messaging_profile)
        community_count = CommunityMember.query.count()
        event_registration_count = EventRegistration.query.count()
        keyword_rule_count = KeywordAutomationRule.query.count()
        survey_flow_count = SurveyFlow.query.count()
        event_count = len(events)
        total_recipients = community_count + event_registration_count
        unsubscribed_count = UnsubscribedContact.query.count()
        inbound_count_7d = 0
        unread_threads_count = 0
        active_survey_sessions = 0
        top_keywords = []
        latest_log = None
        recent_logs = []
        seven_days_ago = utc_now().replace(tzinfo=None) - timedelta(days=7)
        try:
            latest_log = MessageLog.query.order_by(MessageLog.created_at.desc()).first()
            recent_logs = MessageLog.query.order_by(MessageLog.created_at.desc()).limit(5).all()
            inbound_count_7d = InboxMessage.query.filter(
                InboxMessage.direction == 'inbound',
                InboxMessage.created_at >= seven_days_ago,
            ).count()
            unread_threads_count = InboxThread.query.filter(InboxThread.unread_count > 0).count()
            active_survey_sessions = SurveySession.query.filter_by(status='active').count()
            keyword_rows = db.session.query(
                InboxMessage.matched_keyword,
                db.func.count(InboxMessage.id).label('hits'),
            ).filter(
                InboxMessage.direction == 'inbound',
                InboxMessage.created_at >= seven_days_ago,
                InboxMessage.matched_keyword.isnot(None),
                _is_active_trigger_keyword(InboxMessage.matched_keyword),
            ).group_by(
                InboxMessage.matched_keyword,
            ).order_by(
                db.func.count(InboxMessage.id).desc(),
            ).limit(5).all()
            top_keywords = [
                {'keyword': row[0], 'hits': row[1]}
                for row in keyword_rows
            ]
        except OperationalError as exc:
            db.session.rollback()
            current_app.logger.warning(
                'Dashboard query failed due to schema mismatch: %s',
                exc,
            )
        pending_scheduled_count = ScheduledMessage.query.filter_by(status='pending').count()
        success_rate = None
        failure_rate = None
        if latest_log and latest_log.total_recipients:
            success_rate = round((latest_log.success_count / latest_log.total_recipients) * 100, 1)
            failure_rate = round((latest_log.failure_count / latest_log.total_recipients) * 100, 1)

        chart_data = build_chart_data()
        staff_membership_count = 0
        pending_staff_invitation_count = 0
        users_missing_email = []
        if organization is not None:
            staff_membership_count = (
                OrganizationMembership.query
                .filter_by(organization_id=organization.id, role='staff')
                .count()
            )
            pending_staff_invitation_count = (
                OrganizationInvitation.query
                .filter_by(organization_id=organization.id, role='staff', status='pending')
                .count()
            )
            users_missing_email = (
                AppUser.query
                .join(OrganizationMembership, OrganizationMembership.user_id == AppUser.id)
                .filter(OrganizationMembership.organization_id == organization.id)
                .filter(db.or_(AppUser.email.is_(None), AppUser.email == ''))
                .order_by(AppUser.username.asc())
                .all()
            )
        team_ready = staff_membership_count > 0 or pending_staff_invitation_count > 0
        workspace_activation_tasks = _workspace_activation_tasks(
            subscription_view=subscription_view,
            team_ready=team_ready,
            total_recipients=total_recipients,
            keyword_rule_count=keyword_rule_count,
            survey_flow_count=survey_flow_count,
            event_count=event_count,
        )
        can_send_messages = _organization_can_transmit_messages(organization) if saas_mode_enabled() else True
        send_disabled_reason = None
        if saas_mode_enabled() and organization is not None and not can_send_messages:
            send_disabled_reason = (
                subscription_view['next_step']
                if not subscription_view['can_send']
                else a2p_status_view['next_step']
            )

        return {
            'community_count': community_count,
            'event_registration_count': event_registration_count,
            'total_recipients': total_recipients,
            'unsubscribed_count': unsubscribed_count,
            'latest_log': latest_log,
            'recent_logs': recent_logs,
            'pending_scheduled_count': pending_scheduled_count,
            'success_rate': success_rate,
            'failure_rate': failure_rate,
            'chart_data': chart_data,
            'inbound_count_7d': inbound_count_7d,
            'unread_threads_count': unread_threads_count,
            'active_survey_sessions': active_survey_sessions,
            'top_keywords': top_keywords,
            'current_organization': organization,
            'current_subscription': subscription,
            'current_subscription_view': subscription_view,
            'a2p_status_view': a2p_status_view,
            'workspace_activation_tasks': workspace_activation_tasks,
            'can_send_messages': can_send_messages,
            'send_disabled_reason': send_disabled_reason,
            'users_missing_email': users_missing_email,
            'dashboard_is_empty': (
                total_recipients == 0
                and latest_log is None
                and pending_scheduled_count == 0
                and inbound_count_7d == 0
                and unread_threads_count == 0
                and active_survey_sessions == 0
                and not top_keywords
                and not recent_logs
                and chart_data is None
            ),
        }

    def render_dashboard():
        return render_template(
            'dashboard.html',
            events=events,
            admin_test_phone=admin_test_phone,
            app_timezone=app_timezone,
            **build_dashboard_context()
        )
    
    if request.method == 'POST':
        if getattr(current_user, 'effective_role', current_user.role) not in {'admin', 'social_manager'}:
            abort(403)
        subscription_gate = _require_active_subscription()
        if subscription_gate is not None:
            return subscription_gate

        message_body = request.form.get('message_body', '').strip()
        target = request.form.get('target', 'community')
        event_id = request.form.get('event_id', type=int)
        test_mode = request.form.get('test_mode') == 'on'
        include_unsubscribe = request.form.get('include_unsubscribe') == 'on'
        schedule_later = request.form.get('schedule_later') == 'on'
        schedule_date = request.form.get('schedule_date', '').strip()
        schedule_time = request.form.get('schedule_time', '').strip()
        client_timezone = request.form.get('client_timezone', '').strip()
        
        if not message_body:
            flash('Message body is required.', 'error')
            return render_dashboard()

        invalid_tokens = find_invalid_template_tokens(message_body)
        if invalid_tokens:
            allowed_tokens = ', '.join(f'{{{token}}}' for token in ALLOWED_TEMPLATE_TOKENS)
            invalid_list = ', '.join(invalid_tokens)
            flash(
                f'Invalid personalization token(s): {invalid_list}. Use {allowed_tokens}.',
                'error',
            )
            return render_dashboard()
        
        if target == 'event' and not event_id:
            flash('Please select an event.', 'error')
            return render_dashboard()
        
        # Handle scheduled message
        if schedule_later:
            if not schedule_date or not schedule_time:
                flash('Schedule date and time are required.', 'error')
                return render_dashboard()
            
            try:
                tz_name = client_timezone or app_timezone
                tz = None
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    if tz_name != app_timezone:
                        try:
                            tz = ZoneInfo(app_timezone)
                        except Exception:
                            tz = None
                if tz is None:
                    tz = timezone.utc

                scheduled_local = datetime.strptime(f'{schedule_date} {schedule_time}', '%Y-%m-%d %H:%M').replace(tzinfo=tz)
                scheduled_utc = scheduled_local.astimezone(timezone.utc).replace(tzinfo=None)

                if scheduled_utc <= datetime.utcnow():
                    flash('Scheduled time must be in the future.', 'error')
                    return render_dashboard()
                
                # Append unsubscribe text if option is checked
                final_message = message_body
                if include_unsubscribe:
                    final_message = message_body + "\n\nReply STOP to unsubscribe."
                
                scheduled = ScheduledMessage(
                    message_body=final_message,
                    target=target,
                    event_id=event_id if target == 'event' else None,
                    scheduled_at=scheduled_utc,
                    test_mode=test_mode
                )
                db.session.add(scheduled)
                db.session.commit()
                
                flash(f'Message scheduled for {scheduled_local.strftime("%Y-%m-%d %H:%M")}.', 'success')
                return redirect(url_for('main.scheduled_list'))
                
            except ValueError as e:
                flash(f'Invalid date/time format: {e}', 'error')
                return render_dashboard()
        
        # Immediate send
        # Test mode: send only to admin phone
        if test_mode:
            if not admin_test_phone:
                flash('ADMIN_TEST_PHONE not configured. Add it to your .env file.', 'error')
                return render_dashboard()
            recipient_data = [{'phone': admin_test_phone, 'name': 'Admin Test'}]
        else:
            # Get recipients based on target
            if target == 'community':
                members = CommunityMember.query.all()
                recipient_data = [{'phone': m.phone, 'name': m.name} for m in members]
            else:
                # Get registrations for the event (they store phone/name directly)
                registrations = EventRegistration.query.filter_by(event_id=event_id).all()
                recipient_data = [{'phone': r.phone, 'name': r.name} for r in registrations]

            recipient_data, skipped, _ = filter_unsubscribed_recipients(recipient_data)
            if skipped:
                flash(f'Skipped {len(skipped)} unsubscribed recipient(s).', 'warning')

            recipient_data, suppressed_skipped, _ = filter_suppressed_recipients(recipient_data)
            if suppressed_skipped:
                flash(f'Skipped {len(suppressed_skipped)} suppressed recipient(s).', 'warning')
        
        if not recipient_data:
            if test_mode:
                flash('No recipients found for the selected target.', 'error')
            else:
                flash('All recipients are unsubscribed or no recipients were found.', 'error')
            return render_dashboard()
        
        # Append unsubscribe text if option is checked
        final_message = message_body
        if include_unsubscribe:
            final_message = message_body + "\n\nReply STOP to unsubscribe."

        try:
            from rq import Retry

            queue = _get_queue_with_preflight()
        except Exception:
            current_app.logger.exception(
                'Background queue unavailable for blast enqueue organization_id=%s target=%s.',
                _current_organization_id() if saas_mode_enabled() else None,
                target,
            )
            flash(BLAST_QUEUE_UNAVAILABLE_FLASH, 'error')
            return render_dashboard()

        # Persist log before sending begins
        log = MessageLog(
            message_body=final_message,
            target=target,
            event_id=event_id if target == 'event' else None,
            status='processing',
            total_recipients=len(recipient_data),
            success_count=0,
            failure_count=0,
            details='[]'
        )
        db.session.add(log)
        db.session.commit()

        try:
            queue.enqueue(
                'app.tasks.send_bulk_job',
                log.id,
                log.organization_id,
                recipient_data,
                final_message,
                retry=Retry(max=3, interval=[30, 120, 300])
            )
            flash('Blast queued. Sending in the background.', 'success')
            return redirect(url_for('main.log_detail', log_id=log.id))
        except Exception:
            current_app.logger.exception(
                'Failed to enqueue blast log_id=%s organization_id=%s.',
                log.id,
                log.organization_id,
            )
            log.status = 'failed'
            log.details = json.dumps([{'error': BLAST_QUEUE_UNAVAILABLE_FLASH}])
            db.session.commit()
            flash(BLAST_QUEUE_UNAVAILABLE_FLASH, 'error')

    return render_dashboard()


# User Management
@bp.route('/users')
@login_required
@require_roles('admin')
def users_list():
    users = _organization_scoped_user_query().order_by(AppUser.username).all()
    users_missing_email = [user for user in users if not (user.email or '').strip()]
    pending_invitations = []
    if saas_mode_enabled() and not current_user.is_platform_admin:
        invitations = (
            OrganizationInvitation.query
            .filter_by(status='pending')
            .order_by(OrganizationInvitation.created_at.desc())
            .all()
        )
        pending_invitations = [
            {
                'invitation': invitation,
                'accept_url': _invitation_absolute_url(invitation),
                'expires_display': _format_datetime_display(invitation.expires_at),
            }
            for invitation in invitations
        ]
    return render_template(
        'users/list.html',
        users=users,
        pending_invitations=pending_invitations,
        users_missing_email=users_missing_email,
    )


@bp.route('/users/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def users_add():
    if saas_mode_enabled() and not current_user.is_platform_admin:
        flash('Use team invites to add staff in SaaS mode.', 'info')
        return redirect(url_for('main.team_invite'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        full_name = request.form.get('full_name', '').strip() or None
        role = request.form.get('role', '').strip()
        requested_platform_admin = _requested_platform_admin_access()
        phone_input = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        must_change_password = request.form.get('must_change_password') == 'on'

        if not username:
            flash('Username is required.', 'error')
            return render_template('users/form.html', user=None)
        if saas_mode_enabled() and not email:
            flash('Email is required in SaaS mode.', 'error')
            return render_template('users/form.html', user=None)

        if role not in {'admin', 'social_manager'}:
            flash('Role selection is required.', 'error')
            return render_template('users/form.html', user=None)
        platform_admin_error = _platform_admin_access_error(
            requested_platform_admin=requested_platform_admin,
        )
        if platform_admin_error:
            flash(platform_admin_error, 'error')
            return render_template('users/form.html', user=None)

        if not password:
            flash('Password is required.', 'error')
            return render_template('users/form.html', user=None)

        if not phone_input:
            flash('Phone number is required.', 'error')
            return render_template('users/form.html', user=None)

        normalized_phone = normalize_phone(phone_input)
        if not validate_phone(normalized_phone):
            flash('Phone number must be a valid E.164 number.', 'error')
            return render_template('users/form.html', user=None)

        policy_errors = password_policy_errors(password, username=username)
        if policy_errors:
            for error in policy_errors:
                flash(error, 'error')
            return render_template('users/form.html', user=None)

        existing = _find_username_conflict(username)
        if existing:
            flash('A user with this username already exists.', 'error')
            return render_template('users/form.html', user=None)
        if email and AppUser.query.filter(func.lower(AppUser.email) == email).first():
            flash('A user with this email already exists.', 'error')
            return render_template('users/form.html', user=None)

        existing_phone = _find_phone_conflict(
            normalized_phone,
            organization_id=_current_organization_id() if saas_mode_enabled() else None,
        )
        if existing_phone:
            if saas_mode_enabled() and not current_user.is_platform_admin:
                flash('A user with this phone number already exists in this organization.', 'error')
            else:
                flash('A user with this phone number already exists.', 'error')
            return render_template('users/form.html', user=None)

        user = AppUser(
            username=username,
            email=email or None,
            full_name=full_name,
            phone=normalized_phone,
            role='admin' if requested_platform_admin else role,
            is_platform_admin=requested_platform_admin,
            must_change_password=must_change_password,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash('User created successfully.', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('users/form.html', user=None)


@bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def users_edit(user_id):
    user = _organization_scoped_user_get_or_404(user_id)

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        full_name = request.form.get('full_name', '').strip() or None
        role = request.form.get('role', '').strip()
        requested_platform_admin = _requested_platform_admin_access(user)
        phone_input = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        must_change_password = request.form.get('must_change_password') == 'on'

        if not username:
            flash('Username is required.', 'error')
            return render_template('users/form.html', user=user)
        if saas_mode_enabled() and not email:
            flash('Email is required in SaaS mode.', 'error')
            return render_template('users/form.html', user=user)

        if role not in {'admin', 'social_manager'}:
            flash('Role selection is required.', 'error')
            return render_template('users/form.html', user=user)
        platform_admin_error = _platform_admin_access_error(
            requested_platform_admin=requested_platform_admin,
            user=user,
        )
        if platform_admin_error:
            flash(platform_admin_error, 'error')
            return render_template('users/form.html', user=user)

        if not phone_input:
            flash('Phone number is required.', 'error')
            return render_template('users/form.html', user=user)

        normalized_phone = normalize_phone(phone_input)
        if not validate_phone(normalized_phone):
            flash('Phone number must be a valid E.164 number.', 'error')
            return render_template('users/form.html', user=user)

        existing = _find_username_conflict(username, exclude_user_id=user_id)
        if existing:
            flash('A user with this username already exists.', 'error')
            return render_template('users/form.html', user=user)
        if email:
            email_conflict = AppUser.query.filter(
                func.lower(AppUser.email) == email,
                AppUser.id != user_id,
            ).first()
            if email_conflict:
                flash('A user with this email already exists.', 'error')
                return render_template('users/form.html', user=user)

        existing_phone = _find_phone_conflict(
            normalized_phone,
            exclude_user_id=user_id,
            organization_id=_current_organization_id() if saas_mode_enabled() else None,
        )
        if existing_phone:
            if saas_mode_enabled() and not current_user.is_platform_admin:
                flash('A user with this phone number already exists in this organization.', 'error')
            else:
                flash('A user with this phone number already exists.', 'error')
            return render_template('users/form.html', user=user)

        membership = None
        membership_role = None
        if saas_mode_enabled() and not user.is_platform_admin:
            membership = _organization_membership_for_user(user)
            if membership is not None:
                membership_role = _membership_role_from_user_role(role)
                if membership.role == 'owner' and membership_role != 'owner':
                    owner_count = _organization_owner_count(membership.organization_id)
                    if owner_count <= 1:
                        flash('At least one owner is required.', 'error')
                        return render_template('users/form.html', user=user)

        if membership is None and not user.is_platform_admin and user.role == 'admin' and role != 'admin':
            admin_count = _organization_scoped_user_query().filter_by(role='admin').count()
            if admin_count <= 1:
                flash('At least one admin user is required.', 'error')
                return render_template('users/form.html', user=user)

        if password and user.id == current_user.id:
            flash('Use Account > Change Password to update your own password.', 'error')
            return render_template('users/form.html', user=user)

        user.username = username
        user.email = email or None
        user.full_name = full_name
        user.phone = normalized_phone
        if membership is not None and membership_role is not None:
            membership.role = membership_role
            user.role = 'admin' if membership_role == 'owner' else 'social_manager'
        else:
            user.role = 'admin' if requested_platform_admin else role
        user.is_platform_admin = requested_platform_admin
        user.must_change_password = must_change_password
        performed_admin_reset = False
        old_password_hash = None
        if password:
            policy_errors = password_policy_errors(password, username=username)
            if policy_errors:
                for error in policy_errors:
                    flash(error, 'error')
                return render_template('users/form.html', user=user)
            old_password_hash = user.password_hash
            user.set_password(password)
            user.must_change_password = True
            user.rotate_session_nonce()
            performed_admin_reset = True
            store_password_history(
                user.id,
                old_password_hash,
                current_app.config.get('PASSWORD_HISTORY_COUNT', 3),
            )

        db.session.commit()

        if performed_admin_reset:
            record_auth_event(
                'admin_password_reset',
                outcome='success',
                user=user,
                username=user.username,
                client_ip=request.remote_addr or 'unknown',
                metadata={'actor_user_id': current_user.id},
            )
            alert_result = send_security_alert(user, 'admin_password_reset')
            if not alert_result.get('success'):
                record_auth_event(
                    'alert_sms_failed',
                    outcome='failed',
                    user=user,
                    username=user.username,
                    client_ip=request.remote_addr or 'unknown',
                    metadata={
                        'context': 'admin_password_reset',
                        'reason': alert_result.get('reason'),
                        'skipped': alert_result.get('skipped', False),
                    },
                )

        flash('User updated successfully.', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('users/form.html', user=user)


@bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def users_delete(user_id):
    user = _organization_scoped_user_get_or_404(user_id)

    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('main.users_list'))

    if user.is_platform_admin:
        if _platform_admin_count() <= 1:
            flash('At least one platform admin is required.', 'error')
            return redirect(url_for('main.users_list'))
    elif user.role == 'admin':
        admin_count = _organization_scoped_user_query().filter_by(role='admin').count()
        if admin_count <= 1:
            flash('At least one admin user is required.', 'error')
            return redirect(url_for('main.users_list'))

    db.session.delete(user)
    db.session.commit()
    flash('User deleted successfully.', 'success')
    return redirect(url_for('main.users_list'))


@bp.route('/team/invite', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def team_invite():
    if not saas_mode_enabled() or current_user.is_platform_admin:
        abort(404)

    organization = _current_organization()
    if not organization_can_send(organization):
        flash('Activate billing before inviting staff members.', 'error')
        return redirect(url_for('main.setup', step='billing'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        role = request.form.get('role', '').strip().lower()
        if not email:
            flash('Email is required.', 'error')
            return render_template('users/invite_form.html')
        if role not in {'admin', 'social_manager', 'owner', 'staff'}:
            flash('Role is required.', 'error')
            return render_template('users/invite_form.html')

        existing_membership = (
            OrganizationMembership.query
            .join(AppUser, AppUser.id == OrganizationMembership.user_id)
            .filter(OrganizationMembership.organization_id == _current_organization_id())
            .filter(func.lower(AppUser.email) == email)
            .first()
        )
        if existing_membership:
            flash('That email is already on your team.', 'warning')
            return redirect(url_for('main.users_list'))
        _, team_email_error = _organization_email_account_status(email)
        if team_email_error:
            flash(team_email_error, 'error')
            return redirect(url_for('main.users_list'))

        existing_invite = OrganizationInvitation.query.filter_by(email=email, status='pending').first()
        if existing_invite:
            flash('A pending invitation already exists for that email.', 'warning')
            return redirect(url_for('main.users_list'))

        invitation = OrganizationInvitation(
            organization_id=_current_organization_id(),
            email=email,
            role=_membership_role_from_user_role(role),
            invited_by_user_id=current_user.id,
            expires_at=utc_now() + timedelta(days=7),
        )
        db.session.add(invitation)
        db.session.commit()
        flash('Team invitation created.', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('users/invite_form.html')


@bp.route('/team/invitations/<int:invitation_id>/revoke', methods=['POST'])
@login_required
@require_roles('admin')
def team_invitation_revoke(invitation_id):
    if not saas_mode_enabled() or current_user.is_platform_admin:
        abort(404)

    invitation = _tenant_get_or_404(OrganizationInvitation, invitation_id)
    if invitation.status != 'pending':
        flash('Only pending invitations can be revoked.', 'warning')
        return redirect(url_for('main.users_list'))

    invitation.status = 'revoked'
    db.session.commit()
    flash('Invitation revoked.', 'success')
    return redirect(url_for('main.users_list'))


@bp.route('/account/password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not current_password:
            flash('Current password is required.', 'error')
            return render_template('auth/change_password.html')

        if not new_password:
            flash('New password is required.', 'error')
            return render_template('auth/change_password.html')

        if new_password != confirm_password:
            flash('New password and confirmation do not match.', 'error')
            return render_template('auth/change_password.html')

        policy_errors = password_policy_errors(new_password, username=current_user.username)
        if policy_errors:
            for error in policy_errors:
                flash(error, 'error')
            return render_template('auth/change_password.html')

        if not current_user.check_password(current_password):
            flash('Current password is incorrect.', 'error')
            return render_template('auth/change_password.html')

        if is_password_reused(
            current_user,
            new_password,
            current_app.config.get('PASSWORD_HISTORY_COUNT', 3),
        ):
            flash('New password cannot match your current or recently used passwords.', 'error')
            return render_template('auth/change_password.html')

        old_password_hash = current_user.password_hash
        current_user.set_password(new_password)
        current_user.must_change_password = False
        current_user.rotate_session_nonce()
        store_password_history(
            current_user.id,
            old_password_hash,
            current_app.config.get('PASSWORD_HISTORY_COUNT', 3),
        )
        db.session.commit()
        _cleanup_bootstrap_admin_password_if_needed()

        record_auth_event(
            'password_changed',
            outcome='success',
            user=current_user,
            username=current_user.username,
            client_ip=request.remote_addr or 'unknown',
        )
        alert_result = send_security_alert(current_user, 'password_changed')
        if not alert_result.get('success'):
            record_auth_event(
                'alert_sms_failed',
                outcome='failed',
                user=current_user,
                username=current_user.username,
                client_ip=request.remote_addr or 'unknown',
                metadata={
                    'context': 'password_changed',
                    'reason': alert_result.get('reason'),
                    'skipped': alert_result.get('skipped', False),
                },
            )

        logout_user()
        session.clear()
        flash('Password updated successfully. Please sign in again.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/change_password.html')


@bp.route('/account/security-contact', methods=['GET', 'POST'])
@login_required
def security_contact():
    if request.method == 'POST':
        phone_input = request.form.get('phone', '').strip()
        if not phone_input:
            flash('Phone number is required.', 'error')
            return render_template('auth/security_contact.html')

        normalized_phone = normalize_phone(phone_input)
        if not validate_phone(normalized_phone):
            flash('Phone number must be a valid E.164 number.', 'error')
            return render_template('auth/security_contact.html')

        existing = _find_phone_conflict(
            normalized_phone,
            exclude_user_id=current_user.id,
            organization_id=_current_organization_id() if saas_mode_enabled() and not current_user.is_platform_admin else None,
        )
        if existing:
            if saas_mode_enabled() and not current_user.is_platform_admin:
                flash('That phone number is already assigned to another user in this organization.', 'error')
            else:
                flash('That phone number is already assigned to another user.', 'error')
            return render_template('auth/security_contact.html')

        current_user.phone = normalized_phone
        db.session.commit()
        record_auth_event(
            'security_contact_updated',
            outcome='success',
            user=current_user,
            username=current_user.username,
            client_ip=request.remote_addr or 'unknown',
        )
        flash('Security contact saved.', 'success')
        if current_user.must_change_password:
            return redirect(url_for('main.change_password'))
        return redirect(url_for(home_endpoint_for_user(current_user)))

    return render_template('auth/security_contact.html')


@bp.route('/platform/organizations')
@login_required
def platform_organizations_list():
    if not _can_manage_platform():
        abort(403)
    organization_rows = _platform_organization_rows()
    return render_template('platform/organizations_list.html', organization_rows=organization_rows)


@bp.route('/platform/organizations/add', methods=['GET', 'POST'])
@login_required
def platform_organizations_add():
    if not _can_manage_platform():
        abort(403)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        slug = request.form.get('slug', '').strip()
        owner_email = request.form.get('owner_email', '').strip().lower()
        owner_role = (request.form.get('owner_role') or 'owner').strip().lower()

        if not name:
            flash('Organization name is required.', 'error')
            return render_template('platform/organization_form.html')
        if not slug:
            flash('Organization slug is required.', 'error')
            return render_template('platform/organization_form.html')
        if not owner_email:
            flash('Owner email is required.', 'error')
            return render_template('platform/organization_form.html')
        if owner_role != 'owner':
            flash('The initial organization invite must be for an owner.', 'error')
            return render_template('platform/organization_form.html')
        _, owner_email_error = _organization_email_account_status(owner_email)
        if owner_email_error:
            flash(owner_email_error, 'error')
            return render_template('platform/organization_form.html')
        if Organization.query.filter_by(slug=slug).first():
            flash('That organization slug already exists.', 'error')
            return render_template('platform/organization_form.html')

        organization = Organization(name=name, slug=slug, status='active')
        subscription = OrganizationSubscription(
            organization=organization,
            stripe_price_id=current_app.config.get('STRIPE_PRICE_ID'),
            status='incomplete',
        )
        invitation = OrganizationInvitation(
            organization=organization,
            email=owner_email,
            role='owner',
            invited_by_user_id=current_user.id,
            expires_at=utc_now() + timedelta(days=7),
        )
        messaging_profile = OrganizationMessagingProfile(
            organization=organization,
            provider_mode='platform_managed',
            status='pending',
            provider_status='pending',
            sender_review_status='pending',
        )
        db.session.add_all([organization, subscription, invitation, messaging_profile])
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(
                'That sender identity is already assigned to another organization. '
                'Use a dedicated number or Messaging Service SID for each business.',
                'error',
            )
            return render_template('platform/organization_form.html')

        flash('Organization created and owner invite generated.', 'success')
        return redirect(url_for('main.platform_organizations_list'))

    return render_template('platform/organization_form.html')


@bp.route('/platform/organizations/<int:organization_id>/access')
@login_required
def platform_organizations_access(organization_id):
    if not _can_manage_platform():
        abort(403)

    organization = db.get_or_404(Organization, organization_id)
    return _render_platform_organization_access(organization)


@bp.route('/platform/organizations/<int:organization_id>/access/invite-staff', methods=['POST'])
@login_required
def platform_organizations_invite_staff(organization_id):
    if not _can_manage_platform():
        abort(403)

    organization = db.get_or_404(Organization, organization_id)
    email = request.form.get('email', '').strip().lower()
    if not email:
        flash('Staff email is required.', 'error')
        _record_platform_organization_access_event(
            'platform_organization_staff_invite',
            organization=organization,
            target_email=email,
            outcome='failed',
            reason='missing_email',
        )
        return _render_platform_organization_access(
            organization,
            staff_invite_email=email,
        )

    _, email_error = _organization_email_account_status(email)
    if email_error:
        flash(email_error, 'error')
        _record_platform_organization_access_event(
            'platform_organization_staff_invite',
            organization=organization,
            target_email=email,
            outcome='failed',
            reason='email_not_eligible',
        )
        return _render_platform_organization_access(
            organization,
            staff_invite_email=email,
        )

    existing_invitation = _organization_pending_invitation_for_email(organization.id, email)
    if existing_invitation is not None:
        flash('A pending invitation already exists for that email in this organization.', 'warning')
        _record_platform_organization_access_event(
            'platform_organization_staff_invite',
            organization=organization,
            target_email=email,
            outcome='failed',
            reason='duplicate_pending_invitation',
            invitation=existing_invitation,
        )
        return _render_platform_organization_access(
            organization,
            staff_invite_email=email,
        )

    invitation = OrganizationInvitation(
        organization_id=organization.id,
        email=email,
        role='staff',
        invited_by_user_id=current_user.id,
        expires_at=utc_now() + timedelta(days=7),
    )
    db.session.add(invitation)
    db.session.commit()

    _record_platform_organization_access_event(
        'platform_organization_staff_invite',
        organization=organization,
        target_email=email,
        outcome='success',
        invitation=invitation,
    )
    flash('Staff invitation created.', 'success')
    return redirect(url_for('main.platform_organizations_access', organization_id=organization.id))


@bp.route('/platform/organizations/<int:organization_id>/access/billing', methods=['POST'])
@login_required
def platform_organizations_update_billing(organization_id):
    if not _can_manage_platform():
        abort(403)

    organization = db.get_or_404(Organization, organization_id)
    action = (request.form.get('action') or '').strip().lower()
    if action == 'grant_complimentary':
        mark_subscription_complimentary(organization)
        _record_platform_organization_access_event(
            'platform_organization_billing_update',
            organization=organization,
            target_email=None,
            outcome='success',
            reason='grant_complimentary',
        )
        flash('Complimentary billing enabled for this organization.', 'success')
    elif action == 'clear_complimentary':
        clear_complimentary_subscription(organization)
        _record_platform_organization_access_event(
            'platform_organization_billing_update',
            organization=organization,
            target_email=None,
            outcome='success',
            reason='clear_complimentary',
        )
        flash('Complimentary billing cleared. Stripe-managed billing is required again.', 'success')
    else:
        _record_platform_organization_access_event(
            'platform_organization_billing_update',
            organization=organization,
            target_email=None,
            outcome='failed',
            reason='unsupported_action',
        )
        flash('Unsupported billing action.', 'error')
    return redirect(url_for('main.platform_organizations_access', organization_id=organization.id))


@bp.route('/platform/organizations/<int:organization_id>/access/reissue-owner-invite', methods=['POST'])
@login_required
def platform_organizations_reissue_owner_invite(organization_id):
    if not _can_manage_platform():
        abort(403)

    organization = db.get_or_404(Organization, organization_id)
    owner_email = request.form.get('owner_email', '').strip().lower()
    owner_membership = _organization_owner_membership(organization)
    if owner_membership is not None:
        flash(
            'An owner has already joined this organization. Owner invite recovery is no longer available.',
            'error',
        )
        _record_platform_organization_access_event(
            'platform_organization_owner_invite_reissue',
            organization=organization,
            target_email=owner_email,
            outcome='failed',
            reason='owner_already_joined',
        )
        return _render_platform_organization_access(
            organization,
            owner_reissue_email=owner_email,
        )

    if not owner_email:
        flash('Owner email is required.', 'error')
        _record_platform_organization_access_event(
            'platform_organization_owner_invite_reissue',
            organization=organization,
            target_email=owner_email,
            outcome='failed',
            reason='missing_email',
        )
        return _render_platform_organization_access(
            organization,
            owner_reissue_email=owner_email,
        )

    _, owner_email_error = _organization_email_account_status(owner_email)
    if owner_email_error:
        flash(owner_email_error, 'error')
        _record_platform_organization_access_event(
            'platform_organization_owner_invite_reissue',
            organization=organization,
            target_email=owner_email,
            outcome='failed',
            reason='email_not_eligible',
        )
        return _render_platform_organization_access(
            organization,
            owner_reissue_email=owner_email,
        )

    existing_pending_email_invitation = _organization_pending_invitation_for_email(organization.id, owner_email)
    if existing_pending_email_invitation is not None and existing_pending_email_invitation.role != 'owner':
        flash('A pending invitation already exists for that email in this organization.', 'warning')
        _record_platform_organization_access_event(
            'platform_organization_owner_invite_reissue',
            organization=organization,
            target_email=owner_email,
            outcome='failed',
            reason='duplicate_pending_invitation',
            invitation=existing_pending_email_invitation,
        )
        return _render_platform_organization_access(
            organization,
            owner_reissue_email=owner_email,
        )

    pending_owner_invitations = (
        OrganizationInvitation.query
        .filter_by(
            organization_id=organization.id,
            role='owner',
            status='pending',
        )
        .order_by(OrganizationInvitation.created_at.desc(), OrganizationInvitation.id.desc())
        .all()
    )
    revoked_count = 0
    for invitation in pending_owner_invitations:
        invitation.status = 'revoked'
        revoked_count += 1

    invitation = OrganizationInvitation(
        organization_id=organization.id,
        email=owner_email,
        role='owner',
        invited_by_user_id=current_user.id,
        expires_at=utc_now() + timedelta(days=7),
    )
    db.session.add(invitation)
    db.session.commit()

    _record_platform_organization_access_event(
        'platform_organization_owner_invite_reissue',
        organization=organization,
        target_email=owner_email,
        outcome='success',
        invitation=invitation,
        revoked_count=revoked_count,
    )
    flash('Owner invite reissued with a fresh link.', 'success')
    return redirect(url_for('main.platform_organizations_access', organization_id=organization.id))


@bp.route('/platform/organizations/<int:organization_id>/messaging', methods=['GET', 'POST'])
@login_required
def platform_organizations_messaging_edit(organization_id):
    if not _can_manage_platform():
        abort(403)

    organization = db.get_or_404(Organization, organization_id)
    messaging_profile = organization.messaging_profile or ensure_messaging_profile(organization)
    if organization.messaging_profile is None:
        db.session.commit()

    def render_page():
        return render_template(
            'platform/organization_messaging_form.html',
            organization=organization,
            messaging_profile=organization.messaging_profile or messaging_profile,
            onboarding=organization.a2p_onboarding,
            a2p_status=_a2p_status_view(organization.a2p_onboarding, organization.messaging_profile or messaging_profile),
        )

    if request.method == 'POST':
        action = (request.form.get('action') or 'save').strip().lower()
        try:
            if action == 'provision':
                if messaging_profile.provider_mode == 'customer_managed':
                    raise ProviderProvisioningError(
                        'Customer-managed Twilio workspaces do not use platform provisioning.'
                    )
                provision_org(organization.id, actor_user_id=current_user.id)
                flash('Twilio provider provisioned for this organization.', 'success')
                return redirect(url_for('main.platform_organizations_messaging_edit', organization_id=organization.id))
            if action == 'suspend':
                suspend_org(organization.id, actor_user_id=current_user.id)
                flash('Twilio provider suspended for this organization.', 'success')
                return redirect(url_for('main.platform_organizations_messaging_edit', organization_id=organization.id))
            if action == 'resume':
                resume_org(organization.id, actor_user_id=current_user.id)
                flash('Twilio provider resumed for this organization.', 'success')
                return redirect(url_for('main.platform_organizations_messaging_edit', organization_id=organization.id))
            if action == 'release_sender':
                if messaging_profile.provider_mode == 'customer_managed':
                    raise ProviderProvisioningError(
                        'Customer-managed sender state is updated from the external Twilio account configuration.'
                    )
                release_sender(organization.id, actor_user_id=current_user.id)
                flash('Sender assignment released for this organization.', 'success')
                return redirect(url_for('main.platform_organizations_messaging_edit', organization_id=organization.id))
        except ProviderProvisioningError as exc:
            flash(str(exc), 'error')
            return redirect(url_for('main.platform_organizations_messaging_edit', organization_id=organization.id))

        provider_mode = _normalized_provider_mode(
            request.form.get('provider_mode'),
            default=messaging_profile.provider_mode or 'platform_managed',
        )
        sender_number = request.form.get('sender_number', '').strip() or None
        business_type = request.form.get('business_type', '').strip() or None
        use_case = request.form.get('use_case', '').strip() or None
        if provider_mode == 'customer_managed':
            twilio_account_sid = request.form.get('twilio_account_sid', '').strip() or None
            twilio_auth_token = _customer_managed_auth_token_for_save(
                messaging_profile,
                requested_account_sid=twilio_account_sid,
                raw_auth_token=request.form.get('twilio_auth_token', ''),
            )
            messaging_service_sid = request.form.get('messaging_service_sid', '').strip() or None
            messaging_error, normalized_sender, normalized_service_sid = _validate_org_messaging_profile_input(
                sender_number,
                messaging_service_sid,
                organization_id=organization.id,
            )
            if messaging_error:
                flash(messaging_error, 'error')
                return render_page()
            if not twilio_account_sid:
                flash('Twilio account SID is required for customer-managed providers.', 'error')
                return render_page()
            if not twilio_auth_token:
                flash('Twilio auth token is required to validate customer-managed providers.', 'error')
                return render_page()
            try:
                messaging_profile, validation_result = save_customer_managed_profile(
                    organization.id,
                    twilio_account_sid=twilio_account_sid,
                    twilio_auth_token=twilio_auth_token,
                    from_number=normalized_sender or '',
                    messaging_service_sid=normalized_service_sid,
                    business_type=business_type,
                    use_case=use_case,
                    actor_user_id=current_user.id,
                )
                ensure_a2p_event_stream_subscription(organization, messaging_profile)
                _sync_customer_managed_onboarding_state(organization, validation_result)
                db.session.commit()
            except ProviderProvisioningError as exc:
                flash(str(exc), 'error')
                return render_page()
            flash('Customer-managed Twilio settings validated and synced.', 'success')
            return redirect(url_for('main.platform_organizations_messaging_edit', organization_id=organization.id))

        phone_number_sid = request.form.get('phone_number_sid', '').strip() or None
        sender_review_status = (request.form.get('sender_review_status') or 'pending').strip().lower()
        consent_acknowledged = request.form.get('consent_acknowledged') == 'on'

        if not messaging_profile.messaging_service_sid:
            flash('Provision the Twilio provider before assigning a sender.', 'error')
            return render_page()

        messaging_error, normalized_sender, _ = _validate_org_messaging_profile_input(
            sender_number,
            None,
            organization_id=organization.id,
        )
        if messaging_error:
            flash(messaging_error, 'error')
            return render_page()

        if phone_number_sid and not normalized_sender:
            flash('A sender number is required when a phone number SID is provided.', 'error')
            return render_page()

        if normalized_sender and not phone_number_sid:
            flash('A phone number SID is required when a sender number is provided.', 'error')
            return render_page()

        messaging_profile.provider_mode = 'platform_managed'
        messaging_profile.twilio_account_sid = None
        if not messaging_profile.twilio_subaccount_sid:
            messaging_profile.twilio_auth_token_encrypted = None
        messaging_profile.from_number = normalized_sender
        messaging_profile.phone_number_sid = phone_number_sid
        messaging_profile.business_type = business_type
        messaging_profile.use_case = use_case
        messaging_profile.sender_review_status = sender_review_status
        messaging_profile.inbound_identity = normalized_sender or messaging_profile.messaging_service_sid
        messaging_profile.provider_last_checked_at = utc_now()
        messaging_profile.consent_acknowledged_at = utc_now() if consent_acknowledged else None
        if messaging_profile.provider_status != 'suspended':
            if (
                normalized_sender
                and phone_number_sid
                and messaging_profile.messaging_service_sid
                and sender_review_status == 'approved'
                and messaging_profile.consent_acknowledged_at is not None
            ):
                messaging_profile.set_provider_status('active')
            else:
                messaging_profile.set_provider_status('pending')

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(
                'That sender identity is already assigned to another organization. '
                'Use a dedicated sender number for each business.',
                'error',
            )
            messaging_profile = organization.messaging_profile
            return render_page()

        if normalized_sender and phone_number_sid:
            try:
                sync_sender_assignment(organization.id, actor_user_id=current_user.id)
            except ProviderProvisioningError as exc:
                flash(str(exc), 'error')
                return redirect(url_for('main.platform_organizations_messaging_edit', organization_id=organization.id))

        flash('Messaging provider settings updated.', 'success')
        return redirect(url_for('main.platform_organizations_messaging_edit', organization_id=organization.id))

    return render_page()


@bp.route('/platform/organizations/<int:organization_id>/messaging/onboarding', methods=['GET', 'POST'])
@login_required
def platform_organizations_messaging_onboarding(organization_id):
    if not _can_manage_platform():
        abort(403)

    organization = db.get_or_404(Organization, organization_id)
    messaging_profile = organization.messaging_profile or ensure_messaging_profile(organization)
    onboarding = organization.a2p_onboarding or ensure_a2p_onboarding(organization)
    if organization.messaging_profile is None or organization.a2p_onboarding is None:
        db.session.commit()

    def render_page():
        return render_template(
            'platform/organization_a2p_onboarding_form.html',
            organization=organization,
            messaging_profile=messaging_profile,
            onboarding=onboarding,
            a2p_status=_a2p_status_view(onboarding, messaging_profile),
            a2p_form_defaults=_a2p_form_defaults(onboarding),
            hosted_compliance_urls=hosted_a2p_compliance_urls(organization),
            a2p_source_defaults=_a2p_source_defaults(organization, onboarding),
            business_industry_choices=a2p_business_industry_choices(),
            business_region_choices=a2p_business_region_choices(),
            business_type_choices=a2p_business_type_choices(),
            job_position_choices=a2p_job_position_choices(),
            registration_path_choices=a2p_registration_path_choices(),
            registration_identifier_choices=a2p_registration_identifier_choices(),
            number_strategy_choices=a2p_number_strategy_choices(),
            campaign_use_case_choices=a2p_campaign_use_case_choices(),
            customer_managed_a2p=messaging_profile.provider_mode == 'customer_managed',
        )

    if request.method == 'POST':
        action = (request.form.get('action') or 'submit').strip().lower()
        if messaging_profile.provider_mode == 'customer_managed':
            if action == 'refresh':
                flash(
                    'A2P is externally managed for customer-managed Twilio workspaces. '
                    'Refresh the external Twilio account directly, then re-save messaging settings if status changed.',
                    'info',
                )
                return redirect(
                    url_for('main.platform_organizations_messaging_onboarding', organization_id=organization.id)
                )
            flash('A2P is externally managed for customer-managed Twilio workspaces.', 'error')
            return redirect(url_for('main.platform_organizations_messaging_onboarding', organization_id=organization.id))
        try:
            if action == 'submit':
                payload = request.form.to_dict(flat=True)
                payload['business_regions'] = request.form.getlist('business_regions')
                submit_a2p_onboarding(
                    organization.id,
                    payload,
                    actor_user_id=current_user.id,
                )
                flash('Twilio A2P onboarding queued for processing.', 'success')
            elif action == 'refresh':
                refresh_a2p_onboarding(organization.id, actor_user_id=current_user.id)
                flash('Twilio A2P onboarding refresh queued.', 'success')
            elif action == 'cancel':
                cancel_a2p_onboarding(organization.id, actor_user_id=current_user.id)
                flash('Twilio A2P onboarding canceled.', 'success')
            else:
                flash('Unsupported onboarding action.', 'error')
                return redirect(url_for('main.platform_organizations_messaging_onboarding', organization_id=organization.id))
        except ProviderProvisioningError as exc:
            flash(str(exc), 'error')
            onboarding = organization.a2p_onboarding or onboarding
            return render_page()
        return redirect(url_for('main.platform_organizations_messaging_onboarding', organization_id=organization.id))

    return render_page()


@bp.route('/platform/organizations/<int:organization_id>/toggle-status', methods=['POST'])
@login_required
def platform_organizations_toggle_status(organization_id):
    if not _can_manage_platform():
        abort(403)

    organization = db.get_or_404(Organization, organization_id)
    target_status = 'suspended' if organization.status != 'suspended' else 'active'
    organization.status = target_status
    try:
        if organization.messaging_profile is not None:
            if target_status == 'suspended':
                suspend_org(organization.id, actor_user_id=current_user.id)
            else:
                resume_org(organization.id, actor_user_id=current_user.id)
        else:
            db.session.commit()
    except Exception:
        db.session.rollback()
        persisted = db.session.get(Organization, organization_id)
        current_status = persisted.status if persisted is not None else 'unknown'
        current_app.logger.exception(
            'Failed to sync organization_id=%s to status=%s during platform toggle.',
            organization_id,
            target_status,
        )
        flash(
            f'Could not update organization status to {target_status}. Organization remains {current_status}.',
            'error',
        )
        return redirect(url_for('main.platform_organizations_list'))

    flash(f'Organization status updated to {target_status}.', 'success')
    return redirect(url_for('main.platform_organizations_list'))


@bp.route('/invites/<token>', methods=['GET', 'POST'])
def invitation_accept(token):
    invitation = OrganizationInvitation.query.filter_by(token=token).first_or_404()
    if invitation.status != 'pending':
        flash('This invitation is no longer active.', 'error')
        return redirect(url_for('auth.login'))
    expires_at = as_utc_datetime(invitation.expires_at)
    if expires_at and expires_at < utc_now():
        invitation.status = 'expired'
        db.session.commit()
        flash('This invitation has expired.', 'error')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        full_name = request.form.get('full_name', '').strip() or None
        phone_input = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not username:
            flash('Username is required.', 'error')
            return render_template('auth/accept_invitation.html', invitation=invitation)
        if not phone_input:
            flash('Phone number is required.', 'error')
            return render_template('auth/accept_invitation.html', invitation=invitation)
        if password != confirm_password:
            flash('Password confirmation does not match.', 'error')
            return render_template('auth/accept_invitation.html', invitation=invitation)
        policy_errors = password_policy_errors(password, username=username)
        if policy_errors:
            for error in policy_errors:
                flash(error, 'error')
            return render_template('auth/accept_invitation.html', invitation=invitation)

        normalized_phone = normalize_phone(phone_input)
        if not validate_phone(normalized_phone):
            flash('Phone number must be a valid E.164 number.', 'error')
            return render_template('auth/accept_invitation.html', invitation=invitation)
        if _find_username_conflict(username):
            flash('That username is already taken.', 'error')
            return render_template('auth/accept_invitation.html', invitation=invitation)

        existing_user, invitation_email_error = _organization_email_account_status(invitation.email)
        if invitation_email_error:
            flash(invitation_email_error, 'error')
            return render_template('auth/accept_invitation.html', invitation=invitation)
        phone_conflict = _find_phone_conflict(
            normalized_phone,
            exclude_user_id=existing_user.id if existing_user is not None else None,
            organization_id=invitation.organization_id if saas_mode_enabled() else None,
        )
        if phone_conflict:
            flash('That phone number is already assigned to another user in this organization.', 'error')
            return render_template('auth/accept_invitation.html', invitation=invitation)

        user = existing_user or AppUser(
            username=username,
            email=invitation.email,
            full_name=full_name,
            phone=normalized_phone,
            role='admin' if invitation.role == 'owner' else 'social_manager',
            must_change_password=False,
        )
        user.username = username
        user.email = invitation.email
        user.full_name = full_name
        user.phone = normalized_phone
        user.role = 'admin' if invitation.role == 'owner' else 'social_manager'
        user.set_password(password)
        if existing_user is None:
            db.session.add(user)
            db.session.flush()

        membership = OrganizationMembership(
            organization_id=invitation.organization_id,
            user_id=user.id,
            role=invitation.role,
        )
        db.session.add(membership)
        invitation.status = 'accepted'
        invitation.accepted_at = utc_now()
        db.session.commit()

        session.clear()
        login_user(user)
        if invitation.role == 'owner':
            return redirect(url_for('main.setup'))
        flash('Invitation accepted.', 'success')
        return redirect(url_for('main.dashboard'))

    return render_template('auth/accept_invitation.html', invitation=invitation)


@bp.route('/_test/stripe/checkout/<session_id>', methods=['GET', 'POST'])
@login_required
def fake_stripe_checkout(session_id):
    if not current_app.config.get('STRIPE_FAKE_CHECKOUT_ENABLED'):
        abort(404)
    if not is_fake_checkout_session_id(session_id):
        abort(404)
    if _can_manage_platform():
        abort(403)
    if getattr(current_user, 'organization_role', None) != 'owner':
        abort(403)

    organization = _current_organization()
    if organization is None:
        abort(404)

    try:
        requested_org_id = int((request.values.get('organization_id') or '').strip())
    except ValueError:
        abort(400)
    if requested_org_id != organization.id:
        abort(404)

    success_url = (request.values.get('success_url') or '').strip()
    cancel_url = (request.values.get('cancel_url') or '').strip()
    resolved_success_url = success_url.replace('{CHECKOUT_SESSION_ID}', session_id)
    if (
        not success_url
        or not cancel_url
        or not is_safe_url(resolved_success_url, request.host_url)
        or not is_safe_url(cancel_url, request.host_url)
    ):
        abort(400)

    if request.method == 'POST':
        action = (request.form.get('action') or 'complete').strip().lower()
        target_url = cancel_url if action == 'cancel' else resolved_success_url
        return redirect(target_url)

    return render_template(
        'testing/fake_checkout.html',
        organization=organization,
        session_id=session_id,
        success_url=success_url,
        cancel_url=cancel_url,
    )


@bp.route('/compliance/<organization_slug>/sms/privacy', methods=['GET'])
def hosted_sms_privacy_policy(organization_slug):
    organization = _public_organization_by_slug_or_404(organization_slug)
    onboarding = organization.a2p_onboarding
    hosted_urls = hosted_a2p_compliance_urls(organization)
    return render_template(
        'public/sms_compliance_page.html',
        organization=organization,
        onboarding=onboarding,
        hosted_compliance_urls=hosted_urls,
        page_kind='privacy',
        page_title='SMS Privacy Policy',
    )


@bp.route('/compliance/<organization_slug>/sms/terms', methods=['GET'])
def hosted_sms_terms_and_conditions(organization_slug):
    organization = _public_organization_by_slug_or_404(organization_slug)
    onboarding = organization.a2p_onboarding
    hosted_urls = hosted_a2p_compliance_urls(organization)
    return render_template(
        'public/sms_compliance_page.html',
        organization=organization,
        onboarding=onboarding,
        hosted_compliance_urls=hosted_urls,
        page_kind='terms',
        page_title='SMS Terms and Conditions',
    )


@bp.route('/compliance/<organization_slug>/sms/opt-in', methods=['GET'])
def hosted_sms_opt_in(organization_slug):
    organization = _public_organization_by_slug_or_404(organization_slug)
    onboarding = organization.a2p_onboarding
    hosted_urls = hosted_a2p_compliance_urls(organization)
    return render_template(
        'public/sms_compliance_page.html',
        organization=organization,
        onboarding=onboarding,
        hosted_compliance_urls=hosted_urls,
        page_kind='opt_in',
        page_title='SMS Opt-In and Consent',
    )


@bp.route('/setup', methods=['GET', 'POST'])
@login_required
def setup():
    if _can_manage_platform():
        return redirect(url_for('main.platform_home'))

    organization = _current_organization()
    if organization is None:
        abort(404)
    if getattr(current_user, 'organization_role', None) != 'owner':
        return redirect(url_for('main.setup_pending'))

    messaging_profile = organization.messaging_profile or ensure_messaging_profile(organization)
    setup_is_customer_managed = messaging_profile.provider_mode == 'customer_managed'
    onboarding = organization.a2p_onboarding
    if not setup_is_customer_managed and onboarding is None:
        onboarding = ensure_a2p_onboarding(organization)
    if organization.messaging_profile is None or (not setup_is_customer_managed and organization.a2p_onboarding is None):
        db.session.commit()

    session_id = request.args.get('session_id', '').strip()
    try:
        if session_id:
            sync_checkout_session_by_id(session_id, organization)
        elif not organization_can_send(organization) and _should_reconcile_subscription(organization, session_id):
            refresh_subscription_from_stripe(organization, current_user.email or '')
    except Exception:
        current_app.logger.exception('Failed to reconcile setup billing state for organization %s.', organization.id)

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if setup_is_customer_managed and action in {'save_compliance', 'submit_onboarding', 'refresh_onboarding', 'cancel_onboarding'}:
            flash(
                'Customer-managed Twilio activation is handled by your platform admin. This workspace cannot edit external compliance here.',
                'info',
            )
            return redirect(url_for('main.setup', step='provider'))
        try:
            if action == 'save_compliance':
                payload = request.form.to_dict(flat=True)
                payload['business_regions'] = request.form.getlist('business_regions')
                save_a2p_onboarding_draft(
                    organization.id,
                    payload,
                    actor_user_id=current_user.id,
                )
                flash('Business profile saved.', 'success')
                return redirect(url_for('main.setup', step='review'))
            if action == 'submit_onboarding':
                if request.form.get("declaration_accepted") != "on":
                    raise ProviderProvisioningError(
                        "You must confirm the business declaration before submitting A2P onboarding."
                    )
                payload = _setup_submit_payload_from_onboarding(onboarding)
                payload["declaration_accepted"] = request.form.get("declaration_accepted", "")
                submit_a2p_onboarding(
                    organization.id,
                    payload,
                    actor_user_id=current_user.id,
                )
                flash('Twilio A2P onboarding queued for review.', 'success')
                return redirect(url_for('main.setup', step='launch'))
            if action == 'refresh_onboarding':
                refresh_a2p_onboarding(organization.id, actor_user_id=current_user.id)
                flash('Twilio A2P onboarding refresh queued.', 'success')
                return redirect(url_for('main.setup', step='launch'))
            if action == 'cancel_onboarding':
                cancel_a2p_onboarding(organization.id, actor_user_id=current_user.id)
                flash('Twilio A2P onboarding canceled.', 'success')
                return redirect(url_for('main.setup', step='review'))
        except ProviderProvisioningError as exc:
            flash(str(exc), 'error')

    requested_step = (request.args.get('step') or '').strip().lower()
    available_steps = {'account', 'billing', 'launch'}
    if setup_is_customer_managed:
        available_steps.add('provider')
    else:
        available_steps.update({'compliance', 'review'})
    current_step = requested_step if requested_step in available_steps else _setup_current_step(organization)
    a2p_status = _a2p_status_view(onboarding, messaging_profile)

    return render_template(
        'setup/index.html',
        organization=organization,
        messaging_profile=messaging_profile,
        onboarding=onboarding,
        a2p_status=a2p_status,
        setup_is_customer_managed=setup_is_customer_managed,
        current_step=current_step,
        setup_steps=_setup_steps_view(organization),
        setup_status=_setup_status_payload(organization),
        subscription=organization.subscription,
        subscription_view=_subscription_view(organization.subscription),
        a2p_form_defaults=_a2p_form_defaults(onboarding),
        hosted_compliance_urls=hosted_a2p_compliance_urls(organization) if not setup_is_customer_managed else None,
        a2p_source_defaults=_a2p_source_defaults(organization, onboarding) if not setup_is_customer_managed else None,
        business_type_choices=a2p_business_type_choices(),
        business_industry_choices=a2p_business_industry_choices(),
        business_region_choices=a2p_business_region_choices(),
        job_position_choices=a2p_job_position_choices(),
        registration_identifier_choices=a2p_registration_identifier_choices(),
        registration_path_choices=a2p_registration_path_choices(),
        campaign_use_case_choices=a2p_campaign_use_case_choices(),
    )


@bp.route('/setup/pending')
@login_required
def setup_pending():
    if _can_manage_platform():
        return redirect(url_for('main.platform_home'))

    organization = _current_organization()
    if organization is None:
        abort(404)
    if getattr(current_user, 'organization_role', None) == 'owner':
        return redirect(url_for('main.setup'))
    messaging_profile = organization.messaging_profile
    onboarding = organization.a2p_onboarding
    a2p_status = _a2p_status_view(onboarding, messaging_profile)

    return render_template(
        'setup/pending.html',
        organization=organization,
        messaging_profile=messaging_profile,
        a2p_status=a2p_status,
        setup_is_customer_managed=_organization_uses_customer_managed_messaging(organization),
        setup_steps=_setup_steps_view(organization),
        setup_status=_setup_status_payload(organization),
    )


@bp.route('/setup/status')
@login_required
def setup_status():
    if _can_manage_platform():
        abort(403)
    organization = _current_organization()
    if organization is None:
        abort(404)
    return jsonify(_setup_status_payload(organization))


@bp.route('/setup/billing/checkout', methods=['POST'])
@login_required
def setup_billing_checkout():
    if _can_manage_platform():
        abort(403)
    if getattr(current_user, 'organization_role', None) != 'owner':
        abort(403)

    organization = _current_organization()
    if organization is None:
        abort(404)
    if organization_can_send(organization):
        return redirect(url_for('main.setup'))

    success_url = f"{_absolute_url('main.setup')}?step=billing&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{_absolute_url('main.setup')}?step=billing"
    try:
        checkout_session = create_checkout_session(
            organization,
            current_user.email or '',
            success_url,
            cancel_url,
        )
    except Exception as exc:
        current_app.logger.exception('Failed to create setup Stripe checkout session.')
        flash(str(exc), 'error')
        return redirect(url_for('main.setup', step='billing'))
    return redirect(checkout_session.url, code=303)


@bp.route('/billing')
@login_required
def billing_overview():
    _require_billing_access()
    organization = _current_organization()
    if organization is not None:
        session_id = request.args.get('session_id', '').strip()
        try:
            if session_id:
                sync_checkout_session_by_id(session_id, organization)
            elif not organization_can_send(organization) and _should_reconcile_subscription(organization, session_id):
                refresh_subscription_from_stripe(organization, current_user.email or '')
        except Exception:
            current_app.logger.exception('Failed to reconcile Stripe subscription state for organization %s.', organization.id)
    billing_context = _billing_context(organization)
    return render_template(
        'billing/overview.html',
        organization=organization,
        subscription=_current_subscription(),
        subscription_view=billing_context['subscription_view'],
        onboarding_view=billing_context['onboarding_view'],
    )


@bp.route('/billing/checkout', methods=['GET', 'POST'])
@login_required
def billing_checkout():
    _require_billing_access()

    organization = _current_organization()
    if organization is None:
        abort(404)
    if organization_can_send(organization):
        return redirect(url_for('main.dashboard'))

    if request.method != 'POST':
        return redirect(url_for('main.billing_overview'))

    success_url = f"{_absolute_url('main.setup')}?step=billing&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{_absolute_url('main.setup')}?step=billing"
    try:
        checkout_session = create_checkout_session(
            organization,
            current_user.email or '',
            success_url,
            cancel_url,
        )
    except Exception as exc:
        current_app.logger.exception('Failed to create Stripe checkout session.')
        flash(str(exc), 'error')
        return redirect(url_for('main.billing_overview'))
    return redirect(checkout_session.url, code=303)


@bp.route('/billing/portal', methods=['POST'])
@login_required
def billing_portal():
    _require_billing_access()

    organization = _current_organization()
    if organization is None:
        abort(404)

    try:
        portal_session = create_billing_portal_session(
            organization,
            _absolute_url('main.billing_overview'),
        )
    except Exception as exc:
        current_app.logger.exception('Failed to create Stripe billing portal session.')
        flash(str(exc), 'error')
        return redirect(url_for('main.billing_overview'))
    return redirect(portal_session.url, code=303)


@bp.route('/webhooks/stripe', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    payload = request.get_data(as_text=True)
    signature = request.headers.get('Stripe-Signature', '')
    webhook_secret = current_app.config.get('STRIPE_WEBHOOK_SECRET')
    if not webhook_secret:
        current_app.logger.error('Stripe webhook received without STRIPE_WEBHOOK_SECRET configured.')
        return 'Not configured', 500

    try:
        import stripe  # type: ignore
    except ImportError:
        current_app.logger.exception('Stripe package is unavailable for webhook processing.')
        return 'Dependency missing', 500

    try:
        stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')
        event = stripe.Webhook.construct_event(payload=payload, sig_header=signature, secret=webhook_secret)
    except Exception:
        current_app.logger.exception('Stripe webhook signature verification failed.')
        return 'Forbidden', 403

    try:
        process_stripe_webhook_event(event)
    except Exception:
        current_app.logger.exception('Stripe webhook processing failed for event %s.', event.get('id'))
        return 'Webhook processing failed', 500

    return '', 200


@bp.route('/webhooks/twilio/trusthub-status', methods=['POST'])
@csrf.exempt
def twilio_trusthub_status_webhook():
    validation = validate_inbound_signature_detailed(
        _absolute_url('main.twilio_trusthub_status_webhook'),
        request.form.to_dict(flat=True),
        request.headers.get('X-Twilio-Signature'),
    )
    if not validation.is_valid:
        current_app.logger.warning(
            'Rejected Trust Hub webhook due to Twilio signature validation failure. reason=%s',
            validation.reason,
        )
        return '', 403

    sid_candidates = {
        value.strip()
        for value in request.form.values()
        if re.match(r'^(AC|BN|BU|EL|IT|MG|PN|RN)[A-Za-z0-9]+$', value.strip())
    }
    onboarding = None
    if sid_candidates:
        onboarding = (
            OrganizationA2POnboarding.query.filter(
                db.or_(
                    OrganizationA2POnboarding.customer_profile_sid.in_(sid_candidates),
                    OrganizationA2POnboarding.trust_product_sid.in_(sid_candidates),
                    OrganizationA2POnboarding.brand_registration_sid.in_(sid_candidates),
                    OrganizationA2POnboarding.vetting_sid.in_(sid_candidates),
                    OrganizationA2POnboarding.campaign_sid.in_(sid_candidates),
                )
            ).first()
        )
    if onboarding is not None:
        try:
            refresh_a2p_onboarding(onboarding.organization_id)
        except ProviderProvisioningError:
            current_app.logger.info(
                'Ignoring Trust Hub callback refresh for organization_id=%s in status=%s.',
                onboarding.organization_id,
                onboarding.onboarding_status,
            )
    return '', 204


# Community Members Management
@bp.route('/community')
@login_required
def community_list():
    search = request.args.get('search', '').strip()
    
    query = CommunityMember.query
    
    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        search_filters = [
            CommunityMember.name.ilike(pattern, escape='\\'),
            CommunityMember.phone.ilike(pattern, escape='\\'),
        ]
        normalized_search_phone = normalize_phone(search)
        if validate_phone(normalized_search_phone):
            search_filters.append(CommunityMember.phone == normalized_search_phone)
        search_digits = re.sub(r'\D', '', search)
        if search_digits:
            digits_pattern = f'%{escape_like(search_digits)}%'
            search_filters.append(phone_digits_sql(CommunityMember.phone).ilike(digits_pattern, escape='\\'))
        query = query.filter(
            db.or_(*search_filters)
        )
    
    members = query.order_by(CommunityMember.name, CommunityMember.phone).all()
    unsubscribed_phones = get_unsubscribed_phone_set([member.phone for member in members])
    return render_template('community/list.html', members=members, search=search, unsubscribed_phones=unsubscribed_phones)


@bp.route('/community/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def community_add():
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or None
        phone = request.form.get('phone', '').strip()
        
        if not phone:
            flash('Phone number is required.', 'error')
            return render_template('community/form.html', member=None)
        
        phone = normalize_phone(phone)
        if not validate_phone(phone):
            flash('Invalid phone number format.', 'error')
            return render_template('community/form.html', member=None)

        if UnsubscribedContact.query.filter_by(phone=phone).first():
            flash('This number is currently unsubscribed and will not receive messages.', 'warning')
        
        # Check for duplicate
        existing = CommunityMember.query.filter_by(phone=phone).first()
        if existing:
            flash('A member with this phone number already exists.', 'error')
            return render_template('community/form.html', member=None)
        
        member = CommunityMember(name=name, phone=phone)
        db.session.add(member)
        db.session.commit()
        
        flash('Community member added successfully.', 'success')
        return redirect(url_for('main.community_list'))
    
    return render_template('community/form.html', member=None)


@bp.route('/community/<int:member_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def community_edit(member_id):
    member = _tenant_get_or_404(CommunityMember, member_id)
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or None
        phone = request.form.get('phone', '').strip()
        
        if not phone:
            flash('Phone number is required.', 'error')
            return render_template('community/form.html', member=member)
        
        phone = normalize_phone(phone)
        if not validate_phone(phone):
            flash('Invalid phone number format.', 'error')
            return render_template('community/form.html', member=member)

        if UnsubscribedContact.query.filter_by(phone=phone).first():
            flash('This number is currently unsubscribed and will not receive messages.', 'warning')
        
        # Check for duplicate (excluding current)
        existing = CommunityMember.query.filter(
            CommunityMember.phone == phone,
            CommunityMember.id != member_id
        ).first()
        if existing:
            flash('A member with this phone number already exists.', 'error')
            return render_template('community/form.html', member=member)
        
        member.name = name
        member.phone = phone
        db.session.commit()
        
        flash('Community member updated successfully.', 'success')
        return redirect(url_for('main.community_list'))
    
    return render_template('community/form.html', member=member)


@bp.route('/community/<int:member_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def community_delete(member_id):
    member = _tenant_get_or_404(CommunityMember, member_id)
    db.session.delete(member)
    db.session.commit()
    flash('Community member deleted.', 'success')
    return redirect(url_for('main.community_list'))


@bp.route('/community/export')
@login_required
@require_roles('admin')
def community_export():
    members = CommunityMember.query.order_by(CommunityMember.name, CommunityMember.phone).all()

    def rows():
        yield ['name', 'phone', 'created_at']
        for member in members:
            yield [
                member.name or '',
                member.phone,
                member.created_at.isoformat() if member.created_at else '',
            ]

    return _csv_download_response('community_members.csv', rows())


@bp.route('/community/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin')
def community_bulk_delete():
    raw_ids = request.form.getlist('member_ids')
    member_ids = []
    for raw in raw_ids:
        try:
            member_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    member_ids = sorted(set(member_ids))
    if not member_ids:
        flash('No members selected.', 'warning')
        return redirect(url_for('main.community_list'))

    deleted = _tenant_bulk_filter(
        CommunityMember.query.filter(CommunityMember.id.in_(member_ids)),
        CommunityMember,
    ).delete(synchronize_session=False)
    db.session.commit()
    flash(f'Deleted {deleted} member(s).', 'success')
    return redirect(url_for('main.community_list'))


@bp.route('/community/import', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def community_import():
    if request.method == 'POST':
        file = None
        try:
            file, content, error_response = _read_uploaded_csv_text(template_name='community/import.html')
            if error_response is not None:
                return error_response
            parsed = parse_recipients_csv(content)

            if not parsed:
                flash('No valid members found in CSV.', 'error')
                return render_template('community/import.html')

            parsed, skipped = _dedupe_recipients_by_phone(parsed)
            added = 0

            for rec in parsed:
                phone = rec['phone']

                existing = CommunityMember.query.filter_by(phone=phone).first()
                if existing:
                    skipped += 1
                    continue

                member = CommunityMember(
                    name=rec['name'],
                    phone=phone
                )
                db.session.add(member)
                added += 1

            db.session.commit()
            flash(f'Imported {added} members. {skipped} duplicates skipped.', 'success')
            return redirect(url_for('main.community_list'))

        except Exception:
            current_app.logger.exception(
                'Community CSV import failed (filename=%r, user_id=%s).',
                file.filename if file is not None else None,
                current_user.id if current_user.is_authenticated else None,
            )
            db.session.rollback()
            flash(CSV_IMPORT_ERROR_FLASH, 'error')
    
    return render_template('community/import.html')


@bp.route('/community/<int:member_id>/unsubscribe', methods=['POST'])
@login_required
@require_roles('admin')
def community_unsubscribe(member_id):
    member = _tenant_get_or_404(CommunityMember, member_id)
    existing = UnsubscribedContact.query.filter_by(phone=member.phone).first()
    if existing:
        flash('That number is already unsubscribed.', 'warning')
        return redirect(url_for('main.community_list'))

    unsubscribe = UnsubscribedContact(
        name=member.name,
        phone=member.phone,
        source='community'
    )
    db.session.add(unsubscribe)
    db.session.commit()
    flash('Member added to unsubscribed list.', 'success')
    return redirect(url_for('main.community_list'))


# Events Management
@bp.route('/events')
@login_required
def events_list():
    search = request.args.get('search', '').strip()
    query = Event.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.filter(
            db.or_(
                Event.title.ilike(pattern, escape='\\'),
                db.cast(Event.date, db.String).ilike(pattern, escape='\\')
            )
        )

    events = query.order_by(Event.date.desc()).all()
    return render_template('events/list.html', events=events, search=search)


@bp.route('/events/add', methods=['GET', 'POST'])
@login_required
def event_add():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        date_str = request.form.get('date', '').strip()
        
        if not title:
            flash('Event title is required.', 'error')
            return render_template('events/form.html', event=None)
        
        from datetime import datetime
        event_date = None
        if date_str:
            try:
                event_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid date format.', 'error')
                return render_template('events/form.html', event=None)
        
        event = Event(title=title, date=event_date)
        db.session.add(event)
        db.session.commit()
        
        flash('Event created successfully.', 'success')
        return redirect(url_for('main.event_detail', event_id=event.id))
    
    return render_template('events/form.html', event=None)


@bp.route('/events/<int:event_id>')
@login_required
def event_detail(event_id):
    event = _tenant_get_or_404(Event, event_id)
    registrations = EventRegistration.query.filter_by(event_id=event_id).order_by(EventRegistration.name, EventRegistration.phone).all()
    unsubscribed_phones = get_unsubscribed_phone_set([reg.phone for reg in registrations])
    return render_template('events/detail.html', event=event, registrations=registrations, unsubscribed_phones=unsubscribed_phones)


@bp.route('/events/<int:event_id>/edit', methods=['GET', 'POST'])
@login_required
def event_edit(event_id):
    event = _tenant_get_or_404(Event, event_id)
    
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        date_str = request.form.get('date', '').strip()
        
        if not title:
            flash('Event title is required.', 'error')
            return render_template('events/form.html', event=event)
        
        from datetime import datetime
        event_date = None
        if date_str:
            try:
                event_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid date format.', 'error')
                return render_template('events/form.html', event=event)
        
        event.title = title
        event.date = event_date
        db.session.commit()
        
        flash('Event updated successfully.', 'success')
        return redirect(url_for('main.event_detail', event_id=event.id))
    
    return render_template('events/form.html', event=event)


@bp.route('/events/<int:event_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def event_delete(event_id):
    event = _tenant_get_or_404(Event, event_id)
    db.session.delete(event)
    db.session.commit()
    flash('Event deleted.', 'success')
    return redirect(url_for('main.events_list'))


@bp.route('/events/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin')
def events_bulk_delete():
    raw_ids = request.form.getlist('event_ids')
    event_ids = []
    for raw in raw_ids:
        try:
            event_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    event_ids = sorted(set(event_ids))
    if not event_ids:
        flash('No events selected.', 'warning')
        return redirect(url_for('main.events_list'))

    events = Event.query.filter(Event.id.in_(event_ids)).all()
    for event in events:
        db.session.delete(event)

    db.session.commit()
    flash(f'Deleted {len(events)} event(s).', 'success')
    return redirect(url_for('main.events_list'))


@bp.route('/events/<int:event_id>/register', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def event_register(event_id):
    event = _tenant_get_or_404(Event, event_id)
    name = request.form.get('name', '').strip() or None
    phone = request.form.get('phone', '').strip()
    
    if not phone:
        flash('Phone number is required.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))
    
    phone = normalize_phone(phone)
    if not validate_phone(phone):
        flash('Invalid phone number format.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))

    if UnsubscribedContact.query.filter_by(phone=phone).first():
        flash('This number is currently unsubscribed and will not receive messages.', 'warning')
    
    # Check if already registered for this event
    existing = EventRegistration.query.filter_by(event_id=event_id, phone=phone).first()
    if existing:
        flash('This phone number is already registered for this event.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))
    
    registration = EventRegistration(event_id=event_id, name=name, phone=phone)
    db.session.add(registration)
    db.session.commit()
    
    flash('Registration added.', 'success')
    return redirect(url_for('main.event_detail', event_id=event_id))


@bp.route('/events/<int:event_id>/unregister/<int:registration_id>', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def event_unregister(event_id, registration_id):
    registration = EventRegistration.query.filter_by(id=registration_id, event_id=event_id).first()
    if not registration:
        flash('Registration not found for this event.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))
    db.session.delete(registration)
    db.session.commit()
    flash('Registration removed.', 'success')
    return redirect(url_for('main.event_detail', event_id=event_id))


@bp.route('/events/<int:event_id>/registrations/<int:registration_id>/unsubscribe', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def event_registration_unsubscribe(event_id, registration_id):
    registration = EventRegistration.query.filter_by(id=registration_id, event_id=event_id).first()
    if not registration:
        flash('Registration not found for this event.', 'error')
        return redirect(url_for('main.event_detail', event_id=event_id))

    existing = UnsubscribedContact.query.filter_by(phone=registration.phone).first()
    if existing:
        flash('That number is already unsubscribed.', 'warning')
        return redirect(url_for('main.event_detail', event_id=event_id))

    unsubscribe = UnsubscribedContact(
        name=registration.name,
        phone=registration.phone,
        source=f'event:{event_id}'
    )
    db.session.add(unsubscribe)
    db.session.commit()
    flash('Registration added to unsubscribed list.', 'success')
    return redirect(url_for('main.event_detail', event_id=event_id))


@bp.route('/events/<int:event_id>/import', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def event_import_registrations(event_id):
    event = _tenant_get_or_404(Event, event_id)

    file = None
    try:
        file, content, error_response = _read_uploaded_csv_text(
            redirect_endpoint='main.event_detail',
            redirect_values={'event_id': event_id},
        )
        if error_response is not None:
            return error_response
        parsed = parse_recipients_csv(content)

        if not parsed:
            flash('No valid entries found in CSV.', 'error')
            return redirect(url_for('main.event_detail', event_id=event_id))

        parsed, already_registered = _dedupe_recipients_by_phone(parsed)
        added = 0

        for rec in parsed:
            phone = rec['phone']

            # Check if already registered for this event
            existing = EventRegistration.query.filter_by(event_id=event_id, phone=phone).first()
            if existing:
                already_registered += 1
                continue

            registration = EventRegistration(event_id=event_id, name=rec['name'], phone=phone)
            db.session.add(registration)
            added += 1

        db.session.commit()

        msg = f'Added {added} registrations.'
        if already_registered:
            msg += f' {already_registered} already registered.'

        flash(msg, 'success' if added > 0 else 'warning')

    except Exception:
        current_app.logger.exception(
            'Event CSV import failed (event_id=%s, filename=%r, user_id=%s).',
            event_id,
            file.filename if file is not None else None,
            current_user.id if current_user.is_authenticated else None,
        )
        db.session.rollback()
        flash(CSV_IMPORT_ERROR_FLASH, 'error')

    return redirect(url_for('main.event_detail', event_id=event_id))


@bp.route('/events/<int:event_id>/export')
@login_required
def event_export_registrations(event_id):
    event = _tenant_get_or_404(Event, event_id)
    registrations = EventRegistration.query.filter_by(event_id=event_id).order_by(EventRegistration.name, EventRegistration.phone).all()

    def rows():
        yield ['name', 'phone', 'created_at']
        for reg in registrations:
            yield [
                reg.name or '',
                reg.phone,
                reg.created_at.isoformat() if reg.created_at else '',
            ]

    return _csv_download_response(f'event_{event.id}_registrations.csv', rows())


# Message Logs
@bp.route('/logs')
@login_required
def logs_list():
    search = request.args.get('search', '').strip()
    query = MessageLog.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.outerjoin(Event).filter(
            db.or_(
                MessageLog.message_body.ilike(pattern, escape='\\'),
                MessageLog.target.ilike(pattern, escape='\\'),
                Event.title.ilike(pattern, escape='\\')
            )
        )

    try:
        logs = query.order_by(MessageLog.created_at.desc()).limit(100).all()
    except OperationalError as exc:
        current_app.logger.warning(
            'MessageLog list query failed due to schema mismatch: %s',
            exc,
        )
        flash('Logs are temporarily unavailable due to a schema mismatch.', 'error')
        logs = []

    processing_logs = [
        {
            'id': log.id,
            'status': log.status or 'sent',
            'success_count': log.success_count or 0,
            'failure_count': log.failure_count or 0,
        }
        for log in logs
        if log.status == 'processing'
    ]

    return render_template(
        'logs/list.html',
        logs=logs,
        search=search,
        processing_logs=processing_logs,
    )


@bp.route('/logs/<int:log_id>')
@login_required
def log_detail(log_id):
    try:
        log = _tenant_get_or_404(MessageLog, log_id)
    except OperationalError as exc:
        current_app.logger.warning(
            'MessageLog detail query failed due to schema mismatch: %s',
            exc,
        )
        flash('Logs are temporarily unavailable due to a schema mismatch.', 'error')
        return redirect(url_for('main.logs_list'))

    details = _parse_message_log_details(log.details)
    if log.details and not details:
        current_app.logger.warning(
            'MessageLog details payload unusable for log_id=%s.',
            log_id,
        )
    phones = set()
    for detail in details:
        raw_phone = detail.get('phone') or detail.get('to') or detail.get('recipient')
        normalized = normalize_phone(raw_phone) if raw_phone else ''
        detail['normalized_phone'] = normalized
        if normalized:
            phones.add(normalized)

    suppression_status = {}
    if phones:
        unsubscribed_phones = {
            entry.phone for entry in UnsubscribedContact.query.filter(UnsubscribedContact.phone.in_(phones))
        }
        suppressed_phones = {
            entry.phone for entry in SuppressedContact.query.filter(SuppressedContact.phone.in_(phones))
        }
        for phone in phones:
            if phone in unsubscribed_phones:
                suppression_status[phone] = 'unsubscribed'
            elif phone in suppressed_phones:
                suppression_status[phone] = 'suppressed'

    return render_template(
        'logs/detail.html',
        log=log,
        details=details,
        suppression_status=suppression_status,
    )


@bp.route('/logs/status')
@login_required
def logs_status():
    """API endpoint for polling message log status changes."""
    ids_str = request.args.get('ids', '').strip()
    if not ids_str:
        return jsonify({'logs': []})
    try:
        ids = [int(i) for i in ids_str.split(',') if i.strip()]
    except ValueError:
        return jsonify({'logs': []})
    if not ids:
        return jsonify({'logs': []})

    ids = ids[:100]
    try:
        logs = MessageLog.query.filter(MessageLog.id.in_(ids)).all()
    except OperationalError as exc:
        current_app.logger.warning(
            'MessageLog status query failed due to schema mismatch: %s',
            exc,
        )
        return jsonify({'logs': []})

    payload = []
    for log in logs:
        payload.append({
            'id': log.id,
            'status': log.status or 'sent',
            'success_count': log.success_count or 0,
            'failure_count': log.failure_count or 0,
        })

    return jsonify({'logs': payload})


@bp.route('/security/events')
@login_required
@require_roles('admin')
def security_events():
    username = request.args.get('username', '').strip()
    event_type = request.args.get('event_type', '').strip()
    outcome = request.args.get('outcome', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    query = AuthEvent.query
    if username:
        pattern = f"%{escape_like(username)}%"
        query = query.filter(AuthEvent.username.ilike(pattern, escape='\\'))
    if event_type:
        query = query.filter(AuthEvent.event_type == event_type)
    if outcome:
        query = query.filter(AuthEvent.outcome == outcome)

    if date_from:
        try:
            start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            query = query.filter(AuthEvent.created_at >= start)
        except ValueError:
            flash('Invalid from date filter; expected YYYY-MM-DD.', 'error')
    if date_to:
        try:
            end = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc) + timedelta(days=1)
            query = query.filter(AuthEvent.created_at < end)
        except ValueError:
            flash('Invalid to date filter; expected YYYY-MM-DD.', 'error')

    events = query.order_by(AuthEvent.created_at.desc()).limit(500).all()
    event_type_options = [
        row[0]
        for row in db.session.query(AuthEvent.event_type)
        .group_by(AuthEvent.event_type)
        .order_by(AuthEvent.event_type.asc())
        .all()
    ]
    outcome_options = [
        row[0]
        for row in db.session.query(AuthEvent.outcome)
        .group_by(AuthEvent.outcome)
        .order_by(AuthEvent.outcome.asc())
        .all()
    ]
    return render_template(
        'security/events.html',
        events=events,
        filters={
            'username': username,
            'event_type': event_type,
            'outcome': outcome,
            'date_from': date_from,
            'date_to': date_to,
        },
        event_type_options=event_type_options,
        outcome_options=outcome_options,
    )


# Scheduled Messages
def _apply_scheduled_search_filter(query, search: str):
    if not search:
        return query

    escaped = escape_like(search)
    pattern = f'%{escaped}%'
    return query.outerjoin(Event).filter(
        db.or_(
            ScheduledMessage.message_body.ilike(pattern, escape='\\'),
            ScheduledMessage.target.ilike(pattern, escape='\\'),
            Event.title.ilike(pattern, escape='\\')
        )
    )


@bp.route('/scheduled')
@login_required
def scheduled_list():
    search = request.args.get('search', '').strip()
    now = datetime.utcnow()
    query = _apply_scheduled_search_filter(ScheduledMessage.query, search)

    pending = query.filter(ScheduledMessage.status == 'pending').order_by(ScheduledMessage.scheduled_at).all()
    past = query.filter(ScheduledMessage.status != 'pending').order_by(ScheduledMessage.scheduled_at.desc()).limit(50).all()
    pending_ids = [m.id for m in pending]

    return render_template(
        'scheduled/list.html',
        pending=pending,
        past=past,
        now=now,
        pending_ids=pending_ids,
        search=search
    )


@bp.route('/scheduled/<int:scheduled_id>/cancel', methods=['POST'])
@login_required
@require_roles('admin')
def scheduled_cancel(scheduled_id):
    scheduled = _tenant_get_or_404(ScheduledMessage, scheduled_id)
    
    if scheduled.status not in {'pending', 'processing'}:
        flash('Only pending or processing messages can be cancelled.', 'error')
        return redirect(url_for('main.scheduled_list'))
    
    scheduled.status = 'cancelled'
    db.session.commit()
    flash('Scheduled message cancelled.', 'success')
    return redirect(url_for('main.scheduled_list'))


@bp.route('/scheduled/<int:scheduled_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def scheduled_delete(scheduled_id):
    scheduled = _tenant_get_or_404(ScheduledMessage, scheduled_id)
    db.session.delete(scheduled)
    db.session.commit()
    flash('Scheduled message deleted.', 'success')
    return redirect(url_for('main.scheduled_list'))


@bp.route('/scheduled/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin')
def scheduled_bulk_delete():
    ids_str = request.form.get('scheduled_ids', '')
    if not ids_str:
        flash('No messages selected.', 'error')
        return redirect(url_for('main.scheduled_list'))
    
    try:
        ids = [int(i) for i in ids_str.split(',') if i.strip()]
    except ValueError:
        flash('Invalid selection.', 'error')
        return redirect(url_for('main.scheduled_list'))
    
    deleted = _tenant_bulk_filter(
        ScheduledMessage.query.filter(ScheduledMessage.id.in_(ids)),
        ScheduledMessage,
    ).delete(synchronize_session=False)
    db.session.commit()
    flash(f'{deleted} scheduled message(s) deleted.', 'success')
    return redirect(url_for('main.scheduled_list'))


@bp.route('/scheduled/bulk-cancel', methods=['POST'])
@login_required
@require_roles('admin')
def scheduled_bulk_cancel():
    ids_str = request.form.get('scheduled_ids', '')
    if not ids_str:
        flash('No messages selected.', 'error')
        return redirect(url_for('main.scheduled_list'))

    try:
        ids = [int(i) for i in ids_str.split(',') if i.strip()]
    except ValueError:
        flash('Invalid selection.', 'error')
        return redirect(url_for('main.scheduled_list'))

    if not ids:
        flash('No messages selected.', 'error')
        return redirect(url_for('main.scheduled_list'))

    updated = _tenant_bulk_filter(
        ScheduledMessage.query.filter(
        ScheduledMessage.id.in_(ids),
        ScheduledMessage.status.in_(['pending', 'processing']),
    ),
        ScheduledMessage,
    ).update(
        {ScheduledMessage.status: 'cancelled'},
        synchronize_session=False,
    )
    db.session.commit()
    flash(f'Cancelled {updated} scheduled message(s).', 'success')
    return redirect(url_for('main.scheduled_list'))


@bp.route('/scheduled/status')
@login_required
def scheduled_status():
    """API endpoint for polling scheduled message status changes."""
    search = request.args.get('search', '').strip()
    pending = _apply_scheduled_search_filter(
        ScheduledMessage.query,
        search
    ).filter(ScheduledMessage.status == 'pending').all()
    pending_ids = [m.id for m in pending]
    pending_count = len(pending_ids)
    
    # Return current state for comparison
    return jsonify({
        'pending_count': pending_count,
        'pending_ids': pending_ids
    })


@bp.route('/logs/clear', methods=['POST'])
@login_required
@require_roles('admin')
def logs_clear():
    """Clear all message logs - requires admin password confirmation."""
    admin_password = request.form.get('admin_password', '')

    if not current_user.check_password(admin_password):
        flash('Invalid admin password.', 'error')
        return redirect(url_for('main.logs_list'))
    
    # Clear all logs
    deleted_count = _tenant_bulk_filter(MessageLog.query, MessageLog).delete()
    db.session.commit()
    
    flash(f'Successfully cleared {deleted_count} log(s).', 'success')
    return redirect(url_for('main.logs_list'))


# Unsubscribed Contacts
@bp.route('/unsubscribed')
@login_required
def unsubscribed_list():
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    sort_key, sort_dir = normalize_sort_params(
        request.args.get('sort'),
        request.args.get('dir'),
        allowed_keys={'name', 'phone', 'reason', 'category', 'source', 'created_at'},
        default_key='created_at',
        default_dir='desc',
    )
    per_page = 50

    unsubscribed_query = (
        UnsubscribedContact.query
        .outerjoin(
            CommunityMember,
            db.and_(
                CommunityMember.phone == UnsubscribedContact.phone,
                db.or_(
                    CommunityMember.organization_id == UnsubscribedContact.organization_id,
                    db.and_(
                        CommunityMember.organization_id.is_(None),
                        UnsubscribedContact.organization_id.is_(None),
                    ),
                ),
            ),
        )
        .outerjoin(
            InboxThread,
            db.and_(
                InboxThread.phone == UnsubscribedContact.phone,
                db.or_(
                    InboxThread.organization_id == UnsubscribedContact.organization_id,
                    db.and_(
                        InboxThread.organization_id.is_(None),
                        UnsubscribedContact.organization_id.is_(None),
                    ),
                ),
            ),
        )
    )
    suppressed_query = SuppressedContact.query
    search_filter_unsubscribed = ''
    search_filter_suppressed = ''
    tenant_filter_unsubscribed = ''
    tenant_filter_suppressed = ''
    sql_params = {}
    if saas_mode_enabled() and not current_user.is_platform_admin:
        org_id = _current_organization_id()
        sql_params['org_id'] = org_id
        unsubscribed_query = unsubscribed_query.filter(UnsubscribedContact.organization_id == org_id)
        suppressed_query = suppressed_query.filter(SuppressedContact.organization_id == org_id)
        tenant_filter_unsubscribed = "AND u.organization_id = :org_id"
        tenant_filter_suppressed = "AND s.organization_id = :org_id"

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        sql_params['pattern'] = pattern
        unsubscribed_query = unsubscribed_query.filter(
            db.or_(
                UnsubscribedContact.name.ilike(pattern, escape='\\'),
                CommunityMember.name.ilike(pattern, escape='\\'),
                InboxThread.contact_name.ilike(pattern, escape='\\'),
                UnsubscribedContact.phone.ilike(pattern, escape='\\'),
                UnsubscribedContact.reason.ilike(pattern, escape='\\'),
                UnsubscribedContact.source.ilike(pattern, escape='\\'),
            )
        )
        suppressed_query = suppressed_query.filter(
            db.or_(
                SuppressedContact.phone.ilike(pattern, escape='\\'),
                SuppressedContact.reason.ilike(pattern, escape='\\'),
                SuppressedContact.category.ilike(pattern, escape='\\'),
                SuppressedContact.source.ilike(pattern, escape='\\'),
            )
        )
        search_filter_unsubscribed = """
            AND (
                LOWER(u.name) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(cm.name) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(it.contact_name) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(u.phone) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(u.reason) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(u.source) LIKE LOWER(:pattern) ESCAPE '\\'
            )
        """
        search_filter_suppressed = """
            AND (
                LOWER(s.phone) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(s.reason) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(s.category) LIKE LOWER(:pattern) ESCAPE '\\'
                OR LOWER(s.source) LIKE LOWER(:pattern) ESCAPE '\\'
            )
        """

    unsubscribed_count = unsubscribed_query.count()
    suppressed_count = suppressed_query.count()
    total_count = unsubscribed_count + suppressed_count
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    offset = (page - 1) * per_page
    sql_params.update({'limit': per_page, 'offset': offset})
    phone_sort_expr = (
        "CAST(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), '(', ''), ')', ''), '-', ''), ' ', ''), '.', '') AS BIGINT)"
    )
    sort_config = {
        'name': {'expr': 'LOWER(name)', 'null_check': 'name'},
        'phone': {'expr': phone_sort_expr, 'null_check': 'phone'},
        'reason': {'expr': 'LOWER(reason)', 'null_check': 'reason'},
        'category': {'expr': 'LOWER(category)', 'null_check': 'category'},
        'source': {'expr': 'LOWER(source)', 'null_check': 'source'},
        'created_at': {'expr': 'created_at', 'null_check': 'created_at'},
    }
    sort_expr = sort_config[sort_key]['expr']
    null_check = sort_config[sort_key]['null_check']
    null_rank = 1 if sort_dir == 'asc' else 0
    not_null_rank = 0 if sort_dir == 'asc' else 1
    order_by = (
        f"CASE WHEN {null_check} IS NULL OR {null_check} = '' THEN {null_rank} ELSE {not_null_rank} END, "
        f"{sort_expr} {sort_dir}, "
        "created_at DESC, entry_type, id"
    )

    combined_sql = f"""
        SELECT
            id,
            name,
            phone,
            reason,
            category,
            source,
            created_at,
            entry_type
        FROM (
            SELECT
                u.id AS id,
                COALESCE(NULLIF(u.name, ''), NULLIF(cm.name, ''), NULLIF(it.contact_name, '')) AS name,
                u.phone AS phone,
                u.reason AS reason,
                'unsubscribed' AS category,
                u.source AS source,
                u.created_at AS created_at,
                'unsubscribed' AS entry_type
            FROM unsubscribed_contacts u
            LEFT JOIN community_members cm
                ON cm.phone = u.phone
               AND (
                   cm.organization_id = u.organization_id
                   OR (cm.organization_id IS NULL AND u.organization_id IS NULL)
               )
            LEFT JOIN inbox_threads it
                ON it.phone = u.phone
               AND (
                   it.organization_id = u.organization_id
                   OR (it.organization_id IS NULL AND u.organization_id IS NULL)
               )
            WHERE 1 = 1
            {tenant_filter_unsubscribed}
            {search_filter_unsubscribed}
            UNION ALL
            SELECT
                s.id AS id,
                NULL AS name,
                s.phone AS phone,
                s.reason AS reason,
                s.category AS category,
                s.source AS source,
                s.created_at AS created_at,
                'suppressed' AS entry_type
            FROM suppressed_contacts s
            WHERE 1 = 1
            {tenant_filter_suppressed}
            {search_filter_suppressed}
        ) combined
        ORDER BY {order_by}
        LIMIT :limit OFFSET :offset
    """
    combined_query = text(combined_sql).columns(
        id=Integer(),
        name=String(),
        phone=String(),
        reason=Text(),
        category=String(),
        source=String(),
        created_at=DateTime(),
        entry_type=String(),
    )
    combined = [
        dict(row)
        for row in db.session.execute(combined_query, sql_params).mappings().all()
    ]

    return render_template(
        'unsubscribed/list.html',
        entries=combined,
        search=search,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        sort_key=sort_key,
        sort_dir=sort_dir,
    )


@bp.route('/unsubscribed/backfill', methods=['POST'])
@login_required
@require_roles('admin')
def unsubscribed_backfill():
    try:
        queue = _get_queue_with_preflight()
        job = queue.enqueue('app.tasks.backfill_suppressions_job')
        message = f"Backfill queued (job {job.id}). Results will appear shortly."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'message': message, 'job_id': job.id})
        flash(message, 'success')
    except Exception:
        current_app.logger.exception('Failed to queue backfill job')
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': BACKFILL_QUEUE_UNAVAILABLE_FLASH}), 500
        flash(BACKFILL_QUEUE_UNAVAILABLE_FLASH, 'error')
    return redirect(url_for('main.unsubscribed_list'))


@bp.route('/unsubscribed/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def unsubscribed_add():
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or None
        phone = request.form.get('phone', '').strip()
        reason = request.form.get('reason', '').strip() or None
        source = request.form.get('source', '').strip() or 'manual'
        next_url = request.form.get('next')

        if not phone:
            flash('Phone number is required.', 'error')
            return render_template('unsubscribed/form.html', entry=None)

        phone = normalize_phone(phone)
        if not validate_phone(phone):
            flash('Invalid phone number format.', 'error')
            return render_template('unsubscribed/form.html', entry=None)

        existing = UnsubscribedContact.query.filter_by(phone=phone).first()
        if existing:
            flash('That phone number is already unsubscribed.', 'warning')
            if next_url and is_safe_url(next_url, request.host_url):
                return redirect(next_url)
            return redirect(url_for('main.unsubscribed_list'))

        entry = UnsubscribedContact(name=name, phone=phone, reason=reason, source=source)
        db.session.add(entry)
        db.session.commit()
        flash('Added to unsubscribed list.', 'success')

        if next_url and is_safe_url(next_url, request.host_url):
            return redirect(next_url)
        return redirect(url_for('main.unsubscribed_list'))

    return render_template('unsubscribed/form.html', entry=None)


@bp.route('/unsubscribed/import', methods=['GET', 'POST'])
@login_required
@require_roles('admin')
def unsubscribed_import():
    if request.method == 'POST':
        file = None
        try:
            file, content, error_response = _read_uploaded_csv_text(template_name='unsubscribed/import.html')
            if error_response is not None:
                return error_response
            parsed = parse_recipients_csv(content)

            if not parsed:
                flash('No valid entries found in CSV.', 'error')
                return render_template('unsubscribed/import.html')

            parsed, skipped = _dedupe_recipients_by_phone(parsed)
            added = 0

            for rec in parsed:
                phone = rec['phone']

                existing = UnsubscribedContact.query.filter_by(phone=phone).first()
                if existing:
                    skipped += 1
                    continue

                entry = UnsubscribedContact(
                    name=rec['name'],
                    phone=phone,
                    source='import'
                )
                db.session.add(entry)
                added += 1

            db.session.commit()
            flash(f'Imported {added} unsubscribed contact(s). {skipped} duplicates skipped.', 'success')
            return redirect(url_for('main.unsubscribed_list'))

        except Exception:
            current_app.logger.exception(
                'Unsubscribed CSV import failed (filename=%r, user_id=%s).',
                file.filename if file is not None else None,
                current_user.id if current_user.is_authenticated else None,
            )
            db.session.rollback()
            flash(CSV_IMPORT_ERROR_FLASH, 'error')

    return render_template('unsubscribed/import.html')


@bp.route('/unsubscribed/export')
@login_required
@require_roles('admin')
def unsubscribed_export():
    entries = UnsubscribedContact.query.order_by(UnsubscribedContact.created_at.desc()).all()

    def rows():
        yield ['name', 'phone', 'reason', 'source', 'created_at']
        for entry in entries:
            yield [
                entry.name or '',
                entry.phone,
                entry.reason or '',
                entry.source,
                entry.created_at.isoformat() if entry.created_at else '',
            ]

    return _csv_download_response('unsubscribed_contacts.csv', rows())


@bp.route('/unsubscribed/<int:entry_id>/delete', methods=['POST'])
@login_required
@require_roles('admin')
def unsubscribed_delete(entry_id):
    entry = UnsubscribedContact.query.filter_by(id=entry_id).first()
    if entry is None:
        flash('Entry already deleted or not found.', 'warning')
        return redirect(url_for('main.unsubscribed_list'))
    db.session.delete(entry)
    db.session.commit()
    flash('Removed from unsubscribed list.', 'success')
    return redirect(url_for('main.unsubscribed_list'))


@bp.route('/unsubscribed/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin')
def unsubscribed_bulk_delete():
    raw_unsub_ids = request.form.getlist('unsubscribed_ids')
    raw_supp_ids = request.form.getlist('suppressed_ids')

    unsub_ids = []
    for raw in raw_unsub_ids:
        try:
            unsub_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    supp_ids = []
    for raw in raw_supp_ids:
        try:
            supp_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    unsub_ids = sorted(set(unsub_ids))
    supp_ids = sorted(set(supp_ids))

    if not unsub_ids and not supp_ids:
        flash('No entries selected.', 'warning')
        return redirect(url_for('main.unsubscribed_list'))

    deleted_unsub = 0
    deleted_supp = 0

    if unsub_ids:
        deleted_unsub = _tenant_bulk_filter(
            UnsubscribedContact.query.filter(UnsubscribedContact.id.in_(unsub_ids)),
            UnsubscribedContact,
        ).delete(synchronize_session=False)

    if supp_ids:
        deleted_supp = _tenant_bulk_filter(
            SuppressedContact.query.filter(SuppressedContact.id.in_(supp_ids)),
            SuppressedContact,
        ).delete(synchronize_session=False)

    db.session.commit()
    total = deleted_unsub + deleted_supp
    flash(f'Deleted {total} entry/entries ({deleted_unsub} unsubscribed, {deleted_supp} suppressed).', 'success')
    return redirect(url_for('main.unsubscribed_list'))


@bp.route('/webhooks/twilio/inbound', methods=['POST'])
@csrf.exempt
def twilio_inbound_webhook():
    payload = request.form.to_dict(flat=True)
    messaging_profile = resolve_messaging_profile(payload) if saas_mode_enabled() else None
    if current_app.config.get('TWILIO_VALIDATE_INBOUND_SIGNATURE', True):
        signature = request.headers.get('X-Twilio-Signature')
        validation = validate_inbound_signature_detailed(
            request.url,
            payload,
            signature,
            messaging_profile=messaging_profile,
        )
        if not validation.is_valid:
            current_app.logger.warning(
                'Rejected inbound webhook due to Twilio signature validation failure. '
                'reason=%s remote_addr=%s message_sid=%s from=%s',
                validation.reason,
                request.remote_addr,
                payload.get('MessageSid'),
                payload.get('From'),
            )
            return 'Forbidden', 403

    try:
        result = process_inbound_sms(payload)
        current_app.logger.info(
            'Inbound webhook processed: status=%s phone=%s thread_id=%s',
            result.get('status'),
            result.get('phone'),
            result.get('thread_id'),
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to process inbound Twilio webhook payload')
        return 'Internal Server Error', 500

    response = make_response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', 200)
    response.headers['Content-Type'] = 'text/xml'
    return response


@bp.route('/webhooks/twilio/a2p-events', methods=['POST'])
@csrf.exempt
def twilio_a2p_event_stream_webhook():
    if not current_app.config.get('TWILIO_A2P_EVENT_STREAMS_ENABLED'):
        abort(404)

    expected_token = (current_app.config.get('TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN') or '').strip()
    authorization = request.headers.get('Authorization', '')
    bearer_valid = bool(expected_token) and authorization == f'Bearer {expected_token}'

    organization_id = request.args.get('organization_id', type=int)
    messaging_profile = None
    if organization_id:
        organization = db.session.get(Organization, organization_id)
        messaging_profile = organization.messaging_profile if organization is not None else None

    signature = request.headers.get('X-Twilio-Signature')
    validation = None
    if messaging_profile is not None and signature:
        validation = validate_inbound_signature_detailed(
            request.url,
            request.get_data(cache=True, as_text=True),
            signature,
            messaging_profile=messaging_profile,
        )
    elif signature:
        validation = validate_inbound_signature_detailed(
            request.url,
            request.get_data(cache=True, as_text=True),
            signature,
        )

    if validation is not None and validation.is_valid:
        pass
    elif not bearer_valid:
        current_app.logger.warning(
            'Rejected Twilio A2P Event Streams webhook due to auth validation failure. '
            'reason=%s remote_addr=%s organization_id=%s',
            validation.reason if validation is not None else 'missing_signature',
            request.remote_addr,
            organization_id,
        )
        return 'Forbidden', 403

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({'error': 'Expected JSON payload.'}), 400

    try:
        summary = ingest_a2p_event_stream_payload(payload)
        db.session.commit()
    except ProviderProvisioningError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to process Twilio A2P Event Streams payload')
        return jsonify({'error': 'Internal Server Error'}), 500

    return jsonify(summary), 200


@bp.route('/inbox')
@login_required
def inbox_list():
    search = request.args.get('search', '').strip()
    selected_thread_id = request.args.get('thread', type=int)
    mobile_view = request.args.get('view', '').strip().lower()
    show_thread_list_only = mobile_view == 'threads'
    query = InboxThread.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = (
            query.outerjoin(CommunityMember, CommunityMember.phone == InboxThread.phone)
            .filter(
                db.or_(
                    InboxThread.phone.ilike(pattern, escape='\\'),
                    InboxThread.contact_name.ilike(pattern, escape='\\'),
                    CommunityMember.name.ilike(pattern, escape='\\'),
                )
            )
            .distinct()
        )

    threads = query.order_by(InboxThread.last_message_at.desc()).limit(200).all()
    selected_thread = None
    if selected_thread_id:
        selected_thread = next((thread for thread in threads if thread.id == selected_thread_id), None)
        if selected_thread is None:
            selected_thread = InboxThread.query.filter_by(id=selected_thread_id).first()
    elif threads and not show_thread_list_only:
        selected_thread = threads[0]

    messages = []
    active_sessions = []
    active_trigger_keywords: set[str] = set()
    selected_thread_is_unsubscribed = False
    if selected_thread:
        if selected_thread.unread_count:
            mark_thread_read(selected_thread.id)
            selected_thread.unread_count = 0

        messages = InboxMessage.query.filter_by(thread_id=selected_thread.id).order_by(InboxMessage.created_at.asc()).all()
        active_trigger_keywords = _active_trigger_keywords_set()
        active_sessions = SurveySession.query.filter_by(
            thread_id=selected_thread.id,
            status='active',
        ).order_by(SurveySession.started_at.desc()).all()
        selected_thread_is_unsubscribed = (
            UnsubscribedContact.query.filter_by(phone=selected_thread.phone).first() is not None
        )

    thread_display_names = _build_thread_display_names(threads, selected_thread=selected_thread)
    latest_message_id = db.session.query(func.max(InboxMessage.id)).scalar() or 0

    return render_template(
        'inbox/list.html',
        threads=threads,
        selected_thread=selected_thread,
        messages=messages,
        active_sessions=active_sessions,
        active_trigger_keywords=active_trigger_keywords,
        selected_thread_is_unsubscribed=selected_thread_is_unsubscribed,
        thread_display_names=thread_display_names,
        inbox_status_latest_message_id=latest_message_id,
        show_thread_list_only=show_thread_list_only,
        search=search,
    )


@bp.route('/inbox/status')
@login_required
def inbox_status():
    latest_message_id = db.session.query(func.max(InboxMessage.id)).scalar() or 0
    return jsonify({'latest_message_id': int(latest_message_id)})


@bp.route('/inbox/<int:thread_id>/reply', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def inbox_reply(thread_id):
    body = request.form.get('body', '').strip()
    if not body:
        flash('Reply message cannot be empty.', 'error')
        return redirect(url_for('main.inbox_list', thread=thread_id))

    try:
        result = send_thread_reply(thread_id, body, actor=current_user.username)
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed sending manual inbox reply.')
        flash('Failed to send reply. Check server logs for details.', 'error')
        return redirect(url_for('main.inbox_list', thread=thread_id))

    if result.get('success'):
        flash('Reply sent.', 'success')
    elif result.get('status') == 'blocked_opt_out':
        flash('Reply blocked: this contact is unsubscribed. Ask them to text START to resubscribe.', 'warning')
    else:
        error = result.get('error') or 'Unknown error'
        flash(f'Reply could not be delivered: {error}', 'error')

    return redirect(url_for('main.inbox_list', thread=thread_id))


@bp.route('/inbox/threads/<int:thread_id>/update', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def inbox_thread_update(thread_id):
    thread = _tenant_get_or_404(InboxThread, thread_id)
    contact_name = request.form.get('contact_name')
    updated = update_thread_contact_name(thread.id, contact_name)
    if updated is None:
        flash('Thread not found.', 'error')
        return _redirect_to_inbox()

    flash('Thread contact updated.', 'success')
    return _redirect_to_inbox(thread_id=thread.id)


@bp.route('/inbox/threads/<int:thread_id>/delete', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def inbox_thread_delete(thread_id):
    thread = _tenant_get_or_404(InboxThread, thread_id)
    result = delete_thread_with_dependencies(thread.id)
    if result is None:
        flash('Thread not found.', 'error')
        return _redirect_to_inbox()

    flash(
        (
            'Thread deleted '
            f"({result['messages']} message(s), "
            f"{result['sessions']} survey session(s), "
            f"{result['responses']} survey response(s))."
        ),
        'success',
    )
    return _redirect_to_inbox()


@bp.route('/inbox/messages/bulk-delete', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def inbox_messages_bulk_delete():
    thread_id = request.form.get('thread_id', type=int)
    if not thread_id:
        flash('Thread is required.', 'error')
        return _redirect_to_inbox()

    _tenant_get_or_404(InboxThread, thread_id)
    message_ids = _parse_int_ids(request.form.getlist('message_ids'))
    if not message_ids:
        flash('No messages selected.', 'warning')
        return _redirect_to_inbox(thread_id=thread_id)

    deleted = delete_messages_in_thread(thread_id, message_ids)
    flash(f'Deleted {deleted} message(s).', 'success')
    return _redirect_to_inbox(thread_id=thread_id)


@bp.route('/inbox/keywords')
@login_required
@require_roles('admin', 'social_manager')
def keyword_rules_list():
    search = request.args.get('search', '').strip()
    query = KeywordAutomationRule.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.filter(
            db.or_(
                KeywordAutomationRule.keyword.ilike(pattern, escape='\\'),
                KeywordAutomationRule.response_body.ilike(pattern, escape='\\'),
            )
        )

    rules = query.order_by(KeywordAutomationRule.keyword.asc()).all()
    return render_template('inbox/keywords_list.html', rules=rules, search=search)


@bp.route('/inbox/keywords/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def keyword_rule_add():
    form_data = {'keyword': '', 'response_body': '', 'is_active': True}
    if request.method == 'POST':
        keyword = request.form.get('keyword', '')
        response_body = request.form.get('response_body', '').strip()
        is_active = request.form.get('is_active') == 'on'
        normalized_keyword = normalize_keyword(keyword)
        form_data = {
            'keyword': keyword,
            'response_body': response_body,
            'is_active': is_active,
        }

        if not normalized_keyword:
            flash('Keyword is required.', 'error')
            return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)
        if not response_body:
            flash('Auto-reply message is required.', 'error')
            return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)
        if _keyword_conflicts_with_rule(normalized_keyword):
            flash('That keyword already exists.', 'error')
            return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)
        if _keyword_conflicts_with_survey(normalized_keyword):
            flash('That keyword is already used as a survey trigger.', 'error')
            return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)

        rule = KeywordAutomationRule(
            keyword=normalized_keyword,
            response_body=response_body,
            is_active=is_active,
        )
        db.session.add(rule)
        db.session.commit()
        flash('Keyword automation created.', 'success')
        return redirect(url_for('main.keyword_rules_list'))

    return render_template('inbox/keyword_form.html', rule=None, form_data=form_data)


@bp.route('/inbox/keywords/<int:rule_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def keyword_rule_edit(rule_id):
    rule = _tenant_get_or_404(KeywordAutomationRule, rule_id)

    if request.method == 'POST':
        keyword = request.form.get('keyword', '')
        response_body = request.form.get('response_body', '').strip()
        is_active = request.form.get('is_active') == 'on'
        normalized_keyword = normalize_keyword(keyword)

        if not normalized_keyword:
            flash('Keyword is required.', 'error')
            return render_template('inbox/keyword_form.html', rule=rule, form_data=None)
        if not response_body:
            flash('Auto-reply message is required.', 'error')
            return render_template('inbox/keyword_form.html', rule=rule, form_data=None)

        if _keyword_conflicts_with_rule(normalized_keyword, exclude_rule_id=rule.id):
            flash('That keyword already exists.', 'error')
            return render_template('inbox/keyword_form.html', rule=rule, form_data=None)
        if _keyword_conflicts_with_survey(normalized_keyword):
            flash('That keyword is already used as a survey trigger.', 'error')
            return render_template('inbox/keyword_form.html', rule=rule, form_data=None)

        rule.keyword = normalized_keyword
        rule.response_body = response_body
        rule.is_active = is_active
        db.session.commit()
        flash('Keyword automation updated.', 'success')
        return redirect(url_for('main.keyword_rules_list'))

    return render_template('inbox/keyword_form.html', rule=rule, form_data=None)


@bp.route('/inbox/keywords/<int:rule_id>/delete', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def keyword_rule_delete(rule_id):
    rule = _tenant_get_or_404(KeywordAutomationRule, rule_id)
    db.session.delete(rule)
    db.session.commit()
    flash('Keyword automation deleted.', 'success')
    return redirect(url_for('main.keyword_rules_list'))


@bp.route('/inbox/surveys')
@login_required
@require_roles('admin', 'social_manager')
def survey_flows_list():
    search = request.args.get('search', '').strip()
    query = SurveyFlow.query

    if search:
        escaped = escape_like(search)
        pattern = f'%{escaped}%'
        query = query.filter(
            db.or_(
                SurveyFlow.name.ilike(pattern, escape='\\'),
                SurveyFlow.trigger_keyword.ilike(pattern, escape='\\'),
                SurveyFlow.intro_message.ilike(pattern, escape='\\'),
                SurveyFlow.completion_message.ilike(pattern, escape='\\'),
            )
        )

    surveys = query.order_by(SurveyFlow.created_at.desc()).all()
    return render_template('inbox/surveys_list.html', surveys=surveys, search=search)


@bp.route('/inbox/surveys/<int:survey_id>/submissions')
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_submissions(survey_id):
    survey = _tenant_get_or_404(SurveyFlow, survey_id)
    search = request.args.get('search', '').strip()
    page = request.args.get('page', type=int) or 1
    preview_question_indexes = _parse_survey_preview_indexes(
        request.args.getlist('preview_q'),
        question_count=len(survey.questions),
    )
    payload = _build_survey_submission_data(
        survey,
        search=search,
        page=page,
        preview_question_indexes=preview_question_indexes,
    )
    return render_template(
        'inbox/survey_submissions.html',
        survey=survey,
        search=search,
        questions=payload['questions'],
        preview_question_indexes=payload['preview_question_indexes'],
        latest_rows=payload['latest_rows'],
        history_by_phone=payload['history_by_phone'],
        unique_attendees=payload['unique_attendees'],
        total_completed=payload['total_completed'],
        repeat_submitters=payload['repeat_submitters'],
        page=payload['page'],
        total_pages=payload['total_pages'],
        per_page=payload['per_page'],
    )


@bp.route('/inbox/surveys/<int:survey_id>/submissions/export')
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_submissions_export(survey_id):
    survey = _tenant_get_or_404(SurveyFlow, survey_id)
    rows = _iter_survey_submission_export_rows(survey)
    response = Response(stream_with_context(_stream_csv_rows(rows)), mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename="survey_{survey.id}_submissions.csv"'
    return response


@bp.route('/inbox/surveys/add', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_add():
    form_data = {
        'name': '',
        'trigger_keyword': '',
        'intro_message': '',
        'completion_message': '',
        'questions': '',
        'is_active': True,
        'event_link_mode': 'none',
        'existing_event_id': '',
        'new_event_title': '',
        'new_event_date': '',
    }
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        trigger_keyword = normalize_keyword(request.form.get('trigger_keyword', ''))
        intro_message = request.form.get('intro_message', '').strip() or None
        completion_message = request.form.get('completion_message', '').strip() or None
        questions_raw = request.form.get('questions', '')
        questions = parse_survey_questions(questions_raw)
        is_active = request.form.get('is_active') == 'on'
        event_link_mode = (request.form.get('event_link_mode') or 'none').strip().lower()
        if event_link_mode not in {'none', 'existing', 'new'}:
            event_link_mode = 'none'
        existing_event_id = request.form.get('existing_event_id', type=int)
        new_event_title = request.form.get('new_event_title', '').strip()
        new_event_date_raw = request.form.get('new_event_date', '').strip()

        form_data = {
            'name': name,
            'trigger_keyword': trigger_keyword,
            'intro_message': intro_message or '',
            'completion_message': completion_message or '',
            'questions': questions_raw,
            'is_active': is_active,
            'event_link_mode': event_link_mode,
            'existing_event_id': str(existing_event_id or ''),
            'new_event_title': new_event_title,
            'new_event_date': new_event_date_raw,
        }

        linked_event_id = None
        new_event_date = None
        if event_link_mode == 'existing':
            if not existing_event_id:
                flash('Select an existing event to link this survey.', 'error')
                return _render_survey_form(survey=None, form_data=form_data)
            linked_event = Event.query.filter_by(id=existing_event_id).first()
            if linked_event is None:
                flash('Selected event was not found.', 'error')
                return _render_survey_form(survey=None, form_data=form_data)
            linked_event_id = linked_event.id
        elif event_link_mode == 'new':
            if not new_event_title:
                flash('Event title is required when creating a new linked event.', 'error')
                return _render_survey_form(survey=None, form_data=form_data)
            if new_event_date_raw:
                try:
                    new_event_date = datetime.strptime(new_event_date_raw, '%Y-%m-%d').date()
                except ValueError:
                    flash('Invalid linked event date format.', 'error')
                    return _render_survey_form(survey=None, form_data=form_data)

        if not name:
            flash('Survey name is required.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if not trigger_keyword:
            flash('Survey trigger keyword is required.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if not questions:
            flash('At least one survey question is required.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if SurveyFlow.query.filter_by(name=name).first():
            flash('A survey with this name already exists.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if _keyword_conflicts_with_survey(trigger_keyword):
            flash('That survey trigger keyword already exists.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)
        if _keyword_conflicts_with_rule(trigger_keyword):
            flash('That survey trigger keyword is already used by a keyword automation.', 'error')
            return _render_survey_form(survey=None, form_data=form_data)

        if event_link_mode == 'new':
            linked_event = Event(title=new_event_title, date=new_event_date)
            db.session.add(linked_event)
            db.session.flush()
            linked_event_id = linked_event.id

        survey = SurveyFlow(
            name=name,
            trigger_keyword=trigger_keyword,
            intro_message=intro_message,
            completion_message=completion_message,
            linked_event_id=linked_event_id,
            is_active=is_active,
        )
        survey.set_questions(questions)
        db.session.add(survey)
        db.session.commit()
        flash('Survey flow created.', 'success')
        return redirect(url_for('main.survey_flows_list'))

    return _render_survey_form(survey=None, form_data=form_data)


@bp.route('/inbox/surveys/<int:survey_id>/edit', methods=['GET', 'POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_edit(survey_id):
    survey = _tenant_get_or_404(SurveyFlow, survey_id)
    form_data = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        trigger_keyword = normalize_keyword(request.form.get('trigger_keyword', ''))
        intro_message = request.form.get('intro_message', '').strip() or None
        completion_message = request.form.get('completion_message', '').strip() or None
        questions_raw = request.form.get('questions', '')
        questions = parse_survey_questions(questions_raw)
        is_active = request.form.get('is_active') == 'on'
        event_link_mode = (request.form.get('event_link_mode') or 'none').strip().lower()
        if event_link_mode not in {'none', 'existing', 'new'}:
            event_link_mode = 'none'
        existing_event_id = request.form.get('existing_event_id', type=int)
        new_event_title = request.form.get('new_event_title', '').strip()
        new_event_date_raw = request.form.get('new_event_date', '').strip()

        form_data = {
            'name': name,
            'trigger_keyword': trigger_keyword,
            'intro_message': intro_message or '',
            'completion_message': completion_message or '',
            'questions': questions_raw,
            'is_active': is_active,
            'event_link_mode': event_link_mode,
            'existing_event_id': str(existing_event_id or ''),
            'new_event_title': new_event_title,
            'new_event_date': new_event_date_raw,
        }

        linked_event_id = None
        new_event_date = None
        if event_link_mode == 'existing':
            if not existing_event_id:
                flash('Select an existing event to link this survey.', 'error')
                return _render_survey_form(survey=survey, form_data=form_data)
            linked_event = Event.query.filter_by(id=existing_event_id).first()
            if linked_event is None:
                flash('Selected event was not found.', 'error')
                return _render_survey_form(survey=survey, form_data=form_data)
            linked_event_id = linked_event.id
        elif event_link_mode == 'new':
            if not new_event_title:
                flash('Event title is required when creating a new linked event.', 'error')
                return _render_survey_form(survey=survey, form_data=form_data)
            if new_event_date_raw:
                try:
                    new_event_date = datetime.strptime(new_event_date_raw, '%Y-%m-%d').date()
                except ValueError:
                    flash('Invalid linked event date format.', 'error')
                    return _render_survey_form(survey=survey, form_data=form_data)

        if not name:
            flash('Survey name is required.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)
        if not trigger_keyword:
            flash('Survey trigger keyword is required.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)
        if not questions:
            flash('At least one survey question is required.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)

        name_conflict = SurveyFlow.query.filter(
            SurveyFlow.name == name,
            SurveyFlow.id != survey.id,
        ).first()
        if name_conflict:
            flash('A survey with this name already exists.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)

        if _keyword_conflicts_with_survey(trigger_keyword, exclude_survey_id=survey.id):
            flash('That survey trigger keyword already exists.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)
        if _keyword_conflicts_with_rule(trigger_keyword):
            flash('That survey trigger keyword is already used by a keyword automation.', 'error')
            return _render_survey_form(survey=survey, form_data=form_data)

        if event_link_mode == 'new':
            linked_event = Event(title=new_event_title, date=new_event_date)
            db.session.add(linked_event)
            db.session.flush()
            linked_event_id = linked_event.id

        survey.name = name
        survey.trigger_keyword = trigger_keyword
        survey.intro_message = intro_message
        survey.completion_message = completion_message
        survey.linked_event_id = linked_event_id
        survey.is_active = is_active
        survey.set_questions(questions)
        db.session.commit()
        flash('Survey flow updated.', 'success')
        return redirect(url_for('main.survey_flows_list'))

    return _render_survey_form(survey=survey, form_data=form_data)


@bp.route('/inbox/surveys/<int:survey_id>/delete', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_delete(survey_id):
    survey = _tenant_get_or_404(SurveyFlow, survey_id)
    if survey.linked_event_id:
        flash(
            'This survey is linked to an event. Edit it and choose "Do not link to an event" before deleting.',
            'error',
        )
        return redirect(url_for('main.survey_flows_list'))

    result = delete_survey_flow_with_dependencies(survey.id)
    if result is None:
        flash('Survey flow not found.', 'error')
    else:
        flash(
            (
                'Survey flow deleted '
                f"({result['sessions']} session(s), "
                f"{result['responses']} response(s))."
            ),
            'success',
        )
    return redirect(url_for('main.survey_flows_list'))


@bp.route('/inbox/surveys/<int:survey_id>/deactivate', methods=['POST'])
@login_required
@require_roles('admin', 'social_manager')
def survey_flow_deactivate(survey_id):
    survey = _tenant_get_or_404(SurveyFlow, survey_id)
    survey.is_active = False

    now = utc_now()
    active_sessions = SurveySession.query.filter_by(survey_id=survey.id, status='active').all()
    for session in active_sessions:
        session.status = 'cancelled'
        session.completed_at = now
        session.last_activity_at = now

    db.session.commit()
    flash('Survey flow deactivated.', 'success')
    return redirect(url_for('main.survey_flows_list'))
