from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from flask import current_app, url_for
from sqlalchemy.exc import IntegrityError

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
    activation_price_id_for_organization,
    billing_plan_for_code,
    billing_plan_for_price_id,
    billing_plan_options_for_organization,
    recurring_price_id_for_subscription,
    staged_annual_offer_enabled_for_organization,
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
    "invoice.paid",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
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
FAKE_SETUP_CHECKOUT_SESSION_PREFIX = "cs_fake_setup_org_"
REQUIRED_STRIPE_WEBHOOK_EVENTS = frozenset(RELEVANT_STRIPE_EVENT_TYPES - {
    "invoice.paid", "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed", "checkout.session.expired",
})
MANUAL_BILLING_INCIDENTS = {
    "unexpected_subscription", "stripe_ownership_conflict", "stripe_customer_conflict",
    "duplicate_setup_payment", "complimentary_payment_conflict", "retired_checkout_payment",
}


class StaleStripeEventError(RuntimeError):
    """Raised when a verified Stripe event predates applied billing state."""


class StripePaymentProofError(RuntimeError):
    """Raised when Stripe objects do not prove the expected payment."""


class StripeComplimentaryConflictError(RuntimeError):
    """Raised when Stripe reports paid billing for a complimentary organization."""


class BillingTransitionError(RuntimeError):
    """A persisted billing operation must be resolved before another can begin."""


def _billing_utc_now() -> datetime:
    return utc_now().replace(tzinfo=None)


@dataclass(frozen=True)
class StripePriceExpectation:
    config_key: str
    amount_cents: int
    recurring_interval: str | None


LIVE_PRICE_EXPECTATIONS = (
    StripePriceExpectation("STRIPE_ACTIVATION_PRICE_ID", 14999, None),
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


def organization_can_prepare_provider(organization: Organization | None) -> bool:
    if not organization_is_active(organization):
        return False
    if organization_can_send(organization):
        return True
    if organization is None or not staged_annual_offer_enabled_for_organization(organization):
        return False
    return subscription_activation_paid(organization.subscription)


def staged_annual_provider_approval_complete(organization: Organization | None) -> bool:
    if organization is None or not staged_annual_offer_enabled_for_organization(organization):
        return False
    onboarding = organization.a2p_onboarding
    if onboarding is None:
        return False
    onboarding_status = str(onboarding.onboarding_status or "").strip().lower()
    campaign_status = str(onboarding.campaign_status or "").strip().lower()
    return onboarding_status == "approved" or campaign_status in {"approved", "verified"}


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


def _lock_billing_state(organization_id: int) -> tuple[Organization, OrganizationSubscription]:
    from app.tenant import get_current_organization_id, tenant_scope_is_active

    if tenant_scope_is_active() and get_current_organization_id() != organization_id:
        raise StripePaymentProofError("Billing belongs to another organization.")
    with db.session.no_autoflush:
        organization = Organization.query.filter_by(id=organization_id).populate_existing().with_for_update().one()
        subscription = OrganizationSubscription.query.filter_by(
            organization_id=organization_id,
        ).populate_existing().with_for_update().first()
    if subscription is None:
        subscription = OrganizationSubscription(organization=organization, status="incomplete")
        db.session.add(subscription)
        db.session.flush()
    return organization, subscription


def _billing_incident(subscription: OrganizationSubscription, code: str, detail: str, *, exception=BillingTransitionError):
    if subscription.billing_incident_code in MANUAL_BILLING_INCIDENTS:
        existing = subscription.billing_incident_detail or ""
        if code in MANUAL_BILLING_INCIDENTS and detail not in existing:
            subscription.billing_incident_detail = (existing + "\n" + detail).strip()[:2000]
    else:
        subscription.billing_incident_code = code
        subscription.billing_incident_detail = detail[:2000]
    db.session.commit()
    if exception is not None:
        raise exception(detail)


def _clear_billing_incident(subscription: OrganizationSubscription) -> None:
    if subscription.billing_incident_code in MANUAL_BILLING_INCIDENTS:
        return
    subscription.billing_incident_code = None
    subscription.billing_incident_detail = None


def _require_resolved_billing_incident(subscription: OrganizationSubscription) -> None:
    if subscription.billing_incident_code in MANUAL_BILLING_INCIDENTS:
        raise BillingTransitionError(subscription.billing_incident_detail or "Platform billing review is required before another billing change.")


def _saved_checkout_payload(subscription: OrganizationSubscription) -> dict:
    try:
        payload = json.loads(subscription.checkout_request_payload_json or "{}")
    except (TypeError, ValueError) as exc:
        _billing_incident(subscription, "checkout_request_invalid", "The saved checkout needs platform review before another payment can start.")
        raise exc  # pragma: no cover
    return payload if isinstance(payload, dict) else {}


def _checkout_intent(payload: dict) -> tuple:
    return tuple(payload.get(name) for name in ("plan_code", "recurring_price_id", "setup_price_id", "offer_version"))


def _assert_no_existing_stripe_subscription(organization: Organization, subscription: OrganizationSubscription) -> None:
    if subscription_status_is_complimentary(subscription.status):
        raise BillingTransitionError("Complimentary billing is already active for this organization.")
    if fake_checkout_enabled():
        if subscription.stripe_subscription_id or subscription.status in {"active", "trialing"}:
            _billing_incident(subscription, "subscription_already_exists", "An existing subscription must be managed before starting another checkout.")
        return
    if subscription.stripe_subscription_id:
        stripe = _stripe_module()
        try:
            remote = _stripe_object_to_dict(_retry_stripe_operation(
                "subscription.retrieve", lambda: stripe.Subscription.retrieve(subscription.stripe_subscription_id),
            ))
        except Exception:
            _billing_incident(subscription, "subscription_unconfirmed", "The existing subscription could not be confirmed. Refresh billing before starting a new checkout.")
        if str(remote.get("id") or "") != subscription.stripe_subscription_id or str(remote.get("status") or "") not in {"canceled", "incomplete_expired"}:
            _billing_incident(subscription, "subscription_already_exists", "An existing Stripe subscription must be managed or reconciled before starting another checkout.")
    elif subscription.stripe_customer_id:
        stripe = _stripe_module()
        try:
            page = stripe.Subscription.list(customer=subscription.stripe_customer_id, status="all", limit=100)
            subscriptions = [_stripe_object_to_dict(row) for row in _stripe_collection_data(page)]
        except Exception:
            _billing_incident(subscription, "subscription_unconfirmed", "Stripe subscriptions could not be confirmed. Refresh billing before starting a new checkout.")
        chargeable = [row for row in subscriptions if str(row.get("status") or "") not in {"canceled", "incomplete_expired"}]
        if chargeable or _stripe_collection_has_more(page):
            _billing_incident(subscription, "unexpected_subscription", "Stripe has a subscription that is not reconciled with this workspace. Platform billing review is required; no replacement checkout was created.")


def mark_subscription_complimentary(organization: Organization) -> OrganizationSubscription:
    organization, subscription = _lock_billing_state(organization.id)
    _require_resolved_billing_incident(subscription)
    if subscription_status_is_complimentary(subscription.status):
        return subscription
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
    _close_or_reuse_checkouts(organization, subscription)
    organization, subscription = _lock_billing_state(organization.id)
    if subscription.stripe_customer_id or subscription.stripe_subscription_id or subscription_activation_paid(subscription):
        _billing_incident(subscription, "complimentary_payment_conflict", "A payment was recorded before complimentary billing could be enabled. Reconcile it before changing billing.")
    subscription.status = COMPLIMENTARY_SUBSCRIPTION_STATUS
    subscription.current_period_end = None
    subscription.cancel_at_period_end = False
    if not subscription.stripe_price_id:
        subscription.stripe_price_id = current_app.config.get("STRIPE_MONTHLY_PRICE_ID") or current_app.config.get("STRIPE_PRICE_ID")
    _clear_billing_incident(subscription)
    db.session.commit()
    return subscription


def clear_complimentary_subscription(organization: Organization) -> OrganizationSubscription:
    organization, subscription = _lock_billing_state(organization.id)
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
    return fake_checkout_enabled() and normalized.startswith(
        (FAKE_CHECKOUT_SESSION_PREFIX, FAKE_SETUP_CHECKOUT_SESSION_PREFIX)
    )


def _fake_checkout_session_id(organization: Organization) -> str:
    return f"{FAKE_CHECKOUT_SESSION_PREFIX}{organization.id}"


def _fake_setup_checkout_session_id(organization: Organization) -> str:
    return f"{FAKE_SETUP_CHECKOUT_SESSION_PREFIX}{organization.id}"


def _fake_checkout_organization_id(session_id: str) -> int:
    normalized = (session_id or "").strip()
    matched_prefix = next(
        (
            prefix
            for prefix in (FAKE_SETUP_CHECKOUT_SESSION_PREFIX, FAKE_CHECKOUT_SESSION_PREFIX)
            if normalized.startswith(prefix)
        ),
        None,
    )
    if matched_prefix is None:
        raise RuntimeError("Unsupported fake checkout session id.")
    try:
        return int(normalized[len(matched_prefix):].split("_", 1)[0])
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

    target_organization, subscription = _lock_billing_state(target_organization.id)
    if subscription_status_is_complimentary(subscription.status):
        raise BillingTransitionError("Complimentary billing is already active for this organization.")
    checkout_record = StripeCheckoutSession.query.filter_by(
        stripe_checkout_session_id=session_id,
        organization_id=target_organization.id,
    ).first()
    if checkout_record is not None and (
        checkout_record.status in {"expired", "expiry_unknown"}
        or checkout_record.offer_version != _organization_offer_version(target_organization)
    ):
        raise BillingTransitionError("This checkout was superseded. Return to billing for the current state.")
    if (
        checkout_record is None
        and session_id.startswith(FAKE_SETUP_CHECKOUT_SESSION_PREFIX)
    ):
        raise RuntimeError("Fake checkout session was not issued by Twinevia.")
    if checkout_record is not None and checkout_record.billing_plan_code == "setup_only":
        subscription.status = "incomplete"
        subscription.stripe_customer_id = f"cus_fake_org_{target_organization.id}"
        subscription.activation_fee_paid_at = subscription.activation_fee_paid_at or _billing_utc_now()
        subscription.activation_price_id = checkout_record.activation_price_id
        subscription.activation_payment_intent_id = f"pi_fake_setup_org_{target_organization.id}"
        subscription.activation_invoice_id = None
        checkout_record.status = "completed"
        checkout_record.completed_at = _billing_utc_now()
        checkout_record.stripe_customer_id = subscription.stripe_customer_id
        subscription.checkout_request_status = "completed"
        _clear_billing_incident(subscription)
        db.session.commit()
        return subscription

    fake_period_end = _fake_checkout_period_end()
    subscription.status = "trialing" if fake_period_end is not None else "active"
    subscription.current_period_end = fake_period_end
    subscription.cancel_at_period_end = False
    if not subscription.stripe_price_id:
        subscription.stripe_price_id = current_app.config.get("STRIPE_MONTHLY_PRICE_ID") or current_app.config.get("STRIPE_PRICE_ID")
    if subscription.activation_fee_paid_at is None:
        subscription.activation_fee_paid_at = _billing_utc_now()
        subscription.activation_price_id = current_app.config.get("STRIPE_ACTIVATION_PRICE_ID")
        subscription.activation_payment_intent_id = f"pi_fake_org_{target_organization.id}"
        subscription.activation_invoice_id = f"in_fake_org_{target_organization.id}"
    if checkout_record is not None:
        checkout_record.status = "completed"
        checkout_record.completed_at = checkout_record.completed_at or _billing_utc_now()
        checkout_record.stripe_customer_id = f"cus_fake_org_{target_organization.id}"
        checkout_record.stripe_subscription_id = f"sub_fake_org_{target_organization.id}"
    subscription.checkout_request_status = "completed"
    _clear_billing_incident(subscription)
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
    client = stripe.StripeClient(secret_key)

    def resource(service, *, retrieve="retrieve", extra=()):
        # Preserve this service's established injectable boundary while using
        # client-owned credentials, never the SDK's process-global api_key.
        operations = {}
        for public_name in ("create", "retrieve", "list", *extra):
            method_name = retrieve if public_name == "retrieve" else public_name
            method = getattr(service, method_name, None)
            if method is None:
                continue

            def invoke(*args, _method=method, **params):
                options = {}
                if "idempotency_key" in params:
                    options["idempotency_key"] = params.pop("idempotency_key")
                return _method(*args, params=params, options=options)

            operations[public_name] = invoke
        return SimpleNamespace(**operations)

    return SimpleNamespace(
        Account=resource(client.accounts, retrieve="retrieve_current"),
        Price=resource(client.prices), WebhookEndpoint=resource(client.webhook_endpoints),
        Subscription=resource(client.subscriptions), PaymentIntent=resource(client.payment_intents),
        Invoice=resource(client.invoices, extra=("finalize_invoice",)),
        InvoiceItem=resource(client.invoice_items),
        checkout=SimpleNamespace(Session=resource(client.checkout.sessions, extra=("expire", "list_line_items"))),
        billing_portal=SimpleNamespace(
            Configuration=resource(client.billing_portal.configurations),
            Session=resource(client.billing_portal.sessions),
        ),
    )


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
            ).first()
            if issued is None and str(metadata.get("billing_transition_key") or "") == subscription.checkout_request_key:
                issued = _checkout_session_record(str(data_object.get("id") or ""), organization, data_object)
            if issued is None or (issued.offer_version != _organization_offer_version(organization) and issued.status != "completed"):
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
    organization, subscription = _lock_billing_state(organization.id)
    _, expired_count = _close_or_reuse_checkouts(organization, subscription, keep_offer_version=target_offer_version)
    db.session.commit()
    return expired_count


