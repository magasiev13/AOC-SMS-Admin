import os
import tempfile
import unittest


class TestUserCreationMustChangePassword(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
        import importlib
        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser

        self.db = db
        self.AppUser = AppUser
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        admin = self.AppUser(
            username="admin",
            phone="+15550000009",
            role="admin",
            must_change_password=False,
        )
        admin.set_password("admin-pass")
        self.db.session.add(admin)
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
        return self.client.post(
            "/login",
            data={"username": "admin", "password": "admin-pass"},
            follow_redirects=False,
        )

    def test_unchecked_must_change_password_creates_user_without_flag(self) -> None:
        self._login()
        response = self.client.post(
            "/users/add",
            data={
                "username": "new-user",
                "role": "social_manager",
                "phone": "+15551110001",
                "password": "Stronger-pass1!",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        user = self.AppUser.query.filter_by(username="new-user").first()
        self.assertIsNotNone(user)
        self.assertFalse(user.must_change_password)

    def test_checked_must_change_password_creates_user_with_flag(self) -> None:
        self._login()
        response = self.client.post(
            "/users/add",
            data={
                "username": "new-user-2",
                "role": "social_manager",
                "phone": "+15551110002",
                "password": "Stronger-pass1!",
                "must_change_password": "on",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        user = self.AppUser.query.filter_by(username="new-user-2").first()
        self.assertIsNotNone(user)
        self.assertTrue(user.must_change_password)

    def test_user_add_rejects_case_variant_duplicate_username(self) -> None:
        existing = self.AppUser(
            username="CaseUser",
            phone="+15551110010",
            role="social_manager",
            must_change_password=False,
        )
        existing.set_password("Stronger-pass1!")
        self.db.session.add(existing)
        self.db.session.commit()

        self._login()
        response = self.client.post(
            "/users/add",
            data={
                "username": "caseuser",
                "role": "social_manager",
                "phone": "+15551110011",
                "password": "Stronger-pass1!",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A user with this username already exists.", response.data)

        usernames = [user.username.lower() for user in self.AppUser.query.all()]
        self.assertEqual(usernames.count("caseuser"), 1)

    def test_user_edit_rejects_case_variant_duplicate_username(self) -> None:
        first = self.AppUser(
            username="Alpha",
            phone="+15551110020",
            role="social_manager",
            must_change_password=False,
        )
        first.set_password("Stronger-pass1!")
        second = self.AppUser(
            username="Bravo",
            phone="+15551110021",
            role="social_manager",
            must_change_password=False,
        )
        second.set_password("Stronger-pass1!")
        self.db.session.add_all([first, second])
        self.db.session.commit()

        self._login()
        response = self.client.post(
            f"/users/{second.id}/edit",
            data={
                "username": "alpha",
                "role": "social_manager",
                "phone": second.phone,
                "password": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A user with this username already exists.", response.data)

        self.db.session.refresh(second)
        self.assertEqual(second.username, "Bravo")


if __name__ == "__main__":
    unittest.main()
