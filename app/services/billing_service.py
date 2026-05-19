from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

from flask import current_app, url_for

from app import db
from app.models import (
    AppUser,
    Organization,
    OrganizationMembership,
    OrganizationSubscription,
    OrganizationUsageBillingPeriod,
    StripeWebhookEvent,
    utc_now,
)
from app.services.billing_plans import (
    activation_price_id,
    billing_plan_for_code,
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
CHECKOUT_SESSION_CREATED_SKEW_ALLOWANCE = timedelta(minutes=5)
FAKE_CHECKOUT_SESSION_PREFIX = "cs_fake_org_"


def subscription_status_allows_sending(status: str | None) -> bool:
    return (status or "").strip().lower() in ACTIVE_SUBSCRIPTION_STATUSES


def subscription_status_is_complimentary(status: str | None) -> bool:
    return (status or "").strip().lower() == COMPLIMENTARY_SUBSCRIPTION_STATUS


def organization_can_send(organization: Organization | None) -> bool:
    if organization is None:
        return False
    subscription = organization.subscription
    return subscription_status_allows_sending(subscription.status if subscription else None)


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
    db.session.commit()
    return subscription


def _stripe_module():
    try:
        import stripe  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only when dependency missing
        raise RuntimeError("stripe package is not installed.") from exc

    secret_key = current_app.config.get("STRIPE_SECRET_KEY")
    if not secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured.")
    stripe.api_key = secret_key
    return stripe


def _stripe_object_to_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    return dict(value)


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


def create_checkout_session(
    organization: Organization,
    user_email: str,
    success_url: str,
    cancel_url: str,
    *,
    plan_code: str | None = None,
):
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
        db.session.commit()

    if fake_checkout_enabled():
        session_id = _fake_checkout_session_id(organization)
        return SimpleNamespace(
            id=session_id,
            url=_fake_checkout_url(
                organization,
                session_id=session_id,
                success_url=success_url,
                cancel_url=cancel_url,
            ),
        )

    stripe = _stripe_module()
    line_items = []
    if not subscription_activation_paid(subscription):
        activation_id = activation_price_id()
        if not activation_id:
            raise RuntimeError("STRIPE_ACTIVATION_PRICE_ID is not configured.")
        line_items.append({"price": activation_id, "quantity": 1})
    line_items.append({"price": price_id, "quantity": 1})
    checkout_metadata = {"organization_id": str(organization.id)}
    if selected_plan is not None:
        checkout_metadata["billing_plan_code"] = selected_plan.code

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

    session = stripe.checkout.Session.create(**params)
    return session


def create_billing_portal_session(organization: Organization, return_url: str):
    subscription = ensure_subscription_record(organization)
    if subscription_status_is_complimentary(subscription.status):
        raise RuntimeError("Complimentary organizations do not use the Stripe billing portal.")
    if not subscription.stripe_customer_id:
        raise RuntimeError("Organization does not have a Stripe customer yet.")

    stripe = _stripe_module()
    return stripe.billing_portal.Session.create(
        customer=subscription.stripe_customer_id,
        return_url=return_url,
    )


def sync_subscription_from_event(event_type: str, data_object: dict) -> OrganizationSubscription | None:
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

    if subscription_status_is_complimentary(subscription.status):
        return subscription

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

    db.session.commit()
    return subscription


def _apply_stripe_event_to_billing_state(event_type: str, data_object: dict) -> OrganizationSubscription | None:
    if event_type == "checkout.session.completed":
        subscription = sync_subscription_from_event(event_type, data_object)
        stripe_subscription_id = _stripe_subscription_id_from_data_object(event_type, data_object)
        if stripe_subscription_id:
            stripe = _stripe_module()
            stripe_subscription = stripe.Subscription.retrieve(stripe_subscription_id)
            return sync_subscription_from_event(
                "customer.subscription.updated",
                _stripe_object_to_dict(stripe_subscription),
            )
        return subscription

    if event_type in {"invoice.payment_succeeded", "invoice.payment_failed"}:
        stripe_subscription_id = _stripe_subscription_id_from_data_object(event_type, data_object)
        if not stripe_subscription_id:
            return None
        stripe = _stripe_module()
        stripe_subscription = stripe.Subscription.retrieve(stripe_subscription_id)
        return sync_subscription_from_event(
            "customer.subscription.updated",
            _stripe_object_to_dict(stripe_subscription),
        )

    return sync_subscription_from_event(event_type, data_object)


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

    data_object = _stripe_object_to_dict(event.get("data", {}).get("object", {}))
    event_created_at = _event_timestamp_to_utc_datetime(event.get("created"))
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
        subscription = _apply_stripe_event_to_billing_state(event_type, data_object)
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
    session = stripe.checkout.Session.retrieve(session_id)
    data_object = _stripe_object_to_dict(session)
    if organization is not None:
        metadata = data_object.get("metadata") or {}
        org_id = metadata.get("organization_id") or data_object.get("client_reference_id")
        if str(org_id or "") != str(organization.id):
            raise RuntimeError("Checkout session does not belong to this organization.")
    if data_object.get("status") != "complete":
        return None
    return _apply_stripe_event_to_billing_state("checkout.session.completed", data_object)


def refresh_subscription_from_stripe(
    organization: Organization,
    user_email: str = "",
) -> OrganizationSubscription | None:
    subscription = ensure_subscription_record(organization)
    if subscription_status_is_complimentary(subscription.status):
        return subscription

    stripe = _stripe_module()

    if subscription.stripe_subscription_id:
        stripe_subscription = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
        return sync_subscription_from_event(
            "customer.subscription.updated",
            _stripe_object_to_dict(stripe_subscription),
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
            )

    checkout_session = _find_matching_checkout_session(
        stripe,
        organization,
        subscription,
        user_email,
    )
    if checkout_session is not None:
        return _apply_stripe_event_to_billing_state("checkout.session.completed", checkout_session)

    return subscription


def _decimal_to_cents(value: Decimal | int | float | str) -> int:
    normalized = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(normalized * 100)


def _usage_invoice_item_idempotency_key(period: OrganizationUsageBillingPeriod) -> str:
    return (
        f"sms-overage:{period.organization_id}:"
        f"{period.period_start.isoformat()}:{period.period_end.isoformat()}"
    )


def _post_closed_usage_invoice_items() -> dict[str, int]:
    summary = {
        "periods_scanned": 0,
        "periods_posted": 0,
        "periods_skipped": 0,
        "periods_failed": 0,
    }
    period_start, period_end = previous_billing_period_window()
    periods = OrganizationUsageBillingPeriod.query.filter_by(
        period_start=period_start,
        period_end=period_end,
    ).all()
    if not periods:
        return summary

    stripe = _stripe_module()
    for period in periods:
        summary["periods_scanned"] += 1
        if period.stripe_invoice_item_id or (period.status or "").strip().lower() in {"posted", "included"}:
            summary["periods_skipped"] += 1
            continue
        if period.overage_units <= 0:
            period.status = "included"
            summary["periods_skipped"] += 1
            continue

        organization = period.organization
        subscription = organization.subscription if organization is not None else None
        if subscription is None or not subscription.stripe_customer_id:
            period.status = "pending_customer"
            summary["periods_skipped"] += 1
            continue

        try:
            invoice_item = stripe.InvoiceItem.create(
                customer=subscription.stripe_customer_id,
                currency=period.currency or "usd",
                amount=_decimal_to_cents(period.sell_amount),
                description=(
                    f"SMS overage for {(organization.name if organization is not None else f'org {period.organization_id}')}"
                    f" ({period_start.date()} to {(period_end - timedelta(seconds=1)).date()})"
                ),
                metadata={
                    "organization_id": str(period.organization_id),
                    "period_start": period_start.date().isoformat(),
                    "period_end": period_end.date().isoformat(),
                    "overage_units": str(period.overage_units),
                },
                idempotency_key=_usage_invoice_item_idempotency_key(period),
            )
            period.stripe_invoice_item_id = invoice_item.id
            period.status = "posted"
            period.posted_at = utc_now()
            summary["periods_posted"] += 1
        except Exception:
            db.session.rollback()
            summary["periods_failed"] += 1
            current_app.logger.exception(
                "Failed posting SMS overage invoice item for organization_id=%s period_start=%s.",
                period.organization_id,
                period.period_start,
            )
            period = db.session.get(OrganizationUsageBillingPeriod, period.id)
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
