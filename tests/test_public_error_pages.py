"""Public and workspace 404s use their own host-aware recovery paths."""
import importlib

from flask import abort
import pytest
from werkzeug.exceptions import NotFound


@pytest.fixture
def public_error_app(tmp_path, monkeypatch):
    # These synthetic hosts exercise the real production canonical-host hook.
    for key, value in {
        'PYTHON_DOTENV_DISABLED': '1', 'FLASK_ENV': 'production', 'FLASK_DEBUG': '0',
        'SAAS_MODE': '0', 'SECRET_KEY': 'public-error-fixture-secret',
        'SCHEDULER_ENABLED': '0', 'AUTH_ALERTS_ENABLED': '0',
        'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'public-errors.db'),
        'PUBLIC_BASE_URL': 'https://public.fixture.test',
        'APP_BASE_URL': 'https://app.fixture.test',
        'SAAS_BASE_URL': 'https://app.fixture.test',
        'TRUSTED_HOSTS': 'public.fixture.test,www.public.fixture.test,app.fixture.test',
    }.items():
        monkeypatch.setenv(key, value)
    import app.config
    importlib.reload(app.config)
    from app import create_app, db
    application = create_app(run_startup_tasks=False, start_scheduler=False)
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.mark.parametrize('host', ['public.fixture.test', 'www.public.fixture.test'])
def test_unknown_public_host_page_stays_public_and_offers_home(public_error_app, host):
    response = public_error_app.test_client().get('/missing-public-page', base_url=f'https://{host}')
    assert response.status_code == 404 and 'Location' not in response.headers
    html = response.get_data(as_text=True)
    assert 'marketing-page' in html and 'data-public-not-found' in html
    assert 'auth-body' not in html and 'app-sidebar' not in html
    assert 'href="https://public.fixture.test/">Back to home</a>' in html
    recovery = html.split('data-public-not-found', 1)[1].split('</header>', 1)[0]
    assert 'Sign in' not in recovery


def test_unknown_app_host_page_retains_sign_in_recovery(public_error_app):
    response = public_error_app.test_client().get('/missing-workspace-page', base_url='https://app.fixture.test')
    assert response.status_code == 404
    assert b'auth-body' in response.data and b'Page not found' in response.data
    assert b'data-public-not-found' not in response.data
    assert b'href="/login">Sign in</a>' in response.data


@pytest.mark.parametrize('path', ['/features/missing-page', '/privacy/missing-page', '/pricing/'])
def test_unmatched_marketing_path_uses_public_recovery_on_app_host(public_error_app, path):
    response = public_error_app.test_client().get(path, base_url='https://app.fixture.test')
    assert response.status_code == 404
    assert b'data-public-not-found' in response.data
    assert b'href="https://public.fixture.test/">Back to home</a>' in response.data


def test_marketing_prefix_does_not_claim_unrelated_workspace_paths(public_error_app):
    response = public_error_app.test_client().get('/features-malicious/missing', base_url='https://app.fixture.test')
    assert response.status_code == 404
    assert b'auth-body' in response.data and b'data-public-not-found' not in response.data


@pytest.mark.parametrize('path', ['/login?next=%2Fdashboard', '/dashboard'])
def test_known_workspace_routes_still_redirect_to_app_host(public_error_app, path):
    response = public_error_app.test_client().get(path, base_url='https://public.fixture.test')
    assert response.status_code == 308
    assert response.headers['Location'] == 'https://app.fixture.test' + path


def test_existing_marketing_page_error_uses_marketing_layout(public_error_app, monkeypatch):
    monkeypatch.setitem(public_error_app.view_functions, 'main.features', lambda: abort(404))
    response = public_error_app.test_client().get('/features', base_url='https://public.fixture.test')
    assert response.status_code == 404 and b'data-public-not-found' in response.data


@pytest.mark.parametrize('path,headers', [('/api/missing', {}), ('/webhooks/missing', {}),
    ('/missing', {'Accept': 'application/json'})])
def test_machine_error_bodies_stay_unchanged(public_error_app, path, headers):
    response = public_error_app.test_client().get(path, base_url='https://app.fixture.test', headers=headers)
    assert response.status_code == 404
    assert response.data == NotFound().get_response().get_data()


def test_head_and_health_keep_their_protocol_contracts(public_error_app):
    client = public_error_app.test_client()
    response = client.head('/missing', base_url='https://public.fixture.test')
    assert response.status_code == 404 and response.data == b'' and 'Location' not in response.headers
    response = client.get('/health', base_url='https://public.fixture.test')
    assert (response.status_code, response.data) == (200, b'OK')


def test_marketing_path_can_recover_on_a_shared_local_host(public_error_app):
    public_error_app.config.update(PUBLIC_BASE_URL='', APP_BASE_URL='')
    response = public_error_app.test_client().get('/features/missing', base_url='https://app.fixture.test')
    assert response.status_code == 404
    assert b'href="/">Back to home</a>' in response.data
