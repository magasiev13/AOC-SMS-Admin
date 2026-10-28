"""Feedback collection is independent of attendance and safe for staff views."""
import importlib
import json
import logging
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def feedback(tmp_path, monkeypatch):
    root = os.environ.get('FEEDBACK_TEST_POSTGRES_ROOT')
    database_name = None
    if root:
        if root != 'postgresql://audit@127.0.0.1:55432/postgres':
            raise RuntimeError('Feedback tests require the owned loopback PostgreSQL audit root.')
        import psycopg
        from psycopg import sql
        database_name = 'event_feedback_' + uuid4().hex
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(database_name)))
        uri = 'postgresql+psycopg://audit@127.0.0.1:55432/' + database_name
    else:
        uri = 'sqlite:///' + str(tmp_path / 'feedback.db')
    keys = {'DATABASE_URL':uri, 'SAAS_MODE':'1', 'FLASK_DEBUG':'1', 'SCHEDULER_ENABLED':'0',
            'PYTHON_DOTENV_DISABLED':'1', 'SECRET_KEY':'isolated-event-feedback', 'AUTH_ALERTS_ENABLED':'0',
            'REDIS_URL':'redis://127.0.0.1:1/0'}
    saved = {key:os.environ.get(key) for key in keys}
    for key, value in keys.items():
        monkeypatch.setenv(key, value)
    import app.config
    importlib.reload(app.config)
    from app import create_app, db, models
    from app.services import inbox_service as inbox, automation_outbox_service as outbox
    from app.services import event_feedback_service as service
    from app.services.twilio_service import TwilioService
    from app.tenant import organization_context
    application = create_app(run_startup_tasks=False, start_scheduler=False)
    from flask import g, has_app_context
    from flask.testing import FlaskClient
    class FreshIdentityClient(FlaskClient):
        def open(self, *args, **kwargs):
            # This fixture retains an app context for model assertions. Real
            # requests get a fresh g; do not cache one actor across two clients.
            if has_app_context():
                g.pop('_login_user', None)
            return super().open(*args, **kwargs)
    application.test_client_class = FreshIdentityClient
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                              TWILIO_ALLOW_LIVE_SENDS_IN_TESTING=True, TWILIO_BROWSER_FAKE_SENDS=False,
                              SURVEY_START_QUESTION_DELAY_SECONDS=0)
    try:
        with application.app_context():
            db.create_all()
            org = models.Organization(name='Community feedback', slug='feedback-community')
            other = models.Organization(name='Other workspace', slug='feedback-other')
            owner = models.AppUser(username='feedback-owner', email='owner@feedback.test', phone='+17205550911', role='admin')
            owner.set_password('Feedback-fixture-pass1!')
            staff = models.AppUser(username='feedback-staff', email='staff@feedback.test', phone='+17205550912', role='admin')
            staff.set_password('Feedback-fixture-pass1!')
            db.session.add_all([org, other, owner, staff])
            db.session.flush()
            profile = models.OrganizationMessagingProfile(organization=org, provider_mode='platform_managed',
                status='active', provider_status='active', sender_review_status='approved',
                twilio_subaccount_sid='AC'+'f'*32, messaging_service_sid='MG'+'f'*32,
                phone_number_sid='PNfeedbackfixture', from_number='+17205550999', inbound_identity='+17205550999')
            db.session.add_all([profile, models.OrganizationSubscription(organization=org, status='complimentary'),
                models.OrganizationMembership(organization=org, user=owner, role='owner'),
                models.OrganizationMembership(organization=org, user=staff, role='staff')])
            for policy, key in [('terms','TERMS_POLICY_VERSION'),('privacy','PRIVACY_POLICY_VERSION'),
                                ('acceptable_use','ACCEPTABLE_USE_POLICY_VERSION'),('sms','SMS_POLICY_VERSION'),('billing','BILLING_POLICY_VERSION')]:
                db.session.add(models.CustomerPolicyAcceptance(organization=org, user=owner,
                    policy_name=policy, policy_version=application.config[key]))
            event = models.Event(organization=org, title='Past community celebration', date=models.utc_now().date()-timedelta(days=10))
            other_event = models.Event(organization=other, title='Private other event')
            db.session.add_all([event, other_event])
            db.session.commit()
            provider = TwilioService.__new__(TwilioService)
            provider.organization_id, provider.account_sid = org.id, profile.twilio_subaccount_sid
            provider.from_number, provider.messaging_service_sid = profile.from_number, profile.messaging_service_sid
            provider.client = MagicMock()
            provider.client.messages.create.side_effect = lambda **params: SimpleNamespace(
                sid='SM'+uuid4().hex, status='queued', account_sid=profile.twilio_subaccount_sid,
                num_segments='1', price=None, price_unit='usd')
            client, staff_client = application.test_client(), application.test_client()
            client.post('/login', data={'username':owner.email,'password':'Feedback-fixture-pass1!'})
            staff_client.post('/login', data={'username':staff.email,'password':'Feedback-fixture-pass1!'})
            yield SimpleNamespace(app=application, db=db, m=models, service=service, inbox=inbox, outbox=outbox,
                org=org, other=other, owner=owner, staff=staff, event=event, other_event=other_event,
                profile=profile, client=client, staff_client=staff_client, provider=provider,
                organization_context=organization_context, root=root)
            db.session.remove()
            db.engine.dispose()
    finally:
        if database_name:
            import psycopg
            from psycopg import sql
            with psycopg.connect(root, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database_name)))
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(app.config)


