"""Independent runtime-control checks through an executable fake systemctl.

No real systemd, production transport, provider or database is used here. The
companion run/test_runtime_systemd.py verifies real unit semantics only in an
explicitly guarded disposable GitHub-hosted Ubuntu VM.
"""
import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from threading import Thread
from types import SimpleNamespace

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
HELPER = REPOSITORY / 'deploy/runtime_control.py'
SYSTEMD_HARNESS = REPOSITORY / 'run/test_runtime_systemd.py'
RUNTIME_UNITS = (
    'twinevia-saas.service', 'twinevia-saas-worker.service',
    'twinevia-saas-scheduler.timer', 'twinevia-saas-billing-reconcile.timer',
    'twinevia-saas-platform-restart-queue.timer', 'twinevia-saas-a2p-reconcile.timer',
    'twinevia-saas-backup.timer', 'twinevia-saas-readiness.timer',
)
BILLING = 'twinevia-saas-billing-reconcile.timer'
BILLING_SERVICE = BILLING.removesuffix('.timer') + '.service'
PAIRS = tuple(name.removesuffix('.timer') + '.service' for name in RUNTIME_UNITS if name.endswith('.timer'))


FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_SYSTEMCTL_STATE'])
s=json.loads(p.read_text())
args=sys.argv[1:]
verb=next((a for a in args if not a.startswith('-')), '')
units=[a for a in args if a.endswith(('.timer','.service')) and not a.startswith('-')]
call={'verb':verb,'argv':args,'units':units}
s['calls'].append(call)
def finish(code=0,output=''):
    p.write_text(json.dumps(s,sort_keys=True))
    if output:print(output)
    raise SystemExit(code)
for f in s.get('failures',[]):
    if f.get('when','before')=='before' and f['verb']==verb and (not f.get('unit') or f['unit'] in units) and f.get('remaining',1)>0:
        f['remaining']=f.get('remaining',1)-1
        finish(f.get('code',9),'injected systemctl failure')
if verb=='daemon-reload':finish()
if verb in {'list-unit-files','list-units'}:
    finish(output='\n'.join(u+' '+v.get('UnitFileState','disabled') for u,v in s['units'].items()))
for unit in units:
    row=s['units'].get(unit,{'Id':unit,'LoadState':'not-found','ActiveState':'inactive','SubState':'dead','UnitFileState':'not-found','MainPID':'0'})
    if verb=='show':
        properties=[]
        for i,a in enumerate(args):
            if a.startswith('--property='):properties.extend(a.split('=',1)[1].split(','))
            elif a in {'--property','-p'} and i+1<len(args):properties.extend(args[i+1].split(','))
        properties=properties or list(row)
        output='\n'.join(row.get(k,'') if '--value' in args else k+'='+str(row.get(k,'')) for k in properties)
        finish(output=output)
    if verb=='is-active':finish(0 if row['ActiveState']=='active' else 3, '' if '--quiet' in args else row['ActiveState'])
    if verb=='is-enabled':finish(0 if row['UnitFileState'] in {'enabled','enabled-runtime','static','indirect','alias','generated','transient'} else 1, '' if '--quiet' in args else row['UnitFileState'])
    if verb=='cat':finish(0 if row['LoadState']=='loaded' else 1, '# isolated fake unit' if row['LoadState']=='loaded' else '')
    if row['LoadState']=='not-found':finish(4,'Unit not found')
    if verb in {'start','restart'}:
        if row['UnitFileState'] in {'masked','masked-runtime'}:finish(1,'Unit is masked')
        if verb=='start' and row['ActiveState']=='active':continue
        row['ActiveState']='active';row['SubState']='waiting' if unit.endswith('.timer') else 'running'
        row['MainPID']='0' if unit.endswith('.timer') else str(1000+len(s['calls']))
        row['starts']=int(row.get('starts',0))+1
        source=str((Path(s['app_root'])/'current').resolve())
        s['executions'].append({'unit':unit,'source':source})
        if unit=='twinevia-saas-billing-reconcile.timer' and s.get('persistent_catchup',True):
            s['provider_calls'].append({'unit':unit.removesuffix('.timer')+'.service','source':source})
    elif verb=='stop':
        row['ActiveState']='inactive';row['SubState']='dead';row['MainPID']='0'
    elif verb=='enable':
        row['UnitFileState']='enabled-runtime' if '--runtime' in args else 'enabled'
    elif verb=='disable':row['UnitFileState']='disabled'
    elif verb=='mask':row['UnitFileState']='masked-runtime' if '--runtime' in args else 'masked';row['LoadState']='masked'
    elif verb=='unmask':row['UnitFileState']='disabled';row['LoadState']='loaded'
    elif verb=='reset-failed':
        if row['ActiveState']=='failed':row['ActiveState']='inactive'
    else:finish(64,'Unsupported fake systemctl verb '+verb)
    s['units'][unit]=row
for f in s.get('failures',[]):
    if f.get('when')=='after' and f['verb']==verb and (not f.get('unit') or f['unit'] in units) and f.get('remaining',1)>0:
        f['remaining']=f.get('remaining',1)-1
        finish(f.get('code',9),'lost systemctl acknowledgement after state change')
finish()
'''


def _load_helper():
    spec = importlib.util.spec_from_file_location('twinevia_runtime_control_under_test', HELPER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def unit_row(unit, *, active='active', enabled='enabled'):
    return {'Id': unit, 'LoadState': 'masked' if enabled.startswith('masked') else 'loaded',
            'ActiveState': active, 'SubState': 'waiting' if active == 'active' and unit.endswith('.timer') else ('running' if active == 'active' else 'dead'),
            'UnitFileState': enabled, 'MainPID': '101' if active == 'active' and unit.endswith('.service') else '0'}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    module = _load_helper()
    root = tmp_path / 'application'
    state_root = tmp_path / 'runtime-state'
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir()
    binary = binary_dir / 'systemctl'
    binary.write_text(FAKE_SYSTEMCTL)
    binary.chmod(0o755)
    (binary_dir / 'python3').symlink_to(sys.executable)
    sudo = binary_dir / 'sudo'
    sudo.write_text('#!' + sys.executable + '\nimport os,sys\nargs=sys.argv[1:]\nwhile args and args[0].startswith("-"):\n option=args.pop(0)\n if option in {"-u","-g"}:args.pop(0)\nif os.environ.get("FAKE_SUDO_ENV_RESET")=="1":\n keep={k:v for k,v in os.environ.items() if k in {"PATH","FAKE_SYSTEMCTL_STATE","FAKE_SUDO_ENV_RESET","PYTHON_DOTENV_DISABLED","PYTHONPATH"}}\n os.environ.clear();os.environ.update(keep)\nos.execvp(args[0],args)\n')
    sudo.chmod(0o755)
    install = binary_dir / 'install'
    install.write_text('#!' + sys.executable + '''