def update_organization_billing_offer(
    organization: Organization,
    billing_offer: str,
) -> OrganizationSubscription:
    normalized_offer = str(billing_offer or "").strip().lower()
    if normalized_offer not in {"standard", "annual_only", "staged_annual"}:
        raise RuntimeError("Choose a valid checkout offer.")
    target_version = (
        f"{str(current_app.config.get('BILLING_OFFER_VERSION') or '').strip()}:{normalized_offer}"
    )
    if target_version.startswith(":"):
        raise RuntimeError("BILLING_OFFER_VERSION is not configured.")
    organization, subscription = _lock_billing_state(organization.id)
    _require_resolved_billing_incident(subscription)
    _close_or_reuse_checkouts(organization, subscription, keep_offer_version=target_version)
    organization.billing_offer = normalized_offer
    organization.billing_offer_version = target_version
    if not subscription_activation_paid(subscription):
        plan_code = "annual" if normalized_offer in {"annual_only", "staged_annual"} else "monthly"
        plan = billing_plan_for_code(plan_code)
        if plan is not None:
            subscription.stripe_price_id = plan.price_id
    subscription.offer_version = target_version
    _clear_billing_incident(subscription)
    db.session.commit()
    return subscription


def _checkout_session_record(
    session_id: str,
    organization: Organization,
    data_object: dict | None = None,
) -> StripeCheckoutSession:
    record = StripeCheckoutSession.query.filter_by(
        stripe_checkout_session_id=session_id,
    ).first()
    if record is None and data_object is not None:
        subscription = organization.subscription
        transition_key = str((data_object.get("metadata") or {}).get("billing_transition_key") or "")
        if transition_key and transition_key == subscription.checkout_request_key:
            payload = _saved_checkout_payload(subscription)
            record = StripeCheckoutSession(
                organization_id=organization.id, stripe_checkout_session_id=session_id,
                billing_plan_code=payload["plan_code"], recurring_price_id=payload["recurring_price_id"],
                activation_price_id=payload["setup_price_id"], offer_version=payload["offer_version"],
                transition_key=transition_key, status="open",
            )
            db.session.add(record)
            subscription.checkout_request_session_id = session_id
            db.session.flush()
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
    transition_key: str | None = None,
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
            transition_key=transition_key,
            status="open",
        )
        db.session.add(record)
    else:
        if record.organization_id != organization.id:
            raise RuntimeError("Stripe Checkout session id is already bound to another organization.")
        if (record.billing_plan_code, record.recurring_price_id, record.activation_price_id, record.offer_version) != (plan_code, recurring_price_id, setup_price_id, offer_version):
            raise StripePaymentProofError("Stripe Checkout returned an identity already bound to different payment terms.")
        if transition_key and record.transition_key not in {None, transition_key}:
            raise StripePaymentProofError("Stripe Checkout returned an identity owned by another transition.")
        record.transition_key = record.transition_key or transition_key
    organization.billing_offer_version = offer_version
    subscription = organization.subscription
    subscription.offer_version = offer_version
    if transition_key and subscription.checkout_request_key == transition_key:
        subscription.checkout_request_session_id = session_id
        subscription.checkout_request_status = "completed" if record.status == "completed" else "open"
    db.session.commit()
    return record


