import importlib
import os
import tempfile
import unittest


class TestModelPhoneValidation(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import CommunityMember, Event, EventRegistration, UnsubscribedContact

        self.db = db
        self.CommunityMember = CommunityMember
        self.Event = Event
        self.EventRegistration = EventRegistration
        self.UnsubscribedContact = UnsubscribedContact
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()

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

    def test_invalid_phones_are_rejected_by_models(self) -> None:
        with self.assertRaises(ValueError):
            self.CommunityMember(name="Bad Community", phone="foo")

        event = self.Event(title="Phone Validation Event")
        self.db.session.add(event)
        self.db.session.commit()

        with self.assertRaises(ValueError):
            self.EventRegistration(event_id=event.id, name="Bad Event", phone="bar")

        with self.assertRaises(ValueError):
            self.UnsubscribedContact(name="Bad Unsubscribed", phone="baz", source="test")

    def test_valid_phones_are_normalized_before_persist(self) -> None:
        member = self.CommunityMember(name="Good Community", phone="720-555-0401")
        unsubscribed = self.UnsubscribedContact(name="Good Unsubscribed", phone="(720) 555-0402", source="test")
        event = self.Event(title="Normalization Event")
        self.db.session.add_all([member, unsubscribed, event])
        self.db.session.flush()
        registration = self.EventRegistration(event_id=event.id, name="Good Event", phone="720.555.0403")
        self.db.session.add(registration)
        self.db.session.commit()

        self.assertEqual(member.phone, "+17205550401")
        self.assertEqual(unsubscribed.phone, "+17205550402")
        self.assertEqual(registration.phone, "+17205550403")


if __name__ == "__main__":
    unittest.main()
