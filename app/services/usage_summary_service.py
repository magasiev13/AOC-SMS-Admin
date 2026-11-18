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


def workspace_monthly_report(organization, reference_time):
    from sqlalchemy import case
    from app.models import MessageDispatchAttempt, ScheduledMessage
    from app.services.aoc_event_sync_service import AOC_AUTOMATION_SOURCE, EVENT_SYNC_AUTOMATION_SOURCE
    from app.services.billing_plans import billing_plan_for_subscription

    summary = workspace_usage_summary(organization, reference_time=reference_time)
    usage, attempt, schedule = MessagingUsageRecord, MessageDispatchAttempt, ScheduledMessage
    query = _period_usage_query(SimpleNamespace(
        organization_id=organization.id, period_start=summary['period_start'], period_end=summary['period_end'],
    )).filter(usage.direction == 'outbound')
    # Both SID relationships are unique. Org predicates keep mismatched historical
    # references from attributing activity to another workspace.
    query = query.outerjoin(attempt, db.and_(attempt.provider_message_sid == usage.message_sid,
                                              attempt.organization_id == usage.organization_id))
    query = query.outerjoin(schedule, db.and_(schedule.id == attempt.scheduled_message_id,
                                               schedule.organization_id == usage.organization_id))
    origin = case(
        (schedule.automation_source.in_([AOC_AUTOMATION_SOURCE, EVENT_SYNC_AUTOMATION_SOURCE]), 'Automatic'),
        (db.and_(schedule.id.is_not(None), schedule.automation_source.is_(None)), 'Scheduled by a person'),
        (schedule.automation_source == 'feedback_invitation', 'Reviewed feedback invitation'),
        (usage.source.in_(['system', 'survey', 'keyword', 'auth_alert']), 'Automatic'),
        (usage.source.in_(['blast', 'reply', 'operational_test']), 'Sent immediately'),
        else_='Origin unconfirmed',
    )
    known = usage.units_status == 'known'
    results = query.with_entities(origin, db.func.count(usage.id),
        db.func.sum(case((known, 1), else_=0)), db.func.sum(case((known, usage.billable_units), else_=0)),
    ).group_by(origin).all()
    amounts = {label: (int(messages), int(known_messages), int(segments))
               for label, messages, known_messages, segments in results}
    rows = []
    for label in ('Automatic', 'Sent immediately', 'Scheduled by a person', 'Reviewed feedback invitation', 'Origin unconfirmed'):
        messages, known_count, segments = amounts.get(label, (0, 0, 0))
        if messages or label in ('Automatic', 'Sent immediately', 'Scheduled by a person'):
            rows.append({'label': label, 'messages': messages, 'segments': segments, 'pending': messages - known_count,
                         'average': segments / known_count if known_count else None})
    total_messages = sum(row['messages'] for row in rows)
    total_segments = sum(row['segments'] for row in rows)
    known_count = total_messages - sum(row['pending'] for row in rows)
    return dict(summary=summary, rows=rows, messages=total_messages, segments=total_segments,
                average=total_segments / known_count if known_count else None,
                plan=billing_plan_for_subscription(organization.subscription))
