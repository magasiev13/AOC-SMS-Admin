from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from flask import current_app, url_for

from app import db
from app.models import (
    AppUser,
    Organization,
    OrganizationMembership,
    OrganizationSubscription,
    OrganizationUsageBillingPeriod,
    StripeCheckoutSession,
    StripeWebhookEvent,
    utc_now,
)
from app.services.billing_plans import (
    activation_price_id,
    billing_plan_for_code,
    billing_plan_for_price_id,
    billing_plan_options_for_organization,
    recurring_price_id_for_subscription,
    subscription_activation_paid,
)
from app.services.twilio_service import previous_billing_period_window, reconcile_messaging_usage
from app.utils import as_utc_datetime


COMPLIMENTARY_SUBSCRIPTION_STATUS = "complimentary"
ACTIVE_SUBSCRIPTION_STATUSES = {"trialing", "active", COMPLIMENTARY_SUBSCRIPTION_STATUS}
RELEVANT_STRIPE_EVENT_TYPES = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
}
RECONCILEABLE_SUBSCRIPTION_STATUSES = {
    "trialing",
    "active",
    "past_due",
    "unpaid",
    "canceled",
    "incomplete",
}
WEBHOOK_PROCESSING_STALE_AFTER = timedelta(minutes=5)
USAGE_SETTLEMENT_LEASE = timedelta(minutes=15)
CHECKOUT_SESSION_CREATED_SKEW_ALLOWANCE = timedelta(minutes=5)
FAKE_CHECKOUT_SESSION_PREFIX = "cs_fake_org_"
REQUIRED_STRIPE_WEBHOOK_EVENTS = frozenset(RELEVANT_STRIPE_EVENT_TYPES)


class StaleStripeEventError(RuntimeError):
    """Raised when a verified Stripe event predates applied billing state."""


class StripePaymentProofError(RuntimeError):
    """Raised when Stripe objects do not prove the expected payment."""


class StripeComplimentaryConflictError(RuntimeError):
    """Raised when Stripe reports paid billing for a complimentary organization."""


@dataclass(frozen=True)
class StripePriceExpectation:
    config_key: str
    amount_cents: int
    recurring_interval: str | None


LIVE_PRICE_EXPECTATIONS = (
    StripePriceExpectation("STRIPE_ACTIVATION_PRICE_ID", 14900, None),
    StripePriceExpectation("STRIPE_MONTHLY_PRICE_ID", 5999, "month"),
    StripePriceExpectation("STRIPE_ANNUAL_PRICE_ID", 60000, "year"),
)


def subscription_status_allows_sending(status: str | None) -> bool:
    return (status or "").strip().lower() in ACTIVE_SUBSCRIPTION_STATUSES


def subscription_status_is_complimentary(status: str | None) -> bool:
    return (status or "").strip().lower() == COMPLIMENTARY_SUBSCRIPTION_STATUS


def subscription_requires_verified_setup_payment(
    subscription: OrganizationSubscription | None,
) -> bool:
    if subscription is None or subscription_status_is_complimentary(subscription.status):
        return False
    organization_offer_version = ""
    if subscription.organization is not None:
        organization_offer_version = str(
            subscription.organization.billing_offer_version or ""
        ).strip()
    offer_version = str(subscription.offer_version or organization_offer_version).strip()
    return bool(offer_version and offer_version.lower() != "legacy-v1")


def organization_can_send(organization: Organization | None) -> bool:
    if organization is None:
        return False
    subscription = organization.subscription
    if not subscription_status_allows_sending(subscription.status if subscription else None):
        return False
    if subscription_status_is_complimentary(subscription.status if subscription else None):
        return True
    if subscription_requires_verified_setup_payment(subscription):
        return subscription_activation_paid(subscription)
    return True


def organization_is_active(organization: Organization | None) -> bool:
    if organization is None:
        return False
    return (organization.status or "").strip().lower() == "active"


def organization_has_active_messaging(organization: Organization | None) -> bool:
    if organization is None:
        return False
    profile = organization.messaging_profile
    return bool(profile is not None and profile.can_send)


def organization_can_transmit_messages(organization: Organization | None) -> bool:
    return (
        organization_is_active(organization)
        and organization_can_send(organization)
        and organization_has_active_messaging(organization)
    )


def organization_transmit_block_reason(organization: Organization | None) -> str | None:
    if organization is None:
        return "Organization context is missing for message sending."
    if not organization_is_active(organization):
        return "Organization is not active for message sending."
    if not organization_can_send(organization):
        return "Organization billing is not active for message sending."
    if organization_has_active_messaging(organization):
        return None
    return "Messaging provider is not active for this organization."


def ensure_subscription_record(organization: Organization) -> OrganizationSubscription:
    subscription = organization.subscription
    if subscription is not None:
        return subscription

    subscription = OrganizationSubscription(
        organization=organization,
        stripe_price_id=current_app.config.get("STRIPE_MONTHLY_PRICE_ID") or current_app.config.get("STRIPE_PRICE_ID"),
        status="incomplete",
    )
    db.session.add(subscription)
    db.session.flush()
    return subscription


def mark_subscription_complimentary(organization: Organization) -> OrganizationSubscription:
    subscription = ensure_subscription_record(organization)
    chargeable_statuses = {"trialing", "active", "past_due", "unpaid"}
    normalized_status = str(subscription.status or "").strip().lower()
    if (
        subscription.stripe_customer_id
        or subscription.stripe_subscription_id
        or normalized_status in chargeable_statuses
    ):
        raise RuntimeError(
            "Cancel and reconcile the Stripe subscription before granting complimentary billing."
        )
    subscription.status = COMPLIMENTARY_SUBSCRIPTION_STATUS
    subscription.current_period_end = None
    subscription.cancel_at_period_end = False
    if not subscription.stripe_price_id:
        subscription.stripe_price_id = current_app.config.get("STRIPE_MONTHLY_PRICE_ID") or current_app.config.get("STRIPE_PRICE_ID")
    db.session.commit()
    return subscription


def clear_complimentary_subscription(organization: Organization) -> OrganizationSubscription:
    subscription = ensure_subscription_record(organization)
    if not subscription_status_is_complimentary(subscription.status):
        return subscription
    subscription.status = "incomplete"
    subscription.current_period_end = None
    subscription.cancel_at_period_end = False
    db.session.commit()
    return subscription


def fake_checkout_enabled() -> bool:
    return bool(current_app.config.get("STRIPE_FAKE_CHECKOUT_ENABLED"))


def is_fake_checkout_session_id(session_id: str | None) -> bool:
    normalized = (session_id or "").strip()
    return fake_checkout_enabled() and normalized.startswith(FAKE_CHECKOUT_SESSION_PREFIX)


def _fake_checkout_session_id(organization: Organization) -> str:
    return f"{FAKE_CHECKOUT_SESSION_PREFIX}{organization.id}"


def _fake_checkout_organization_id(session_id: str) -> int:
    normalized = (session_id or "").strip()
    if not normalized.startswith(FAKE_CHECKOUT_SESSION_PREFIX):
        raise RuntimeError("Unsupported fake checkout session id.")
    try:
        return int(normalized[len(FAKE_CHECKOUT_SESSION_PREFIX):])
    except ValueError as exc:
        raise RuntimeError("Invalid fake checkout session id.") from exc


def _fake_checkout_url(
    organization: Organization,
    *,
    session_id: str,
    success_url: str,
    cancel_url: str,
) -> str:
    return url_for(
        "main.fake_stripe_checkout",
        session_id=session_id,
        organization_id=organization.id,
        success_url=success_url,
        cancel_url=cancel_url,
        _external=True,
    )


def _fake_checkout_period_end() -> datetime | None:
    trial_days = int(current_app.config.get("BILLING_TRIAL_DAYS", 0) or 0)
    if trial_days <= 0:
        return None
    return utc_now() + timedelta(days=trial_days)


def _apply_fake_checkout_session(
    session_id: str,
    organization: Organization | None = None,
) -> OrganizationSubscription:
    fake_organization_id = _fake_checkout_organization_id(session_id)
    target_organization = organization
    if target_organization is None:
        target_organization = db.session.get(Organization, fake_organization_id)
    if target_organization is None:
        raise RuntimeError("Fake checkout organization was not found.")
    if target_organization.id != fake_organization_id:
        raise RuntimeError("Fake checkout session does not belong to this organization.")

    subscription = ensure_subscription_record(target_organization)
    fake_period_end = _fake_checkout_period_end()
    subscription.status = "trialing" if fake_period_end is not None else "active"
    subscription.current_period_end = fake_period_end
    subscription.cancel_at_period_end = False
    if not subscription.stripe_price_id:
        subscription.stripe_price_id = current_app.config.get("STRIPE_MONTHLY_PRICE_ID") or current_app.config.get("STRIPE_PRICE_ID")
    if subscription.activation_fee_paid_at is None:
        subscription.activation_fee_paid_at = utc_now()
        subscription.activation_price_id = current_app.config.get("STRIPE_ACTIVATION_PRICE_ID")
        subscription.activation_payment_intent_id = f"pi_fake_org_{target_organization.id}"
        subscription.activation_invoice_id = f"in_fake_org_{target_organization.id}"
    db.session.commit()
    return subscription


