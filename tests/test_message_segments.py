"""Message records total provider segment evidence without inventing missing units."""
import json
import re
from types import SimpleNamespace

import pytest

from app.services.delivery_status_service import message_segment_summary, project_log_delivery
from tests.test_event_feedback import feedback


def _summary(details, *, records=None, accepted=None):
    projected = project_log_delivery(1, details, records_by_sid=records or {})
    log = SimpleNamespace(success_count=len({row['sid'] for row in details if row.get('sid')})
                          if accepted is None else accepted)
    return message_segment_summary(log, projected)


def _record(segments, *, organization_id=1, units_status='known'):
    return SimpleNamespace(organization_id=organization_id, units_status=units_status,
                           billable_units=segments, delivery_status='delivered',
                           delivery_error_message=None, delivery_error_code=None)


def test_segments_count_each_sid_once_and_prefer_reconciled_provider_units():
    details = [
        {'sid': 'SMsaved', 'success': True, 'num_segments': '2'},
        {'sid': 'SMsaved', 'success': True, 'num_segments': '2'},
        {'sid': 'SMreconciled', 'success': True, 'num_segments': '9'},
        {'success': False, 'status': 'failed', 'num_segments': '8'},
    ]
    assert _summary(details, records={'SMreconciled': _record(3)}) == {
        'total': 5, 'confirmed_recipients': 2, 'pending_recipients': 0,
    }


@pytest.mark.parametrize('raw', [None, '', '0', 0, '-1', 'bad', '1.5', True])
def test_missing_or_initial_zero_provider_segments_remain_pending(raw):
    assert _summary([{'sid': 'SMpending', 'success': True, 'num_segments': raw}]) == {
        'total': 0, 'confirmed_recipients': 0, 'pending_recipients': 1,
    }


def test_confirmed_zero_is_kept_and_not_replaced_with_saved_positive_units():
    assert _summary([{'sid': 'SMzero', 'success': True, 'num_segments': '2'}],
                    records={'SMzero': _record(0)}) == {
        'total': 0, 'confirmed_recipients': 1, 'pending_recipients': 0,
    }


def test_partial_segments_and_unresolved_acceptance_do_not_claim_a_complete_total():
    assert _summary([
        {'sid': 'SMknown', 'success': True, 'num_segments': '2'},
        {'sid': 'SMpending', 'success': True, 'num_segments': '0'},
        {'success': True},
        {'success': False, 'status': 'ambiguous'},
        {'success': False, 'status': 'failed'},
    ], accepted=4) == {
        'total': 2, 'confirmed_recipients': 1, 'pending_recipients': 4,
    }


def test_foreign_receipt_units_are_not_projected_into_the_current_workspace():
    assert _summary([{'sid': 'SMforeign', 'success': True}],
                    records={'SMforeign': _record(999, organization_id=2)}) == {
        'total': 0, 'confirmed_recipients': 0, 'pending_recipients': 1,
    }


@pytest.fixture
def segment_record(feedback):
    f = feedback
    details = [{'sid': f'SMsegments{index}', 'name': f'Recipient {index:02d}',
                'phone': f'+1555012{index:04d}', 'success': True, 'status': 'sent',
                'num_segments': '1'} for index in range(60)]
    details.append({'name': 'Not submitted', 'success': False, 'status': 'failed', 'num_segments': '100'})
    details.append(dict(details[0]))
    with f.organization_context(f.org.id):
        log = f.m.MessageLog(organization_id=f.org.id, target='community', status='sent',
                            message_body='Segment evidence fixture', total_recipients=61,
                            success_count=60, failure_count=1, details=json.dumps(details))
        f.db.session.add_all([
            log,
            f.m.MessagingUsageRecord(organization_id=f.org.id, message_sid='SMsegments0',
                                     delivery_status='delivered', units_status='known', billable_units=2),
            f.m.MessagingUsageRecord(organization_id=f.org.id, message_sid='SMsegments1',
                                     delivery_status='undelivered', units_status='known', billable_units=3),
            f.m.MessagingUsageRecord(organization_id=f.other.id, message_sid='SMsegments2',
                                     delivery_status='delivered', units_status='known', billable_units=999),
        ])
        f.db.session.commit()
    f.log, f.segment_details = log, details
    return f


def _display(f, *, client=None, query=''):
    response = (client or f.client).get(f'/logs/{f.log.id}{query}')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    match = re.search(r'<span data-message-segments>(.*?)</span>', html)
    assert match
    return match.group(1), html


def test_message_record_segment_total_is_independent_of_recipient_page_and_filter(segment_record):
    f = segment_record
    saved = f.log.details
    for query in ('', '?page=2', '?outcome=not_delivered', '?outcome=not_submitted'):
        display, _ = _display(f, query=query)
        assert display == 'Segments used: 63'
    f.db.session.refresh(f.log)
    assert f.log.details == saved


def test_staff_sees_segment_total_without_phone_data_and_cannot_read_foreign_log(segment_record):
    f = segment_record
    display, html = _display(f, client=f.staff_client)
    assert display == 'Segments used: 63'
    assert '<th>Phone</th>' not in html
    for row in f.segment_details:
        if row.get('phone'):
            assert row['phone'] not in html and row['phone'][1:] not in html
    foreign_log = f.m.MessageLog(organization_id=f.other.id, target='community',
                               message_body='Other workspace segment record', success_count=1)
    f.db.session.add(foreign_log)
    f.db.session.commit()
    assert f.client.get(f'/logs/{foreign_log.id}').status_code == 404


def test_message_record_labels_partial_units_and_keeps_unknown_units_pending(segment_record):
    f = segment_record
    f.log.details = json.dumps([
        {'sid': 'SMsegments0', 'success': True},
        {'sid': 'SMpending', 'success': True, 'num_segments': '0'},
    ])
    f.log.success_count = 2
    f.db.session.commit()
    display, _ = _display(f)
    assert display == 'Segments used: 2 confirmed; remaining segments pending provider confirmation'
    f.log.details = json.dumps([{'sid': 'SMpending', 'success': True, 'num_segments': '0'}])
    f.log.success_count = 1
    f.db.session.commit()
    display, _ = _display(f)
    assert display == 'Segments pending provider confirmation'


def test_message_record_can_display_provider_confirmed_zero(segment_record):
    f = segment_record
    record = f.m.MessagingUsageRecord.query.filter_by(message_sid='SMsegments0').one()
    record.billable_units = 0
    f.log.details = json.dumps([{'sid': record.message_sid, 'success': True, 'num_segments': '2'}])
    f.log.success_count = 1
    f.db.session.commit()
    display, _ = _display(f)
    assert display == 'Segments used: 0'