def create_flow(f, *, questions=None, event=None):
    event = event or f.event
    values = f.service.defaults_for_event(event)
    if questions:
        values['questions'] = '\n'.join(questions)
    with f.organization_context(event.organization_id):
        row = f.service.save_event_feedback(event, values)
        f.db.session.commit()
    return row


def start_session(f, flow, phone='+17205550145'):
    with f.organization_context(f.org.id):
        thread = f.m.InboxThread(organization_id=f.org.id, phone=phone, contact_name='Original inbox name')
        f.db.session.add(thread)
        f.db.session.flush()
        session, _ = f.inbox._start_survey(flow, thread, phone)
        f.db.session.commit()
    return session


def input_message(f, body, sid=None, phone='+17205550145'):
    with patch('app.services.automation_outbox_service.enqueue_for_inbound', return_value=[]):
        result = f.inbox.process_inbound_sms({'From':phone,'To':f.profile.from_number,
            'MessagingServiceSid':f.profile.messaging_service_sid,'AccountSid':f.profile.twilio_subaccount_sid,
            'Body':body,'MessageSid':sid or 'SM'+uuid4().hex})
    f.db.session.expire_all()
    return result


def drain(f):
    for row in f.m.AutomationReplyOutbox.query.filter(f.m.AutomationReplyOutbox.status.in_(f.outbox.ACTIVE)).order_by(f.m.AutomationReplyOutbox.id).all():
        with patch('app.services.automation_outbox_service.get_twilio_service', return_value=f.provider):
            f.outbox.dispatch_automation_reply(f.org.id, row.id)
    f.db.session.expire_all()


def advance_session(f, session, body):
    with f.organization_context(session.organization_id):
        replies = f.inbox._advance_survey(session, body)
        f.db.session.flush()
        return replies


def linked_registration_waiting(f, *, questions=None):
    with f.organization_context(f.org.id):
        member = f.m.CommunityMember(organization=f.org, phone='+17205550145', name='Original community name')
        registration = f.m.EventRegistration(organization=f.org, event=f.event, phone=member.phone,
            name='Original registered attendee', booking_spaces=3, external_source='aoc',
            external_booking_id='Existing booking', selections_json='[{"label":"Family","quantity":3}]')
        flow = f.m.SurveyFlow(organization=f.org, name='Original linked registration', trigger_keyword='RSVPOLD',
            linked_event=f.event, completion_message='Registration recorded.')
        flow.set_questions(questions or ['What is your name?'])
        f.db.session.add_all([member, registration, flow]); f.db.session.commit()
    assert input_message(f, flow.trigger_keyword)['status'] == 'survey_started'
    session = f.m.SurveySession.query.filter_by(survey_id=flow.id).one()
    before = {column.name:getattr(registration, column.name) for column in registration.__table__.columns}
    return SimpleNamespace(flow=flow, session=session, registration=registration, member=member, before=before)


@pytest.mark.parametrize('pending_old_reply', [False, True])
def test_exact_feedback_command_hands_off_old_registration_without_answer_or_name_change(feedback, pending_old_reply):
    f = feedback; old = linked_registration_waiting(f); flow = create_flow(f)
    if not pending_old_reply:
        drain(f)
    calls_before = f.provider.client.messages.create.call_count
    result = input_message(f, ' \n' + flow.trigger_keyword.lower() + '\t ', 'SM-feedback-handoff')
    assert result['status'] == 'survey_started'
    assert old.session.status == 'cancelled' and old.session.current_question_index == 0
    assert f.m.SurveyResponse.query.count() == 0
    assert {column.name:getattr(old.registration,column.name) for column in old.registration.__table__.columns} == old.before
    assert old.member.name == 'Original community name'
    session = f.m.SurveySession.query.filter_by(survey_id=flow.id).one()
    assert session.status == 'active' and session.purpose == 'event_feedback'
    inbound = f.m.InboxMessage.query.filter_by(message_sid='SM-feedback-handoff').one()
    assert inbound.automation_source_id == flow.id and inbound.matched_keyword == flow.trigger_keyword
    row_count = f.m.AutomationReplyOutbox.query.count()
    assert input_message(f, flow.trigger_keyword, 'SM-feedback-handoff')['status'] == 'duplicate'
    assert f.m.AutomationReplyOutbox.query.count() == row_count and flow.start_count == 1
    drain(f)
    old_reply = f.m.AutomationReplyOutbox.query.filter_by(survey_session_id=old.session.id).one()
    assert old_reply.status == ('cancelled' if pending_old_reply else 'accepted')
    assert f.provider.client.messages.create.call_count == calls_before + 2
    assert old.session.status == 'cancelled' and session.status == 'active'


