"""Exercise the installed Stripe SDK with a synthetic HTTP transport only."""
import json
from urllib.parse import parse_qs, urlsplit

from flask import Flask
import pytest
import stripe
from stripe._http_client import HTTPClient

from app.services.billing_service import _stripe_module


class RetainedTransport(HTTPClient):
    name = 'twinevia-isolated-contract-test'

    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, method, url, headers, post_data=None, *, _usage=None):
        self.calls.append({'method': method.lower(), 'url': url, 'headers': dict(headers), 'body': post_data})
        return json.dumps({'id': 'fixture', 'object': 'invoice', 'data': [], 'has_more': False}), 200, {'request-id': 'isolated-fixture'}


@pytest.fixture
def sdk(monkeypatch):
    transport = RetainedTransport()
    original = stripe.StripeClient
    monkeypatch.setattr(stripe, 'StripeClient', lambda key: original(key, http_client=transport, max_network_retries=0))
    app = Flask(__name__)
    app.config['STRIPE_SECRET_KEY'] = 'sk_test_isolated_first'
    with app.app_context():
        yield app, transport


def test_checkout_adapter_preserves_sdk_request_options_and_original_parameters(sdk):
    _, transport = sdk
    api = _stripe_module()
    for _ in range(2):
        api.checkout.Session.create(mode='subscription', customer='cus_fixture', line_items=[{'price': 'price_fixture', 'quantity': 1}], idempotency_key='retained-checkout-key')
    first, second = transport.calls
    assert first == second
    assert first['headers']['Idempotency-Key'] == 'retained-checkout-key'
    body = parse_qs(first['body'])
    assert body['mode'] == ['subscription']
    assert body['line_items[0][price]'] == ['price_fixture']
    assert 'idempotency_key' not in body
    api.checkout.Session.expire('cs_fixture', idempotency_key='retained-expiry-key')
    assert urlsplit(transport.calls[-1]['url']).path == '/v1/checkout/sessions/cs_fixture/expire'
    assert transport.calls[-1]['headers']['Idempotency-Key'] == 'retained-expiry-key'


def test_two_client_credentials_remain_owned_without_changing_sdk_global_key(sdk, monkeypatch):
    app, transport = sdk
    monkeypatch.setattr(stripe, 'api_key', 'unchanged-global-fixture')
    first = _stripe_module()
    app.config['STRIPE_SECRET_KEY'] = 'sk_test_isolated_second'
    second = _stripe_module()
    second.Subscription.retrieve('sub_second')
    first.Subscription.retrieve('sub_first')
    assert transport.calls[0]['headers']['Authorization'] == 'Bearer sk_test_isolated_second'
    assert transport.calls[1]['headers']['Authorization'] == 'Bearer sk_test_isolated_first'
    assert stripe.api_key == 'unchanged-global-fixture'


def test_usage_invoice_lookup_and_finalize_use_installed_sdk_endpoints(sdk):
    _, transport = sdk
    api = _stripe_module()
    api.Invoice.retrieve('in_fixture', expand=['lines'])
    api.InvoiceItem.list(invoice='in_fixture', customer='cus_fixture', limit=100)
    api.Invoice.finalize_invoice('in_fixture', auto_advance=False, idempotency_key='same-finalize-key')
    assert [call['method'] for call in transport.calls] == ['get', 'get', 'post']
    assert [urlsplit(call['url']).path for call in transport.calls] == ['/v1/invoices/in_fixture', '/v1/invoiceitems', '/v1/invoices/in_fixture/finalize']
    assert parse_qs(urlsplit(transport.calls[1]['url']).query)['invoice'] == ['in_fixture']
    assert transport.calls[-1]['headers']['Idempotency-Key'] == 'same-finalize-key'
