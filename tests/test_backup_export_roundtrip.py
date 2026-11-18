"""Artifact round trips use synthetic encrypted bytes, never real backups."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from zipfile import ZipFile

import pytest

from deploy.verify_backup_export import digest, download_github_backup, verify_export, verify_zip


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


def make_zip(directory, output):
    with ZipFile(output, "w") as archive:
        for path in directory.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(directory))
    return digest(output)


def test_github_readback_uses_returned_id_and_compares_zip_and_all_four_files(copies, tmp_path, monkeypatch):
    expected, downloaded, _, sha = copies
    zip_path = tmp_path / "uploaded.zip"
    zip_sha = make_zip(downloaded, zip_path)
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "test/example")
    monkeypatch.setenv("GH_TOKEN", "fixture-only")

    def fetch(command, **kwargs):
        assert command[-1] == "https://api.github.com/repos/test/example/actions/artifacts/12345/zip"
        assert "--retry-all-errors" in command and "--retry-max-time" in command
        assert "fixture-only" not in command
        assert kwargs["input"] == "Authorization: Bearer fixture-only\n"
        shutil.copyfile(zip_path, command[command.index("--output") + 1])

    monkeypatch.setattr(subprocess, "run", fetch)
    assert download_github_backup(expected, "12345", zip_sha) == sha


def test_zip_rejects_wrong_digest_or_replaced_files(copies, tmp_path):
    expected, downloaded, files, _ = copies
    zip_path = tmp_path / "uploaded.zip"
    make_zip(downloaded, zip_path)
    with pytest.raises(ValueError, match="upload digest"):
        verify_zip(expected, zip_path, "0" * 64)
    (files / "fixture.hmac").write_text("b" * 64 + "\n")
    with pytest.raises(ValueError, match="bytes differ"):
        verify_zip(expected, zip_path, make_zip(downloaded, zip_path))
    (files / "unexpected").touch()
    with pytest.raises(ValueError, match="entries differ"):
        verify_zip(expected, zip_path, make_zip(downloaded, zip_path))


def test_only_matching_current_receipt_can_replace_active_backup(copies, tmp_path):
    _, _, files, sha = copies
    # Exercise real file reads, hashing and atomic promotion in a disposable
    # directory. Production ownership, logging and flock live on Linux.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ["chown", "logger"] + ([] if shutil.which("flock") else ["flock"]):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    status = tmp_path / "status.json"
    pending = tmp_path / "status.json.pending-123"
    original = {"offsite_verified": True, "completed_at": "2026-01-03T00:00:00+00:00"}
    status.write_text(json.dumps(original))
    candidate = {"local_path": str(files / "fixture.enc"), "local_hmac_path": str(files / "fixture.hmac"),
                 "offsite_mode": "github_actions", "offsite_verified": False,
                 "completed_at": "2026-01-02T00:00:00+00:00", "encrypted_sha256": sha}
    pending.write_text(json.dumps(candidate))
    cmd = ["bash", str(Path(__file__).resolve().parents[1] / "deploy/confirm_github_backup.sh"),
           "--status-file", str(status), "--repository", "test/example", "--run-id", "123",
           "--artifact-name", "backup-123-1", "--artifact-id", "567", "--encrypted-sha256"]
    late = subprocess.run([*cmd, sha], env=env, capture_output=True, text=True)
    assert late.returncode != 0 and "newer verified" in late.stderr
    assert json.loads(status.read_text()) == original
    candidate["completed_at"] = "2026-01-04T00:00:00+00:00"
    pending.write_text(json.dumps(candidate))
    mismatch = subprocess.run([*cmd, "0" * 64], env=env, capture_output=True, text=True)
    assert mismatch.returncode != 0 and "SHA-256" in mismatch.stderr
    assert json.loads(status.read_text()) == original
    subprocess.run([*cmd, sha], env=env, check=True)
    active = json.loads(status.read_text())
    assert active["offsite_verified"] and active["offsite_artifact_id"] == "567"
    assert active["offsite_reference"].endswith("/runs/123/artifacts/567")
    assert not pending.exists()