def test_feedback_handoff_retains_prior_registration_answers_and_pending_question_history(feedback):
    f = feedback; old = linked_registration_waiting(f, questions=['What is your name?', 'How many people?'])
    drain(f); input_message(f, 'Actual person', 'SM-original-name')
    prior_answers = [(row.question_prompt,row.answer) for row in old.session.responses]
    flow = create_flow(f)
    assert input_message(f, flow.trigger_keyword)['status'] == 'survey_started'
    drain(f)
    assert old.session.status == 'cancelled'
    assert [(row.question_prompt,row.answer) for row in old.session.responses] == prior_answers == [('What is your name?','Actual person')]
    assert {column.name:getattr(old.registration,column.name) for column in old.registration.__table__.columns} == old.before
    assert f.m.AutomationReplyOutbox.query.filter_by(body='How many people?').one().status == 'cancelled'
    assert all(call.kwargs['body'] != 'How many people?' for call in f.provider.client.messages.create.call_args_list)


@pytest.mark.parametrize('known_result', ['accepted', 'ambiguous'])
def test_feedback_handoff_preserves_interrupted_old_provider_evidence_without_resubmission(feedback, known_result):
    f = feedback; old = linked_registration_waiting(f); flow = create_flow(f)
    row = f.m.AutomationReplyOutbox.query.filter_by(survey_session_id=old.session.id).one()
    fake_accept = f.provider.client.messages.create.side_effect
    if known_result == 'ambiguous':
        f.provider.client.messages.create.side_effect = TimeoutError('Synthetic lost provider acknowledgement')
    try:
        with patch('app.services.automation_outbox_service.get_twilio_service', return_value=f.provider), \
             patch('app.services.automation_outbox_service._finish', side_effect=RuntimeError('Interrupted before outbox finalization')):
            with pytest.raises(RuntimeError, match='Interrupted'):
                f.outbox.dispatch_automation_reply(f.org.id, row.id)
    finally:
        f.provider.client.messages.create.side_effect = fake_accept
        f.db.session.rollback(); f.db.session.expire_all()
    attempt = f.m.MessageDispatchAttempt.query.filter_by(logical_send_key=row.logical_send_key).one()
    assert attempt.status == ('sent' if known_result == 'accepted' else 'ambiguous')
    original_sid = attempt.provider_message_sid
    row.worker_lease_until = f.outbox._now() - timedelta(seconds=1)
    f.db.session.commit()
    assert old.session.status == 'active'
    assert input_message(f, flow.trigger_keyword)['status'] == 'survey_started'
    calls_before = f.provider.client.messages.create.call_count
    with patch('app.services.automation_outbox_service.get_twilio_service', side_effect=AssertionError('Never resubmit an uncertain or accepted old question')):
        result = f.outbox.dispatch_automation_reply(f.org.id, row.id)
        replay = f.outbox.dispatch_automation_reply(f.org.id, row.id)
    assert result['status'] == replay['status'] == known_result
    assert result.get('sid') == original_sid and attempt.provider_message_sid == original_sid
    assert f.provider.client.messages.create.call_count == calls_before
    drain(f)
    assert f.provider.client.messages.create.call_count == calls_before + 2
    assert old.session.status == 'cancelled'
    assert f.m.SurveySession.query.filter_by(survey_id=flow.id).one().status == 'active'
    assert {column.name:getattr(old.registration,column.name) for column in old.registration.__table__.columns} == old.before


@pytest.mark.parametrize('question_index', [0, 1])
@pytest.mark.parametrize('pending_reply', [False, True])
def test_repeated_feedback_keyword_replays_only_frozen_current_prompt_without_restarting(feedback, question_index, pending_reply):
    f = feedback; flow = create_flow(f, questions=['Frozen rating?', 'Frozen comment?'])
    input_message(f, flow.trigger_keyword)
    session = f.m.SurveySession.query.one()
    if question_index:
        drain(f); input_message(f, '5')
    if not pending_reply:
        drain(f)
    answers_before = [(row.question_prompt,row.answer) for row in session.responses]
    flow.set_questions(['Edited question?']); f.db.session.commit()
    row_count = f.m.AutomationReplyOutbox.query.count()
    result = input_message(f, flow.trigger_keyword, 'SM-repeat-current-feedback')
    assert result['status'] == ('survey_reply_pending' if pending_reply else 'survey_prompt_repeated')
    assert f.m.SurveySession.query.count() == 1 and flow.start_count == 1
    assert session.current_question_index == question_index and session.status == 'active'
    assert [(row.question_prompt,row.answer) for row in session.responses] == answers_before
    assert f.m.AutomationReplyOutbox.query.count() == row_count + (0 if pending_reply else 1)
    if not pending_reply:
        assert f.m.AutomationReplyOutbox.query.order_by(f.m.AutomationReplyOutbox.id.desc()).first().body == ['Frozen rating?','Frozen comment?'][question_index]
    assert input_message(f, flow.trigger_keyword, 'SM-repeat-current-feedback')['status'] == 'duplicate'
    drain(f)
    assert f.m.EventRegistration.query.count() == 0