import os,shutil,sys
from pathlib import Path
args=sys.argv[1:];files=[];mode=None
while args:
 value=args.pop(0)
 if value in {'-o','-g'}:args.pop(0)
 elif value=='-m':mode=int(args.pop(0),8)
 elif value.startswith('-'):raise SystemExit('Unsupported isolated install option')
 else:files.append(value)
source,target=map(Path,files)
if os.environ.get('FAKE_INSTALL_FAILURE')=='1':raise SystemExit(42)
shutil.copyfile(source,target)
if mode is not None:target.chmod(mode)
''')
    install.chmod(0o755)
    root.mkdir()
    subprocess.run(['git', '-C', str(root), 'init', '--quiet'], check=True)
    original, sha = _release(root, 'original')
    candidate, new_sha = _release(root, 'candidate')
    (root / 'current').symlink_to(original)
    state = tmp_path / 'fake-systemctl.json'
    state.write_text(json.dumps({'app_root': str(root), 'calls': [], 'executions': [], 'provider_calls': [],
                                'failures': [], 'units': {**{u: unit_row(u) for u in RUNTIME_UNITS},
                                                         **{u: unit_row(u, active='inactive', enabled='static') for u in PAIRS}}}))
    monkeypatch.setenv('FAKE_SYSTEMCTL_STATE', str(state))
    monkeypatch.setenv('PATH', str(binary_dir) + os.pathsep + os.environ['PATH'])
    systemctl = module.Systemctl(binary=str(binary))
    controller = module.RuntimeController(app_root=root, state_root=state_root, systemctl=systemctl)
    return SimpleNamespace(module=module, controller=controller, systemctl=systemctl, root=root,
                           state_root=state_root, state=state, original=original, candidate=candidate,
                           binary=binary, temporary=tmp_path, sha=sha, new_sha=new_sha)


def _release(root, name):
    relative_files = ['deploy/runtime_control.py', 'deploy/runtime_state.sh', 'app/services/twilio_service.py',
                      'app/services/delivery_status_service.py',
                      'app/saas_migrations/027_historical_usage_observation_boundary.py']
    for relative in relative_files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, target)
    contract = root / 'deploy/runtime-contract.json'
    contract.write_text(json.dumps({'version': 1, 'historical_usage_observation_boundary': True,
                                    'schema_min': '027', 'schema_max': '027'}))
    (root / 'fixture-source-name').write_text(name)
    subprocess.run(['git', '-C', str(root), 'add', *relative_files, 'deploy/runtime-contract.json', 'fixture-source-name'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Runtime fixture', '-c', 'user.email=runtime@fixture.test',
                    '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', name], check=True)
    sha = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    archive = subprocess.run(['git', '-C', str(root), 'archive', '--format=tar', sha], capture_output=True, check=True).stdout
    path = root / 'releases' / name
    path.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(path, filter='data')
    (path / 'release.json').write_text(json.dumps({'release_id': name, 'source_sha': sha,
                                                'runtime_control_version': 1, 'rollback_safety_version': 1}))
    (path / '.release.env').write_text(f'APP_RELEASE_ID={name}\nAPP_RELEASE_SHA={sha}\n')
    return path, sha


def _state(runtime):
    return json.loads(runtime.state.read_text())


def _edit(runtime, unit=None, **values):
    state = _state(runtime)
    if unit:
        state['units'][unit].update(values)
    else:
        state.update(values)
    runtime.state.write_text(json.dumps(state))


def _switch(runtime, target):
    (runtime.root / 'current').unlink(missing_ok=True)
    (runtime.root / 'current').symlink_to(target)


def _operating_state(runtime):
    return {unit: (row['ActiveState'], row['UnitFileState']) for unit, row in _state(runtime)['units'].items() if unit in RUNTIME_UNITS}


@pytest.fixture
def readiness(runtime, monkeypatch):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers.get('X-Twinevia-Readiness-Token') == 'runtime-fixture-token'
            identity = runtime.controller.identity()
            receipt = runtime.controller.receipt(identity)
            held = receipt['held_units'] if receipt else []
            report = {'ready': not held, 'state': 'maintenance' if held else 'ready',
                      'release_sha': identity['source_sha'], 'release_id': identity['release_id'],
                      'maintenance': {'held_units': held},
                      'checks': [{'name': 'runtime_maintenance', 'ready': not held},
                                 {'name': 'scheduler_timers', 'ready': not held},
                                 {'name': 'postgresql', 'ready': True}]}
            requests.append(report)
            encoded = json.dumps(report).encode()
            self.send_response(503 if held else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *arguments):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = runtime.controller.verify_readiness
    monkeypatch.setenv('READINESS_TOKEN', 'runtime-fixture-token')
    monkeypatch.setattr(runtime.controller, 'verify_readiness',
                        lambda *arguments, **options: original(*arguments, url=f'http://127.0.0.1:{server.server_port}/ready?format=json', **options))
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize('enabled,active', [('enabled', 'inactive'), ('disabled', 'inactive'), ('masked', 'inactive'),
                                         ('enabled-runtime', 'active'), ('masked-runtime', 'inactive'), ('disabled', 'active')])
def test_systemctl_adapter_retains_raw_active_and_enabled_states(runtime, enabled, active):
    _edit(runtime, BILLING, **unit_row(BILLING, active=active, enabled=enabled))
    result = runtime.systemctl.inspect(BILLING)
    assert result['active_state'] == active
    assert result['unit_file_state'] == enabled
    assert result['load_state'] == ('masked' if enabled.startswith('masked') else 'loaded')


@pytest.mark.parametrize('enabled', ['enabled', 'disabled', 'masked', 'enabled-runtime', 'masked-runtime'])
def test_preserved_pause_never_momentarily_starts_timer_or_changes_enablement(runtime, enabled):
    _edit(runtime, BILLING, **unit_row(BILLING, active='inactive', enabled=enabled))
    runtime.controller.pause([BILLING], reason='Controlled financial maintenance', expected_sha=runtime.sha)
    original_state = _operating_state(runtime)
    snapshot = runtime.controller.capture(kind='deploy')
    runtime.controller.quiesce(snapshot)
    _switch(runtime, runtime.candidate)
    runtime.controller.activate(snapshot)
    runtime.controller.assert_state(snapshot)
    assert _operating_state(runtime) == original_state
    assert _state(runtime)['provider_calls'] == []
    assert not any(call['verb'] in {'enable', 'unmask'} for call in _state(runtime)['calls'])
    assert not any(call['unit'] == BILLING for call in _state(runtime)['executions'])


def test_active_but_disabled_timer_survives_successful_promotion(runtime):
    _edit(runtime, BILLING, UnitFileState='disabled')
    original_state = _operating_state(runtime)
    snapshot = runtime.controller.capture(kind='release')
    runtime.controller.quiesce(snapshot)
    _switch(runtime, runtime.candidate)
    runtime.controller.activate(snapshot)
    runtime.controller.assert_state(snapshot)
    assert _operating_state(runtime) == original_state
    assert not any(call['verb'] == 'enable' for call in _state(runtime)['calls'])
    executions = _state(runtime)['executions']
    assert all(row['source'] == str(runtime.candidate) for row in executions)
    assert {row['unit'] for row in executions} >= {'twinevia-saas.service', 'twinevia-saas-worker.service'}


@pytest.mark.parametrize('inactive_service', ['twinevia-saas.service', 'twinevia-saas-worker.service'])
def test_active_only_restart_preserves_inactive_long_running_service(runtime, inactive_service):
    _edit(runtime, inactive_service, **unit_row(inactive_service, active='inactive', enabled='disabled'))
    before = _operating_state(runtime)
    snapshot = runtime.controller.capture(kind='restart')
    runtime.controller.quiesce(snapshot)
    runtime.controller.activate(snapshot)
    assert _operating_state(runtime) == before
    assert not any(row['unit'] == inactive_service for row in _state(runtime)['executions'])


def test_unknown_paused_timer_blocks_deploy_before_any_mutation(runtime):
    _edit(runtime, BILLING, ActiveState='inactive', SubState='dead')
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.capture(kind='deploy')
    assert not any(call['verb'] not in {'show', 'is-active', 'is-enabled'} for call in _state(runtime)['calls'])
    assert _state(runtime)['provider_calls'] == []
    assert (runtime.root / 'current').resolve() == runtime.original


@pytest.mark.parametrize('unit', [BILLING, BILLING_SERVICE])
@pytest.mark.parametrize('file_state', ['linked', 'linked-runtime'])
def test_external_linked_definitions_block_template_replacement_before_mutation(runtime, unit, file_state):
    _edit(runtime, unit, UnitFileState=file_state)
    before = _operating_state(runtime)
    with pytest.raises(runtime.module.RuntimeControlError, match='linked external definition'):
        runtime.controller.capture(kind='deploy')
    assert _operating_state(runtime) == before
    assert all(call['verb'] == 'show' for call in _state(runtime)['calls'])
    assert _state(runtime)['provider_calls'] == []
    assert (runtime.root / 'current').resolve() == runtime.original


def test_activating_oneshot_is_stopped_and_never_restarted_by_rollback(runtime):
    runtime.controller.pause([BILLING], reason='Controlled financial maintenance', expected_sha=runtime.sha)
    _edit(runtime, BILLING_SERVICE, ActiveState='activating', SubState='start', MainPID='31415')
    snapshot = runtime.controller.capture(kind='rollback')
    runtime.controller.quiesce(snapshot, rollback=True)
    assert _state(runtime)['units'][BILLING_SERVICE]['ActiveState'] == 'inactive'
    _switch(runtime, runtime.candidate)
    runtime.controller.restore(snapshot)
    runtime.controller.assert_state(snapshot)
    assert _state(runtime)['units'][BILLING_SERVICE]['MainPID'] == '0'
    assert not any(row['unit'] == BILLING_SERVICE for row in _state(runtime)['executions'])
    assert _state(runtime)['provider_calls'] == []


def test_containment_stop_failure_is_reported_without_starting_paused_work(runtime):
    runtime.controller.pause([BILLING], reason='Controlled financial maintenance', expected_sha=runtime.sha)
    snapshot = runtime.controller.capture(kind='deploy')
    _edit(runtime, failures=[{'verb': 'stop', 'unit': BILLING_SERVICE, 'remaining': 1, 'code': 9}])
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.quiesce(snapshot, rollback=True)
    assert _state(runtime)['provider_calls'] == []
    assert (runtime.root / 'current').resolve() == runtime.original


def test_explicit_resume_is_bound_to_reviewed_source_and_idempotent_request(runtime, readiness):
    runtime.controller.pause([BILLING], reason='Controlled financial maintenance', expected_sha=runtime.sha)
    before = _state(runtime)['provider_calls']
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.resume([BILLING], reason='Wrong source must fail', expected_sha=runtime.new_sha, request_id='repair-approval')
    assert _state(runtime)['provider_calls'] == before
    runtime.controller.resume([BILLING], reason='Verified repair and schema', expected_sha=runtime.sha, request_id='repair-approval')
    runtime.controller.resume([BILLING], reason='Verified repair and schema', expected_sha=runtime.sha, request_id='repair-approval')
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'active'
    assert len(_state(runtime)['provider_calls']) == 1
    assert len(readiness) == 1 and readiness[0]['state'] == 'maintenance'
    actions = [json.loads(path.read_text()) for path in (runtime.state_root / 'actions').glob('*.json')]
    resumed = [row for row in actions if row['action'] == 'maintenance_resumed']
    assert len(resumed) == 1 and resumed[0]['request_id'] == 'repair-approval'
    assert resumed[0]['actor'] and resumed[0]['at']


def test_repeating_partial_resume_keeps_other_holds_and_original_request(runtime, readiness):
    scheduler = 'twinevia-saas-scheduler.timer'
    runtime.controller.pause([BILLING, scheduler], reason='Two controlled background holds', expected_sha=runtime.sha)
    runtime.controller.resume([BILLING], reason='Billing repair verified', expected_sha=runtime.sha, request_id='partial-review')
    runtime.controller.resume([BILLING], reason='Billing repair verified', expected_sha=runtime.sha, request_id='partial-review')
    assert runtime.controller.receipt()['held_units'] == [scheduler]
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'active'
    assert _state(runtime)['units'][scheduler]['ActiveState'] == 'inactive'
    assert len(_state(runtime)['provider_calls']) == 1


def test_completed_resume_request_cannot_remove_a_later_independent_pause(runtime, readiness):
    runtime.controller.pause([BILLING], reason='First reviewed hold', expected_sha=runtime.sha)
    runtime.controller.resume([BILLING], reason='First repair verified', expected_sha=runtime.sha, request_id='approved-once')
    runtime.controller.pause([BILLING], reason='A later independent incident', expected_sha=runtime.sha)
    later = runtime.controller.receipt()
    calls_before = list(_state(runtime)['provider_calls'])
    runtime.controller.resume([BILLING], reason='First repair verified', expected_sha=runtime.sha, request_id='approved-once')
    assert runtime.controller.receipt() == later
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'inactive'
    assert _state(runtime)['provider_calls'] == calls_before


def test_fresh_resume_without_declared_hold_cannot_report_success_or_mutate_runtime(runtime):
    before = _operating_state(runtime)
    assert runtime.controller.receipt() is None
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.resume([BILLING], reason='A fresh request has no held operation to review',
                                  expected_sha=runtime.sha, request_id='no-hold-review')
    assert _operating_state(runtime) == before
    assert all(call['verb'] == 'show' for call in _state(runtime)['calls'])
    assert _state(runtime)['provider_calls'] == []
    assert not (runtime.state_root / 'resume-requests').exists()
    runtime.controller.pause([BILLING], reason='Later independent incident requires its own review', expected_sha=runtime.sha)
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'inactive'
    assert runtime.controller.receipt()['reason'] == 'Later independent incident requires its own review'
    assert _state(runtime)['provider_calls'] == []


def test_uncertain_resume_start_acknowledgement_recontains_current_unit(runtime, readiness):
    runtime.controller.pause([BILLING], reason='Hold before verified repair', expected_sha=runtime.sha)
    receipt = runtime.controller.receipt()
    _edit(runtime, failures=[{'verb': 'start', 'unit': BILLING, 'remaining': 1, 'when': 'after', 'code': 9}])
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.resume([BILLING], reason='Reviewed repair', expected_sha=runtime.sha, request_id='uncertain-start')
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'inactive'
    assert _state(runtime)['units'][BILLING_SERVICE]['ActiveState'] == 'inactive'
    assert runtime.controller.receipt() == receipt


@pytest.mark.parametrize('failure_point', ['receipt-write', 'receipt-unlink', 'audit-write', 'completed-request-write'])
@pytest.mark.parametrize('acknowledgement', ['before-write', 'after-write'])
def test_resume_finalization_failure_recontains_started_timer_and_preserves_hold(
        runtime, readiness, monkeypatch, failure_point, acknowledgement):
    scheduler = 'twinevia-saas-scheduler.timer'
    held = [BILLING, scheduler] if failure_point == 'receipt-write' else [BILLING]
    receipt = runtime.controller.pause(held, reason='Hold before durable-finalization fault', expected_sha=runtime.sha)
    original_write = runtime.module._write_json
    original_unlink = Path.unlink
    injected = []

    def inject(point, phase):
        if point != failure_point or phase != acknowledgement or injected:
            return
        injected.append((point, phase))
        # A persistent timer may already have launched its paired job when
        # operational evidence fails. Containment must stop both processes.
        _edit(runtime, BILLING_SERVICE, ActiveState='activating', SubState='start', MainPID='31415')
        raise OSError('Injected durable resume finalization failure')

    def failing_write(path, value, *args, **options):
        point = None
        if Path(path) == runtime.controller.maintenance_file:
            point = 'receipt-write'
        elif value.get('action') == 'maintenance_resumed':
            point = 'audit-write'
        elif value.get('status') == 'completed' and Path(path).parent.name == 'resume-requests':
            point = 'completed-request-write'
        inject(point, 'before-write')
        result = original_write(path, value, *args, **options)
        inject(point, 'after-write')
        return result

    def failing_unlink(path, *args, **options):
        point = 'receipt-unlink' if path == runtime.controller.maintenance_file else None
        inject(point, 'before-write')
        result = original_unlink(path, *args, **options)
        inject(point, 'after-write')
        return result

    monkeypatch.setattr(runtime.module, '_write_json', failing_write)
    monkeypatch.setattr(Path, 'unlink', failing_unlink)
    with pytest.raises((OSError, runtime.module.RuntimeControlError)):
        runtime.controller.resume([BILLING], reason='Reviewed repair with stable identity',
                                  expected_sha=runtime.sha, request_id='durable-finalization-review')
    assert injected == [(failure_point, acknowledgement)]
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'inactive'
    assert _state(runtime)['units'][BILLING_SERVICE]['ActiveState'] == 'inactive'
    assert runtime.controller.receipt() == receipt
    requests = list((runtime.state_root / 'resume-requests').glob('*.json'))
    assert len(requests) == 1
    assert json.loads(requests[0].read_text())['status'] == 'review_required'
    calls = list(_state(runtime)['provider_calls'])
    assert len(calls) == 1
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.resume([BILLING], reason='Reviewed repair with stable identity',
                                  expected_sha=runtime.sha, request_id='durable-finalization-review')
    assert _state(runtime)['provider_calls'] == calls
    assert runtime.controller.receipt() == receipt


def test_resume_without_durable_request_identity_cannot_start_work(runtime, readiness):
    runtime.controller.pause([BILLING], reason='Explicit reviewed hold', expected_sha=runtime.sha)
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.resume([BILLING], reason='Review without operation identity', expected_sha=runtime.sha)
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'inactive'
    assert _state(runtime)['provider_calls'] == []


def test_expired_hold_can_be_explicitly_renewed_without_resuming_or_losing_other_holds(runtime):
    scheduler = 'twinevia-saas-scheduler.timer'
    now = datetime.now(timezone.utc)
    runtime.controller.clock = lambda: now
    original = runtime.controller.pause([BILLING, scheduler], reason='Initial maintenance review', expected_sha=runtime.sha)
    later = now + timedelta(hours=25)
    runtime.controller.clock = lambda: later
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.capture(kind='deploy')
    renewed = runtime.controller.pause([BILLING], reason='Explicit next-day maintenance review', expected_sha=runtime.sha)
    assert set(renewed['held_units']) == {BILLING, scheduler}
    assert renewed['transaction_id'] != original['transaction_id']
    assert datetime.fromisoformat(renewed['declared_at']) == later
    assert runtime.controller.capture(kind='deploy')['held_units'] == [scheduler, BILLING]
    assert _state(runtime)['provider_calls'] == []
    assert all(_state(runtime)['units'][unit]['ActiveState'] == 'inactive' for unit in [BILLING, scheduler])


def test_masked_unit_render_preserves_mask_and_writes_owned_vendor_definition(runtime):
    _edit(runtime, BILLING, **unit_row(BILLING, active='inactive', enabled='masked'))
    runtime.controller.pause([BILLING], reason='Mask retained across installation', expected_sha=runtime.sha)
    snapshot = runtime.controller.capture(kind='deploy')
    target = runtime.temporary / 'etc/systemd/system' / BILLING
    target.parent.mkdir(parents=True)
    target.symlink_to('/dev/null')
    rendered = runtime.temporary / 'rendered.timer'
    rendered.write_text('[Unit]\nDescription=Reviewed replacement timer\n')
    vendor = runtime.temporary / 'vendor/systemd/system'
    runtime.controller.render_unit(snapshot, rendered, target, vendor_dir=vendor)
    assert target.is_symlink() and os.readlink(target) == '/dev/null'
    assert (vendor / BILLING).read_text() == rendered.read_text()
    assert _state(runtime)['units'][BILLING]['UnitFileState'] == 'masked'


def test_explicit_fresh_defaults_and_failed_install_cleanup(runtime):
    (runtime.root / 'current').unlink()
    for unit in RUNTIME_UNITS + PAIRS:
        _edit(runtime, unit, LoadState='not-found', ActiveState='inactive', SubState='dead', UnitFileState='not-found', MainPID='0')
    snapshot = runtime.controller.capture(kind='deploy', fresh_install=True)
    _switch(runtime, runtime.candidate)
    for unit in RUNTIME_UNITS:
        _edit(runtime, unit, **unit_row(unit, active='inactive', enabled='disabled'))
    for unit in PAIRS:
        _edit(runtime, unit, **unit_row(unit, active='inactive', enabled='static'))
    runtime.controller.activate(snapshot, fresh_install=True)
    assert all(state == ('active', 'enabled') for state in _operating_state(runtime).values())
    assert not any(row['unit'] in PAIRS for row in _state(runtime)['executions'])
    runtime.controller.quiesce(snapshot, rollback=True)
    runtime.controller.restore(snapshot)
    assert all(state == ('inactive', 'disabled') for state in _operating_state(runtime).values())


def test_unsafe_rollback_target_is_rejected_without_any_runtime_action(runtime):
    unsafe = runtime.root / 'releases' / 'unsafe'
    unsafe.mkdir()
    (unsafe / 'release.json').write_text(json.dumps({'release_id': 'old', 'source_sha': 'c' * 40}))
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.verify_target(unsafe)
    assert _state(runtime)['calls'] == []
    assert _state(runtime)['provider_calls'] == []


def _captured_promotion(runtime):
    runtime.controller.pause([BILLING], reason='Preserved deployment hold', expected_sha=runtime.sha)
    original = _operating_state(runtime)
    snapshot = runtime.controller.capture(kind='deploy')
    runtime.controller.quiesce(snapshot)
    _switch(runtime, runtime.candidate)
    runtime.controller.activate(snapshot)
    state_file = runtime.state_root / 'transactions' / 'captured-original.json'
    runtime.module._write_json(state_file, snapshot)
    return snapshot, state_file, original


SHELL_TRANSPORT = r'''
set -euo pipefail
source "$RUNTIME_LIBRARY"
atomic_link() {
  if [[ "${FAKE_LINK_FAIL:-0}" == "1" ]]; then return 67; fi
  python3 - "$1" "$2" <<'PY'
import os,sys
from pathlib import Path
target,link=Path(sys.argv[1]),Path(sys.argv[2])
temporary=link.with_name(link.name+'.fixture-next')
temporary.symlink_to(target)
os.replace(temporary,link)
PY
}
'''


@pytest.mark.parametrize('failure', ['none', 'paired-stop', 'unsafe-target', 'link-transport', 'restore-start'])
def test_actual_shared_rollback_function_propagates_failures_and_preserves_hold(runtime, failure):
    _snapshot, state_file, original = _captured_promotion(runtime)
    _edit(runtime, BILLING_SERVICE, ActiveState='activating', SubState='start', MainPID='31415')
    target = runtime.original
    if failure == 'paired-stop':
        _edit(runtime, failures=[{'verb': 'stop', 'unit': BILLING_SERVICE, 'remaining': 1, 'code': 9}])
    elif failure == 'unsafe-target':
        target = runtime.root / 'releases' / 'unsafe'
        target.mkdir()
        (target / 'release.json').write_text(json.dumps({'source_sha': 'c' * 40, 'release_id': 'unsafe'}))
    elif failure == 'restore-start':
        _edit(runtime, failures=[{'verb': 'restart', 'unit': 'twinevia-saas.service', 'remaining': 1, 'code': 9}])
    script = runtime.temporary / 'actual-shared-recovery.sh'
    script.write_text(SHELL_TRANSPORT + '\nruntime_restore_release "$RESTORE_TARGET"\nprintf "restoration-complete\\n"\n')
    environment = {**os.environ, 'APP_ROOT': str(runtime.root), 'CURRENT_LINK': str(runtime.root / 'current'),
                   'TWINEVIA_RUNTIME_STATE_FILE': str(state_file), 'RUNTIME_LIBRARY': str(REPOSITORY / 'deploy/runtime_state.sh'),
                   'RESTORE_TARGET': str(target), 'FAKE_LINK_FAIL': '1' if failure == 'link-transport' else '0'}
    result = subprocess.run(['/bin/bash', str(script)], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 if failure == 'none' else result.returncode != 0
    assert ('restoration-complete' in result.stdout) is (failure == 'none')
    assert _state(runtime)['provider_calls'] == []
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'inactive'
    if failure != 'paired-stop':
        assert _state(runtime)['units'][BILLING_SERVICE]['ActiveState'] == 'inactive'
    assert (runtime.root / 'current').resolve() == (runtime.original if failure in {'none', 'restore-start'} else runtime.candidate)
    if failure == 'none':
        assert _operating_state(runtime) == original


def test_actual_nested_shell_entrypoints_share_one_original_snapshot(runtime):
    runtime.controller.pause([BILLING], reason='Original paused billing', expected_sha=runtime.sha)
    original_state = _operating_state(runtime)
    nested = runtime.temporary / 'nested-release.sh'
    nested.write_text('''set -euo pipefail
