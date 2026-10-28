"""State-preserving Twinevia systemd operations; no application/provider imports.

Existing installations never acquire enablement or lose an explicit pause merely
because their code is deployed, rolled back, or restarted. Root-owned receipts
describe intentional maintenance; public readiness remains false during a hold.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


SERVICE_UNITS = ('twinevia-saas.service', 'twinevia-saas-worker.service')
TIMER_UNITS = tuple('twinevia-saas-' + name + '.timer' for name in (
    'scheduler', 'billing-reconcile', 'platform-restart-queue', 'a2p-reconcile', 'backup', 'readiness',
))
RUNTIME_UNITS = SERVICE_UNITS + TIMER_UNITS
PAIRED_SERVICES = {unit: unit.removesuffix('.timer') + '.service' for unit in TIMER_UNITS}
DEFAULT_STATE_ROOT = '/var/lib/twinevia-saas/runtime'
DEFAULT_MAINTENANCE_FILE = '/var/lib/twinevia-saas/runtime-maintenance.json'
SHA_PATTERN = re.compile(r'^[a-f0-9]{40}$')
FILE_STATES = {'enabled', 'enabled-runtime', 'disabled', 'masked', 'masked-runtime', 'static', 'indirect', 'linked', 'linked-runtime'}
LEGACY_SAFE_SHA = 'ad7814b2946f3bec1c4e543e297e2e443340f619'
FEEDBACK_PRIVACY_TRANSITION_SHA = '25cd1109cca886848f61425dd18439d6b8bbc9ff'
LEGACY_SAFETY_HASHES = {
    'app/models.py': 'd3af855a62d6e4cf271620bc48771e1894938c7daaacc8c3281fb5d314f62ccb',
    'app/services/delivery_status_service.py': '0b544eec2fe986298aeb15bcd649f39230fb51e85a00a19b09f9c81ce3252c7b',
    'app/services/twilio_service.py': '11b720167b5434b32ffa92a195a5518764cde0efbe1dae9d324e00ec71b7a93b',
    'app/services/billing_service.py': '81502bedb3bfe367d45a60889a3e3f2dc0b9ab204d63bc408d2f9939c0eb14b0',
    'app/services/suppression_service.py': '4f94a145485f802c4e1115777ed51f79fdb8e55b32c96ba3304f39f620678b24',
    'app/saas_migrations/027_historical_usage_observation_boundary.py': '98c537d550ee3609c8c6c8d7fc8a433ec6e8e1394b4b236728c04fa2ce499b2f',
}


class RuntimeControlError(RuntimeError):
    pass


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise RuntimeControlError('Runtime timestamps must include their UTC offset.')
    return value.astimezone(timezone.utc)


def _json_file(path, trusted_uid=None):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != (os.geteuid() if trusted_uid is None else trusted_uid) or info.st_mode & 0o022:
                raise RuntimeControlError('Operational state must be owned by its trusted operator and not writable by others.')
            raw = stream.read(131073)
            if len(raw) > 131072:
                raise RuntimeControlError('Operational state exceeds the supported size.')
            value = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise RuntimeControlError(f'Cannot read trusted operational state: {path.name}.') from exc
    if not isinstance(value, dict):
        raise RuntimeControlError('Operational state must be a JSON object.')
    return value


def read_maintenance_receipt(path, *, release_sha=None, trusted_uid=0, now=None, allow_expired=False):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None
    value = _json_file(path, trusted_uid)
    held = value.get('held_units')
    states = value.get('states')
    if (value.get('version') != 1 or not isinstance(held, list) or not held or not all(isinstance(unit, str) for unit in held)
            or len(set(held)) != len(held) or not set(held) <= set(TIMER_UNITS)
            or not isinstance(states, dict) or set(states) != set(held)
            or not str(value.get('reason') or '').strip() or not str(value.get('actor') or '').strip()
            or not SHA_PATTERN.fullmatch(str(value.get('release_sha') or ''))
            or not str(value.get('release_id') or '').strip()):
        raise RuntimeControlError('Maintenance intent is malformed or does not identify its scope and operator.')
    if release_sha is not None and value['release_sha'] != release_sha:
        raise RuntimeControlError('Maintenance intent belongs to another running release.')
    try:
        declared = _utc(datetime.fromisoformat(value['declared_at']))
        expires = _utc(datetime.fromisoformat(value['expires_at']))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeControlError('Maintenance intent has invalid timestamps.') from exc
    if declared > _utc(now) + timedelta(seconds=30) or (expires <= _utc(now) and not allow_expired) or expires <= declared:
        raise RuntimeControlError('Maintenance intent is expired or not yet valid; work remains paused until explicit review.')
    for unit in held:
        if not isinstance(states[unit], dict) or states[unit].get('active_state') != 'inactive' or states[unit].get('unit_file_state') not in FILE_STATES:
            raise RuntimeControlError('Maintenance intent does not describe an inactive timer and its enablement.')
    return value


def _write_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        _json_file(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist the rename and a newly created containing directory before an
        # acknowledgement can authorize another operation after host recovery.
        for directory in (path.parent, path.parent.parent):
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Systemctl:
    def __init__(self, binary='systemctl', run=None):
        self.binary, self.run = binary, run or subprocess.run

    def command(self, *args, check=True):
        result = self.run([self.binary, *args], capture_output=True, text=True, timeout=45, check=False)
        if check and result.returncode:
            raise RuntimeControlError(f'systemctl {args[0]} failed; operation stopped without claiming recovery.')
        return result

    def inspect(self, unit):
        result = self.command('show', unit, '--no-pager', '--property=LoadState', '--property=ActiveState',
                              '--property=SubState', '--property=UnitFileState', '--property=MainPID', check=False)
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        value = {key: fields.get(raw, 'unknown') for key, raw in (
            ('load_state', 'LoadState'), ('active_state', 'ActiveState'), ('sub_state', 'SubState'), ('unit_file_state', 'UnitFileState'), ('main_pid', 'MainPID'))}
        if result.returncode and value['load_state'] != 'not-found':
            raise RuntimeControlError(f'Cannot inspect runtime unit {unit}.')
        return value

    def start(self, unit): self.command('start', unit)
    def stop(self, unit): self.command('stop', unit)
    def restart(self, unit): self.command('restart', unit)
    def enable(self, unit): self.command('enable', unit)


class RuntimeController:
    def __init__(self, app_root, state_root=None, systemctl=None, clock=None):
        self.app_root = Path(app_root).resolve()
        self.state_root = Path(state_root or DEFAULT_STATE_ROOT).resolve()
        self.maintenance_file = self.state_root.parent / 'runtime-maintenance.json'
        self.systemctl = systemctl or Systemctl()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self): return _utc(self.clock())

    def current(self):
        link = self.app_root / 'current'
        return link.resolve(strict=True) if link.is_symlink() else None

    def identity(self, path=None):
        path = path or self.current()
        if path is None:
            return None
        try:
            value = json.loads((Path(path) / 'release.json').read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeControlError('Release identity is missing; retain the source and review recovery before activation.') from exc
        if not isinstance(value, dict) or not SHA_PATTERN.fullmatch(str(value.get('source_sha') or '')) or not value.get('release_id'):
            raise RuntimeControlError('Release identity is invalid.')
        return {**value, 'path': str(Path(path).resolve())}

    def audit(self, action, **details):
        folder = self.state_root / 'actions'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_json(folder / (uuid4().hex + '.json'), {'action': action, 'at': self.now().isoformat(),
                    'actor': os.environ.get('SUDO_USER') or os.environ.get('USER') or str(os.geteuid()), **details})

    def receipt(self, identity=None, *, allow_expired=False):
        identity = identity or self.identity()
        return read_maintenance_receipt(self.maintenance_file, release_sha=identity['source_sha'] if identity else None,
                                        trusted_uid=os.geteuid(), now=self.now(), allow_expired=allow_expired)

    def capture(self, kind='deploy', fresh_install=False):
        units = {unit: self.systemctl.inspect(unit) for unit in RUNTIME_UNITS}
        paired = {unit: self.systemctl.inspect(unit) for unit in PAIRED_SERVICES.values()}
        identity = self.identity()
        if fresh_install:
            if identity is not None or any(row['load_state'] != 'not-found' for row in (*units.values(), *paired.values())):
                raise RuntimeControlError('Default activation is restricted to a genuinely fresh installation.')
        else:
            if identity is None:
                raise RuntimeControlError('Existing runtime has no reviewed release identity; reconcile it before changing operating state.')
            for unit, row in units.items():
                if row['load_state'] not in {'loaded', 'masked'} or row['active_state'] not in {'active', 'inactive'} or row['unit_file_state'] not in FILE_STATES:
                    raise RuntimeControlError(f'{unit} is missing or transitioning; inspect it before changing runtime code.')
                if row['active_state'] == 'active' and row['unit_file_state'].startswith('masked'):
                    raise RuntimeControlError(f'{unit} is active and masked; declare a pause or review its mask before recycling it.')
                if kind != 'restart' and row['unit_file_state'] in {'linked', 'linked-runtime'}:
                    raise RuntimeControlError(f'{unit} uses a linked external definition; preserve and review that unit before installing templates.')
            if kind != 'restart':
                for unit, row in paired.items():
                    if row['unit_file_state'] in {'linked', 'linked-runtime'}:
                        raise RuntimeControlError(f'{unit} uses a linked external definition; review it before installing templates.')
                for unit in SERVICE_UNITS:
                    if units[unit]['active_state'] != 'active' or units[unit]['unit_file_state'].startswith('masked'):
                        raise RuntimeControlError(f'{unit} cannot be recycled while preserving its current state; explicitly recover it first.')
        held = [unit for unit in TIMER_UNITS if units[unit]['active_state'] == 'inactive'] if not fresh_install else []
        receipt = self.receipt(identity) if not fresh_install else None
        if kind in {'deploy', 'release', 'rollback', 'install'} and (held or receipt):
            if receipt is None or set(receipt['held_units']) != set(held):
                raise RuntimeControlError('Inactive timers have no matching maintenance declaration. Declare/adopt the pause explicitly; deployment will not resume them.')
            for unit in held:
                if units[unit]['unit_file_state'] != receipt['states'][unit]['unit_file_state']:
                    raise RuntimeControlError('Held timer enablement changed after its maintenance declaration.')
        return {'version': 1, 'transaction_id': uuid4().hex, 'kind': kind, 'app_root': str(self.app_root),
                'state_root': str(self.state_root), 'captured_at': self.now().isoformat(), 'actor': os.environ.get('SUDO_USER') or 'root',
                'before_release': identity, 'units': units, 'paired_services': paired, 'held_units': held,
                'maintenance_before': receipt, 'fresh_install': fresh_install, 'owner_pid': os.getpid()}

    def verify_target(self, path, *, transition_snapshot=None):
        path = Path(path).resolve(strict=True)
        if path.parent != (self.app_root / 'releases').resolve():
            raise RuntimeControlError('Recovery target must be a retained release inside this application.')
        for protected in (path, path / 'release.json'):
            info = protected.stat()
            if protected.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise RuntimeControlError('Recovery source/identity is not owned and protected by its trusted operator.')
        identity = self.identity(path)
        sha = identity['source_sha']
        if sha == LEGACY_SAFE_SHA:
            if self.maintenance_file.exists():
                raise RuntimeControlError('The legacy recovery baseline supports normal operation only; keep maintenance contained and use a reviewed repair instead of this code rollback.')
            current_identity = self.identity()
            already_legacy = current_identity and current_identity['source_sha'] == LEGACY_SAFE_SHA
            recovering_first_upgrade = (transition_snapshot is not None
                and (transition_snapshot.get('before_release') or {}).get('source_sha') == LEGACY_SAFE_SHA
                and transition_snapshot.get('kind') in {'deploy', 'release', 'install'}
                and not transition_snapshot.get('held_units'))
            if not already_legacy and not recovering_first_upgrade:
                raise RuntimeControlError('The transition baseline predates the repaired account and survey guards. A standalone rollback would reintroduce known bugs; use a reviewed current repair.')
            if any(not (path / name).is_file() or hashlib.sha256((path / name).read_bytes()).hexdigest() != digest
                   for name, digest in LEGACY_SAFETY_HASHES.items()):
                raise RuntimeControlError('The approved recovery baseline differs from its reviewed safety files.')
            ceiling = '027'
        else:
            try:
                contract = json.loads((path / 'deploy/runtime-contract.json').read_text())
            except (OSError, ValueError) as exc:
                raise RuntimeControlError('Older code lacks the required recovery safety contract. Use a reviewed matching backup restore, not a standalone code rollback.') from exc
            if not isinstance(contract, dict) or contract.get('version') != 1 or contract.get('historical_usage_observation_boundary') is not True:
                raise RuntimeControlError('Recovery target does not declare the required historical observation boundary.')
            if contract.get('logout_session_revocation') is not True or contract.get('active_survey_session_guard') is not True:
                raise RuntimeControlError('Recovery target predates the required account and survey safety guards.')
            ceiling = str(contract.get('schema_max') or '')
            floor = str(contract.get('schema_min') or '')
            if (not re.fullmatch(r'\d{3}', ceiling) or not re.fullmatch(r'\d{3}', floor)
                    or floor > ceiling or floor > '028'):
                raise RuntimeControlError('Recovery target schema contract is invalid.')
            if ceiling >= '028' and (contract.get('feedback_registration_isolation') is not True
                                     or contract.get('staff_phone_privacy') is not True):
                raise RuntimeControlError('Recovery target predates the required feedback and staff contact privacy guards.')
            # Schema 028 is additive, so an interrupted first promotion may
            # recover its exact prior operating release. This is not authority
            # for a later standalone downgrade that would expose staff numbers
            # or forget the feedback/registration boundary. The original root
            # capture and full Git source proof remain required.
            if (sha == FEEDBACK_PRIVACY_TRANSITION_SHA and ceiling == '027'
                    and isinstance(transition_snapshot, dict)):
                before = transition_snapshot.get('before_release') or {}
                units = transition_snapshot.get('units') or {}
                if (transition_snapshot.get('version') == 1
                        and transition_snapshot.get('kind') in {'deploy', 'release', 'install'}
                        and transition_snapshot.get('app_root') == str(self.app_root)
                        and transition_snapshot.get('state_root') == str(self.state_root)
                        and transition_snapshot.get('fresh_install') is False
                        and transition_snapshot.get('held_units') == []
                        and transition_snapshot.get('maintenance_before', 'unavailable') is None
                        and not self.maintenance_file.exists() and not self.maintenance_file.is_symlink()
                        and isinstance(before, dict) and isinstance(units, dict)
                        and before.get('source_sha') == sha and before.get('path') == str(path)
                        and before.get('release_id') == identity['release_id']
                        and set(units) == set(RUNTIME_UNITS)
                        and all(isinstance(row, dict)
                                and row.get('load_state') == 'loaded' and row.get('active_state') == 'active'
                                and row.get('unit_file_state') in FILE_STATES
                                and not row.get('unit_file_state', '').startswith(('masked', 'linked'))
                                for row in units.values())):
                    ceiling = '028'
        current = self.current()
        current_versions = [p.name[:3] for p in (current / 'app/saas_migrations').glob('[0-9][0-9][0-9]_*.py')] if current else []
        if max(current_versions, default='000') > ceiling:
            raise RuntimeControlError('Recovery target has not been reviewed for the current expanded schema.')
        if not (path / 'app/saas_migrations/027_historical_usage_observation_boundary.py').is_file():
            raise RuntimeControlError('Recovery target predates the historical safety boundary.')
        result = subprocess.run(['git', '-c', 'safe.directory=' + str(self.app_root), '-C', str(self.app_root), 'archive', '--format=tar', sha],
                                capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeControlError('Cannot verify retained release bytes against their reviewed Git identity.')
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            expected_names = set()
            for member in archive:
                if not member.isfile() or member.name.startswith('output/'):
                    continue
                file = path / member.name
                expected_names.add(member.name)
                if not file.is_file() or file.is_symlink() or file.read_bytes() != archive.extractfile(member).read():
                    raise RuntimeControlError(f'Retained release source differs from Git at {member.name}; review the hotfix before recovery.')
                info = file.stat()
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise RuntimeControlError('Reviewed source is writable outside its trusted operator; recovery is refused.')
        excluded_roots = {'venv', 'instance', 'output', 'node_modules', '.git'}
        generated_files = {'.env', '.release.env', 'release.json', 'source-preservation.json', 'requirements.resolved.txt'}
        for directory, subdirectories, files in os.walk(path, followlinks=False):
            relative_directory = Path(directory).relative_to(path)
            subdirectories[:] = [name for name in subdirectories if name != '__pycache__' and (relative_directory != Path('.') or name not in excluded_roots)]
            for name in (*subdirectories, *files):
                file = Path(directory) / name
                relative = file.relative_to(path).as_posix()
                if relative_directory == Path('.') and name in generated_files:
                    continue
                if (file.is_file() or file.is_symlink()) and relative not in expected_names:
                    raise RuntimeControlError('Retained release contains an unreviewed source addition; preserve and reconcile its hotfix first.')
        return identity

    def quiesce(self, snapshot, rollback=False):
        for unit in TIMER_UNITS:
            self.systemctl.stop(unit)
        for timer in snapshot['held_units']:
            paired = PAIRED_SERVICES[timer]
            originally_running = snapshot['paired_services'][paired]['active_state'] in {'active', 'activating', 'deactivating'}
            if snapshot.get('maintenance_before') or not originally_running:
                self.systemctl.stop(paired)
        deadline = time.monotonic() + 30
        while True:
            running = [unit for unit in PAIRED_SERVICES.values()
                       if self.systemctl.inspect(unit)['active_state'] in {'active', 'activating', 'deactivating'}]
            if not running:
                break
            if time.monotonic() >= deadline:
                raise RuntimeControlError('A background operation is still running. Timers remain contained; wait for it before switching source.')
            time.sleep(0.2)
        self.audit('runtime_quiesced', transaction_id=snapshot['transaction_id'], rollback=rollback)
        if not snapshot['fresh_install'] and snapshot['units']['twinevia-saas-worker.service']['active_state'] == 'active':
            self.systemctl.stop('twinevia-saas-worker.service')

    def _rebind_receipt(self, snapshot):
        receipt = snapshot.get('maintenance_before')
        if receipt:
            identity = self.identity()
            receipt = {**receipt, 'release_sha': identity['source_sha'], 'release_id': identity['release_id'],
                       'updated_at': self.now().isoformat(), 'transaction_id': snapshot['transaction_id']}
            _write_json(self.maintenance_file, receipt, 0o644)
        elif self.maintenance_file.exists():
            _json_file(self.maintenance_file)
            self.maintenance_file.unlink()

    def activate(self, snapshot, fresh_install=False):
        self.verify_target(self.current())
        if bool(fresh_install) != bool(snapshot['fresh_install']):
            raise RuntimeControlError('Default activation does not match the captured installation state.')
        for unit in RUNTIME_UNITS:
            before = snapshot['units'][unit]
            if fresh_install:
                self.systemctl.enable(unit)
            elif self.systemctl.inspect(unit)['unit_file_state'] != before['unit_file_state']:
                raise RuntimeControlError(f'{unit} enablement changed during deployment; refusing automatic activation.')
            if fresh_install or before['active_state'] == 'active':
                self.systemctl.restart(unit) if unit in SERVICE_UNITS else self.systemctl.start(unit)
            else:
                self.systemctl.stop(unit)
                if unit in PAIRED_SERVICES:
                    self.systemctl.stop(PAIRED_SERVICES[unit])
        self._rebind_receipt(snapshot)
        self.assert_state(snapshot)

    def restore(self, snapshot):
        # Caller quiesces before restoring the source/environment links. Never
        # replay a captured oneshot: its external action may already have happened.
        if snapshot['fresh_install']:
            for unit in RUNTIME_UNITS:
                if self.systemctl.inspect(unit)['load_state'] == 'not-found':
                    continue
                self.systemctl.stop(unit)
                self.systemctl.command('disable', unit)
            for unit in PAIRED_SERVICES.values():
                if self.systemctl.inspect(unit)['load_state'] != 'not-found':
                    self.systemctl.stop(unit)
        else:
            self.verify_target(self.current())
            for unit in RUNTIME_UNITS:
                before = snapshot['units'][unit]
                if self.systemctl.inspect(unit)['unit_file_state'] != before['unit_file_state']:
                    raise RuntimeControlError(f'{unit} enablement no longer matches the original snapshot; manual recovery required.')
                if before['active_state'] == 'active':
                    self.systemctl.restart(unit) if unit in SERVICE_UNITS else self.systemctl.start(unit)
                else:
                    self.systemctl.stop(unit)
                    if unit in PAIRED_SERVICES:
                        self.systemctl.stop(PAIRED_SERVICES[unit])
        self._rebind_receipt(snapshot)
        self.assert_state(snapshot, restoring=True)
        self.audit('runtime_restored', transaction_id=snapshot['transaction_id'])

    def assert_state(self, snapshot, *, restoring=False):
        observed = {unit: self.systemctl.inspect(unit) for unit in RUNTIME_UNITS}
        for unit, row in observed.items():
            before = snapshot['units'][unit]
            if snapshot['fresh_install']:
                if restoring and row['load_state'] == 'not-found':
                    continue
                expected_active, expected_file = ('inactive', 'disabled') if restoring else ('active', 'enabled')
            else:
                expected_active, expected_file = before['active_state'], before['unit_file_state']
            if row['active_state'] != expected_active or row['unit_file_state'] != expected_file:
                raise RuntimeControlError(f'{unit} differs from the reviewed runtime operating state.')
        binary = Path(shutil.which(getattr(self.systemctl, 'binary', 'systemctl')) or '/nonexistent').resolve()
        if sys.platform == 'linux' and binary == Path('/usr/bin/systemctl').resolve():
            for unit in SERVICE_UNITS:
                if observed[unit]['active_state'] != 'active':
                    continue
                try:
                    pid = int(observed[unit]['main_pid'])
                    process_source = Path(f'/proc/{pid}/cwd').resolve(strict=True)
                except (OSError, ValueError, KeyError) as exc:
                    raise RuntimeControlError('The active service process source could not be verified.') from exc
                if pid <= 0 or process_source != self.current():
                    raise RuntimeControlError('An active service still executes a different release; runtime acceptance failed.')
        return observed

    def render_unit(self, snapshot, rendered, target, vendor_dir='/usr/local/lib/systemd/system'):
        target = Path(target)
        unit = target.name
        original = snapshot['units'].get(unit) or snapshot['paired_services'].get(unit)
        if original is None:
            raise RuntimeControlError('Unit template is outside the canonical Twinevia runtime.')
        content = Path(rendered).read_bytes()
        destination = Path(vendor_dir) / unit if original['unit_file_state'].startswith('masked') else target
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.' + unit, dir=destination.parent)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)

    def pause(self, units, reason, expected_sha):
        identity = self.identity()
        if not identity or identity['source_sha'] != expected_sha or not set(units) <= set(TIMER_UNITS) or not units or not reason.strip():
            raise RuntimeControlError('Pause requires exact current source, explicit timer names and a reason.')
        if identity['source_sha'] == LEGACY_SAFE_SHA:
            raise RuntimeControlError('This retained legacy baseline supports normal recovery only. Forward-deploy the maintenance-capable release before declaring a new maintenance hold.')
        existing = self.receipt(identity, allow_expired=True)
        held = sorted(set(units) | set(existing['held_units'] if existing else []))
        for unit in held:
            self.systemctl.stop(unit)
            self.systemctl.stop(PAIRED_SERVICES[unit])
        states = {unit: self.systemctl.inspect(unit) for unit in held}
        if any(row['active_state'] != 'inactive' for row in states.values()):
            raise RuntimeControlError('A requested timer did not stop; maintenance has not been declared.')
        if any(self.systemctl.inspect(PAIRED_SERVICES[unit])['active_state'] in {'active', 'activating', 'deactivating'} for unit in held):
            raise RuntimeControlError('A background job is still running; maintenance has not been declared.')
        receipt = {'version': 1, 'held_units': held, 'states': states, 'reason': reason[:500],
                   'actor': os.environ.get('SUDO_USER') or 'root', 'declared_at': self.now().isoformat(),
                   'updated_at': self.now().isoformat(), 'expires_at': (self.now() + timedelta(hours=24)).isoformat(),
                   'release_sha': expected_sha, 'release_id': identity['release_id'], 'transaction_id': uuid4().hex}
        _write_json(self.maintenance_file, receipt, 0o644)
        self.audit('maintenance_paused', **receipt)
        return receipt

    def resume(self, units, reason, expected_sha, request_id=None):
        identity = self.verify_target(self.current())
        if identity['source_sha'] != expected_sha or not units or not set(units) <= set(TIMER_UNITS) or not reason.strip() or not request_id:
            raise RuntimeControlError('Resume requires the exact reviewed current source, explicit timers, a reason and a stable request ID.')
        units = sorted(set(units))
        request_file = None
        payload = {'units': sorted(set(units)), 'reason': reason[:500], 'release_sha': expected_sha}
        if request_id:
            if len(request_id) > 128:
                raise RuntimeControlError('Resume request identity is too long.')
            request_file = self.state_root / 'resume-requests' / (hashlib.sha256(request_id.encode()).hexdigest() + '.json')
            if request_file.exists():
                previous = _json_file(request_file)
                if previous.get('payload') != payload:
                    raise RuntimeControlError('Resume request identity already belongs to a different reviewed action.')
                if previous.get('status') == 'completed':
                    return previous['result']
                if previous.get('status') in {'prepared', 'review_required'}:
                    raise RuntimeControlError('A prior resume acknowledgement is incomplete. Review its actual timer/job state before authorizing a new request; it will not be replayed automatically.')
        receipt = self.receipt(identity)
        if receipt is None:
            raise RuntimeControlError('No valid maintenance receipt authorizes this resume.')
        if not set(units) <= set(receipt['held_units']):
            raise RuntimeControlError('Requested resume includes work outside the declared hold.')
        for unit in units:
            row = self.systemctl.inspect(unit)
            job = self.systemctl.inspect(PAIRED_SERVICES[unit])
            if job['active_state'] in {'active', 'activating', 'deactivating'} or job['load_state'] != 'loaded' or job['unit_file_state'] in {'masked', 'masked-runtime', 'linked', 'linked-runtime'}:
                raise RuntimeControlError('The held timer job is running, unavailable or externally linked; inspect it before resuming.')
            if row['unit_file_state'].startswith('masked'):
                raise RuntimeControlError('The timer is masked. Explicitly review/unmask it before resuming; resume never removes a mask.')
            if row['unit_file_state'] != receipt['states'][unit]['unit_file_state']:
                raise RuntimeControlError('Timer enablement changed after declaration; review the maintenance scope first.')
        self.verify_readiness(expected_held=set(receipt['held_units']))
        if request_file:
            _write_json(request_file, {'status': 'prepared', 'payload': payload, 'maintenance_id': receipt['transaction_id']})
        original_receipt = receipt
        started = []
        try:
            for unit in units:
                started.append(unit)
                self.systemctl.start(unit)
                if self.systemctl.inspect(unit)['active_state'] != 'active':
                    raise RuntimeControlError('Resumed timer did not become active.')
            remaining = [unit for unit in receipt['held_units'] if unit not in units]
            if remaining:
                receipt = {**receipt, 'held_units': remaining, 'states': {unit: receipt['states'][unit] for unit in remaining},
                           'updated_at': self.now().isoformat()}
                _write_json(self.maintenance_file, receipt, 0o644)
            else:
                self.maintenance_file.unlink()
            self.audit('maintenance_resumed', units=units, reason=reason[:500], release_sha=expected_sha, request_id=request_id)
            result = {'status': 'resumed', 'units': units, 'remaining_holds': remaining, 'release_sha': expected_sha}
            if request_file:
                _write_json(request_file, {'status': 'completed', 'payload': payload, 'maintenance_id': receipt['transaction_id'], 'result': result})
        except BaseException as exc:
            failed_containment = []
            for unit in started:
                for target in (unit, PAIRED_SERVICES[unit]):
                    try:
                        self.systemctl.stop(target)
                    except (RuntimeControlError, OSError, subprocess.SubprocessError):
                        failed_containment.append(target)
            try:
                _write_json(self.maintenance_file, original_receipt, 0o644)
            except (RuntimeControlError, OSError):
                failed_containment.append('maintenance declaration')
            if request_file:
                try:
                    _write_json(request_file, {'status': 'review_required', 'payload': payload,
                                'maintenance_id': original_receipt['transaction_id'], 'failed_containment': failed_containment})
                except (RuntimeControlError, OSError):
                    failed_containment.append('resume acknowledgement')
            if failed_containment:
                raise RuntimeControlError('Resume failed and containment is incomplete (' + ', '.join(failed_containment) + '); inspect the operation before any further source change.') from exc
            raise
        return result

    def verify_readiness(self, snapshot=None, *, url=None, expected_held=None):
        identity = self.identity()
        held = set(snapshot['held_units']) if snapshot else set(expected_held or [])
        url = url or 'http://127.0.0.1:8100/ready?format=json'
        token = os.environ.get('READINESS_TOKEN', '')
        if not token:
            raise RuntimeControlError('An authenticated readiness proof is required before accepting or resuming runtime work.')
        headers = {'X-Twinevia-Readiness-Token': token, 'Accept': 'application/json'}
        from urllib.parse import urlsplit
        if urlsplit(url).hostname == '127.0.0.1':
            host = urlsplit(os.environ.get('APP_BASE_URL', '')).hostname
            if host: headers['Host'] = host
        try:
            try:
                response = urlopen(Request(url, headers=headers), timeout=10)
            except HTTPError as exc:
                if exc.code != 503: raise
                response = exc
            with response:
                report = json.load(response)
        except (OSError, ValueError) as exc:
            raise RuntimeControlError('Authenticated readiness proof is unavailable; runtime acceptance remains incomplete.') from exc
        legacy_ready = (identity['source_sha'] == LEGACY_SAFE_SHA and not held and report.get('ready') is True
                        and report.get('release_id') == identity['release_id'] and not report.get('release_sha'))
        if not legacy_ready and (report.get('release_sha') != identity['source_sha'] or report.get('release_id') != identity['release_id']):
            raise RuntimeControlError('Readiness response does not identify the newly selected release.')
        if held:
            receipt = self.receipt(identity)
            failed = {row['name'] for row in report.get('checks', []) if not row['ready']}
            if (report.get('ready') is not False or report.get('state') != 'maintenance' or receipt is None
                    or set(receipt['held_units']) != held or not failed <= {'scheduler_timers', 'runtime_maintenance'}
                    or not failed or set((report.get('maintenance') or {}).get('held_units', [])) != held):
                raise RuntimeControlError('Maintenance cannot conceal another readiness failure or change its reviewed scope.')
        elif report.get('ready') is not True or (report.get('state') != 'ready' and not legacy_ready):
            raise RuntimeControlError('The application is not ready; no maintenance exception is authorized.')
        return {'state': 'ready' if legacy_ready else report['state'], 'release_sha': identity['source_sha'], 'held_units': sorted(held)}


@contextmanager
def runtime_lock(controller):
    controller.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = controller.state_root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeControlError('Runtime transaction directory is not trusted.')
    descriptor = os.open(controller.state_root / 'operation.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeControlError('Another runtime operation is in progress; retry after it completes.') from exc
        yield descriptor
    finally:
        os.close(descriptor)


def _controller_from_state(path):
    snapshot = _json_file(path)
    if snapshot.get('version') != 1 or set(snapshot.get('units', {})) != set(RUNTIME_UNITS):
        raise RuntimeControlError('Captured runtime snapshot is invalid.')
    controller = RuntimeController(snapshot['app_root'], snapshot['state_root'])
    try:
        os.kill(int(snapshot['owner_pid']), 0)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeControlError('The runtime transaction owner is no longer running; begin explicit recovery rather than reusing stale state.') from exc
    return controller, snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['transaction', 'quiesce', 'activate', 'restore', 'assert', 'verify-target', 'verify-readiness', 'render-unit', 'restart-active', 'pause', 'resume', 'check', 'units'])
    parser.add_argument('--app-root', default='/opt/twinevia-saas')
    parser.add_argument('--state-root')
    parser.add_argument('--state')
    parser.add_argument('--kind', default='deploy')
    parser.add_argument('--fresh-install', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--target')
    parser.add_argument('--rendered')
    parser.add_argument('--vendor-unit-dir', default='/usr/local/lib/systemd/system')
    parser.add_argument('--unit', action='append', default=[])
    parser.add_argument('--reason', default='')
    parser.add_argument('--expected-sha', default='')
    parser.add_argument('--request-id')
    parser.add_argument('--url')
    parser.add_argument('--readiness-token-stdin', action='store_true')
    args, child = parser.parse_known_args(argv)
    if child and child[0] == '--': child = child[1:]
    if child and args.command != 'transaction': parser.error('Unexpected command arguments.')
    if args.command == 'units':
        print('\n'.join(TIMER_UNITS if args.kind == 'timers' else RUNTIME_UNITS))
        return 0
    controller = RuntimeController(args.app_root, args.state_root)
    if args.readiness_token_stdin:
        token = sys.stdin.read(4097)
        if len(token) > 4096:
            raise RuntimeControlError('Readiness authentication value is invalid.')
        os.environ['READINESS_TOKEN'] = token
    if args.command == 'transaction':
        if not child: parser.error('A transaction requires an explicit child command after --.')
        with runtime_lock(controller) as descriptor:
            snapshot = controller.capture(args.kind, args.fresh_install)
            state = controller.state_root / 'transactions' / (snapshot['transaction_id'] + '.json')
            _write_json(state, snapshot)
            environment = {**os.environ, 'TWINEVIA_RUNTIME_STATE_FILE': str(state)}
            result = subprocess.run(child, env=environment, pass_fds=(descriptor,), check=False)
            controller.audit('runtime_transaction_finished', transaction_id=snapshot['transaction_id'], exit_code=result.returncode)
            return result.returncode
    if args.state:
        controller, snapshot = _controller_from_state(args.state)
        if args.command == 'quiesce': controller.quiesce(snapshot, args.rollback)
        elif args.command == 'activate': controller.activate(snapshot, args.fresh_install)
        elif args.command == 'restore': controller.restore(snapshot)
        elif args.command == 'assert': print(json.dumps(controller.assert_state(snapshot)))
        elif args.command == 'verify-readiness': print(json.dumps(controller.verify_readiness(snapshot, url=args.url)))
        elif args.command == 'render-unit': controller.render_unit(snapshot, args.rendered, args.target, args.vendor_unit_dir)
        elif args.command == 'verify-target': print(json.dumps(controller.verify_target(args.target, transition_snapshot=snapshot)))
        else: parser.error('Command cannot be used inside an existing transaction.')
    elif args.command == 'verify-target':
        print(json.dumps(controller.verify_target(args.target)))
    elif args.command == 'check':
        snapshot = controller.capture('deploy')
        controller.verify_target(controller.current())
        controller.assert_state(snapshot)
        print(json.dumps(controller.verify_readiness(snapshot, url=args.url)))
    elif args.command in {'restart-active', 'pause', 'resume'}:
        if os.geteuid() != 0:
            raise RuntimeControlError('Pause/resume/restart runtime operations require the root operator; application restart access cannot grant resume authority.')
        with runtime_lock(controller):
            if args.command == 'restart-active':
                snapshot = controller.capture('restart')
                controller.quiesce(snapshot)
                controller.activate(snapshot)
                controller.audit('active_services_restarted', held_units=snapshot['held_units'])
                print(json.dumps({'status': 'succeeded', 'held_units': snapshot['held_units']}))
            elif args.command == 'pause': print(json.dumps(controller.pause(args.unit, args.reason, args.expected_sha)))
            else: print(json.dumps(controller.resume(args.unit, args.reason, args.expected_sha, args.request_id)))
    else:
        parser.error('This command requires a captured runtime snapshot.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeControlError, OSError, subprocess.SubprocessError) as exc:
        print(f'Runtime operation stopped: {exc}', file=sys.stderr)
        sys.exit(1)
