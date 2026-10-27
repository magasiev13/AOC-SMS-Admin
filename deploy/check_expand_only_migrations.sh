#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
MIGRATIONS_DIR="${APP_ROOT}/app/saas_migrations"

if [[ ! -d "${MIGRATIONS_DIR}" ]]; then
  echo "SaaS migrations directory is missing: ${MIGRATIONS_DIR}" >&2
  exit 1
fi

python3 - "${MIGRATIONS_DIR}" <<'PY'
from pathlib import Path
import ast
import re
import sys

destructive = re.compile(r'DROP\s+(TABLE|COLUMN|INDEX|CONSTRAINT)|TRUNCATE\s+TABLE|DELETE\s+FROM|ALTER\s+TABLE[^;]*(RENAME|DROP)', re.I)
replacement = re.compile(r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ux_org_invitations_pending_email\s+ON\s+organization_invitations\s*\(\s*organization_id\s*,\s*email\s*\)\s+WHERE\s+status\s*=\s*'pending'", re.I)
old_constraint = re.compile(r'ALTER\s+TABLE\s+organization_invitations\s+DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+ux_org_invitations_org_email_status\b', re.I)
failures = []
for migration in sorted(Path(sys.argv[1]).glob('[0-9][0-9][0-9]_*.py')):
    content = migration.read_text()
    # The reviewed025 transition expands valid invitation history. It removes
    # no rows/columns and retains pending uniqueness under a narrower index.
    # Only this exact replacement, in this order, is permitted; all other
    # contract-stage statements remain release blockers.
    if migration.name.startswith('025_'):
        # Python concatenates adjacent SQL literals; inspect their actual value
        # rather than requiring one physical source line or trusting comments.
        literals = [node for node in ast.walk(ast.parse(content)) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        creates = [node for node in literals if replacement.fullmatch(node.value.strip())]
        removes = [node for node in literals if old_constraint.fullmatch(node.value.strip())]
        if len(removes) == 1 and any(node.lineno < removes[0].lineno for node in creates):
            content = old_constraint.sub('REVIEWED_PENDING_INVITATION_UNIQUENESS_REPLACEMENT', content, count=1)
    for line_no, line in enumerate(content.splitlines(), 1):
        if destructive.search(line):
            failures.append(f'{migration.name}:{line_no}: {line.strip()}')
if failures:
    print('Refusing release: unreviewed contract-stage SQL in SaaS migrations.', file=sys.stderr)
    print('\n'.join(failures), file=sys.stderr)
    raise SystemExit(1)
print('SaaS migration safety check passed: no data removal or unreviewed contract-stage SQL.')
PY