@pytest.mark.parametrize('pending_old_reply', [False, True])
def test_paused_feedback_command_preserves_old_registration_conversation(feedback, pending_old_reply):
    f = feedback; old = linked_registration_waiting(f); flow = create_flow(f)
    flow.is_active = False; f.db.session.commit()
    if not pending_old_reply:
        drain(f)
    assert input_message(f, flow.trigger_keyword)['status'] == 'feedback_paused'
    assert old.session.status == 'active' and old.session.current_question_index == 0
    assert f.m.SurveyResponse.query.count() == 0 and flow.start_count == 0
    assert f.m.SurveySession.query.count() == 1
    assert {column.name:getattr(old.registration,column.name) for column in old.registration.__table__.columns} == old.before
    notice = f.m.AutomationReplyOutbox.query.order_by(f.m.AutomationReplyOutbox.id.desc()).first()
    assert notice.source == 'system' and notice.survey_session_id is None and 'paused' in notice.body
    drain(f)
    assert old.session.status == 'active'


def test_paused_feedback_does_not_steal_keyword_from_active_rule(feedback):
    f = feedback; paused = create_flow(f)
    paused.is_active = False; f.db.session.commit()
    with f.organization_context(f.org.id):
        owner = f.m.KeywordAutomationRule(organization=f.org, keyword=paused.trigger_keyword,
            response_body='Active rule reply.', is_active=True)
        f.db.session.add(owner); f.db.session.commit()
    result = input_message(f, paused.trigger_keyword)
    assert result['status'] == 'keyword_reply'
    assert f.m.SurveySession.query.filter_by(survey_id=paused.id).count() == 0 and paused.start_count == 0
    reply = f.m.AutomationReplyOutbox.query.one()
    assert reply.body == 'Active rule reply.'
    assert reply.source == 'keyword' and reply.source_id == owner.id
    assert f.m.SurveyResponse.query.count() == 0 and f.m.EventRegistration.query.count() == 0


def test_current_schema_and_editor_preserve_paused_feedback_keyword_uniqueness(feedback):
    f = feedback; paused = create_flow(f)
    paused.is_active = False; f.db.session.commit()
    values = f.service.defaults_for_event(f.event); values['name'] = 'Replacement feedback'
    with f.organization_context(f.org.id):
        with pytest.raises(ValueError, match='trigger is already used'):
            f.service.save_event_feedback(f.event, values)
        duplicate = f.m.SurveyFlow(organization=f.org, name='Direct replacement', purpose='event_feedback',
            feedback_event=f.event, trigger_keyword=paused.trigger_keyword)
        f.db.session.add(duplicate)
        with pytest.raises(IntegrityError):
            f.db.session.commit()
        f.db.session.rollback()
        assert f.m.SurveyFlow.query.count() == 1 and not paused.is_active


@pytest.mark.parametrize('paused_target', [False, True])
def test_different_feedback_command_preserves_existing_feedback_answers(feedback, paused_target):
    f = feedback; original = create_flow(f); input_message(f, original.trigger_keyword); drain(f)
    input_message(f, '5'); old_session = f.m.SurveySession.query.one()
    values = f.service.defaults_for_event(f.event)
    values.update(name='Different event feedback', trigger_keyword='OTHERFEEDBACK')
    with f.organization_context(f.org.id):
        target = f.service.save_event_feedback(f.event, values)
        target.is_active = not paused_target; f.db.session.commit()
    result = input_message(f, target.trigger_keyword)
    assert result['status'] == ('feedback_paused' if paused_target else 'survey_started')
    assert old_session.status == ('active' if paused_target else 'cancelled')
    assert [row.answer for row in old_session.responses] == ['5']
    assert f.m.SurveyResponse.query.count() == 1
    assert f.m.SurveySession.query.count() == (1 if paused_target else 2)
    assert f.m.EventRegistration.query.count() == 0