source "$RUNTIME_LIBRARY"
runtime_transaction release "$0"
printf '%s' "$TWINEVIA_RUNTIME_STATE_FILE" > "$NESTED_CAPTURE_PATH"
runtime_ctl activate
''')
    outer = runtime.temporary / 'outer-deployment.sh'
    outer.write_text(SHELL_TRANSPORT + '''
runtime_transaction deploy "$0"
printf '%s' "$TWINEVIA_RUNTIME_STATE_FILE" > "$OUTER_CAPTURE_PATH"
runtime_ctl quiesce
atomic_link "$CANDIDATE_RELEASE" "$CURRENT_LINK"
/bin/bash "$NESTED_SCRIPT"
''')
    outer_path, nested_path = runtime.temporary / 'outer-capture-path', runtime.temporary / 'nested-capture-path'
    environment = {**os.environ, 'APP_ROOT': str(runtime.root), 'CURRENT_LINK': str(runtime.root / 'current'),
                   'TWINEVIA_RUNTIME_STATE_ROOT': str(runtime.state_root),
                   'RUNTIME_LIBRARY': str(REPOSITORY / 'deploy/runtime_state.sh'),
                   'CANDIDATE_RELEASE': str(runtime.candidate), 'NESTED_SCRIPT': str(nested),
                   'OUTER_CAPTURE_PATH': str(outer_path), 'NESTED_CAPTURE_PATH': str(nested_path)}
    environment.pop('TWINEVIA_RUNTIME_STATE_FILE', None)
    result = subprocess.run(['/bin/bash', str(outer)], env=environment, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outer_path.read_text() == nested_path.read_text()
    snapshots = list((runtime.state_root / 'transactions').glob('*.json'))
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].read_text())
    assert snapshot['before_release']['source_sha'] == runtime.sha
    assert snapshot['units'][BILLING]['active_state'] == 'inactive'
    assert snapshot['units']['twinevia-saas.service']['active_state'] == 'active'
    assert _operating_state(runtime) == original_state
    assert _state(runtime)['provider_calls'] == []


def test_shared_shell_helper_is_physically_bound_before_current_link_changes(runtime):
    legacy = runtime.root / 'releases' / 'legacy-without-runtime-helper'
    legacy.mkdir()
    script = runtime.temporary / 'physical-helper-binding.sh'
    script.write_text('''set -euo pipefail
