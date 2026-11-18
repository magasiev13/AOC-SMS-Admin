"""Send and history lead with receipt facts without relabeling submission failures."""
import json
from sqlalchemy import event

from tests.test_event_feedback import feedback


def seed_outcomes(feedback):
    m, db = feedback.m, feedback.db
    rows = []
    for index, state in enumerate(['delivered', 'undelivered', 'sent']):
        sid = f'SMsummaryfixture{index}'
        rows.append({'sid': sid, 'success': True, 'name': f'Fixture {index}'})
        db.session.add(m.MessagingUsageRecord(organization_id=feedback.org.id, message_sid=sid,
            delivery_status=state))
    rows.append({'success': False, 'status': 'failed', 'name': 'Not submitted', 'error': 'Fixture rejection'})
    log = m.MessageLog(organization_id=feedback.org.id, target='community', status='sent',
        message_body='Mixed delivery summary fixture', total_recipients=4, success_count=3,
        failure_count=1, details=json.dumps(rows))
    db.session.add(log)
    db.session.commit()
    return log


def test_send_screen_leads_with_delivery_and_keeps_all_outcomes_distinct(feedback):
    seed_outcomes(feedback)
    response = feedback.client.get('/dashboard')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-last-message-delivery' in html and '1 of 4' in html
    assert 'recipients delivered' in html
    assert '3 accepted by provider · 1 not submitted · 1 not delivered · 1 no receipt yet' in html
    assert 'Delivery Confirmed' in html and 'Provider Acceptance</div>' not in html


def test_history_table_phone_cards_and_preview_include_delivery_counts(feedback):
    seed_outcomes(feedback)
    response = feedback.client.get('/logs')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('1 delivered · 1 not delivered') == 3
    assert html.count('3 accepted by provider · 1 not submitted · 1 no receipt yet') == 3
    assert '1 failed' not in html


def test_history_delivery_projection_batches_receipt_reads(feedback):
    seed_outcomes(feedback)
    for index in range(12):
        feedback.db.session.add(feedback.m.MessageLog(organization_id=feedback.org.id,
            message_body=f'Batch fixture {index}', target='community', status='sent', total_recipients=1,
            success_count=1, failure_count=0, details=json.dumps([{'sid':f'SMbatchfixture{index}', 'success':True}])))
    feedback.db.session.commit()
    statements = []
    def capture(_connection, _cursor, statement, *_args):
        if statement.lstrip().lower().startswith('select') and 'messaging_usage_records' in statement.lower():
            statements.append(statement)
    event.listen(feedback.db.engine, 'before_cursor_execute', capture)
    try:
        assert feedback.client.get('/logs').status_code == 200
    finally:
        event.remove(feedback.db.engine, 'before_cursor_execute', capture)
    assert len(statements) == 1


def test_large_supported_history_page_bounds_sql_parameters_and_keeps_all_receipt_facts(feedback):
    from app.services.delivery_status_service import batch_message_results

    m, db = feedback.m, feedback.db
    logs = []
    for batch in range(15):
        rows = [{'sid': f'SMscalefixture{index:06d}', 'success': True}
                for index in range(batch * 5000, (batch + 1) * 5000)]
        log = m.MessageLog(organization_id=feedback.org.id, target='community', status='sent',
            message_body=f'Large retained send {batch}', total_recipients=5000, success_count=5000,
            failure_count=0, details=json.dumps(rows))
        db.session.add(log)
        logs.append(log)
    for index, state in [(0, 'delivered'), (10000, 'undelivered'), (74999, 'delivered')]:
        db.session.add(m.MessagingUsageRecord(organization_id=feedback.org.id,
            message_sid=f'SMscalefixture{index:06d}', delivery_status=state))
    # A foreign receipt whose SID appears in the local historical JSON must
    # never become delivery evidence for this workspace.
    db.session.add(m.MessagingUsageRecord(organization_id=feedback.other.id,
        message_sid='SMscalefixture020000', delivery_status='delivered'))
    db.session.commit()
    parameter_counts = []

    def capture(_connection, _cursor, statement, parameters, _context, _executemany):
        if statement.lstrip().lower().startswith('select') and 'messaging_usage_records' in statement.lower():
            parameter_counts.append(len(parameters))

    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        with feedback.organization_context(feedback.org.id):
            summaries = batch_message_results(logs)
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)
    assert len(parameter_counts) > 1
    assert max(parameter_counts) <= 10001  # 10,000 SIDs plus the tenant boundary.
    assert len(summaries) == 15
    assert sum(row['accepted'] for row in summaries.values()) == 75000
    assert sum(row['delivered'] for row in summaries.values()) == 2
    assert sum(row['not_delivered'] for row in summaries.values()) == 1
    assert sum(row['no_receipt'] for row in summaries.values()) == 74997
    assert summaries[logs[4].id]['delivered'] == 0
