"""Branded browser errors retain machine endpoint contracts and HTTP status."""
from unittest.mock import patch

import pytest
from flask import abort
from werkzeug.exceptions import default_exceptions

from tests.test_ux_recovery import ux, login


def install_error_route(ux):
    def controlled_error(status):
        if status == 500:
            raise RuntimeError('Controlled application error')
        abort(status)

    ux.app.add_url_rule('/error-fixture/<int:status>', 'error_fixture', controlled_error)
    ux.app.add_url_rule('/api/error-fixture/<int:status>', 'api_error_fixture', controlled_error)
    ux.app.add_url_rule('/webhooks/error-fixture/<int:status>', 'webhook_error_fixture', controlled_error)
    ux.app.config['PROPAGATE_EXCEPTIONS'] = False


@pytest.mark.parametrize('status, heading', [
    (400, b'This request could not be completed'),
    (404, b'Page not found'),
    (500, b'Something went wrong'),
])
@pytest.mark.parametrize('signed_in', [False, True])
def test_error_pages_have_recovery_and_appropriate_layout(ux, status, heading, signed_in):
    install_error_route(ux)
    if signed_in:
        login(ux)
    response = ux.client.get(f'/error-fixture/{status}')
    assert response.status_code == status
    assert heading in response.data
    assert b'Controlled application error' not in response.data
    if signed_in:
        assert b'app-sidebar' in response.data and b'Back to workspace' in response.data
        assert response.headers['Cache-Control'] == 'private, no-store'
    else:
        assert b'auth-body' in response.data and b'Sign in' in response.data
        assert b'app-sidebar' not in response.data


@pytest.mark.parametrize('status', [400, 404, 500])
@pytest.mark.parametrize('path,headers', [
    ('/error-fixture/', {'Accept': 'application/json'}),
    ('/error-fixture/', {'Content-Type': 'application/json'}),
    ('/api/error-fixture/', {}),
    ('/webhooks/error-fixture/', {}),
])
def test_machine_errors_keep_framework_response_body(ux, status, path, headers):
    install_error_route(ux)
    response = ux.client.get(f'{path}{status}', headers=headers)
    assert response.status_code == status
    expected = default_exceptions[status]().get_response()
    assert response.data == expected.get_data()
    assert response.content_type == expected.content_type


def test_health_and_explicit_webhook_error_bodies_are_unchanged(ux):
    response = ux.client.get('/health')
    assert (response.status_code, response.data) == (200, b'OK')
    response = ux.client.post('/webhooks/twilio/delivery')
    assert (response.status_code, response.data) == (403, b'Forbidden')
    ux.app.config['STRIPE_WEBHOOK_SECRET'] = None
    response = ux.client.post('/webhooks/stripe', data='{}', content_type='application/json')
    assert (response.status_code, response.data) == (500, b'Not configured')
    ux.app.config['STRIPE_WEBHOOK_SECRET'] = 'isolated-webhook-secret'
    with patch('stripe.Webhook.construct_event', side_effect=ValueError('Controlled invalid signature')):
        response = ux.client.post('/webhooks/stripe', data='{}', content_type='application/json')
    assert (response.status_code, response.data) == (403, b'Forbidden')


def test_missing_page_is_branded_without_a_matching_route(ux):
    response = ux.client.get('/this-page-does-not-exist')
    assert response.status_code == 404
    assert b'Page not found' in response.data and b'auth-body' in response.data


def test_rejected_host_stays_400_without_url_generation_or_reflection(ux):
    ux.app.config['TRUSTED_HOSTS'] = ['localhost']
    response = ux.client.get('/dashboard', headers={'Host': 'rejected.invalid'})
    assert response.status_code == 400
    assert b'Request address not supported' in response.data
    assert b'Twinevia' in response.data
    assert b'rejected.invalid' not in response.data
    assert b'href=' not in response.data
