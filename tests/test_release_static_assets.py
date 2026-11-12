"""Release assets are public and versioned; private release files stay private."""
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

from flask import render_template_string, url_for
import pytest

from deploy.static_permissions import expose_release_static
from tests.test_ux_recovery import ux


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("release", ["20261111T010203Z-reviewed", ""])
def test_every_generated_static_url_uses_the_release_version(ux, monkeypatch, release):
    monkeypatch.delenv("APP_VERSION", raising=False)
    ux.app.config["APP_RELEASE_ID"] = release
    expected = release or "dev"
    with ux.app.test_request_context():
        direct = url_for("static", filename="js/app.js")
        markup = render_template_string('{{ url_for("static", filename="css/app.css", v=app_version) }}')
    assert parse_qs(urlsplit(direct).query)["v"] == [expected]
    assert parse_qs(urlsplit(markup).query)["v"] == [expected]
    page = ux.client.get("/login")
    assert page.status_code == 200
    assets = re.findall(r'(?:href|src)="(/static/[^\"]+)"', page.get_data(as_text=True))
    assert assets
    assert all(parse_qs(urlsplit(asset).query)["v"] == [expected] for asset in assets)


def _restricted_release(tmp_path):
    app_root = tmp_path / "application"
    release = app_root / "releases" / "reviewed-release"
    static_root = release / "app" / "static"
    (static_root / "css").mkdir(parents=True)
    (static_root / "css" / "app.css").write_text("body { color: black; }")
    (release / "venv").mkdir()
    private = (app_root / ".env", release / "app" / "routes.py", release / "venv" / "runtime")
    for path in private:
        path.write_text("Synthetic private fixture")
    for path in (app_root, *app_root.rglob("*")):
        path.chmod(0o750 if path.is_dir() else 0o640)
    return app_root, release, static_root, private


def _modes(root):
    return {path: stat.S_IMODE(path.lstat().st_mode) for path in (root, *root.rglob("*"))}


def test_public_static_files_and_traversal_do_not_expose_private_siblings(tmp_path):
    app_root, release, static_root, private = _restricted_release(tmp_path)
    before = _modes(app_root)
    for _ in range(2):
        expose_release_static(app_root, release)
    after = _modes(app_root)
    ancestors = {app_root, app_root / "releases", release, release / "app"}
    for path, old_mode in before.items():
        if path in ancestors:
            assert after[path] == old_mode | 0o001
            assert after[path] & 0o006 == 0
        elif path == static_root or static_root in path.parents:
            assert after[path] == old_mode | (0o005 if path.is_dir() else 0o004)
        else:
            assert after[path] == old_mode
    assert all(after[path] == 0o640 for path in private)


@pytest.mark.parametrize("link_kind", ["file_symlink", "directory_symlink", "hardlink", "static_symlink"])
def test_public_tree_links_are_rejected_before_any_permissions_change(tmp_path, link_kind):
    app_root, release, static_root, private = _restricted_release(tmp_path)
    if link_kind == "file_symlink":
        (static_root / "private-link").symlink_to(private[0])
    elif link_kind == "directory_symlink":
        (static_root / "private-dir").symlink_to(release / "venv", target_is_directory=True)
    elif link_kind == "hardlink":
        os.link(private[0], static_root / "private-hardlink")
    else:
        moved = release / "private-static"
        static_root.rename(moved)
        static_root.symlink_to(moved, target_is_directory=True)
    before = _modes(app_root)
    with pytest.raises(ValueError, match="symlinks|hard links"):
        expose_release_static(app_root, release)
    assert _modes(app_root) == before


def test_web_template_compresses_and_caches_only_the_static_location():
    source = (ROOT / "deploy/nginx/twinevia.conf").read_text()
    locations = re.findall(r'location \^~ /static/ \{([^}]+)\}', source)
    assert len(locations) == 2
    for location in locations:
        assert "alias /opt/twinevia-saas/current/app/static/;" in location
        assert "gzip on;" in location and "gzip_vary on;" in location
        assert "text/css application/javascript" in location and "image/svg+xml" in location
        assert 'add_header Cache-Control $twinevia_static_cache_control;' in location
        assert 'add_header X-Content-Type-Options "nosniff" always;' in location
        assert 'add_header Strict-Transport-Security "max-age=31536000" always;' in location
        assert "autoindex off;" in location and "proxy_pass" not in location


@pytest.mark.parametrize('query,expected', [
    ('', 'public, max-age=300, must-revalidate'),
    ('v', 'public, max-age=300, must-revalidate'),
    ('v=', 'public, max-age=300, must-revalidate'),
    ('theme=dark&v=&image=logo', 'public, max-age=300, must-revalidate'),
    ('version=reviewed-release', 'public, max-age=300, must-revalidate'),
    ('preview=reviewed-release', 'public, max-age=300, must-revalidate'),
    ('v=reviewed-release', 'public, max-age=31536000, immutable'),
    ('theme=dark&v=reviewed-release', 'public, max-age=31536000, immutable'),
    ('v=0&image=logo', 'public, max-age=31536000, immutable'),
])
def test_web_template_immutable_cache_requires_a_nonempty_version_query(query, expected):
    source = (ROOT / 'deploy/nginx/twinevia.conf').read_text()
    mapping = re.findall(r'map \$arg_v \$twinevia_static_cache_control \{([^}]+)\}', source)
    assert len(mapping) == 1
    # Exercise the actual literal map entries without invoking a web server.
    entries = re.findall(r'^\s*(""|default)\s+"([^"]+)";', mapping[0], re.MULTILINE)
    assert len(entries) == 2
    policies = dict(entries)
    version = parse_qs(query, keep_blank_values=True).get('v', [''])[0]
    assert policies['default' if version else '""'] == expected
    assert 'map $args $twinevia_static_cache_control' not in source