def test_feedback_keyword_in_other_tenant_does_not_take_over_local_conversation(feedback):
    f = feedback; old = linked_registration_waiting(f, questions=['Your comment?', 'Anything else?'])
    drain(f); foreign = create_flow(f, event=f.other_event)
    result = input_message(f, foreign.trigger_keyword)
    assert result['status'] == 'survey_response'
    assert old.session.status == 'active' and old.session.current_question_index == 1
    assert [row.answer for row in old.session.responses] == [foreign.trigger_keyword]
    assert f.m.SurveySession.query.filter_by(survey_id=foreign.id).count() == 0
    assert foreign.start_count == 0


@pytest.mark.parametrize('change', ['paused', 'legacy_purpose'])
def test_repeat_keyword_retains_frozen_active_feedback_after_form_state_changes(feedback, change):
    f = feedback; flow = create_flow(f, questions=['Original rating?', 'Original comment?'])
    input_message(f, flow.trigger_keyword); drain(f); input_message(f, '5'); drain(f)
    session = f.m.SurveySession.query.one()
    if change == 'paused':
        flow.is_active = False
    else:
        flow.purpose = 'survey'; flow.linked_event = f.event
    flow.set_questions(['Replacement name?']); f.db.session.commit()
    assert input_message(f, flow.trigger_keyword)['status'] == 'survey_prompt_repeated'
    assert f.m.SurveySession.query.count() == 1 and session.current_question_index == 1
    assert [row.answer for row in session.responses] == ['5']
    assert f.m.AutomationReplyOutbox.query.order_by(f.m.AutomationReplyOutbox.id.desc()).first().body == 'Original comment?'
    assert f.m.EventRegistration.query.count() == 0


@pytest.mark.parametrize('body', ['FEEDBACK1 please', 'I enjoyed FEEDBACK1', 'FEEDBACK1!'])
def test_feedback_command_requires_the_whole_normalized_keyword(feedback, body):
    f = feedback; flow = create_flow(f)
    assert flow.trigger_keyword == 'FEEDBACK1'
    assert input_message(f, body)['status'] == 'stored'
    assert f.m.SurveySession.query.count() == 0 and f.m.AutomationReplyOutbox.query.count() == 0


def test_feedback_command_does_not_bypass_stop_or_outbound_readiness(feedback):
    f = feedback; old = linked_registration_waiting(f); flow = create_flow(f)
    row_count = f.m.AutomationReplyOutbox.query.count()
    with patch('app.services.billing_service.organization_can_transmit_messages', return_value=False):
        result = input_message(f, flow.trigger_keyword)
    assert result['status'] == 'stored' and result['automation_blocked']
    assert old.session.status == 'active' and old.session.current_question_index == 0
    assert f.m.AutomationReplyOutbox.query.count() == row_count and flow.start_count == 0
    assert input_message(f, 'STOP')['status'] == 'opt_out'
    assert old.session.status == 'cancelled'
    # A later command cannot silently remove an explicit opt-out or submit a reply.
    input_message(f, flow.trigger_keyword); drain(f)
    assert f.m.UnsubscribedContact.query.count() == 1
    assert f.provider.client.messages.create.call_count == 0
    assert all(row.status == 'cancelled' for row in f.m.SurveySession.query.all())
    assert {column.name:getattr(old.registration,column.name) for column in old.registration.__table__.columns} == old.before


def test_feedback_rating_comment_does_not_rename_or_change_attendance(feedback):
    f = feedback
    member = f.m.CommunityMember(organization=f.org, phone='+17205550145', name='Community name')
    registration = f.m.EventRegistration(organization=f.org, event=f.event, phone=member.phone,
        name='Registered attendee', booking_spaces=4, external_source='aoc', external_booking_id='Original booking',
        selections_json='[{"label":"Family","quantity":4}]')
    f.db.session.add_all([member, registration]); f.db.session.commit()
    before = {column.name:getattr(registration, column.name) for column in registration.__table__.columns}
    flow = create_flow(f)
    session = start_session(f, flow)
    advance_session(f, session, '5')
    advance_session(f, session, 'Excellent celebration!')
    f.db.session.commit(); f.db.session.expire_all()
    assert session.status == 'completed'
    assert [response.answer for response in session.responses] == ['5','Excellent celebration!']
    assert {column.name:getattr(registration,column.name) for column in registration.__table__.columns} == before
    assert f.db.session.get(f.m.CommunityMember,member.id).name == 'Community name'
    assert session.thread.contact_name == 'Original inbox name'
    assert f.m.EventRegistration.query.count() == 1


def test_feedback_never_registers_a_new_respondent_even_with_a_linked_event(feedback):
    f = feedback; flow = create_flow(f, questions=['What worked?'])
    flow.linked_event_id = f.event.id  # Defense against legacy/manual misconfiguration.
    f.db.session.commit(); session = start_session(f, flow)
    advance_session(f, session, 'The food!'); f.db.session.commit()
    assert f.m.EventRegistration.query.count() == 0
    assert f.m.CommunityMember.query.count() == 0


