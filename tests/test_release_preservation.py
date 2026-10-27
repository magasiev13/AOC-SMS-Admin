"""Local source/archive fixtures; never connects to databases or production."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from deploy.release_source import (
    archive_application,
    preservation_report,
    resolve_application_root,
    verify_application_archive,
    verify_backup_identity,
)


class TestReleasePreservation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="twinevia-release-test-")
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        (self.checkout / "app").mkdir(parents=True)
        (self.checkout / "app/worker.py").write_text("base behavior\n", encoding="utf-8")
        (self.checkout / "app/base.py").write_text("unchanged\n", encoding="utf-8")
        (self.checkout / "requirements.txt").write_text("Werkzeug==3.1.8\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "user.name", "Isolated fixture")
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture base")
        self.base_sha = self.git("rev-parse", "HEAD").strip()
        self.active = self.root / "releases/hotfix"
        self.candidate = self.root / "candidate"
        for target in (self.active, self.candidate):
            shutil.copytree(self.checkout, target, ignore=shutil.ignore_patterns(".git"))
        (self.active / "app/worker.py").write_text("live hotfix\n", encoding="utf-8")
        (self.active / "release.json").write_text(json.dumps({"release_id": "active-hotfix", "source_sha": self.base_sha}), encoding="utf-8")
        (self.checkout / "current").symlink_to(self.active, target_is_directory=True)

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.checkout), *arguments], text=True)

    def archive(self):
        archive = self.root / "application.tar"
        manifest = self.root / "application.manifest.json"
        identity = archive_application(self.checkout, archive, manifest, "a" * 40)
        return archive, manifest, identity

    def test_candidate_from_clean_main_refuses_to_discard_active_hotfix(self):
        report = preservation_report(self.checkout, self.checkout, self.candidate)
        self.assertFalse(report["ready"])
        self.assertEqual(report["blockers"], ["app/worker.py"])
        self.assertEqual(report["overrides"][0]["disposition"], "would_revert_live_override")

    def test_carried_and_further_changed_hotfix_are_reported_for_review(self):
        for content, disposition in (("live hotfix\n", "carried_forward"), ("live hotfix\nnew fix\n", "changed_in_candidate")):
            with self.subTest(disposition=disposition):
                (self.candidate / "app/worker.py").write_text(content, encoding="utf-8")
                report = preservation_report(self.checkout, self.active, self.candidate)
                self.assertTrue(report["ready"])
                self.assertEqual(report["overrides"][0]["disposition"], disposition)

    def test_missing_new_live_module_is_not_silently_dropped(self):
        (self.candidate / "app/worker.py").write_text("live hotfix\n", encoding="utf-8")
        (self.active / "app/new_hotfix.py").write_text("new live module\n", encoding="utf-8")
        report = preservation_report(self.checkout, self.active, self.candidate)
        self.assertFalse(report["ready"])
        self.assertEqual(report["blockers"], ["app/new_hotfix.py"])

    def test_missing_active_identity_refuses_preservation_check(self):
        (self.active / "release.json").unlink()
        with self.assertRaisesRegex(ValueError, "lacks a declared source SHA"):
            preservation_report(self.checkout, self.active, self.candidate)

    def test_existing_bootstrap_release_identity_is_supported(self):
        (self.active / "release.json").unlink()
        (self.active / ".release.env").write_text(f"APP_RELEASE_ID=bootstrap\nAPP_RELEASE_SHA={self.base_sha}\n", encoding="utf-8")
        (self.candidate / "app/worker.py").write_text("live hotfix\n", encoding="utf-8")
        report = preservation_report(self.checkout, self.active, self.candidate)
        self.assertTrue(report["ready"])
        self.assertEqual(report["active_release"]["release_id"], "bootstrap")

    def test_dangling_current_does_not_fall_back_to_checkout(self):
        (self.checkout / "current").unlink()
        (self.checkout / "current").symlink_to(self.root / "missing")
        with self.assertRaises(FileNotFoundError):
            resolve_application_root(self.checkout)

    def test_archive_reproduces_current_and_excludes_environment_dependencies(self):
        (self.active / ".env").write_text("PRIVATE=synthetic-fixture-only\n", encoding="utf-8")
        (self.active / "venv").mkdir()
        (self.active / "venv/runtime").write_text("not source", encoding="utf-8")
        (self.active / "link.py").symlink_to("app/worker.py")
        os.link(self.active / "app/base.py", self.active / "app/hardlink.py")
        archive, manifest, identity = self.archive()
        with tarfile.open(archive) as handle:
            self.assertEqual(handle.extractfile("app/worker.py").read(), b"live hotfix\n")
            self.assertEqual(handle.extractfile("app/hardlink.py").read(), b"unchanged\n")
            self.assertTrue(handle.getmember("link.py").issym())
            self.assertNotIn(".env", handle.getnames())
            self.assertFalse(any(name.startswith("venv/") for name in handle.getnames()))
        verify_application_archive(archive, manifest)
        self.assertEqual(identity["release_id"], "active-hotfix")
        self.assertEqual(identity["source_sha"], self.base_sha)
        self.assertEqual(identity["target_source_sha"], "a" * 40)
        self.assertEqual(identity["root"], str(self.active.resolve()))

    def test_archive_changed_content_is_rejected_against_captured_manifest(self):
        archive, manifest, _ = self.archive()
        with tarfile.open(archive) as handle:
            entries = [(member, handle.extractfile(member).read() if member.isfile() else None) for member in handle.getmembers()]
        with tarfile.open(archive, "w") as handle:
            for member, content in entries:
                if member.name == "app/worker.py":
                    content = b"wrong checkout content\n"
                    member.size = len(content)
                handle.addfile(member, io.BytesIO(content) if content is not None else None)
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_application_archive(archive, manifest)

    def test_predeploy_backup_must_match_runtime_and_target_source(self):
        _archive, _manifest, identity = self.archive()
        status = self.root / "status.json"
        status.write_text(json.dumps({"application": identity}), encoding="utf-8")
        verify_backup_identity(self.checkout, status, "a" * 40)
        with self.assertRaisesRegex(ValueError, "different target"):
            verify_backup_identity(self.checkout, status, "b" * 40)
        (self.active / "app/worker.py").write_text("changed after backup\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_backup_identity(self.checkout, status, "a" * 40)

    def test_old_status_cannot_claim_new_predeployment_identity_proof(self):
        status = self.root / "status.json"
        status.write_text(json.dumps({"release_id": "checkout-main"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "lacks running-application identity"):
            verify_backup_identity(self.checkout, status, "a" * 40)


if __name__ == "__main__":
    unittest.main()
