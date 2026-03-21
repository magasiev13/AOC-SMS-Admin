from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from flask import current_app

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
from app.services.twilio_service import previous_billing_period_window, reconcile_messaging_usage


ACTIVE_SUBSCRIPTION_STATUSES = {"trialing", "active"}
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


def _stripe_object_to_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    return dict(value)


def _as_utc_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_timestamp_to_utc_datetime(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _extract_organization_id(data_object: dict) -> int | None:
    metadata = data_object.get("metadata") or {}
    client_reference_id = data_object.get("client_reference_id")
    raw_value = metadata.get("organization_id") or client_reference_id
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _stripe_subscription_id_from_data_object(event_type: str, data_object: dict) -> str | None:
    if event_type.startswith("customer.subscription."):
        return data_object.get("id")
    return data_object.get("subscription")


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
    period_end = _as_utc_datetime(subscription.current_period_end)
    return (
        subscription.status,
        subscription.stripe_customer_id,
        subscription.stripe_subscription_id,
        subscription.stripe_price_id,
        period_end.isoformat() if period_end else None,
        bool(subscription.cancel_at_period_end),
    )


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
    organization_id = _extract_organization_id(data_object)

    subscription = None
    if organization_id:
        organization = db.session.get(Organization, organization_id)
        if organization is None:
            return None
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
    if organization_id is not None:
        record.organization_id = organization_id
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
        _as_utc_datetime(record.last_seen_at or record.received_at)
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
            raise RuntimeError(f"No local subscription matched Stripe event {event_id}.")
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
    stripe = _stripe_module()
    subscription = ensure_subscription_record(organization)

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

    checkout_sessions = stripe.checkout.Session.list(limit=20)
    for checkout_session in checkout_sessions.data:
        data_object = _stripe_object_to_dict(checkout_session)
        metadata = data_object.get("metadata") or {}
        organization_id = metadata.get("organization_id") or data_object.get("client_reference_id")
        if str(organization_id or "") != str(organization.id):
            continue
        if user_email and (data_object.get("customer_email") or "").strip().lower() != user_email.strip().lower():
            continue
        if data_object.get("status") != "complete":
            continue
        return _apply_stripe_event_to_billing_state("checkout.session.completed", data_object)

    return subscription


def _decimal_to_cents(value: Decimal | int | float | str) -> int:
    normalized = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(normalized * 100)


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
