"""Pinned local libraries keep browser functionality independent of a CDN."""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest

from tests.test_ux_recovery import ux, login


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'app/static/vendor'
MANIFEST = json.loads((VENDOR / 'manifest.json').read_text())


def test_vendor_manifest_preserves_existing_versions_and_license_files():
    versions = {'/'.join(path.split('/')[:2])
                for path in MANIFEST if path.endswith(('.css', '.js'))}
    assert versions == {'bootstrap/5.3.2', 'bootstrap-icons/1.11.1',
                        'bootstrap-icons/1.11.3', 'chart.js/4.4.1'}
    for path, entry in MANIFEST.items():
        local = VENDOR / path
        assert hashlib.sha256(local.read_bytes()).hexdigest() == entry['sha256']
        package, version = path.split('/')[:2]
        assert entry['source'].startswith(f'https://cdn.jsdelivr.net/npm/{package}@{version}/')
    for package in ('bootstrap/5.3.2', 'bootstrap-icons/1.11.1', 'bootstrap-icons/1.11.3', 'chart.js/4.4.1'):
        license = next((VENDOR / package).glob('LICENSE*'))
        assert license.relative_to(VENDOR).as_posix() in MANIFEST
        assert 'MIT License' in license.read_text()


@pytest.mark.parametrize('version', ['1.11.1', '1.11.3'])
def test_icon_font_urls_resolve_to_the_matching_local_fonts(version):
    stylesheet = next((VENDOR / f'bootstrap-icons/{version}/font').glob('*.css'))
    references = re.findall(r'url\([\"\']?([^\)\"\']+)', stylesheet.read_text())
    assert len(references) == 2
    for reference in references:
        url = urlsplit(reference)
        assert not url.scheme and not url.netloc
        font = (stylesheet.parent / url.path).resolve()
        assert font.is_relative_to(stylesheet.parent)
        assert font.is_file() and font.relative_to(VENDOR).as_posix() in MANIFEST


def test_templates_load_browser_assets_from_the_app_and_csp_has_no_cdn_exception():
    from app.config import DEFAULT_CONTENT_SECURITY_POLICY
    for template in (ROOT / 'app/templates').rglob('*.html'):
        assert not re.search(r'<(?:script|link)[^>]+(?:src|href)=[\"\']https?://', template.read_text()), template
    assert 'https://cdn.jsdelivr.net' not in DEFAULT_CONTENT_SECURITY_POLICY
    assert "font-src 'self' data:;" in DEFAULT_CONTENT_SECURITY_POLICY
    assert "script-src 'self' 'unsafe-inline';" in DEFAULT_CONTENT_SECURITY_POLICY
    assert "style-src 'self' 'unsafe-inline';" in DEFAULT_CONTENT_SECURITY_POLICY


def test_workspace_signin_and_marketing_render_local_libraries(ux):
    signin = ux.client.get('/login')
    assert b'/static/vendor/bootstrap/5.3.2/css/bootstrap.min.css' in signin.data
    assert b'/static/vendor/bootstrap-icons/1.11.1/font/bootstrap-icons.css' in signin.data
    assert b'/static/vendor/bootstrap/5.3.2/js/bootstrap.bundle.min.js' in signin.data
    marketing = ux.client.get('/')
    assert b'/static/vendor/bootstrap-icons/1.11.3/font/bootstrap-icons.min.css' in marketing.data
    login(ux)
    dashboard = ux.client.get('/dashboard')
    assert dashboard.status_code == 200
    assert b'/static/vendor/chart.js/4.4.1/chart.umd.min.js' in dashboard.data
    assert b'https://cdn.jsdelivr.net' not in dashboard.data
    for asset in MANIFEST:
        response = ux.client.get('/static/vendor/' + asset)
        assert response.status_code == 200
        assert hashlib.sha256(response.data).hexdigest() == MANIFEST[asset]['sha256']