def test_feedback_context_and_questions_remain_frozen_after_flow_edits(feedback):
    f = feedback; flow = create_flow(f, questions=['Original rating?', 'Original comment?'])
    session = start_session(f, flow)
    flow.purpose = 'survey'; flow.linked_event_id = f.event.id
    flow.set_questions(['Give your name']); flow.completion_message = 'You registered'
    f.event.title = 'Renamed event'; f.db.session.commit()
    assert advance_session(f, session, '5') == ['Original comment?']
    assert 'Original comment?' in f.inbox._survey_confirmation_prompt(session, '5')
    assert advance_session(f, session, 'Keep the music') == ['Thank you for your feedback!']
    f.db.session.commit()
    assert session.feedback_event_title == 'Past community celebration'
    assert session.feedback_event_id == f.event.id
    assert session.purpose == 'event_feedback'
    assert f.m.EventRegistration.query.count() == 0
    assert [response.question_prompt for response in session.responses] == ['Original rating?','Original comment?']


def test_original_linked_registration_survey_keeps_existing_behavior(feedback):
    f = feedback
    flow = f.m.SurveyFlow(organization=f.org, name='RSVP', trigger_keyword='RSVP', linked_event=f.event)
    flow.set_questions(['Your name?']); f.db.session.add(flow); f.db.session.commit()
    session = start_session(f, flow); advance_session(f, session, 'Actual attendee'); f.db.session.commit()
    row = f.m.EventRegistration.query.one()
    assert row.name == 'Actual attendee' and row.event_id == f.event.id
    assert session.purpose == 'survey' and session.feedback_event_id is None


@pytest.mark.parametrize('damaged', ['{', '{}', '[42]', '[]', None])
def test_missing_feedback_prompt_snapshot_cancels_without_inventing_completion(feedback, damaged):
    f = feedback; flow = create_flow(f); session = start_session(f, flow)
    session.feedback_questions_json = damaged
    replies = advance_session(f, session, '5'); f.db.session.commit()
    assert session.status == 'cancelled'
    assert f.m.SurveyResponse.query.count() == 0
    assert f.m.EventRegistration.query.count() == 0
    assert flow.completion_count == 0 and 'no longer available' in replies[0]


def test_feedback_inbound_replay_does_not_duplicate_answers_or_delivery(feedback):
    f = feedback; flow = create_flow(f)
    assert input_message(f, flow.trigger_keyword, 'SM-feedback-start')['status'] == 'survey_started'
    drain(f)
    assert input_message(f, '5', 'SM-feedback-rating')['status'] == 'survey_response'
    drain(f)
    call_count = f.provider.client.messages.create.call_count
    assert input_message(f, '5', 'SM-feedback-rating')['status'] == 'duplicate'
    drain(f)
    assert f.provider.client.messages.create.call_count == call_count
    assert f.m.SurveyResponse.query.count() == 1
    assert input_message(f, 'More dancing', 'SM-feedback-comment')['status'] == 'survey_response'
    drain(f)
    assert f.m.SurveyResponse.query.count() == 2
    assert f.m.SurveySession.query.one().status == 'completed'
    assert f.m.EventRegistration.query.count() == 0


def test_stop_cancels_queued_feedback_without_provider_submission(feedback):
    f = feedback; flow = create_flow(f)
    input_message(f, flow.trigger_keyword); input_message(f, 'STOP')
    drain(f)
    assert f.provider.client.messages.create.call_count == 0
    assert f.m.SurveySession.query.one().status == 'cancelled'
    assert f.m.AutomationReplyOutbox.query.filter_by(status='cancelled').count() == 2
    assert f.m.UnsubscribedContact.query.count() == 1
    assert f.m.EventRegistration.query.count() == 0


@pytest.mark.parametrize('keyword',['STOP','quit','help','YES','START','STOP anything'])
def test_feedback_cannot_take_over_messaging_control_words(feedback, keyword):
    f = feedback; values = f.service.defaults_for_event(f.event); values['trigger_keyword'] = keyword
    with f.organization_context(f.org.id), pytest.raises(ValueError, match='control word'):
        f.service.save_event_feedback(f.event, values)
    assert f.m.SurveyFlow.query.count() == 0


@pytest.mark.parametrize('kind', ['survey','keyword'])
def test_feedback_creation_reuses_cross_automation_keyword_conflict_rules(feedback, kind):
    f = feedback
    if kind == 'survey':row = f.m.SurveyFlow(organization=f.org,name='Existing',trigger_keyword='FEEDBACK'+str(f.event.id))
    else:row = f.m.KeywordAutomationRule(organization=f.org,keyword='FEEDBACK'+str(f.event.id),response_body='Existing')
    f.db.session.add(row); f.db.session.commit()
    with f.organization_context(f.org.id), pytest.raises(ValueError, match='trigger'):
        f.service.save_event_feedback(f.event,f.service.defaults_for_event(f.event))
    assert f.m.SurveyFlow.query.filter_by(purpose='event_feedback').count() == 0


