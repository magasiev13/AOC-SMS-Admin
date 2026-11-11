"""History and conversation pages stay bounded without hiding older records."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from flask import template_rendered
import pytest

from tests.test_logs_routes import TestLogsRoutes as _LogsFixture


class _PagerLinks(HTMLParser):
    def __init__(self, text, label):
        super().__init__()
        self.label = label
        self.inside = False
        self.links = []
        self.feed(text)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'nav' and attrs.get('aria-label') == self.label:
            self.inside = True
        elif tag == 'a' and self.inside:
            self.links.append(attrs['href'])

    def handle_endtag(self, tag):
        if tag == 'nav':
            self.inside = False


@contextmanager
def _rendered_context(app, template_name):
    captured = []

    def capture(sender, template, context, **extra):
        if template.name == template_name:
            captured.append(context.copy())

    with template_rendered.connected_to(capture, app):
        yield captured


@pytest.fixture
def history():
    f = _LogsFixture()
    f.setUp()
    try:
        from app.models import InboxThread
        f.InboxThread = InboxThread
        stamp = datetime(2026, 10, 31, 12)
        logs = [f.MessageLog(
            organization_id=f.organization.id, target='community', status='sent',
            message_body=f'{"Matching" if index < 73 else "Other"} history {index:03d}',
            created_at=stamp, total_recipients=0, success_count=0, failure_count=0,
        ) for index in range(123)]
        threads = [InboxThread(
            organization_id=f.organization.id, phone=f'+1555100{index:04d}',
            contact_name=f'{"Matching" if index < 73 else "Other"} conversation {index:03d}',
            last_message_at=stamp,
        ) for index in range(217)]
        other = f.Organization(name='Other tenant', slug='other-tenant', status='active')
        f.db.session.add_all([*logs, *threads, other])
        f.db.session.flush()
        foreign_log = f.MessageLog(
            organization_id=other.id, target='community', status='sent',
            message_body='Matching private foreign history', created_at=stamp,
        )
        foreign_thread = InboxThread(
            organization_id=other.id, phone='+15559999999',
            contact_name='Matching private foreign conversation', last_message_at=stamp,
        )
        f.db.session.add_all([foreign_log, foreign_thread])
        f.db.session.commit()
        f.log_ids = [row.id for row in reversed(logs)]
        f.thread_ids = [row.id for row in reversed(threads)]
        f.matching_log_ids = [row.id for row in reversed(logs[:73])]
        f.matching_thread_ids = [row.id for row in reversed(threads[:73])]
        f.foreign_thread_id = foreign_thread.id
        f._login()
        yield f
    finally:
        f.tearDown()


def _page(f, path, **query):
    template = 'logs/list.html' if path == '/logs' else 'inbox/list.html'
    with _rendered_context(f.app, template) as captured:
        response = f.client.get(path, query_string=query)
    assert response.status_code == 200
    assert len(captured) == 1
    assert 'private foreign' not in response.get_data(as_text=True)
    return response, captured[0]


@pytest.mark.parametrize('path,rows_key,ids_key,page_count,total', [
    ('/logs', 'logs', 'log_ids', 3, 123),
    ('/inbox', 'threads', 'thread_ids', 5, 217),
])
def test_every_record_is_reachable_in_stable_tenant_scoped_pages(history, path, rows_key, ids_key, page_count, total):
    f = history
    seen = []
    for page in range(1, page_count + 1):
        response, context = _page(f, path, page=page, view='threads')
        actual = [row.id for row in context[rows_key]]
        assert actual == getattr(f, ids_key)[(page - 1) * 50:page * 50]
        assert len(actual) <= 50
        assert context['page'] == page and context['total_pages'] == page_count
        assert context['per_page'] == 50 and context['total_count'] == total
        assert f'Showing {(page - 1) * 50 + 1}–{min(page * 50, total)} of {total}' in response.get_data(as_text=True)
        seen.extend(actual)
    assert seen == getattr(f, ids_key)
    assert len(set(seen)) == total


@pytest.mark.parametrize('path,rows_key,ids_key,label', [
    ('/logs', 'logs', 'matching_log_ids', 'Message History Pagination'),
    ('/inbox', 'threads', 'matching_thread_ids', 'Conversation Pagination'),
])
def test_search_is_preserved_across_pages(history, path, rows_key, ids_key, label):
    f = history
    seen = []
    for page in (1, 2):
        response, context = _page(f, path, search='Matching', page=page, view='threads')
        assert context['total_count'] == 73 and context['total_pages'] == 2
        seen.extend(row.id for row in context[rows_key])
        links = _PagerLinks(response.get_data(as_text=True), label).links
        assert len(links) == 1
        for link in links:
            query = parse_qs(urlsplit(link).query)
            assert query['search'] == ['Matching']
            assert query['page'] == [str(3 - page)]
            if path == '/inbox':
                assert query['view'] == ['threads']
    assert seen == getattr(f, ids_key)


@pytest.mark.parametrize('path,last_page', [('/logs', 3), ('/inbox', 5)])
@pytest.mark.parametrize('value,expected', [('nonsense', 1), ('1.5', 1), ('0', 1), ('-12', 1), ('999999', None)])
def test_malformed_and_out_of_range_pages_are_clamped(history, path, last_page, value, expected):
    _, context = _page(history, path, page=value, view='threads')
    assert context['page'] == (expected or last_page)


@pytest.mark.parametrize('path,rows_key', [('/logs', 'logs'), ('/inbox', 'threads')])
def test_empty_search_has_a_single_empty_page(history, path, rows_key):
    response, context = _page(history, path, search='No such record', page=99, view='threads')
    assert context[rows_key] == []
    assert context['total_count'] == 0 and context['page'] == context['total_pages'] == 1
    assert 'rel="next"' not in response.get_data(as_text=True)


def test_selected_conversation_and_message_navigation_keep_the_list_page(history):
    f = history
    from app.models import InboxMessage
    selected_id = f.matching_thread_ids[-1]
    thread = f.db.session.get(f.InboxThread, selected_id)
    stamp = datetime(2026, 10, 31, 12)
    f.db.session.add_all([InboxMessage(
        organization_id=f.organization.id, thread_id=selected_id,
        phone=thread.phone, direction='inbound', body=f'Message {index}',
        created_at=stamp + timedelta(seconds=index),
    ) for index in range(102)])
    f.db.session.commit()
    response, context = _page(f, '/inbox', search='Matching', page=2, thread=selected_id)
    assert context['selected_thread'].id == selected_id
    for key in ('older_messages_url', 'latest_messages_url'):
        query = parse_qs(urlsplit(context[key]).query)
        assert query['page'] == ['2'] and query['search'] == ['Matching']
        assert query['thread'] == [str(selected_id)]
    links = _PagerLinks(response.get_data(as_text=True), 'Conversation Pagination').links
    assert len(links) == 1
    assert parse_qs(urlsplit(links[0]).query)['thread'] == [str(selected_id)]
    text = response.get_data(as_text=True)
    assert f'search=Matching&amp;view=threads&amp;page=2' in text
    _, foreign = _page(f, '/inbox', page=2, thread=f.foreign_thread_id)
    assert foreign['selected_thread'] is None
