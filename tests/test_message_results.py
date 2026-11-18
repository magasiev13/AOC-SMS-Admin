"""Message records show saved acceptance and current receipts independently."""
from contextlib import contextmanager
from datetime import datetime
import json
from unittest.mock import patch

from flask import template_rendered
import pytest

from tests.test_event_feedback import feedback


@pytest.fixture
def results(feedback):
    f = feedback
    details = []
    with f.organization_context(f.org.id):
        for index in range(125):
            phone = f'+1720557{index:04d}'
            detail = {'phone': phone, 'name': f'Recipient {index:03d}'}
            if index < 90 or index >= 115:
                detail.update(success=True, status='sent', sid=f'SMresult{index:03d}')
                delivery = 'delivered' if index < 60 else ('undelivered' if index >= 115 else 'sent')
                f.db.session.add(f.m.MessagingUsageRecord(
                    organization_id=f.org.id, message_sid=detail['sid'], delivery_status=delivery,
                    delivery_error_code='30003' if index >= 115 else None,
                    delivery_error_message=('Provider error 30003' if index % 2 else 'Unreachable destination handset') if index >= 115 else None,
                ))
            elif index < 110:
                detail.update(success=False, status='failed', error='Provider rejected this submission.')
            else:
                detail.update(success=False, status='ambiguous', error='Provider acceptance remains unresolved.')
            details.append(detail)
        log = f.m.MessageLog(organization_id=f.org.id, message_body='Saved audience message',
            target='community', status='sent', total_recipients=125, success_count=100,
            failure_count=20, details=json.dumps(details), created_at=datetime(2026, 10, 31, 14, 2))
        f.db.session.add(log)
        f.db.session.commit()
    f.log, f.details = log, details
    return f


@contextmanager
def _context(app):
    captured = []

    def capture(sender, template, context, **extra):
        if template.name == 'logs/detail.html':
            captured.append(context.copy())

    with template_rendered.connected_to(capture, app):
        yield captured


def _get(f, client=None, **query):
    with _context(f.app) as captured:
        response = (client or f.client).get(f'/logs/{f.log.id}', query_string=query)
    assert response.status_code == 200
    assert len(captured) == 1
    return response, captured[0]


def test_summary_has_every_outcome_and_never_rewrites_submission_evidence(results):
    f = results
    saved = (f.log.success_count, f.log.failure_count, f.log.details)
    response, context = _get(f)
    assert context['result_summary'] == {
        'total': 125, 'accepted': 100, 'not_submitted': 20,
        'delivered': 60, 'not_delivered': 10, 'no_receipt': 30, 'acceptance_unresolved': 5,
    }
    assert [(row['key'], row['value']) for row in context['surface_view']['stats']] == [
        ('accepted', 100), ('not_submitted', 20), ('delivered', 60), ('not_delivered', 10), ('no_receipt', 30),
    ]
    body = response.get_data(as_text=True)
    for label in ['Accepted by provider', 'Not submitted', 'Delivered', 'Not delivered', 'No receipt yet']:
        assert label in body
    assert 'Provider acceptance remains unresolved for 5 recipients' in body
    f.db.session.refresh(f.log)
    assert (f.log.success_count, f.log.failure_count, f.log.details) == saved


def test_recipient_pages_are_bounded_stable_and_failures_come_first(results):
    f = results
    seen = []
    for page in (1, 2, 3):
        _, context = _get(f, page=page)
        assert context['total_count'] == 125 and context['total_pages'] == 3
        assert len(context['details']) == (50 if page < 3 else 25)
        seen.extend(row['name'] for row in context['details'])
        if page == 1:
            assert [row['result_category'] for row in context['details'][:10]] == ['not_delivered'] * 10
            assert context['details'][0]['name'] == 'Recipient 115'
            assert [row['result_category'] for row in context['details'][10:30]] == ['not_submitted'] * 20
    expected_indexes = [*range(115, 125), *range(90, 110), *range(110, 115), *range(60, 90), *range(60)]
    assert seen == [f'Recipient {index:03d}' for index in expected_indexes]