source "$APP_ROOT/current/deploy/runtime_state.sh"
helper_before="$_TWINEVIA_RUNTIME_HELPER"
python3 - "$APP_ROOT/current" "$LEGACY_TARGET" <<'PY'
import os,sys
from pathlib import Path
link=Path(sys.argv[1]);temporary=link.with_name('current.fixture-next')
temporary.symlink_to(sys.argv[2]);os.replace(temporary,link)
PY
runtime_ctl units
printf '%s\\n' "$helper_before" >&2
''')
    environment = {**os.environ, 'APP_ROOT': str(runtime.root), 'LEGACY_TARGET': str(legacy),
                   'TWINEVIA_RUNTIME_STATE_FILE': str(runtime.temporary / 'unused-state.json')}
    result = subprocess.run(['/bin/bash', str(script)], env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(result.stdout.splitlines()) == set(RUNTIME_UNITS)
    assert str(runtime.original / 'deploy/runtime_control.py') in result.stderr
    assert '/current/' not in result.stderr
    assert _state(runtime)['calls'] == []


def _actual_shell_function(relative, name):
    source = (REPOSITORY / relative).read_text()
    function = re.search(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}', source, flags=re.MULTILINE | re.DOTALL)
    assert function, f'The actual recovery function {name} is missing from {relative}.'
    return function.group(0)


def _release_shell_transport():
    options = next(line for line in (REPOSITORY / 'deploy/release_twinevia_saas.sh').read_text().splitlines() if line.startswith('set -'))
    return SHELL_TRANSPORT.replace('set -euo pipefail', options, 1)


@pytest.mark.parametrize('trigger', ['health', 'readiness', 'unexpected-ERR'])
def test_actual_inner_release_error_trap_keeps_original_pause(runtime, trigger):
    _snapshot, state_file, original = _captured_promotion(runtime)
    script = runtime.temporary / 'actual-inner-trap.sh'
    setup = '''
