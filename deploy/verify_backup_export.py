"""Verify an encrypted backup downloaded from artifact storage against its export.

This checks the stored bytes without decrypting or disclosing customer data.
The restore drill separately authenticates and restores the encrypted archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True, type=Path)
    parser.add_argument("--downloaded", required=True, type=Path)
    args = parser.parse_args()
    try:
        archive_hash = verify_export(args.expected, args.downloaded)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Backup download verification failed: {exc}\n")
    print(f"Downloaded encrypted backup verified: SHA-256 {archive_hash}")


if __name__ == "__main__":
    main()
