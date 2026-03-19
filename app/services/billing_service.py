from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app

from app import db
from app.models import Organization, OrganizationSubscription


ACTIVE_SUBSCRIPTION_STATUSES = {"trialing", "active"}


def subscription_status_allows_sending(status: str | None) -> bool:
    return (status or "").strip().lower() in ACTIVE_SUBSCRIPTION_STATUSES


def organization_can_send(organization: Organization | None) -> bool:
    if organization is None:
        return False
    subscription = organization.subscription
    return subscription_status_allows_sending(subscription.status if subscription else None)


def ensure_subscription_record(organization: Organization) -> OrganizationSubscription:
    subscription = organization.subscription
    if subscription is not None:
        return subscription

    subscription = OrganizationSubscription(
        organization=organization,
        stripe_price_id=current_app.config.get("STRIPE_PRICE_ID"),
        status="incomplete",
    )
    db.session.add(subscription)
    db.session.flush()
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


def create_checkout_session(organization: Organization, user_email: str, success_url: str, cancel_url: str):
    stripe = _stripe_module()
    subscription = ensure_subscription_record(organization)
    price_id = current_app.config.get("STRIPE_PRICE_ID")
    if not price_id:
        raise RuntimeError("STRIPE_PRICE_ID is not configured.")

    params = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(organization.id),
        "metadata": {"organization_id": str(organization.id)},
        "subscription_data": {
            "metadata": {"organization_id": str(organization.id)},
        },
    }

    if user_email:
        params["customer_email"] = user_email
    if subscription.stripe_customer_id:
        params["customer"] = subscription.stripe_customer_id
        params.pop("customer_email", None)

    trial_days = int(current_app.config.get("BILLING_TRIAL_DAYS", 14) or 0)
    if trial_days > 0 and not subscription.stripe_subscription_id:
        params["subscription_data"]["trial_period_days"] = trial_days

    session = stripe.checkout.Session.create(**params)
    return session


def create_billing_portal_session(organization: Organization, return_url: str):
    stripe = _stripe_module()
    subscription = ensure_subscription_record(organization)
    if not subscription.stripe_customer_id:
        raise RuntimeError("Organization does not have a Stripe customer yet.")

    return stripe.billing_portal.Session.create(
        customer=subscription.stripe_customer_id,
        return_url=return_url,
    )


def sync_subscription_from_event(event_type: str, data_object: dict) -> OrganizationSubscription | None:
    organization_id = None
    metadata = data_object.get("metadata") or {}
    client_reference_id = data_object.get("client_reference_id")
    if metadata.get("organization_id"):
        organization_id = int(metadata["organization_id"])
    elif client_reference_id:
        organization_id = int(client_reference_id)

    subscription = None
    if organization_id:
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            return None
        subscription = ensure_subscription_record(organization)

    if subscription is None:
        stripe_customer_id = data_object.get("customer")
        stripe_subscription_id = data_object.get("subscription") or data_object.get("id")
        query = OrganizationSubscription.query
        if stripe_subscription_id:
            subscription = query.filter_by(stripe_subscription_id=stripe_subscription_id).first()
        if subscription is None and stripe_customer_id:
            subscription = query.filter_by(stripe_customer_id=stripe_customer_id).first()
        if subscription is None:
            return None

    stripe_customer_id = data_object.get("customer")
    stripe_subscription_id = data_object.get("subscription") or data_object.get("id")
    status = data_object.get("status")

    if stripe_customer_id:
        subscription.stripe_customer_id = stripe_customer_id
    if stripe_subscription_id:
        subscription.stripe_subscription_id = stripe_subscription_id
    if status:
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
        subscription.status = subscription.status or "active"

    db.session.commit()
    return subscription
