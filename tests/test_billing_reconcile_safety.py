"""Reconciliation classifies synthetic Stripe failures without provider calls."""
import importlib
import logging
from unittest.mock import Mock

import pytest
import stripe
from flask import Flask
from sqlalchemy import create_engine, inspect, text

from tests.test_billing_transitions import billing, organization, begin, checkout_event
from app.services.billing_service import _retry_stripe_operation


@pytest.mark.parametrize("error", [
    stripe.InvalidRequestError("Fixture missing", "id", code="resource_missing", http_status=404),
    stripe.AuthenticationError("Fixture authentication", http_status=401),
    stripe.PermissionError("Fixture authorization", http_status=403),
    ValueError("Fixture unexpected error"),
])
def test_permanent_or_unexpected_error_is_typed_and_never_retried(error):
    app = Flask(__name__)
    operation = Mock(side_effect=error)
    with app.app_context(), pytest.raises(type(error)) as caught:
        _retry_stripe_operation("subscription.retrieve", operation)
    assert caught.value is error
    assert operation.call_count == 1


@pytest.mark.parametrize("error", [
    stripe.APIConnectionError("Fixture connection"),
    stripe.RateLimitError("Fixture rate limit", http_status=429),
    stripe.APIError("Fixture unavailable", http_status=503),
    TimeoutError("Fixture timeout"),
])
def test_transient_errors_retry_and_recover(error, monkeypatch):
    app = Flask(__name__)
    operation = Mock(side_effect=[error, {"status": "active"}])
    sleep = Mock()
    monkeypatch.setattr("app.services.billing_service.time.sleep", sleep)
    with app.app_context():
        assert _retry_stripe_operation("subscription.retrieve", operation) == {"status": "active"}
    assert operation.call_count == 2 and sleep.call_count == 1


def _isolate_usage(billing, monkeypatch):
    monkeypatch.setattr(billing.service, "reconcile_messaging_usage", lambda: dict(
        records_seen=0, records_finalized=0, records_pending=0, records_errored=0, periods_updated=0))
    monkeypatch.setattr(billing.service, "_post_closed_usage_invoice_items", lambda: dict(
        periods_scanned=0, periods_posted=0, periods_skipped=0, periods_failed=0))


def _missing_subscription(billing):
    subscription = organization(billing).subscription
    subscription.status = "canceled"
    subscription.stripe_customer_id = "cus_fixture_missing"
    subscription.stripe_subscription_id = "sub_fixture_missing"
    subscription.billing_incident_code = "stripe_ownership_conflict"
    subscription.billing_incident_detail = "Retained incident evidence"
    billing.db.session.commit()
    billing.provider.api.Subscription.retrieve.side_effect = stripe.InvalidRequestError(
        "Fixture missing", "id", code="resource_missing", http_status=404)
    return subscription


def test_canceled_missing_is_recorded_once_and_skipped_on_later_run(billing, monkeypatch, caplog):
    _isolate_usage(billing, monkeypatch)
    subscription = _missing_subscription(billing)
    with caplog.at_level(logging.WARNING):
        first = billing.service.reconcile_billing_subscriptions()
        observed = subscription.stripe_missing_observed_at
        second = billing.service.reconcile_billing_subscriptions()
        # An explicit owner refresh still inspects newly completed checkouts.
        billing.service.refresh_subscription_from_stripe(organization(billing))
    assert first["failed"] == 0 and first["updated"] == 1
    assert second["scanned"] == second["failed"] == 0
    assert subscription.stripe_missing_subscription_id == "sub_fixture_missing"
    assert observed is not None and subscription.stripe_missing_observed_at == observed
    assert subscription.status == "canceled" and subscription.stripe_subscription_id == "sub_fixture_missing"
    assert subscription.billing_incident_code == "stripe_ownership_conflict"
    assert subscription.billing_incident_detail == "Retained incident evidence"
    assert billing.provider.api.Subscription.retrieve.call_count == 2
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1 and warnings[0].exc_info is None


@pytest.mark.parametrize("change", ["active", "replacement"])
def test_missing_marker_cannot_hide_active_or_replacement_resource_failure(billing, monkeypatch, change):
    _isolate_usage(billing, monkeypatch)
    subscription = _missing_subscription(billing)
    billing.service.reconcile_billing_subscriptions()
    if change == "active":
        subscription.status = "active"
    else:
        subscription.stripe_subscription_id = "sub_fixture_replacement"
        billing.provider.api.Subscription.retrieve.side_effect = stripe.AuthenticationError("Fixture authentication", http_status=401)
    billing.db.session.commit()
    summary = billing.service.reconcile_billing_subscriptions()
    assert summary["scanned"] == summary["failed"] == 1
    assert billing.provider.api.Subscription.retrieve.call_count == 2


@pytest.mark.parametrize("automatic", [False, True])
def test_refresh_still_recovers_newly_completed_checkout(billing, monkeypatch, automatic):
    _isolate_usage(billing, monkeypatch)
    first = begin(billing)
    billing.provider.pay(first.id)
    billing.service.process_stripe_webhook_event(checkout_event(billing, first.id))
    subscription = organization(billing).subscription
    old_id = subscription.stripe_subscription_id
    billing.provider.subscriptions[old_id]["status"] = "canceled"
    billing.service.refresh_subscription_from_stripe(organization(billing))
    later = begin(billing, "annual")
    subscription.stripe_missing_subscription_id = old_id
    subscription.stripe_missing_observed_at = billing.models.utc_now().replace(tzinfo=None)
    billing.db.session.commit()
    billing.provider.pay(later.id)
    if automatic:
        summary = billing.service.reconcile_billing_subscriptions()
        assert summary["updated"] == 1 and summary["failed"] == 0
        recovered = organization(billing).subscription
    else:
        recovered = billing.service.refresh_subscription_from_stripe(organization(billing))
    assert recovered.status == "active"
    assert recovered.stripe_subscription_id != old_id


def test_unexpected_reconcile_error_remains_visible(billing, monkeypatch):
    _isolate_usage(billing, monkeypatch)
    monkeypatch.setattr(billing.service, "refresh_subscription_from_stripe", Mock(side_effect=ValueError("Fixture failure")))
    assert billing.service.reconcile_billing_subscriptions()["failed"] == 1


def test_missing_subscription_migration_is_expand_only_and_idempotent():
    engine = create_engine("sqlite://")
    migration = importlib.import_module("app.saas_migrations.032_record_missing_canceled_subscriptions")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE organization_subscriptions (id INTEGER PRIMARY KEY, status VARCHAR(30), stripe_subscription_id VARCHAR(80))"))
        connection.execute(text("INSERT INTO organization_subscriptions VALUES (1, 'canceled', 'sub_fixture_retained')"))
        for _ in range(2):
            migration.apply(connection, logging.getLogger(__name__))
        row = connection.execute(text("SELECT * FROM organization_subscriptions")).mappings().one()
        assert dict(row) == dict(id=1, status="canceled", stripe_subscription_id="sub_fixture_retained",
                                stripe_missing_subscription_id=None, stripe_missing_observed_at=None)
        assert {column["name"] for column in inspect(connection).get_columns("organization_subscriptions")} == set(row)
    engine.dispose()