@pytest.mark.parametrize('outcome,count,category', [
    ('not_delivered', 10, 'not_delivered'), ('no_receipt', 30, 'no_receipt'), ('not_submitted', 20, 'not_submitted'),
])
def test_filters_keep_distinct_outcomes_and_full_summary(results, outcome, count, category):
    response, context = _get(results, outcome=outcome, page=999)
    assert context['page'] == 1 and context['total_count'] == count
    assert len(context['details']) == count
    assert {row['result_category'] for row in context['details']} == {category}
    assert context['result_summary']['accepted'] == 100 and context['result_summary']['no_receipt'] == 30
    assert f'outcome={outcome}' in response.get_data(as_text=True)


@pytest.mark.parametrize('query,expected_page', [({'page': 'bad'}, 1), ({'page': -1}, 1), ({'page': 999}, 3), ({'outcome': 'invalid'}, 1)])
def test_invalid_recipient_parameters_are_clamped(results, query, expected_page):
    _, context = _get(results, **query)
    assert context['page'] == expected_page and context['outcome'] == 'all'


def test_carrier_errors_have_one_code_wording(results):
    response, context = _get(results, outcome='not_delivered')
    assert {row['delivery_error'] for row in context['details']} == {'Unreachable destination handset (30003)'}
    assert 'Provider error 30003' not in response.get_data(as_text=True)


@pytest.mark.parametrize('code,description', [
    ('30003', 'Unreachable destination handset'),
    ('30004', 'Message blocked'),
    ('30005', 'Unknown destination handset'),
    ('30006', 'Landline or unreachable carrier'),
    ('30007', 'Message filtered'),
    ('30008', 'Unknown delivery error'),
    ('30034', 'Unregistered sender for US A2P 10DLC'),
    ('21610', 'Recipient opted out'),
])
def test_common_carrier_code_wording_is_independent_of_receipt_description(code, description):
    from app.services.delivery_status_service import _canonical_delivery_error

    for provider_description in (None, f'Provider error {code}', 'Different polling or callback description'):
        assert _canonical_delivery_error(provider_description, code) == f'{description} ({code})'
        assert _canonical_delivery_error(provider_description, int(code)) == f'{description} ({code})'
    assert _canonical_delivery_error(f'Provider error {code}') == f'{description} ({code})'
    assert _canonical_delivery_error(f'Carrier description ({code})') == f'{description} ({code})'
    assert _canonical_delivery_error(f'Provider error {code}0') == f'Provider error {code}0'


@pytest.mark.parametrize('code,messages,expected', [
    ('30005', ['Provider error 30005', 'Different retained provider description'],
     ['Unknown destination handset (30005)', 'Unknown destination handset (30005)']),
    (None, ['Provider error 30005', 'Different retained description (30005)'],
     ['Unknown destination handset (30005)', 'Unknown destination handset (30005)']),
    ('39999', ['New provider error description', 'Different retained provider description'],
     ['New provider error description (39999)', 'Different retained provider description (39999)']),
    ('39999', [None, ''], ['Provider error (39999)', 'Provider error (39999)']),
])
def test_coded_carrier_error_projection_retains_provider_detail_and_raw_audit_messages(results, code, messages, expected):
    f = results
    records = [f.m.MessagingUsageRecord.query.filter_by(
        organization_id=f.org.id, message_sid=f'SMresult{index}',
    ).one() for index in (115, 116)]
    for record, message in zip(records, messages):
        record.delivery_error_code = code
        record.delivery_error_message = message
    f.db.session.commit()
    original_details = f.log.details
    response, context = _get(f, outcome='not_delivered')
    rows = [row for row in context['details'] if row['name'] in {'Recipient 115', 'Recipient 116'}]
    assert len(rows) == 2
    assert [row['delivery_error'] for row in rows] == expected
    for description in expected:
        assert description in response.get_data(as_text=True)
    for record, message in zip(records, messages):
        f.db.session.refresh(record)
        assert record.delivery_error_code == code and record.delivery_error_message == message
    f.db.session.refresh(f.log)
    assert f.log.details == original_details


