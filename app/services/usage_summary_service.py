"""Read existing period and segment evidence without reconciling or billing it."""
from decimal import Decimal
from types import SimpleNamespace

from app import db
from app.models import MessagingUsageRecord, OrganizationUsageBillingPeriod
from app.services.billing_plans import included_segments_for_subscription, segment_count_label
from app.services.twilio_service import _currency_rate, _period_usage_query, _period_window, _usage_currency
from app.tenant import get_current_organization_id, tenant_scope_is_active


def workspace_usage_is_billed(organization) -> bool:
    if organization is None:
        return False
    subscription = organization.subscription
    profile = organization.messaging_profile
    return not (
        (subscription is not None and (subscription.status or '').strip().lower() == 'complimentary')
        or (profile is not None and profile.provider_mode == 'customer_managed')
    )


def _money_label(amount: Decimal, currency: str) -> str:
    return f'${amount:,.2f}' if currency == 'usd' else f'{currency.upper()} {amount:,.2f}'


def workspace_usage_summary(organization, *, reference_time=None) -> dict | None:
    if organization is None:
        return None
    with db.session.no_autoflush:
        if tenant_scope_is_active() and get_current_organization_id() != organization.id:
            raise ValueError('Usage belongs to another workspace.')
        start, end = _period_window(reference_time)
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
        billed = workspace_usage_is_billed(organization)
        period = OrganizationUsageBillingPeriod.query.filter_by(
            organization_id=organization.id, period_start=start, period_end=end,
        ).first()
        evidence = _period_usage_query(SimpleNamespace(
            organization_id=organization.id, period_start=start, period_end=end,
        )).filter(MessagingUsageRecord.direction == 'outbound')
        if billed:
            pending = evidence.filter(db.or_(
                MessagingUsageRecord.units_status != 'known',
                MessagingUsageRecord.cost_status != 'known',
                MessagingUsageRecord.billing_period_start.is_(None),
                MessagingUsageRecord.billing_period_end.is_(None),
                MessagingUsageRecord.billing_review_required.is_(True),
                MessagingUsageRecord.historical_observation_only.is_(True),
            )).count()
            known = evidence.filter(
                MessagingUsageRecord.billable.is_(True),
                MessagingUsageRecord.units_status == 'known', MessagingUsageRecord.cost_status == 'known',
                MessagingUsageRecord.billing_period_start == start, MessagingUsageRecord.billing_period_end == end,
            )
        else:
            pending = evidence.filter(MessagingUsageRecord.units_status != 'known').count()
            known = evidence.filter(MessagingUsageRecord.units_status == 'known')
        if period is not None:
            used = int(period.used_units or 0)
            included = int(period.included_units or 0)
            overage = int(period.overage_units or 0)
            amount = Decimal(period.sell_amount or 0)
            currency = (period.currency or _usage_currency()).lower()
        else:
            used = int(known.with_entities(db.func.coalesce(db.func.sum(MessagingUsageRecord.billable_units), 0)).scalar() or 0)
            included = included_segments_for_subscription(organization.subscription)
            overage = max(0, used - included)
            amount = Decimal(overage) * _currency_rate()
            currency = _usage_currency()
        return {
            'period_start': start, 'period_end': end,
            'used_segments': used, 'used_label': segment_count_label(used),
            'included_segments': included, 'included_label': segment_count_label(included),
            'overage_segments': overage, 'projected_overage': amount,
            'projected_overage_label': _money_label(amount, currency),
            'currency': currency, 'billed': billed,
            'pending_records': pending, 'stored_period': period is not None,
        }
