"""Rendered keyword history must belong to its recorded automation origin."""
from datetime import timedelta
import importlib.util
from pathlib import Path

import pytest


_support_spec = importlib.util.spec_from_file_location(
    'twinevia_origin_ui_fixture', Path(__file__).with_name('test_keyword_legacy_editing.py'),
)
_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(_support)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    generator = _support.keywords.__wrapped__(tmp_path, monkeypatch)
    fixture = next(generator)
    try:
        yield fixture
    finally:
        next(generator, None)


def automation(workspace, kind='keyword', *, org=None, active=True, created_at=None, identity=None, keyword='JOIN'):
    values = {'organization_id': (org or workspace.org).id, 'is_active': active,
              'created_at': created_at or workspace.m.utc_now().replace(tzinfo=None) - timedelta(minutes=10)}
    if identity is not None:
        values['id'] = identity
    if kind == 'survey':
        row = workspace.m.SurveyFlow(name='Origin survey', trigger_keyword=keyword, **values)
        row.set_questions(['Original question?'])
    else:
        row = workspace.m.KeywordAutomationRule(keyword=keyword, response_body='Original response', **values)
    workspace.db.session.add(row)
    workspace.db.session.commit()
    return row


def message(workspace, *, kind='keyword', origin=None, keyword='JOIN', created_at=None, org=None, phone='+17205550799'):
    org = org or workspace.org
    thread = workspace.m.InboxThread(organization_id=org.id, phone=phone)
    workspace.db.session.add(thread)
    workspace.db.session.flush()
    row = workspace.m.InboxMessage(organization_id=org.id, thread_id=thread.id, phone=phone,
        direction='inbound', body='Original inbound message', matched_keyword=keyword,
        automation_source=kind if origin is not None else None, automation_source_id=origin.id if origin is not None else None,
        created_at=created_at or workspace.m.utc_now().replace(tzinfo=None))
    workspace.db.session.add(row)
    workspace.db.session.commit()
    return thread, row


def rendered(workspace, thread):
    response = workspace.client.get(f'/inbox?thread={thread.id}')
    assert response.status_code == 200
    return response.get_data(as_text=True)


def assert_historical(html, keyword='JOIN'):
    assert f'• keyword: <code>{keyword}</code>' not in html
    assert 'Show historical keyword' in html
    assert f'Historical keyword: <code>{keyword}</code>' in html


def test_recreated_keyword_with_different_id_cannot_relabel_old_inbound_history(workspace):
    original = automation(workspace)
    origin_id = original.id
    thread, old = message(workspace, origin=original)
    workspace.db.session.delete(original)
    workspace.db.session.commit()
    replacement = automation(workspace, identity=origin_id + 100)
    html = rendered(workspace, thread)
    assert_historical(html)
    assert old.automation_source_id != replacement.id


@pytest.mark.parametrize('kind', ['keyword', 'survey'])
def test_current_active_recorded_origin_keeps_visible_keyword(workspace, kind):
    origin = automation(workspace, kind)
    thread, _ = message(workspace, kind=kind, origin=origin)
    html = rendered(workspace, thread)
    assert '• keyword: <code>JOIN</code>' in html
    assert 'Show historical keyword' not in html


@pytest.mark.parametrize('kind', ['keyword', 'survey'])
def test_reused_automation_id_does_not_make_older_history_current(workspace, kind):
    origin = automation(workspace, kind)
    origin_id = origin.id
    thread, old = message(workspace, kind=kind, origin=origin,
                          created_at=workspace.m.utc_now().replace(tzinfo=None) - timedelta(minutes=1))
    workspace.db.session.delete(origin)
    workspace.db.session.commit()
    replacement = automation(workspace, kind, identity=origin_id,
                             created_at=workspace.m.utc_now().replace(tzinfo=None))
    assert replacement.created_at > old.created_at
    assert_historical(rendered(workspace, thread))


@pytest.mark.parametrize('kind', ['keyword', 'survey'])
def test_inactive_recorded_origin_remains_historical(workspace, kind):
    origin = automation(workspace, kind, active=False)
    thread, _ = message(workspace, kind=kind, origin=origin)
    html = rendered(workspace, thread)
    assert_historical(html)
    assert 'The original automation is inactive, changed, or no longer available.' in html


@pytest.mark.parametrize('kind', ['keyword', 'survey'])
def test_legacy_unknown_origin_is_not_inferred_from_current_keyword(workspace, kind):
    automation(workspace, kind)
    thread, old = message(workspace, origin=None)
    html = rendered(workspace, thread)
    assert_historical(html)
    assert 'The original automation is unknown.' in html
    workspace.db.session.refresh(old)
    assert (old.automation_source, old.automation_source_id) == (None, None)


def test_current_origin_with_changed_trigger_does_not_retag_original_keyword(workspace):
    origin = automation(workspace)
    thread, _ = message(workspace, origin=origin)
    origin.keyword = 'REPLACEMENT'
    workspace.db.session.commit()
    assert_historical(rendered(workspace, thread))


def test_origin_type_is_required_even_when_other_table_has_matching_id_and_keyword(workspace):
    origin = automation(workspace)
    thread, old = message(workspace, kind='survey', origin=origin)
    assert workspace.m.SurveyFlow.query.filter_by(id=origin.id).first() is None
    assert old.automation_source_id == origin.id
    assert_historical(rendered(workspace, thread))


def test_foreign_tenant_origin_cannot_make_local_history_current_or_expose_conversation(workspace):
    foreign = automation(workspace, org=workspace.other)
    automation(workspace)
    thread, _ = message(workspace, origin=foreign)
    assert_historical(rendered(workspace, thread))
    foreign_thread, secret = message(workspace, org=workspace.other, origin=foreign, phone='+17205550798')
    secret.body = 'Private other tenant origin traffic'
    workspace.db.session.commit()
    response = workspace.client.get(f'/inbox?thread={foreign_thread.id}')
    assert response.status_code == 200
    assert b'Private other tenant origin traffic' not in response.data


@pytest.mark.parametrize('kind', ['keyword', 'survey'])
def test_current_origin_comparison_normalizes_legacy_whitespace(workspace, kind):
    from sqlalchemy import text
    origin = automation(workspace, kind, keyword='JOIN CODE')
    thread, _ = message(workspace, kind=kind, origin=origin, keyword='join  code')
    table, column = ('keyword_automation_rules', 'keyword') if kind == 'keyword' else ('survey_flows', 'trigger_keyword')
    workspace.db.session.execute(text(f'UPDATE {table} SET {column}=:keyword WHERE id=:id'),
                                 {'keyword': '\t Join\nCODE ', 'id': origin.id})
    workspace.db.session.commit()
    html = rendered(workspace, thread)
    assert '• keyword: <code>join  code</code>' in html
    assert 'Show historical keyword' not in html

