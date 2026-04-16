import importlib
import os
import tempfile
import unittest


class TestTestRecipientRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = {
            "FLASK_DEBUG": os.environ.get("FLASK_DEBUG"),
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "SAAS_MODE": os.environ.get("SAAS_MODE"),
        }
        os.environ["FLASK_DEBUG"] = "1"
        os.environ["SAAS_MODE"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import (
            AppUser,
            Organization,
            OrganizationMembership,
            OrganizationMessagingProfile,
            OrganizationSettingsAuditLog,
            OrganizationSubscription,
            OrganizationTestRecipient,
        )

        self.db = db
        self.AppUser = AppUser
        self.Organization = Organization
        self.OrganizationMembership = OrganizationMembership
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.OrganizationSettingsAuditLog = OrganizationSettingsAuditLog
        self.OrganizationSubscription = OrganizationSubscription
        self.OrganizationTestRecipient = OrganizationTestRecipient

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        self.organization = self.Organization(name="Acme", slug="acme", status="active")
        self.subscription = self.OrganizationSubscription(
            organization=self.organization,
            stripe_price_id="price_test_123",
            status="complimentary",
        )
        self.messaging_profile = self.OrganizationMessagingProfile(
            organization=self.organization,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACsub_acme",
            messaging_service_sid="MGacme0001",
            phone_number_sid="PNacme0001",
            from_number="+15550009999",
            inbound_identity="+15550009999",
            status="active",
            provider_status="active",
            sender_review_status="approved",
        )
        self.owner = self.AppUser(
            username="owner",
            email="owner@acme.test",
            full_name="Owner User",
            phone="+15550000001",
            role="admin",
            must_change_password=False,
        )
        self.owner.set_password("Owner-pass1!")
        self.staff = self.AppUser(
            username="staff",
            email="staff@acme.test",
            full_name="Staff User",
            phone="+15550000002",
            role="social_manager",
            must_change_password=False,
        )
        self.staff.set_password("Staff-pass1!")
        self.db.session.add_all([
            self.organization,
            self.subscription,
            self.messaging_profile,
            self.owner,
            self.staff,
        ])
        self.db.session.flush()
        self.db.session.add_all([
            self.OrganizationMembership(
                organization_id=self.organization.id,
                user_id=self.owner.id,
                role="owner",
            ),
            self.OrganizationMembership(
                organization_id=self.organization.id,
                user_id=self.staff.id,
                role="staff",
            ),
        ])
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._ctx.pop()
        self._temp_dir.cleanup()
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _login(self, username: str, password: str) -> None:
        response = self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_owner_can_edit_test_recipients_page(self) -> None:
        self._login("owner@acme.test", "Owner-pass1!")

        response = self.client.post(
            "/settings/test-recipients",
            data={
                "recipient_label[]": ["Board Chair", "Ops"],
                "recipient_phone[]": ["+15550001111", "+15550002222"],
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        rows = self.OrganizationTestRecipient.query.filter_by(
            organization_id=self.organization.id
        ).order_by(self.OrganizationTestRecipient.phone.asc()).all()
        self.assertEqual([row.phone for row in rows], ["+15550001111", "+15550002222"])
        self.assertEqual(self.OrganizationSettingsAuditLog.query.count(), 1)
        self.assertIn("Internal test recipients updated.", response.get_data(as_text=True))

    def test_staff_cannot_access_test_recipients_page(self) -> None:
        self._login("staff@acme.test", "Staff-pass1!")

        response = self.client.get("/settings/test-recipients", follow_redirects=False)

        self.assertEqual(response.status_code, 403)

    def test_dashboard_shows_owner_manage_link_and_staff_read_only_count(self) -> None:
        self.db.session.add(
            self.OrganizationTestRecipient(
                organization_id=self.organization.id,
                phone="+15550001111",
                label="Board Chair",
            )
        )
        self.db.session.commit()

        self._login("owner@acme.test", "Owner-pass1!")
        owner_response = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(owner_response.status_code, 200)
        self.assertIn("Manage test recipients", owner_response.get_data(as_text=True))

        self.client.post("/logout", follow_redirects=False)
        self._login("staff@acme.test", "Staff-pass1!")
        staff_response = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(staff_response.status_code, 200)
        self.assertIn("1 saved test recipient", staff_response.get_data(as_text=True))
        self.assertNotIn("Manage test recipients", staff_response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
