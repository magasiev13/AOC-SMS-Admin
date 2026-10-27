"""Artifact round trips use synthetic encrypted bytes, never real backups."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from deploy.verify_backup_export import verify_export


@pytest.fixture
def copies(tmp_path):
    exported = tmp_path / "exported"
    original = exported / "twinevia-backup-export-123"
    original.mkdir(parents=True)
    encrypted = original / "fixture.enc"
    encrypted.write_bytes(b"synthetic encrypted backup fixture")
    sha = hashlib.sha256(encrypted.read_bytes()).hexdigest()
    (original / "fixture.hmac").write_text("a" * 64 + "\n")
    application = {"release_id": "hotfix-fixture", "source_sha": "b" * 40, "target_source_sha": "c" * 40}
    (original / "backup-status.json").write_text(json.dumps({"encrypted_sha256": sha, "application": application}))
    (original / "artifact-manifest.json").write_text(json.dumps({"archive": "fixture.enc", "hmac": "fixture.hmac", "encrypted_sha256": sha, "application": application}))
    downloaded = tmp_path / "downloaded"
    shutil.copytree(exported, downloaded)
    return exported, downloaded, downloaded / original.name, sha


def test_roundtrip_proves_exact_backup_and_runtime_identity(copies):
    exported, downloaded, _, sha = copies
    assert verify_export(exported, downloaded) == sha


@pytest.mark.parametrize("name", ["fixture.enc", "fixture.hmac", "backup-status.json", "artifact-manifest.json"])
def test_changed_download_cannot_be_confirmed(copies, name):
    exported, downloaded, directory, _ = copies
    path = directory / name
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        verify_export(exported, downloaded)


def test_self_consistent_replaced_download_cannot_claim_original_identity(copies):
    exported, downloaded, directory, _ = copies
    path = directory / "fixture.enc"
    path.write_bytes(b"different synthetic encrypted backup")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("artifact-manifest.json", "backup-status.json"):
        path = directory / name
        payload = json.loads(path.read_text())
        payload["encrypted_sha256"] = sha
        path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="differs from the original"):
        verify_export(exported, downloaded)


def test_artifact_path_cannot_escape_its_directory(copies):
    exported, downloaded, directory, _ = copies
    path = directory / "artifact-manifest.json"
    payload = json.loads(path.read_text())
    payload["archive"] = "../fixture.enc"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="plain filenames"):
        verify_export(exported, downloaded)


def test_duplicate_backup_manifests_fail_closed(copies):
    exported, downloaded, directory, _ = copies
    shutil.copytree(directory, downloaded / "another-export")
    with pytest.raises(ValueError, match="exactly one"):
        verify_export(exported, downloaded)
