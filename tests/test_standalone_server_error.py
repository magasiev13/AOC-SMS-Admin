"""Owned 500 responses survive failed user loading and unavailable DB cleanup."""
from unittest.mock import Mock

from flask import g

from tests.test_ux_recovery import ux, login


def test_error_handler_never_loads_user_queries_or_template_context(ux, monkeypatch):
    ux.app.config.update(PROPAGATE_EXCEPTIONS=False, APP_BASE_URL='https://workspace.example.test')
    loader = Mock(side_effect=RuntimeError('User loader unavailable'))
    execute = Mock(side_effect=RuntimeError('Database execution unavailable'))
    get = Mock(side_effect=RuntimeError('Database lookup unavailable'))
    rollback = Mock(side_effect=RuntimeError('Database rollback unavailable'))
    context = Mock(side_effect=RuntimeError('Template context unavailable'))
    ux.app.context_processor(context)

    def database_outage():
        # Account gates already ran normally. Everything the error handler used
        # to depend on now fails, and the cached user is deliberately discarded.
        g.pop('_login_user', None)
        monkeypatch.setattr(ux.app.login_manager, '_user_callback', loader)
        monkeypatch.setattr(ux.db.session, 'execute', execute)
        monkeypatch.setattr(ux.db.session, 'get', get)
        monkeypatch.setattr(ux.db.session, 'rollback', rollback)
        raise RuntimeError('Synthetic database outage')

    ux.app.add_url_rule('/standalone-error-fixture', 'standalone_error_fixture', database_outage)
    login(ux)
    response = ux.client.get('/standalone-error-fixture')
    assert response.status_code == 500
    assert b'Something went wrong - Twinevia' in response.data
    assert b'href="https://workspace.example.test/dashboard"' in response.data
    assert b'app-sidebar' not in response.data and b'auth-body' not in response.data
    assert b'Synthetic database outage' not in response.data
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    rollback.assert_called_once_with()
    loader.assert_not_called()
    execute.assert_not_called()
    get.assert_not_called()
    context.assert_not_called()


def test_original_user_loader_failure_still_returns_standalone_500(ux, monkeypatch):
    ux.app.config.update(PROPAGATE_EXCEPTIONS=False, APP_BASE_URL='', SAAS_BASE_URL='')
    login(ux)
    g.pop('_login_user', None)
    loader = Mock(side_effect=RuntimeError('Synthetic user loader outage'))
    rollback = Mock(side_effect=RuntimeError('Synthetic failed rollback'))
    monkeypatch.setattr(ux.app.login_manager, '_user_callback', loader)
    monkeypatch.setattr(ux.db.session, 'rollback', rollback)
    response = ux.client.get('/dashboard')
    assert response.status_code == 500
    assert b'Something went wrong - Twinevia' in response.data
    assert b'href="/dashboard"' in response.data
    assert b'Synthetic user loader outage' not in response.data
    loader.assert_called_once()
    rollback.assert_called_once_with()
