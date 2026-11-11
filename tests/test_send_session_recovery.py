"""Expired browser sessions fail safely without changing send eligibility."""
from tests.test_ux_recovery import ux, login


def test_review_requires_json_auth_but_pages_redirect(ux):
    response = ux.client.post('/dashboard/review', headers={'Accept': 'application/json'})
    assert response.status_code == 401
    assert response.json['login_url'] == '/login?next=/dashboard'
    assert ux.client.get('/dashboard').status_code == 302


def test_bad_token_returns_json_and_branded_page(ux):
    login(ux)
    ux.app.config['WTF_CSRF_ENABLED'] = True
    response = ux.client.post('/dashboard/review', data={'csrf_token': 'invalid'}, headers={'Accept': 'application/json'})
    assert response.status_code == 400
    assert 'expired' in response.json['error']
    response = ux.client.post('/dashboard', data={'csrf_token': 'invalid'})
    assert response.status_code == 400
    assert b'This page has expired' in response.data
    assert b'app-sidebar' in response.data


def test_expired_session_with_csrf_enabled_still_requests_signin(ux):
    ux.app.config['WTF_CSRF_ENABLED'] = True
    response = ux.client.post('/dashboard/review', data={'csrf_token': 'old'}, headers={'Accept': 'application/json'})
    assert response.status_code == 401
    assert 'message is saved' in response.json['error']