promoted=1
runtime_quiesced=1
old_release="$ORIGINAL_RELEASE"
release_id=owned-candidate
health_ready=0
readiness_ready=0
'''
    if trigger == 'unexpected-ERR':
        body = _actual_shell_function('deploy/release_twinevia_saas.sh', 'rollback_on_error') + '''
trap rollback_on_error ERR
python3 -c 'raise SystemExit(17)'
'''
    else:
        body = _actual_release_failure_branch(trigger)
    script.write_text(_release_shell_transport() + setup + body)
    environment = {**os.environ, 'APP_ROOT': str(runtime.root), 'CURRENT_LINK': str(runtime.root / 'current'),
                   'TWINEVIA_RUNTIME_STATE_FILE': str(state_file), 'RUNTIME_LIBRARY': str(REPOSITORY / 'deploy/runtime_state.sh'),
                   'ORIGINAL_RELEASE': str(runtime.original), 'FAILURE_TRIGGER': trigger}
    result = subprocess.run(['/bin/bash', str(script)], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == (17 if trigger == 'unexpected-ERR' else 1), result.stdout + result.stderr
    assert (runtime.root / 'current').resolve() == runtime.original, result.stdout + result.stderr
    assert _operating_state(runtime) == original
    assert _state(runtime)['provider_calls'] == []


def _actual_release_failure_branch(trigger):
    source = (REPOSITORY / 'deploy/release_twinevia_saas.sh').read_text()
    start = source.index('if [[ "${' + trigger + '_ready}" != "1" ]]; then')
    following = '\nreadiness_ready=0' if trigger == 'health' else '\ncurrent_real='
    end = source.index(following, start)
    return source[start:end]


def test_actual_inner_error_inside_runtime_ctl_function_triggers_source_recovery(runtime):
    runtime.controller.pause([BILLING], reason='Hold through failed activation', expected_sha=runtime.sha)
    original_state = _operating_state(runtime)
    snapshot = runtime.controller.capture(kind='release')
    runtime.controller.quiesce(snapshot)
    _switch(runtime, runtime.candidate)
    state_file = runtime.state_root / 'transactions' / 'activation-failure.json'
    runtime.module._write_json(state_file, snapshot)
    _edit(runtime, failures=[{'verb': 'restart', 'unit': 'twinevia-saas-worker.service', 'remaining': 1, 'code': 9}])
    script = runtime.temporary / 'actual-inner-function-error.sh'
    script.write_text(_release_shell_transport() + '\n' + _actual_shell_function('deploy/release_twinevia_saas.sh', 'rollback_on_error') + '''
