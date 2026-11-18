"""Verify an encrypted backup downloaded from artifact storage against its export.

This checks the stored bytes without decrypting or disclosing customer data.
The restore drill separately authenticates and restores the encrypted archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def export_files(root: Path) -> tuple[dict[str, Path], dict]:
    manifests = list(root.rglob("artifact-manifest.json"))
    if len(manifests) != 1:
        raise ValueError("Expected exactly one backup artifact manifest.")
    manifest = manifests[0]
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    names = [metadata.get("archive"), metadata.get("hmac"), "backup-status.json", "artifact-manifest.json"]
    if any(not isinstance(name, str) or not name or Path(name).name != name for name in names):
        raise ValueError("Backup artifact names must be plain filenames.")
    if len(set(names)) != 4:
        raise ValueError("Backup artifact entries must be distinct.")
    files = {name: manifest.parent / name for name in names}
    if any(path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()) for path in files.values()):
        raise ValueError("Backup artifact has a missing or unsafe entry.")
    if not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("encrypted_sha256", ""))):
        raise ValueError("Backup artifact has no valid encrypted archive hash.")
    if digest(files[metadata["archive"]]) != metadata["encrypted_sha256"]:
        raise ValueError("Encrypted archive does not match its manifest.")
    if not re.fullmatch(r"[0-9a-f]{64}\n?", files[metadata["hmac"]].read_text(encoding="ascii")):
        raise ValueError("Backup authentication sidecar is malformed.")
    status = json.loads(files["backup-status.json"].read_text(encoding="utf-8"))
    if status.get("encrypted_sha256") != metadata["encrypted_sha256"] or status.get("application") != metadata.get("application"):
        raise ValueError("Backup status and artifact identity differ.")
    return files, metadata


def verify_export(expected_root: Path, downloaded_root: Path) -> str:
    expected, metadata = export_files(expected_root)
    downloaded, _ = export_files(downloaded_root)
    if expected.keys() != downloaded.keys() or any(digest(path) != digest(downloaded[name]) for name, path in expected.items()):
        raise ValueError("Downloaded backup differs from the original encrypted export.")
    return metadata["encrypted_sha256"]


def verify_zip(expected_root: Path, archive: Path, zip_sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", zip_sha256) or digest(archive) != zip_sha256:
        raise ValueError("Stored ZIP differs from the upload digest.")
    expected, metadata = export_files(expected_root)
    expected_paths = {path.relative_to(expected_root).as_posix(): path for path in expected.values()}
    with ZipFile(archive) as downloaded:
        entries = [entry for entry in downloaded.infolist() if not entry.is_dir()]
        if len(entries) != len(expected_paths) or {entry.filename for entry in entries} != expected_paths.keys():
            raise ValueError("Stored ZIP entries differ from the original export.")
        for entry in entries:
            original = expected_paths[entry.filename]
            if entry.file_size != original.stat().st_size:
                raise ValueError("Stored backup size differs from the original export.")
            with downloaded.open(entry) as handle:
                stored_hash = hashlib.file_digest(handle, "sha256").hexdigest()
            if stored_hash != digest(original):
                raise ValueError("Stored backup bytes differ from the original export.")
    return metadata["encrypted_sha256"]


def download_github_backup(expected: Path, artifact_id: str, zip_sha256: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", artifact_id):
        raise ValueError("Expected the immutable numeric upload artifact ID.")
    api_url = os.environ["GITHUB_API_URL"]
    repository = os.environ["GITHUB_REPOSITORY"]
    with TemporaryDirectory() as temporary:
        archive = Path(temporary) / "artifact.zip"
        # curl strips Authorization on the redirect to Actions blob storage. It
        # truncates the output file before retrying a short or failed transfer.
        subprocess.run([
            "curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
            "--connect-timeout", "10", "--max-time", "45", "--retry", "5", "--retry-all-errors",
            "--retry-delay", "3", "--retry-max-time", "90", "--header", "@-",
            "--output", str(archive), f"{api_url}/repos/{repository}/actions/artifacts/{artifact_id}/zip",
        ], input=f"Authorization: Bearer {os.environ['GH_TOKEN']}\n", text=True, check=True)
        return verify_zip(expected, archive, zip_sha256)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--downloaded", type=Path)
    source.add_argument("--github-artifact-id")
    parser.add_argument("--zip-sha256")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        if args.github_artifact_id is not None:
            archive_hash = download_github_backup(args.expected, args.github_artifact_id, args.zip_sha256 or "")
        else:
            archive_hash = verify_export(args.expected, args.downloaded)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                print(f"encrypted_sha256={archive_hash}", file=output)
    except (OSError, ValueError, KeyError, BadZipFile, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Backup download verification failed: {exc}\n")
    print(f"Downloaded encrypted backup verified: SHA-256 {archive_hash}")


if __name__ == "__main__":
    main()
