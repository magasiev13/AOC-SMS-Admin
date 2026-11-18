"""Expose only release static assets to the web server, never private source."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat


def expose_release_static(app_root: Path, release_root: Path) -> None:
    app_root = Path(os.path.abspath(app_root))
    release_root = Path(os.path.abspath(release_root))
    releases = app_root / "releases"
    if release_root.parent != releases:
        raise ValueError("Static release must be directly inside the application's releases directory.")
    ancestors = (app_root, releases, release_root, release_root / "app")
    static_root = release_root / "app" / "static"

    # Validate the complete public tree before changing any permissions. A
    # symlink or hard link must never grant public access to a private target.
    directories = []
    files = []
    for directory in (*ancestors, static_root):
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("Static asset paths must be real directories, without symlinks.")
    for parent, child_directories, child_files in os.walk(static_root, followlinks=False):
        directory = Path(parent)
        directories.append(directory)
        for name in child_directories:
            if not stat.S_ISDIR((directory / name).lstat().st_mode):
                raise ValueError("Static assets cannot contain symlinks or special files.")
        for name in child_files:
            path = directory / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Static assets must be regular files without symlinks or hard links.")
            files.append(path)

    for directory in ancestors:
        directory.chmod(stat.S_IMODE(directory.stat().st_mode) | stat.S_IXOTH)
    for directory in directories:
        directory.chmod(stat.S_IMODE(directory.stat().st_mode) | stat.S_IROTH | stat.S_IXOTH)
    for path in files:
        path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IROTH)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    args = parser.parse_args()
    expose_release_static(args.app_root, args.release_root)


if __name__ == "__main__":
    main()
