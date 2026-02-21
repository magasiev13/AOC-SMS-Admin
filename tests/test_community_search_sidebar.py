import importlib
import os
import tempfile
import unittest


class TestCommunitySearchSidebar(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, CommunityMember

        self.db = db
        self.AppUser = AppUser
        self.CommunityMember = CommunityMember

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
            phone="+15550000001",
            role="admin",
            must_change_password=False,
        )
        admin.set_password("admin-pass")
        self.db.session.add(admin)
        self.db.session.add(self.CommunityMember(name="Mariam Avetisyan", phone="+17202345027"))
        self.db.session.add(self.CommunityMember(name=None, phone="+17205550123"))
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
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin-pass"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_base_sidebar_search_form_posts_to_community(self) -> None:
        self._login()
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8", errors="ignore")
        self.assertIn('form class="app-search"', html)
        self.assertIn('action="/community"', html)
        self.assertIn('bi bi-search', html)
        self.assertIn('name="search"', html)
        self.assertIn('type="text"', html)
        self.assertIn('autocomplete="off"', html)

    def test_community_search_matches_name(self) -> None:
        self._login()
        response = self.client.get("/community", query_string={"search": "Mariam"})
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8", errors="ignore")
        self.assertIn("+17202345027", html)
        self.assertNotIn("No community members found", html)

    def test_community_search_matches_formatted_phone(self) -> None:
        self._login()
        response = self.client.get("/community", query_string={"search": "(720) 234-5027"})
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8", errors="ignore")
        self.assertIn("+17202345027", html)
        self.assertNotIn("No community members found", html)

    def test_community_search_matches_digits_only_phone(self) -> None:
        self._login()
        response = self.client.get("/community", query_string={"search": "7205550123"})
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8", errors="ignore")
        self.assertIn("+17205550123", html)
        self.assertNotIn("No community members found", html)


if __name__ == "__main__":
    unittest.main()
