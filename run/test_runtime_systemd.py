#!/usr/bin/env python3
"""Exercise runtime containment only on a disposable GitHub-hosted Ubuntu VM.

This is deliberately not a production diagnostic or a general systemctl tool.
The guard runs before any file/unit mutation. Provider activity is a local JSONL
ledger, and every fixture service has its IP networking denied by systemd.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import tarfile
from threading import Thread
import time
from uuid import uuid4


REPOSITORY = Path(__file__).resolve().parents[1]
RUNTIME_UNITS = (
    'twinevia-saas.service', 'twinevia-saas-worker.service',
    'twinevia-saas-scheduler.timer', 'twinevia-saas-billing-reconcile.timer',
    'twinevia-saas-platform-restart-queue.timer', 'twinevia-saas-a2p-reconcile.timer',
    'twinevia-saas-backup.timer', 'twinevia-saas-readiness.timer',
)
PAIRED_ONESHOTS = tuple(name.removesuffix('.timer') + '.service' for name in RUNTIME_UNITS if name.endswith('.timer'))
ALL_UNITS = RUNTIME_UNITS + PAIRED_ONESHOTS


def append_event(path, event):
    value = dict(event, at=datetime.now(timezone.utc).isoformat())
    with Path(path).open('a', encoding='utf-8') as output:
        output.write(json.dumps(value, sort_keys=True) + '\n')
        output.flush()
        os.fsync(output.fileno())


def run(command, *, check=True, timeout=30, **kwargs):
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    if check and completed.returncode:
        raise RuntimeError(f'Command failed ({completed.returncode}): {command!r}\n{completed.stdout}\n{completed.stderr}')
    return completed


def require_empty_systemd_inventory(systemctl):
    for command in ['list-unit-files', 'list-units']:
        result = run([str(systemctl), command, '--all', '--no-legend', '--no-pager', 'twinevia-saas*'], check=False)
        if 'twinevia-saas' in result.stdout:
            raise RuntimeError('Refusing systemd tests: pre-existing Twinevia units were found.')
        # Clean Ubuntu returns 1 for list-unit-files when the name filter has
        # no matches. That exact empty result is safe; diagnostics, unexpected
        # output and other failures cannot establish an empty inventory.
        allowed_codes = {0, 1} if command == 'list-unit-files' else {0}
        if result.returncode not in allowed_codes or result.stdout.strip() or result.stderr.strip():
            raise RuntimeError(f'Refusing systemd tests: cannot establish an empty {command} inventory '
                               f'(exit {result.returncode}).\n{result.stdout}\n{result.stderr}')


def oneshot_is_contained(properties, old_pid, financial_ledger):
    if (properties.get('ActiveState') not in {'inactive', 'failed'}
            or properties.get('MainPID') != '0' or properties.get('ControlPID') != '0'
            or financial_ledger):
        return False
    try:
        os.kill(old_pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def require_owned_github_vm():
    expected = {'GITHUB_ACTIONS': 'true', 'CI': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted'}
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('Refusing systemd tests: use root only in the disposable GitHub-hosted Ubuntu validation VM.')
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Refusing systemd tests: GITHUB_ACTIONS=true, CI=true and RUNNER_ENVIRONMENT=github-hosted are required.')
    if Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise RuntimeError('Refusing systemd tests: PID 1 is not systemd.')
    workspace = os.environ.get('GITHUB_WORKSPACE', '')
    if not workspace or Path(workspace).resolve() != REPOSITORY:
        raise RuntimeError('Refusing systemd tests: this checkout must be the GitHub job workspace.')
    if 'ID=ubuntu' not in Path('/etc/os-release').read_text().splitlines():
        raise RuntimeError('Refusing systemd tests: an Ubuntu GitHub-hosted VM is required.')
    if Path('/opt/twinevia-saas').exists() or Path('/var/lib/twinevia-saas').exists():
        raise RuntimeError('Refusing systemd tests: a Twinevia installation/state directory already exists.')
    systemctl = Path('/usr/bin/systemctl')
    if not systemctl.is_file():
        raise RuntimeError('Refusing systemd tests: /usr/bin/systemctl is unavailable.')
    require_empty_systemd_inventory(systemctl)
    for root in ['/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system', '/usr/local/lib/systemd/system']:
        directory = Path(root)
        if directory.is_dir() and any(directory.glob('twinevia-saas*')):
            raise RuntimeError(f'Refusing systemd tests: pre-existing Twinevia files exist under {directory}.')
    return systemctl


def service_child(arguments):
    root = Path(arguments.root).resolve()
    if (sys.platform != 'linux' or os.geteuid() != 0 or root.parent != Path('/var/tmp')
            or not root.name.startswith('twinevia-systemd-tests-')):
        raise RuntimeError('Refusing fixture child outside its owned disposable Linux workspace.')
    owner = json.loads((root / 'fixture-owner.json').read_text())
    if owner.get('identity') != os.environ.get('TWINEVIA_SYSTEMD_TEST_OWNER') or not owner.get('identity'):
        raise RuntimeError('Refusing fixture child without its owned systemd invocation identity.')
    identity = {'event': 'service_started', 'unit': arguments.unit, 'pid': os.getpid(),
                'source': str((root / 'current').resolve())}
    try:
        network_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        identity['ip_socket_denied'] = True
    else:
        network_socket.close()
        identity['ip_socket_denied'] = False
    append_event(root / 'process-ledger.jsonl', identity)
    if not identity['ip_socket_denied']:
        return 91
    if arguments.unit == 'twinevia-saas-billing-reconcile.service':
        # An intentionally running old-code oneshot is contained before this
        # barrier opens. A fake financial call can only append to this ledger.
        while not (root / 'allow-fake-financial-operation').exists():
            time.sleep(0.05)
        append_event(root / 'fake-financial-ledger.jsonl', dict(identity, event='fake_financial_operation'))
        return 0
    if arguments.unit.endswith('.service') and arguments.unit not in RUNTIME_UNITS:
        return 0
    while True:
        time.sleep(0.2)


class OwnedSystemdFixture:
    def __init__(self, systemctl, evidence_directory):
        self.systemctl = systemctl
        self.evidence_directory = Path(evidence_directory)
        self.root = Path(tempfile.mkdtemp(prefix='twinevia-systemd-tests-', dir='/var/tmp'))
        self.identity = uuid4().hex
        self.owner_marker = f'# twinevia-runtime-test-owner:{self.identity}'
        (self.root / 'fixture-owner.json').write_text(json.dumps({'identity': self.identity}))
        (self.root / 'fixture-owner.json').chmod(0o600)
        self.vendor = Path('/usr/local/lib/systemd/system')
        self.created_files = []
        self.events = []
        self.releases = {}
        self.started_at = datetime.now(timezone.utc).isoformat()

    def ctl(self, *arguments, check=True):
        result = run([str(self.systemctl), *arguments], check=check)
        self.events.append({'argv': list(arguments), 'returncode': result.returncode,
                            'stdout': result.stdout, 'stderr': result.stderr})
        return result

    def properties(self, unit):
        output = self.ctl('show', unit, '--no-pager', '--property=LoadState,ActiveState,SubState,UnitFileState,MainPID,ControlPID,Result,ActiveEnterTimestampMonotonic').stdout
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def wait(self, condition, *, description, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        raise AssertionError(f'Timed out waiting for {description}.')

    def ledger(self, name='process-ledger.jsonl'):
        path = self.root / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def install(self):
        self.vendor.mkdir(parents=True, exist_ok=True)
        child = self.root / 'service_child.py'
        shutil.copyfile(Path(__file__), child)
        run(['git', '-C', str(self.root), 'init', '--quiet'])
        for name in ['original', 'candidate']:
            self.releases[name] = self.build_release(name)
        (self.root / 'current').symlink_to(self.root / 'releases/original')
        for unit in ALL_UNITS:
            if unit.endswith('.timer'):
                content = f'''[Unit]
Description=Owned Twinevia runtime containment timer
[Timer]
OnActiveSec=1h
OnCalendar=2099-01-01
Persistent=true
Unit={unit.removesuffix('.timer')}.service
[Install]
WantedBy=timers.target
'''
            else:
                kind = 'simple' if unit in RUNTIME_UNITS else 'oneshot'
                content = f'''[Unit]
Description=Owned Twinevia runtime containment service
[Service]
Type={kind}
WorkingDirectory={self.root}/current
ExecStart=/usr/bin/python3 {child} child --root {self.root} --unit {unit}
Environment=TWINEVIA_SYSTEMD_TEST_OWNER={self.identity}
TimeoutStartSec=120
TimeoutStopSec=5
KillMode=control-group
PrivateNetwork=true
IPAddressDeny=any
RestrictAddressFamilies=AF_UNIX
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths={self.root}
'''
                if unit in RUNTIME_UNITS:
                    content += '[Install]\nWantedBy=multi-user.target\n'
            path = self.vendor / unit
            if path.exists() or path.is_symlink():
                raise RuntimeError(f'Owned test refuses to replace existing {path}.')
            path.write_text(self.owner_marker + '\n' + content)
            self.created_files.append(path)
        self.ctl('daemon-reload')

    def build_release(self, name):
        paths = ['deploy/runtime_control.py', 'deploy/runtime-contract.json',
                 'app/saas_migrations/027_historical_usage_observation_boundary.py']
        for relative in paths:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY / relative, destination)
        (self.root / 'fixture-source-name').write_text(name)
        run(['git', '-C', str(self.root), 'add', *paths, 'fixture-source-name'])
        run(['git', '-C', str(self.root), '-c', 'user.name=Owned runtime fixture',
             '-c', 'user.email=runtime@fixture.test', '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', name])
        sha = run(['git', '-C', str(self.root), 'rev-parse', 'HEAD']).stdout.strip()
        archive = subprocess.run(['git', '-C', str(self.root), 'archive', '--format=tar', sha], capture_output=True, check=True).stdout
        target = self.root / 'releases' / name
        target.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(target, filter='data')
        (target / 'release.json').write_text(json.dumps({'release_id': name, 'source_sha': sha}))
        (target / '.release.env').write_text(f'APP_RELEASE_ID={name}\nAPP_RELEASE_SHA={sha}\n')
        return {'path': target, 'sha': sha}

    def switch(self, name):
        temporary = self.root / 'current.next'
        temporary.symlink_to(self.releases[name]['path'])
        os.replace(temporary, self.root / 'current')

    def operating_state(self):
        return {unit: {key: value for key, value in self.properties(unit).items()
                       if key in {'LoadState', 'ActiveState', 'SubState', 'UnitFileState'}}
                for unit in RUNTIME_UNITS}

    def render_owned_units(self, controller, snapshot):
        rendered_directory = self.root / 'rendered-units'
        rendered_directory.mkdir()
        rendered = []
        for unit in ALL_UNITS:
            source = self.vendor / unit
            if source.is_symlink() or not source.read_text().startswith(self.owner_marker + '\n'):
                raise RuntimeError(f'Refusing to render a definition not owned by this fixture: {source}')
            source_bytes = source.read_bytes()
            staged = rendered_directory / unit
            staged.write_bytes(source_bytes)
            before = snapshot['units'].get(unit) or snapshot['paired_services'][unit]
            target = Path('/etc/systemd/system') / unit
            masked = before['unit_file_state'].startswith('masked')
            destination = source if masked else target
            if destination.exists() or destination.is_symlink():
                if (destination not in self.created_files or destination.is_symlink()
                        or not destination.read_text().startswith(self.owner_marker + '\n')):
                    raise RuntimeError(f'Refusing to overwrite an unowned unit definition: {destination}')
            mask_path = None
            if masked:
                directory = '/run/systemd/system' if before['unit_file_state'] == 'masked-runtime' else '/etc/systemd/system'
                mask_path = Path(directory) / unit
                assert mask_path.is_symlink() and os.readlink(mask_path) == '/dev/null', mask_path
                if target != mask_path and (target.exists() or target.is_symlink()):
                    raise RuntimeError(f'Refusing to render behind an unowned overriding definition: {target}')
            controller.render_unit(snapshot, staged, target, vendor_dir=self.vendor)
            if destination not in self.created_files:
                self.created_files.append(destination)
            assert destination.read_bytes() == source_bytes
            if mask_path is not None:
                assert mask_path.is_symlink() and os.readlink(mask_path) == '/dev/null', mask_path
            rendered.append({'unit': unit, 'destination': str(destination),
                             'preserved_mask': str(mask_path) if mask_path else None})
        self.events.append({'operation': 'render_owned_units', 'definitions': rendered})
        self.ctl('daemon-reload')
        return rendered

    def cleanup(self):
        # Exact names were absent before this fixture and only recorded files
        # carrying this fixture's marker may be removed. Never use broad globs.
        self.evidence_directory.mkdir(parents=True, exist_ok=True)
        if self.created_files:
            journal = run(['/usr/bin/journalctl', '--no-pager', '--output=short-iso', '--lines=150',
                           *[argument for unit in ALL_UNITS for argument in ['--unit', unit]]], check=False)
            (self.evidence_directory / 'owned-unit-journal.log').write_text(journal.stdout + journal.stderr)
        if self.created_files:
            self.ctl('stop', *ALL_UNITS, check=False)
            still_running = [unit for unit in ALL_UNITS if self.properties(unit)['ActiveState'] in {'active', 'activating', 'deactivating'}]
            if still_running:
                (self.evidence_directory / 'systemctl-transcript.json').write_text(json.dumps(self.events, indent=2) + '\n')
                raise RuntimeError('Fixture cleanup could not stop owned units; retaining source for VM teardown: ' + ', '.join(still_running))
            self.ctl('disable', *RUNTIME_UNITS, check=False)
            self.ctl('disable', '--runtime', *RUNTIME_UNITS, check=False)
            self.ctl('unmask', *ALL_UNITS, check=False)
            self.ctl('unmask', '--runtime', *ALL_UNITS, check=False)
            self.ctl('clean', '--what=state', *[unit for unit in RUNTIME_UNITS if unit.endswith('.timer')], check=False)
            for path in self.created_files:
                if path.exists() and path.read_text().startswith(self.owner_marker + '\n'):
                    path.unlink()
                elif path.exists() or path.is_symlink():
                    raise RuntimeError(f'Refusing to remove a file no longer owned by this fixture: {path}')
            self.ctl('daemon-reload')
            self.ctl('reset-failed', *ALL_UNITS, check=False)
        (self.evidence_directory / 'systemctl-transcript.json').write_text(json.dumps(self.events, indent=2) + '\n')
        for name in ['process-ledger.jsonl', 'fake-financial-ledger.jsonl']:
            path = self.root / name
            if path.exists():
                shutil.copyfile(path, self.evidence_directory / name)
        shutil.rmtree(self.root)


def load_control():
    spec = importlib.util.spec_from_file_location('owned_runtime_control', REPOSITORY / 'deploy/runtime_control.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_suite(fixture):
    control = load_control()
    controller = control.RuntimeController(fixture.root, fixture.root / 'control-state',
                                          systemctl=control.Systemctl(binary=str(fixture.systemctl)))
    results = []
    fresh = controller.capture(kind='deploy', fresh_install=True)
    fixture.install()
    controller.activate(fresh, fresh_install=True)
    controller.assert_state(fresh)
    results.append({'case': 'explicit fresh defaults', 'units': fixture.operating_state()})

    fixture.ctl('stop', *RUNTIME_UNITS)
    fixture.ctl('enable', *RUNTIME_UNITS)
    held = ['twinevia-saas-billing-reconcile.timer', 'twinevia-saas-scheduler.timer',
            'twinevia-saas-backup.timer', 'twinevia-saas-readiness.timer']
    fixture.ctl('disable', 'twinevia-saas-scheduler.timer')
    fixture.ctl('mask', 'twinevia-saas-backup.timer')
    fixture.ctl('mask', '--runtime', 'twinevia-saas-readiness.timer')
    fixture.ctl('disable', 'twinevia-saas-platform-restart-queue.timer')
    fixture.ctl('enable', '--runtime', 'twinevia-saas-platform-restart-queue.timer')
    fixture.ctl('start', *[unit for unit in RUNTIME_UNITS if unit not in held])
    fixture.ctl('disable', 'twinevia-saas-a2p-reconcile.timer')
    sha = fixture.releases['original']['sha']
    controller.pause(held, reason='Owned CI financial/background containment', expected_sha=sha)
    before = fixture.operating_state()
    billing_entered = fixture.properties('twinevia-saas-billing-reconcile.timer')['ActiveEnterTimestampMonotonic']
    fixture.ctl('start', '--no-block', 'twinevia-saas-billing-reconcile.service')
    fixture.wait(lambda: any(row['unit'] == 'twinevia-saas-billing-reconcile.service' for row in fixture.ledger()),
                 description='network-denied old-source oneshot startup')
    running = fixture.properties('twinevia-saas-billing-reconcile.service')
    old_pid = next(row['pid'] for row in reversed(fixture.ledger()) if row['unit'] == 'twinevia-saas-billing-reconcile.service')
    assert running['ActiveState'] == 'activating' and str(old_pid) in {running['MainPID'], running['ControlPID']}, running
    snapshot = controller.capture(kind='deploy')
    rendered = fixture.render_owned_units(controller, snapshot)
    assert len(rendered) == len(ALL_UNITS)
    assert fixture.operating_state() == before
    assert fixture.properties('twinevia-saas-billing-reconcile.timer')['ActiveEnterTimestampMonotonic'] == billing_entered
    results.append({'case': 'render all 14 definitions preserves raw states and both mask types',
                    'units': fixture.operating_state(), 'definitions': rendered})
    controller.quiesce(snapshot, rollback=True)
    fixture.wait(lambda: oneshot_is_contained(fixture.properties('twinevia-saas-billing-reconcile.service'),
                                             old_pid, fixture.ledger('fake-financial-ledger.jsonl')),
                 description='contained old-source oneshot termination')
    contained_oneshot = fixture.properties('twinevia-saas-billing-reconcile.service')
    assert oneshot_is_contained(contained_oneshot, old_pid, fixture.ledger('fake-financial-ledger.jsonl'))
    assert fixture.ledger('fake-financial-ledger.jsonl') == []
    fixture.switch('candidate')
    controller.activate(snapshot)
    controller.assert_state(snapshot)
    assert fixture.operating_state() == before
    assert fixture.properties('twinevia-saas-billing-reconcile.timer')['ActiveEnterTimestampMonotonic'] == billing_entered
    sys.path.insert(0, str(REPOSITORY))
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    from flask import Flask
    from app.services.readiness_service import _runtime_unit_facts
    readiness_app = Flask('owned-runtime-systemd-facts')
    readiness_app.config['READINESS_SYSTEMCTL_TIMEOUT_SECONDS'] = 5
    facts = _runtime_unit_facts(readiness_app)
    assert set(facts) == set(RUNTIME_UNITS)
    assert all(not facts[unit].get('error') for unit in RUNTIME_UNITS), facts
    for unit in RUNTIME_UNITS:
        assert facts[unit]['active_state'] == before[unit]['ActiveState']
        assert facts[unit]['unit_file_state'] == before[unit]['UnitFileState']
    fixture.wait(lambda: {'twinevia-saas.service', 'twinevia-saas-worker.service'} <= {
        row['unit'] for row in fixture.ledger() if row['source'] == str(fixture.releases['candidate']['path'])},
        description='candidate source execution by web and worker')
    assert all(row['ip_socket_denied'] for row in fixture.ledger())
    results.append({'case': 'promotion preserves raw state and contains activating oneshot',
                    'units': before, 'application_readiness_unit_facts': facts,
                    'old_oneshot_pid': old_pid, 'contained_oneshot': contained_oneshot, 'fake_financial_calls': 0})

    for failure in ['inner promotion health failure', 'outer environment recovery failure']:
        controller.quiesce(snapshot, rollback=True)
        fixture.switch('original')
        controller.restore(snapshot)
        controller.assert_state(snapshot)
        assert fixture.operating_state() == before
        assert fixture.properties('twinevia-saas-billing-reconcile.timer')['ActiveEnterTimestampMonotonic'] == billing_entered
        assert fixture.ledger('fake-financial-ledger.jsonl') == []
        results.append({'case': failure, 'units': fixture.operating_state(), 'fake_financial_calls': 0})

    unsafe = fixture.root / 'releases' / 'unsafe'
    unsafe.mkdir()
    (unsafe / 'release.json').write_text(json.dumps({'release_id': 'unsafe', 'source_sha': 'c' * 40}))
    try:
        controller.verify_target(unsafe)
    except control.RuntimeControlError:
        pass
    else:
        raise AssertionError('Old target without the safety contract was accepted.')
    assert fixture.operating_state() == before
    results.append({'case': 'unsafe standalone rollback refused', 'fake_financial_calls': 0})

    requests = []
    class ReadinessHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('X-Twinevia-Readiness-Token') != 'owned-ci-readiness-token':
                self.send_error(403)
                return
            identity = controller.identity()
            receipt = controller.receipt(identity)
            paused = receipt['held_units'] if receipt else []
            payload = {'release_id': identity['release_id'], 'release_sha': identity['source_sha'],
                       'ready': not paused, 'state': 'maintenance' if paused else 'ready',
                       'maintenance': {'held_units': paused},
                       'checks': [{'name': 'runtime_maintenance', 'ready': not paused},
                                  {'name': 'scheduler_timers', 'ready': not paused},
                                  {'name': 'migrations', 'ready': True}]}
            requests.append(payload)
            encoded = json.dumps(payload).encode()
            self.send_response(503 if paused else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), ReadinessHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_verify = controller.verify_readiness
    os.environ['READINESS_TOKEN'] = 'owned-ci-readiness-token'
    controller.verify_readiness = lambda *args, **kwargs: original_verify(
        *args, url=f'http://127.0.0.1:{server.server_port}/ready?format=json', **kwargs)
    try:
        controller.resume(['twinevia-saas-billing-reconcile.timer'], reason='Reviewed CI repair',
                          expected_sha=sha, request_id='owned-ci-resume')
        resumed_entered = fixture.properties('twinevia-saas-billing-reconcile.timer')['ActiveEnterTimestampMonotonic']
        controller.resume(['twinevia-saas-billing-reconcile.timer'], reason='Reviewed CI repair',
                          expected_sha=sha, request_id='owned-ci-resume')
        assert fixture.properties('twinevia-saas-billing-reconcile.timer')['ActiveEnterTimestampMonotonic'] == resumed_entered
        assert fixture.properties('twinevia-saas-scheduler.timer')['ActiveState'] == 'inactive'
        assert fixture.properties('twinevia-saas-backup.timer')['UnitFileState'] == 'masked'
        assert fixture.properties('twinevia-saas-readiness.timer')['UnitFileState'] == 'masked-runtime'
        assert fixture.properties('twinevia-saas-readiness.timer')['ActiveState'] == 'inactive'
        results.append({'case': 'reviewed partial resume is idempotent and keeps other holds',
                        'readiness_requests': requests, 'fake_financial_calls': 0})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert fixture.ledger('fake-financial-ledger.jsonl') == []
    fixture.evidence_directory.mkdir(parents=True, exist_ok=True)
    (fixture.evidence_directory / 'systemd-results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps({'cases_passed': len(results), 'fake_financial_calls': 0,
                      'evidence_directory': str(fixture.evidence_directory)}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command')
    subparsers.add_parser('check-guard')
    child = subparsers.add_parser('child')
    child.add_argument('--root', required=True)
    child.add_argument('--unit', choices=ALL_UNITS, required=True)
    parser.add_argument('--evidence-dir', default=str(REPOSITORY / 'output/maintenance-recovery-2026-10-27/systemd'))
    arguments = parser.parse_args()
    if arguments.command == 'child':
        return service_child(arguments)
    systemctl = require_owned_github_vm()
    if arguments.command == 'check-guard':
        print('Disposable GitHub-hosted Ubuntu systemd guard passed.')
        return 0
    fixture = OwnedSystemdFixture(systemctl, arguments.evidence_dir)
    try:
        run_suite(fixture)
    finally:
        fixture.cleanup()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, AssertionError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
