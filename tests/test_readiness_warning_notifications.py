"""Warning delivery never gates readiness and does not alert every timer tick."""
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys

import pytest

from deploy.readiness_alert_state import notification_due, record_notification, REPEAT_SECONDS


ROOT = Path(__file__).resolve().parents[1]


def test_notification_state_tracks_changes_recurrence_and_hourly_reminder(tmp_path):
    path = tmp_path / 'state/alert.json'
    assert notification_due(path, 'Billing warning', now=100)
    record_notification(path, 'Billing warning', now=100)
    assert not notification_due(path, 'Billing warning', now=220)
    assert notification_due(path, 'Different billing warning', now=220)
    assert notification_due(path, 'Billing warning', now=100 + REPEAT_SECONDS)
    assert notification_due(path, 'Billing warning', now=90)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(json.loads(path.read_text())) == {'fingerprint', 'notified_at'}
    assert 'Billing warning' not in path.read_text()
    path.unlink()
    assert notification_due(path, 'Billing warning', now=230)


@pytest.mark.parametrize('contents', ['not JSON', '[]', '{"notified_at":"invalid"}',
                                      '{"notified_at":"NaN","fingerprint":"x"}'])
def test_damaged_notification_state_does_not_silence_warning(tmp_path, contents):
    path = tmp_path / 'alert.json'
    path.write_text(contents)
    assert notification_due(path, 'Billing warning')


@pytest.fixture
def monitor(tmp_path):
    root, binaries = tmp_path / 'application', tmp_path / 'bin'
    (root / 'venv/bin').mkdir(parents=True)
    (root / 'deploy').mkdir()
    binaries.mkdir()
    (root / 'deploy/__init__.py').write_text('')
    (root / 'deploy/runtime_control.py').write_text('RUNTIME_UNITS = ("fixture.service",)\n')
    python = root / 'venv/bin/python'
    python.write_text('#!/bin/sh\nif [ "$1" = "-m" ]; then cat "$FIXTURE_REPORT"; exit 0; fi\n'
                      f'exec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o700)
    for name, source in {
        'systemctl': '#!/bin/sh\nexit 0\n',
        'logger': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FIXTURE_LOG"\n',
        'curl': '#!/bin/sh\ncat >> "$FIXTURE_DELIVERED"\nexit "${FIXTURE_CURL_RESULT:-0}"\n',
        'hostname': '#!/bin/sh\nprintf "local-fixture\\n"\n',
    }.items():
        path = binaries / name
        path.write_text(source)
        path.chmod(0o700)
    report, log, delivered, state = (tmp_path / name for name in ('report.json', 'journal.txt', 'delivered.txt', 'state.json'))
    environment = {'PATH': str(binaries) + os.pathsep + os.defpath, 'APP_ROOT': str(root),
        'TWINEVIA_ENV_FILE': str(tmp_path / 'absent-env'), 'FIXTURE_REPORT': str(report),
        'FIXTURE_LOG': str(log), 'FIXTURE_DELIVERED': str(delivered),
        'READINESS_ALERT_STATE_FILE': str(state), 'ALERT_WEBHOOK_URL': 'https://fixture.invalid/alert',
        'UPTIME_MONITOR_HEARTBEAT_URL': ''}

    def run(warnings):
        report.write_text(json.dumps({'ready': True, 'state': 'ready', 'warnings': warnings}))
        result = subprocess.run(['bash', str(ROOT / 'deploy/run_readiness_once.sh')], env=environment,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return log.read_text()
    return run, environment, delivered, state


@pytest.mark.parametrize('configured', [False, True])
def test_ready_billing_warning_delivered_or_logged_once_then_on_change(monitor, configured):
    run, environment, delivered, state = monitor
    if not configured:
        environment['ALERT_WEBHOOK_URL'] = ''
    warning = 'Billing reconciliation warning: the last billing reconciliation run failed.'
    first = run([warning])
    assert warning in first and state.exists()
    second = run([warning])
    assert second.count(warning) == 1
    if configured:
        assert len(delivered.read_text().splitlines()) == 1
        assert warning in json.loads(delivered.read_text().splitlines()[0])['text']
    else:
        assert 'Alerting not configured' in first and not delivered.exists()
        assert second.count('Alerting not configured') == 1
    third = run(['Billing reconciliation warning: result could not be inspected.'])
    assert 'result could not be inspected' in third
    run([])
    assert not state.exists()
    run([warning])
    if configured:
        assert len(delivered.read_text().splitlines()) == 3


def test_notification_delivery_failure_is_logged_and_retried_without_failing_readiness(monitor):
    run, environment, delivered, state = monitor
    environment['FIXTURE_CURL_RESULT'] = '22'
    assert 'delivery will be retried' in run(['Billing warning'])
    assert not state.exists()
    environment['FIXTURE_CURL_RESULT'] = '0'
    run(['Billing warning'])
    assert len(delivered.read_text().splitlines()) == 2 and state.exists()


def test_readiness_unit_keeps_notification_state_across_releases_and_private_tmp():
    source = (ROOT / 'deploy/twinevia-saas-readiness.service').read_text()
    assert 'StateDirectory=twinevia-saas-readiness\n' in source
    assert 'StateDirectoryMode=0700\n' in source and 'PrivateTmp=true\n' in source
