"""Organization-owned billing transitions; Stripe is replaced only at its API boundary."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import os
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from unittest.mock import MagicMock, patch


POSTGRES_ROOT = "postgresql://audit@127.0.0.1:55432/postgres"


class StripeBoundary:
    def __init__(self):
        self.sessions, self.requests, self.subscriptions = {}, {}, {}
        self.create_unknown = False
        self.retrieve_unknown = False
        self.expire_mode = None
        self.on_create = None
        self.on_expire = None
        self.api = SimpleNamespace(
            checkout=SimpleNamespace(Session=SimpleNamespace(
                create=MagicMock(side_effect=self.create), retrieve=MagicMock(side_effect=self.retrieve),
                expire=MagicMock(side_effect=self.expire), list=MagicMock(side_effect=self.list_sessions),
                list_line_items=MagicMock(side_effect=self.line_items),
            )),
            Subscription=SimpleNamespace(
                retrieve=MagicMock(side_effect=lambda key: deepcopy(self.subscriptions[key])),
                list=MagicMock(side_effect=lambda **kw: {"data": [deepcopy(row) for row in self.subscriptions.values() if row["customer"] == kw["customer"]], "has_more": False}),
            ),
            Invoice=SimpleNamespace(retrieve=MagicMock(side_effect=self.invoice)),
            PaymentIntent=SimpleNamespace(retrieve=MagicMock()),
        )

    def create(self, **params):
        key = params.pop("idempotency_key")
        if key not in self.requests:
            session_id = "cs_test_" + uuid4().hex
            self.requests[key] = deepcopy(params)
            self.sessions[session_id] = dict(
                deepcopy(params), id=session_id, status="open", payment_status="unpaid",
                url="https://checkout.stripe.test/" + session_id, request_key=key,
            )
        else:
            assert self.requests[key] == params, "An idempotency retry changed its original parameters"
            session_id = next(sid for sid, row in self.sessions.items() if row["request_key"] == key)
        if self.on_create:
            self.on_create(key, params)
        if self.create_unknown:
            raise TimeoutError("Controlled create accepted; response lost")
        return SimpleNamespace(**deepcopy(self.sessions[session_id]))

    def retrieve(self, key):
        if self.retrieve_unknown:
            raise TimeoutError("Controlled retrieval unavailable")
        return SimpleNamespace(**deepcopy(self.sessions[key]))

    def expire(self, key, **options):
        assert options["idempotency_key"] == "twinevia-expire-" + key
        if self.expire_mode == "payment_wins":
            self.pay(key)
            if self.on_expire:
                self.on_expire(key)
            raise RuntimeError("Checkout is already complete")
        if self.expire_mode == "unknown":
            raise TimeoutError("Controlled expiration unconfirmed")
        self.sessions[key]["status"] = "expired"
        self.sessions[key]["url"] = None
        if self.expire_mode == "accepted_then_timeout":
            raise TimeoutError("Controlled expiration accepted; response lost")
        return deepcopy(self.sessions[key])

    def list_sessions(self, **params):
        rows = [deepcopy(row) for row in self.sessions.values()]
        if params.get("status"):
            rows = [row for row in rows if row["status"] == params["status"]]
        return {"data": rows, "has_more": False}

    def line_items(self, key, **params):
        return {"data": [{"price": {"id": row["price"]}, "quantity": row["quantity"]} for row in self.sessions[key]["line_items"]]}

    def pay(self, key, *, subscription_id=None):
        session = self.sessions[key]
        session.update(status="complete", payment_status="paid", customer="cus_transition", currency="usd")
        if session["mode"] == "payment":
            session.update(amount_total=14999, payment_intent={"id": "pi_" + key, "status": "succeeded", "currency": "usd", "amount_received": 14999})
        else:
            subscription_id = subscription_id or "sub_" + key
            session.update(subscription=subscription_id, invoice="in_" + key)
            self.subscriptions[subscription_id] = {
                "id": subscription_id, "customer": "cus_transition", "status": "active",
                "metadata": deepcopy(session["metadata"]),
                "items": {"data": [{"price": {"id": session["line_items"][-1]["price"]}}]},
            }

    def invoice(self, key, **params):
        session_id = key.removeprefix("in_")
        session = self.sessions[session_id]
        return {
            "id": key, "paid": True, "status": "paid", "amount_paid": 100000,
            "payment_intent": {"id": "pi_" + session_id, "status": "succeeded", "amount_received": 100000},
            "lines": self.line_items(session_id),
        }


@pytest.fixture
def billing(tmp_path, monkeypatch):
    from app import db, models
    from app.services import billing_service as service
    from app.tenant import init_tenant_scoping

    postgres_root = os.environ.get("BILLING_TEST_POSTGRES_ROOT")
    name = None
    if postgres_root:
        if postgres_root != POSTGRES_ROOT:
            raise RuntimeError("Billing tests accept only the explicit isolated loopback PostgreSQL root.")
        import psycopg
        from psycopg import sql

        name = "billing_transition_" + uuid4().hex
        with psycopg.connect(postgres_root, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(name)))
        uri = "postgresql+psycopg://audit@127.0.0.1:55432/" + name
    else:
        uri = "sqlite:///" + str(tmp_path / "billing.db")
    app = Flask(__name__)
    app.config.update(
        TESTING=True, SAAS_MODE=True, SQLALCHEMY_DATABASE_URI=uri, SECRET_KEY="isolated-billing",
        SQLALCHEMY_TRACK_MODIFICATIONS=False, STRIPE_SECRET_KEY="sk_test_unused",
        STRIPE_MONTHLY_PRICE_ID="price_monthly", STRIPE_PRICE_ID="price_monthly",
        STRIPE_ANNUAL_PRICE_ID="price_annual", STRIPE_ACTIVATION_PRICE_ID="price_setup",
        BILLING_OFFER_VERSION="transition-v1", STRIPE_CONFIGURATION_VALIDATION_ATTEMPTS=1,
    )
    db.init_app(app)
    init_tenant_scoping()
    boundary = StripeBoundary()
    original_stripe_module = service._stripe_module
    monkeypatch.setattr(service, "_stripe_module", lambda: boundary.api)
    try:
        with app.app_context():
            try:
                db.create_all()
                org = models.Organization(name="Billing transition", slug="billing-transition")
                other = models.Organization(name="Other billing tenant", slug="other-billing-transition")
                subscription = models.OrganizationSubscription(organization=org, status="incomplete", stripe_price_id="price_monthly")
                db.session.add_all([org, other, subscription])
                db.session.commit()
                yield SimpleNamespace(app=app, db=db, models=models, service=service, provider=boundary,
                                      organization_id=org.id, other_id=other.id, postgres=bool(postgres_root),
                                      original_stripe_module=original_stripe_module)
            finally:
                db.session.remove()
                db.engine.dispose()
    finally:
        if name:
            import psycopg
            from psycopg import sql

            with psycopg.connect(POSTGRES_ROOT, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def organization(billing):
    return billing.db.session.get(billing.models.Organization, billing.organization_id)


def begin(billing, plan="monthly", **kwargs):
    return billing.service.create_checkout_session(
        organization(billing), kwargs.get("email", "owner@example.test"),
        kwargs.get("success", "https://app.example.test/return?session_id={CHECKOUT_SESSION_ID}"),
        kwargs.get("cancel", "https://app.example.test/billing"), plan_code=plan,
    )


def open_sessions(billing):
    return [row for row in billing.provider.sessions.values() if row["status"] == "open"]


def test_monthly_annual_monthly_has_one_payable_url_and_each_previous_url_is_expired(billing):
    sessions = [begin(billing, plan) for plan in ["monthly", "annual", "monthly"]]
    assert len({session.id for session in sessions}) == 3
    assert [billing.provider.sessions[session.id]["status"] for session in sessions] == ["expired", "expired", "open"]
    assert len(open_sessions(billing)) == 1
    assert billing.provider.api.checkout.Session.expire.call_count == 2
    assert billing.models.StripeCheckoutSession.query.filter_by(status="open").count() == 1


def test_same_plan_reuses_session_and_does_not_change_original_checkout_terms(billing):
    first = begin(billing)
    second = begin(billing, email="changed@example.test", success="https://app.example.test/new-return")
    assert first.id == second.id
    assert billing.provider.api.checkout.Session.create.call_count == 1
    assert billing.models.StripeCheckoutSession.query.count() == 1


def test_stripe_boundary_uses_instance_credentials_and_request_options_without_global_mutation(billing):
    import stripe

    with patch.object(stripe, "api_key", "unchanged-global-key"), patch.object(stripe, "StripeClient") as factory:
        api = billing.original_stripe_module()
        api.checkout.Session.create(mode="subscription", idempotency_key="saved-key")
        api.checkout.Session.expire("cs_saved", idempotency_key="expire-saved")
        api.Account.retrieve()
        factory.assert_called_once_with("sk_test_unused")
        factory.return_value.checkout.sessions.create.assert_called_once_with(
            params={"mode": "subscription"}, options={"idempotency_key": "saved-key"},
        )
        factory.return_value.checkout.sessions.expire.assert_called_once_with(
            "cs_saved", params={}, options={"idempotency_key": "expire-saved"},
        )
        factory.return_value.accounts.retrieve_current.assert_called_once_with(params={}, options={})
        assert stripe.api_key == "unchanged-global-key"


def test_checkout_request_commits_frozen_identity_before_first_stripe_call(billing):
    def observe(key, params):
        with Session(billing.db.engine) as observer:
            saved = observer.scalar(select(billing.models.OrganizationSubscription).where(
                billing.models.OrganizationSubscription.organization_id == billing.organization_id,
            ))
            assert saved.checkout_request_key == key
            assert saved.checkout_request_status == "creating"
            assert json.loads(saved.checkout_request_payload_json)["params"] == params

    billing.provider.on_create = observe
    begin(billing)


def test_lost_create_response_reuses_committed_key_and_exact_parameters(billing):
    billing.provider.create_unknown = True
    with pytest.raises(billing.service.BillingTransitionError, match="request is saved"):
        begin(billing)
    subscription = organization(billing).subscription
    key, payload = subscription.checkout_request_key, subscription.checkout_request_payload_json
    assert subscription.checkout_request_status == "unknown"
    assert len(billing.provider.requests) == len(open_sessions(billing)) == 1
    billing.db.session.remove()
    billing.provider.create_unknown = False
    recovered = begin(billing, email="different@example.test", success="https://app.example.test/reloaded")
    assert recovered.id in billing.provider.sessions
    assert organization(billing).subscription.checkout_request_key == key
    assert organization(billing).subscription.checkout_request_payload_json == payload
    assert len(billing.provider.requests) == len(open_sessions(billing)) == 1


def test_different_plan_cannot_bypass_unconfirmed_creation(billing):
    billing.provider.create_unknown = True
    with pytest.raises(billing.service.BillingTransitionError):
        begin(billing)
    original_key = organization(billing).subscription.checkout_request_key
    with pytest.raises(billing.service.BillingTransitionError):
        begin(billing, "annual")
    assert set(billing.provider.requests) == {original_key}
    assert len(open_sessions(billing)) == 1


def test_database_failure_after_remote_create_preserves_original_request_for_recovery(billing):
    commit = billing.db.session.commit
    calls = 0

    def fail_record_once():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Controlled database failure after Stripe accepted")
        return commit()

    with patch.object(billing.db.session, "commit", side_effect=fail_record_once):
        with pytest.raises(billing.service.BillingTransitionError, match="could not be recorded"):
            begin(billing)
    key = organization(billing).subscription.checkout_request_key
    assert organization(billing).subscription.checkout_request_status == "unknown"
    recovered = begin(billing)
    assert recovered.id in billing.provider.sessions
    assert set(billing.provider.requests) == {key}
    assert len(open_sessions(billing)) == 1


def test_unknown_create_cannot_be_reissued_after_stripe_idempotency_retention_window(billing):
    billing.provider.create_unknown = True
    with pytest.raises(billing.service.BillingTransitionError):
        begin(billing)
    organization(billing).subscription.checkout_request_started_at = billing.models.utc_now() - timedelta(hours=25)
    billing.db.session.commit()
    original_calls = billing.provider.api.checkout.Session.create.call_count
    billing.provider.create_unknown = False
    with pytest.raises(billing.service.BillingTransitionError, match="retry window has ended"):
        begin(billing)
    assert billing.provider.api.checkout.Session.create.call_count == original_calls


@pytest.mark.parametrize("action", ["plan", "complimentary", "offer"])
def test_unknown_expiration_blocks_every_conflicting_transition(billing, action):
    first = begin(billing)
    billing.provider.expire_mode = "unknown"
    with pytest.raises(billing.service.BillingTransitionError, match="expiration is unconfirmed"):
        if action == "plan":
            begin(billing, "annual")
        elif action == "complimentary":
            billing.service.mark_subscription_complimentary(organization(billing))
        else:
            billing.service.update_organization_billing_offer(organization(billing), "annual_only")
    assert billing.provider.sessions[first.id]["status"] == "open"
    assert len(billing.provider.requests) == 1
    assert organization(billing).subscription.status == "incomplete"
    assert organization(billing).subscription.billing_incident_code == "checkout_expiry_unconfirmed"


def test_expiration_accepted_with_response_loss_is_verified_before_new_plan(billing):
    first = begin(billing)
    billing.provider.expire_mode = "accepted_then_timeout"
    second = begin(billing, "annual")
    assert first.id != second.id
    assert billing.provider.sessions[first.id]["status"] == "expired"
    assert len(open_sessions(billing)) == 1


def test_complimentary_conversion_expires_all_payable_sessions_first(billing):
    first = begin(billing)
    subscription = billing.service.mark_subscription_complimentary(organization(billing))
    assert subscription.status == "complimentary"
    assert billing.provider.sessions[first.id]["status"] == "expired"
    assert open_sessions(billing) == []
    with pytest.raises(billing.service.BillingTransitionError, match="Complimentary"):
        begin(billing)


@pytest.mark.parametrize("action", ["plan", "complimentary", "offer"])
def test_payment_winning_expiration_race_blocks_conflicting_transition(billing, action):
    first = begin(billing)
    billing.provider.expire_mode = "payment_wins"
    with pytest.raises(billing.service.BillingTransitionError, match="payment was reconciled"):
        if action == "plan":
            begin(billing, "annual")
        elif action == "complimentary":
            billing.service.mark_subscription_complimentary(organization(billing))
        else:
            billing.service.update_organization_billing_offer(organization(billing), "annual_only")
    subscription = organization(billing).subscription
    assert subscription.status == "active"
    assert subscription.activation_payment_intent_id == "pi_" + first.id
    assert len(billing.provider.requests) == 1
    assert organization(billing).billing_offer == "standard"


def test_completed_unpaid_session_blocks_complimentary_without_granting_access(billing):
    first = begin(billing)
    billing.provider.sessions[first.id].update(status="complete", payment_status="unpaid")
    with pytest.raises(billing.service.BillingTransitionError, match="payment confirmation is pending"):
        billing.service.mark_subscription_complimentary(organization(billing))
    assert organization(billing).subscription.status == "incomplete"
    assert organization(billing).subscription.billing_incident_code == "checkout_payment_pending"


def test_unexpected_subscription_never_replaces_existing_identity_and_persists_incident(billing):
    subscription = organization(billing).subscription
    subscription.stripe_customer_id = "cus_transition"
    subscription.stripe_subscription_id = "sub_owned"
    subscription.status = "active"
    billing.db.session.commit()
    with pytest.raises(billing.service.StripePaymentProofError, match="already owns sub_owned"):
        billing.service.sync_subscription_from_event("customer.subscription.updated", {
            "id": "sub_unexpected", "customer": "cus_transition", "status": "active",
            "metadata": {"organization_id": str(billing.organization_id)},
        }, billing.models.utc_now(), "evt_unexpected", True)
    billing.db.session.rollback()
    billing.db.session.refresh(subscription)
    assert subscription.stripe_subscription_id == "sub_owned" and subscription.status == "active"
    assert subscription.billing_incident_code == "unexpected_subscription"


def test_authoritative_remote_read_cannot_apply_an_older_event_over_newer_state(billing):
    now = billing.models.utc_now()
    body = {"id": "sub_owned", "customer": "cus_transition", "status": "canceled", "metadata": {"organization_id": str(billing.organization_id)}}
    billing.service.sync_subscription_from_event("customer.subscription.updated", body, now, "evt_newer", True)
    with pytest.raises(billing.service.StaleStripeEventError):
        billing.service.sync_subscription_from_event("customer.subscription.updated", dict(body, status="active"), now - timedelta(seconds=1), "evt_older", True)
    billing.db.session.rollback()
    assert organization(billing).subscription.status == "canceled"


def test_billing_transition_rejects_a_different_active_tenant_context(billing):
    from app.tenant import organization_context

    with organization_context(billing.other_id), pytest.raises(billing.service.StripePaymentProofError, match="another organization"):
        begin(billing)
    assert billing.provider.api.checkout.Session.create.call_count == 0


def checkout_event(billing, session_id, event_id="evt_checkout", *, created=None):
    return {"id": event_id, "type": "checkout.session.completed",
            "created": created or int(billing.models.utc_now().timestamp()),
            "data": {"object": deepcopy(billing.provider.sessions[session_id])}}


def test_paid_webhook_recovers_checkout_whose_create_response_was_never_recorded(billing):
    billing.provider.create_unknown = True
    with pytest.raises(billing.service.BillingTransitionError):
        begin(billing)
    session_id = next(iter(billing.provider.sessions))
    assert billing.models.StripeCheckoutSession.query.count() == 0
    billing.provider.pay(session_id)
    # Provider idempotency retention has elapsed, but a verified existing
    # resource still resolves the saved request without another create.
    organization(billing).subscription.checkout_request_started_at = billing.models.utc_now() - timedelta(days=2)
    billing.db.session.commit()
    record = billing.service.process_stripe_webhook_event(checkout_event(billing, session_id))
    assert record.status == "processed"
    subscription = organization(billing).subscription
    assert subscription.status == "active" and subscription.checkout_request_status == "completed"
    assert subscription.checkout_request_session_id == session_id
    assert subscription.activation_payment_intent_id == "pi_" + session_id
    assert billing.models.StripeCheckoutSession.query.one().transition_key == subscription.checkout_request_key
    assert len(billing.provider.requests) == 1


def test_setup_fee_proof_is_preserved_on_duplicate_checkout_webhooks_and_later_resubscribe(billing):
    first = begin(billing)
    billing.provider.pay(first.id)
    event = checkout_event(billing, first.id)
    billing.service.process_stripe_webhook_event(event)
    subscription = organization(billing).subscription
    original_proof = (subscription.activation_fee_paid_at, subscription.activation_payment_intent_id, subscription.activation_invoice_id)
    billing.service.process_stripe_webhook_event(event)
    assert (subscription.activation_fee_paid_at, subscription.activation_payment_intent_id, subscription.activation_invoice_id) == original_proof
    old_subscription = subscription.stripe_subscription_id
    billing.provider.subscriptions[old_subscription]["status"] = "canceled"
    billing.service.refresh_subscription_from_stripe(organization(billing))
    later = begin(billing, "annual")
    assert billing.provider.sessions[later.id]["line_items"] == [{"price": "price_annual", "quantity": 1}]
    billing.provider.pay(later.id)
    billing.service.process_stripe_webhook_event(checkout_event(billing, later.id, "evt_later_checkout"))
    assert organization(billing).subscription.stripe_subscription_id != old_subscription
    assert (subscription.activation_fee_paid_at, subscription.activation_payment_intent_id, subscription.activation_invoice_id) == original_proof
    current_subscription = subscription.stripe_subscription_id
    late_checkout = checkout_event(billing, first.id, "evt_old_checkout_replay")
    assert billing.service.process_stripe_webhook_event(late_checkout).status == "ignored"
    late_deleted = {"id": "evt_old_subscription_deleted", "type": "customer.subscription.deleted",
                    "created": int(billing.models.utc_now().timestamp()),
                    "data": {"object": deepcopy(billing.provider.subscriptions[old_subscription])}}
    assert billing.service.process_stripe_webhook_event(late_deleted).status == "ignored"
    assert subscription.stripe_subscription_id == current_subscription
    assert subscription.billing_incident_code is None


def test_asynchronous_checkout_completion_waits_for_verified_success(billing):
    first = begin(billing)
    billing.provider.sessions[first.id]["status"] = "complete"
    pending_event = checkout_event(billing, first.id, "evt_async_pending")
    assert billing.service.process_stripe_webhook_event(pending_event).status == "processed"
    assert organization(billing).subscription.status == "incomplete"
    assert organization(billing).subscription.activation_payment_intent_id is None
    assert organization(billing).subscription.checkout_request_status == "payment_pending"
    billing.provider.pay(first.id)
    paid_event = checkout_event(billing, first.id, "evt_async_paid")
    paid_event["type"] = "checkout.session.async_payment_succeeded"
    assert billing.service.process_stripe_webhook_event(paid_event).status == "processed"
    assert organization(billing).subscription.status == "active"
    assert organization(billing).subscription.activation_payment_intent_id == "pi_" + first.id


def test_asynchronous_failure_is_visible_and_never_creates_a_replacement_payment(billing):
    first = begin(billing)
    billing.provider.sessions[first.id]["status"] = "complete"
    event = checkout_event(billing, first.id, "evt_async_failure")
    event["type"] = "checkout.session.async_payment_failed"
    assert billing.service.process_stripe_webhook_event(event).status == "processed"
    assert "payment failure" in organization(billing).subscription.billing_incident_detail
    with pytest.raises(billing.service.BillingTransitionError):
        begin(billing, "annual")
    assert len(billing.provider.requests) == 1
    assert organization(billing).subscription.status == "incomplete"


@pytest.mark.parametrize("recovery", ["existing_invoice_webhook", "periodic_refresh"])
def test_paid_invoice_resolves_lagging_checkout_flag_without_additional_webhook_configuration(billing, recovery):
    first = begin(billing)
    billing.provider.pay(first.id)
    # Stripe's subscription and invoice are associated with the issued session,
    # but its payment flag still lags behind the definitive invoice proof.
    billing.provider.sessions[first.id]["payment_status"] = "unpaid"
    paid = {"value": False}
    original_invoice = billing.provider.invoice

    def invoice_state(key, **params):
        result = original_invoice(key, **params)
        if not paid["value"]:
            result.update(paid=False, status="open")
        return result

    billing.provider.api.Invoice.retrieve.side_effect = invoice_state
    pending = checkout_event(billing, first.id, "evt_initial_complete_unpaid")
    assert billing.service.process_stripe_webhook_event(pending).status == "processed"
    subscription = organization(billing).subscription
    assert subscription.activation_fee_paid_at is None
    assert subscription.checkout_request_status == "payment_pending"
    paid["value"] = True
    if recovery == "existing_invoice_webhook":
        event = {"id": "evt_initial_invoice_paid", "type": "invoice.payment_succeeded",
                 "created": int(billing.models.utc_now().timestamp()),
                 "data": {"object": {"id": "in_" + first.id, "customer": "cus_transition", "subscription": "sub_" + first.id}}}
        assert billing.service.process_stripe_webhook_event(event).status == "processed"
    else:
        billing.service.refresh_subscription_from_stripe(organization(billing))
    assert subscription.status == "active" and subscription.checkout_request_status == "completed"
    assert subscription.activation_payment_intent_id == "pi_" + first.id
    assert subscription.stripe_subscription_id == "sub_" + first.id
    assert len(billing.provider.requests) == 1
    assert billing.provider.sessions[first.id]["payment_status"] == "unpaid"


def test_expired_checkout_webhook_confirms_remote_expiration_without_payment(billing):
    first = begin(billing)
    billing.provider.api.checkout.Session.expire(first.id, idempotency_key="twinevia-expire-" + first.id)
    event = checkout_event(billing, first.id, "evt_expired")
    event["type"] = "checkout.session.expired"
    assert billing.service.process_stripe_webhook_event(event).status == "processed"
    assert organization(billing).subscription.checkout_request_status == "expired"
    assert organization(billing).subscription.activation_payment_intent_id is None
    next_session = begin(billing, "annual")
    assert next_session.id != first.id and len(open_sessions(billing)) == 1


def test_expected_webhook_does_not_erase_unexpected_subscription_incident(billing):
    subscription = organization(billing).subscription
    subscription.stripe_subscription_id = "sub_owned"
    subscription.stripe_customer_id = "cus_transition"
    subscription.billing_incident_code = "unexpected_subscription"
    subscription.billing_incident_detail = "A separate subscription needs operator review."
    billing.db.session.commit()
    billing.service.sync_subscription_from_event("customer.subscription.updated", {
        "id": "sub_owned", "customer": "cus_transition", "status": "active",
        "metadata": {"organization_id": str(billing.organization_id)},
    }, billing.models.utc_now(), "evt_expected", True)
    assert subscription.billing_incident_code == "unexpected_subscription"
    assert subscription.stripe_subscription_id == "sub_owned"


def test_billing_view_exposes_incident_without_claiming_provider_readiness(billing):
    from app.routes import _billing_surface_view, _subscription_view

    subscription = organization(billing).subscription
    subscription.billing_incident_code = "unexpected_subscription"
    subscription.billing_incident_detail = "An existing Stripe subscription needs platform review."
    billing.db.session.commit()
    view = _subscription_view(subscription)
    assert view["title"] == "Billing review required"
    assert view["show_checkout"] is False
    assert view["next_step"] == subscription.billing_incident_detail
    surface = _billing_surface_view(organization=organization(billing), subscription_view=view, onboarding_view=None)
    assert "Sending enabled" not in str(surface) and "Billing inactive" in str(surface)


def test_billing_view_offers_saved_checkout_recovery_without_an_api_operation(billing):
    from app.routes import _subscription_view

    subscription = organization(billing).subscription
    subscription.checkout_request_status = "unknown"
    subscription.checkout_request_started_at = billing.models.utc_now().replace(tzinfo=None)
    subscription.billing_incident_code = "checkout_creation_unconfirmed"
    subscription.billing_incident_detail = "The earlier checkout request is saved."
    billing.db.session.commit()
    view = _subscription_view(subscription)
    assert view["title"] == "Checkout confirmation pending"
    assert view["show_checkout"] is True and view["checkout_button_label"] == "Resume saved checkout"
    assert billing.provider.api.checkout.Session.create.call_count == 0


def test_customer_subscription_discovery_is_an_incident_instead_of_silent_adoption(billing):
    subscription = organization(billing).subscription
    subscription.stripe_customer_id = "cus_transition"
    billing.db.session.commit()
    billing.provider.subscriptions["sub_unowned"] = {
        "id": "sub_unowned", "customer": "cus_transition", "status": "active",
    }
    with pytest.raises(billing.service.BillingTransitionError, match="not linked"):
        billing.service.refresh_subscription_from_stripe(organization(billing))
    assert subscription.stripe_subscription_id is None
    assert subscription.billing_incident_code == "unexpected_subscription"
    assert len(billing.provider.requests) == 0


def test_invoice_posting_is_blocked_without_underlying_verified_usage_evidence(billing):
    from app.services.twilio_service import previous_billing_period_window

    start, end = previous_billing_period_window()
    period = billing.models.OrganizationUsageBillingPeriod(
        organization_id=billing.organization_id, period_start=start, period_end=end,
        included_units=0, used_units=10, overage_units=10, sell_amount="0.30", currency="usd",
        status="pending", settlement_due_at=end,
    )
    organization(billing).subscription.stripe_customer_id = "cus_transition"
    billing.db.session.add(period)
    billing.db.session.commit()
    result = billing.service._post_closed_usage_invoice_items()
    assert result["periods_posted"] == 0 and result["periods_skipped"] == 1
    assert period.status == "review_required" and period.settlement_due_at is None
    assert period.stripe_invoice_id is None and period.stripe_invoice_item_id is None


def test_migration_022_adds_ownership_without_rewriting_existing_payment_records(billing):
    import importlib
    from sqlalchemy import inspect

    subscription = organization(billing).subscription
    subscription.stripe_customer_id = "cus_retained"
    subscription.stripe_subscription_id = "sub_retained"
    subscription.activation_payment_intent_id = "pi_retained"
    billing.db.session.add(billing.models.StripeCheckoutSession(
        organization_id=billing.organization_id, stripe_checkout_session_id="cs_retained",
        billing_plan_code="monthly", recurring_price_id="price_monthly", activation_price_id="price_setup",
        offer_version="prior-offer", status="completed", stripe_customer_id="cus_retained", stripe_subscription_id="sub_retained",
    ))
    billing.db.session.commit()
    billing.db.session.remove()
    new_columns = {
        "organization_subscriptions": {"checkout_request_key", "checkout_request_payload_json", "checkout_request_started_at",
            "checkout_request_status", "checkout_request_session_id", "billing_incident_code", "billing_incident_detail"},
        "stripe_checkout_sessions": {"transition_key"},
    }
    migration = importlib.import_module("app.saas_migrations.022_add_billing_transition_ownership")
    with billing.db.engine.begin() as connection:
        snapshots = {}
        columns = {}
        for table, added in new_columns.items():
            columns[table] = [row["name"] for row in inspect(connection).get_columns(table) if row["name"] not in added]
            names = ", ".join(columns[table])
            snapshots[table] = connection.execute(text(f"SELECT {names} FROM {table} ORDER BY id")).fetchall()
        connection.execute(text("DROP INDEX ix_stripe_checkout_sessions_transition_key"))
        for table, added in new_columns.items():
            for column in added:
                connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        migration.apply(connection, billing.app.logger)
        migration.apply(connection, billing.app.logger)
        for table, added in new_columns.items():
            assert added <= {row["name"] for row in inspect(connection).get_columns(table)}
            names = ", ".join(columns[table])
            assert connection.execute(text(f"SELECT {names} FROM {table} ORDER BY id")).fetchall() == snapshots[table]


@pytest.mark.parametrize("different_plans", [False, True])
def test_postgres_competing_checkout_requests_have_one_payable_identity(billing, different_plans):
    if not billing.postgres:
        pytest.skip("Real row-lock concurrency requires BILLING_TEST_POSTGRES_ROOT.")
    start = Barrier(2)

    def submit(index):
        with billing.app.app_context():
            pid = billing.db.session.scalar(text("SELECT pg_backend_pid()"))
            start.wait(timeout=10)
            result = begin(billing, "annual" if different_plans and index else "monthly")
            return result.id, pid

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(submit, range(2)))
    assert results[0][1] != results[1][1]
    assert len(open_sessions(billing)) == 1
    assert len(billing.provider.requests) == (2 if different_plans else 1)
    assert len({result[0] for result in results}) == (2 if different_plans else 1)


def test_postgres_checkout_and_complimentary_grant_serialize_without_leaving_payable_url(billing):
    if not billing.postgres:
        pytest.skip("Real row-lock concurrency requires BILLING_TEST_POSTGRES_ROOT.")
    provider_entered, release_provider = Event(), Event()

    def pause_create(key, params):
        provider_entered.set()
        assert release_provider.wait(timeout=10)

    billing.provider.on_create = pause_create

    def start_checkout():
        with billing.app.app_context():
            pid = billing.db.session.scalar(text("SELECT pg_backend_pid()"))
            result = begin(billing)
            return result.id, pid

    def grant():
        with billing.app.app_context():
            pid = billing.db.session.scalar(text("SELECT pg_backend_pid()"))
            result = billing.service.mark_subscription_complimentary(organization(billing))
            return result.status, pid

    with ThreadPoolExecutor(max_workers=2) as workers:
        checkout = workers.submit(start_checkout)
        try:
            assert provider_entered.wait(timeout=10)
            complimentary = workers.submit(grant)
        finally:
            release_provider.set()
        checkout_result = checkout.result(timeout=15)
        complimentary_result = complimentary.result(timeout=15)
    assert checkout_result[1] != complimentary_result[1]
    assert complimentary_result[0] == "complimentary" and open_sessions(billing) == []


def test_postgres_duplicate_paid_webhooks_have_one_projection_and_payment_identity(billing):
    if not billing.postgres:
        pytest.skip("Real row-lock concurrency requires BILLING_TEST_POSTGRES_ROOT.")
    first = begin(billing)
    billing.provider.pay(first.id)
    event = checkout_event(billing, first.id)
    start = Barrier(2)

    def deliver():
        with billing.app.app_context():
            pid = billing.db.session.scalar(text("SELECT pg_backend_pid()"))
            start.wait(timeout=10)
            record = billing.service.process_stripe_webhook_event(deepcopy(event))
            return record.id, pid

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: deliver(), range(2)))
    assert results[0][1] != results[1][1] and results[0][0] == results[1][0]
    billing.db.session.expire_all()
    assert billing.models.StripeWebhookEvent.query.count() == 1
    assert billing.models.StripeWebhookEvent.query.one().status == "processed"
    assert organization(billing).subscription.activation_payment_intent_id == "pi_" + first.id
    assert len(billing.provider.requests) == 1


@pytest.mark.parametrize("action", ["plan", "complimentary"])
def test_postgres_payment_webhook_races_plan_or_complimentary_transition(billing, action):
    if not billing.postgres:
        pytest.skip("Real row-lock concurrency requires BILLING_TEST_POSTGRES_ROOT.")
    first = begin(billing)
    paid, webhook_started = Event(), Event()
    billing.provider.expire_mode = "payment_wins"

    def payment_during_expire(session_id):
        paid.set()
        assert webhook_started.wait(timeout=10)

    billing.provider.on_expire = payment_during_expire

    def apply_webhook():
        with billing.app.app_context():
            pid = billing.db.session.scalar(text("SELECT pg_backend_pid()"))
            assert paid.wait(timeout=10)
            webhook_started.set()
            result = billing.service.process_stripe_webhook_event(checkout_event(billing, first.id))
            return result.status, pid

    def conflict():
        with billing.app.app_context():
            pid = billing.db.session.scalar(text("SELECT pg_backend_pid()"))
            with pytest.raises(billing.service.BillingTransitionError, match="payment was reconciled"):
                if action == "plan":
                    begin(billing, "annual")
                else:
                    billing.service.mark_subscription_complimentary(organization(billing))
            return pid

    with ThreadPoolExecutor(max_workers=2) as workers:
        webhook = workers.submit(apply_webhook)
        conflicting = workers.submit(conflict)
        webhook_result = webhook.result(timeout=15)
        assert webhook_result[1] != conflicting.result(timeout=15)
    billing.db.session.expire_all()
    assert webhook_result[0] in {"processed", "ignored"}
    assert organization(billing).subscription.status == "active"
    assert organization(billing).subscription.activation_payment_intent_id == "pi_" + first.id
    assert len(billing.provider.requests) == 1


class UsageSettlementBoundary:
    """A provider can accept an operation independently of its HTTP acknowledgement."""
    def __init__(self):
        self.invoices, self.items, self.requests = {}, {}, {}
        self.fail_after = None
        self.api = SimpleNamespace(
            Invoice=SimpleNamespace(create=MagicMock(side_effect=self.create_invoice),
                                    retrieve=MagicMock(side_effect=lambda key: deepcopy(self.invoices[key])),
                                    finalize_invoice=MagicMock(side_effect=self.finalize)),
            InvoiceItem=SimpleNamespace(create=MagicMock(side_effect=self.create_item),
                                        retrieve=MagicMock(side_effect=lambda key: deepcopy(self.items[key])),
                                        list=MagicMock(side_effect=self.list_items)),
        )

    def accepted(self, operation, params, create):
        key = params.pop('idempotency_key')
        if key not in self.requests:
            self.requests[key] = deepcopy(params)
            result = create(params)
        else:
            assert self.requests[key] == params, 'Financial retry changed frozen request parameters'
            result = next(row for row in (self.invoices if operation == 'invoice' else self.items).values() if row['request_key'] == key)
        result['request_key'] = key
        if self.fail_after == operation:
            raise TimeoutError('Controlled accepted provider operation; acknowledgement lost')
        return deepcopy(result)

    def create_invoice(self, **params):
        def create(payload):
            row = dict(deepcopy(payload), id='in_usage_' + uuid4().hex, status='draft', currency='usd', total=0)
            self.invoices[row['id']] = row
            return row
        return self.accepted('invoice', params, create)

    def create_item(self, **params):
        if self.fail_after == 'before_item':
            raise TimeoutError('Controlled operation did not establish remote item proof')
        def create(payload):
            row = dict(deepcopy(payload), id='ii_usage_' + uuid4().hex)
            self.items[row['id']] = row
            self.invoices[row['invoice']]['total'] += row['amount']
            return row
        return self.accepted('item', params, create)

    def list_items(self, **params):
        return {'data': [deepcopy(row) for row in self.items.values() if row['invoice'] == params['invoice']], 'has_more': False}

    def finalize(self, key, **params):
        self.invoices[key]['status'] = 'open'
        if self.fail_after == 'finalize':
            raise TimeoutError('Controlled invoice finalized; acknowledgement lost')
        return deepcopy(self.invoices[key])


def usage_settlement(billing, monkeypatch):
    from decimal import Decimal
    from app.services import twilio_service

    subscription = organization(billing).subscription
    subscription.status, subscription.stripe_customer_id = 'active', 'cus_usage_owner'
    start, end = twilio_service.previous_billing_period_window()
    start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    period = billing.models.OrganizationUsageBillingPeriod(
        organization_id=billing.organization_id, period_start=start, period_end=end,
        included_units=1000, used_units=1005, overage_units=5, sell_amount=Decimal('0.15'), status='pending',
    )
    billing.db.session.add(period)
    billing.db.session.add(billing.models.MessagingUsageRecord(
        organization_id=billing.organization_id, message_sid='SM_usage_' + uuid4().hex,
        billable=True, billable_units=1005, cost_status='known', units_status='known',
        provider_cost=Decimal('0.02'), provider_currency='usd',
        billing_period_start=start, billing_period_end=end, billing_review_required=False,
    ))
    billing.db.session.commit()
    provider = UsageSettlementBoundary()
    monkeypatch.setattr(billing.service, '_stripe_module', lambda: provider.api)
    return period, provider, twilio_service


@pytest.mark.parametrize('failure', ['invoice', 'item', 'finalize'])
def test_usage_settlement_acceptance_loss_resumes_original_objects_after_aggregation(billing, monkeypatch, failure):
    period, provider, twilio = usage_settlement(billing, monkeypatch)
    provider.fail_after = failure
    assert billing.service._post_closed_usage_invoice_items()['periods_failed'] == 1
    billing.db.session.refresh(period)
    started = period.settlement_started_at
    assert period.status == 'error' and period.settlement_version == 1 and started is not None
    invoice_id = next(iter(provider.invoices))
    # A changed rate or normal aggregation must not rewrite a financial attempt.
    billing.app.config['BILLING_OUTBOUND_SEGMENT_RATE_USD'] = '0.9900'
    twilio.upsert_closed_usage_billing_periods()
    billing.db.session.refresh(period)
    assert period.status == 'error' and period.settlement_version == 1
    assert period.settlement_started_at == started
    provider.fail_after = None
    assert billing.service._post_closed_usage_invoice_items()['periods_posted'] == 1
    billing.db.session.refresh(period)
    assert (period.status, period.invoiced_units, period.settlement_version, period.stripe_invoice_id) == ('posted', 5, 1, invoice_id)
    assert len(provider.invoices) == len(provider.items) == 1
    assert next(iter(provider.items.values()))['amount'] == 15
    assert provider.api.InvoiceItem.create.call_count == 1
    assert billing.service._post_closed_usage_invoice_items()['periods_posted'] == 0


@pytest.mark.parametrize('failure', ['invoice', 'before_item'])
def test_unconfirmed_usage_creation_past_key_retention_holds_original_identity(billing, monkeypatch, failure):
    period, provider, twilio = usage_settlement(billing, monkeypatch)
    provider.fail_after = failure
    assert billing.service._post_closed_usage_invoice_items()['periods_failed'] == 1
    billing.db.session.refresh(period)
    invoice_id = period.stripe_invoice_id
    period.settlement_started_at = billing.service._billing_utc_now() - timedelta(hours=25)
    billing.db.session.commit()
    twilio.upsert_closed_usage_billing_periods()
    provider.fail_after = None
    calls = (provider.api.Invoice.create.call_count, provider.api.InvoiceItem.create.call_count)
    assert billing.service._post_closed_usage_invoice_items()['periods_skipped'] == 1
    billing.db.session.refresh(period)
    assert (period.status, period.settlement_version, period.stripe_invoice_id) == ('review_required', 1, invoice_id)
    assert (provider.api.Invoice.create.call_count, provider.api.InvoiceItem.create.call_count) == calls
    assert len(provider.invoices) == 1 and len(provider.items) == 0
    assert billing.models.OrganizationProviderAuditLog.query.filter_by(action='usage_settlement_review').count() == 1


@pytest.mark.parametrize('legacy_start', [False, True])
def test_verified_accepted_usage_item_resumes_after_retention_without_new_objects(billing, monkeypatch, legacy_start):
    period, provider, twilio = usage_settlement(billing, monkeypatch)
    provider.fail_after = 'item'
    assert billing.service._post_closed_usage_invoice_items()['periods_failed'] == 1
    billing.db.session.refresh(period)
    period.settlement_started_at = None if legacy_start else billing.service._billing_utc_now() - timedelta(days=4)
    billing.db.session.commit()
    invoice_id = period.stripe_invoice_id
    twilio.upsert_closed_usage_billing_periods()
    provider.fail_after = None
    assert billing.service._post_closed_usage_invoice_items()['periods_posted'] == 1
    billing.db.session.refresh(period)
    assert (period.stripe_invoice_id, period.settlement_version, period.invoiced_units) == (invoice_id, 1, 5)
    assert len(provider.invoices) == len(provider.items) == 1
    assert provider.api.Invoice.create.call_count == provider.api.InvoiceItem.create.call_count == 1


@pytest.mark.parametrize('change', ['invoice_customer', 'invoice_total', 'item_customer', 'item_amount', 'item_currency', 'item_metadata'])
def test_usage_recovery_requires_authoritative_owner_terms_and_amount_proof(billing, monkeypatch, change):
    period, provider, _twilio = usage_settlement(billing, monkeypatch)
    provider.fail_after = 'item'
    assert billing.service._post_closed_usage_invoice_items()['periods_failed'] == 1
    invoice, item = next(iter(provider.invoices.values())), next(iter(provider.items.values()))
    if change == 'invoice_customer':
        invoice['customer'] = 'cus_other_tenant'
    elif change == 'invoice_total':
        invoice['total'] = 99
    elif change == 'item_customer':
        item['customer'] = 'cus_other_tenant'
    elif change == 'item_amount':
        item['amount'] = 99
    elif change == 'item_currency':
        item['currency'] = 'eur'
    else:
        item['metadata']['organization_id'] = str(billing.other_id)
    provider.fail_after = None
    assert billing.service._post_closed_usage_invoice_items()['periods_skipped'] == 1
    billing.db.session.refresh(period)
    assert period.status == 'review_required' and period.settlement_version == 1
    assert period.invoiced_units == 0
    provider.api.Invoice.finalize_invoice.assert_not_called()
    assert len(provider.invoices) == len(provider.items) == 1


def test_new_usage_retry_uses_durable_attempt_start_instead_of_old_period_creation(billing, monkeypatch):
    period, provider, _twilio = usage_settlement(billing, monkeypatch)
    period.created_at = billing.service._billing_utc_now() - timedelta(days=30)
    billing.db.session.commit()
    provider.fail_after = 'invoice'
    assert billing.service._post_closed_usage_invoice_items()['periods_failed'] == 1
    provider.fail_after = None
    assert billing.service._post_closed_usage_invoice_items()['periods_posted'] == 1
    assert len(provider.invoices) == 1 and period.settlement_version == 1


def test_migration_026_retains_existing_invoice_and_legacy_unknown_start(billing, monkeypatch):
    import importlib
    from sqlalchemy import inspect

    period, _provider, _twilio = usage_settlement(billing, monkeypatch)
    period.status, period.settlement_version, period.stripe_invoice_id = 'error', 7, 'in_legacy_retained'
    billing.db.session.commit()
    period_id = period.id
    billing.db.session.remove()
    with billing.db.engine.begin() as connection:
        connection.execute(text('ALTER TABLE organization_usage_billing_periods DROP COLUMN settlement_started_at'))
        migration = importlib.import_module('app.saas_migrations.026_add_usage_settlement_started_at')
        migration.apply(connection, MagicMock())
        migration.apply(connection, MagicMock())
        assert 'settlement_started_at' in {row['name'] for row in inspect(connection).get_columns('organization_usage_billing_periods')}
    period = billing.db.session.get(billing.models.OrganizationUsageBillingPeriod, period_id)
    assert (period.status, period.settlement_version, period.stripe_invoice_id, period.settlement_started_at) == ('error', 7, 'in_legacy_retained', None)