def _execute_saved_checkout(organization: Organization, subscription: OrganizationSubscription):
    """Replay a committed request, including its original URLs and price terms."""
    payload = _saved_checkout_payload(subscription)
    request_key = subscription.checkout_request_key
    if not request_key or not isinstance(payload.get("params"), dict):
        _billing_incident(subscription, "checkout_request_invalid", "The saved checkout needs platform review before another payment can start.")
    started = as_utc_datetime(subscription.checkout_request_started_at)
    if started is None or utc_now() - started >= timedelta(hours=23):
        _billing_incident(subscription, "checkout_creation_unconfirmed", "The earlier checkout outcome is still unknown and its safe retry window has ended. Platform billing review is required before another payment can start.")
    try:
        if fake_checkout_enabled():
            prefix = FAKE_SETUP_CHECKOUT_SESSION_PREFIX if payload["plan_code"] == "setup_only" else FAKE_CHECKOUT_SESSION_PREFIX
            session_id = f"{prefix}{organization.id}_{request_key[-12:]}"
            session = SimpleNamespace(id=session_id, status="open", url=_fake_checkout_url(
                organization, session_id=session_id, success_url=payload["params"]["success_url"],
                cancel_url=payload["params"]["cancel_url"],
            ))
        else:
            stripe = _stripe_module()
            session = _retry_stripe_operation(
                "checkout.session.create", lambda: stripe.checkout.Session.create(
                    **payload["params"], idempotency_key=request_key,
                ),
            )
    except Exception as exc:
        subscription.checkout_request_status = "unknown"
        _billing_incident(subscription, "checkout_creation_unconfirmed", "The checkout request is saved, but Stripe confirmation was lost. Resume this saved checkout; do not start a replacement payment.")
        raise exc  # pragma: no cover
    try:
        _record_issued_checkout_session(
            session, organization, payload["plan_code"], payload["recurring_price_id"],
            payload["setup_price_id"], payload["offer_version"], request_key,
        )
    except Exception:
        db.session.rollback()
        organization, subscription = _lock_billing_state(organization.id)
        if subscription.checkout_request_key != request_key or subscription.checkout_request_status == "completed":
            raise BillingTransitionError("Billing was reconciled while the checkout response was being recorded. Reload billing before continuing.")
        subscription.checkout_request_status = "unknown"
        _billing_incident(subscription, "checkout_creation_unconfirmed", "Stripe may have created the saved checkout, but its result could not be recorded. Resume the saved checkout before starting another payment.")
    return session


def _checkout_record_snapshot(organization: Organization, subscription: OrganizationSubscription, record: StripeCheckoutSession):
    if is_fake_checkout_session_id(record.stripe_checkout_session_id):
        params = _saved_checkout_payload(subscription).get("params", {})
        cancel_url = params.get("cancel_url") or url_for("main.setup", step="billing", _external=True)
        success_url = params.get("success_url") or cancel_url + "&session_id={CHECKOUT_SESSION_ID}"
        return SimpleNamespace(
            id=record.stripe_checkout_session_id, status="expired" if record.status == "expired" else "open",
            url=_fake_checkout_url(organization, session_id=record.stripe_checkout_session_id,
                                  success_url=success_url, cancel_url=cancel_url),
        )
    stripe = _stripe_module()
    try:
        remote = _retry_stripe_operation(
            "checkout.session.retrieve", lambda: stripe.checkout.Session.retrieve(record.stripe_checkout_session_id),
        )
    except Exception:
        _billing_incident(subscription, "checkout_state_unconfirmed", "The existing checkout could not be confirmed. Refresh billing before switching plans or granting complimentary access.")
    data = _stripe_object_to_dict(remote)
    if str(data.get("id") or "") != record.stripe_checkout_session_id:
        _billing_incident(subscription, "checkout_identity_conflict", "Stripe returned a different checkout identity. Platform billing review is required.")
    remote_organization_id = _extract_organization_id(data)
    if remote_organization_id is not None and remote_organization_id != organization.id:
        _billing_incident(subscription, "checkout_identity_conflict", "The checkout ownership did not match this workspace. Platform billing review is required.")
    return remote


def _inspect_checkout_state(organization: Organization, subscription: OrganizationSubscription, record: StripeCheckoutSession, remote) -> str:
    data = _stripe_object_to_dict(remote)
    state = str(data.get("status") or "").strip().lower()
    if state == "expired":
        record.status = "expired"
        record.expired_at = record.expired_at or _billing_utc_now()
        if subscription.checkout_request_session_id == record.stripe_checkout_session_id:
            subscription.checkout_request_status = "expired"
        return state
    if state == "complete":
        if str(data.get("payment_status") or "").lower() not in {"paid", "no_payment_required"}:
            record.status = "payment_pending"
            if subscription.checkout_request_session_id == record.stripe_checkout_session_id:
                subscription.checkout_request_status = "payment_pending"
            _billing_incident(subscription, "checkout_payment_pending", "An existing checkout is complete and payment confirmation is pending. Refresh billing before another billing change.")
        # Payment evidence and the authoritative subscription must be projected
        # before any competing payable path or complimentary state is offered.
        sync_checkout_session_by_id(record.stripe_checkout_session_id, organization)
        raise BillingTransitionError("The existing checkout payment was reconciled. Reload billing before starting another billing change.")
    if state != "open":
        _billing_incident(subscription, "checkout_state_unconfirmed", "The existing checkout state is unknown. Platform billing review is required before another payment can start.")
    return state


def _expire_checkout_record(organization: Organization, subscription: OrganizationSubscription, record: StripeCheckoutSession) -> None:
    if is_fake_checkout_session_id(record.stripe_checkout_session_id):
        record.status = "expired"
        record.expired_at = _billing_utc_now()
        return
    stripe = _stripe_module()
    remote = None
    try:
        remote = _retry_stripe_operation(
            "checkout.session.expire", lambda: stripe.checkout.Session.expire(
                record.stripe_checkout_session_id,
                idempotency_key="twinevia-expire-" + record.stripe_checkout_session_id,
            ),
        )
    except Exception:
        pass  # The expiration may have succeeded despite losing its response.
    data = _stripe_object_to_dict(remote) if remote is not None else {}
    if data.get("id") != record.stripe_checkout_session_id or data.get("status") != "expired":
        remote = _checkout_record_snapshot(organization, subscription, record)
        state = _inspect_checkout_state(organization, subscription, record, remote)
        if state != "expired":
            record.status = "expiry_unknown"
            _billing_incident(subscription, "checkout_expiry_unconfirmed", "The previous checkout is still payable or its expiration is unconfirmed. Billing was not changed; retry after reconciliation.")
    record.status = "expired"
    record.expired_at = record.expired_at or _billing_utc_now()
    if subscription.checkout_request_session_id == record.stripe_checkout_session_id:
        subscription.checkout_request_status = "expired"


