"""Operational failures remain visible without contacting real infrastructure."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
import pytest

from app.services import readiness_service as service
from deploy.runtime_control import RUNTIME_UNITS


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('output,returncode,ready', [
    ('LoadState=loaded\nResult=success\nExecMainStatus=0\n', 0, True),
    ('LoadState=loaded\nResult=exit-code\nExecMainStatus=1\n', 0, False),
    ('LoadState=loaded\nResult=success\nExecMainStatus=1\n', 0, False),
    ('LoadState=loaded\nResult=timeout\nExecMainStatus=0\n', 0, False),
    ('LoadState=not-found\nResult=success\nExecMainStatus=0\n', 0, False),
    ('LoadState=loaded\nResult=success\n', 0, False),
    ('LoadState=loaded\nResult=success\nExecMainStatus=invalid\n', 0, False),
    ('LoadState=loaded\nResult=success\nExecMainStatus=0\nResult=success\n', 0, False),
    ('', 0, False),
    ('LoadState=loaded\nResult=success\nExecMainStatus=0\n', 1, False),
])
def test_billing_readiness_requires_complete_successful_result(monkeypatch, output, returncode, ready):
    app = Flask(__name__)
    app.config['READINESS_SYSTEMCTL_TIMEOUT_SECONDS'] = 3
    runner = Mock(return_value=subprocess.CompletedProcess([], returncode, stdout=output))
    monkeypatch.setattr(service.subprocess, 'run', runner)
    check = service._billing_reconcile_check(app)
    assert check.name == 'billing_reconcile' and check.ready is ready
    command = runner.call_args.args[0]
    assert command[:2] == ('systemctl', 'show')
    assert command[-1] == 'twinevia-saas-billing-reconcile.service'
    assert runner.call_args.kwargs['timeout'] == 3


@pytest.mark.parametrize('failure', [FileNotFoundError('isolated missing command'), subprocess.TimeoutExpired('systemctl', 3)])
def test_billing_readiness_reports_inspection_errors(monkeypatch, failure):
    monkeypatch.setattr(service.subprocess, 'run', Mock(side_effect=failure))
    check = service._billing_reconcile_check(Flask(__name__))
    assert check.name == 'billing_reconcile' and check.ready is False
    assert 'could not be inspected' in check.detail


@pytest.fixture
def readiness_case(monkeypatch):
    app = Flask(__name__)
    app.config.update(SAAS_MODE=True, DEBUG=False, OPERATIONS_MONITORING_MODE='github_actions',
                      ALERT_WEBHOOK_URL='', APP_RELEASE_ID='fixture-release')
    for name in ('_database_check', '_migration_check', '_configuration_check', '_backup_check', '_restore_drill_check'):
        monkeypatch.setattr(service, name, lambda *args, key=name: service.ReadinessCheck(key, True, 'healthy fixture'))
    monkeypatch.setattr(service, '_runtime_readiness', lambda app: (
        (service.ReadinessCheck('runtime_services', True, 'healthy fixture'),), {}, None,
        ('Existing reboot warning',), False))
    monkeypatch.setattr(service, '_redis_and_queue_checks', lambda app: (
        service.ReadinessCheck('redis', True, 'healthy fixture'), service.ReadinessCheck('queue_operation', True, 'healthy fixture')))
    billing = Mock(return_value=service.ReadinessCheck('billing_reconcile', True, 'healthy fixture'))
    monkeypatch.setattr(service, '_billing_reconcile_check', billing)
    return SimpleNamespace(app=app, billing=billing)


def test_production_readiness_warns_when_alerting_missing_even_with_github_monitor(readiness_case):
    case = readiness_case
    report = service.run_readiness_checks(case.app, require_worker=False, require_launch_artifacts=True)
    assert report.ready is True
    case.billing.assert_called_once_with(case.app)
    assert 'Existing reboot warning' in report.warnings
    assert any('alerting not configured' in warning.lower() for warning in report.as_dict()['warnings'])


def test_configured_alert_destination_removes_only_missing_alert_warning(readiness_case):
    case = readiness_case
    case.app.config['ALERT_WEBHOOK_URL'] = 'https://fixture.invalid/alert'
    report = service.run_readiness_checks(case.app, require_worker=False, require_launch_artifacts=True)
    assert report.ready and report.warnings == ('Existing reboot warning',)


@pytest.mark.parametrize('debug,saas,launch', [(True, True, True), (False, False, True), (False, True, False)])
def test_isolated_modes_skip_production_billing_and_alert_checks(readiness_case, debug, saas, launch):
    case = readiness_case
    case.app.config.update(DEBUG=debug, SAAS_MODE=saas)
    report = service.run_readiness_checks(case.app, require_worker=False, require_launch_artifacts=launch)
    assert report.ready
    case.billing.assert_not_called()
    assert not any('alerting not configured' in warning.lower() for warning in report.warnings)


@pytest.mark.parametrize('detail', ['the last billing reconciliation run failed',
                                  'billing reconciliation result could not be inspected'])
def test_billing_failure_is_a_warning_without_gating_readiness(readiness_case, detail):
    case = readiness_case
    case.billing.return_value = service.ReadinessCheck('billing_reconcile', False, detail)
    report = service.run_readiness_checks(case.app, require_worker=False, require_launch_artifacts=True)
    assert report.ready is True and report.state == 'ready'
    assert all(check.ready for check in report.checks)
    assert f'Billing reconciliation warning: {detail}.' in report.warnings


def test_billing_warning_preserves_declared_maintenance_verification(readiness_case, monkeypatch):
    case = readiness_case
    monkeypatch.setattr(service, '_runtime_readiness', lambda app: (
        (service.ReadinessCheck('scheduler_timers', False, 'held fixture'),
         service.ReadinessCheck('runtime_maintenance', False, 'declared fixture')),
        {}, {'reason': 'Reviewed fixture hold'}, (), True))
    case.billing.return_value = service.ReadinessCheck('billing_reconcile', False, 'controlled failure')
    report = service.run_readiness_checks(case.app, require_worker=False, require_launch_artifacts=True)
    assert report.ready is False and report.state == 'maintenance'
    assert any('controlled failure' in warning for warning in report.warnings)
    assert {check.name for check in report.checks if not check.ready} == {'scheduler_timers', 'runtime_maintenance'}


@pytest.mark.parametrize('destination_configured', [False, True])
def test_host_failure_delivers_alert_or_logs_missing_destination(tmp_path, destination_configured):
    root, binaries = tmp_path / 'application', tmp_path / 'bin'
    (root / 'venv/bin').mkdir(parents=True)
    (root / 'deploy').mkdir()
    binaries.mkdir()
    (root / 'deploy/__init__.py').write_text('')
    (root / 'deploy/runtime_control.py').write_text(f'RUNTIME_UNITS = {RUNTIME_UNITS!r}\n')
    python = root / 'venv/bin/python'
    python.write_text('#!/bin/sh\nif [ "$1" = "-m" ]; then printf \'{"ready":false,"state":"not_ready"}\\n\'; exit 1; fi\n'
                      f'exec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o700)
    scripts = {
        'systemctl': '#!/bin/sh\nexit 0\n',
        'logger': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FIXTURE_LOG"\n',
        'curl': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FIXTURE_CURL"\ncat > "$FIXTURE_PAYLOAD"\n',
        'hostname': '#!/bin/sh\nprintf "isolated-fixture\\n"\n',
    }
    for name, source in scripts.items():
        command = binaries / name
        command.write_text(source)
        command.chmod(0o700)
    log, calls, payload = (tmp_path / name for name in ('journal.txt', 'curl.txt', 'payload.json'))
    env = {'PATH': str(binaries) + os.pathsep + os.defpath, 'APP_ROOT': str(root),
           'TWINEVIA_ENV_FILE': str(tmp_path / 'absent-local-env'), 'PYTHON_DOTENV_DISABLED': '1',
           'ALERT_WEBHOOK_URL': 'https://fixture.invalid/alert' if destination_configured else '',
           'UPTIME_MONITOR_HEARTBEAT_URL': 'https://fixture.invalid/heartbeat',
           'FIXTURE_LOG': str(log), 'FIXTURE_CURL': str(calls), 'FIXTURE_PAYLOAD': str(payload)}
    result = subprocess.run(['bash', str(ROOT / 'deploy/run_readiness_once.sh')], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1, result.stderr
    logged = log.read_text()
    assert 'readiness passed' not in logged
    if destination_configured:
        assert 'https://fixture.invalid/alert' in calls.read_text()
        assert 'heartbeat' not in calls.read_text()
        assert json.loads(payload.read_text()) == {'text': 'Twinevia application readiness checks failed on isolated-fixture.'}
        assert 'Alerting not configured' not in logged
    else:
        assert not calls.exists() and not payload.exists()
        assert '-p user.warning' in logged
        assert 'Alerting not configured' in logged and 'could not be delivered' in logged