promoted=1
runtime_quiesced=1
old_release="$ORIGINAL_RELEASE"
trap rollback_on_error ERR
runtime_ctl activate
''')
    environment = {**os.environ, 'APP_ROOT': str(runtime.root), 'CURRENT_LINK': str(runtime.root / 'current'),
                   'TWINEVIA_RUNTIME_STATE_FILE': str(state_file), 'RUNTIME_LIBRARY': str(REPOSITORY / 'deploy/runtime_state.sh'),
                   'ORIGINAL_RELEASE': str(runtime.original)}
    result = subprocess.run(['/bin/bash', str(script)], env=environment, capture_output=True, text=True, timeout=25)
    assert result.returncode != 0
    assert (runtime.root / 'current').resolve() == runtime.original, result.stdout + result.stderr
    assert _operating_state(runtime) == original_state
    assert _state(runtime)['provider_calls'] == []


@pytest.mark.parametrize('trigger', ['health', 'readiness', 'unexpected-ERR'])
@pytest.mark.parametrize('stop_fails', [False, True], ids=['contained', 'stop-failure-retains-source'])
def test_actual_fresh_release_failure_stops_running_oneshot_before_removing_source(runtime, trigger, stop_fails):
    (runtime.root / 'current').unlink()
    for unit in RUNTIME_UNITS + PAIRS:
        _edit(runtime, unit, LoadState='not-found', ActiveState='inactive', SubState='dead', UnitFileState='not-found', MainPID='0')
    snapshot = runtime.controller.capture(kind='deploy', fresh_install=True)
    _switch(runtime, runtime.candidate)
    for unit in RUNTIME_UNITS:
        _edit(runtime, unit, **unit_row(unit, active='inactive', enabled='disabled'))
    for unit in PAIRS:
        _edit(runtime, unit, **unit_row(unit, active='inactive', enabled='static'))
    runtime.controller.activate(snapshot, fresh_install=True)
    _edit(runtime, BILLING_SERVICE, ActiveState='activating', SubState='start', MainPID='31415')
    if stop_fails:
        _edit(runtime, failures=[{'verb': 'stop', 'unit': BILLING_SERVICE, 'remaining': 1, 'code': 9}])
    state_file = runtime.state_root / 'transactions' / 'fresh-capture.json'
    runtime.module._write_json(state_file, snapshot)
    setup = '''
promoted=1
runtime_quiesced=1
old_release=""
release_id=owned-fresh-candidate
health_ready=0
readiness_ready=0
'''
    body = _actual_release_failure_branch(trigger) if trigger != 'unexpected-ERR' else _actual_shell_function('deploy/release_twinevia_saas.sh', 'rollback_on_error') + '''
