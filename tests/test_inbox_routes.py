import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


class TestInboxRoutes(unittest.TestCase):
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
            AppUser,
            CommunityMember,
            InboxMessage,
            InboxThread,
            SurveyFlow,
            SurveyResponse,
            SurveySession,
        )

        self.db = db
        self.AppUser = AppUser
        self.CommunityMember = CommunityMember
        self.InboxMessage = InboxMessage
        self.InboxThread = InboxThread
        self.SurveyFlow = SurveyFlow
        self.SurveySession = SurveySession
        self.SurveyResponse = SurveyResponse

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        admin = self.AppUser(username="admin", role="admin", must_change_password=False)
        admin.set_password("admin-pass")
        social_manager = self.AppUser(username="social", role="social_manager", must_change_password=False)
        social_manager.set_password("social-pass")
        viewer = self.AppUser(username="viewer", role="viewer", must_change_password=False)
        viewer.set_password("viewer-pass")
        self.db.session.add_all([admin, social_manager, viewer])
        self.db.session.commit()

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

    def _login(self) -> None:
        self._login_as("admin", "admin-pass")

    def _login_as(self, username: str, password: str) -> None:
        response = self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def _logout(self) -> None:
        response = self.client.get("/logout", follow_redirects=False)
        self.assertEqual(response.status_code, 302)

    def _create_thread(self, *, phone: str, contact_name: str | None = None):
        thread = self.InboxThread(phone=phone, contact_name=contact_name)
        self.db.session.add(thread)
        self.db.session.flush()
        return thread

    def _create_message(
        self,
        *,
        thread,
        body: str,
        direction: str,
        created_at: datetime | None = None,
    ):
        message = self.InboxMessage(
            thread_id=thread.id,
            phone=thread.phone,
            direction=direction,
            body=body,
            created_at=created_at or datetime.now(timezone.utc),
        )
        self.db.session.add(message)
        self.db.session.flush()
        return message

    def _create_survey_session(self, *, thread, name: str, keyword: str):
        survey = self.SurveyFlow(
            name=name,
            trigger_keyword=keyword,
            intro_message=None,
            completion_message=None,
            is_active=True,
        )
        survey.set_questions(["How are you?"])
        self.db.session.add(survey)
        self.db.session.flush()

        session = self.SurveySession(
            survey_id=survey.id,
            thread_id=thread.id,
            phone=thread.phone,
            status="active",
        )
        self.db.session.add(session)
        self.db.session.flush()

        response = self.SurveyResponse(
            session_id=session.id,
            survey_id=survey.id,
            phone=thread.phone,
            question_index=0,
            question_prompt="How are you?",
            answer="Great",
        )
        self.db.session.add(response)
        self.db.session.flush()
        return survey, session, response

    def test_inbox_status_requires_login(self) -> None:
        response = self.client.get("/inbox/status", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_inbox_status_returns_zero_when_no_messages(self) -> None:
        self._login()
        response = self.client.get("/inbox/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("latest_message_id"), 0)

    def test_inbox_status_returns_latest_message_id(self) -> None:
        self._login()

        thread = self.InboxThread(phone="+17202808358")
        self.db.session.add(thread)
        self.db.session.flush()

        message = self.InboxMessage(
            thread_id=thread.id,
            phone=thread.phone,
            direction="inbound",
            body="Hello from inbound",
        )
        self.db.session.add(message)
        self.db.session.commit()

        response = self.client.get("/inbox/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("latest_message_id"), message.id)

    def test_inbox_renders_community_name_with_phone_secondary(self) -> None:
        self._login()

        thread = self.InboxThread(phone="+17202808358")
        self.db.session.add(thread)
        self.db.session.add(self.CommunityMember(name="Alex Rivera", phone=thread.phone))
        self.db.session.commit()

        response = self.client.get(f"/inbox?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<div class="fw-semibold">Alex Rivera</div>', html)
        self.assertIn(f'<div class="small text-muted">{thread.phone}</div>', html)

    def test_inbox_community_name_overrides_existing_thread_contact_name(self) -> None:
        self._login()

        thread = self.InboxThread(phone="+17202345027", contact_name="Twilio Name")
        self.db.session.add(thread)
        self.db.session.add(self.CommunityMember(name="Community Name", phone=thread.phone))
        self.db.session.commit()

        response = self.client.get(f"/inbox?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<div class="fw-semibold">Community Name</div>', html)
        self.assertIn(f'value="{thread.contact_name}"', html)

    def test_inbox_search_matches_community_name_when_thread_name_missing(self) -> None:
        self._login()

        thread = self.InboxThread(phone="+17209990000")
        self.db.session.add(thread)
        self.db.session.add(self.CommunityMember(name="Jordan Blake", phone=thread.phone))
        self.db.session.commit()

        response = self.client.get("/inbox?search=Jordan")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Jordan Blake", html)
        self.assertIn(thread.phone, html)

    def test_inbox_thread_update_contact_name(self) -> None:
        self._login()
        thread = self._create_thread(phone="+17205550001")
        self.db.session.commit()

        response = self.client.post(
            f"/inbox/threads/{thread.id}/update",
            data={"contact_name": "  Jordan Blake ", "search": "Jordan"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"thread={thread.id}", response.headers.get("Location", ""))
        self.assertIn("search=Jordan", response.headers.get("Location", ""))

        updated = self.db.session.get(self.InboxThread, thread.id)
        self.assertEqual(updated.contact_name, "Jordan Blake")

    def test_inbox_thread_update_blank_contact_name_clears_value(self) -> None:
        self._login()
        thread = self._create_thread(phone="+17205550002", contact_name="Legacy Name")
        self.db.session.commit()

        response = self.client.post(
            f"/inbox/threads/{thread.id}/update",
            data={"contact_name": "   ", "search": ""},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        updated = self.db.session.get(self.InboxThread, thread.id)
        self.assertIsNone(updated.contact_name)

    def test_inbox_messages_bulk_delete_rebuilds_thread_rollup(self) -> None:
        self._login()
        thread = self._create_thread(phone="+17205550003")
        now = datetime.now(timezone.utc)
        old_message = self._create_message(
            thread=thread,
            body="First inbound",
            direction="inbound",
            created_at=now - timedelta(minutes=5),
        )
        latest_message = self._create_message(
            thread=thread,
            body="Latest outbound",
            direction="outbound",
            created_at=now - timedelta(minutes=1),
        )
        thread.last_message_at = latest_message.created_at
        thread.last_message_preview = latest_message.body
        thread.last_direction = latest_message.direction
        thread.unread_count = 3
        self.db.session.commit()

        response = self.client.post(
            "/inbox/messages/bulk-delete",
            data={"thread_id": thread.id, "message_ids": [str(latest_message.id)], "search": ""},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"thread={thread.id}", response.headers.get("Location", ""))
        self.assertIsNone(self.db.session.get(self.InboxMessage, latest_message.id))

        updated_thread = self.db.session.get(self.InboxThread, thread.id)
        self.assertEqual(updated_thread.last_message_preview, "First inbound")
        self.assertEqual(updated_thread.last_direction, "inbound")
        self.assertEqual(updated_thread.unread_count, 1)
        self.assertEqual(
            updated_thread.last_message_at.replace(tzinfo=None),
            old_message.created_at.replace(tzinfo=None),
        )

    def test_inbox_thread_delete_removes_messages_and_survey_data(self) -> None:
        self._login()
        thread = self._create_thread(phone="+17205550004")
        message = self._create_message(thread=thread, body="Hello", direction="inbound")
        _survey, session, response = self._create_survey_session(
            thread=thread,
            name="Cleanup Survey",
            keyword="CLEANUP",
        )
        self.db.session.commit()

        response_http = self.client.post(
            f"/inbox/threads/{thread.id}/delete",
            data={"search": ""},
            follow_redirects=False,
        )
        self.assertEqual(response_http.status_code, 302)
        self.assertIsNone(self.db.session.get(self.InboxThread, thread.id))
        self.assertIsNone(self.db.session.get(self.InboxMessage, message.id))
        self.assertIsNone(self.db.session.get(self.SurveySession, session.id))
        self.assertIsNone(self.db.session.get(self.SurveyResponse, response.id))

    def test_inbox_messages_bulk_delete_scopes_to_selected_thread(self) -> None:
        self._login()
        thread_one = self._create_thread(phone="+17205550007")
        thread_two = self._create_thread(phone="+17205550008")
        now = datetime.now(timezone.utc)
        message_one = self._create_message(
            thread=thread_one,
            body="Delete me 1",
            direction="inbound",
            created_at=now - timedelta(minutes=4),
        )
        message_two = self._create_message(
            thread=thread_one,
            body="Delete me 2",
            direction="outbound",
            created_at=now - timedelta(minutes=3),
        )
        surviving_in_thread = self._create_message(
            thread=thread_one,
            body="Keep me",
            direction="outbound",
            created_at=now - timedelta(minutes=1),
        )
        other_thread_message = self._create_message(
            thread=thread_two,
            body="Other thread",
            direction="inbound",
            created_at=now - timedelta(minutes=2),
        )
        thread_one.last_message_at = surviving_in_thread.created_at
        thread_one.last_message_preview = surviving_in_thread.body
        thread_one.last_direction = surviving_in_thread.direction
        self.db.session.commit()

        response = self.client.post(
            "/inbox/messages/bulk-delete",
            data={
                "thread_id": str(thread_one.id),
                "message_ids": [str(message_one.id), str(message_two.id), str(other_thread_message.id)],
                "search": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.db.session.get(self.InboxMessage, message_one.id))
        self.assertIsNone(self.db.session.get(self.InboxMessage, message_two.id))
        self.assertIsNotNone(self.db.session.get(self.InboxMessage, surviving_in_thread.id))
        self.assertIsNotNone(self.db.session.get(self.InboxMessage, other_thread_message.id))

        updated_thread = self.db.session.get(self.InboxThread, thread_one.id)
        self.assertEqual(updated_thread.last_message_preview, "Keep me")
        self.assertEqual(updated_thread.last_direction, "outbound")

    def test_inbox_messages_bulk_delete_no_selection_shows_warning(self) -> None:
        self._login()
        thread = self._create_thread(phone="+17205550009")
        self.db.session.commit()

        response_messages = self.client.post(
            "/inbox/messages/bulk-delete",
            data={"thread_id": str(thread.id), "search": ""},
            follow_redirects=True,
        )
        self.assertEqual(response_messages.status_code, 200)
        self.assertIn(b"No messages selected.", response_messages.data)

    def test_removed_inbox_delete_routes_return_404(self) -> None:
        self._login()
        response_threads = self.client.post("/inbox/threads/bulk-delete", follow_redirects=False)
        self.assertEqual(response_threads.status_code, 404)
        response_message = self.client.post("/inbox/messages/1/delete", follow_redirects=False)
        self.assertEqual(response_message.status_code, 404)

    def test_social_manager_can_update_and_delete_inbox_records(self) -> None:
        thread = self._create_thread(phone="+17205550010")
        message = self._create_message(thread=thread, body="Delete me", direction="inbound")
        second_thread = self._create_thread(phone="+17205550011")
        self.db.session.commit()

        self._login_as("social", "social-pass")

        response_update = self.client.post(
            f"/inbox/threads/{thread.id}/update",
            data={"contact_name": "Social Updated", "search": ""},
            follow_redirects=False,
        )
        self.assertEqual(response_update.status_code, 302)
        updated = self.db.session.get(self.InboxThread, thread.id)
        self.assertEqual(updated.contact_name, "Social Updated")

        response_message_bulk_delete = self.client.post(
            "/inbox/messages/bulk-delete",
            data={"thread_id": str(thread.id), "message_ids": [str(message.id)], "search": ""},
            follow_redirects=False,
        )
        self.assertEqual(response_message_bulk_delete.status_code, 302)
        self.assertIsNone(self.db.session.get(self.InboxMessage, message.id))

        response_thread_delete = self.client.post(
            f"/inbox/threads/{second_thread.id}/delete",
            data={"search": ""},
            follow_redirects=False,
        )
        self.assertEqual(response_thread_delete.status_code, 302)
        self.assertIsNone(self.db.session.get(self.InboxThread, second_thread.id))

    def test_non_privileged_user_cannot_mutate_inbox(self) -> None:
        thread = self._create_thread(phone="+17205550012")
        message = self._create_message(thread=thread, body="viewer", direction="inbound")
        self.db.session.commit()

        self._login_as("viewer", "viewer-pass")

        update_response = self.client.post(
            f"/inbox/threads/{thread.id}/update",
            data={"contact_name": "Blocked"},
            follow_redirects=False,
        )
        self.assertEqual(update_response.status_code, 403)

        delete_response = self.client.post(
            "/inbox/messages/bulk-delete",
            data={"thread_id": str(thread.id), "message_ids": [str(message.id)]},
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 403)

    def test_inbox_ui_has_single_delete_model(self) -> None:
        self._login()
        thread = self._create_thread(phone="+17205550014")
        message = self._create_message(thread=thread, body="Visible", direction="inbound")
        self.db.session.commit()

        response = self.client.get(f"/inbox?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count("Delete Thread"), 1)
        self.assertNotIn("Delete Selected", html)
        self.assertIn("Delete selected messages", html)
        self.assertIn('id="bulkMessageDeleteButton"', html)
        self.assertIn('aria-label="Delete selected messages" disabled', html)
        self.assertNotIn("selectAllThreads", html)
        self.assertNotIn("bulkThreadDeleteForm", html)
        self.assertNotIn(f"/inbox/messages/{message.id}/delete", html)

    def test_inbox_mutation_requires_login(self) -> None:
        thread = self._create_thread(phone="+17205550013")
        self.db.session.commit()
        self._logout()

        response = self.client.post(
            f"/inbox/threads/{thread.id}/update",
            data={"contact_name": "No Login"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