def test_feedback_validation_does_not_partially_mutate_saved_flow(feedback):
    f = feedback; flow = create_flow(f); previous = (flow.name,flow.questions_json,flow.trigger_keyword)
    values = f.service.form_values(flow); values.update(name='New name',questions='x'*321)
    with f.organization_context(f.org.id), pytest.raises(ValueError, match='320'):
        f.service.save_event_feedback(f.event,values,flow=flow)
    assert (flow.name,flow.questions_json,flow.trigger_keyword) == previous


def test_feedback_invitation_only_suggests_existing_event_audience(feedback):
    f = feedback; flow = create_flow(f)
    message = f.service.feedback_invitation_text(flow,f.event)
    assert flow.trigger_keyword in message and f.event.title in message
    with pytest.raises(ValueError):f.service.feedback_invitation_text(flow,f.other_event)
    assert f.provider.client.messages.create.call_count == 0
    assert f.m.ScheduledMessage.query.count() == 0


def test_feedback_routes_create_edit_review_and_pause_without_sending(feedback):
    f = feedback
    assert 'event_feedback' in f.app.blueprints
    assert f.client.get(f'/events/{f.event.id}/feedback').status_code == 200
    values = f.service.defaults_for_event(f.event); values['is_active']='on'
    result = f.client.post(f'/events/{f.event.id}/feedback/new',data=values)
    assert result.status_code == 302
    flow = f.m.SurveyFlow.query.filter_by(purpose='event_feedback').one()
    detail = f.client.get(result.location)
    assert detail.status_code == 200 and b'Review invitation' in detail.data
    assert f'feedback_id={flow.id}'.encode() in detail.data
    assert f.provider.client.messages.create.call_count == 0
    assert f.m.EventRegistration.query.count() == 0
    edit = f.client.post(f'/events/{f.event.id}/feedback/{flow.id}/edit',data=dict(values,questions='What should we improve?'))
    assert edit.status_code == 302
    archive = f.client.post(f'/events/{f.event.id}/feedback/{flow.id}/archive')
    assert archive.status_code == 302 and not f.db.session.get(f.m.SurveyFlow,flow.id).is_active
    assert f.m.SurveyFlow.query.count() == 1


def test_feedback_routes_are_tenant_bound(feedback):
    f = feedback; foreign = create_flow(f,event=f.other_event)
    for client in [f.client,f.staff_client]:
        assert client.get(f'/events/{f.other_event.id}/feedback').status_code == 404
        assert client.post(f'/events/{f.other_event.id}/feedback/new',data={}).status_code == 404
        assert client.get(f'/events/{f.event.id}/feedback/{foreign.id}').status_code == 404


def test_staff_feedback_pages_omit_phone_fields_and_redact_response_text(feedback):
    f = feedback; phone='+17205550145'; flow=create_flow(f,questions=['Your comments?'])
    session=start_session(f,flow,phone=phone);session.thread.contact_name=phone
    advance_session(f, session,'Please call 720-555-0145 or '+phone);f.db.session.commit()
    result=f.staff_client.get(f'/events/{f.event.id}/feedback/{flow.id}')
    assert result.status_code==200
    body=result.get_data(as_text=True)
    assert phone not in body and '720-555-0145' not in body
    assert f'Respondent #{session.id}' in body
    assert f'thread={session.thread_id}' in body
    assert 'export' not in body.lower() or 'export' not in body.lower().split('responses')[-1]
    owner=f.client.get(f'/events/{f.event.id}/feedback/{flow.id}')
    assert '720-555-0145' in owner.get_data(as_text=True)


def test_feedback_response_pagination_keeps_each_session_prompt_and_context(feedback):
    f=feedback;flow=create_flow(f,questions=['Original question?'])
    for number in range(28):
        session=start_session(f,flow,phone=f'+1720444{number:04d}')
        advance_session(f, session,'Answer '+str(number))
    f.db.session.commit();flow.set_questions(['New question?']);f.event.title='Renamed event';f.db.session.commit()
    with f.app.test_request_context('/feedback'):
        from flask_login import login_user
        login_user(f.owner)
        with f.organization_context(f.org.id):summary=f.service.feedback_summary(f.event,flow)
    assert summary['completed']==28 and len(summary['rows'])==25 and summary['pages']==2
    assert all(row['answers'][0]['prompt']=='Original question?' for row in summary['rows'])
    assert all(row['event_title']=='Past community celebration' for row in summary['rows'])
    assert all('phone' not in row for row in summary['rows'])


def test_staff_cannot_read_or_overwrite_phone_bearing_feedback_editor(feedback):
    f=feedback;flow=create_flow(f,questions=['Call +17205550145 with any questions?'])
    before=flow.questions_json
    url=f'/events/{f.event.id}/feedback/{flow.id}/edit'
    assert f.staff_client.get(url).status_code==403
    assert f.staff_client.post(url,data={'name':'Replacement','questions':'Changed'}).status_code==403
    f.db.session.expire_all()
    assert f.db.session.get(f.m.SurveyFlow,flow.id).questions_json==before
    assert f.client.get(url).status_code==200


