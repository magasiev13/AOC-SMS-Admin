"""New-only automatic reply durability, ordering and provider uncertainty."""
import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import MagicMock, patch
from uuid import uuid4


class TestAutomationReplyOutbox(unittest.TestCase):
    def setUp(self):
        self.saved_env = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory(prefix='twinevia-automation-outbox-')
        self.postgres_root = os.environ.get('AUTOMATION_TEST_POSTGRES_ROOT')
        self.postgres_db_name = None
        if self.postgres_root:
            parsed = urlsplit(self.postgres_root)
            if parsed.hostname != '127.0.0.1' or parsed.port != 55432 or parsed.username != 'audit' or parsed.path != '/postgres':
                raise RuntimeError('Automation tests accept only the isolated PostgreSQL audit root.')
            import psycopg
            from psycopg import sql
            self.postgres_db_name = 'automation_outbox_' + uuid4().hex
            with psycopg.connect(self.postgres_root,autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(self.postgres_db_name)))
            database_url = urlunsplit(('postgresql+psycopg',parsed.netloc,'/'+self.postgres_db_name,'',''))
        else:
            database_url = 'sqlite:///'+self.tmp.name+'/automation.db'
        os.environ.update(DATABASE_URL=database_url,SAAS_MODE='1',FLASK_DEBUG='1',SCHEDULER_ENABLED='0',
                          PYTHON_DOTENV_DISABLED='1',AUTH_ALERTS_ENABLED='0',SECRET_KEY='isolated-automation-test')
        import app.config
        importlib.reload(app.config)
        from app import create_app,db,models
        from app.services import automation_outbox_service,inbox_service,twilio_service
        self.db,self.models,self.outbox,self.inbox,self.twilio=db,models,automation_outbox_service,inbox_service,twilio_service
        self.app=create_app(run_startup_tasks=False,start_scheduler=False)
        self.app.config.update(TESTING=True,WTF_CSRF_ENABLED=False,TWILIO_AUTH_TOKEN='fake-automation-token',
                               TWILIO_ALLOW_LIVE_SENDS_IN_TESTING=True,TWILIO_BROWSER_FAKE_SENDS=False,
                               SURVEY_START_QUESTION_DELAY_SECONDS=0)
        self.context=self.app.app_context()
        self.context.push()
        self.assertEqual(self.app.config['SQLALCHEMY_DATABASE_URI'],database_url)
        db.create_all()
        self.org=models.Organization(name='Automation fixture',slug='automation-fixture',status='active')
        self.profile=models.OrganizationMessagingProfile(organization=self.org,provider_mode='platform_managed',
            twilio_subaccount_sid='AC'+'a'*32,messaging_service_sid='MG'+'a'*32,from_number='+17205559000',
            inbound_identity='+17205559000',provider_status='active',status='active',sender_review_status='approved')
        subscription=models.OrganizationSubscription(organization=self.org,status='complimentary')
        db.session.add_all([self.org,self.profile,subscription])
        db.session.flush()
        self.org_id=self.org.id
        self.rule=models.KeywordAutomationRule(organization_id=self.org_id,keyword='HELLO',response_body='Hello back!',is_active=True)
        self.survey=models.SurveyFlow(organization_id=self.org_id,name='Feedback',trigger_keyword='SURVEY',intro_message='Survey intro',
            questions_json=json.dumps(['Question one?','Question two?']),completion_message='Survey complete',is_active=True)
        db.session.add_all([self.rule,self.survey])
        db.session.commit()
        self.phone='+17205550145'
        self.calls=[]
        self.provider=self.twilio.TwilioService.__new__(self.twilio.TwilioService)
        self.provider.organization_id,self.provider.account_sid=self.org_id,'AC'+'a'*32
        self.provider.from_number,self.provider.messaging_service_sid='+17205559000','MG'+'a'*32
        self.provider.client=MagicMock()
        def accept(**params):
            self.calls.append(dict(params,observed_monotonic=time.monotonic()))
            return SimpleNamespace(sid=f'SM{len(self.calls):032x}',status='queued',account_sid=self.provider.account_sid,num_segments='1',price=None,price_unit='usd')
        self.provider.client.messages.create.side_effect=accept
        self.queue=MagicMock()
        self.queue.fetch_job.return_value=None
        self.queue_patch=patch('app.services.automation_outbox_service.get_queue',return_value=self.queue)
        self.queue_patch.start()

    def tearDown(self):
        self.queue_patch.stop()
        self.db.session.remove()
        self.db.engine.dispose()
        self.context.pop()
        if self.postgres_db_name:
            import psycopg
            from psycopg import sql
            with psycopg.connect(self.postgres_root,autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(self.postgres_db_name)))
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self.saved_env)

    def _payload(self,body='HELLO',sid='SM-inbound-keyword'):
        return {'From':self.phone,'To':'+17205559000','MessagingServiceSid':'MG'+'a'*32,'AccountSid':'AC'+'a'*32,'Body':body,'MessageSid':sid}

    def _post(self,body='HELLO',sid='SM-inbound-keyword'):
        from twilio.request_validator import RequestValidator
        payload=self._payload(body,sid)
        signature=RequestValidator(self.app.config['TWILIO_AUTH_TOKEN']).compute_signature('http://localhost/webhooks/twilio/inbound',payload)
        with self.app.app_context(),self.app.test_client() as client:
            return client.post('/webhooks/twilio/inbound',data=payload,headers={'X-Twilio-Signature':signature})

    def _rows(self):
        self.db.session.expire_all()
        return self.models.AutomationReplyOutbox.query.order_by(self.models.AutomationReplyOutbox.id).all()

    def _dispatch(self,row):
        row_id=row if isinstance(row,int) else row.id
        with patch('app.services.automation_outbox_service.get_twilio_service',return_value=self.provider):
            result=self.outbox.dispatch_automation_reply(self.org_id,row_id)
        self.db.session.expire_all()
        return result

    def _drain(self):
        for row in self._rows():
            if row.status in self.outbox.ACTIVE:
                self._dispatch(row)

    def _enable_foreign_key_actions(self):
        if self.db.engine.dialect.name == 'sqlite':
            from sqlalchemy import event, text
            self.db.session.rollback()
            def enable_foreign_keys(connection, _record, _proxy):
                connection.execute('PRAGMA foreign_keys=ON')
            event.listen(self.db.engine, 'checkout', enable_foreign_keys)
            self.assertEqual(self.db.session.execute(text('PRAGMA foreign_keys')).scalar_one(), 1)

    def _delete_survey_flow(self):
        from app.tenant import organization_context
        survey_id = self.survey.id
        with organization_context(self.org_id):
            deleted = self.inbox.delete_survey_flow_with_dependencies(survey_id)
        self.assertEqual(deleted['surveys'], 1)
        self.assertEqual(deleted['sessions'], 1)
        self.db.session.expire_all()

    def test_deleted_survey_flow_cancels_queued_active_question_before_provider_construction(self):
        self._enable_foreign_key_actions()
        self.assertEqual(self._post('SURVEY').status_code, 200)
        intro, question = self._rows()
        question_id, session_id = question.id, question.survey_session_id
        self.assertIsNotNone(session_id)
        self.assertEqual(question.survey_session_state, 'active')
        self._dispatch(intro)
        self.assertEqual(len(self.calls), 1)
        self._delete_survey_flow()
        question = self.db.session.get(self.models.AutomationReplyOutbox, question_id)
        self.assertIsNone(question.survey_session_id)
        self.assertEqual(question.survey_session_state, 'active')
        self.assertIsNone(self.db.session.get(self.models.SurveySession, session_id))
        with patch('app.services.automation_outbox_service.get_twilio_service', return_value=self.provider) as factory:
            result = self.outbox.dispatch_automation_reply(self.org_id, question_id)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['not_submitted'])
        self.assertIsNone(result['sid'])
        factory.assert_not_called()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self._rows()[1].status, 'cancelled')
        self.assertEqual(self.models.MessageDispatchAttempt.query.count(), 1)

    def test_deleted_session_cancels_saved_active_reply_even_when_survey_still_exists(self):
        self._enable_foreign_key_actions()
        self._post('SURVEY')
        row_id = self._rows()[0].id
        session = self.models.SurveySession.query.one()
        self.db.session.delete(session)
        self.db.session.commit()
        self.db.session.expire_all()
        row = self.db.session.get(self.models.AutomationReplyOutbox, row_id)
        self.assertIsNone(row.survey_session_id)
        self.assertEqual(row.survey_session_state, 'active')
        self.assertTrue(self.models.SurveyFlow.query.one().is_active)
        with patch('app.services.automation_outbox_service.get_twilio_service', return_value=self.provider) as factory:
            result = self.outbox.dispatch_automation_reply(self.org_id, row_id)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['not_submitted'])
        factory.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_survey_deleted_in_attempt_claim_gap_is_rechecked_before_provider_http(self):
        self._enable_foreign_key_actions()
        self._post('SURVEY')
        intro, question = self._rows()
        question_id = question.id
        self._dispatch(intro)
        guard = self.outbox.lock_automation_submission
        def delete_before_guard(*args, **options):
            self._delete_survey_flow()
            return guard(*args, **options)
        with patch('app.services.automation_outbox_service.lock_automation_submission', side_effect=delete_before_guard) as final_guard:
            result = self._dispatch(question_id)
        final_guard.assert_called_once()
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['not_submitted'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self._rows()[1].status, 'cancelled')
        attempt = self.models.MessageDispatchAttempt.query.filter_by(logical_send_key=question.logical_send_key).one()
        self.assertEqual(attempt.status, 'failed')
        self.assertIsNone(attempt.provider_message_sid)

    def test_completed_survey_reply_and_unbound_keyword_survive_flow_deletion(self):
        self._enable_foreign_key_actions()
        self._post('SURVEY')
        self._drain()
        self._post('Answer one', 'SM-delete-completion-answer-one')
        self._drain()
        self._post('Answer two', 'SM-delete-completion-answer-two')
        completion = self._rows()[-1]
        completion_id = completion.id
        self.assertEqual((completion.body, completion.survey_session_state), ('Survey complete', 'completed'))
        self._delete_survey_flow()
        completion = self.db.session.get(self.models.AutomationReplyOutbox, completion_id)
        self.assertIsNone(completion.survey_session_id)
        self.assertEqual(self._dispatch(completion_id)['status'], 'accepted')
        self._post('HELLO', 'SM-keyword-after-survey-delete')
        keyword = self._rows()[-1]
        self.assertEqual(keyword.source, 'keyword')
        self.assertIsNone(keyword.survey_session_id)
        self.assertIsNone(keyword.survey_session_state)
        self.assertEqual(self._dispatch(keyword.id)['status'], 'accepted')
        self.assertEqual([call['body'] for call in self.calls],
                         ['Survey intro', 'Question one?', 'Question two?', 'Survey complete', 'Hello back!'])

    def test_deleted_survey_preserves_committed_acceptance_and_sid_without_provider_replay(self):
        self._enable_foreign_key_actions()
        self._post('SURVEY')
        intro, question = self._rows()
        question_id = question.id
        self._dispatch(intro)
        with patch('app.services.automation_outbox_service._finish', side_effect=RuntimeError('interrupted before survey bubble')):
            with self.assertRaises(RuntimeError):
                self._dispatch(question_id)
        self.db.session.rollback()
        attempt = self.models.MessageDispatchAttempt.query.filter_by(logical_send_key=question.logical_send_key).one()
        self.assertEqual(attempt.status, 'sent')
        accepted_sid = attempt.provider_message_sid
        self.assertTrue(accepted_sid)
        self._delete_survey_flow()
        with patch('app.services.automation_outbox_service.get_twilio_service', side_effect=AssertionError('Known survey acceptance must not reconstruct a provider')):
            result = self.outbox.dispatch_automation_reply(self.org_id, question_id)
            replay = self.outbox.dispatch_automation_reply(self.org_id, question_id)
        self.assertEqual((result['status'], result['sid']), ('accepted', accepted_sid))
        self.assertEqual(replay['sid'], accepted_sid)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(), 2)
        self.assertEqual(self.models.InboxMessage.query.filter_by(message_sid=accepted_sid).count(), 1)

    def test_deleted_survey_preserves_ambiguous_history_and_later_known_sid_without_resending(self):
        self._enable_foreign_key_actions()
        self._post('SURVEY')
        intro, question = self._rows()
        question_id = question.id
        self._dispatch(intro)
        def unknown(**parameters):
            self.calls.append(parameters)
            raise TimeoutError('Survey provider acknowledgement was lost')
        self.provider.client.messages.create.side_effect = unknown
        self._dispatch(question_id)
        saved = self.db.session.get(self.models.AutomationReplyOutbox, question_id)
        self.assertEqual(saved.status, 'ambiguous')
        bubble_id = saved.inbox_message_id
        self._delete_survey_flow()
        with patch('app.services.automation_outbox_service.get_twilio_service', side_effect=AssertionError('Ambiguous survey must not submit again')):
            result = self.outbox.dispatch_automation_reply(self.org_id, question_id)
            self.assertEqual(result['status'], 'ambiguous')
            attempt = self.models.MessageDispatchAttempt.query.filter_by(logical_send_key=saved.logical_send_key).one()
            attempt.status, attempt.provider_message_sid = 'sent', 'SM' + 'e' * 32
            self.db.session.commit()
            self.outbox.recover_automation_replies()
        saved = self.db.session.get(self.models.AutomationReplyOutbox, question_id)
        self.assertEqual((saved.status, saved.inbox_message_id), ('accepted', bubble_id))
        self.assertEqual(json.loads(saved.result_json)['sid'], 'SM' + 'e' * 32)
        self.assertEqual(self.models.InboxMessage.query.filter_by(message_sid='SM' + 'e' * 32).count(), 1)
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(), 2)
        self.assertEqual(len(self.calls), 2)

    def test_inbound_state_and_exact_reply_intent_commit_before_any_provider_call(self):
        self.assertEqual(self._post().status_code,200)
        rows=self._rows()
        self.assertEqual(len(rows),1)
        inbound=self.models.InboxMessage.query.filter_by(direction='inbound').one()
        self.assertEqual(rows[0].logical_send_key,f'automation:{inbound.id}:0:keyword:{self.rule.id}')
        self.assertEqual((rows[0].body,rows[0].phone,rows[0].status),('Hello back!',self.phone,'queued'))
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),0)
        self.assertEqual(self.calls,[])
        self._dispatch(rows[0])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),1)
        self.assertEqual(self._rows()[0].status,'accepted')

    def test_interruption_after_inbound_commit_is_recovered_by_duplicate_sid_without_state_replay(self):
        with patch('app.services.automation_outbox_service.enqueue_for_inbound',side_effect=RuntimeError('interrupted after inbound commit')):
            self.assertEqual(self._post('SURVEY').status_code,500)
        ids=[row.id for row in self._rows()]
        self.assertEqual(len(ids),2)
        self.assertEqual(self.models.SurveySession.query.count(),1)
        self.assertEqual(self._post('SURVEY').status_code,200)
        self.assertEqual([row.id for row in self._rows()],ids)
        self.assertEqual(self.models.SurveySession.query.count(),1)
        self._drain()
        self.assertEqual([call['body'] for call in self.calls],['Survey intro','Question one?'])
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),2)

    def test_survey_recovers_between_replies_in_order_and_answers_wait_for_question_submission(self):
        self._post('SURVEY')
        self.assertEqual(self._post('Early answer','SM-early-answer').status_code,200)
        self.assertEqual(self.models.SurveyResponse.query.count(),0)
        first,second=self._rows()
        self._dispatch(second)
        self.assertEqual(self.calls,[])
        self._dispatch(first)
        self._dispatch(second)
        self.assertEqual([call['body'] for call in self.calls],['Survey intro','Question one?'])
        self._post('Answer one','SM-answer-one')
        self.assertEqual(self.models.SurveyResponse.query.count(),1)
        self._post('Answer one','SM-answer-one')
        self.assertEqual(self.models.SurveyResponse.query.count(),1)
        self._drain()
        self.assertEqual([call['body'] for call in self.calls],['Survey intro','Question one?','Question two?'])

    def test_provider_commit_before_bubble_recovers_known_acceptance_even_when_provider_pauses(self):
        self._post()
        row=self._rows()[0]
        with patch('app.services.automation_outbox_service._finish',side_effect=RuntimeError('interrupted before history commit')):
            with self.assertRaises(RuntimeError):
                self._dispatch(row)
        self.db.session.rollback()
        self.assertEqual(self.models.MessageDispatchAttempt.query.one().status,'sent')
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),0)
        self.profile.set_provider_status('suspended')
        self.db.session.commit()
        with patch('app.services.automation_outbox_service.get_twilio_service',side_effect=AssertionError('Known acceptance must not construct a provider')):
            self.outbox.dispatch_automation_reply(self.org_id,row.id)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self._rows()[0].status,'accepted')
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),1)
        self._dispatch(row.id)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),1)

    def test_usage_failure_rolls_back_history_and_outbox_then_recovers_same_accepted_attempt(self):
        self._post()
        row=self._rows()[0]
        with patch('app.services.automation_outbox_service.record_usage_candidates',side_effect=RuntimeError('isolated usage write failure')):
            with self.assertRaises(RuntimeError):
                self._dispatch(row)
        self.db.session.rollback()
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),0)
        self.assertEqual(self.models.MessagingUsageRecord.query.count(),0)
        self.assertEqual(self.models.MessageDispatchAttempt.query.one().status,'sent')
        self.assertNotEqual(self._rows()[0].status,'accepted')
        with patch('app.services.automation_outbox_service.get_twilio_service',side_effect=AssertionError('Accepted recovery must not submit again')):
            self.outbox.dispatch_automation_reply(self.org_id,row.id)
        self.assertEqual(self._rows()[0].status,'accepted')
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),1)
        self.assertEqual(self.models.MessagingUsageRecord.query.count(),1)
        self.assertEqual(len(self.calls),1)

    def test_retained_inbound_sid_prevents_replay_after_inbox_message_deletion(self):
        self._post()
        self._drain()
        inbound=self.models.InboxMessage.query.filter_by(direction='inbound').one()
        self.db.session.delete(inbound)
        self.db.session.commit()
        self.assertEqual(self._post().status_code,200)
        self.assertEqual(len(self._rows()),1)
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='inbound').count(),0)
        self.assertEqual(len(self.calls),1)

    def test_rq_job_preserves_survey_intro_question_minimum_delay(self):
        self.app.config['SURVEY_START_QUESTION_DELAY_SECONDS']=0.06
        self._post('SURVEY')
        row=self._rows()[0]
        with patch('app.create_app',return_value=self.app),patch('app.services.automation_outbox_service.get_twilio_service',return_value=self.provider):
            self.outbox.deliver_automation_job(self.org_id,row.id)
        self.assertEqual([call['body'] for call in self.calls],['Survey intro','Question one?'])
        self.assertGreaterEqual(self.calls[1]['observed_monotonic']-self.calls[0]['observed_monotonic'],0.05)

    def test_stop_after_queue_cancels_unsent_response_without_provider_call(self):
        self._post()
        self._post('STOP','SM-stop-after-queue')
        self._drain()
        self.assertEqual(self.calls,[])
        row=self._rows()[0]
        self.assertEqual(row.status,'cancelled')
        self.assertIn('unsubscribed',row.last_error)
        self.assertEqual(self.models.UnsubscribedContact.query.filter_by(phone=self.phone).count(),1)

    def test_stop_in_attempt_claim_commit_gap_is_checked_before_actual_provider_http(self):
        self._post()
        row=self._rows()[0]
        guard=self.outbox.lock_automation_submission
        def stop_before_guard(*args,**kwargs):
            self.assertEqual(self._post('STOP','SM-stop-at-claim').status_code,200)
            return guard(*args,**kwargs)
        with patch('app.services.automation_outbox_service.lock_automation_submission',side_effect=stop_before_guard):
            self._dispatch(row)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.models.MessageDispatchAttempt.query.one().status,'failed')
        self.assertEqual(self._rows()[0].status,'cancelled')

    def test_pending_new_records_age_out_explicitly_and_cancel_only_the_bound_session(self):
        self._post('SURVEY')
        session=self.models.SurveySession.query.one()
        for row in self._rows():
            row.expires_at=self.outbox._now()-timedelta(seconds=1)
        self.db.session.commit()
        self._drain()
        self.assertEqual(self.calls,[])
        self.db.session.expire_all()
        self.assertEqual(session.status,'cancelled')
        self.assertEqual(self._rows()[0].status,'expired')
        self.assertTrue(all(row.status in {'expired','cancelled'} for row in self._rows()))

    def test_unknown_provider_acceptance_is_not_retried_and_local_known_upgrade_preserves_one_bubble(self):
        self._post()
        row=self._rows()[0]
        def unknown(**params):
            self.calls.append(params)
            raise TimeoutError('isolated response lost after write')
        self.provider.client.messages.create.side_effect=unknown
        self._dispatch(row)
        self.assertEqual(self._rows()[0].status,'ambiguous')
        self._dispatch(row)
        self.outbox.recover_automation_replies()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),1)
        attempt=self.models.MessageDispatchAttempt.query.one()
        attempt.status,attempt.provider_message_sid='sent','SM'+'f'*32
        self.db.session.commit()
        self.outbox.recover_automation_replies()
        self.assertEqual(self._rows()[0].status,'accepted')
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='outbound').count(),1)
        self.assertEqual(len(self.calls),1)

    def test_verified_429_retries_the_same_attempt_with_backoff_and_a_bound(self):
        from twilio.base.exceptions import TwilioRestException
        self._post()
        row=self._rows()[0]
        self.app.config['AUTOMATION_REPLY_MAX_RETRIES']=1
        def rejected(**params):
            self.calls.append(params)
            raise TwilioRestException(429,'/Messages',msg='Too many requests',code=20429)
        self.provider.client.messages.create.side_effect=rejected
        self._dispatch(row)
        self.assertEqual(self._rows()[0].status,'pending')
        self.assertEqual(self.models.MessageDispatchAttempt.query.one().status,'queued')
        self._dispatch(row)
        self.assertEqual(len(self.calls),1)
        row=self._rows()[0]
        row.ready_at,row.next_enqueue_at=self.outbox._now()-timedelta(seconds=1),self.outbox._now()-timedelta(seconds=1)
        self.db.session.commit()
        self._dispatch(row)
        self.assertEqual(self._rows()[0].status,'failed')
        self.assertEqual(self.models.MessageDispatchAttempt.query.count(),1)
        self.assertEqual(self.models.MessageDispatchAttempt.query.one().attempt_count,2)
        self.assertEqual(len(self.calls),2)

    def test_paused_inbound_does_not_create_new_automation_or_advance_survey(self):
        self.profile.set_provider_status('suspended')
        self.db.session.commit()
        self._post('SURVEY')
        self.assertEqual(self._rows(),[])
        self.assertEqual(self.models.SurveySession.query.count(),0)
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='inbound').count(),1)

    def test_additive_migration_never_backfills_historical_automatic_messages(self):
        thread=self.models.InboxThread(organization_id=self.org_id,phone=self.phone)
        self.db.session.add(thread)
        self.db.session.flush()
        self.db.session.add(self.models.InboxMessage(organization_id=self.org_id,thread_id=thread.id,phone=self.phone,direction='outbound',body='Historical reply',automation_source='survey'))
        self.db.session.commit()
        migration=importlib.import_module('app.saas_migrations.024_add_automation_reply_outbox')
        with self.db.engine.begin() as connection:
            migration.apply(connection,MagicMock())
            migration.apply(connection,MagicMock())
        self.assertEqual(self._rows(),[])
        self.assertEqual(self.outbox.recover_automation_replies()['seen'],0)
        self.queue.enqueue.assert_not_called()

    def test_postgres_concurrent_duplicate_sid_commits_one_flow_and_original_outbox_keys(self):
        if not self.postgres_root:
            self.skipTest('Concurrent inbound/outbox replay requires isolated PostgreSQL.')
        barrier=threading.Barrier(3)
        results,errors=[],[]
        def inbound():
            try:
                barrier.wait(timeout=5)
                results.append(self._post('SURVEY','SM-concurrent-outbox').status_code)
            except BaseException as error:
                errors.append(error)
        self.db.session.rollback()
        workers=[threading.Thread(target=inbound) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(10)
        self.assertEqual(errors,[])
        self.assertEqual(results,[200,200])
        self.assertEqual(len(self._rows()),2)
        self.assertEqual(self.models.SurveySession.query.count(),1)
        self.assertEqual(self.models.InboxMessage.query.filter_by(direction='inbound').count(),1)
        self._drain()
        self.assertEqual(len(self.calls),2)

    def test_wrong_organization_cannot_dispatch_or_complete_another_tenant_reply(self):
        self._post()
        row=self._rows()[0]
        other=self.models.Organization(name='Other automation tenant',slug='other-automation',status='active')
        self.db.session.add(other)
        self.db.session.commit()
        self.assertEqual(self.outbox.dispatch_automation_reply(other.id,row.id)['status'],'missing')
        self.assertEqual(self._rows()[0].status,'queued')
        self.assertEqual(self.calls,[])

    def test_postgres_stop_ack_waits_for_inflight_then_blocks_remaining_automatic_reply(self):
        if not self.postgres_root:
            self.skipTest('STOP/submission linearization requires isolated PostgreSQL.')
        import psycopg
        from sqlalchemy import text
        entered,release,stop_started,stop_done=threading.Event(),threading.Event(),threading.Event(),threading.Event()
        errors,stop_results,stop_pid=[],[],[]
        original=self.provider.client.messages.create.side_effect
        def inflight(**params):
            entered.set()
            if not release.wait(8):
                raise TimeoutError('Test provider barrier was not released')
            return original(**params)
        self.provider.client.messages.create.side_effect=inflight
        @self.app.before_request
        def observe_stop_backend():
            from flask import request
            if request.form.get('Body')=='STOP':
                stop_pid.append(self.db.session.execute(text('SELECT pg_backend_pid()')).scalar_one())
                stop_started.set()
        self._post('SURVEY')
        first_id=self._rows()[0].id
        def sender():
            try:
                with self.app.app_context():
                    self._dispatch(first_id)
            except BaseException as error:
                errors.append(error)
        def stopper():
            try:
                stop_results.append(self._post('STOP','SM-stop-during-automation').status_code)
                stop_done.set()
            except BaseException as error:
                errors.append(error)
        self.db.session.rollback()
        send_thread,stop_thread=threading.Thread(target=sender),threading.Thread(target=stopper)
        send_thread.start()
        try:
            self.assertTrue(entered.wait(5))
            stop_thread.start()
            self.assertTrue(stop_started.wait(3))
            waiting=False
            with psycopg.connect(self.postgres_root,autocommit=True,client_encoding='UTF8') as observer:
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    row=observer.execute('SELECT wait_event_type, cardinality(pg_blocking_pids(pid)) FROM pg_stat_activity WHERE pid=%s',(stop_pid[0],)).fetchone()
                    if row and row[0]=='Lock' and row[1]>0:
                        waiting=True
                        break
                    time.sleep(0.01)
            self.assertTrue(waiting,'STOP did not wait for the guarded in-flight provider submission')
            self.assertFalse(stop_done.is_set())
        finally:
            release.set()
            send_thread.join(10)
            if stop_thread.ident:
                stop_thread.join(10)
        self.assertEqual(errors,[])
        self.assertEqual(stop_results,[200])
        self.assertFalse(send_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self._drain()
        self.assertEqual(len(self.calls),1)
        self.assertEqual([row.status for row in self._rows()],['accepted','cancelled'])
