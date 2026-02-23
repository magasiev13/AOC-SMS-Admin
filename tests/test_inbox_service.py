import importlib
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch


class TestInboxService(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import (
            CommunityMember,
            Event,
            EventRegistration,
            InboxMessage,
            InboxThread,
            KeywordAutomationRule,
            SurveyFlow,
            SurveyResponse,
            SurveySession,
            UnsubscribedContact,
        )
        from app.services.inbox_service import process_inbound_sms, send_thread_reply

        self.CommunityMember = CommunityMember
        self.db = db
        self.Event = Event
        self.EventRegistration = EventRegistration
        self.InboxMessage = InboxMessage
        self.InboxThread = InboxThread
        self.KeywordAutomationRule = KeywordAutomationRule
        self.SurveyFlow = SurveyFlow
        self.SurveySession = SurveySession
        self.SurveyResponse = SurveyResponse
        self.UnsubscribedContact = UnsubscribedContact
        self.process_inbound_sms = process_inbound_sms
        self.send_thread_reply = send_thread_reply

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            TWILIO_VALIDATE_INBOUND_SIGNATURE=False,
            INBOUND_AUTO_REPLY_ENABLED=True,
            SURVEY_AMBIGUOUS_DUPLICATE_WINDOW_SECONDS=3,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._app_context.pop()
        self._temp_dir.cleanup()

        if self._original_flask_debug is None:
            os.environ.pop("FLASK_DEBUG", None)
        else:
            os.environ["FLASK_DEBUG"] = self._original_flask_debug
        os.environ.pop("DATABASE_URL", None)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_keyword_rule_matches_and_replies(self, mock_get_twilio) -> None:
        rule = self.KeywordAutomationRule(keyword="HELP", response_body="Support is on the way.", is_active=True)
        self.db.session.add(rule)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM111",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        result = self.process_inbound_sms(
            {"From": "+15551234567", "Body": "help", "MessageSid": "SM-IN-1"}
        )
        self.assertEqual(result["status"], "keyword_reply")

        thread = self.InboxThread.query.filter_by(phone="+15551234567").first()
        self.assertIsNotNone(thread)

        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.created_at.asc())
            .all()
        )
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].direction, "inbound")
        self.assertEqual(messages[0].matched_keyword, "HELP")
        self.assertEqual(messages[1].direction, "outbound")
        self.assertEqual(messages[1].body, "Support is on the way.")

        refreshed = self.db.session.get(self.KeywordAutomationRule, rule.id)
        self.assertEqual(refreshed.match_count, 1)
        self.assertIsNotNone(refreshed.last_matched_at)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_keyword_rule_whitespace_normalization_matches_inbound(self, mock_get_twilio) -> None:
        rule = self.KeywordAutomationRule(
            keyword="  help   now ",
            response_body="Support is on the way.",
            is_active=True,
        )
        self.db.session.add(rule)
        self.db.session.commit()
        self.assertEqual(rule.keyword, "HELP NOW")

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM111A",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        result = self.process_inbound_sms(
            {"From": "+15551234567", "Body": "help now", "MessageSid": "SM-IN-1A"}
        )
        self.assertEqual(result["status"], "keyword_reply")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_survey_flow_starts_and_completes(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="RSVP Flow",
            trigger_keyword="RSVP",
            intro_message="Thanks for joining.",
            completion_message="All set. Thank you!",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "How many guests?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM222",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        start_result = self.process_inbound_sms(
            {"From": "+15550001111", "Body": "RSVP", "MessageSid": "SM-IN-2"}
        )
        self.assertEqual(start_result["status"], "survey_started")

        session = self.SurveySession.query.filter_by(phone="+15550001111", status="active").first()
        self.assertIsNotNone(session)
        self.assertEqual(session.current_question_index, 0)

        first_answer = self.process_inbound_sms(
            {"From": "+15550001111", "Body": "Alex", "MessageSid": "SM-IN-3"}
        )
        self.assertEqual(first_answer["status"], "survey_response")

        session = self.SurveySession.query.filter_by(phone="+15550001111").first()
        self.assertEqual(session.current_question_index, 1)
        self.assertEqual(session.status, "active")

        second_answer = self.process_inbound_sms(
            {"From": "+15550001111", "Body": "3", "MessageSid": "SM-IN-4"}
        )
        self.assertEqual(second_answer["status"], "survey_response")

        session = self.SurveySession.query.filter_by(phone="+15550001111").first()
        self.assertEqual(session.status, "completed")
        self.assertIsNotNone(session.completed_at)

        responses = self.SurveyResponse.query.filter_by(phone="+15550001111").all()
        self.assertEqual(len(responses), 2)

        refreshed_survey = self.db.session.get(self.SurveyFlow, survey.id)
        self.assertEqual(refreshed_survey.start_count, 1)
        self.assertEqual(refreshed_survey.completion_count, 1)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_duplicate_message_sid_during_active_survey_is_idempotent(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="RSVP Idempotent Flow",
            trigger_keyword="RSVP DUP",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "How many guests?", "Any notes?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM777A",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        self.process_inbound_sms(
            {"From": "+15550001112", "Body": "RSVP DUP", "MessageSid": "SM-IN-DUP-START"}
        )
        first_answer = self.process_inbound_sms(
            {"From": "+15550001112", "Body": "Alex", "MessageSid": "SM-IN-DUP-A1"}
        )
        duplicate_answer = self.process_inbound_sms(
            {"From": "+15550001112", "Body": "Alex", "MessageSid": "SM-IN-DUP-A1"}
        )

        self.assertEqual(first_answer["status"], "survey_response")
        self.assertEqual(duplicate_answer["status"], "duplicate")

        session = self.SurveySession.query.filter_by(phone="+15550001112").first()
        self.assertIsNotNone(session)
        self.assertEqual(session.current_question_index, 1)
        self.assertEqual(session.status, "active")

        responses = (
            self.SurveyResponse.query.filter_by(phone="+15550001112")
            .order_by(self.SurveyResponse.question_index.asc())
            .all()
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].answer, "Alex")

        thread = self.InboxThread.query.filter_by(phone="+15550001112").first()
        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.id.asc())
            .all()
        )
        outbound_questions = [
            message.body
            for message in messages
            if message.direction == "outbound"
            and "?" in message.body
        ]
        self.assertEqual(outbound_questions.count("How many guests?"), 1)
        self.assertEqual(outbound_questions.count("Any notes?"), 0)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_empty_inbound_during_active_survey_does_not_advance(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="RSVP Empty Guard Flow",
            trigger_keyword="RSVP EMPTY",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "How many guests?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM777B",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        self.process_inbound_sms(
            {"From": "+15550001113", "Body": "RSVP EMPTY", "MessageSid": "SM-IN-EMPTY-START"}
        )
        ignored = self.process_inbound_sms(
            {"From": "+15550001113", "Body": "   ", "MessageSid": "SM-IN-EMPTY-1"}
        )

        self.assertEqual(ignored["status"], "survey_ignored_empty")
        session = self.SurveySession.query.filter_by(phone="+15550001113").first()
        self.assertIsNotNone(session)
        self.assertEqual(session.current_question_index, 0)
        self.assertEqual(session.status, "active")

        responses = self.SurveyResponse.query.filter_by(phone="+15550001113").all()
        self.assertEqual(len(responses), 0)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_rapid_same_text_different_sid_requires_confirmation(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="RSVP Confirm Flow",
            trigger_keyword="RSVP CONFIRM",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["Question one?", "Question two?", "Question three?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM777D",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        self.process_inbound_sms(
            {"From": "+15550001116", "Body": "RSVP CONFIRM", "MessageSid": "SM-IN-CONFIRM-START"}
        )
        first_answer = self.process_inbound_sms(
            {"From": "+15550001116", "Body": "YES", "MessageSid": "SM-IN-CONFIRM-A1"}
        )
        ambiguous = self.process_inbound_sms(
            {"From": "+15550001116", "Body": "YES", "MessageSid": "SM-IN-CONFIRM-A2"}
        )

        self.assertEqual(first_answer["status"], "survey_response")
        self.assertEqual(ambiguous["status"], "survey_confirmation_required")

        session = self.SurveySession.query.filter_by(phone="+15550001116").first()
        self.assertIsNotNone(session)
        self.assertEqual(session.current_question_index, 1)
        self.assertEqual(session.status, "active")

        responses = (
            self.SurveyResponse.query.filter_by(phone="+15550001116")
            .order_by(self.SurveyResponse.question_index.asc())
            .all()
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].answer, "YES")

        thread = self.InboxThread.query.filter_by(phone="+15550001116").first()
        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.id.asc())
            .all()
        )
        self.assertTrue(
            any(
                message.direction == "outbound"
                and "reply: CONFIRM YES" in message.body
                for message in messages
            )
        )

        confirmed = self.process_inbound_sms(
            {"From": "+15550001116", "Body": "CONFIRM YES", "MessageSid": "SM-IN-CONFIRM-A3"}
        )
        self.assertEqual(confirmed["status"], "survey_response")

        session = self.SurveySession.query.filter_by(phone="+15550001116").first()
        self.assertEqual(session.current_question_index, 2)
        self.assertEqual(session.status, "active")

        responses = (
            self.SurveyResponse.query.filter_by(phone="+15550001116")
            .order_by(self.SurveyResponse.question_index.asc())
            .all()
        )
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[1].answer, "YES")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_same_text_different_sid_after_window_advances_without_confirmation(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="RSVP Confirm Window Flow",
            trigger_keyword="RSVP WINDOW",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["Question one?", "Question two?", "Question three?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM777E",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        self.process_inbound_sms(
            {"From": "+15550001117", "Body": "RSVP WINDOW", "MessageSid": "SM-IN-WINDOW-START"}
        )
        first_answer = self.process_inbound_sms(
            {"From": "+15550001117", "Body": "YES", "MessageSid": "SM-IN-WINDOW-A1"}
        )
        self.assertEqual(first_answer["status"], "survey_response")

        response = self.SurveyResponse.query.filter_by(
            phone="+15550001117",
            question_index=0,
        ).first()
        self.assertIsNotNone(response)
        response.created_at = response.created_at - timedelta(seconds=4)
        self.db.session.commit()

        second_answer = self.process_inbound_sms(
            {"From": "+15550001117", "Body": "YES", "MessageSid": "SM-IN-WINDOW-A2"}
        )
        self.assertEqual(second_answer["status"], "survey_response")

        session = self.SurveySession.query.filter_by(phone="+15550001117").first()
        self.assertEqual(session.current_question_index, 2)
        self.assertEqual(session.status, "active")

        responses = (
            self.SurveyResponse.query.filter_by(phone="+15550001117")
            .order_by(self.SurveyResponse.question_index.asc())
            .all()
        )
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[1].answer, "YES")

        thread = self.InboxThread.query.filter_by(phone="+15550001117").first()
        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.id.asc())
            .all()
        )
        self.assertFalse(
            any(
                message.direction == "outbound"
                and "reply: CONFIRM YES" in message.body
                for message in messages
            )
        )

    @patch("app.services.inbox_service.get_twilio_service")
    def test_inbound_sms_message_sid_field_is_used_for_idempotency(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="RSVP SmsMessageSid Flow",
            trigger_keyword="RSVP SMS SID",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "How many guests?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM777C",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        self.process_inbound_sms(
            {"From": "+15550001115", "Body": "RSVP SMS SID", "SmsMessageSid": "SM-IN-SMS-SID-START"}
        )
        first_answer = self.process_inbound_sms(
            {"From": "+15550001115", "Body": "Alex", "SmsMessageSid": "SM-IN-SMS-SID-A1"}
        )
        duplicate_answer = self.process_inbound_sms(
            {"From": "+15550001115", "Body": "Alex", "SmsMessageSid": "SM-IN-SMS-SID-A1"}
        )

        self.assertEqual(first_answer["status"], "survey_response")
        self.assertEqual(duplicate_answer["status"], "duplicate")

        responses = self.SurveyResponse.query.filter_by(phone="+15550001115").all()
        self.assertEqual(len(responses), 1)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_linked_survey_completion_creates_event_registration(self, mock_get_twilio) -> None:
        event = self.Event(title="Spring Gala")
        self.db.session.add(event)
        self.db.session.flush()

        survey = self.SurveyFlow(
            name="Linked RSVP",
            trigger_keyword="JOIN GALA",
            intro_message="Welcome.",
            completion_message="Done.",
            linked_event_id=event.id,
            is_active=True,
        )
        survey.set_questions(["What is your name?", "How many guests?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM900",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        self.process_inbound_sms(
            {"From": "+15557770001", "Body": "JOIN GALA", "MessageSid": "SM-IN-LINK-1"}
        )
        self.process_inbound_sms(
            {"From": "+15557770001", "Body": "Alex", "MessageSid": "SM-IN-LINK-2"}
        )
        self.process_inbound_sms(
            {"From": "+15557770001", "Body": "2", "MessageSid": "SM-IN-LINK-3"}
        )

        registration = self.EventRegistration.query.filter_by(
            event_id=event.id,
            phone="+15557770001",
        ).first()
        self.assertIsNotNone(registration)
        self.assertEqual(registration.name, "Alex")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_linked_survey_completion_upserts_event_registration_by_phone(self, mock_get_twilio) -> None:
        event = self.Event(title="Summer Meetup")
        self.db.session.add(event)
        self.db.session.flush()

        survey = self.SurveyFlow(
            name="Linked RSVP Upsert",
            trigger_keyword="SUMMER RSVP",
            intro_message="Welcome.",
            completion_message="Done.",
            linked_event_id=event.id,
            is_active=True,
        )
        survey.set_questions(["Name?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM901",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        self.process_inbound_sms(
            {"From": "+15557770002", "Body": "SUMMER RSVP", "MessageSid": "SM-IN-UP-1"}
        )
        self.process_inbound_sms(
            {"From": "+15557770002", "Body": "Alex", "MessageSid": "SM-IN-UP-2"}
        )
        self.process_inbound_sms(
            {"From": "+15557770002", "Body": "SUMMER RSVP", "MessageSid": "SM-IN-UP-3"}
        )
        self.process_inbound_sms(
            {"From": "+15557770002", "Body": "Jordan", "MessageSid": "SM-IN-UP-4"}
        )

        registrations = self.EventRegistration.query.filter_by(
            event_id=event.id,
            phone="+15557770002",
        ).all()
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0].name, "Jordan")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_unlinked_survey_does_not_create_event_registration(self, mock_get_twilio) -> None:
        event = self.Event(title="Unlinked Event")
        self.db.session.add(event)

        survey = self.SurveyFlow(
            name="Plain Survey",
            trigger_keyword="PLAIN SURVEY",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["Name?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM902",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        self.process_inbound_sms(
            {"From": "+15557770003", "Body": "PLAIN SURVEY", "MessageSid": "SM-IN-NL-1"}
        )
        self.process_inbound_sms(
            {"From": "+15557770003", "Body": "Casey", "MessageSid": "SM-IN-NL-2"}
        )

        registration = self.EventRegistration.query.filter_by(phone="+15557770003").first()
        self.assertIsNone(registration)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_survey_trigger_whitespace_normalization_matches_inbound(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="Normalized Trigger Survey",
            trigger_keyword="  check   in ",
            intro_message="Thanks for joining.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?"])
        self.db.session.add(survey)
        self.db.session.commit()
        self.assertEqual(survey.trigger_keyword, "CHECK IN")

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM222A",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        result = self.process_inbound_sms(
            {"From": "+15550001111", "Body": "check in", "MessageSid": "SM-IN-2A"}
        )
        self.assertEqual(result["status"], "survey_started")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_active_survey_yes_is_recorded_as_answer(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="Attendance Flow",
            trigger_keyword="ATTEND",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "Are you attending?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM555",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        start_result = self.process_inbound_sms(
            {"From": "+15559990000", "Body": "ATTEND", "MessageSid": "SM-IN-YES-1"}
        )
        self.assertEqual(start_result["status"], "survey_started")

        first_answer = self.process_inbound_sms(
            {"From": "+15559990000", "Body": "Taylor", "MessageSid": "SM-IN-YES-2"}
        )
        self.assertEqual(first_answer["status"], "survey_response")

        second_answer = self.process_inbound_sms(
            {"From": "+15559990000", "Body": "YES", "MessageSid": "SM-IN-YES-3"}
        )
        self.assertEqual(second_answer["status"], "survey_response")

        session = self.SurveySession.query.filter_by(phone="+15559990000").first()
        self.assertEqual(session.status, "completed")
        self.assertIsNotNone(session.completed_at)

        responses = (
            self.SurveyResponse.query.filter_by(phone="+15559990000")
            .order_by(self.SurveyResponse.question_index.asc())
            .all()
        )
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[1].answer, "YES")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15559990000").first()
        self.assertIsNone(unsubscribed)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_confirm_prefix_without_pending_confirmation_is_recorded_verbatim(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="Confirm Prefix Literal Flow",
            trigger_keyword="CONFIRM LITERAL",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "Share your response phrase."])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM556",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        start_result = self.process_inbound_sms(
            {"From": "+15559990001", "Body": "CONFIRM LITERAL", "MessageSid": "SM-IN-CONF-LIT-1"}
        )
        self.assertEqual(start_result["status"], "survey_started")

        first_answer = self.process_inbound_sms(
            {"From": "+15559990001", "Body": "Taylor", "MessageSid": "SM-IN-CONF-LIT-2"}
        )
        self.assertEqual(first_answer["status"], "survey_response")

        second_answer = self.process_inbound_sms(
            {"From": "+15559990001", "Body": "CONFIRM YES", "MessageSid": "SM-IN-CONF-LIT-3"}
        )
        self.assertEqual(second_answer["status"], "survey_response")

        responses = (
            self.SurveyResponse.query.filter_by(phone="+15559990001")
            .order_by(self.SurveyResponse.question_index.asc())
            .all()
        )
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[1].answer, "CONFIRM YES")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_cancel_during_active_survey_opts_out_and_cancels_session(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="Cancel Flow",
            trigger_keyword="CHECKIN",
            intro_message="Starting flow.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "How many guests?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM666",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        start_result = self.process_inbound_sms(
            {"From": "+15551112222", "Body": "CHECKIN", "MessageSid": "SM-IN-CANCEL-1"}
        )
        self.assertEqual(start_result["status"], "survey_started")

        cancel_result = self.process_inbound_sms(
            {"From": "+15551112222", "Body": "CANCEL", "MessageSid": "SM-IN-CANCEL-2"}
        )
        self.assertEqual(cancel_result["status"], "opt_out")

        session = self.SurveySession.query.filter_by(phone="+15551112222").first()
        self.assertEqual(session.status, "cancelled")
        self.assertIsNotNone(session.completed_at)

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15551112222").first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.source, "inbound")
        self.assertEqual(unsubscribed.reason, "Inbound STOP keyword received")

        thread = self.InboxThread.query.filter_by(phone="+15551112222").first()
        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.created_at.asc())
            .all()
        )
        self.assertFalse(
            any("You are unsubscribed and will no longer receive SMS alerts." in msg.body for msg in messages)
        )

    @patch("app.services.inbox_service.get_twilio_service")
    def test_cancel_without_active_survey_opts_out(self, mock_get_twilio) -> None:
        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM777",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        cancel_result = self.process_inbound_sms(
            {"From": "+15556667777", "Body": "CANCEL", "MessageSid": "SM-IN-CANCEL-3"}
        )
        self.assertEqual(cancel_result["status"], "opt_out")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15556667777").first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.source, "inbound")
        self.assertEqual(unsubscribed.reason, "Inbound STOP keyword received")

        thread = self.InboxThread.query.filter_by(phone="+15556667777").first()
        self.assertIsNotNone(thread)
        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.created_at.asc())
            .all()
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].direction, "inbound")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_stop_then_start_updates_unsubscribe_state(self, mock_get_twilio) -> None:
        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM333",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        stop_result = self.process_inbound_sms(
            {"From": "+15554443333", "Body": "STOP", "MessageSid": "SM-IN-5"}
        )
        self.assertEqual(stop_result["status"], "opt_out")
        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15554443333").first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.source, "inbound")
        self.assertEqual(unsubscribed.reason, "Inbound STOP keyword received")

        start_result = self.process_inbound_sms(
            {"From": "+15554443333", "Body": "START", "MessageSid": "SM-IN-6"}
        )
        self.assertEqual(start_result["status"], "opt_in")
        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15554443333").first()
        self.assertIsNone(unsubscribed)

    def test_stop_unsubscribe_prefers_community_name_over_thread_name(self) -> None:
        self.db.session.add(self.CommunityMember(name="Community Name", phone="+15553334444"))
        self.db.session.commit()

        stop_result = self.process_inbound_sms(
            {
                "From": "+15553334444",
                "Body": "STOP",
                "MessageSid": "SM-IN-COMMUNITY-NAME-STOP",
                "ProfileName": "Thread Name",
            }
        )
        self.assertEqual(stop_result["status"], "opt_out")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15553334444").first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.name, "Community Name")
        self.assertEqual(unsubscribed.source, "inbound")

    def test_stop_unsubscribe_uses_thread_name_when_community_missing(self) -> None:
        stop_result = self.process_inbound_sms(
            {
                "From": "+15553335555",
                "Body": "STOP",
                "MessageSid": "SM-IN-THREAD-NAME-STOP",
                "ProfileName": "Thread Name",
            }
        )
        self.assertEqual(stop_result["status"], "opt_out")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15553335555").first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.name, "Thread Name")
        self.assertEqual(unsubscribed.source, "inbound")

    def test_stop_unsubscribe_uses_event_registration_name_when_other_names_missing(self) -> None:
        event = self.Event(title="Fallback Event")
        self.db.session.add(event)
        self.db.session.flush()
        self.db.session.add(
            self.EventRegistration(
                event_id=event.id,
                name="Event Registration Name",
                phone="+15553337777",
            )
        )
        self.db.session.commit()

        stop_result = self.process_inbound_sms(
            {
                "From": "+15553337777",
                "Body": "STOP",
                "MessageSid": "SM-IN-EVENT-NAME-STOP",
            }
        )
        self.assertEqual(stop_result["status"], "opt_out")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15553337777").first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.name, "Event Registration Name")
        self.assertEqual(unsubscribed.source, "inbound")

    def test_stop_unsubscribe_matches_community_name_with_legacy_phone_format(self) -> None:
        self.db.session.add(
            self.CommunityMember(
                name="Legacy Community Name",
                phone="(555) 333-8899",
            )
        )
        self.db.session.commit()

        stop_result = self.process_inbound_sms(
            {
                "From": "+15553338899",
                "Body": "STOP",
                "MessageSid": "SM-IN-LEGACY-PHONE-STOP",
            }
        )
        self.assertEqual(stop_result["status"], "opt_out")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15553338899").first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.name, "Legacy Community Name")
        self.assertEqual(unsubscribed.source, "inbound")

    def test_stop_unsubscribe_keeps_existing_name(self) -> None:
        self.db.session.add(
            self.UnsubscribedContact(
                name="Existing Name",
                phone="+15553336666",
                reason="Old reason",
                source="manual",
            )
        )
        self.db.session.commit()

        stop_result = self.process_inbound_sms(
            {
                "From": "+15553336666",
                "Body": "STOP",
                "MessageSid": "SM-IN-KEEP-NAME-STOP",
                "ProfileName": "Thread Name",
            }
        )
        self.assertEqual(stop_result["status"], "opt_out")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15553336666").first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.name, "Existing Name")
        self.assertEqual(unsubscribed.reason, "Inbound STOP keyword received")
        self.assertEqual(unsubscribed.source, "inbound")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_send_thread_reply_blocks_when_recipient_is_unsubscribed(self, mock_get_twilio) -> None:
        thread = self.InboxThread(phone="+15554445555", contact_name="Thread Name")
        self.db.session.add(thread)
        self.db.session.add(
            self.UnsubscribedContact(
                phone=thread.phone,
                reason="Inbound STOP keyword received",
                source="inbound",
            )
        )
        self.db.session.commit()

        result = self.send_thread_reply(thread.id, "Hello from admin", actor="admin")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "blocked_opt_out")
        mock_get_twilio.assert_not_called()

        outbound_count = self.InboxMessage.query.filter_by(thread_id=thread.id, direction="outbound").count()
        self.assertEqual(outbound_count, 0)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_send_thread_reply_opt_out_failure_upserts_unsubscribed_with_message_failure_source(self, mock_get_twilio) -> None:
        thread = self.InboxThread(phone="+15554446666", contact_name="Thread Name")
        self.db.session.add(thread)
        self.db.session.add(self.CommunityMember(name="Community Name", phone=thread.phone))
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": False,
            "sid": None,
            "status": "failed",
            "error": "Attempt to send to unsubscribed recipient (21610)",
        }
        mock_get_twilio.return_value = mock_service

        result = self.send_thread_reply(thread.id, "Hello from admin", actor="admin")
        self.assertFalse(result["success"])

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone=thread.phone).first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.name, "Community Name")
        self.assertEqual(unsubscribed.source, "message_failure")
        self.assertEqual(unsubscribed.reason, "Attempt to send to unsubscribed recipient (21610)")

        outbound_message = (
            self.InboxMessage.query.filter_by(thread_id=thread.id, direction="outbound")
            .order_by(self.InboxMessage.id.desc())
            .first()
        )
        self.assertIsNotNone(outbound_message)
        self.assertEqual(outbound_message.delivery_status, "failed")
        self.assertEqual(outbound_message.delivery_error, "Attempt to send to unsubscribed recipient (21610)")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_send_thread_reply_opt_out_failure_uses_event_registration_name_fallback(self, mock_get_twilio) -> None:
        event = self.Event(title="Manual Reply Fallback Event")
        self.db.session.add(event)
        self.db.session.flush()
        self.db.session.add(
            self.EventRegistration(
                event_id=event.id,
                name="Event Registration Name",
                phone="+15554447777",
            )
        )
        thread = self.InboxThread(phone="+15554447777")
        self.db.session.add(thread)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": False,
            "sid": None,
            "status": "failed",
            "error": "Attempt to send to unsubscribed recipient (21610)",
        }
        mock_get_twilio.return_value = mock_service

        result = self.send_thread_reply(thread.id, "Hello from admin", actor="admin")
        self.assertFalse(result["success"])

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone=thread.phone).first()
        self.assertIsNotNone(unsubscribed)
        self.assertEqual(unsubscribed.name, "Event Registration Name")
        self.assertEqual(unsubscribed.source, "message_failure")
        self.assertEqual(unsubscribed.reason, "Attempt to send to unsubscribed recipient (21610)")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_start_when_already_subscribed_sends_ack(self, mock_get_twilio) -> None:
        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM888",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        start_result = self.process_inbound_sms(
            {"From": "+15550009999", "Body": "START", "MessageSid": "SM-IN-START-1"}
        )
        self.assertEqual(start_result["status"], "opt_in")

        thread = self.InboxThread.query.filter_by(phone="+15550009999").first()
        self.assertIsNotNone(thread)
        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.created_at.asc())
            .all()
        )
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].direction, "inbound")
        self.assertEqual(messages[1].direction, "outbound")
        self.assertIn("already subscribed", messages[1].body)

    def test_model_indexes_match_migration_indexes(self) -> None:
        from sqlalchemy import inspect

        inspector = inspect(self.db.engine)
        inbox_thread_indexes = {index["name"] for index in inspector.get_indexes("inbox_threads")}
        survey_session_indexes = {index["name"] for index in inspector.get_indexes("survey_sessions")}

        self.assertIn("ix_inbox_threads_last_message_at", inbox_thread_indexes)
        self.assertIn("ix_survey_sessions_phone_status", survey_session_indexes)

    def test_webhook_rejects_when_signature_required(self) -> None:
        self.app.config["TWILIO_VALIDATE_INBOUND_SIGNATURE"] = True
        response = self.client.post(
            "/webhooks/twilio/inbound",
            data={"From": "+15557778888", "Body": "Hello", "MessageSid": "SM-IN-7"},
        )
        self.assertEqual(response.status_code, 403)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_webhook_accepts_when_signature_validation_disabled(self, mock_get_twilio) -> None:
        self.app.config["TWILIO_VALIDATE_INBOUND_SIGNATURE"] = False
        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM444",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        response = self.client.post(
            "/webhooks/twilio/inbound",
            data={"From": "+15558889999", "Body": "hello", "MessageSid": "SM-IN-8"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<Response></Response>", response.data)


if __name__ == "__main__":
    unittest.main()
