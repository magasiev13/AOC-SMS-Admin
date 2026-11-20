"""A sent-draft marker is consumed only by the layout emitting its script."""
import re

import pytest
from flask import render_template, render_template_string, session
from werkzeug.exceptions import InternalServerError

from tests.test_ux_recovery import login, permitted_contact, review_fields, ux


DRAFT_KEY = 'twinevia:send-draft:fixture-user:fixture-workspace'


def test_schedule_acknowledges_revision_only_after_save_and_on_matching_replay(ux):
    permitted_contact(ux)
    login(ux)
    revision = '01' * 16
    fields = review_fields(schedule_later='on', schedule_date='2099-12-31',
                           schedule_time='23:59', draft_revision=revision)
    rejected = ux.client.post('/dashboard', data={**fields, 'review_token': 'expired'})
    assert rejected.status_code == 200
    assert 'Location' not in rejected.headers
    assert ux.models.ScheduledMessage.query.count() == 0
    with ux.client.session_transaction() as browser_session:
        assert 'clear_send_draft_key' not in browser_session

    review = ux.client.post('/dashboard/review', data=fields).get_json()
    fields['review_token'] = review['review_token']
    for _ in range(2):
        saved = ux.client.post('/dashboard', data=fields)
        assert saved.status_code == 302
        assert saved.location == f'/scheduled#sent-draft-{revision}'
        assert ux.models.ScheduledMessage.query.count() == 1


def test_unrelated_template_context_does_not_consume_sent_draft_marker(ux):
    with ux.app.test_request_context('/template-fixture'):
        session['clear_send_draft_key'] = DRAFT_KEY
        rendered = render_template_string('<p>{{ product_name }}</p>')
        assert 'Twinevia' in rendered
        assert DRAFT_KEY not in rendered
        assert session['clear_send_draft_key'] == DRAFT_KEY


@pytest.mark.parametrize('template', ['errors/500.html', 'errors/invalid_host.html'])
def test_standalone_template_preserves_marker_without_emitting_cleanup(ux, template):
    with ux.app.test_request_context('/template-fixture'):
        session['clear_send_draft_key'] = DRAFT_KEY
        rendered = render_template(template, product_name='Twinevia', workspace_url='/dashboard')
        assert session['clear_send_draft_key'] == DRAFT_KEY
        assert DRAFT_KEY not in rendered
        assert 'localStorage' not in rendered


@pytest.mark.parametrize('layout', ['base.html', 'auth/base.html', 'marketing/base.html'])
def test_every_common_layout_emits_cleanup_before_assets_and_consumes_once(ux, layout):
    with ux.app.test_request_context('/template-fixture'):
        session['clear_send_draft_key'] = DRAFT_KEY
        template = '{% extends "' + layout + '" %}{% block content %}<p>Fixture content</p>{% endblock %}'
        rendered = render_template_string(template)
        assert rendered.count(DRAFT_KEY) == 1
        assert '/static/js/pages/includes-clear-sent-draft.js' in rendered
        assert rendered.index(DRAFT_KEY) < rendered.index('/static/')
        assert 'clear_send_draft_key' not in session
        second = render_template_string(template)
        assert DRAFT_KEY not in second
        assert 'clear_send_draft_key' not in session


def test_standalone_500_handler_does_not_read_or_consume_session_marker(ux, monkeypatch):
    from app import routes

    class UnavailableSession:
        def __getattr__(self, name):
            raise AssertionError('Standalone error handler must not read session state')

        def __getitem__(self, key):
            raise AssertionError('Standalone error handler must not read session state')

        def __contains__(self, key):
            raise AssertionError('Standalone error handler must not read session state')

    with ux.app.test_request_context('/error-fixture'):
        session['clear_send_draft_key'] = DRAFT_KEY
        monkeypatch.setattr(routes, 'session', UnavailableSession())
        response = routes.request_error_page(InternalServerError())
        assert response.status_code == 500
        assert b'Something went wrong - Twinevia' in response.data
        assert session['clear_send_draft_key'] == DRAFT_KEY
        assert DRAFT_KEY.encode() not in response.data
        assert b'localStorage' not in response.data