def _stripe_module() -> Any:
    try:
        import stripe  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only when dependency missing
        raise RuntimeError("stripe package is not installed.") from exc

    secret_key = current_app.config.get("STRIPE_SECRET_KEY")
    if not secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured.")
    stripe.api_key = secret_key
    return stripe


def _stripe_object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return dict(value)


def _retry_stripe_operation(operation_name: str, operation: Callable[[], Any]) -> Any:
    attempts = int(current_app.config.get("STRIPE_CONFIGURATION_VALIDATION_ATTEMPTS", 3) or 3)
    if attempts < 1:
        raise RuntimeError("STRIPE_CONFIGURATION_VALIDATION_ATTEMPTS must be at least 1.")
    last_error: Exception | None = None
    for attempt_number in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            current_app.logger.warning(
                "Stripe configuration read failed.",
                extra={
                    "operation": operation_name,
                    "attempt_number": attempt_number,
                    "attempt_limit": attempts,
                    "error_type": type(exc).__name__,
                },
            )
            if attempt_number < attempts:
                time.sleep(min(0.25 * attempt_number, 1.0))
    if last_error is None:
        raise RuntimeError(f"Stripe configuration read failed without an error: {operation_name}.")
    raise RuntimeError(
        f"Stripe configuration read failed after {attempts} attempts: {operation_name}: {last_error}"
    ) from last_error


def _portal_supported_price_ids(configuration: dict[str, Any]) -> set[str]:
    subscription_update = (
        configuration.get("features", {})
        .get("subscription_update", {})
    )
    supported: set[str] = set()
    for product in subscription_update.get("products", []) or []:
        if not isinstance(product, dict):
            continue
        for price_id in product.get("prices", []) or []:
            normalized = str(price_id or "").strip()
            if normalized:
                supported.add(normalized)
    return supported


def validate_live_stripe_configuration() -> dict[str, str]:
    """Verify live resource ownership, prices, webhook, and portal restrictions."""
    stripe = _stripe_module()
    expected_account_id = str(current_app.config["STRIPE_EXPECTED_ACCOUNT_ID"]).strip()
    secret_key = str(current_app.config["STRIPE_SECRET_KEY"]).strip()
    account_validation = "resource_ownership"
    if not secret_key.startswith("rk_live_"):
        account = _stripe_object_to_dict(
            _retry_stripe_operation("account.retrieve", lambda: stripe.Account.retrieve())
        )
        if str(account.get("id") or "").strip() != expected_account_id:
            raise RuntimeError(
                "Stripe account mismatch: "
                f"expected {expected_account_id}, received {account.get('id') or 'missing id'}."
            )
        account_validation = "account_endpoint"

    validated_price_ids: dict[str, str] = {}
    for expectation in LIVE_PRICE_EXPECTATIONS:
        price_id = str(current_app.config.get(expectation.config_key) or "").strip()
        price = _stripe_object_to_dict(
            _retry_stripe_operation(
                f"price.retrieve:{price_id}",
                lambda: stripe.Price.retrieve(price_id),
            )
        )
        recurring = price.get("recurring") if isinstance(price.get("recurring"), dict) else None
        actual_interval = str(recurring.get("interval") or "").strip() if recurring else None
        errors: list[str] = []
        if str(price.get("id") or "").strip() != price_id:
            errors.append("returned id does not match")
        if price.get("active") is not True:
            errors.append("price is not active")
        if price.get("livemode") is not True:
            errors.append("price is not live mode")
        if str(price.get("currency") or "").lower() != "usd":
            errors.append("currency is not USD")
        if int(price.get("unit_amount") or -1) != expectation.amount_cents:
            errors.append(f"amount is not {expectation.amount_cents} cents")
        if actual_interval != expectation.recurring_interval:
            errors.append(
                f"recurrence is {actual_interval or 'one-time'}, expected "
                f"{expectation.recurring_interval or 'one-time'}"
            )
        if errors:
            raise RuntimeError(f"Stripe price {price_id} is invalid: {', '.join(errors)}.")
        validated_price_ids[expectation.config_key] = price_id

    webhook_endpoint_id = str(current_app.config["STRIPE_WEBHOOK_ENDPOINT_ID"]).strip()
    endpoint = _stripe_object_to_dict(
        _retry_stripe_operation(
            f"webhook_endpoint.retrieve:{webhook_endpoint_id}",
            lambda: stripe.WebhookEndpoint.retrieve(webhook_endpoint_id),
        )
    )
    expected_webhook_url = f"{str(current_app.config['APP_BASE_URL']).rstrip('/')}/webhooks/stripe"
    enabled_events = {str(value) for value in endpoint.get("enabled_events", []) or []}
    missing_events = sorted(REQUIRED_STRIPE_WEBHOOK_EVENTS - enabled_events)
    if str(endpoint.get("url") or "").rstrip("/") != expected_webhook_url.rstrip("/"):
        raise RuntimeError(
            f"Stripe webhook endpoint URL must be {expected_webhook_url}; received {endpoint.get('url') or 'missing'}."
        )
    if str(endpoint.get("status") or "").lower() != "enabled":
        raise RuntimeError(f"Stripe webhook endpoint {webhook_endpoint_id} is not enabled.")
    if missing_events:
        raise RuntimeError(
            f"Stripe webhook endpoint {webhook_endpoint_id} is missing events: {', '.join(missing_events)}."
        )

    portal_configuration_id = str(current_app.config["STRIPE_PORTAL_CONFIGURATION_ID"]).strip()
    portal = _stripe_object_to_dict(
        _retry_stripe_operation(
            f"billing_portal.configuration.retrieve:{portal_configuration_id}",
            lambda: stripe.billing_portal.Configuration.retrieve(
                portal_configuration_id,
                expand=["features.subscription_update.products"],
            ),
        )
    )
    subscription_update = portal.get("features", {}).get("subscription_update", {})
    expected_portal_prices = {
        validated_price_ids["STRIPE_MONTHLY_PRICE_ID"],
        validated_price_ids["STRIPE_ANNUAL_PRICE_ID"],
    }
    actual_portal_prices = _portal_supported_price_ids(portal)
    actual_allowed_updates = {
        str(value or "").strip()
        for value in subscription_update.get("default_allowed_updates", []) or []
        if str(value or "").strip()
    }
    if portal.get("active") is not True:
        raise RuntimeError(f"Stripe portal configuration {portal_configuration_id} is not active.")
    if portal.get("livemode") is not True:
        raise RuntimeError(f"Stripe portal configuration {portal_configuration_id} is not live mode.")
    if subscription_update.get("enabled") is not True:
        raise RuntimeError("Stripe portal subscription updates must be enabled with explicit products.")
    if actual_allowed_updates != {"price"}:
        raise RuntimeError(
            "Stripe portal subscription updates must allow price changes only; "
            f"received {sorted(actual_allowed_updates)}."
        )
    if actual_portal_prices != expected_portal_prices:
        raise RuntimeError(
            "Stripe portal price allowlist mismatch: "
            f"expected {sorted(expected_portal_prices)}, received {sorted(actual_portal_prices)}."
        )

    return {
        "account_id": expected_account_id,
        "account_validation": account_validation,
        "webhook_endpoint_id": webhook_endpoint_id,
        "portal_configuration_id": portal_configuration_id,
    }


