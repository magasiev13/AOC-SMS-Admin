"""Displayed contact pages must not truncate event counts, audiences or exports."""
import csv
from contextlib import contextmanager
from html.parser import HTMLParser
from io import StringIO
from urllib.parse import parse_qs, urlsplit

from flask import template_rendered
import pytest

from tests.test_event_feedback import feedback


class PageLinks(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.region = None
        self.links = {'Messaging Contact Pages': [], 'Booking pages': []}
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'nav' and attrs.get('aria-label') in self.links:
            self.region = attrs['aria-label']
        if tag == 'a' and self.region:
            self.links[self.region].append(attrs['href'])

    def handle_endtag(self, tag):
        if tag == 'nav':
            self.region = None


@contextmanager
def event_context(app):
    rendered = []

    def capture(sender, template, context, **extra):
        if template.name == 'events/detail.html':
            rendered.append(context.copy())

    with template_rendered.connected_to(capture, app):
        yield rendered


def get_page(f, client=None, **query):
    with event_context(f.app) as rendered:
        response = (client or f.client).get(f'/events/{f.event.id}', query_string=query)
    assert response.status_code == 200
    assert len(rendered) == 1
    return response, rendered[0]


@pytest.fixture
def paged_event(feedback):
    f = feedback
    from app.booking_models import EventBooking
    from app.services.recipient_permission_service import record_permission_evidence

    registrations = []
    with f.organization_context(f.org.id):
        for index in range(237):
            row = f.m.EventRegistration(organization=f.org, event=f.event,
                name=f'Registrant {index // 5:03d}', phone=f'+1720556{index:04d}',
                booking_spaces=index % 3 + 1)
            registrations.append(row)
            f.db.session.add(row)
            record_permission_evidence(f.org.id, row.phone, purpose='event', event_id=f.event.id,
                decision='granted', source='pagination-fixture', evidence_key=f'grant-{index}',
                verified=True, metadata={'proof': 'Synthetic event registration permission.'})
        f.db.session.add(f.m.UnsubscribedContact(organization=f.org, phone=registrations[0].phone))
        f.db.session.add(f.m.SuppressedContact(organization=f.org, phone=registrations[102].phone,
            reason='Synthetic manual exclusion', category='manual'))
        record_permission_evidence(f.org.id, registrations[-1].phone,
            purpose='event', event_id=f.event.id, decision='denied', source='pagination-fixture',
            evidence_key='withdraw-last', metadata={'proof': 'Synthetic withdrawal.'})
        for index in range(211):
            f.db.session.add(EventBooking(organization_id=f.org.id, event_id=f.event.id,
                source='manual', source_scope='pagination-fixture', external_booking_id=f'cancelled-{index}',
                name=f'Cancelled booking {index:03d}', status='cancelled', is_active=False,
                party_size=0, contact_state='missing'))
        f.db.session.commit()
    # Include both an ordinary other-event record and an adversarial mismatched
    # row. An event-id filter alone must not expose either foreign tenant row.
    f.db.session.add_all([
        f.m.EventRegistration(organization=f.other, event=f.other_event,
            name='Foreign private event contact', phone='+17205569998'),
        f.m.EventRegistration(organization=f.other, event=f.event,
            name='Foreign tenant matching event', phone='+17205569999'),
    ])
    f.db.session.commit()
    f.pagination_rows = sorted(registrations, key=lambda row: (row.name, row.phone, row.id))
    f.pagination_phones = [row.phone for row in registrations]
    return f


def test_contact_pages_are_stable_bounded_and_keep_independent_booking_pages(paged_event):
    f = paged_event
    seen = []
    for number in [1, 2, 3]:
        response, context = get_page(f, contact_page=number, booking_page=2)
        rows = context['registrations']
        assert [row.id for row in rows] == [row.id for row in f.pagination_rows[(number - 1) * 100:number * 100]]
        assert len(rows) == (100 if number < 3 else 37)
        assert context['registration_total'] == 237 and context['contact_pages'] == 3
        assert context['contact_page'] == number and context['booking_page'] == 2
        seen.extend(row.id for row in rows)
        text = response.get_data(as_text=True)
        assert f'Showing {(number - 1) * 100 + 1}–{min(number * 100, 237)} of 237 messaging contacts' in text
        assert f'Registrant {0 if number > 1 else 47:03d}' not in text
        for private in ['Foreign private event contact', 'Foreign tenant matching event', '+17205569998', '+17205569999']:
            assert private not in text
        links = PageLinks(response).links
        assert links['Messaging Contact Pages']
        for href in links['Messaging Contact Pages']:
            parsed = urlsplit(href)
            query = parse_qs(parsed.query)
            assert query['booking_page'] == ['2'] and parsed.fragment == 'registrations'
            assert int(query['contact_page'][0]) in {number - 1, number + 1}
        assert len(links['Booking pages']) == 2
        for href in links['Booking pages']:
            parsed = urlsplit(href)
            query = parse_qs(parsed.query)
            assert query['contact_page'] == [str(number)] and parsed.fragment == 'registrations'
            assert int(query['booking_page'][0]) in {1, 3}
    assert len(seen) == 237 and len(set(seen)) == 237


def test_contact_page_arguments_clamp_without_losing_records(paged_event):
    f = paged_event
    for supplied, expected in [('-20', 1), ('0', 1), ('not-a-page', 1), ('999999', 3)]:
        _, context = get_page(f, contact_page=supplied)
        assert context['contact_page'] == expected
        assert [row.id for row in context['registrations']] == [
            row.id for row in f.pagination_rows[(expected - 1) * 100:expected * 100]]


def test_full_event_counts_preview_and_export_are_independent_of_display_page(paged_event):
    f = paged_event
    stats = []
    for page in [1, 2, 3]:
        response, context = get_page(f, contact_page=page)
        values = {row['label'].casefold(): row['value'] for row in context['surface_view']['stats']}
        stats.append(values)
        assert values['reachable contacts'] == 234
        assert values['reserved places'] == sum(row.capacity_spaces for row in f.pagination_rows)
        assert b'3 contacts excluded by opt-outs, blocks or permission rules.' in response.data
    assert stats[0] == stats[1] == stats[2]
    for client in [f.client, f.staff_client]:
        preview = client.get('/dashboard/audience-preview', query_string={
            'target': 'event', 'event_id': f.event.id, 'contact_page': 3})
        assert preview.status_code == 200
        assert preview.json['total'] == 237 and preview.json['eligible'] == 234
        assert preview.json['excluded'] == 3
        assert client.get('/dashboard/audience-preview', query_string={
            'target': 'event', 'event_id': f.other_event.id}).status_code == 404
    exported = f.client.get(f'/events/{f.event.id}/export?contact_page=3')
    assert exported.status_code == 200
    rows = list(csv.DictReader(StringIO(exported.get_data(as_text=True))))
    assert len(rows) == 237
    # CSV formula-injection protection deliberately prefixes leading '+'.
    assert {row['phone'] for row in rows} == {"'" + phone for phone in f.pagination_phones}
    assert sum(row['sms_eligibility'] == 'sendable' for row in rows) == 234
    assert sum(row['sms_eligibility'] == 'blocked' for row in rows) == 3
    assert f.staff_client.get(f'/events/{f.event.id}/export?contact_page=3').status_code == 403


def test_staff_contact_pages_keep_names_but_no_raw_phone_data(paged_event):
    f = paged_event
    for page in [1, 2, 3]:
        response, context = get_page(f, f.staff_client, contact_page=page, booking_page=2)
        text = response.get_data(as_text=True)
        assert context['registration_total'] == 237
        assert f.pagination_rows[(page - 1) * 100].name in text
        assert 'data-event-registration-tool' not in text
        assert 'name="phone"' not in text
        for private in f.pagination_phones + ['+17205569998', '+17205569999', f.profile.from_number]:
            assert private not in text
        assert 'Foreign tenant matching event' not in text
    assert f.staff_client.get(f'/events/{f.other_event.id}?contact_page=2').status_code == 404


def test_empty_event_has_one_empty_contact_page(feedback):
    response, context = get_page(feedback, contact_page=999999)
    assert context['registration_total'] == 0
    assert context['contact_page'] == 1 and context['contact_pages'] == 1
    assert context['registrations'] == []
    assert b'No messaging contacts' in response.data
    assert PageLinks(response).links['Messaging Contact Pages'] == []