def test_processing_and_conflicting_receipt_evidence_remain_visible(results):
    f = results
    f.log.status = 'processing'
    record = f.m.MessagingUsageRecord.query.filter_by(organization_id=f.org.id, message_sid='SMresult115').one()
    record.delivery_status = 'conflict'
    record.delivery_error_code = None
    record.delivery_error_message = 'Conflicting authenticated terminal receipts.'
    f.db.session.commit()
    response, context = _get(f, outcome='no_receipt')
    assert 'Provider submission in progress' in response.get_data(as_text=True)
    assert context['result_summary']['not_delivered'] == 9 and context['result_summary']['no_receipt'] == 31
    conflict = next(row for row in context['details'] if row['name'] == 'Recipient 115')
    assert conflict['receipt_label'] == 'Conflicting delivery receipts'
    assert conflict['delivery_tone'] == 'warning text-dark'


def test_created_at_occurs_once_in_workspace_time_independent_of_browser_cookie(results):
    f = results
    f.app.config['APP_TIMEZONE'] = 'America/Denver'
    f.client.set_cookie('client_timezone', 'Asia/Tokyo')
    response, context = _get(f)
    assert context['surface_view']['created_at_display'] == 'Sat, Oct 31, 2026 · 8:02 AM MDT'
    body = response.get_data(as_text=True)
    assert body.count('Sat, Oct 31, 2026 · 8:02 AM MDT') == 1
    assert 'UTC' not in body


def test_staff_omits_numbers_in_all_recipient_pages_and_free_text(results):
    f = results
    f.details[115]['name'] = 'Call +17205558888'
    f.details[90]['error'] = 'Rejected +17205558889'
    f.log.message_body = 'Contact +17205558890 for details.'
    f.log.details = json.dumps(f.details)
    f.db.session.commit()
    numbers = [row['phone'] for row in f.details] + ['+17205558888', '+17205558889', '+17205558890', f.profile.from_number]
    for page in (1, 2, 3):
        response, _ = _get(f, f.staff_client, page=page)
        body = response.get_data(as_text=True)
        for phone in numbers:
            assert phone not in body and phone[1:] not in body and phone[-10:] not in body
        assert '<th>Phone</th>' not in body


def test_foreign_log_and_foreign_receipts_cannot_be_read(results):
    f = results
    foreign = f.m.MessageLog(organization_id=f.other.id, target='community', message_body='Private other log')
    record = f.m.MessagingUsageRecord(organization_id=f.other.id, message_sid='SMforeignresult', delivery_status='delivered')
    f.db.session.add_all([foreign, record])
    f.db.session.commit()
    assert f.client.get(f'/logs/{foreign.id}').status_code == 404
    from app.services.delivery_status_service import project_log_delivery
    with f.organization_context(f.org.id):
        detail = {'success': True, 'sid': record.message_sid}
        assert project_log_delivery(f.org.id, [detail])[0]['delivery_status'] == 'pending'
        assert project_log_delivery(f.org.id, [detail], records_by_sid={record.message_sid: record})[0]['delivery_status'] == 'pending'


def test_summary_preserves_aggregate_failure_count_and_deduplicates_receipts(results):
    f = results
    from app.services.delivery_status_service import message_result_summary
    f.log.failure_count = 25  # Legacy aggregates may include uncertain submissions.
    f.log.details = json.dumps([*f.details, f.details[0]])
    f.db.session.commit()
    with f.organization_context(f.org.id):
        result = message_result_summary(f.log)
    assert result['not_submitted'] == 25 and result['delivered'] == 60
    assert result['no_receipt'] == 30 and result['acceptance_unresolved'] == 5


def test_accepted_legacy_row_without_sid_waits_for_receipt_and_batch_shares_queries(results):
    f = results
    from app.services import delivery_status_service as service
    legacy = f.m.MessageLog(organization_id=f.org.id, target='community', message_body='Legacy accepted',
        total_recipients=1, success_count=1, failure_count=0,
        details=json.dumps({'results': [{'success': True, 'name': 'Legacy recipient'}]}))
    f.db.session.add(legacy)
    f.db.session.commit()
    with f.organization_context(f.org.id), patch.object(service, '_delivery_records', wraps=service._delivery_records) as read:
        summaries = service.batch_message_results([f.log, legacy])
    assert read.call_count == 1
    assert summaries[legacy.id]['no_receipt'] == 1 and summaries[legacy.id]['not_submitted'] == 0
    response = f.client.get(f'/logs/{legacy.id}?outcome=no_receipt')
    assert response.status_code == 200 and 'Legacy recipient' in response.get_data(as_text=True)
