"""Read-only source identity checks and exact application backup archives.

This module deliberately does not import the application or load its environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Any


EXCLUDED_ROOTS = {"venv", ".git", "releases", "current", "previous", "instance", "node_modules", "output", ".env"}
RUNTIME_PREFIXES = ("app/", "deploy/", "run/", "tests/")
RUNTIME_FILES = {"requirements.txt", "wsgi.py"}


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected an object in {path.name}.")
    return payload


def resolve_application_root(app_root: Path) -> Path:
    """A deploy checkout is not the application when it has a current release."""
    current = app_root / "current"
    selected = current if current.exists() or current.is_symlink() else app_root
    resolved = selected.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Application source root must be a directory.")
    return resolved


def release_identity(release_root: Path) -> dict[str, str | None]:
    metadata_path = release_root / "release.json"
    if metadata_path.is_file():
        metadata = _json(metadata_path)
        source_sha = str(metadata.get("source_sha") or "")
        release_id = str(metadata.get("release_id") or release_root.name)
    else:
        # Existing recoverable bootstrap releases predate release.json.
        values = {}
        env_path = release_root / ".release.env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"APP_RELEASE_SHA", "APP_RELEASE_ID"}:
                    values[key] = value
        source_sha = values.get("APP_RELEASE_SHA", "")
        release_id = values.get("APP_RELEASE_ID", release_root.name)
    if source_sha and not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Active release declares an invalid source SHA.")
    return {"release_id": release_id, "source_sha": source_sha or None}


def _entry(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {"type": "symlink", "target": os.readlink(path)}
    if path.is_file():
        return {"type": "file", "sha256": _digest(path.read_bytes()), "mode": path.stat().st_mode & 0o777}
    raise ValueError(f"Unsupported source entry: {path.name}")


def application_manifest(release_root: Path) -> dict[str, dict[str, Any]]:
    entries = {}
    for directory, subdirectories, files in os.walk(release_root, followlinks=False):
        relative_directory = Path(directory).relative_to(release_root)
        if relative_directory == Path("."):
            subdirectories[:] = [name for name in subdirectories if name not in EXCLUDED_ROOTS]
            files = [name for name in files if name not in EXCLUDED_ROOTS]
        for name in sorted(list(subdirectories)):
            path = Path(directory) / name
            if path.is_symlink():
                entries[path.relative_to(release_root).as_posix()] = _entry(path)
                subdirectories.remove(name)
        for name in sorted(files):
            path = Path(directory) / name
            entries[path.relative_to(release_root).as_posix()] = _entry(path)
    return dict(sorted(entries.items()))


def manifest_fingerprint(entries: dict[str, dict[str, Any]]) -> str:
    return _digest(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def verify_application_archive(archive_path: Path, manifest_path: Path) -> None:
    expected = _json(manifest_path)
    actual = {}
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.name in actual:
                raise ValueError("Application archive contains an unsafe or duplicate path.")
            if member.isfile():
                handle = archive.extractfile(member)
                if handle is None:
                    raise ValueError("Application archive file is unreadable.")
                actual[member.name] = {"type": "file", "sha256": _digest(handle.read()), "mode": member.mode & 0o777}
            elif member.issym():
                actual[member.name] = {"type": "symlink", "target": member.linkname}
            else:
                raise ValueError("Application archive contains an unexpected entry type.")
    if actual != expected:
        raise ValueError("Application archive does not match the captured running-source manifest.")


def archive_application(app_root: Path, archive_path: Path, manifest_path: Path, target_source_sha: str | None = None) -> dict[str, Any]:
    target_source_sha = target_source_sha or None
    if target_source_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", target_source_sha):
        raise ValueError("Backup target source SHA is invalid.")
    release_root = resolve_application_root(app_root)
    entries = application_manifest(release_root)
    manifest_path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with tarfile.open(archive_path, "w", dereference=False) as archive:
        for name in entries:
            path = release_root / name
            info = archive.gettarinfo(str(path), arcname=name)
            if entries[name]["type"] == "file":
                # A source hard link is archived as a complete file too.
                info.type, info.linkname = tarfile.REGTYPE, ""
                info.size = path.stat().st_size
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            else:
                archive.addfile(info)
    verify_application_archive(archive_path, manifest_path)
    return {
        **release_identity(release_root),
        "root": str(release_root),
        "content_sha256": manifest_fingerprint(entries),
        "file_count": len(entries),
        "target_source_sha": target_source_sha,
    }


def verify_backup_identity(app_root: Path, status_path: Path, target_source_sha: str) -> None:
    release_root = resolve_application_root(app_root)
    status = _json(status_path)
    captured = status.get("application")
    if not isinstance(captured, dict):
        raise ValueError("Pre-deployment backup lacks running-application identity proof.")
    expected = {**release_identity(release_root), "content_sha256": manifest_fingerprint(application_manifest(release_root))}
    if any(captured.get(key) != value for key, value in expected.items()):
        raise ValueError("Pre-deployment backup does not match the current running application.")
    if captured.get("target_source_sha") != target_source_sha:
        raise ValueError("Pre-deployment backup belongs to a different target source SHA.")


def preservation_report(source_root: Path, current_root: Path, candidate_root: Path) -> dict[str, Any]:
    active = resolve_application_root(current_root)
    identity = release_identity(active)
    source_sha = identity["source_sha"]
    if not source_sha:
        raise ValueError("Active release lacks a declared source SHA; reconcile it before release.")
    git = ["git", "-c", f"safe.directory={source_root.resolve()}", "-C", str(source_root)]
    tree = subprocess.check_output([*git, "ls-tree", "-r", "-z", source_sha])
    base_objects = {}
    def is_runtime_path(name: str) -> bool:
        return (name.startswith(RUNTIME_PREFIXES) or name in RUNTIME_FILES) and "__pycache__" not in Path(name).parts and not name.endswith((".pyc", ".pyo"))
    for raw_entry in tree.split(b"\0"):
        if raw_entry:
            metadata, raw_name = raw_entry.split(b"\t", 1)
            _mode, kind, object_id = metadata.decode("ascii").split()
            name = raw_name.decode("utf-8")
            if kind == "blob" and is_runtime_path(name):
                base_objects[name] = object_id
    active_names = {name for name in application_manifest(active) if is_runtime_path(name)}
    overrides = []
    for name in sorted(set(base_objects) | active_names):
        base = subprocess.check_output([*git, "cat-file", "blob", base_objects[name]]) if name in base_objects else None
        active_path = active / name
        candidate_path = candidate_root / name
        def content(path: Path) -> bytes | None:
            if path.is_symlink():
                return os.readlink(path).encode("utf-8")
            return path.read_bytes() if path.is_file() else None
        current = content(active_path)
        if current == base:
            continue
        candidate = content(candidate_path)
        disposition = "carried_forward" if candidate == current else "changed_in_candidate"
        if candidate is None and current is not None:
            disposition = "missing_in_candidate"
        elif candidate == base:
            disposition = "would_revert_live_override"
        overrides.append({"path": name, "base_sha256": _digest(base) if base is not None else None, "active_sha256": _digest(current) if current is not None else None,
                          "candidate_sha256": _digest(candidate) if candidate is not None else None, "disposition": disposition})
    blockers = [row["path"] for row in overrides if row["disposition"] in {"would_revert_live_override", "missing_in_candidate"}]
    return {"active_release": identity, "active_root": str(active), "overrides": overrides, "blockers": blockers, "ready": not blockers}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive")
    archive.add_argument("--app-root", required=True, type=Path)
    archive.add_argument("--archive", required=True, type=Path)
    archive.add_argument("--manifest", required=True, type=Path)
    archive.add_argument("--metadata", required=True, type=Path)
    archive.add_argument("--target-source-sha")
    verify = commands.add_parser("verify-archive")
    verify.add_argument("--archive", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    backup = commands.add_parser("verify-backup")
    backup.add_argument("--app-root", required=True, type=Path)
    backup.add_argument("--status", required=True, type=Path)
    backup.add_argument("--target-source-sha", required=True)
    preserve = commands.add_parser("preservation-check")
    preserve.add_argument("--source-root", required=True, type=Path)
    preserve.add_argument("--current-root", required=True, type=Path)
    preserve.add_argument("--candidate-root", required=True, type=Path)
    preserve.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "archive":
            result = archive_application(args.app_root, args.archive, args.manifest, args.target_source_sha)
            args.metadata.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        elif args.command == "verify-archive":
            verify_application_archive(args.archive, args.manifest)
        elif args.command == "verify-backup":
            verify_backup_identity(args.app_root, args.status, args.target_source_sha)
        else:
            result = preservation_report(args.source_root, args.current_root, args.candidate_root)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if not result["ready"]:
                raise ValueError("Candidate would discard active release overrides: " + ", ".join(result["blockers"]))
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
