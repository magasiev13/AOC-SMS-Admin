"""The owner handoff stays actionable and free of private deployment details."""
from pathlib import Path
import re


def test_hardening_handoff_links_resolve_and_checks_remain_owner_actions():
    docs = Path(__file__).resolve().parents[1] / 'docs'
    text = (docs / 'production-hardening-checklist.md').read_text()
    assert text.count('- [ ]') == 7
    assert 'owner-run checklist' in text
    assert 'does not apply any server' in text
    assert 'off-server archive' in text and 'decryption key' in text
    assert 'opt-outs and suppression records' in text
    assert 'delivered alert' in text and 'billing' in text
    assert 'IPv4 and IPv6' in text and 'private configuration' in text
    assert 'production-hardening-checklist.md' in (docs / 'README.md').read_text()
    for target in re.findall(r'\]\(([^)]+)\)', text):
        assert (docs / target).is_file(), target
    assert not re.search(r'https?://|(?:\d{1,3}\.){3}\d{1,3}|[\w.+-]+@[\w.-]+', text)
    assert '.codex-backups' not in text
