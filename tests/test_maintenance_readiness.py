"""Maintenance is observable non-readiness, never a waiver for another failure."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape
import pytest

from app.services import readiness_service as service
from deploy.runtime_control import RUNTIME_UNITS, SERVICE_UNITS, TIMER_UNITS, read_maintenance_receipt


ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
BILLING = 'twinevia-saas-billing-reconcile.timer'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config.update(APP_RELEASE_ID='reviewed-release', APP_RELEASE_SHA=SHA,
                      READINESS_SYSTEMCTL_TIMEOUT_SECONDS=5,
                      READINESS_REQUIRED_SYSTEMD_TIMERS=','.join(TIMER_UNITS),
                      RUNTIME_MAINTENANCE_FILE=str(tmp_path / 'maintenance.json'))
    facts = {unit: {'load_state': 'loaded', 'active_state': 'active',
                    'sub_state': 'running' if unit in SERVICE_UNITS else 'waiting',
                    'unit_file_state': 'enabled'} for unit in RUNTIME_UNITS}

    def inspect(command, **kwargs):
        assert command[0:2] == ('systemctl', 'show')
        assert all(unit in command for unit in RUNTIME_UNITS)
        output = '\n\n'.join('\n'.join((f'Id={unit}', f'LoadState={row["load_state"]}',
                                          f'ActiveState={row["active_state"]}', f'SubState={row["sub_state"]}',
                                          f'UnitFileState={row["unit_file_state"]}')) for unit, row in facts.items())
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')

    runner = Mock(side_effect=inspect)
    monkeypatch.setattr(service.subprocess, 'run', runner)
    for name, check_name in (('_database_check', 'postgresql'), ('_migration_check', 'migrations'),
                             ('_configuration_check', 'required_configuration'), ('_backup_check', 'encrypted_backup'),
                             ('_restore_drill_check', 'restore_drill'), ('_worker_check', 'rq_worker')):
        monkeypatch.setattr(service, name, lambda *args, key=check_name: service.ReadinessCheck(key, True, 'healthy fixture'))
    monkeypatch.setattr(service, '_redis_and_queue_checks', lambda app: (
        service.ReadinessCheck('redis', True, 'healthy fixture'), service.ReadinessCheck('queue_operation', True, 'healthy fixture')))
    # Files are synthetic and owned by the local test user. Production retains
    # trusted_uid=0; only this fixture injects its own owner into the real parser.
    monkeypatch.setattr(service, 'read_maintenance_receipt', lambda path, **kwargs:
                        read_maintenance_receipt(path, trusted_uid=os.geteuid(), **kwargs))

    def declare(file_state='enabled', **changes):
        facts[BILLING].update(active_state='inactive', sub_state='dead', unit_file_state=file_state)
        now = datetime.now(timezone.utc)
        receipt = dict(version=1, held_units=[BILLING], states={BILLING: deepcopy(facts[BILLING])},
                       reason='Review the historical recovery repair', actor='fixture operator',
                       declared_at=now.isoformat(), updated_at=now.isoformat(),
                       expires_at=(now + timedelta(hours=24)).isoformat(), release_sha=SHA,
                       release_id='reviewed-release', transaction_id='fixture-maintenance-transaction')
        receipt.update(changes)
        path = Path(app.config['RUNTIME_MAINTENANCE_FILE'])
        path.write_text(json.dumps(receipt))
        path.chmod(0o600)
        return receipt

    return SimpleNamespace(app=app, facts=facts, runner=runner, declare=declare,
                           report=lambda: service.run_readiness_checks(app, require_worker=True, require_launch_artifacts=True))


def test_canonical_eight_units_are_reported_with_source_and_one_readonly_probe(runtime):
    report = runtime.report()
    payload = report.as_dict()
    assert report.ready and report.state == 'ready'
    assert payload['release_sha'] == SHA and payload['release_id'] == 'reviewed-release'
    assert set(payload['units']) == set(RUNTIME_UNITS) and len(payload['units']) == 8
    assert all(row['active_state'] == 'active' for row in payload['units'].values())
    assert runtime.runner.call_count == 1


@pytest.mark.parametrize('file_state', ['enabled', 'disabled', 'masked', 'masked-runtime'])
def test_valid_exact_hold_is_maintenance_but_remains_publicly_nonready(runtime, file_state):
    runtime.declare(file_state)
    if file_state.startswith('masked'):
        runtime.facts[BILLING]['load_state'] = 'masked'
        path = Path(runtime.app.config['RUNTIME_MAINTENANCE_FILE'])
        receipt = json.loads(path.read_text())
        receipt['states'][BILLING]['load_state'] = 'masked'
        path.write_text(json.dumps(receipt))
    report = runtime.report()
    assert report.ready is False and report.state == 'maintenance'
    assert report.maintenance['actor'] == 'fixture operator'
    assert report.maintenance['release_sha'] == SHA
    assert report.units[BILLING]['held'] is True
    failed = {row.name for row in report.checks if not row.ready}
    assert failed == {'scheduler_timers', 'runtime_maintenance'}


def test_public_route_keeps_503_plain_status_and_additive_authenticated_json(runtime, monkeypatch):
    runtime.declare()
    report = runtime.report()
    from app import routes

    runtime.app.config['READINESS_TOKEN'] = 'isolated-maintenance-readiness-token'
    runtime.app.add_url_rule('/ready', view_func=routes.readiness)
    monkeypatch.setattr(routes, 'run_readiness_checks', lambda *args, **kwargs: report)
    client = runtime.app.test_client()
    assert client.get('/ready').status_code == 404
    headers = {'X-Twinevia-Readiness-Token': runtime.app.config['READINESS_TOKEN']}
    response = client.get('/ready', headers=headers)
    assert response.status_code == 503 and response.data == b'NOT READY'
    assert response.headers['Cache-Control'] == 'no-store'
    response = client.get('/ready', headers={**headers, 'Accept': 'application/json'})
    assert response.status_code == 503 and response.get_json()['ready'] is False
    assert response.get_json()['state'] == 'maintenance'
    assert response.get_json()['maintenance']['reason'] == 'Review the historical recovery repair'


@pytest.mark.parametrize('failure', ['postgresql', 'migrations', 'required_configuration', 'encrypted_backup', 'restore_drill', 'rq_worker', 'redis', 'queue_operation'])
def test_maintenance_cannot_hide_an_independent_failure(runtime, monkeypatch, failure):
    runtime.declare()
    methods = {'postgresql': '_database_check', 'migrations': '_migration_check',
               'required_configuration': '_configuration_check', 'encrypted_backup': '_backup_check',
               'restore_drill': '_restore_drill_check', 'rq_worker': '_worker_check'}
    if failure in methods:
        monkeypatch.setattr(service, methods[failure], lambda *args: service.ReadinessCheck(failure, False, 'independent failure'))
    else:
        monkeypatch.setattr(service, '_redis_and_queue_checks', lambda app: (
            service.ReadinessCheck('redis', failure != 'redis', 'fixture'),
            service.ReadinessCheck('queue_operation', failure != 'queue_operation', 'fixture')))
    report = runtime.report()
    assert report.ready is False and report.state == 'not_ready'
    assert any(check.name == failure and check.ready is False for check in report.checks)


def test_undeclared_pause_and_additional_pause_are_not_maintenance(runtime):
    runtime.facts[BILLING].update(active_state='inactive', sub_state='dead')
    assert runtime.report().state == 'not_ready'
    runtime.declare()
    runtime.facts['twinevia-saas-backup.timer'].update(active_state='inactive', sub_state='dead')
    assert runtime.report().state == 'not_ready'


def test_configuration_cannot_remove_a_canonical_required_timer(runtime):
    runtime.app.config['READINESS_REQUIRED_SYSTEMD_TIMERS'] = 'twinevia-saas-billing-reconcile.timer'
    runtime.facts['twinevia-saas-platform-restart-queue.timer']['active_state'] = 'inactive'
    assert runtime.report().state == 'not_ready'


@pytest.mark.parametrize('change', ['resumed', 'enablement', 'failed', 'missing_service'])
def test_changed_operating_state_cannot_reuse_the_maintenance_waiver(runtime, change):
    runtime.declare()
    if change == 'resumed':
        runtime.facts[BILLING].update(active_state='active', sub_state='waiting')
    elif change == 'enablement':
        runtime.facts[BILLING]['unit_file_state'] = 'disabled'
    elif change == 'failed':
        runtime.facts[BILLING]['active_state'] = 'failed'
    else:
        runtime.facts[SERVICE_UNITS[0]]['load_state'] = 'not-found'
    report = runtime.report()
    assert report.ready is False and report.state == 'not_ready'


@pytest.mark.parametrize('invalid', ['expired', 'future', 'wrong_sha', 'missing_app_sha', 'world_writable', 'invalid_json', 'null_state', 'string_state', 'list_state', 'symlink'])
def test_invalid_or_untrusted_receipt_is_structured_nonreadiness(runtime, invalid):
    now = datetime.now(timezone.utc)
    changes = {'expired': {'expires_at': (now - timedelta(seconds=1)).isoformat()},
               'future': {'declared_at': (now + timedelta(hours=1)).isoformat()},
               'wrong_sha': {'release_sha': 'b' * 40}}
    runtime.declare(**changes.get(invalid, {}))
    path = Path(runtime.app.config['RUNTIME_MAINTENANCE_FILE'])
    if invalid == 'missing_app_sha':
        runtime.app.config['APP_RELEASE_SHA'] = ''
    elif invalid == 'world_writable':
        path.chmod(0o666)
    elif invalid == 'invalid_json':
        path.write_text('{broken')
    elif invalid in {'null_state', 'string_state', 'list_state'}:
        receipt = json.loads(path.read_text())
        receipt['states'][BILLING] = {'null_state': None, 'string_state': 'inactive', 'list_state': []}[invalid]
        path.write_text(json.dumps(receipt))
    elif invalid == 'symlink':
        original = path.with_suffix('.original')
        path.rename(original)
        path.symlink_to(original)
    report = runtime.report()
    assert report.state == 'not_ready' and report.ready is False
    assert any(check.name == 'runtime_maintenance' and not check.ready for check in report.checks)


def test_an_active_disabled_service_is_active_with_a_reboot_warning(runtime):
    runtime.facts[SERVICE_UNITS[1]]['unit_file_state'] = 'disabled'
    report = runtime.report()
    assert report.ready and report.state == 'ready'
    assert report.units[SERVICE_UNITS[1]]['active_state'] == 'active'
    assert report.units[SERVICE_UNITS[1]]['unit_file_state'] == 'disabled'
    assert any('may not start after a reboot' in warning for warning in report.warnings)


@pytest.mark.parametrize('result', ['timeout', 'nonzero', 'incomplete', 'duplicate'])
def test_failed_or_incomplete_systemd_proof_is_never_maintenance(runtime, result):
    runtime.declare()
    if result == 'timeout':
        runtime.runner.side_effect = subprocess.TimeoutExpired('systemctl', 5)
    else:
        original = runtime.runner.side_effect(('systemctl', 'show', *RUNTIME_UNITS))
        if result == 'nonzero':
            original.returncode = 1
        elif result == 'incomplete':
            original.stdout = original.stdout.split('\n\n', 1)[0]
        else:
            original.stdout += '\n\n' + original.stdout.split('\n\n', 1)[0]
        runtime.runner.side_effect = None
        runtime.runner.return_value = original
    assert runtime.report().state == 'not_ready'


def _render_platform(maintenance):
    loader = ChoiceLoader([DictLoader({'base.html': '{% block content %}{% endblock %}'}),
                           FileSystemLoader(str(ROOT / 'app/templates'))])
    environment = Environment(loader=loader, autoescape=select_autoescape())
    return environment.get_template('platform/home.html').render(
        platform_home={'summary': {}, 'attention_rows': [], 'recent_rows': [], 'maintenance': maintenance,
                       'service_restart': {'enabled': True, 'last_result': None}},
        csrf_token=lambda: 'fixture-csrf', url_for=lambda *args, **kwargs: '#')


def test_platform_shows_operator_reason_date_and_source_without_claiming_billing_stopped(runtime):
    runtime.declare(reason='<script>unsafe reason</script>')
    view = service.platform_maintenance_view(runtime.app)
    html = _render_platform(view)
    assert 'Maintenance declared' in html and 'fixture operator' in html
    assert SHA in html and view['declared_at'] in html and view['expires_at'] in html
    assert '&lt;script&gt;unsafe reason&lt;/script&gt;' in html
    assert 'Billing reconciliation is paused.' in html
    assert 'Stripe renewals, webhooks, and user-initiated checkout may continue.' in html
    assert 'Restart active services' in html and 'role="status"' in html
    assert '<script>unsafe reason</script>' not in html


def test_invalid_maintenance_receipt_disables_restart_with_explanation(runtime):
    runtime.declare(release_sha='b' * 40)
    html = _render_platform(service.platform_maintenance_view(runtime.app))
    assert 'Maintenance state cannot be verified' in html and 'Host review is required' in html
    assert 'disabled aria-describedby="platform-maintenance-state"' in html and 'role="alert"' in html


def _monitor_script():
    source = (ROOT / '.github/workflows/monitor-twinevia-production.yml').read_text()
    after = source.split('if python3 - "${READY_STATUS}"', 1)[1].split("<<'PY'\n", 1)[1]
    return textwrap.dedent(after.split('\n          PY', 1)[0])


@pytest.mark.parametrize('mode', ['ready', 'maintenance', 'maintenance_with_failure', 'invalid_json', 'empty_checks'])
def test_monitor_keeps_maintenance_nonready_and_does_not_hide_other_failures(tmp_path, mode):
    payload = {'state': 'ready', 'ready': True, 'checks': [{'name': 'postgresql', 'ready': True}]}
    code = 200
    if mode.startswith('maintenance'):
        payload.update(state='maintenance', ready=False, maintenance={'held_units': [BILLING], 'actor': 'fixture operator',
                       'reason': 'Reviewed repair', 'declared_at': '2026-10-27T00:00:00Z', 'release_sha': SHA},
                       checks=[{'name': 'scheduler_timers', 'ready': False}, {'name': 'runtime_maintenance', 'ready': False}])
        code = 503
    if mode == 'maintenance_with_failure':
        payload['checks'].append({'name': 'postgresql', 'ready': False})
    if mode == 'empty_checks':
        payload['checks'] = []
    body, diagnostics, state = (tmp_path / name for name in ('body.json', 'diagnostics.txt', 'state.txt'))
    body.write_text('{invalid' if mode == 'invalid_json' else json.dumps(payload))
    result = subprocess.run([sys.executable, '-c', _monitor_script(), str(code), str(body), str(diagnostics), str(state)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == (0 if mode == 'ready' else 1)
    expected = mode if mode in {'ready', 'maintenance'} else 'not_ready'
    assert state.read_text().strip() == expected
    if mode == 'maintenance':
        assert 'normal operational readiness has not returned' in diagnostics.read_text()
        assert 'fixture operator' in diagnostics.read_text() and 'Reviewed repair' in diagnostics.read_text()


@pytest.mark.parametrize('readiness_state', ['maintenance', 'not_ready', 'ready'])
def test_monitor_requires_full_recovery_before_closing_an_uptime_incident(tmp_path, readiness_state):
    source = (ROOT / '.github/workflows/monitor-twinevia-production.yml').read_text()
    script = textwrap.dedent(source.split('      - name: Update uptime incident', 1)[1].split('        run: |\n', 1)[1])
    commands = tmp_path / 'commands.txt'
    fake_gh = tmp_path / 'gh'
    fake_gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_COMMANDS"\nif [ "$1 $2" = "issue list" ]; then printf "7\\n"; fi\n')
    fake_gh.chmod(0o700)
    (tmp_path / 'twinevia-readiness-state.txt').write_text(readiness_state + '\n')
    (tmp_path / 'twinevia-monitor-diagnostics.txt').write_text('Fixture readiness state: ' + readiness_state)
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'], 'RUNNER_TEMP': str(tmp_path),
           'GH_COMMANDS': str(commands), 'JOB_STATUS': 'success', 'GITHUB_REPOSITORY': 'fixture/twinevia', 'GITHUB_RUN_ID': '123'}
    result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    recorded = commands.read_text().splitlines()
    assert any(line.startswith('issue close ') for line in recorded) is (readiness_state == 'ready')
    assert any(line.startswith('issue comment ') for line in recorded)


def test_configuration_uses_the_core_timer_and_receipt_defaults():
    from deploy.runtime_control import DEFAULT_MAINTENANCE_FILE
    env = dict(os.environ)
    env.pop('READINESS_REQUIRED_SYSTEMD_TIMERS', None)
    env.pop('RUNTIME_MAINTENANCE_FILE', None)
    env.update(PYTHON_DOTENV_DISABLED='1', APP_RELEASE_SHA=SHA)
    result = subprocess.run([sys.executable, '-c',
        'import json; from app.config import Config; print(json.dumps([Config.READINESS_REQUIRED_SYSTEMD_TIMERS, Config.RUNTIME_MAINTENANCE_FILE, Config.APP_RELEASE_SHA]))'],
        env=env, cwd=ROOT, capture_output=True, text=True, check=True)
    timers, receipt, sha = json.loads(result.stdout)
    assert tuple(timers.split(',')) == TIMER_UNITS
    assert receipt == DEFAULT_MAINTENANCE_FILE and sha == SHA


@pytest.mark.parametrize('state', ['maintenance', 'not_ready', 'ready'])
def test_host_monitor_never_heartbeats_or_claims_recovery_while_nonready(tmp_path, state):
    app_root, binaries = tmp_path / 'application', tmp_path / 'bin'
    (app_root / 'venv/bin').mkdir(parents=True)
    binaries.mkdir()
    payload = {'ready': state == 'ready', 'state': state,
               'maintenance': {'held_units': [BILLING], 'actor': 'fixture operator', 'reason': 'Reviewed fixture repair',
                               'declared_at': '2026-10-27T00:00:00Z', 'expires_at': '2026-10-28T00:00:00Z', 'release_sha': SHA}}
    fixture = tmp_path / 'readiness.json'
    fixture.write_text(json.dumps(payload))
    runtime_env = tmp_path / 'runtime.env'
    runtime_env.write_text('APP_ROOT=/not-the-running-release\n')
    fake_python = app_root / 'venv/bin/python'
    fake_python.write_text('#!/bin/sh\nif [ "$1" = "-m" ]; then cat "$READINESS_FIXTURE"; exit "$READINESS_EXIT"; fi\nexec "$REAL_PYTHON" "$@"\n')
    scripts = {
        'systemctl': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_CALLS"\nif [ "$HELD_BILLING" = "1" ] && [ "$3" = "twinevia-saas-billing-reconcile.timer" ]; then exit 3; fi\n',
        'logger': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$MONITOR_LOG"\n',
        'curl': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$MONITOR_CURL"\n',
    }
    fake_python.chmod(0o700)
    for name, content in scripts.items():
        path = binaries / name
        path.write_text(content)
        path.chmod(0o700)
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
           'APP_ROOT': str(app_root), 'TWINEVIA_ENV_FILE': str(runtime_env),
           'PYTHONPATH': str(ROOT) + os.pathsep + os.environ.get('PYTHONPATH', ''),
           'PYTHON_DOTENV_DISABLED': '1', 'REAL_PYTHON': sys.executable,
           'READINESS_FIXTURE': str(fixture), 'READINESS_EXIT': '0' if state == 'ready' else '1',
           'HELD_BILLING': '0' if state == 'ready' else '1', 'ALERT_WEBHOOK_URL': '',
           'UPTIME_MONITOR_HEARTBEAT_URL': 'https://fixture.invalid/heartbeat', 'APP_RELEASE_ID': 'fixture-release',
           'SYSTEMCTL_CALLS': str(tmp_path / 'systemctl.txt'), 'MONITOR_LOG': str(tmp_path / 'monitor.txt'),
           'MONITOR_CURL': str(tmp_path / 'curl.txt')}
    result = subprocess.run(['bash', str(ROOT / 'deploy/run_readiness_once.sh')],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == (0 if state == 'ready' else 1), result.stderr
    logged = (tmp_path / 'monitor.txt').read_text()
    if state == 'ready':
        assert 'readiness passed' in logged
        assert 'https://fixture.invalid/heartbeat' in (tmp_path / 'curl.txt').read_text()
        checked = (tmp_path / 'systemctl.txt').read_text()
        assert all(unit in checked for unit in RUNTIME_UNITS)
    else:
        assert 'readiness passed' not in logged
        assert not (tmp_path / 'curl.txt').exists()
        if state == 'maintenance':
            assert 'maintenance remains non-ready' in logged and 'fixture operator' in logged
            assert 'Reviewed fixture repair' in logged and SHA in logged