def _close_or_reuse_checkouts(organization: Organization, subscription: OrganizationSubscription, *, desired: dict | None = None, keep_offer_version: str | None = None):
    """The organization lock owns every path capable of leaving a payable URL."""
    if subscription.checkout_request_status in {"creating", "unknown"} and not subscription.checkout_request_session_id:
        _execute_saved_checkout(organization, subscription)
        organization, subscription = _lock_billing_state(organization.id)
    records = StripeCheckoutSession.query.filter_by(organization_id=organization.id).filter(
        StripeCheckoutSession.status.in_(["open", "expiry_unknown", "payment_pending"]),
    ).order_by(StripeCheckoutSession.id.desc()).all()
    retained = None
    closed = 0
    for record in records:
        remote = _checkout_record_snapshot(organization, subscription, record)
        if _inspect_checkout_state(organization, subscription, record, remote) == "expired":
            continue
        compatible = desired is not None and (
            record.billing_plan_code, record.recurring_price_id, record.activation_price_id, record.offer_version
        ) == _checkout_intent(desired)
        keep_offer = keep_offer_version is not None and record.offer_version == keep_offer_version
        if retained is None and (compatible or keep_offer):
            retained = remote
        else:
            _expire_checkout_record(organization, subscription, record)
            closed += 1
    if retained is not None and desired is not None:
        data = _stripe_object_to_dict(retained)
        if not str(data.get("url") or getattr(retained, "url", "") or "").strip():
            _billing_incident(subscription, "checkout_url_unconfirmed", "The saved checkout is still open but its payment link is unavailable. Platform billing review is required.")
        _clear_billing_incident(subscription)
    return retained, closed


def _reserve_checkout(organization: Organization, subscription: OrganizationSubscription, payload: dict):
    if subscription_status_is_complimentary(subscription.status) or _organization_offer_version(organization) != payload["offer_version"]:
        raise BillingTransitionError("Billing changed while checkout was being prepared. Refresh billing before continuing.")
    request_key = "twinevia-checkout-" + uuid4().hex
    payload["params"]["metadata"]["billing_transition_key"] = request_key
    for nested in ("subscription_data", "payment_intent_data"):
        if nested in payload["params"]:
            payload["params"][nested]["metadata"]["billing_transition_key"] = request_key
    subscription.checkout_request_key = request_key
    subscription.checkout_request_payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    subscription.checkout_request_started_at = _billing_utc_now()
    subscription.checkout_request_status = "creating"
    subscription.checkout_request_session_id = None
    subscription.stripe_price_id = payload["recurring_price_id"]
    organization.billing_offer_version = payload["offer_version"]
    subscription.offer_version = payload["offer_version"]
    _clear_billing_incident(subscription)
    # The provider can accept a request even if this process never sees a reply.
    db.session.commit()
    organization, subscription = _lock_billing_state(organization.id)
    if subscription.checkout_request_key != request_key:
        _billing_incident(subscription, "checkout_ownership_conflict", "Billing changed while checkout was being prepared. Refresh billing before continuing.")
    if subscription_status_is_complimentary(subscription.status):
        raise BillingTransitionError("Complimentary billing is already active for this organization.")
    if subscription.checkout_request_session_id:
        record = _checkout_session_record(subscription.checkout_request_session_id, organization)
        remote = _checkout_record_snapshot(organization, subscription, record)
        _inspect_checkout_state(organization, subscription, record, remote)
        db.session.commit()
        return remote
    return _execute_saved_checkout(organization, subscription)


