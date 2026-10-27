"""Verify browser logout without relying on one request's cached login state."""
import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def remembered_app(tmp_path, monkeypatch):
    with monkeypatch.context() as environment:
        environment.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'remember.db'))
        environment.setenv('FLASK_DEBUG', '1')
        environment.setenv('SAAS_MODE', '0')
        environment.setenv('SCHEDULER_ENABLED', '0')
        config_module = importlib.import_module('app.config')
        importlib.reload(config_module)
        from app import create_app, db
        from app.models import AppUser
        app = create_app(run_startup_tasks=False, start_scheduler=False)
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                          SESSION_COOKIE_SECURE=False, REMEMBER_COOKIE_SECURE=False)
        monkeypatch.setattr('app.auth.send_security_alert', lambda *a, **kw: {'success': True})
        monkeypatch.setattr('app.routes.send_security_alert', lambda *a, **kw: {'success': True})
        with app.app_context():
            db.create_all()
            user = AppUser(username='remember-owner', phone='+17205550991',
                           role='admin', must_change_password=False)
            user.set_password('Remember-local1!')
            db.session.add(user)
            db.session.commit()
        try:
            yield SimpleNamespace(app=app, db=db)
        finally:
            with app.app_context():
                db.session.remove()
                db.drop_all()
                db.engine.dispose()
    importlib.reload(config_module)


@pytest.mark.parametrize('cookie_name', ['remember_token', 'twinevia_remember_test'])
def test_logout_clears_remember_cookie_before_a_fresh_client_can_restore_it(remembered_app, cookie_name):
    app = remembered_app.app
    app.config['REMEMBER_COOKIE_NAME'] = cookie_name
    client = app.test_client()
    login = client.post('/login', data={'username': 'remember-owner',
                        'password': 'Remember-local1!', 'remember': 'on'})
    assert login.status_code == 302
    assert client.get_cookie(cookie_name) is not None
    response = client.post('/logout')
    assert response.status_code == 302
    # Copy the cookies the browser actually retains after logout into a fresh
    # client, where strong-session checks cannot mask a retained credential.
    fresh = app.test_client()
    retained = client.get_cookie(cookie_name)
    if retained is not None:
        fresh.set_cookie(cookie_name, retained.value)
    dashboard = fresh.get('/dashboard')
    assert dashboard.status_code == 302
    assert '/login' in dashboard.headers['Location']
    assert retained is None
    assert any(header.startswith(cookie_name + '=;') and 'Max-Age=0' in header
               for header in response.headers.getlist('Set-Cookie'))


def test_password_change_also_expires_the_device_remember_cookie(remembered_app):
    app = remembered_app.app
    client = app.test_client()
    client.post('/login', data={'username': 'remember-owner', 'password': 'Remember-local1!', 'remember': 'on'})
    assert client.get_cookie('remember_token') is not None
    response = client.post('/account/password', data={
        'current_password': 'Remember-local1!', 'new_password': 'Changed-local2!',
        'confirm_password': 'Changed-local2!',
    })
    assert response.status_code == 302 and '/login' in response.headers['Location']
    assert client.get_cookie('remember_token') is None


def test_logout_revokes_cookies_captured_by_an_inflight_request(remembered_app):
    app = remembered_app.app
    client = app.test_client()
    client.post('/login', data={'username': 'remember-owner', 'password': 'Remember-local1!', 'remember': 'on'})
    assert client.get('/dashboard').status_code == 200
    captured = [client.get_cookie(app.config['SESSION_COOKIE_NAME']), client.get_cookie('remember_token')]
    assert all(captured)
    assert client.post('/logout').status_code == 302
    late_response_browser = app.test_client()
    for cookie in captured:
        late_response_browser.set_cookie(cookie.key, cookie.value)
    response = late_response_browser.get('/dashboard')
    assert response.status_code == 302 and '/login' in response.headers['Location']


@pytest.mark.parametrize('gate', ['missing_organization', 'suspended_organization'])
def test_tenant_access_revocation_also_clears_remember_cookie(remembered_app, gate):
    app = remembered_app.app
    if gate == 'suspended_organization':
        from app.models import AppUser, Organization, OrganizationMembership
        with app.app_context():
            user = AppUser.query.filter_by(username='remember-owner').one()
            organization = Organization(name='Paused fixture', slug='paused-fixture', status='suspended')
            remembered_app.db.session.add(OrganizationMembership(organization=organization, user=user, role='owner'))
            remembered_app.db.session.commit()
    client = app.test_client()
    client.post('/login', data={'username': 'remember-owner', 'password': 'Remember-local1!', 'remember': 'on'})
    assert client.get_cookie('remember_token') is not None
    app.config['SAAS_MODE'] = True
    response = client.get('/dashboard')
    assert response.status_code == 302 and '/login' in response.headers['Location']
    assert client.get_cookie('remember_token') is None
