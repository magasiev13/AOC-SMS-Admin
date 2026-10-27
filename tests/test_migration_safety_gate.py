"""The invitation history exception cannot authorize general destructive SQL."""
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/check_expand_only_migrations.sh'
CREATE = "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_invitations_pending_email ON organization_invitations (organization_id, email) WHERE status = 'pending'"
REPLACE = 'ALTER TABLE organization_invitations DROP CONSTRAINT IF EXISTS ux_org_invitations_org_email_status'


def gate(tmp_path, content, name='025_pending_invitation_history.py'):
    folder = tmp_path / 'app/saas_migrations'
    folder.mkdir(parents=True)
    (folder / name).write_text('\n'.join(f'statement_{index} = {line!r}' for index, line in enumerate(content.splitlines())))
    return subprocess.run(['bash', str(SCRIPT)], env={**os.environ, 'APP_ROOT': str(tmp_path)}, capture_output=True, text=True)


def test_reviewed_invitation_uniqueness_replacement_is_allowed(tmp_path):
    assert gate(tmp_path, CREATE + '\n' + REPLACE).returncode == 0


@pytest.mark.parametrize('sql', [
    'DROP TABLE community_members', 'DROP INDEX another_index',
    'ALTER TABLE organizations DROP COLUMN name', 'ALTER TABLE organizations RENAME TO missing',
    'TRUNCATE TABLE inbox_messages', 'DELETE FROM organization_invitations',
    'ALTER TABLE organization_invitations DROP CONSTRAINT a_different_constraint',
])
def test_invitation_exception_does_not_allow_other_destructive_statements(tmp_path, sql):
    assert gate(tmp_path, CREATE + '\n' + REPLACE + '\n' + sql).returncode != 0


def test_replacement_requires_the_pending_index_first(tmp_path):
    assert gate(tmp_path, REPLACE + '\n' + CREATE).returncode != 0


def test_constraint_cannot_be_removed_without_replacement(tmp_path):
    assert gate(tmp_path, REPLACE).returncode != 0


def test_exception_cannot_be_reused_by_another_migration(tmp_path):
    assert gate(tmp_path, CREATE + '\n' + REPLACE, '026_unreviewed.py').returncode != 0