def create_checkout_session(
    organization: Organization,
    user_email: str,
    success_url: str,
    cancel_url: str,
    *,
    plan_code: str | None = None,
):
    organization, subscription = _lock_billing_state(organization.id)
    _require_resolved_billing_incident(subscription)
    if subscription_status_is_complimentary(subscription.status):
        raise BillingTransitionError("Complimentary billing is already active for this organization.")
    eligible_plans = billing_plan_options_for_organization(organization)
    eligible_codes = {plan.code for plan in eligible_plans}
    selected = billing_plan_for_code(plan_code) if plan_code else None
    if plan_code and (selected is None or selected.code not in eligible_codes):
        raise BillingTransitionError("Choose a valid billing option.")
    if selected is None and len(eligible_plans) == 1:
        selected = eligible_plans[0]
    price_id = selected.price_id if selected else recurring_price_id_for_subscription(subscription)
    resolved = selected or billing_plan_for_price_id(price_id)
    if not resolved or resolved.code not in eligible_codes or not price_id:
        raise BillingTransitionError("Choose a valid billing option from the current Twinevia offer.")
    setup_paid = subscription_activation_paid(subscription)
    setup_only = staged_annual_offer_enabled_for_organization(organization) and not setup_paid
    if staged_annual_offer_enabled_for_organization(organization) and setup_paid and not staged_annual_provider_approval_complete(organization):
        raise BillingTransitionError("The $600 annual payment becomes available after provider approval.")
    setup_price_id = None if setup_paid else activation_price_id_for_organization(organization)
    if not setup_paid and not setup_price_id:
        raise BillingTransitionError("The Stripe setup price is not configured.")
    offer_version = _organization_offer_version(organization)
    actual_plan_code = "setup_only" if setup_only else resolved.code
    metadata = {
        "organization_id": str(organization.id), "billing_plan_code": actual_plan_code,
        "billing_offer_version": offer_version,
    }
    params = {
        "mode": "payment" if setup_only else "subscription",
        "line_items": ([{"price": setup_price_id, "quantity": 1}] if setup_price_id else [])
                      + ([] if setup_only else [{"price": price_id, "quantity": 1}]),
        "success_url": success_url, "cancel_url": cancel_url,
        "client_reference_id": str(organization.id), "metadata": dict(metadata),
    }
    if setup_only:
        params["metadata"]["billing_checkout_kind"] = "setup_only"
        params["payment_intent_data"] = {"metadata": dict(params["metadata"])}
        if not subscription.stripe_customer_id:
            params["customer_creation"] = "always"
    else:
        params["subscription_data"] = {"metadata": dict(metadata)}
        trial_days = int(current_app.config.get("BILLING_TRIAL_DAYS", 0) or 0)
        if trial_days > 0 and not subscription.stripe_subscription_id:
            params["subscription_data"]["trial_period_days"] = trial_days
    if subscription.stripe_customer_id:
        params["customer"] = subscription.stripe_customer_id
    elif user_email:
        params["customer_email"] = user_email
    payload = {
        "plan_code": actual_plan_code, "recurring_price_id": price_id, "setup_price_id": setup_price_id,
        "offer_version": offer_version, "params": params,
    }
    existing, _ = _close_or_reuse_checkouts(organization, subscription, desired=payload)
    if existing is not None:
        db.session.commit()
        return existing
    _assert_no_existing_stripe_subscription(organization, subscription)
    return _reserve_checkout(organization, subscription, payload)


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
    *,
    minimum_amount_cents: int | None = None,
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
    expected_amount_cents = minimum_amount_cents if minimum_amount_cents is not None else _decimal_to_cents(
        current_app.config.get("BILLING_ACTIVATION_FEE_USD") or "149.99"
    )
    if int(invoice.get("amount_paid") or 0) < expected_amount_cents:
        raise StripePaymentProofError(
            f"Stripe invoice {invoice_id} does not prove the $149.99 setup payment."
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
    if int(payment_intent.get("amount_received") or 0) < expected_amount_cents:
        raise StripePaymentProofError(
            f"Stripe PaymentIntent {payment_intent_id} does not prove the $149.99 setup payment."
        )
    return payment_intent_id, invoice


def _verified_staged_setup_payment_intent(
    stripe: Any,
    checkout_session: dict[str, Any],
) -> str:
    session_id = str(checkout_session.get("id") or "").strip()
    expected_amount_cents = int(
        (
            Decimal(
                str(
                    current_app.config.get("BILLING_ACTIVATION_FEE_USD")
                    or "149.99"
                )
            )
            * Decimal("100")
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    if str(checkout_session.get("currency") or "").strip().lower() != "usd":
        raise StripePaymentProofError(
            f"Checkout session {session_id} setup payment is not in USD."
        )
    if int(checkout_session.get("amount_total") or -1) != expected_amount_cents:
        raise StripePaymentProofError(
            f"Checkout session {session_id} does not prove the expected setup amount."
        )

    raw_payment_intent = checkout_session.get("payment_intent")
    if isinstance(raw_payment_intent, dict):
        payment_intent = raw_payment_intent
    else:
        payment_intent_id = str(raw_payment_intent or "").strip()
        if not payment_intent_id:
            raise StripePaymentProofError(
                f"Checkout session {session_id} does not reference a PaymentIntent."
            )
        payment_intent = _stripe_object_to_dict(
            _retry_stripe_operation(
                f"payment_intent.retrieve:{payment_intent_id}",
                lambda: stripe.PaymentIntent.retrieve(payment_intent_id),
            )
        )

    payment_intent_id = str(payment_intent.get("id") or "").strip()
    if not payment_intent_id:
        raise StripePaymentProofError(
            f"Checkout session {session_id} has an invalid PaymentIntent."
        )
    if str(payment_intent.get("status") or "").strip().lower() != "succeeded":
        raise StripePaymentProofError(
            f"Stripe PaymentIntent {payment_intent_id} has not succeeded."
        )
    if str(payment_intent.get("currency") or "").strip().lower() != "usd":
        raise StripePaymentProofError(
            f"Stripe PaymentIntent {payment_intent_id} is not in USD."
        )
    if int(payment_intent.get("amount_received") or -1) != expected_amount_cents:
        raise StripePaymentProofError(
            f"Stripe PaymentIntent {payment_intent_id} does not prove the expected setup amount."
        )
    if (
        current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED")
        and payment_intent.get("livemode") is not True
    ):
        raise StripePaymentProofError(
            f"Stripe PaymentIntent {payment_intent_id} is not live mode."
        )
    return payment_intent_id


def _verify_checkout_session_payment(
    session_id: str,
    *,
    allow_confirmed_payment_proof: bool = False,
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
    organization, subscription = _lock_billing_state(organization.id)
    record = _checkout_session_record(session_id, organization, data_object)
    current_offer_version = _organization_offer_version(organization)
    metadata = data_object.get("metadata") or {}
    if record.offer_version != current_offer_version and record.status != "completed":
        _billing_incident(subscription, "retired_checkout_payment", f"Checkout session {session_id} belongs to expired offer {record.offer_version}. Platform review is required before changing billing.", exception=StripePaymentProofError)
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
    pending_session_flag = str(data_object.get("payment_status") or "").lower() != "paid"
    if pending_session_flag and not allow_confirmed_payment_proof:
        raise StripePaymentProofError(f"Checkout session {session_id} is not paid.")
    if subscription_status_is_complimentary(subscription.status):
        _billing_incident(subscription, "complimentary_payment_conflict", f"Stripe reported chargeable billing for complimentary organization {organization.id}: checkout_session_id={session_id}. Platform reconciliation is required.", exception=StripeComplimentaryConflictError)
    remote_customer = str(data_object.get("customer") or "").strip()
    remote_subscription = str(data_object.get("subscription") or "").strip()
    if subscription.stripe_customer_id and remote_customer != subscription.stripe_customer_id:
        _billing_incident(subscription, "stripe_customer_conflict", "The paid checkout customer differs from the workspace billing owner. Platform reconciliation is required.", exception=StripePaymentProofError)
    if subscription.stripe_subscription_id and remote_subscription and remote_subscription != subscription.stripe_subscription_id and subscription.status not in {"canceled", "incomplete_expired"}:
        if record.status == "completed" and record.stripe_subscription_id == remote_subscription:
            prior = _stripe_object_to_dict(_retry_stripe_operation("subscription.retrieve", lambda: stripe.Subscription.retrieve(remote_subscription)))
            if prior.get("id") == remote_subscription and prior.get("status") in {"canceled", "incomplete_expired"} and str(prior.get("customer") or "") == remote_customer:
                raise StaleStripeEventError("A completed checkout for an inactive prior subscription cannot replace current billing.")
        _billing_incident(subscription, "unexpected_subscription", "The paid checkout reports another Stripe subscription. The existing subscription was preserved; platform reconciliation is required.", exception=StripePaymentProofError)

    actual_quantities = _checkout_line_item_quantities(stripe, session_id)
    if record.billing_plan_code == "setup_only":
        if str(metadata.get("billing_checkout_kind") or "") != "setup_only":
            raise StripePaymentProofError(
                f"Checkout session {session_id} setup metadata does not match its issuance record."
            )
        if not record.activation_price_id:
            raise StripePaymentProofError(
                f"Checkout session {session_id} does not identify its setup price."
            )
        expected_quantities = {record.activation_price_id: 1}
    else:
        expected_quantities = {record.recurring_price_id: 1}
        if record.activation_price_id:
            expected_quantities[record.activation_price_id] = 1
    if actual_quantities != expected_quantities:
        raise StripePaymentProofError(
            f"Checkout session {session_id} line items do not match the issued offer."
        )

    if record.billing_plan_code == "setup_only":
        customer_id = str(data_object.get("customer") or "").strip()
        if not customer_id:
            raise StripePaymentProofError(
                f"Checkout session {session_id} did not create a Stripe customer."
            )
        payment_intent_id = _verified_staged_setup_payment_intent(
            stripe,
            data_object,
        )
        if subscription_activation_paid(subscription) and subscription.activation_payment_intent_id != payment_intent_id:
            _billing_incident(subscription, "duplicate_setup_payment", "Another setup payment was reported after setup was already verified. The original payment proof was preserved; platform review is required.", exception=StripePaymentProofError)
        subscription.stripe_customer_id = customer_id
        subscription.stripe_price_id = record.recurring_price_id
        subscription.status = "incomplete"
        subscription.activation_fee_paid_at = subscription.activation_fee_paid_at or _billing_utc_now()
        subscription.activation_price_id = record.activation_price_id
        subscription.activation_payment_intent_id = payment_intent_id
        subscription.activation_invoice_id = None
        subscription.offer_version = record.offer_version
        record.status = "completed"
        record.completed_at = record.completed_at or _billing_utc_now()
        record.stripe_customer_id = customer_id
        record.stripe_subscription_id = None
        if subscription.checkout_request_session_id == session_id:
            subscription.checkout_request_status = "completed"
        _clear_billing_incident(subscription)
        db.session.commit()
        return subscription, data_object, record

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
        if subscription_activation_paid(subscription) and subscription.activation_payment_intent_id != payment_intent_id:
            record.status = "payment_conflict"
            record.stripe_customer_id = remote_customer or None
            record.stripe_subscription_id = remote_subscription or None
            _billing_incident(subscription, "duplicate_setup_payment", "Another setup payment was reported after setup was already verified. The original payment proof was preserved; platform review is required.", exception=StripePaymentProofError)
        subscription.activation_fee_paid_at = subscription.activation_fee_paid_at or _billing_utc_now()
        subscription.activation_price_id = record.activation_price_id
        subscription.activation_payment_intent_id = payment_intent_id
        subscription.activation_invoice_id = invoice_id
    elif pending_session_flag:
        invoice_id = str(data_object.get("invoice") or "").strip()
        if not invoice_id:
            raise StripePaymentProofError("The recurring checkout has no paid invoice proof yet.")
        _verified_invoice_payment_intent(stripe, invoice_id, record.recurring_price_id, minimum_amount_cents=1)

    record.status = "completed"
    record.completed_at = record.completed_at or _billing_utc_now()
    record.stripe_customer_id = str(data_object.get("customer") or "").strip() or None
    record.stripe_subscription_id = str(data_object.get("subscription") or "").strip() or None
    subscription.offer_version = record.offer_version
    if subscription.checkout_request_session_id == session_id:
        subscription.checkout_request_status = "completed"
    _clear_billing_incident(subscription)
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
    # Stripe event timestamps have second precision; local reconciliation must
    # not make the rest of that same second appear older than a manual refresh.
    if normalized_created_at.replace(microsecond=0) < previous_created_at.replace(microsecond=0):
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
    stripe_customer_id = str(data_object.get("customer") or "").strip()
    stripe_subscription_id = str(_stripe_subscription_id_from_data_object(event_type, data_object) or "").strip()
    subscription = OrganizationSubscription.query.filter_by(stripe_subscription_id=stripe_subscription_id).first() if stripe_subscription_id else None
    if subscription is None and stripe_customer_id:
        subscription = OrganizationSubscription.query.filter_by(stripe_customer_id=stripe_customer_id).first()
    if subscription is not None:
        owner_id = subscription.organization_id
        organization, subscription = _lock_billing_state(owner_id)
        if organization_id is not None and organization_id != owner_id:
            _billing_incident(subscription, "stripe_ownership_conflict", "Stripe resource ownership disagrees with workspace metadata. Existing billing was preserved; platform review is required.", exception=StripePaymentProofError)
    elif organization_id and db.session.get(Organization, organization_id) is not None:
        organization, subscription = _lock_billing_state(organization_id)
    else:
        return None

    if subscription_status_is_complimentary(subscription.status):
        stripe_customer_id = str(data_object.get("customer") or "").strip()
        stripe_subscription_id = str(
            _stripe_subscription_id_from_data_object(event_type, data_object) or ""
        ).strip()
        _billing_incident(subscription, "complimentary_payment_conflict",
            "Stripe reported chargeable billing for complimentary organization "
            f"{subscription.organization_id}: event_id={event_id}, "
            f"customer_id={stripe_customer_id or 'missing'}, "
            f"subscription_id={stripe_subscription_id or 'missing'}. "
            "Cancel or reconcile the Stripe subscription before processing this event.",
            exception=StripeComplimentaryConflictError,
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

    if subscription.stripe_customer_id and stripe_customer_id and subscription.stripe_customer_id != stripe_customer_id:
        _billing_incident(subscription, "stripe_customer_conflict", "Stripe reported a different billing customer. Existing billing was preserved; platform review is required.", exception=StripePaymentProofError)
    authorized_replacement = False
    if subscription.stripe_subscription_id and stripe_subscription_id and subscription.stripe_subscription_id != stripe_subscription_id:
        if status in {"canceled", "incomplete_expired"} and stripe_customer_id == subscription.stripe_customer_id:
            raise StaleStripeEventError("An inactive prior Stripe subscription cannot replace current billing.")
        authorized_replacement = subscription.status in {"canceled", "incomplete_expired"} and StripeCheckoutSession.query.filter_by(
            organization_id=subscription.organization_id, stripe_subscription_id=stripe_subscription_id, status="completed",
        ).first() is not None
        if not authorized_replacement:
            _billing_incident(subscription, "unexpected_subscription", f"Stripe reported subscription {stripe_subscription_id}, but this workspace already owns {subscription.stripe_subscription_id}. Existing billing was preserved; platform reconciliation is required.", exception=StripePaymentProofError)

    if not authorized_replacement and _event_precedes_applied_state(subscription, event_created_at):
        raise StaleStripeEventError(f"Stripe event {event_id} is older than {subscription.last_stripe_event_id}.")

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
        authorized_replacement or previous_created_at is None
        or (
            normalized_created_at is not None
            and normalized_created_at > previous_created_at
        )
    ):
        subscription.last_stripe_event_created_at = as_utc_datetime(event_created_at).replace(tzinfo=None)
        subscription.last_stripe_event_id = event_id if len(event_id) <= 80 else "local-sync:" + hashlib.sha256(event_id.encode()).hexdigest()

    _clear_billing_incident(subscription)

    db.session.commit()
    return subscription


def _sync_verified_checkout(subscription, verified_session, record, event_created_at, event_id):
    _lock_billing_state(subscription.organization_id)
    if record.billing_plan_code == "setup_only":
        return subscription
    stripe_subscription_id = str(verified_session.get("subscription") or "").strip()
    if not stripe_subscription_id:
        raise StripePaymentProofError(
            f"Checkout session {verified_session.get('id')} does not reference a subscription."
        )
    stripe = _stripe_module()
    stripe_subscription = _retry_stripe_operation(
        f"subscription.retrieve:{stripe_subscription_id}",
        lambda: stripe.Subscription.retrieve(stripe_subscription_id),
    )
    subscription_data = _stripe_object_to_dict(stripe_subscription)
    if _extract_organization_id(subscription_data) not in {None, subscription.organization_id}:
        _billing_incident(subscription, "stripe_ownership_conflict", "The paid checkout subscription belongs to another workspace. Platform reconciliation is required.", exception=StripePaymentProofError)
    subscription_data.setdefault("metadata", {}).setdefault("organization_id", str(subscription.organization_id))
    return sync_subscription_from_event(
        "customer.subscription.updated",
        subscription_data,
        event_created_at,
        event_id,
        True,
    )


def _apply_stripe_event_to_billing_state(
    event_type: str,
    data_object: dict[str, Any],
    event_created_at: datetime,
    event_id: str,
) -> OrganizationSubscription | None:
    if event_type == "checkout.session.async_payment_succeeded":
        event_type = "checkout.session.completed"
    elif event_type == "invoice.paid":
        event_type = "invoice.payment_succeeded"
    # Serialize authoritative provider reads with plan and complimentary writes.
    remote_id = _stripe_subscription_id_from_data_object(event_type, data_object)
    owned = OrganizationSubscription.query.filter_by(stripe_subscription_id=remote_id).first() if remote_id else None
    if owned is None and data_object.get("customer"):
        owned = OrganizationSubscription.query.filter_by(stripe_customer_id=data_object["customer"]).first()
    issued = StripeCheckoutSession.query.filter_by(stripe_checkout_session_id=data_object.get("id")).first() if event_type.startswith("checkout.session.") else None
    owner_id = owned.organization_id if owned is not None else (issued.organization_id if issued is not None else _extract_organization_id(data_object))
    if owner_id and db.session.get(Organization, owner_id) is not None:
        _lock_billing_state(owner_id)
    if event_type in {"checkout.session.async_payment_failed", "checkout.session.expired"}:
        session_id = str(data_object.get("id") or "")
        stripe = _stripe_module()
        remote = _stripe_object_to_dict(_retry_stripe_operation("checkout.session.retrieve", lambda: stripe.checkout.Session.retrieve(session_id)))
        remote_owner = _extract_organization_id(remote)
        if not remote_owner:
            raise StripePaymentProofError("Checkout does not identify its workspace.")
        organization, subscription = _lock_billing_state(remote_owner)
        record = _checkout_session_record(session_id, organization, remote)
        if remote.get("status") == "complete" and remote.get("payment_status") == "paid":
            return _apply_stripe_event_to_billing_state("checkout.session.completed", remote, event_created_at, event_id)
        if remote.get("status") == "expired":
            _inspect_checkout_state(organization, subscription, record, remote)
            _clear_billing_incident(subscription)
            db.session.commit()
            return subscription
        record.status = "payment_pending"
        record.stripe_customer_id = str(remote.get("customer") or "") or None
        record.stripe_subscription_id = str(remote.get("subscription") or "") or None
        if subscription.checkout_request_session_id == session_id:
            subscription.checkout_request_status = "payment_pending"
        _billing_incident(subscription, "checkout_payment_pending", "Stripe reported a checkout payment failure or unresolved expiration. Reconcile the associated payment/subscription before starting a replacement; contact platform support.", exception=None)
        return subscription
    if event_type == "checkout.session.completed":
        session_id = str(data_object.get("id") or "").strip()
        if not session_id:
            raise StripePaymentProofError("Checkout event is missing a session id.")
        if data_object.get("payment_status") == "unpaid":
            try:
                verified = _verify_checkout_session_payment(session_id, allow_confirmed_payment_proof=True)
            except StripePaymentProofError:
                remote_owner = _extract_organization_id(data_object)
                if not remote_owner:
                    raise
                organization, subscription = _lock_billing_state(remote_owner)
                if subscription.billing_incident_code in MANUAL_BILLING_INCIDENTS:
                    raise
                record = _checkout_session_record(session_id, organization, data_object)
                record.status = "payment_pending"
                record.stripe_customer_id = str(data_object.get("customer") or "") or None
                record.stripe_subscription_id = str(data_object.get("subscription") or "") or None
                if subscription.checkout_request_session_id == session_id:
                    subscription.checkout_request_status = "payment_pending"
                _billing_incident(subscription, "checkout_payment_pending", "Checkout is complete, but payment proof is pending. No billing access was granted. Refresh billing before another payment change.", exception=None)
                return subscription
        else:
            verified = _verify_checkout_session_payment(session_id)
        return _sync_verified_checkout(*verified, event_created_at, event_id)

    if event_type in {"invoice.payment_succeeded", "invoice.payment_failed"}:
        stripe_subscription_id = _stripe_subscription_id_from_data_object(event_type, data_object)
        if not stripe_subscription_id:
            return None
        pending_checkout = StripeCheckoutSession.query.filter_by(
            stripe_subscription_id=stripe_subscription_id, status="payment_pending",
        ).order_by(StripeCheckoutSession.id.desc()).first()
        if event_type == "invoice.payment_succeeded" and pending_checkout is not None:
            organization, subscription = _lock_billing_state(pending_checkout.organization_id)
            remote = _checkout_record_snapshot(organization, subscription, pending_checkout)
            remote_data = _stripe_object_to_dict(remote)
            if remote_data.get("status") == "complete":
                return _apply_stripe_event_to_billing_state("checkout.session.completed", remote_data, event_created_at, event_id)
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
            if _extract_organization_id(subscription_data) not in {None, organization_id}:
                raise StripePaymentProofError("Stripe subscription ownership differs from the received event.")
            subscription_data.setdefault("metadata", {}).setdefault("organization_id", str(organization_id))
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
        record.event_created_at = as_utc_datetime(event_created_at).replace(tzinfo=None)
    if not record.received_at:
        record.received_at = as_utc_datetime(now).replace(tzinfo=None)
    record.last_seen_at = as_utc_datetime(now).replace(tzinfo=None)


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
            received_at=as_utc_datetime(now).replace(tzinfo=None),
            last_seen_at=as_utc_datetime(now).replace(tzinfo=None),
            attempt_count=1,
            status="processing",
        )
        db.session.add(record)
    else:
        record.attempt_count = int(record.attempt_count or 0) + 1

    with db.session.no_autoflush:
        _populate_webhook_record(
            record, event_id=event_id, event_type=event_type,
            data_object=data_object, event_created_at=event_created_at, now=now,
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
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first() is None:
            raise
        # The competing delivery durably owns processing; the normal duplicate
        # path returns its saved state without applying another payment.
        return process_stripe_webhook_event(event)

    if event_type not in RELEVANT_STRIPE_EVENT_TYPES:
        ignored = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
        if ignored is None:
            raise RuntimeError("Stripe webhook ledger record disappeared during ignore flow.")
        ignored.status = "ignored"
        ignored.processed_at = _billing_utc_now()
        ignored.last_error = None
        ignored.last_seen_at = _billing_utc_now()
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
            ignored.processed_at = _billing_utc_now()
            ignored.last_error = f"No local subscription matched Stripe event {event_id}."
            ignored.last_seen_at = _billing_utc_now()
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
        ignored.processed_at = _billing_utc_now()
        ignored.last_seen_at = _billing_utc_now()
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
        failed.last_seen_at = _billing_utc_now()
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
    processed.organization_id = subscription.organization_id
    processed.processed_at = _billing_utc_now()
    processed.last_error = None
    processed.last_seen_at = _billing_utc_now()
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
    organization, subscription = _lock_billing_state(organization.id)
    if subscription_status_is_complimentary(subscription.status):
        return subscription

    stripe = _stripe_module()

    known_checkouts = StripeCheckoutSession.query.filter_by(organization_id=organization.id).filter(
        StripeCheckoutSession.status.in_(["open", "expiry_unknown", "payment_pending"]),
    ).order_by(StripeCheckoutSession.id.desc()).all()
    for record in known_checkouts:
        remote = _checkout_record_snapshot(organization, subscription, record)
        data = _stripe_object_to_dict(remote)
        if data.get("status") == "complete":
            return _apply_stripe_event_to_billing_state(
                "checkout.session.completed", data, utc_now(), f"checkout-refresh:{record.stripe_checkout_session_id}:{uuid4().hex}",
            )
        if data.get("status") == "expired":
            _inspect_checkout_state(organization, subscription, record, remote)
        elif data.get("status") == "open":
            record.status = "open"
            if subscription.checkout_request_session_id == record.stripe_checkout_session_id:
                subscription.checkout_request_status = "open"
            _clear_billing_incident(subscription)
        else:
            _billing_incident(subscription, "checkout_state_unconfirmed", "The existing checkout state is unknown. Platform billing review is required.")
    db.session.commit()
    organization, subscription = _lock_billing_state(organization.id)

    if subscription.stripe_subscription_id:
        if subscription_requires_verified_setup_payment(subscription) and not subscription_activation_paid(subscription):
            checkout_session = _find_matching_checkout_session(stripe, organization, subscription, user_email)
            if checkout_session is not None:
                return _apply_stripe_event_to_billing_state(
                    "checkout.session.completed", checkout_session, utc_now(), f"checkout-proof-refresh:{subscription.id}:{uuid4().hex}",
                )
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
        checkout_session = _find_matching_checkout_session(stripe, organization, subscription, user_email)
        if checkout_session is not None:
            return _apply_stripe_event_to_billing_state(
                "checkout.session.completed", checkout_session, utc_now(), f"checkout-refresh:{checkout_session['id']}:{uuid4().hex}",
            )
        stripe_subscriptions = stripe.Subscription.list(
            customer=subscription.stripe_customer_id,
            status="all",
            limit=5,
        )
        unexpected = [_stripe_object_to_dict(row) for row in _stripe_collection_data(stripe_subscriptions)
                      if str(_stripe_object_to_dict(row).get("status") or "") not in {"canceled", "incomplete_expired"}]
        if unexpected or _stripe_collection_has_more(stripe_subscriptions):
            _billing_incident(subscription, "unexpected_subscription", "Stripe has a subscription that is not linked to a verified workspace checkout. Existing billing was preserved; platform reconciliation is required.")

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


class UsageSettlementReviewError(RuntimeError):
    """Financial identity or an old unacknowledged operation needs human proof."""


def _usage_settlement_object_id(value: Any) -> str:
    return str(value.get("id") if isinstance(value, dict) else value or "").strip()


def _verify_usage_settlement_invoice(data, invoice_id, customer_id, metadata, currency):
    if str(data.get("id") or "") != invoice_id or _usage_settlement_object_id(data.get("customer")) != customer_id:
        raise UsageSettlementReviewError("Retained usage invoice identity/customer differs from its billing owner.")
    if str(data.get("currency") or "").lower() != currency:
        raise UsageSettlementReviewError("Retained usage invoice currency differs from its frozen period.")
    actual_metadata = data.get("metadata") or {}
    if any(str(actual_metadata.get(key) or "") != value for key, value in metadata.items()):
        raise UsageSettlementReviewError("Retained usage invoice metadata differs from its original settlement.")
    if str(data.get("status") or "") not in {"draft", "open", "paid"}:
        raise UsageSettlementReviewError("Retained usage invoice is not a draft or collectible invoice.")
    if current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED") and data.get("livemode") is not True:
        raise UsageSettlementReviewError("Retained usage invoice is not live mode.")


def _verify_usage_settlement_item(data, invoice_id, customer_id, metadata, currency, amount):
    if not data.get("id") or _usage_settlement_object_id(data.get("invoice")) != invoice_id or _usage_settlement_object_id(data.get("customer")) != customer_id:
        raise UsageSettlementReviewError("Retained usage invoice item belongs to a different invoice/customer.")
    actual_metadata = data.get("metadata") or {}
    if any(str(actual_metadata.get(key) or "") != value for key, value in metadata.items()):
        raise UsageSettlementReviewError("Retained usage invoice item metadata differs from its original settlement.")
    if str(data.get("currency") or "").lower() != currency or data.get("amount") != amount:
        raise UsageSettlementReviewError("Retained usage invoice item amount/currency differs from its frozen settlement.")
    if current_app.config.get("STRIPE_LIVE_CONFIGURATION_REQUIRED") and data.get("livemode") is not True:
        raise UsageSettlementReviewError("Retained usage invoice item is not live mode.")


def _hold_usage_settlement_for_review(period, reason):
    from app.models import OrganizationProviderAuditLog

    period.status = "review_required"
    period.settlement_due_at = None
    identity = f'"period_id": {period.id},'
    existing = OrganizationProviderAuditLog.query.filter_by(
        organization_id=period.organization_id, action="usage_settlement_review",
    ).filter(OrganizationProviderAuditLog.metadata_json.contains(identity)).first()
    if existing is None:
        db.session.add(OrganizationProviderAuditLog(
            organization_id=period.organization_id, action="usage_settlement_review", status="pending",
            message=str(reason), created_at=_billing_utc_now(),
            metadata_json=json.dumps({"period_id": period.id, "settlement_version": period.settlement_version,
                                      "stripe_invoice_id": period.stripe_invoice_id,
                                      "stripe_invoice_item_id": period.stripe_invoice_item_id}, sort_keys=True),
        ))


def _post_closed_usage_invoice_items() -> dict[str, int]:
    from app.services.twilio_service import usage_period_billing_block_reason

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
        if period.status == "review_required" or usage_period_billing_block_reason(period):
            if period.status not in {"posted", "carry_forward"}:
                period.status = "review_required"
                period.settlement_due_at = None
            db.session.commit()
            summary["periods_skipped"] += 1
            continue
        if int(period.invoiced_units or 0) > 0 or period.status in {"posted", "carry_forward"}:
            # A settled snapshot cannot silently become authorization for a
            # second historical collection when later facts differ.
            db.session.rollback()
            summary["periods_skipped"] += 1
            continue
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

        resume_existing_settlement = bool(int(period.settlement_version or 0) > 0 or period.stripe_invoice_id or period.stripe_invoice_item_id)
        if resume_existing_settlement:
            settlement_version = int(period.settlement_version or 0)
            if settlement_version <= 0:
                _hold_usage_settlement_for_review(period, "Retained invoice has no trustworthy settlement version; existing financial identity was preserved.")
                db.session.commit()
                summary["periods_skipped"] += 1
                continue
        else:
            settlement_version = int(period.settlement_version or 0) + 1
            period.settlement_started_at = _billing_utc_now()
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
        # The period is frozen once a settlement starts. Configuration changes
        # cannot change the amount replayed under an existing provider key.
        rate = Decimal(str(period.sell_amount or 0)) / Decimal(int(period.overage_units or 0))
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
            retry_started = as_utc_datetime(period.settlement_started_at)
            creation_retry_is_safe = retry_started is not None and retry_started > now - timedelta(hours=23)
            customer_id = str(subscription.stripe_customer_id)
            currency = str(period.currency or "usd").lower()
            invoice_was_already_persisted = bool(period.stripe_invoice_id)
            if invoice_was_already_persisted:
                invoice_id = str(period.stripe_invoice_id or "").strip()
                invoice = _retry_stripe_operation(
                    f"invoice.retrieve:{invoice_id}",
                    lambda: stripe.Invoice.retrieve(invoice_id),
                )
            else:
                if not creation_retry_is_safe:
                    raise UsageSettlementReviewError("Usage invoice creation is unconfirmed beyond the safe idempotency retry window; original settlement identity was preserved.")
                invoice = _retry_stripe_operation(
                    f"invoice.create:{invoice_key}",
                    lambda: stripe.Invoice.create(
                        customer=customer_id,
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
            _verify_usage_settlement_invoice(invoice_data, invoice_id, customer_id, metadata, currency)
            settlement_units = intended_settlement_units
            settlement_amount_cents = _decimal_to_cents(Decimal(settlement_units) * rate)

            period.stripe_invoice_id = invoice_id
            period.status = "invoicing"
            db.session.commit()

            retained_item = None
            if period.stripe_invoice_item_id:
                retained_item = _stripe_object_to_dict(_retry_stripe_operation(
                    f"invoice_item.retrieve:{period.stripe_invoice_item_id}",
                    lambda: stripe.InvoiceItem.retrieve(period.stripe_invoice_item_id),
                ))
                if str(retained_item.get("id") or "") != period.stripe_invoice_item_id:
                    raise UsageSettlementReviewError("Retrieved usage invoice item differs from the retained item identity.")
            elif resume_existing_settlement or invoice_was_already_persisted:
                items = _stripe_object_to_dict(_retry_stripe_operation(
                    f"invoice_item.list:{invoice_id}",
                    lambda: stripe.InvoiceItem.list(invoice=invoice_id, limit=100),
                ))
                rows = items.get("data")
                if not isinstance(rows, list) or items.get("has_more") or len(rows) > 1:
                    raise UsageSettlementReviewError("Usage invoice items cannot be proven as one complete settlement item; manual review is required.")
                if rows:
                    retained_item = _stripe_object_to_dict(rows[0])
            if retained_item is not None:
                _verify_usage_settlement_item(retained_item, invoice_id, customer_id, metadata, currency, settlement_amount_cents)
                period.stripe_invoice_item_id = str(retained_item["id"])
                period.status = "item_created"
                db.session.commit()
            else:
                if not creation_retry_is_safe:
                    raise UsageSettlementReviewError("Usage item creation is unconfirmed beyond the safe idempotency retry window; no replacement item was created.")
                invoice_item = _retry_stripe_operation(
                    f"invoice_item.create:{item_key}",
                    lambda: stripe.InvoiceItem.create(
                        customer=customer_id,
                        invoice=invoice_id,
                        currency=currency,
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
                _verify_usage_settlement_item(invoice_item_data, invoice_id, customer_id, metadata, currency, settlement_amount_cents)
                invoice_item_id = str(
                    invoice_item_data.get("id") or getattr(invoice_item, "id", "")
                ).strip()
                if not invoice_item_id:
                    raise RuntimeError("Stripe did not return an invoice item id for SMS overage settlement.")
                period.stripe_invoice_item_id = invoice_item_id
                period.status = "item_created"
                db.session.commit()

            # Item identity alone does not prove the invoice has no additional
            # charge. Read its current total before making it collectible.
            invoice_data = _stripe_object_to_dict(_retry_stripe_operation(
                f"invoice.retrieve:{invoice_id}", lambda: stripe.Invoice.retrieve(invoice_id),
            ))
            _verify_usage_settlement_invoice(invoice_data, invoice_id, customer_id, metadata, currency)
            if invoice_data.get("total") != settlement_amount_cents:
                raise UsageSettlementReviewError("Usage invoice total differs from the frozen settlement amount; no collection was authorized.")
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
                _verify_usage_settlement_invoice(finalized_data, invoice_id, customer_id, metadata, currency)
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
            period.posted_at = _billing_utc_now()
            summary["periods_posted"] += 1
        except UsageSettlementReviewError as exc:
            db.session.rollback()
            period = db.session.get(OrganizationUsageBillingPeriod, period_id)
            if period is not None:
                _hold_usage_settlement_for_review(period, str(exc))
            summary["periods_skipped"] += 1
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