def test_feedback_context_migration_is_additive_and_idempotent(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'old-feedback.db'))
    module=importlib.import_module('app.saas_migrations.028_event_feedback_context')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE events(id INTEGER PRIMARY KEY,title TEXT)'))
        connection.execute(text('CREATE TABLE survey_flows(id INTEGER PRIMARY KEY,name TEXT,linked_event_id INTEGER,questions_json TEXT)'))
        connection.execute(text('CREATE TABLE survey_sessions(id INTEGER PRIMARY KEY,survey_id INTEGER,phone TEXT,status TEXT)'))
        connection.execute(text("INSERT INTO events VALUES(7,'Original event')"))
        connection.execute(text("INSERT INTO survey_flows VALUES(3,'Original survey',7,'[\"Your name?\"]')"))
        connection.execute(text("INSERT INTO survey_sessions VALUES(9,3,'+17205550145','active')"))
        before=connection.execute(text('SELECT id,name,linked_event_id,questions_json FROM survey_flows')).all()
        module.apply(connection,logging.getLogger(__name__));module.apply(connection,logging.getLogger(__name__))
        assert connection.execute(text('SELECT id,name,linked_event_id,questions_json FROM survey_flows')).all()==before
        assert connection.execute(text('SELECT purpose,feedback_event_id FROM survey_flows')).one()==('survey',None)
        assert connection.execute(text('SELECT purpose,feedback_event_id,feedback_questions_json FROM survey_sessions')).one()==('survey',None,None)
        columns={column['name'] for column in inspect(connection).get_columns('survey_sessions')}
        assert {'feedback_event_title','feedback_completion_message','feedback_questions_json'}<=columns
    engine.dispose()


def test_legacy_feedback_compatibility_migration_does_not_reinterpret_existing_sessions(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy-feedback.db'))
    module=importlib.import_module('app.migrations.027_event_feedback_context')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE events(id INTEGER PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE survey_flows(id INTEGER PRIMARY KEY,linked_event_id INTEGER)'))
        connection.execute(text('CREATE TABLE survey_sessions(id INTEGER PRIMARY KEY,phone TEXT)'))
        connection.execute(text("INSERT INTO survey_sessions VALUES(4,'+17205550145')"))
        module.apply(connection,logging.getLogger(__name__));module.apply(connection,logging.getLogger(__name__))
        assert connection.execute(text('SELECT id,phone,purpose,feedback_event_id FROM survey_sessions')).one()==(4,'+17205550145','survey',None)
    engine.dispose()


def test_actual_postgres_upgrade_preserves_pre_feedback_rows_and_repeated_application(feedback):
    f = feedback
    if not f.root:
        pytest.skip('This upgrade branch requires the explicitly configured owned PostgreSQL fixture.')
    original = f.m.SurveyFlow(organization=f.org,name='Existing event signup',trigger_keyword='LEGACYRSVP',linked_event=f.event)
    original.set_questions(['Your name?']);f.db.session.add(original);f.db.session.commit()
    session=start_session(f,original)
    flow_id,session_id=original.id,session.id
    engine=f.db.engine;f.db.session.remove()
    migration=importlib.import_module('app.saas_migrations.028_event_feedback_context')
    with engine.begin() as connection:
        # Remove only this new concern from this unique disposable test DB,
        # reconstructing the actual pre028 PostgreSQL tables and data.
        for table, additions in migration.COLUMNS.items():
            for name, _definition in additions:
                connection.execute(text(f'ALTER TABLE {table} DROP COLUMN {name}'))
        flows_before=connection.execute(text('SELECT to_jsonb(t) FROM survey_flows t ORDER BY id')).scalars().all()
        sessions_before=connection.execute(text('SELECT to_jsonb(t) FROM survey_sessions t ORDER BY id')).scalars().all()
        migration.apply(connection,logging.getLogger(__name__))
        migration.apply(connection,logging.getLogger(__name__))
        def old_values(table):
            additions={name for name,_ in migration.COLUMNS[table]}
            return [{key:value for key,value in row.items() if key not in additions}
                    for row in connection.execute(text(f'SELECT to_jsonb(t) FROM {table} t ORDER BY id')).scalars()]
        assert old_values('survey_flows')==flows_before
        assert old_values('survey_sessions')==sessions_before
        assert connection.execute(text('SELECT purpose,feedback_event_id FROM survey_flows WHERE id=:id'),{'id':flow_id}).one()==('survey',None)
        assert connection.execute(text('SELECT purpose,feedback_event_id FROM survey_sessions WHERE id=:id'),{'id':session_id}).one()==('survey',None)
