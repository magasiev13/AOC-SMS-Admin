"""New inbound origin is recorded at processing time, never inferred later."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest


_support_spec = importlib.util.spec_from_file_location(
    'twinevia_keyword_origin_fixture', Path(__file__).with_name('test_automation_reply_outbox.py'),
)
_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(_support)


@pytest.fixture
def inbox():
    # Reuse the exact-target SaaS fixture, including its mocked queue/provider,
    # without inheriting or collecting another copy of its component tests.
    fixture = _support.TestAutomationReplyOutbox(methodName='runTest')
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def receive(inbox, body, sid, *, profile=None):
    payload = inbox._payload(body, sid)
    if profile is not None:
        payload.update(To=profile.from_number, MessagingServiceSid=profile.messaging_service_sid,
                       AccountSid=profile.twilio_subaccount_sid)
    result = inbox.inbox.process_inbound_sms(payload)
    inbox.db.session.expire_all()
    message = inbox.models.InboxMessage.query.filter_by(message_sid=sid, direction='inbound').one()
    return result, message


def test_new_keyword_match_records_selected_rule_origin_and_normalized_trigger(inbox):
    result, message = receive(inbox, '  heLLo   extra context  ', 'SM-origin-rule')
    assert result['status'] == 'keyword_reply'
    assert (message.automation_source, message.automation_source_id) == ('keyword', inbox.rule.id)
    assert message.matched_keyword == 'HELLO'
    saved = inbox.models.AutomationReplyOutbox.query.one()
    assert (saved.source, saved.source_id) == ('keyword', inbox.rule.id)
    assert saved.body == 'Hello back!'
    assert inbox.calls == []


def test_full_keyword_match_origin_wins_over_first_word_rule(inbox):
    full = inbox.models.KeywordAutomationRule(organization_id=inbox.org_id, keyword='HELLO WORLD',
                                              response_body='Exact phrase response', is_active=True)
    inbox.db.session.add(full)
    inbox.db.session.commit()
    result, message = receive(inbox, 'hello\t WORLD', 'SM-origin-full-phrase')
    assert result['status'] == 'keyword_reply'
    assert (message.matched_keyword, message.automation_source, message.automation_source_id) == ('HELLO WORLD', 'keyword', full.id)
    assert inbox.models.AutomationReplyOutbox.query.one().body == 'Exact phrase response'


def test_deleted_and_recreated_keyword_does_not_rewrite_original_inbound_origin(inbox):
    _, original = receive(inbox, 'HELLO', 'SM-origin-before-delete')
    original_id, origin_id, received_at = original.id, inbox.rule.id, original.created_at
    inbox.db.session.delete(inbox.rule)
    inbox.db.session.commit()
    replacement = inbox.models.KeywordAutomationRule(id=origin_id + 100, organization_id=inbox.org_id,
        keyword='HELLO', response_body='Replacement response', is_active=True)
    inbox.db.session.add(replacement)
    inbox.db.session.commit()
    result, old = receive(inbox, 'HELLO', 'SM-origin-before-delete')
    assert result['status'] == 'duplicate'
    assert (old.id, old.automation_source, old.automation_source_id, old.created_at) == (original_id, 'keyword', origin_id, received_at)
    result, new = receive(inbox, 'HELLO', 'SM-origin-after-recreate')
    assert result['status'] == 'keyword_reply'
    assert (new.automation_source, new.automation_source_id) == ('keyword', replacement.id)
    assert replacement.match_count == 1
    assert inbox.models.AutomationReplyOutbox.query.count() == 2


def test_active_survey_start_pending_empty_and_answers_retain_actual_survey_origin(inbox):
    result, trigger = receive(inbox, 'survey', 'SM-origin-survey-start')
    assert result['status'] == 'survey_started'
    assert (trigger.automation_source, trigger.automation_source_id, trigger.matched_keyword) == ('survey', inbox.survey.id, 'SURVEY')
    result, pending = receive(inbox, 'Early answer', 'SM-origin-survey-pending')
    assert result['status'] == 'survey_reply_pending'
    assert (pending.automation_source, pending.automation_source_id) == ('survey', inbox.survey.id)
    assert inbox.models.SurveyResponse.query.count() == 0
    result, empty = receive(inbox, '   ', 'SM-origin-survey-empty')
    assert result['status'] == 'survey_ignored_empty'
    assert (empty.automation_source, empty.automation_source_id) == ('survey', inbox.survey.id)
    inbox._drain()
    result, first = receive(inbox, 'First answer', 'SM-origin-survey-first')
    assert result['status'] == 'survey_response'
    assert (first.automation_source, first.automation_source_id, first.matched_keyword) == ('survey', inbox.survey.id, 'SURVEY')
    inbox._drain()
    result, final = receive(inbox, 'Final answer', 'SM-origin-survey-final')
    assert result['status'] == 'survey_response'
    assert (final.automation_source, final.automation_source_id) == ('survey', inbox.survey.id)
    assert inbox.models.SurveySession.query.one().status == 'completed'
    assert inbox.models.SurveyResponse.query.count() == 2
    assert [row.source for row in inbox._rows()] == ['survey'] * 4


@pytest.mark.parametrize('keyword', ['STOP', 'CANCEL', 'QUIT'])
def test_transport_stop_precedence_does_not_claim_survey_or_rule_origin(inbox, keyword):
    receive(inbox, 'SURVEY', 'SM-origin-stop-survey')
    result, message = receive(inbox, keyword, 'SM-origin-stop-' + keyword)
    assert result['status'] == 'opt_out'
    assert (message.automation_source, message.automation_source_id, message.matched_keyword) == (None, None, None)
    assert inbox.models.UnsubscribedContact.query.filter_by(organization_id=inbox.org_id, phone=inbox.phone).count() == 1
    assert inbox.models.SurveySession.query.one().status == 'cancelled'
    assert inbox.calls == []


def test_paused_ordinary_inbound_is_not_claimed_as_processed_survey_traffic(inbox):
    receive(inbox, 'SURVEY', 'SM-origin-paused-start')
    inbox.profile.set_provider_status('suspended')
    inbox.db.session.commit()
    result, message = receive(inbox, 'Paused answer', 'SM-origin-paused-answer')
    assert result['status'] == 'stored'
    assert (message.automation_source, message.automation_source_id, message.matched_keyword) == (None, None, None)
    assert inbox.models.SurveySession.query.one().current_question_index == 0
    assert inbox.models.SurveyResponse.query.count() == 0
    assert len(inbox._rows()) == 2
    assert inbox.calls == []


def test_legacy_duplicate_and_unmatched_text_keep_unknown_origin(inbox):
    thread = inbox.models.InboxThread(organization_id=inbox.org_id, phone=inbox.phone)
    inbox.db.session.add(thread)
    inbox.db.session.flush()
    legacy = inbox.models.InboxMessage(organization_id=inbox.org_id, thread_id=thread.id, phone=inbox.phone,
        direction='inbound', body='HELLO', message_sid='SM-origin-legacy', matched_keyword='HELLO')
    inbox.db.session.add(legacy)
    inbox.db.session.commit()
    result, old = receive(inbox, 'HELLO', 'SM-origin-legacy')
    assert result['status'] == 'duplicate'
    assert (old.automation_source, old.automation_source_id) == (None, None)
    result, ordinary = receive(inbox, 'No matching automation here', 'SM-origin-unmatched')
    assert result['status'] == 'stored'
    assert (ordinary.automation_source, ordinary.automation_source_id, ordinary.matched_keyword) == (None, None, None)
    assert inbox.models.AutomationReplyOutbox.query.count() == 0
    assert inbox.rule.match_count == 0


def test_same_keyword_in_two_tenants_records_each_actual_rule(inbox):
    other = inbox.models.Organization(name='Other origin tenant', slug='other-origin-tenant', status='active')
    profile = inbox.models.OrganizationMessagingProfile(organization=other, provider_mode='platform_managed',
        twilio_subaccount_sid='AC' + 'b' * 32, messaging_service_sid='MG' + 'b' * 32,
        from_number='+17205559001', inbound_identity='+17205559001', provider_status='active',
        status='active', sender_review_status='approved')
    subscription = inbox.models.OrganizationSubscription(organization=other, status='complimentary')
    inbox.db.session.add_all([other, profile, subscription])
    inbox.db.session.flush()
    rule = inbox.models.KeywordAutomationRule(organization_id=other.id, keyword='HELLO', response_body='Other tenant', is_active=True)
    inbox.db.session.add(rule)
    inbox.db.session.commit()
    _, first = receive(inbox, 'HELLO', 'SM-origin-first-tenant')
    _, second = receive(inbox, 'HELLO', 'SM-origin-other-tenant', profile=profile)
    assert (first.organization_id, first.automation_source, first.automation_source_id) == (inbox.org_id, 'keyword', inbox.rule.id)
    assert (second.organization_id, second.automation_source, second.automation_source_id) == (other.id, 'keyword', rule.id)
    assert first.thread_id != second.thread_id
    assert inbox.calls == []


def test_manual_reply_retains_manual_origin_after_matched_inbound(inbox):
    from app.tenant import organization_context

    _, inbound = receive(inbox, 'HELLO', 'SM-origin-manual-inbound')
    with organization_context(inbox.org_id), patch('app.services.inbox_service.get_twilio_service', return_value=inbox.provider):
        result = inbox.inbox.send_thread_reply(inbound.thread_id, 'A person replied', actor='Owner')
    assert result['success'] is True
    manual = inbox.models.InboxMessage.query.filter_by(direction='outbound', body='A person replied').one()
    assert (manual.automation_source, manual.automation_source_id, manual.matched_keyword) == ('manual', None, None)
    assert (inbound.automation_source, inbound.automation_source_id) == ('keyword', inbox.rule.id)
    assert len(inbox.calls) == 1