trap rollback_on_error ERR
python3 -c 'raise SystemExit(17)'
'''
    script = runtime.temporary / 'actual-fresh-failure.sh'
    script.write_text(_release_shell_transport() + setup + body)
    environment = {**os.environ, 'APP_ROOT': str(runtime.root), 'CURRENT_LINK': str(runtime.root / 'current'),
                   'TWINEVIA_RUNTIME_STATE_FILE': str(state_file), 'RUNTIME_LIBRARY': str(REPOSITORY / 'deploy/runtime_state.sh')}
    result = subprocess.run(['/bin/bash', str(script)], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    if stop_fails:
        assert (runtime.root / 'current').resolve() == runtime.candidate
        assert _state(runtime)['units'][BILLING_SERVICE]['ActiveState'] == 'activating'
    else:
        assert _state(runtime)['units'][BILLING_SERVICE]['ActiveState'] == 'inactive', result.stdout + result.stderr
        assert all(row['ActiveState'] == 'inactive' for row in _state(runtime)['units'].values())
        assert not (runtime.root / 'current').is_symlink()


def test_fresh_capture_rejects_preexisting_orphaned_paired_oneshot(runtime):
    (runtime.root / 'current').unlink()
    for unit in RUNTIME_UNITS + PAIRS:
        _edit(runtime, unit, LoadState='not-found', ActiveState='inactive', SubState='dead', UnitFileState='not-found', MainPID='0')
    _edit(runtime, BILLING_SERVICE, LoadState='loaded', ActiveState='activating', SubState='start', UnitFileState='static', MainPID='31415')
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.capture(kind='deploy', fresh_install=True)
    assert not any(call['verb'] not in {'show'} for call in _state(runtime)['calls'])


def test_rollback_guard_rejects_extra_root_import_shadow(runtime):
    (runtime.original / 'flask.py').write_text('raise RuntimeError("Unreviewed import shadow")\n')
    with pytest.raises(runtime.module.RuntimeControlError):
        runtime.controller.verify_target(runtime.original)
    assert _state(runtime)['calls'] == []


@pytest.mark.parametrize('fresh', [False, True])
def test_sudo_environment_reset_cannot_lose_captured_root_or_fresh_contract(runtime, fresh):
    if fresh:
        (runtime.root / 'current').unlink()
        for unit in RUNTIME_UNITS + PAIRS:
            _edit(runtime, unit, LoadState='not-found', ActiveState='inactive', SubState='dead', UnitFileState='not-found', MainPID='0')
    else:
        runtime.controller.pause([BILLING], reason='Hold through sudo environment reset', expected_sha=runtime.sha)
    output = runtime.temporary / 'child-context.json'
    script = runtime.temporary / 'sanitized-deployment.sh'
    script.write_text('''set -euo pipefail
APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
source "$APP_ROOT/deploy/runtime_state.sh"
runtime_transaction deploy "$0"
python3 - ''' + shlex.quote(str(output)) + ''' <<'PY'
import json,os,sys
from pathlib import Path
snapshot=json.loads(Path(os.environ['TWINEVIA_RUNTIME_STATE_FILE']).read_text())
Path(sys.argv[1]).write_text(json.dumps({'app_root':os.environ.get('APP_ROOT'),
  'fresh':os.environ.get('TWINEVIA_INITIALIZE_RUNTIME','0'),'snapshot':snapshot}))
PY
''')
    environment = {**os.environ, 'APP_ROOT': str(runtime.root), 'TWINEVIA_RUNTIME_STATE_ROOT': str(runtime.state_root),
                   'TWINEVIA_INITIALIZE_RUNTIME': '1' if fresh else '0', 'FAKE_SUDO_ENV_RESET': '1'}
    environment.pop('TWINEVIA_RUNTIME_STATE_FILE', None)
    result = subprocess.run(['/bin/bash', str(script)], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    child = json.loads(output.read_text())
    assert child['app_root'] == str(runtime.root)
    assert child['fresh'] == ('1' if fresh else '0')
    assert child['snapshot']['fresh_install'] is fresh


@pytest.mark.parametrize('failure', ['none', 'original-health', 'both-health', 'containment', 'environment-install', 'restore-start'])
def test_actual_outer_environment_exit_trap_preserves_captured_pause(runtime, failure):
    _snapshot, state_file, original = _captured_promotion(runtime)
    # The inner release trap has selected the original safe source; this is
    # the distinct outer environment rollback that previously restarted all.
    _switch(runtime, runtime.original)
    _edit(runtime, BILLING_SERVICE, ActiveState='activating', SubState='start', MainPID='31415')
    if failure == 'containment':
        _edit(runtime, failures=[{'verb': 'stop', 'unit': BILLING_SERVICE, 'remaining': 1, 'code': 9}])
    elif failure == 'restore-start':
        _edit(runtime, failures=[{'verb': 'restart', 'unit': 'twinevia-saas.service', 'remaining': 1, 'code': 9}])
    original_env, staged_env, target_env = [runtime.temporary / name for name in ['original.env', 'staged.env', 'selected.env']]
    original_env.write_text('FIXTURE_CONFIGURATION=original\n')
    staged_env.write_text('FIXTURE_CONFIGURATION=validated-staged\n')
    target_env.write_text(staged_env.read_text())
    script = runtime.temporary / 'actual-outer-exit.sh'
    script.write_text(SHELL_TRANSPORT + '''
