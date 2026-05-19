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
    billing_interval: str
    price_label: str
    checkout_label: str


PLAN_DEFINITIONS = (
    (
        "monthly",
        "Monthly",
        ("STRIPE_MONTHLY_PRICE_ID", "STRIPE_PRICE_ID"),
        "BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS",
        1000,
        "month",
        "BILLING_MONTHLY_PRICE_USD",
        "59.99",
        "Pay monthly",
    ),
    (
        "annual",
        "Annual upfront",
        ("STRIPE_ANNUAL_PRICE_ID",),
        "BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS",
        1000,
        "year",
        "BILLING_ANNUAL_PRICE_USD",
        "600.00",
        "Pay $600 upfront",
    ),
    (
        "growth",
        "Growth",
        ("STRIPE_GROWTH_PRICE_ID",),
        "BILLING_GROWTH_INCLUDED_OUTBOUND_SEGMENTS",
        3000,
        "month",
        "BILLING_GROWTH_PRICE_USD",
        "99.00",
        "Pay monthly",
    ),
    (
        "scale",
        "Scale",
        ("STRIPE_SCALE_PRICE_ID",),
        "BILLING_SCALE_INCLUDED_OUTBOUND_SEGMENTS",
        10000,
        "month",
        "BILLING_SCALE_PRICE_USD",
        "199.00",
        "Pay monthly",
    ),
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
    for (
        code,
        name,
        price_keys,
        included_key,
        default_included,
        billing_interval,
        display_price_key,
        default_display_price,
        checkout_label,
    ) in PLAN_DEFINITIONS:
        price_id = ""
        for price_key in price_keys:
            price_id = str(current_app.config.get(price_key) or "").strip()
            if price_id:
                break
        if not price_id:
            continue
        included_segments = _positive_int(current_app.config.get(included_key), default_included)
        price_label = _money_label(
            current_app.config.get(display_price_key) or default_display_price,
            minimum_places=2 if code == "monthly" else 0,
        )
        interval_label = "mo" if billing_interval == "month" else "yr"
        plans.append(
            BillingPlan(
                code=code,
                name=name,
                price_id=price_id,
                included_segments=included_segments,
                billing_interval=billing_interval,
                price_label=f"{price_label}/{interval_label}",
                checkout_label=checkout_label,
            )
        )
    return plans


def _configured_csv_values(config_key: str) -> set[str]:
    raw_value = str(current_app.config.get(config_key) or "")
    return {
        item.strip().lower()
        for item in raw_value.split(",")
        if item.strip()
    }


def annual_only_offer_enabled_for_organization(organization) -> bool:
    if organization is None:
        return False
    billing_offer = str(getattr(organization, "billing_offer", "") or "").strip().lower()
    if billing_offer == "annual_only":
        return True

    slug = str(getattr(organization, "slug", "") or "").strip().lower()
    org_id = str(getattr(organization, "id", "") or "").strip().lower()
    annual_only_slugs = _configured_csv_values("BILLING_ANNUAL_ONLY_ORG_SLUGS")
    annual_only_ids = _configured_csv_values("BILLING_ANNUAL_ONLY_ORG_IDS")
    return bool((slug and slug in annual_only_slugs) or (org_id and org_id in annual_only_ids))


def eligible_billing_plan_codes_for_organization(organization) -> tuple[str, ...]:
    if annual_only_offer_enabled_for_organization(organization):
        return ("annual",)
    return ("monthly", "annual")


def billing_plan_options_for_organization(organization) -> list[BillingPlan]:
    eligible_codes = set(eligible_billing_plan_codes_for_organization(organization))
    return [plan for plan in billing_plan_catalog() if plan.code in eligible_codes]


def billing_plan_for_code(code: str | None) -> BillingPlan | None:
    normalized = str(code or "").strip().lower()
    if not normalized:
        return None
    for plan in billing_plan_catalog():
        if plan.code == normalized:
            return plan
    return None


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
    return str(
        current_app.config.get("STRIPE_MONTHLY_PRICE_ID")
        or current_app.config.get("STRIPE_PRICE_ID")
        or ""
    ).strip()


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
