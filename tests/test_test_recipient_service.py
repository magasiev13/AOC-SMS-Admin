import importlib
import os
import tempfile
import unittest


class TestTestRecipientService(unittest.TestCase):
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
            OrganizationSettingsAuditLog,
            OrganizationTestRecipient,
        )
        from app.services.test_recipient_service import (
            TEST_RECIPIENT_MAX_COUNT,
            build_test_recipient_snapshot,
            normalize_test_recipient_entries,
            replace_test_recipients,
            seed_owner_test_recipient,
            seed_test_recipients_from_owner_phones,
        )

        self.db = db
        self.AppUser = AppUser
        self.Organization = Organization
        self.OrganizationMembership = OrganizationMembership
        self.OrganizationSettingsAuditLog = OrganizationSettingsAuditLog
        self.OrganizationTestRecipient = OrganizationTestRecipient
        self.TEST_RECIPIENT_MAX_COUNT = TEST_RECIPIENT_MAX_COUNT
        self.build_test_recipient_snapshot = build_test_recipient_snapshot
        self.normalize_test_recipient_entries = normalize_test_recipient_entries
        self.replace_test_recipients = replace_test_recipients
        self.seed_owner_test_recipient = seed_owner_test_recipient
        self.seed_test_recipients_from_owner_phones = seed_test_recipients_from_owner_phones

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()

        self.organization = self.Organization(name="Acme", slug="acme", status="active")
        self.owner = self.AppUser(
            username="owner",
            email="owner@acme.test",
            full_name="Owner User",
            phone="+15550000001",
            role="admin",
            must_change_password=False,
        )
        self.owner.set_password("Owner-pass1!")
        self.db.session.add_all([self.organization, self.owner])
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=self.organization.id,
                user_id=self.owner.id,
                role="owner",
            )
        )
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

    def test_normalize_test_recipient_entries_dedupes_and_enforces_cap(self) -> None:
        entries = self.normalize_test_recipient_entries(
            [
                {"label": "Board", "phone": "(555) 000-1111"},
                {"label": "Board duplicate", "phone": "+15550001111"},
                {"label": "", "phone": ""},
            ]
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["phone"], "+15550001111")
        self.assertEqual(entries[0]["label"], "Board")

        with self.assertRaises(ValueError):
            self.normalize_test_recipient_entries(
                [
                    {"label": f"Row {index}", "phone": f"+1555000{index:04d}"}
                    for index in range(self.TEST_RECIPIENT_MAX_COUNT + 1)
                ]
            )

    def test_seed_owner_test_recipient_and_owner_backfill(self) -> None:
        self.seed_owner_test_recipient(self.organization.id, self.owner)
        self.db.session.commit()

        rows = self.OrganizationTestRecipient.query.filter_by(
            organization_id=self.organization.id
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].phone, "+15550000001")
        self.assertEqual(rows[0].label, "Owner User")

        second_owner = self.AppUser(
            username="second-owner",
            email="second-owner@acme.test",
            full_name="Second Owner",
            phone="+15550000002",
            role="admin",
            must_change_password=False,
        )
        second_owner.set_password("Second-pass1!")
        self.db.session.add(second_owner)
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=self.organization.id,
                user_id=second_owner.id,
                role="owner",
            )
        )
        self.db.session.commit()

        self.seed_test_recipients_from_owner_phones(self.organization.id)
        self.db.session.commit()

        phones = {
            row.phone
            for row in self.OrganizationTestRecipient.query.filter_by(organization_id=self.organization.id).all()
        }
        self.assertEqual(phones, {"+15550000001", "+15550000002"})

    def test_replace_test_recipients_creates_masked_audit_row(self) -> None:
        self.replace_test_recipients(
            self.organization.id,
            [
                {"label": "Board Chair", "phone": "+15550001111"},
                {"label": "Ops", "phone": "+15550002222"},
            ],
            actor_user_id=self.owner.id,
        )
        self.db.session.commit()

        audit_row = self.OrganizationSettingsAuditLog.query.one()
        metadata = audit_row.metadata_payload
        self.assertEqual(audit_row.category, "test_recipients")
        self.assertEqual(metadata["before_count"], 0)
        self.assertEqual(metadata["after_count"], 2)
        self.assertEqual(len(metadata["after_phones"]), 2)
        self.assertTrue(all("*" in value for value in metadata["after_phones"]))

    def test_build_test_recipient_snapshot_supports_one_and_all(self) -> None:
        self.replace_test_recipients(
            self.organization.id,
            [
                {"label": "Board Chair", "phone": "+15550001111"},
                {"label": "Ops", "phone": "+15550002222"},
            ],
        )
        self.db.session.commit()

        selection_mode, snapshot_json, recipients = self.build_test_recipient_snapshot(
            self.organization.id,
            selection_mode="one",
            selected_phone="+15550002222",
        )
        self.assertEqual(selection_mode, "one")
        self.assertIn("+15550002222", snapshot_json)
        self.assertEqual(recipients, [{"phone": "+15550002222", "name": "Ops"}])

        selection_mode, snapshot_json, recipients = self.build_test_recipient_snapshot(
            self.organization.id,
            selection_mode="all",
        )
        self.assertEqual(selection_mode, "all")
        self.assertIn("+15550001111", snapshot_json)
        self.assertEqual(len(recipients), 2)


if __name__ == "__main__":
    unittest.main()