def test_release_flow_exposes_both_bootstrap_and_candidate_static_after_restriction():
    source = (ROOT / "deploy/release_twinevia_saas.sh").read_text()
    assert 'sudo install -d -o root -g "${APP_GROUP}" -m 0751 "${RELEASES_DIR}"' in source
    assert source.count('/deploy/static_permissions.py" --app-root "${APP_ROOT}"') == 3
    assert source.index('sudo chmod -R u=rwX,g=rX,o= "${pending_release}"') < source.index('--release-root "${pending_release}"')
    assert source.index('sudo chmod -R u=rwX,g=rX,o= "${bootstrap_pending}"') < source.index('--release-root "${bootstrap_dir}"')


def _historical_static_preparation(entrypoint):
    """Execute the actual pre-activation guards with fake privileged transport."""
    if entrypoint == 'release':
        source = (ROOT / 'deploy/release_twinevia_saas.sh').read_text()
        block = re.search(
            r'^if \[\[ -n "\$\{old_release\}" && "\$\{TWINEVIA_INITIALIZE_RUNTIME:-0\}" != "1" \]\]; then\n.*?^fi$',
            source, re.MULTILINE | re.DOTALL,
        ).group(0)
        assert source.index('--release-root "${old_release}"') < source.index('atomic_link "${release_dir}" "${CURRENT_LINK}"')
        return block
    source = (ROOT / 'deploy/rollback_twinevia_saas.sh').read_text()
    start = source.index('runtime_ctl verify-target --target "${target_release}"')
    block = source[start:source.index('\natomic_link() {', start)]
    assert source.index('--release-root "${target_release}"') < source.index('atomic_link "${target_release}" "${CURRENT_LINK}"')
    return block


@pytest.mark.parametrize('entrypoint', ['release', 'rollback'])
@pytest.mark.parametrize('verified', [False, True])
def test_verified_historical_static_is_prepared_before_activation_with_trusted_new_helper(tmp_path, entrypoint, verified):
    app_root, release, static_root, private = _restricted_release(tmp_path)
    # This retained source can use schema 032 while predating P1.4. It has no
    # static-permission helper of its own; source/schema guards remain external.
    migration = release / 'app/saas_migrations/032_historical_fixture.py'
    migration.parent.mkdir()
    migration.write_text('"""Synthetic retained schema 032 fixture."""\n')
    migration.chmod(0o640)
    assert not (release / 'deploy/static_permissions.py').exists()
    before = _modes(app_root)
    calls = tmp_path / 'calls.txt'
    script = tmp_path / 'prepare-historical-static.sh'
    script.write_text('''set -euo pipefail
runtime_ctl() {
  [[ "$1" == "verify-target" && "$2" == "--target" && "$3" == "$target_release" ]]
  printf 'verified-target\\n' >> "$CALL_LOG"
  [[ "$VERIFIED" == "1" ]]
}
sudo() {
  if [[ "$1" == "-u" ]]; then
    printf 'schema-doctor\\n' >> "$CALL_LOG"
    return 0
  fi
  [[ "$1" == "python3.11" ]]
  shift
  [[ "$1" == "$TRUSTED_HELPER" ]]
  printf 'trusted-static-helper\\n' >> "$CALL_LOG"
  "$FIXTURE_PYTHON" "$@"
}
''' + _historical_static_preparation(entrypoint) + '\nprintf "activation-ready\\n" >> "$CALL_LOG"\n')
    result = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=10, env={
        'PATH': os.defpath, 'APP_ROOT': str(app_root), 'APP_USER': 'fixture-user',
        'SOURCE_ROOT': str(ROOT), 'old_release': str(release), 'target_release': str(release),
        'ENV_FILE': str(tmp_path / 'never-read-environment'), 'CALL_LOG': str(calls),
        'VERIFIED': '1' if verified else '0', 'FIXTURE_PYTHON': sys.executable,
        'TRUSTED_HELPER': str(ROOT / 'deploy/static_permissions.py'),
        '_TWINEVIA_RUNTIME_HELPER': str(ROOT / 'deploy/runtime_control.py'),
    })
    recorded = calls.read_text().splitlines()
    if not verified:
        assert result.returncode != 0
        assert recorded == ['verified-target']
        assert _modes(app_root) == before
    else:
        assert result.returncode == 0, result.stderr
        assert recorded == ['verified-target'] + (['schema-doctor'] if entrypoint == 'rollback' else []) + ['trusted-static-helper', 'activation-ready']
        assert all(path.stat().st_mode & stat.S_IXOTH for path in (app_root, app_root / 'releases', release, release / 'app', static_root))
        assert (static_root / 'css/app.css').stat().st_mode & stat.S_IROTH
        assert all(stat.S_IMODE(path.stat().st_mode) == before[path] for path in (*private, migration))