def _event_timestamp_to_utc_datetime(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _extract_organization_id(data_object: dict) -> int | None:
    metadata = data_object.get("metadata") or {}
    parent_subscription_metadata = (
        data_object.get("parent", {})
        .get("subscription_details", {})
        .get("metadata", {})
    )
    line_metadata_candidates = []
    for line_item in data_object.get("lines", {}).get("data", []) or []:
        if isinstance(line_item, dict):
            line_metadata_candidates.append(line_item.get("metadata") or {})
    client_reference_id = data_object.get("client_reference_id")
    raw_value = (
        metadata.get("organization_id")
        or parent_subscription_metadata.get("organization_id")
        or next(
            (
                line_metadata.get("organization_id")
                for line_metadata in line_metadata_candidates
                if line_metadata.get("organization_id")
            ),
            None,
        )
        or client_reference_id
    )
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _existing_organization_id(organization_id: int | None) -> int | None:
    if organization_id is None:
        return None
    if db.session.get(Organization, organization_id) is None:
        return None
    return organization_id


def _stripe_subscription_id_from_data_object(event_type: str, data_object: dict) -> str | None:
    if event_type.startswith("customer.subscription."):
        return data_object.get("id")
    subscription_id = (
        data_object.get("subscription")
        or data_object.get("parent", {})
        .get("subscription_details", {})
        .get("subscription")
    )
    if subscription_id:
        return subscription_id

    for line_item in data_object.get("lines", {}).get("data", []) or []:
        if not isinstance(line_item, dict):
            continue
        subscription_id = (
            line_item.get("parent", {})
            .get("subscription_item_details", {})
            .get("subscription")
        )
        if subscription_id:
            return subscription_id
    return None


def _owner_email_for_organization(organization: Organization) -> str:
    owner_membership = (
        OrganizationMembership.query
        .join(AppUser, AppUser.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == organization.id)
        .filter(OrganizationMembership.role == "owner")
        .filter(AppUser.email.isnot(None))
        .order_by(OrganizationMembership.id.asc())
        .first()
    )
    if owner_membership and owner_membership.user and owner_membership.user.email:
        return owner_membership.user.email

    membership = (
        OrganizationMembership.query
        .join(AppUser, AppUser.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == organization.id)
        .filter(AppUser.email.isnot(None))
        .order_by(OrganizationMembership.id.asc())
        .first()
    )
    if membership and membership.user and membership.user.email:
        return membership.user.email
    return ""


def _subscription_state_snapshot(subscription: OrganizationSubscription | None) -> tuple:
    if subscription is None:
        return ()
    period_end = as_utc_datetime(subscription.current_period_end)
    return (
        subscription.status,
        subscription.stripe_customer_id,
        subscription.stripe_subscription_id,
        subscription.stripe_price_id,
        period_end.isoformat() if period_end else None,
        bool(subscription.cancel_at_period_end),
    )


def _checkout_session_reference_time(subscription: OrganizationSubscription) -> datetime | None:
    reference_candidates = [
        as_utc_datetime(subscription.created_at),
        as_utc_datetime(subscription.organization.created_at) if subscription.organization is not None else None,
    ]
    return max((candidate for candidate in reference_candidates if candidate is not None), default=None)


def _checkout_session_matches_current_subscription(
    data_object: dict,
    subscription: OrganizationSubscription,
) -> bool:
    reference_time = _checkout_session_reference_time(subscription)
    session_created_at = _event_timestamp_to_utc_datetime(data_object.get("created"))
    if reference_time is None or session_created_at is None:
        return True
    return session_created_at >= (reference_time - CHECKOUT_SESSION_CREATED_SKEW_ALLOWANCE)


def _stripe_collection_data(collection) -> list:
    if isinstance(collection, dict):
        data = collection.get("data") or []
    else:
        data = getattr(collection, "data", []) or []
    if isinstance(data, list):
        return data
    return list(data)


def _stripe_collection_has_more(collection) -> bool:
    if isinstance(collection, dict):
        return bool(collection.get("has_more", False))
    has_more = getattr(collection, "has_more", False)
    return has_more if isinstance(has_more, bool) else False


def _find_matching_checkout_session(
    stripe,
    organization: Organization,
    subscription: OrganizationSubscription,
    user_email: str = "",
) -> dict | None:
    normalized_email = user_email.strip().lower()
    reference_time = _checkout_session_reference_time(subscription)
    params = {
        "status": "complete",
        "limit": 100,
    }
    if reference_time is not None:
        params["created"] = {
            "gte": int((reference_time - CHECKOUT_SESSION_CREATED_SKEW_ALLOWANCE).timestamp()),
        }
    if subscription.stripe_customer_id:
        params["customer"] = subscription.stripe_customer_id

    starting_after = None
    while True:
        page_params = dict(params)
        if starting_after:
            page_params["starting_after"] = starting_after

        page = stripe.checkout.Session.list(**page_params)
        sessions = _stripe_collection_data(page)
        if not sessions:
            return None

        oldest_session_created_at = None
        for checkout_session in sessions:
            data_object = _stripe_object_to_dict(checkout_session)
            created_at = _event_timestamp_to_utc_datetime(data_object.get("created"))
            if created_at is not None and (
                oldest_session_created_at is None or created_at < oldest_session_created_at
            ):
                oldest_session_created_at = created_at

            metadata = data_object.get("metadata") or {}
            organization_id = metadata.get("organization_id") or data_object.get("client_reference_id")
            if str(organization_id or "") != str(organization.id):
                continue
            if normalized_email and (data_object.get("customer_email") or "").strip().lower() != normalized_email:
                continue
            if data_object.get("status") != "complete":
                continue
            if not _checkout_session_matches_current_subscription(data_object, subscription):
                continue
            issued = StripeCheckoutSession.query.filter_by(
                stripe_checkout_session_id=str(data_object.get("id") or ""),
                organization_id=organization.id,
                status="open",
            ).first()
            if issued is None or issued.offer_version != _organization_offer_version(organization):
                continue
            return data_object

        if reference_time is not None and oldest_session_created_at is not None:
            if oldest_session_created_at < (reference_time - CHECKOUT_SESSION_CREATED_SKEW_ALLOWANCE):
                return None
        if not _stripe_collection_has_more(page):
            return None

        last_session = _stripe_object_to_dict(sessions[-1])
        starting_after = last_session.get("id")
        if not starting_after:
            return None


def _organization_offer_version(organization: Organization) -> str:
    catalog_version = str(current_app.config.get("BILLING_OFFER_VERSION") or "").strip()
    if not catalog_version:
        raise RuntimeError("BILLING_OFFER_VERSION is not configured.")
    offer_code = str(organization.billing_offer or "standard").strip().lower()
    return f"{catalog_version}:{offer_code}"


def expire_incompatible_checkout_sessions(
    organization: Organization,
    target_offer_version: str,
) -> int:
    sessions = (
        StripeCheckoutSession.query
        .filter_by(organization_id=organization.id, status="open")
        .filter(StripeCheckoutSession.offer_version != target_offer_version)
        .all()
    )
    if not sessions:
        return 0

    stripe = None if fake_checkout_enabled() else _stripe_module()
    expired_count = 0
    for record in sessions:
        if stripe is not None and not is_fake_checkout_session_id(record.stripe_checkout_session_id):
            _retry_stripe_operation(
                f"checkout.session.expire:{record.stripe_checkout_session_id}",
                lambda: stripe.checkout.Session.expire(record.stripe_checkout_session_id),
            )
        record.status = "expired"
        record.expired_at = utc_now()
        expired_count += 1
    db.session.commit()
    return expired_count


def update_organization_billing_offer(
    organization: Organization,
    billing_offer: str,
) -> OrganizationSubscription:
    normalized_offer = str(billing_offer or "").strip().lower()
    if normalized_offer not in {"standard", "annual_only"}:
        raise RuntimeError("Choose a valid checkout offer.")
    target_version = (
        f"{str(current_app.config.get('BILLING_OFFER_VERSION') or '').strip()}:{normalized_offer}"
    )
    if target_version.startswith(":"):
        raise RuntimeError("BILLING_OFFER_VERSION is not configured.")
    expire_incompatible_checkout_sessions(organization, target_version)
    organization.billing_offer = normalized_offer
    organization.billing_offer_version = target_version
    subscription = ensure_subscription_record(organization)
    if not subscription_activation_paid(subscription):
        plan_code = "annual" if normalized_offer == "annual_only" else "monthly"
        plan = billing_plan_for_code(plan_code)
        if plan is not None:
            subscription.stripe_price_id = plan.price_id
    subscription.offer_version = target_version
    db.session.commit()
    return subscription


def _checkout_session_record(
    session_id: str,
    organization: Organization,
) -> StripeCheckoutSession:
    record = StripeCheckoutSession.query.filter_by(
        stripe_checkout_session_id=session_id,
    ).first()
    if record is None:
        raise StripePaymentProofError(
            "Checkout session was not issued by this Twinevia release."
        )
    if record.organization_id != organization.id:
        raise StripePaymentProofError("Checkout session belongs to another organization.")
    return record


def _record_issued_checkout_session(
    session: Any,
    organization: Organization,
    plan_code: str,
    recurring_price_id: str,
    setup_price_id: str | None,
    offer_version: str,
) -> StripeCheckoutSession:
    session_data = _stripe_object_to_dict(session)
    session_id = str(session_data.get("id") or getattr(session, "id", "") or "").strip()
    if not session_id:
        raise RuntimeError("Stripe Checkout did not return a session id.")
    record = StripeCheckoutSession.query.filter_by(
        stripe_checkout_session_id=session_id,
    ).first()
    if record is None:
        record = StripeCheckoutSession(
            organization_id=organization.id,
            stripe_checkout_session_id=session_id,
            billing_plan_code=plan_code,
            recurring_price_id=recurring_price_id,
            activation_price_id=setup_price_id,
            offer_version=offer_version,
            status="open",
        )
        db.session.add(record)
    else:
        if record.organization_id != organization.id:
            raise RuntimeError("Stripe Checkout session id is already bound to another organization.")
        record.billing_plan_code = plan_code
        record.recurring_price_id = recurring_price_id
        record.activation_price_id = setup_price_id
        record.offer_version = offer_version
        record.status = "open"
    organization.billing_offer_version = offer_version
    organization.subscription.offer_version = offer_version
    db.session.commit()
    return record


def create_checkout_session(
    organization: Organization,
    user_email: str,
    success_url: str,
    cancel_url: str,
    *,
    plan_code: str | None = None,
):
    requested_offer_version = _organization_offer_version(organization)
    expire_incompatible_checkout_sessions(organization, requested_offer_version)
    organization = (
        Organization.query
        .filter(Organization.id == organization.id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    subscription = ensure_subscription_record(organization)
    if subscription_status_is_complimentary(subscription.status):
        raise RuntimeError("Complimentary billing is already active for this organization.")

    eligible_plans = billing_plan_options_for_organization(organization)
    eligible_plan_codes = {plan.code for plan in eligible_plans}
    selected_plan = billing_plan_for_code(plan_code) if plan_code else None
    if plan_code and (selected_plan is None or selected_plan.code not in eligible_plan_codes):
        raise RuntimeError("Choose a valid billing option.")
    if selected_plan is None and len(eligible_plans) == 1:
        selected_plan = eligible_plans[0]
    price_id = selected_plan.price_id if selected_plan is not None else recurring_price_id_for_subscription(subscription)
    if not price_id:
        raise RuntimeError("STRIPE_MONTHLY_PRICE_ID or STRIPE_PRICE_ID is not configured.")
    if selected_plan is not None and subscription.stripe_price_id != price_id:
        subscription.stripe_price_id = price_id
    resolved_plan = selected_plan or billing_plan_for_price_id(price_id)
    if resolved_plan is None or resolved_plan.code not in eligible_plan_codes:
        raise RuntimeError("The subscription price is not part of the current Twinevia offer.")

    offer_version = _organization_offer_version(organization)
    setup_price_id = None if subscription_activation_paid(subscription) else activation_price_id()
    if not setup_price_id and not subscription_activation_paid(subscription):
        raise RuntimeError("STRIPE_ACTIVATION_PRICE_ID is not configured.")

    if fake_checkout_enabled():
        session_id = _fake_checkout_session_id(organization)
        session = SimpleNamespace(
            id=session_id,
            url=_fake_checkout_url(
                organization,
                session_id=session_id,
                success_url=success_url,
                cancel_url=cancel_url,
            ),
        )
        _record_issued_checkout_session(
            session,
            organization,
            resolved_plan.code,
            price_id,
            setup_price_id,
            offer_version,
        )
        return session

    stripe = _stripe_module()
    compatible_open_sessions = (
        StripeCheckoutSession.query
        .filter_by(
            organization_id=organization.id,
            billing_plan_code=resolved_plan.code,
            recurring_price_id=price_id,
            activation_price_id=setup_price_id,
            offer_version=offer_version,
            status="open",
        )
        .order_by(StripeCheckoutSession.id.desc())
        .all()
    )
    for record in compatible_open_sessions:
        existing_session = _retry_stripe_operation(
            f"checkout.session.retrieve:{record.stripe_checkout_session_id}",
            lambda: stripe.checkout.Session.retrieve(
                record.stripe_checkout_session_id
            ),
        )
        existing_data = _stripe_object_to_dict(existing_session)
        existing_status = str(existing_data.get("status") or "").strip().lower()
        existing_url = str(
            existing_data.get("url")
            or getattr(existing_session, "url", "")
            or ""
        ).strip()
        if existing_status == "open" and existing_url:
            db.session.commit()
            return existing_session
        if existing_status == "complete":
            db.session.commit()
            raise RuntimeError(
                "An existing Checkout session is complete and is being reconciled."
            )
        record.status = "expired"
        record.expired_at = utc_now()

    line_items: list[dict[str, Any]] = []
    if setup_price_id:
        line_items.append({"price": setup_price_id, "quantity": 1})
    line_items.append({"price": price_id, "quantity": 1})
    checkout_metadata = {
        "organization_id": str(organization.id),
        "billing_plan_code": resolved_plan.code,
        "billing_offer_version": offer_version,
    }

    params = {
        "mode": "subscription",
        "line_items": line_items,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(organization.id),
        "metadata": checkout_metadata,
        "subscription_data": {
            "metadata": checkout_metadata,
        },
    }

    if user_email:
        params["customer_email"] = user_email
    if subscription.stripe_customer_id:
        params["customer"] = subscription.stripe_customer_id
        params.pop("customer_email", None)

    trial_days = int(current_app.config.get("BILLING_TRIAL_DAYS", 0) or 0)
    if trial_days > 0 and not subscription.stripe_subscription_id:
        params["subscription_data"]["trial_period_days"] = trial_days

    checkout_attempt = (
        StripeCheckoutSession.query
        .filter_by(
            organization_id=organization.id,
            billing_plan_code=resolved_plan.code,
            offer_version=offer_version,
        )
        .count()
        + 1
    )
    checkout_request_seed = "|".join(
        (
            str(organization.id),
            offer_version,
            resolved_plan.code,
            price_id,
            setup_price_id or "paid",
            str(checkout_attempt),
        )
    )
    checkout_request_key = (
        "twinevia-checkout-"
        + hashlib.sha256(checkout_request_seed.encode("utf-8")).hexdigest()
    )
    session = _retry_stripe_operation(
        f"checkout.session.create:{organization.id}:{offer_version}:{resolved_plan.code}",
        lambda: stripe.checkout.Session.create(
            **params,
            idempotency_key=checkout_request_key,
        ),
    )
    try:
        _record_issued_checkout_session(
            session,
            organization,
            resolved_plan.code,
            price_id,
            setup_price_id,
            offer_version,
        )
    except Exception:
        db.session.rollback()
        session_data = _stripe_object_to_dict(session)
        session_id = str(session_data.get("id") or getattr(session, "id", "") or "").strip()
        if session_id:
            _retry_stripe_operation(
                f"checkout.session.expire:{session_id}",
                lambda: stripe.checkout.Session.expire(session_id),
            )
        raise
    return session


def create_billing_portal_session(organization: Organization, return_url: str):
    subscription = ensure_subscription_record(organization)
    if subscription_status_is_complimentary(subscription.status):
        raise RuntimeError("Complimentary organizations do not use the Stripe billing portal.")
    if not subscription.stripe_customer_id:
        raise RuntimeError("Organization does not have a Stripe customer yet.")

    stripe = _stripe_module()
    portal_configuration_id = str(current_app.config.get("STRIPE_PORTAL_CONFIGURATION_ID") or "").strip()
    if current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED") and not portal_configuration_id:
        raise RuntimeError("STRIPE_PORTAL_CONFIGURATION_ID is required for live billing.")
    params: dict[str, str] = {
        "customer": subscription.stripe_customer_id,
        "return_url": return_url,
    }
    if portal_configuration_id:
        params["configuration"] = portal_configuration_id
    return stripe.billing_portal.Session.create(
        **params,
    )


def _stripe_line_item_price_id(line_item: dict[str, Any]) -> str:
    price = line_item.get("price")
    if isinstance(price, dict):
        return str(price.get("id") or "").strip()
    if isinstance(price, str):
        return price.strip()
    pricing = line_item.get("pricing")
    if isinstance(pricing, dict):
        price_details = pricing.get("price_details")
        if isinstance(price_details, dict):
            return str(price_details.get("price") or "").strip()
    return ""


def _checkout_line_item_quantities(stripe: Any, session_id: str) -> dict[str, int]:
    collection = _retry_stripe_operation(
        f"checkout.session.list_line_items:{session_id}",
        lambda: stripe.checkout.Session.list_line_items(session_id, limit=100),
    )
    quantities: dict[str, int] = {}
    for raw_line_item in _stripe_collection_data(collection):
        line_item = _stripe_object_to_dict(raw_line_item)
        price_id = _stripe_line_item_price_id(line_item)
        if not price_id:
            raise StripePaymentProofError(
                f"Checkout session {session_id} contains a line without a price id."
            )
        quantity = int(line_item.get("quantity") or 0)
        quantities[price_id] = quantities.get(price_id, 0) + quantity
    return quantities


def _verified_invoice_payment_intent(
    stripe: Any,
    invoice_id: str,
    setup_price_id: str,
) -> tuple[str, dict[str, Any]]:
    invoice = _stripe_object_to_dict(
        _retry_stripe_operation(
            f"invoice.retrieve:{invoice_id}",
            lambda: stripe.Invoice.retrieve(invoice_id, expand=["payment_intent"]),
        )
    )
    if invoice.get("paid") is not True or str(invoice.get("status") or "").lower() != "paid":
        raise StripePaymentProofError(f"Stripe invoice {invoice_id} is not paid.")
    if current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED") and invoice.get("livemode") is not True:
        raise StripePaymentProofError(f"Stripe invoice {invoice_id} is not live mode.")
    if int(invoice.get("amount_paid") or 0) < 14900:
        raise StripePaymentProofError(
            f"Stripe invoice {invoice_id} does not prove the $149 setup payment."
        )

    invoice_line_items = invoice.get("lines", {}).get("data", []) or []
    invoice_prices = {
        _stripe_line_item_price_id(_stripe_object_to_dict(line_item))
        for line_item in invoice_line_items
    }
    if setup_price_id not in invoice_prices:
        raise StripePaymentProofError(
            f"Stripe invoice {invoice_id} does not contain setup price {setup_price_id}."
        )

    raw_payment_intent = invoice.get("payment_intent")
    if isinstance(raw_payment_intent, dict):
        payment_intent = raw_payment_intent
    else:
        payment_intent_id = str(raw_payment_intent or "").strip()
        if not payment_intent_id:
            raise StripePaymentProofError(
                f"Stripe invoice {invoice_id} does not reference a PaymentIntent."
            )
        payment_intent = _stripe_object_to_dict(
            _retry_stripe_operation(
                f"payment_intent.retrieve:{payment_intent_id}",
                lambda: stripe.PaymentIntent.retrieve(payment_intent_id),
            )
        )
    payment_intent_id = str(payment_intent.get("id") or "").strip()
    if not payment_intent_id:
        raise StripePaymentProofError(f"Stripe invoice {invoice_id} has an invalid PaymentIntent.")
    if str(payment_intent.get("status") or "").lower() != "succeeded":
        raise StripePaymentProofError(
            f"Stripe PaymentIntent {payment_intent_id} has not succeeded."
        )
    if (
        current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED")
        and payment_intent.get("livemode") is not True
    ):
        raise StripePaymentProofError(
            f"Stripe PaymentIntent {payment_intent_id} is not live mode."
        )
    if int(payment_intent.get("amount_received") or 0) < 14900:
        raise StripePaymentProofError(
            f"Stripe PaymentIntent {payment_intent_id} does not prove the $149 setup payment."
        )
    return payment_intent_id, invoice


def _verify_checkout_session_payment(
    session_id: str,
) -> tuple[OrganizationSubscription, dict[str, Any], StripeCheckoutSession]:
    stripe = _stripe_module()
    data_object = _stripe_object_to_dict(
        _retry_stripe_operation(
            f"checkout.session.retrieve:{session_id}",
            lambda: stripe.checkout.Session.retrieve(session_id),
        )
    )
    organization_id = _extract_organization_id(data_object)
    organization = db.session.get(Organization, organization_id) if organization_id else None
    if organization is None:
        raise StripePaymentProofError(
            f"Checkout session {session_id} does not identify an existing organization."
        )
    record = _checkout_session_record(session_id, organization)
    current_offer_version = _organization_offer_version(organization)
    metadata = data_object.get("metadata") or {}
    if record.offer_version != current_offer_version:
        raise StripePaymentProofError(
            f"Checkout session {session_id} belongs to expired offer {record.offer_version}."
        )
    if str(metadata.get("billing_offer_version") or "") != record.offer_version:
        raise StripePaymentProofError(
            f"Checkout session {session_id} offer metadata does not match its issuance record."
        )
    if str(data_object.get("status") or "").lower() != "complete":
        raise StripePaymentProofError(f"Checkout session {session_id} is not complete.")
    if (
        current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED")
        and data_object.get("livemode") is not True
    ):
        raise StripePaymentProofError(f"Checkout session {session_id} is not live mode.")
    if str(data_object.get("payment_status") or "").lower() != "paid":
        raise StripePaymentProofError(f"Checkout session {session_id} is not paid.")

    actual_quantities = _checkout_line_item_quantities(stripe, session_id)
    expected_quantities = {record.recurring_price_id: 1}
    if record.activation_price_id:
        expected_quantities[record.activation_price_id] = 1
    if actual_quantities != expected_quantities:
        raise StripePaymentProofError(
            f"Checkout session {session_id} line items do not match the issued offer."
        )

    subscription = ensure_subscription_record(organization)
    if record.activation_price_id:
        invoice_id = str(data_object.get("invoice") or "").strip()
        if not invoice_id:
            raise StripePaymentProofError(
                f"Checkout session {session_id} does not reference its paid invoice."
            )
        payment_intent_id, _invoice = _verified_invoice_payment_intent(
            stripe,
            invoice_id,
            record.activation_price_id,
        )
        subscription.activation_fee_paid_at = utc_now()
        subscription.activation_price_id = record.activation_price_id
        subscription.activation_payment_intent_id = payment_intent_id
        subscription.activation_invoice_id = invoice_id

    record.status = "completed"
    record.completed_at = utc_now()
    record.stripe_customer_id = str(data_object.get("customer") or "").strip() or None
    record.stripe_subscription_id = str(data_object.get("subscription") or "").strip() or None
    subscription.offer_version = record.offer_version
    db.session.commit()
    return subscription, data_object, record


def _event_precedes_applied_state(
    subscription: OrganizationSubscription,
    event_created_at: datetime,
) -> bool:
    previous_created_at = as_utc_datetime(subscription.last_stripe_event_created_at)
    if previous_created_at is None:
        return False
    normalized_created_at = as_utc_datetime(event_created_at)
    if normalized_created_at is None:
        return True
    if normalized_created_at < previous_created_at:
        return True
    if normalized_created_at > previous_created_at:
        return False
    return False


def sync_subscription_from_event(
    event_type: str,
    data_object: dict[str, Any],
    event_created_at: datetime,
    event_id: str,
    authoritative_snapshot: bool,
) -> OrganizationSubscription | None:
    organization_id = _extract_organization_id(data_object)

    subscription = None
    if organization_id:
        organization = db.session.get(Organization, organization_id)
        if organization is not None:
            subscription = ensure_subscription_record(organization)

    if subscription is None:
        stripe_customer_id = data_object.get("customer")
        stripe_subscription_id = _stripe_subscription_id_from_data_object(event_type, data_object)
        query = OrganizationSubscription.query
        if stripe_subscription_id:
            subscription = query.filter_by(stripe_subscription_id=stripe_subscription_id).first()
        if subscription is None and stripe_customer_id:
            subscription = query.filter_by(stripe_customer_id=stripe_customer_id).first()
        if subscription is None:
            return None

    subscription = (
        OrganizationSubscription.query
        .filter(OrganizationSubscription.id == subscription.id)
        .populate_existing()
        .with_for_update()
        .one()
    )

    if subscription_status_is_complimentary(subscription.status):
        stripe_customer_id = str(data_object.get("customer") or "").strip()
        stripe_subscription_id = str(
            _stripe_subscription_id_from_data_object(event_type, data_object) or ""
        ).strip()
        raise StripeComplimentaryConflictError(
            "Stripe reported chargeable billing for complimentary organization "
            f"{subscription.organization_id}: event_id={event_id}, "
            f"customer_id={stripe_customer_id or 'missing'}, "
            f"subscription_id={stripe_subscription_id or 'missing'}. "
            "Cancel or reconcile the Stripe subscription before processing this event."
        )

    if (
        not authoritative_snapshot
        and _event_precedes_applied_state(subscription, event_created_at)
    ):
        raise StaleStripeEventError(
            f"Stripe event {event_id} is older than {subscription.last_stripe_event_id}."
        )
    if (
        authoritative_snapshot
        and current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED")
        and data_object.get("livemode") is not True
    ):
        raise StripePaymentProofError(
            f"Stripe subscription snapshot for event {event_id} is not live mode."
        )

    stripe_customer_id = data_object.get("customer")
    stripe_subscription_id = _stripe_subscription_id_from_data_object(event_type, data_object)
    status = data_object.get("status")

    if stripe_customer_id:
        subscription.stripe_customer_id = stripe_customer_id
    if stripe_subscription_id:
        subscription.stripe_subscription_id = stripe_subscription_id
    if status and event_type != "checkout.session.completed":
        subscription.status = status
    if data_object.get("items", {}).get("data"):
        first_item = data_object["items"]["data"][0]
        if first_item.get("price", {}).get("id"):
            subscription.stripe_price_id = first_item["price"]["id"]

    period_end = data_object.get("current_period_end")
    if period_end:
        subscription.current_period_end = datetime.fromtimestamp(int(period_end), tz=timezone.utc)
    cancel_at_period_end = data_object.get("cancel_at_period_end")
    if cancel_at_period_end is not None:
        subscription.cancel_at_period_end = bool(cancel_at_period_end)

    if event_type == "checkout.session.completed":
        if data_object.get("subscription"):
            subscription.stripe_subscription_id = data_object["subscription"]
        if not subscription.status:
            subscription.status = "active"

    previous_created_at = as_utc_datetime(subscription.last_stripe_event_created_at)
    normalized_created_at = as_utc_datetime(event_created_at)
    if (
        previous_created_at is None
        or (
            normalized_created_at is not None
            and normalized_created_at > previous_created_at
        )
    ):
        subscription.last_stripe_event_created_at = event_created_at
        subscription.last_stripe_event_id = event_id

    db.session.commit()
    return subscription


def _apply_stripe_event_to_billing_state(
    event_type: str,
    data_object: dict[str, Any],
    event_created_at: datetime,
    event_id: str,
) -> OrganizationSubscription | None:
    if event_type == "checkout.session.completed":
        session_id = str(data_object.get("id") or "").strip()
        if not session_id:
            raise StripePaymentProofError("Checkout event is missing a session id.")
        subscription, verified_session, _record = _verify_checkout_session_payment(session_id)
        stripe_subscription_id = str(verified_session.get("subscription") or "").strip()
        if not stripe_subscription_id:
            raise StripePaymentProofError(
                f"Checkout session {session_id} does not reference a subscription."
            )
        stripe = _stripe_module()
        stripe_subscription = _retry_stripe_operation(
            f"subscription.retrieve:{stripe_subscription_id}",
            lambda: stripe.Subscription.retrieve(stripe_subscription_id),
        )
        subscription_data = _stripe_object_to_dict(stripe_subscription)
        subscription_data.setdefault("metadata", {})["organization_id"] = str(
            subscription.organization_id
        )
        return sync_subscription_from_event(
            "customer.subscription.updated",
            subscription_data,
            event_created_at,
            event_id,
            True,
        )

    if event_type in {"invoice.payment_succeeded", "invoice.payment_failed"}:
        stripe_subscription_id = _stripe_subscription_id_from_data_object(event_type, data_object)
        if not stripe_subscription_id:
            return None
        stripe = _stripe_module()
        stripe_subscription = _retry_stripe_operation(
            f"subscription.retrieve:{stripe_subscription_id}",
            lambda: stripe.Subscription.retrieve(stripe_subscription_id),
        )
        return sync_subscription_from_event(
            "customer.subscription.updated",
            _stripe_object_to_dict(stripe_subscription),
            event_created_at,
            event_id,
            True,
        )

    if event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        stripe_subscription_id = _stripe_subscription_id_from_data_object(event_type, data_object)
        if not stripe_subscription_id:
            return None
        stripe = _stripe_module()
        stripe_subscription = _retry_stripe_operation(
            f"subscription.retrieve:{stripe_subscription_id}",
            lambda: stripe.Subscription.retrieve(stripe_subscription_id),
        )
        subscription_data = _stripe_object_to_dict(stripe_subscription)
        organization_id = _extract_organization_id(data_object)
        if organization_id:
            subscription_data.setdefault("metadata", {})["organization_id"] = str(
                organization_id
            )
        return sync_subscription_from_event(
            "customer.subscription.updated",
            subscription_data,
            event_created_at,
            event_id,
            True,
        )

    return sync_subscription_from_event(
        event_type,
        data_object,
        event_created_at,
        event_id,
        False,
    )


def _populate_webhook_record(
    record: StripeWebhookEvent,
    *,
    event_id: str,
    event_type: str,
    data_object: dict,
    event_created_at: datetime | None,
    now: datetime,
) -> None:
    organization_id = _extract_organization_id(data_object)
    record.stripe_event_id = event_id
    record.event_type = event_type
    record.stripe_object_id = data_object.get("id")
    record.stripe_customer_id = data_object.get("customer")
    record.stripe_subscription_id = _stripe_subscription_id_from_data_object(event_type, data_object)
    existing_organization_id = _existing_organization_id(organization_id)
    if existing_organization_id is not None:
        record.organization_id = existing_organization_id
    record.signature_verified = True
    if event_created_at is not None:
        record.event_created_at = event_created_at
    if not record.received_at:
        record.received_at = now
    record.last_seen_at = now


def process_stripe_webhook_event(event: dict) -> StripeWebhookEvent:
    event_id = str(event.get("id") or "").strip()
    event_type = str(event.get("type") or "").strip()
    if not event_id:
        raise RuntimeError("Stripe event is missing an id.")
    if not event_type:
        raise RuntimeError("Stripe event is missing a type.")
    if current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED") and event.get("livemode") is not True:
        raise StripePaymentProofError(f"Stripe event {event_id} is not live mode.")

    data_object = _stripe_object_to_dict(event.get("data", {}).get("object", {}))
    event_created_at = _event_timestamp_to_utc_datetime(event.get("created"))
    if event_created_at is None:
        raise RuntimeError(f"Stripe event {event_id} is missing a valid created timestamp.")
    now = utc_now()

    record = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
    previous_status = record.status if record is not None else None
    previous_processed_at = record.processed_at if record is not None else None
    previous_last_activity = (
        as_utc_datetime(record.last_seen_at or record.received_at)
        if record is not None
        else None
    )
    if record is None:
        record = StripeWebhookEvent(
            stripe_event_id=event_id,
            received_at=now,
            last_seen_at=now,
            attempt_count=1,
            status="processing",
        )
        db.session.add(record)
    else:
        record.attempt_count = int(record.attempt_count or 0) + 1

    _populate_webhook_record(
        record,
        event_id=event_id,
        event_type=event_type,
        data_object=data_object,
        event_created_at=event_created_at,
        now=now,
    )

    if previous_status in {"processed", "ignored"}:
        current_app.logger.info(
            "Stripe webhook already finalized event_id=%s event_type=%s organization_id=%s status=%s attempt_count=%s",
            event_id,
            event_type,
            record.organization_id,
            previous_status,
            record.attempt_count,
        )
        db.session.commit()
        return record

    if previous_status == "processing":
        if (
            previous_last_activity
            and previous_last_activity >= now - WEBHOOK_PROCESSING_STALE_AFTER
            and previous_processed_at is None
        ):
            current_app.logger.info(
                "Stripe webhook still processing event_id=%s event_type=%s organization_id=%s attempt_count=%s",
                event_id,
                event_type,
                record.organization_id,
                record.attempt_count,
            )
            db.session.commit()
            return record

    record.status = "processing"
    record.processed_at = None
    record.last_error = None
    db.session.commit()

    if event_type not in RELEVANT_STRIPE_EVENT_TYPES:
        ignored = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
        if ignored is None:
            raise RuntimeError("Stripe webhook ledger record disappeared during ignore flow.")
        ignored.status = "ignored"
        ignored.processed_at = utc_now()
        ignored.last_error = None
        ignored.last_seen_at = utc_now()
        db.session.commit()
        current_app.logger.info(
            "Stripe webhook ignored event_id=%s event_type=%s organization_id=%s subscription_id=%s",
            event_id,
            event_type,
            ignored.organization_id,
            ignored.stripe_subscription_id,
        )
        return ignored

    try:
        subscription = _apply_stripe_event_to_billing_state(
            event_type,
            data_object,
            event_created_at,
            event_id,
        )
        if subscription is None:
            ignored = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
            if ignored is None:
                raise RuntimeError("Stripe webhook ledger record disappeared during unmatched flow.")
            ignored.status = "ignored"
            ignored.processed_at = utc_now()
            ignored.last_error = f"No local subscription matched Stripe event {event_id}."
            ignored.last_seen_at = utc_now()
            db.session.commit()
            current_app.logger.warning(
                "Stripe webhook ignored unmatched event_id=%s event_type=%s organization_id=%s subscription_id=%s",
                event_id,
                event_type,
                ignored.organization_id,
                ignored.stripe_subscription_id,
            )
            return ignored
    except StaleStripeEventError as exc:
        db.session.rollback()
        ignored = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
        if ignored is None:
            raise
        ignored.status = "ignored"
        ignored.last_error = str(exc)[:2000]
        ignored.processed_at = utc_now()
        ignored.last_seen_at = utc_now()
        db.session.commit()
        current_app.logger.warning(
            "Stripe webhook ignored because it was stale.",
            extra={
                "event_id": event_id,
                "event_type": event_type,
                "organization_id": ignored.organization_id,
            },
        )
        return ignored
    except Exception as exc:
        db.session.rollback()
        failed = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
        if failed is None:
            raise
        failed.status = "failed"
        failed.last_error = str(exc)[:2000]
        failed.processed_at = None
        failed.last_seen_at = utc_now()
        db.session.commit()
        current_app.logger.exception(
            "Stripe webhook failed event_id=%s event_type=%s organization_id=%s subscription_id=%s",
            event_id,
            event_type,
            failed.organization_id,
            failed.stripe_subscription_id,
        )
        raise

    processed = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
    if processed is None:
        raise RuntimeError("Stripe webhook ledger record disappeared during completion flow.")
    processed.status = "processed"
    processed.processed_at = utc_now()
    processed.last_error = None
    processed.last_seen_at = utc_now()
    db.session.commit()
    current_app.logger.info(
        "Stripe webhook processed event_id=%s event_type=%s organization_id=%s subscription_id=%s status=%s",
        event_id,
        event_type,
        processed.organization_id,
        processed.stripe_subscription_id,
        processed.status,
    )
    return processed


def sync_checkout_session_by_id(
    session_id: str,
    organization: Organization | None = None,
) -> OrganizationSubscription | None:
    if is_fake_checkout_session_id(session_id):
        return _apply_fake_checkout_session(session_id, organization)

    stripe = _stripe_module()
    session = _retry_stripe_operation(
        f"checkout.session.retrieve:{session_id}",
        lambda: stripe.checkout.Session.retrieve(session_id),
    )
    data_object = _stripe_object_to_dict(session)
    if organization is not None:
        metadata = data_object.get("metadata") or {}
        org_id = metadata.get("organization_id") or data_object.get("client_reference_id")
        if str(org_id or "") != str(organization.id):
            raise RuntimeError("Checkout session does not belong to this organization.")
    if data_object.get("status") != "complete":
        return None
    return _apply_stripe_event_to_billing_state(
        "checkout.session.completed",
        data_object,
        utc_now(),
        f"manual-checkout-sync:{session_id}:{uuid4().hex}",
    )


def refresh_subscription_from_stripe(
    organization: Organization,
    user_email: str = "",
) -> OrganizationSubscription | None:
    subscription = ensure_subscription_record(organization)
    if subscription_status_is_complimentary(subscription.status):
        return subscription

    stripe = _stripe_module()

    if subscription.stripe_subscription_id:
        stripe_subscription = _retry_stripe_operation(
            f"subscription.retrieve:{subscription.stripe_subscription_id}",
            lambda: stripe.Subscription.retrieve(subscription.stripe_subscription_id),
        )
        return sync_subscription_from_event(
            "customer.subscription.updated",
            _stripe_object_to_dict(stripe_subscription),
            utc_now(),
            f"subscription-refresh:{subscription.id}:{uuid4().hex}",
            True,
        )

    if subscription.stripe_customer_id:
        stripe_subscriptions = stripe.Subscription.list(
            customer=subscription.stripe_customer_id,
            status="all",
            limit=5,
        )
        if stripe_subscriptions.data:
            return sync_subscription_from_event(
                "customer.subscription.updated",
                _stripe_object_to_dict(stripe_subscriptions.data[0]),
                utc_now(),
                f"customer-subscription-refresh:{subscription.id}:{uuid4().hex}",
                True,
            )

    checkout_session = _find_matching_checkout_session(
        stripe,
        organization,
        subscription,
        user_email,
    )
    if checkout_session is not None:
        session_id = str(checkout_session.get("id") or "").strip()
        return _apply_stripe_event_to_billing_state(
            "checkout.session.completed",
            checkout_session,
            utc_now(),
            f"checkout-refresh:{session_id}:{uuid4().hex}",
        )

    return subscription


def _decimal_to_cents(value: Decimal | int | float | str) -> int:
    normalized = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(normalized * 100)


def _usage_invoice_item_idempotency_key(
    period: OrganizationUsageBillingPeriod,
    settlement_version: int,
) -> str:
    return (
        f"sms-overage:{period.organization_id}:"
        f"{period.period_start.isoformat()}:{period.period_end.isoformat()}:v{settlement_version}"
    )


def _usage_invoice_idempotency_key(
    period: OrganizationUsageBillingPeriod,
    settlement_version: int,
) -> str:
    return (
        f"sms-overage-invoice:{period.organization_id}:"
        f"{period.period_start.isoformat()}:{period.period_end.isoformat()}:v{settlement_version}"
    )


def _post_closed_usage_invoice_items() -> dict[str, int]:
    summary = {
        "periods_scanned": 0,
        "periods_posted": 0,
        "periods_skipped": 0,
        "periods_failed": 0,
    }
    now = utc_now()
    periods = OrganizationUsageBillingPeriod.query.filter(
        OrganizationUsageBillingPeriod.overage_units > OrganizationUsageBillingPeriod.invoiced_units,
    ).order_by(
        OrganizationUsageBillingPeriod.period_end.asc(),
        OrganizationUsageBillingPeriod.id.asc(),
    ).all()
    if not periods:
        return summary

    stripe = _stripe_module()
    for period in periods:
        summary["periods_scanned"] += 1
        period = (
            OrganizationUsageBillingPeriod.query
            .filter(OrganizationUsageBillingPeriod.id == period.id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        settlement_updated_at = as_utc_datetime(period.updated_at)
        if (
            period.status in {"invoicing", "item_created"}
            and settlement_updated_at is not None
            and settlement_updated_at >= now - USAGE_SETTLEMENT_LEASE
        ):
            db.session.rollback()
            summary["periods_skipped"] += 1
            continue
        grace_hours = int(current_app.config.get("BILLING_USAGE_SETTLEMENT_GRACE_HOURS", 72) or 72)
        settlement_due_at = as_utc_datetime(period.settlement_due_at)
        if settlement_due_at is None:
            settlement_due_at = as_utc_datetime(period.period_end) + timedelta(hours=grace_hours)
            period.settlement_due_at = settlement_due_at
        if settlement_due_at > now:
            summary["periods_skipped"] += 1
            continue
        outstanding_units = max(0, int(period.overage_units or 0) - int(period.invoiced_units or 0))
        if outstanding_units <= 0:
            period.status = "included"
            period.settlement_due_at = None
            summary["periods_skipped"] += 1
            continue

        organization = period.organization
        subscription = organization.subscription if organization is not None else None
        if subscription is None or not subscription.stripe_customer_id:
            period.status = "pending_customer"
            summary["periods_skipped"] += 1
            continue
        if subscription_status_is_complimentary(subscription.status):
            period.status = "included"
            period.overage_units = 0
            period.sell_amount = Decimal("0")
            period.settlement_due_at = None
            summary["periods_skipped"] += 1
            continue

        resume_existing_settlement = bool(
            int(period.settlement_version or 0) > 0
            and period.status in {"invoicing", "item_created", "error"}
        )
        if resume_existing_settlement:
            settlement_version = int(period.settlement_version or 0)
        else:
            settlement_version = int(period.settlement_version or 0) + 1
            period.stripe_invoice_id = None
            period.stripe_invoice_item_id = None
        period.settlement_version = settlement_version
        period.status = "invoicing"
        db.session.commit()

        period = db.session.get(OrganizationUsageBillingPeriod, period.id)
        if period is None:
            raise RuntimeError("Usage billing period disappeared during settlement.")
        organization = period.organization
        subscription = organization.subscription if organization is not None else None
        if subscription is None or not subscription.stripe_customer_id:
            raise RuntimeError("Stripe customer disappeared during usage settlement.")
        invoice_key = _usage_invoice_idempotency_key(period, settlement_version)
        item_key = _usage_invoice_item_idempotency_key(period, settlement_version)
        rate = Decimal(str(current_app.config.get("BILLING_OUTBOUND_SEGMENT_RATE_USD") or "0.0300"))
        intended_settlement_units = outstanding_units
        period_id = period.id
        period_organization_id = period.organization_id
        period_start = period.period_start
        try:
            metadata = {
                "organization_id": str(period.organization_id),
                "period_start": period.period_start.date().isoformat(),
                "period_end": period.period_end.date().isoformat(),
                "overage_units": str(intended_settlement_units),
                "settlement_version": str(settlement_version),
            }
            invoice_was_already_persisted = bool(period.stripe_invoice_id)
            if invoice_was_already_persisted:
                invoice_id = str(period.stripe_invoice_id or "").strip()
                invoice = _retry_stripe_operation(
                    f"invoice.retrieve:{invoice_id}",
                    lambda: stripe.Invoice.retrieve(invoice_id),
                )
            else:
                invoice = _retry_stripe_operation(
                    f"invoice.create:{invoice_key}",
                    lambda: stripe.Invoice.create(
                        customer=subscription.stripe_customer_id,
                        collection_method="charge_automatically",
                        auto_advance=False,
                        description="Twinevia monthly outbound SMS overage",
                        metadata=metadata,
                        idempotency_key=invoice_key,
                    ),
                )
                invoice_id = str(
                    _stripe_object_to_dict(invoice).get("id")
                    or getattr(invoice, "id", "")
                ).strip()
            if not invoice_id:
                raise RuntimeError("Stripe did not return an invoice id for SMS overage settlement.")
            invoice_data = _stripe_object_to_dict(invoice)
            invoice_metadata = (
                invoice_data.get("metadata")
                if isinstance(invoice_data.get("metadata"), dict)
                else {}
            )
            if invoice_was_already_persisted:
                if str(invoice_metadata.get("organization_id") or "") != str(period.organization_id):
                    raise RuntimeError("Persisted Stripe overage invoice organization metadata is invalid.")
                if str(invoice_metadata.get("settlement_version") or "") != str(settlement_version):
                    raise RuntimeError("Persisted Stripe overage invoice settlement version is invalid.")
            try:
                settlement_units = int(
                    invoice_metadata.get("overage_units")
                    or intended_settlement_units
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Stripe overage invoice has invalid unit metadata.") from exc
            if settlement_units <= 0 or settlement_units > outstanding_units:
                raise RuntimeError("Stripe overage invoice unit metadata exceeds outstanding usage.")
            settlement_amount_cents = _decimal_to_cents(Decimal(settlement_units) * rate)

            period.stripe_invoice_id = invoice_id
            period.status = "invoicing"
            db.session.commit()

            if not period.stripe_invoice_item_id:
                invoice_item = _retry_stripe_operation(
                    f"invoice_item.create:{item_key}",
                    lambda: stripe.InvoiceItem.create(
                        customer=subscription.stripe_customer_id,
                        invoice=invoice_id,
                        currency=period.currency or "usd",
                        amount=settlement_amount_cents,
                        description=(
                            f"{settlement_units} outbound SMS segment overage"
                            f" ({period.period_start.date()} to {(period.period_end - timedelta(seconds=1)).date()})"
                        ),
                        metadata=metadata,
                        idempotency_key=item_key,
                    ),
                )
                invoice_item_data = _stripe_object_to_dict(invoice_item)
                invoice_item_id = str(
                    invoice_item_data.get("id") or getattr(invoice_item, "id", "")
                ).strip()
                if not invoice_item_id:
                    raise RuntimeError("Stripe did not return an invoice item id for SMS overage settlement.")
                period.stripe_invoice_item_id = invoice_item_id
                period.status = "item_created"
                db.session.commit()

            invoice_status = str(invoice_data.get("status") or "").strip().lower()
            if invoice_status in {"void", "uncollectible"}:
                raise RuntimeError(
                    f"Stripe overage invoice {invoice_id} is {invoice_status} and cannot be finalized."
                )
            if invoice_status not in {"open", "paid"}:
                finalized_invoice = _retry_stripe_operation(
                    f"invoice.finalize:{invoice_key}",
                    lambda: stripe.Invoice.finalize_invoice(
                        invoice_id,
                        auto_advance=True,
                        idempotency_key=f"{invoice_key}:finalize",
                    ),
                )
                finalized_data = _stripe_object_to_dict(finalized_invoice)
                finalized_status = str(finalized_data.get("status") or "").strip().lower()
                if (
                    current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED")
                    and finalized_status not in {"open", "paid"}
                ):
                    raise RuntimeError(
                        f"Stripe overage invoice {invoice_id} did not reach an automatically collectible state."
                    )
                if (
                    current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED")
                    and finalized_data.get("livemode") is not True
                ):
                    raise RuntimeError(f"Stripe overage invoice {invoice_id} is not live mode.")

            period.invoiced_units = int(period.invoiced_units or 0) + settlement_units
            period.settlement_due_at = None
            period.status = "posted"
            period.posted_at = utc_now()
            summary["periods_posted"] += 1
        except Exception:
            db.session.rollback()
            summary["periods_failed"] += 1
            current_app.logger.exception(
                "Failed posting SMS overage invoice item for organization_id=%s period_start=%s.",
                period_organization_id,
                period_start,
            )
            period = db.session.get(OrganizationUsageBillingPeriod, period_id)
            if period is not None:
                period.status = "error"
        db.session.commit()

    return summary


def reconcile_billing_subscriptions() -> dict[str, int]:
    summary = {
        "scanned": 0,
        "updated": 0,
        "unchanged": 0,
        "failed": 0,
        "usage_records_seen": 0,
        "usage_records_finalized": 0,
        "usage_records_pending": 0,
        "usage_records_errored": 0,
        "usage_periods_updated": 0,
        "usage_periods_scanned": 0,
        "usage_periods_posted": 0,
        "usage_periods_skipped": 0,
        "usage_periods_failed": 0,
    }

    subscriptions = OrganizationSubscription.query.all()
    for subscription in subscriptions:
        if subscription_status_is_complimentary(subscription.status):
            continue
        needs_reconcile = (
            not subscription.stripe_customer_id
            or not subscription.stripe_subscription_id
            or (subscription.status or "").strip().lower() in RECONCILEABLE_SUBSCRIPTION_STATUSES
        )
        if not needs_reconcile:
            continue

        summary["scanned"] += 1
        organization = subscription.organization
        before = _subscription_state_snapshot(subscription)
        try:
            owner_email = _owner_email_for_organization(organization) if organization is not None else ""
            refreshed = refresh_subscription_from_stripe(organization, owner_email) if organization is not None else None
            after = _subscription_state_snapshot(refreshed or subscription)
            if after != before:
                summary["updated"] += 1
            else:
                summary["unchanged"] += 1
        except Exception:
            db.session.rollback()
            summary["failed"] += 1
            current_app.logger.exception(
                "Billing reconciliation failed for organization_id=%s subscription_id=%s.",
                subscription.organization_id,
                subscription.id,
            )

    current_app.logger.info(
        "[Billing Reconcile] scanned=%d updated=%d unchanged=%d failed=%d",
        summary["scanned"],
        summary["updated"],
        summary["unchanged"],
        summary["failed"],
    )
    usage_summary = reconcile_messaging_usage()
    summary["usage_records_seen"] = usage_summary["records_seen"]
    summary["usage_records_finalized"] = usage_summary["records_finalized"]
    summary["usage_records_pending"] = usage_summary["records_pending"]
    summary["usage_records_errored"] = usage_summary["records_errored"]
    summary["usage_periods_updated"] = usage_summary["periods_updated"]

    invoice_summary = _post_closed_usage_invoice_items()
    summary["usage_periods_scanned"] = invoice_summary["periods_scanned"]
    summary["usage_periods_posted"] = invoice_summary["periods_posted"]
    summary["usage_periods_skipped"] = invoice_summary["periods_skipped"]
    summary["usage_periods_failed"] = invoice_summary["periods_failed"]

    current_app.logger.info(
        "[Usage Reconcile] records_seen=%d finalized=%d pending=%d errored=%d periods_updated=%d periods_posted=%d",
        summary["usage_records_seen"],
        summary["usage_records_finalized"],
        summary["usage_records_pending"],
        summary["usage_records_errored"],
        summary["usage_periods_updated"],
        summary["usage_periods_posted"],
    )
    return summary