resolve_health_host() { printf 'fixture.test\\n'; }
check_saas_health() {
  cat "$ENV_TARGET_FILE" >> "$HEALTH_SEQUENCE"
  if [[ "$HEALTH_CASE" == "both-health" ]]; then return 1; fi
  if [[ "$HEALTH_CASE" == "original-health" ]] && grep -q '=original' "$ENV_TARGET_FILE"; then return 1; fi
  return 0
}
''' + _actual_shell_function('deploy/deploy_twinevia_saas.sh', 'cleanup_env_stage') + '''
env_installed=1
release_started=1
deployment_completed=0
trap cleanup_env_stage EXIT
exit 17
''')
    health_sequence = runtime.temporary / 'health-sequence'
    environment = {**os.environ, 'APP_ROOT': str(runtime.root), 'CURRENT_LINK': str(runtime.root / 'current'),
                   'TWINEVIA_RUNTIME_STATE_FILE': str(state_file), 'RUNTIME_LIBRARY': str(REPOSITORY / 'deploy/runtime_state.sh'),
                   'ENV_STAGE_FILE': str(staged_env), 'ENV_ORIGINAL_FILE': str(original_env),
                   'ENV_TARGET_FILE': str(target_env), 'APP_GROUP': 'fixture', 'HEALTH_CASE': failure,
                   'HEALTH_SEQUENCE': str(health_sequence), 'FAKE_INSTALL_FAILURE': '1' if failure == 'environment-install' else '0'}
    result = subprocess.run(['/bin/bash', str(script)], env=environment, capture_output=True, text=True, timeout=25)
    assert result.returncode != 0
    assert (runtime.root / 'current').resolve() == runtime.original
    assert _state(runtime)['units'][BILLING]['ActiveState'] == 'inactive'
    assert _state(runtime)['provider_calls'] == []
    if failure in {'none', 'original-health', 'both-health'}:
        assert _operating_state(runtime) == original
        expected_configuration = 'original' if failure == 'none' else 'validated-staged'
        assert target_env.read_text() == f'FIXTURE_CONFIGURATION={expected_configuration}\n'
    if failure in {'original-health', 'both-health'}:
        assert health_sequence.read_text().splitlines() == ['FIXTURE_CONFIGURATION=original', 'FIXTURE_CONFIGURATION=validated-staged']
    if failure in {'containment', 'environment-install'}:
        assert target_env.read_text() == 'FIXTURE_CONFIGURATION=validated-staged\n'
        assert not health_sequence.exists()


def test_real_systemd_harness_refuses_local_or_untrusted_environment(tmp_path):
    environment = {key: value for key, value in os.environ.items() if key not in {'GITHUB_ACTIONS', 'CI', 'RUNNER_ENVIRONMENT'}}
    result = subprocess.run([sys.executable, str(SYSTEMD_HARNESS), 'check-guard'], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'Refusing systemd tests' in result.stderr
    assert not (tmp_path / 'systemctl-transcript.json').exists()


@pytest.mark.parametrize('command,code,stdout,stderr,accepted', [
    ('list-unit-files', 0, '', '', True),
    ('list-unit-files', 1, '', '', True),
    ('list-unit-files', 2, '', '', False),
    ('list-unit-files', 127, '', '', False),
    ('list-unit-files', 1, '', 'Failed to connect to bus: Host is down', False),
    ('list-unit-files', 0, '', 'Failed to list unit files', False),
    ('list-unit-files', 1, 'Unexpected inventory diagnostic', '', False),
    ('list-unit-files', 0, 'twinevia-saas.service enabled enabled', '', False),
    ('list-unit-files', 1, 'twinevia-saas-backup.timer masked enabled', '', False),
    ('list-units', 1, '', '', False),
    ('list-units', 0, '', 'Failed to connect to bus: No such file or directory', False),
    ('list-units', 0, 'twinevia-saas-worker.service loaded active running', '', False),
])
def test_real_systemd_inventory_guard_handles_clean_no_matches_but_rejects_uncertainty(
        tmp_path, monkeypatch, command, code, stdout, stderr, accepted):
    spec = importlib.util.spec_from_file_location('owned_systemd_harness_under_test', SYSTEMD_HARNESS)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    responses = {name: {'code': 0, 'stdout': '', 'stderr': ''} for name in ['list-unit-files', 'list-units']}
    responses[command] = {'code': code, 'stdout': stdout, 'stderr': stderr}
    configuration = tmp_path / 'responses.json'
    configuration.write_text(json.dumps(responses))
    calls = tmp_path / 'calls.jsonl'
    binary = tmp_path / 'systemctl'
    binary.write_text('#!' + sys.executable + '''
import json,os,sys
from pathlib import Path
with Path(os.environ['OWNED_INVENTORY_CALLS']).open('a') as output:
    output.write(json.dumps(sys.argv[1:])+'\\n')
result=json.loads(Path(os.environ['OWNED_INVENTORY_RESPONSES']).read_text())[sys.argv[1]]
sys.stdout.write(result['stdout']);sys.stderr.write(result['stderr'])
raise SystemExit(result['code'])
''')
    binary.chmod(0o755)
    monkeypatch.setenv('OWNED_INVENTORY_RESPONSES', str(configuration))
    monkeypatch.setenv('OWNED_INVENTORY_CALLS', str(calls))
    if accepted:
        harness.require_empty_systemd_inventory(binary)
    else:
        with pytest.raises(RuntimeError, match='Refusing systemd tests'):
            harness.require_empty_systemd_inventory(binary)
    arguments = [json.loads(line) for line in calls.read_text().splitlines()]
    assert arguments
    assert all(row[0] in {'list-unit-files', 'list-units'} for row in arguments)
    assert all(row[1:] == ['--all', '--no-legend', '--no-pager', 'twinevia-saas*'] for row in arguments)
    if accepted:
        assert [row[0] for row in arguments] == ['list-unit-files', 'list-units']


@pytest.mark.parametrize('state,main_pid,control_pid,old_alive,financial_calls,accepted', [
    ('inactive', '0', '0', False, [], True),
    ('failed', '0', '0', False, [], True),
    ('failed', '0', '0', True, [], False),
    ('failed', '123', '0', False, [], False),
    ('failed', '0', '123', False, [], False),
    ('failed', 'unknown', '0', False, [], False),
    ('inactive', '0', '0', False, [{'event': 'fake_financial_operation'}], False),
    ('active', '0', '0', False, [], False),
    ('activating', '0', '0', False, [], False),
    ('deactivating', '0', '0', False, [], False),
])
def test_real_systemd_oneshot_containment_requires_terminal_state_zero_pids_and_no_financial_work(
        monkeypatch, state, main_pid, control_pid, old_alive, financial_calls, accepted):
    spec = importlib.util.spec_from_file_location('owned_systemd_containment_under_test', SYSTEMD_HARNESS)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    properties = {'ActiveState': state, 'MainPID': main_pid, 'ControlPID': control_pid, 'Result': 'signal'}
    original = dict(properties)

    def probe_old_pid(pid, signal):
        assert (pid, signal) == (31415, 0)
        if not old_alive:
            raise ProcessLookupError('The contained child exited')

    monkeypatch.setattr(harness.os, 'kill', probe_old_pid)
    assert harness.oneshot_is_contained(properties, 31415, financial_calls) is accepted
    assert properties == original


def test_real_systemd_cleanup_removes_exact_owned_runtime_enablement_and_masks(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('owned_systemd_cleanup_under_test', SYSTEMD_HARNESS)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    fixture = harness.OwnedSystemdFixture.__new__(harness.OwnedSystemdFixture)
    fixture.root = tmp_path / 'owned-fixture'
    fixture.root.mkdir()
    fixture.evidence_directory = tmp_path / 'evidence'
    fixture.owner_marker = '# owned-cleanup-regression'
    definition = fixture.root / 'owned-unit-definition'
    definition.write_text(fixture.owner_marker + '\n[Unit]\nDescription=Owned test\n')
    fixture.created_files = [definition]
    fixture.events = []
    calls = []

    def ctl(*arguments, **options):
        calls.append(arguments)
        return SimpleNamespace(stdout='', stderr='', returncode=0)

    fixture.ctl = ctl
    fixture.properties = lambda unit: {'ActiveState': 'inactive'}
    monkeypatch.setattr(harness, 'run', lambda *args, **options: SimpleNamespace(stdout='owned journal', stderr='', returncode=0))
    fixture.cleanup()
    assert ('disable', '--runtime', *RUNTIME_UNITS) in calls
    assert ('unmask', '--runtime', *(RUNTIME_UNITS + PAIRS)) in calls
    assert ('disable', *RUNTIME_UNITS) in calls
    assert ('unmask', *(RUNTIME_UNITS + PAIRS)) in calls
    assert all(set(argument for argument in call if argument.endswith(('.service', '.timer'))) <= set(RUNTIME_UNITS + PAIRS)
               for call in calls)
    assert not fixture.root.exists()
