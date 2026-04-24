from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from flask import current_app

from app.models import OrganizationSubscription


@dataclass(frozen=True)
class BillingPlan:
    code: str
    name: str
    price_id: str
    included_segments: int


PLAN_DEFINITIONS = (
    ("starter", "Starter", "STRIPE_PRICE_ID", "BILLING_STARTER_INCLUDED_OUTBOUND_SEGMENTS", 1000),
    ("growth", "Growth", "STRIPE_GROWTH_PRICE_ID", "BILLING_GROWTH_INCLUDED_OUTBOUND_SEGMENTS", 3000),
    ("scale", "Scale", "STRIPE_SCALE_PRICE_ID", "BILLING_SCALE_INCLUDED_OUTBOUND_SEGMENTS", 10000),
)


def _positive_int(value, fallback: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(fallback))


def _money_label(value, *, minimum_places: int = 0, maximum_places: int = 4) -> str:
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    normalized = format(amount.normalize(), "f")
    if "." in normalized:
        whole, fraction = normalized.split(".", 1)
        fraction = fraction[:maximum_places].rstrip("0")
        if len(fraction) < minimum_places:
            fraction = fraction.ljust(minimum_places, "0")
        normalized = f"{whole}.{fraction}" if fraction else whole
    elif minimum_places:
        normalized = f"{normalized}.{'0' * minimum_places}"
    return f"${normalized}"


def billing_plan_catalog() -> list[BillingPlan]:
    plans: list[BillingPlan] = []
    for code, name, price_key, included_key, default_included in PLAN_DEFINITIONS:
        price_id = str(current_app.config.get(price_key) or "").strip()
        if not price_id:
            continue
        included_segments = _positive_int(current_app.config.get(included_key), default_included)
        plans.append(
            BillingPlan(
                code=code,
                name=name,
                price_id=price_id,
                included_segments=included_segments,
            )
        )
    return plans


def billing_plan_for_price_id(price_id: str | None) -> BillingPlan | None:
    normalized = str(price_id or "").strip()
    if not normalized:
        return None
    for plan in billing_plan_catalog():
        if plan.price_id == normalized:
            return plan
    return None


def default_billing_plan() -> BillingPlan | None:
    catalog = billing_plan_catalog()
    return catalog[0] if catalog else None


def billing_plan_for_subscription(subscription: OrganizationSubscription | None) -> BillingPlan | None:
    if subscription is not None and str(subscription.stripe_price_id or "").strip():
        return billing_plan_for_price_id(subscription.stripe_price_id)
    return default_billing_plan()


def recurring_price_id_for_subscription(subscription: OrganizationSubscription | None) -> str:
    plan = billing_plan_for_subscription(subscription)
    if plan is not None:
        return plan.price_id
    return str(current_app.config.get("STRIPE_PRICE_ID") or "").strip()


def included_segments_for_subscription(subscription: OrganizationSubscription | None) -> int:
    if subscription is not None and str(subscription.stripe_price_id or "").strip():
        plan = billing_plan_for_price_id(subscription.stripe_price_id)
        if plan is None:
            return _positive_int(current_app.config.get("BILLING_INCLUDED_OUTBOUND_SEGMENTS"), 0)
        return plan.included_segments
    plan = default_billing_plan()
    if plan is not None:
        return plan.included_segments
    return _positive_int(current_app.config.get("BILLING_INCLUDED_OUTBOUND_SEGMENTS"), 0)


def subscription_activation_paid(subscription: OrganizationSubscription | None) -> bool:
    if subscription is None:
        return False
    status = (subscription.status or "").strip().lower()
    if status == "complimentary":
        return True
    return bool(
        subscription.stripe_customer_id
        or subscription.stripe_subscription_id
        or status in {"active", "trialing"}
    )


def activation_price_id() -> str:
    return str(current_app.config.get("STRIPE_ACTIVATION_PRICE_ID") or "").strip()


def overage_rate_label() -> str:
    return _money_label(current_app.config.get("BILLING_OUTBOUND_SEGMENT_RATE_USD"), minimum_places=2)


def activation_fee_label() -> str:
    return _money_label(current_app.config.get("BILLING_ACTIVATION_FEE_USD"), minimum_places=0)


def segment_count_label(value: int) -> str:
    return f"{_positive_int(value, 0):,}"
