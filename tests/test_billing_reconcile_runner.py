"""Execute the oneshot runner against stub modules and an isolated app root."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


RUNNER = Path(__file__).resolve().parents[1] / 'deploy' / 'run_billing_reconcile_once.sh'


def run_reconcile(tmp_path, operation):
    root = tmp_path / 'isolated-app'
    for directory in ('deploy', 'venv/bin', 'app/services'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    check = root / 'deploy' / 'check_python_runtime.sh'
    check.write_text('#!/bin/sh\nexit 0\n')
    check.chmod(0o755)
    python = root / 'venv/bin/python'
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    (root / 'app/__init__.py').write_text(
        'from contextlib import nullcontext\n'
        'from types import SimpleNamespace\n'
        'def create_app(**kwargs):\n'
        '    assert kwargs == {"run_startup_tasks": False, "start_scheduler": False}\n'
        '    return SimpleNamespace(app_context=nullcontext)\n'
    )
    (root / 'app/services/__init__.py').write_text('')
    (root / 'app/services/billing_service.py').write_text(
        'from pathlib import Path\n'
        'def reconcile_billing_subscriptions():\n'
        '    Path(__file__).with_name("called").touch()\n'
        f'    {operation}\n'
    )
    result = subprocess.run(
        ['bash', str(RUNNER)], cwd=tmp_path,
        env={'PATH': os.defpath, 'APP_ROOT': str(root), 'PYTHON_DOTENV_DISABLED': '1'},
        capture_output=True, text=True, timeout=10,
    )
    assert (root / 'app/services/called').exists()
    return result


@pytest.mark.parametrize('failed, usage_failed, expected_status', [
    (0, 0, 0),
    (1, 0, 1),
    (0, 1, 1),
    (1, 1, 1),
])
def test_runner_exit_status_reports_subscription_and_usage_failures(tmp_path, failed, usage_failed, expected_status):
    summary = {'failed': failed, 'usage_periods_failed': usage_failed, 'missing_canceled': 1}
    result = run_reconcile(tmp_path, f'return {json.dumps(summary)}')
    assert result.returncode == expected_status
    assert result.stderr == ''


def test_runner_unexpected_exception_is_nonzero_and_visible(tmp_path):
    result = run_reconcile(tmp_path, 'raise RuntimeError("Controlled reconciliation failure")')
    assert result.returncode == 1
    assert 'Stripe billing reconciliation failed: Controlled reconciliation failure' in result.stderr
    assert 'Traceback' in result.stderr
