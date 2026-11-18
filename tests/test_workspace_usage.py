"""Workspace usage is a tenant-bound, read-only view of retained segment facts."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from app.services.twilio_service import _period_window
from app.services.usage_summary_service import workspace_usage_summary
from app.tenant import organization_context
from tests.test_ux_recovery import ux, login


def _window():
    return tuple(value.replace(tzinfo=None) for value in _period_window(None))


def _mode(ux, mode):
    organization = ux.db.session.get(ux.models.Organization, ux.org_id)
    organization.subscription.status = 'complimentary' if mode == 'complimentary' else 'active'
    organization.subscription.activation_fee_paid_at = ux.models.utc_now()
    organization.subscription.stripe_price_id = 'price_fixture_custom'
    organization.messaging_profile.provider_mode = 'customer_managed' if mode == 'customer_managed' else 'platform_managed'
    ux.app.config.update(BILLING_INCLUDED_OUTBOUND_SEGMENTS=1000, BILLING_OUTBOUND_SEGMENT_RATE_USD='0.0300')
    ux.db.session.commit()
    return organization


def _record(ux, identity, segments, **changes):
    start, end = _window()
    values = dict(organization_id=ux.org_id, message_sid='SMfixture-usage-' + identity,
        billable_units=segments, billable=True, direction='outbound', cost_status='known', units_status='known',
        billing_period_start=start, billing_period_end=end, historical_observation_only=False,
        created_at=start + timedelta(days=1))
    values.update(changes)
    record = ux.models.MessagingUsageRecord(**values)
    ux.db.session.add(record)
    return record


def _period(ux, organization_id, **changes):
    start, end = _window()
    values = dict(organization_id=organization_id, period_start=start, period_end=end,
        used_units=7060, included_units=1234, overage_units=5826, sell_amount=Decimal('174.78'), currency='usd')
    values.update(changes)
    period = ux.models.OrganizationUsageBillingPeriod(**values)
    ux.db.session.add(period)
    return period


@pytest.mark.parametrize('mode', ['billed', 'complimentary', 'customer_managed'])
def test_stored_current_period_is_authoritative_and_shared_by_send_and_billing(ux, monkeypatch, mode):
    organization = _mode(ux, mode)
    _period(ux, ux.org_id)
    _period(ux, ux.other_id, used_units=999999, sell_amount=Decimal('9999.99'))
    _record(ux, 'disagreeing-observation', 3)
    ux.db.session.commit()
    stripe = Mock(side_effect=AssertionError('No Stripe access is allowed for usage display'))
    provider = Mock(side_effect=AssertionError('No provider access is allowed for usage display'))
    monkeypatch.setattr('app.services.billing_service._stripe_module', stripe)
    monkeypatch.setattr('app.services.twilio_service._client_for_usage_reconciliation', provider)
    summary = workspace_usage_summary(organization)
    assert summary['used_segments'] == 7060 and summary['included_segments'] == 1234
    assert summary['overage_segments'] == 5826 and summary['projected_overage'] == Decimal('174.78')
    assert summary['stored_period'] is True and summary['billed'] is (mode == 'billed')
    login(ux)
    for route in ('/dashboard', '/billing'):
        response = ux.client.get(route)
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert '7,060 segments used this period' in text and '999,999' not in text
        if mode == 'billed':
            assert '1,234 segments included' in text and '$174.78 projected overage' in text
        else:
            assert 'Not billed by Twinevia' in text
            assert 'projected overage' not in text and 'SMS segments included' not in text
            # Encoding explanations can discuss characters per segment; an
            # inapplicable monetary overage field must still be absent.
            assert 'included SMS segments per month' not in text and '>Overage<' not in text
        if mode == 'complimentary':
            assert 'Complimentary ·' not in text
    stripe.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize('mode,used,amount', [('billed', 1100, Decimal('3.00')),
                                            ('complimentary', 10104, Decimal('273.12')),
                                            ('customer_managed', 10104, Decimal('273.12'))])
def test_fallback_matches_settlement_facts_for_billed_and_all_known_usage_for_not_billed(ux, mode, used, amount):
    organization = _mode(ux, mode)
    start, end = _window()
    _record(ux, 'billable', 1100)
    _record(ux, 'not-billable', 9000, billable=False)
    _record(ux, 'cost-pending', 4, cost_status='pending')
    _record(ux, 'segments-pending', 5000, units_status='pending')
    _record(ux, 'other-workspace', 99999, organization_id=ux.other_id)
    _record(ux, 'inbound', 99999, direction='inbound')
    _record(ux, 'assigned-elsewhere', 99999, billing_period_start=start-timedelta(days=35), billing_period_end=start,
            created_at=start+timedelta(days=1))
    _record(ux, 'unassigned-previous-month', 99999, billing_period_start=None, billing_period_end=None,
            created_at=start-timedelta(days=1))
    ux.db.session.commit()
    summary = workspace_usage_summary(organization)
    assert summary['stored_period'] is False
    assert summary['used_segments'] == used and summary['projected_overage'] == amount
    assert summary['pending_records'] == (2 if mode == 'billed' else 1)
    assert ux.models.OrganizationUsageBillingPeriod.query.count() == 0


def test_unassigned_current_usage_is_visible_without_assigning_a_period(ux):
    organization = _mode(ux, 'complimentary')
    record = _record(ux, 'unassigned', 17, billing_period_start=None, billing_period_end=None)
    ux.db.session.commit()
    assert workspace_usage_summary(organization)['used_segments'] == 17
    assert record.billing_period_start is None and record.billing_period_end is None


def test_usage_read_performs_no_writes_or_autoflush_and_rejects_other_active_tenant(ux):
    organization = _mode(ux, 'billed')
    _record(ux, 'retained', 1100)
    ux.db.session.commit()
    other = ux.db.session.get(ux.models.Organization, ux.other_id)
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(ux.db.engine, 'before_cursor_execute', capture)
    try:
        # A display read must not flush even unrelated caller-owned changes.
        organization.name = 'Unsaved caller-owned name'
        with organization_context(ux.org_id):
            assert workspace_usage_summary(organization)['used_segments'] == 1100
            with pytest.raises(ValueError, match='another workspace'):
                workspace_usage_summary(other)
        assert not [statement for statement in statements if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
    finally:
        event.remove(ux.db.engine, 'before_cursor_execute', capture)
        ux.db.session.rollback()


def test_pending_usage_caveat_is_visible_on_both_surfaces(ux):
    _mode(ux, 'billed')
    _record(ux, 'pending-cost', 7, cost_status='pending')
    ux.db.session.commit()
    login(ux)
    for route in ('/dashboard', '/billing'):
        response = ux.client.get(route)
        assert response.status_code == 200
        assert b'Some usage is still being confirmed' in response.data
    assert b'Estimated SMS Segments' in ux.client.get('/dashboard').data


def test_newly_accepted_unconfirmed_usage_is_pending_before_it_becomes_billable(ux, monkeypatch):
    organization = _mode(ux, 'billed')
    from app.services.twilio_service import record_usage_candidates
    provider = Mock(side_effect=AssertionError('Usage display must not contact the provider'))
    monkeypatch.setattr('app.services.twilio_service._client_for_usage_reconciliation', provider)
    monkeypatch.setattr('app.services.twilio_service._client_for_profile', provider)
    # Fully known non-billable traffic is excluded without becoming pending.
    _record(ux, 'complete-not-billable', 5, billable=False)
    ux.db.session.commit()
    assert record_usage_candidates(ux.org_id, [{
        'sid': 'SMfixture-newly-accepted-pending', 'success': True, 'status': 'queued',
        'num_segments': None, 'provider_price': None, 'provider_currency': 'usd',
    }]) == 1
    record = ux.models.MessagingUsageRecord.query.filter_by(message_sid='SMfixture-newly-accepted-pending').one()
    assert record.billable is False
    assert record.units_status == record.cost_status == 'pending'
    assert record.billing_period_start is None and record.billing_period_end is None
    summary = workspace_usage_summary(organization)
    assert summary['pending_records'] == 1
    assert summary['used_segments'] == 0 and summary['projected_overage'] == Decimal('0')
    login(ux)
    for route in ('/dashboard', '/billing'):
        response = ux.client.get(route)
        assert response.status_code == 200
        assert b'Some usage is still being confirmed' in response.data
    assert ux.models.OrganizationUsageBillingPeriod.query.count() == 0
    assert record.billable is False and record.units_status == record.cost_status == 'pending'
    provider.assert_not_called()


def test_report_origin_month_scope_and_partial_segment_denominator(ux):
    from app.services.usage_summary_service import workspace_monthly_report
    organization = _mode(ux, 'billed')
    start, end = _window()
    for identity, source, segments, automated in [
        ('reminder', 'scheduled', 4, True), ('manual', 'scheduled', 2, False),
        ('inbox', 'reply', 1, None), ('keyword', 'keyword', 3, None), ('old', 'scheduled', 2, None),
    ]:
        usage = _record(ux, identity, segments, source=source)
        if automated is not None:
            schedule = ux.models.ScheduledMessage(organization_id=ux.org_id, scheduled_at=start,
                status='sent', message_body='fixture', target='community',
                automation_source='event_sync' if automated else None)
            ux.db.session.add(schedule)
            ux.db.session.flush()
            ux.db.session.add(ux.models.MessageDispatchAttempt(organization_id=ux.org_id, scheduled_message_id=schedule.id,
                provider_message_sid=usage.message_sid, logical_send_key=identity, recipient_phone='+17205550109',
                message_body_digest='a'*64, send_kind='blast', status='sent'))
    _record(ux, 'partial', 99, units_status='pending', source='blast')
    _record(ux, 'other', 200, organization_id=ux.other_id)
    _record(ux, 'prior', 200, billing_period_start=None, billing_period_end=None, created_at=start-timedelta(seconds=1))
    ux.db.session.commit()
    with organization_context(ux.org_id):
        report = workspace_monthly_report(organization, start)
    amounts = {row['label']: (row['messages'], row['segments'], row['average']) for row in report['rows']}
    assert amounts == {'Automatic':(2,7,3.5), 'Sent immediately':(2,1,1), 'Scheduled by a person':(1,2,2), 'Origin unconfirmed':(1,2,2)}
    assert report['messages'] == 6 and report['segments'] == 12 and report['average'] == 12/5
    login(ux)
    assert b'Origin unconfirmed' in ux.client.get('/billing/usage').data
    assert ux.client.get('/billing/usage?month=invalid').status_code == 400
    assert ux.client.get('/billing/usage?month=2099-09').status_code == 400
    assert b'Origin unconfirmed' not in ux.client.get('/billing/usage?month=2000-01').data
    login(ux, role='staff')
    assert ux.client.get('/billing/usage').status_code == 403
