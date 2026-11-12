"""Billing operations warn without blocking the real runtime acceptance gate.

The report is built by the application; only infrastructure observations and
HTTP transport are fakes. No network, service command, database or provider runs.
"""
import io
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from app.services import readiness_service as service
from deploy.runtime_control import RuntimeController, RuntimeControlError
from tests.test_maintenance_readiness import runtime, HELD_TIMER


BILLING_DETAILS = [
    'the last billing reconciliation run failed',
    'billing reconciliation result could not be inspected',
]


def _billing_warning(runtime, monkeypatch, detail):
    runtime.app.config.update(SAAS_MODE=True, DEBUG=False,
                              ALERT_WEBHOOK_URL='https://alerts.fixture.invalid/operational')
    inspection = Mock(return_value=service.ReadinessCheck('billing_reconcile', False, detail))
    monkeypatch.setattr(service, '_billing_reconcile_check', inspection)
    return inspection


def _selected_controller(runtime, tmp_path, operation):
    source_sha = ('b' if operation == 'rollback' else 'a') * 40
    release_id = 'reviewed-previous-release' if operation == 'rollback' else 'reviewed-new-release'
    app_root = tmp_path / 'runtime-application'
    release = app_root / 'releases' / release_id
    release.mkdir(parents=True)
    (release / 'release.json').write_text(json.dumps({
        'release_id': release_id, 'source_sha': source_sha,
    }))
    (app_root / 'current').symlink_to(release)
    runtime.app.config.update(APP_RELEASE_ID=release_id, APP_RELEASE_SHA=source_sha)
    controller = RuntimeController(app_root, tmp_path / 'runtime-state' / 'transactions')
    controller.maintenance_file = Path(runtime.app.config['RUNTIME_MAINTENANCE_FILE'])
    return controller


def _serve_report(monkeypatch, report):
    payload = report.as_dict()
    requests = []

    def response(request, timeout):
        assert timeout == 10
        assert request.get_header('X-twinevia-readiness-token') == 'isolated-billing-warning-token'
        assert request.get_header('Accept') == 'application/json'
        requests.append(request)
        body = io.BytesIO(json.dumps(payload).encode())
        if not payload['ready']:
            # urlopen returns the body of authenticated maintenance through the
            # actual HTTPError503 branch in RuntimeController.verify_readiness.
            raise HTTPError(request.full_url, 503, 'Fixture non-readiness', {}, body)
        return body

    monkeypatch.setenv('READINESS_TOKEN', 'isolated-billing-warning-token')
    monkeypatch.setattr('deploy.runtime_control.urlopen', response)
    return payload, requests


@pytest.mark.parametrize('operation', ['release', 'rollback'])
@pytest.mark.parametrize('detail', BILLING_DETAILS)
def test_release_and_rollback_accept_warning_for_the_selected_source(runtime, tmp_path, monkeypatch, operation, detail):
    controller = _selected_controller(runtime, tmp_path, operation)
    inspection = _billing_warning(runtime, monkeypatch, detail)
    report = runtime.report()
    assert report.ready is True and report.state == 'ready'
    assert all(check.ready for check in report.checks)
    assert any(detail in warning for warning in report.warnings)
    inspection.assert_called_once_with(runtime.app)
    _, requests = _serve_report(monkeypatch, report)
    result = controller.verify_readiness(
        {'held_units': [], 'kind': operation}, url='https://readiness.fixture.invalid/ready?format=json')
    assert result == {'state': 'ready', 'release_sha': controller.identity()['source_sha'], 'held_units': []}
    assert len(requests) == 1


@pytest.mark.parametrize('detail', BILLING_DETAILS)
def test_declared_maintenance_accepts_billing_warning_without_an_extra_failed_check(runtime, tmp_path, monkeypatch, detail):
    controller = _selected_controller(runtime, tmp_path, 'release')
    runtime.declare()
    _billing_warning(runtime, monkeypatch, detail)
    report = runtime.report()
    assert report.ready is False and report.state == 'maintenance'
    assert {check.name for check in report.checks if not check.ready} == {'scheduler_timers', 'runtime_maintenance'}
    assert any(detail in warning for warning in report.warnings)
    _serve_report(monkeypatch, report)
    result = controller.verify_readiness(
        {'held_units': [HELD_TIMER], 'kind': 'rollback'},
        url='https://readiness.fixture.invalid/ready?format=json')
    assert result['state'] == 'maintenance' and result['held_units'] == [HELD_TIMER]


@pytest.mark.parametrize('maintenance', [False, True])
def test_billing_warning_never_excuses_an_independent_database_failure(runtime, tmp_path, monkeypatch, maintenance):
    controller = _selected_controller(runtime, tmp_path, 'release')
    if maintenance:
        runtime.declare()
    _billing_warning(runtime, monkeypatch, BILLING_DETAILS[0])
    monkeypatch.setattr(service, '_database_check', lambda: service.ReadinessCheck('postgresql', False, 'Fixture database failure'))
    report = runtime.report()
    assert report.ready is False and report.state == 'not_ready'
    assert any(check.name == 'postgresql' and not check.ready for check in report.checks)
    assert report.warnings
    _serve_report(monkeypatch, report)
    with pytest.raises(RuntimeControlError, match='Maintenance cannot conceal|application is not ready'):
        controller.verify_readiness(
            {'held_units': [HELD_TIMER] if maintenance else []},
            url='https://readiness.fixture.invalid/ready?format=json')


def test_billing_warning_cannot_waive_selected_release_identity(runtime, tmp_path, monkeypatch):
    controller = _selected_controller(runtime, tmp_path, 'rollback')
    _billing_warning(runtime, monkeypatch, BILLING_DETAILS[1])
    report = runtime.report()
    assert report.ready and report.warnings
    payload, _ = _serve_report(monkeypatch, report)
    payload['release_sha'] = 'c' * 40
    with pytest.raises(RuntimeControlError, match='does not identify the newly selected release'):
        controller.verify_readiness(url='https://readiness.fixture.invalid/ready?format=json')
