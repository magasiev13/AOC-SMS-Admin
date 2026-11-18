"""Purpose-scoped import permissions with immutable source wording and per-row evidence."""
import csv
import io
from datetime import datetime, timezone

from flask import current_app

from app.models import ConsentSource, utc_now
from app.services.recipient_permission_service import record_permission_evidence
from app.utils import normalize_phone, validate_phone


def evidence_rows(content):
    reader = csv.DictReader(io.StringIO(content))
    fields = ['name', 'phone', 'reference', 'consented_at']
    if reader.fieldnames != fields:
        raise ValueError('Use the CSV header name,phone,reference,consented_at for a consent source.')
    rows = []
    seen = set()
    max_rows = current_app.config['CSV_IMPORT_MAX_ROWS']
    for row in reader:
        if len(rows) >= max_rows:
            raise ValueError('The CSV exceeds the upload row limit.')
        if set(row) != set(fields) or any(value is None for value in row.values()):
            raise ValueError('Each consent row requires all four columns.')
        phone = normalize_phone(row['phone'])
        reference = row['reference'].strip()
        if not validate_phone(phone) or not reference or len(reference) > 120 or len(row['name']) > 100:
            raise ValueError('Each row needs a valid phone, a name up to 100 characters, and a short source reference.')
        if reference in seen:
            raise ValueError('Each consent reference must identify one source record.')
        seen.add(reference)
        try:
            when = datetime.fromisoformat(row['consented_at'].strip().replace('Z', '+00:00'))
            if when.tzinfo is None:
                raise ValueError()
            when = when.astimezone(timezone.utc).replace(tzinfo=None)
            if when > utc_now().replace(tzinfo=None):
                raise ValueError()
        except (ValueError, OverflowError) as exc:
            raise ValueError('Use a past consent timestamp with an explicit time zone on each row.') from exc
        rows.append({'phone': phone, 'name': row['name'].strip(), 'reference': reference, 'when': when})
    return rows


def record_import_row(source: ConsentSource, row):
    record_permission_evidence(
        source.organization_id, row['phone'], purpose=source.purpose, event_id=source.event_id,
        decision='granted', source=f'approved_import:{source.id}', evidence_key=row['reference'],
        occurred_at=row['when'], verified=True,
        metadata={'proof': row['reference'], 'source_id': source.id, 'disclosure': source.disclosure,
                  'method': 'attested CSV import'},
    )
