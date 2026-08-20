import csv
import io
import re
from collections import Counter
from datetime import timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

from sqlalchemy import func


ALLOWED_TEMPLATE_TOKENS = ("name", "first_name", "full_name")
_TEMPLATE_TOKEN_RE = re.compile(
    r"\{(" + "|".join(ALLOWED_TEMPLATE_TOKENS) + r")\}",
    re.IGNORECASE,
)
_TEMPLATE_TOKEN_SCAN_RE = re.compile(r"\{([^{}]+)\}")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")
_SMS_NORMALIZATION_REPLACEMENTS = {
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "…": "...",
    "\u00a0": " ",
    "\u2007": " ",
    "\u2009": " ",
    "\u202f": " ",
    "•": "-",
}
_GSM_7_BASIC_CHARSET = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_7_EXTENDED_CHARSET = set("^{}\\[~]|€")


def escape_like(value: str) -> str:
    """
    Escape special LIKE pattern characters (% and _) plus escape tokens
    (\\) to prevent SQL injection via wildcard abuse in search queries.
    """
    if not value:
        return value
    return value.replace('\\', r'\\').replace('%', r'\%').replace('_', r'\_')


def is_safe_url(target: str | None, host_url: str) -> bool:
    if not target or not host_url:
        return False
    parsed_host_url = urlparse(host_url)
    redirect_url = urlparse(urljoin(host_url, target))
    return redirect_url.scheme in ("http", "https") and parsed_host_url.netloc == redirect_url.netloc


def as_utc_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_keyword(value: str) -> str:
    """Normalize automation/survey keywords for storage and matching."""
    return ' '.join((value or '').upper().strip().split())


def normalize_sms_body(value: str | None) -> str:
    """Normalize deterministic high-cost SMS punctuation before persistence/send."""
    return _normalize_sms_body_with_details(value or "")["normalized_body"]


def analyze_sms_body(value: str | None, *, apply_normalization: bool = True) -> dict[str, object]:
    original_body = value or ""
    normalized_payload = (
        _normalize_sms_body_with_details(original_body)
        if apply_normalization
        else {
            "normalized_body": original_body,
            "normalization_applied": False,
            "normalized_character_delta": 0,
            "normalized_character_savings": 0,
            "replacement_count": 0,
            "replacements": [],
        }
    )
    normalized_body = normalized_payload["normalized_body"]
    original_metrics = _sms_body_metrics(original_body)
    normalized_metrics = _sms_body_metrics(normalized_body)
    original_segments = int(original_metrics["segment_count"])
    normalized_segments = int(normalized_metrics["segment_count"])

    return {
        "original_body": original_body,
        "normalized_body": normalized_body,
        "normalization_applied": bool(normalized_payload["normalization_applied"]),
        "normalized_character_delta": int(normalized_payload["normalized_character_delta"]),
        "normalized_character_savings": int(normalized_payload["normalized_character_savings"]),
        "replacement_count": int(normalized_payload["replacement_count"]),
        "replacements": list(normalized_payload["replacements"]),
        "encoding": normalized_metrics["encoding"],
        "segment_count": normalized_segments,
        "characters_used": int(normalized_metrics["characters_used"]),
        "characters_to_next_segment": int(normalized_metrics["characters_to_next_segment"]),
        "segment_limit": int(normalized_metrics["segment_limit"]),
        "original_encoding": original_metrics["encoding"],
        "original_segment_count": original_segments,
        "original_characters_used": int(original_metrics["characters_used"]),
        "original_characters_to_next_segment": int(original_metrics["characters_to_next_segment"]),
        "original_segment_limit": int(original_metrics["segment_limit"]),
        "normalized_segment_delta": normalized_segments - original_segments,
        "segments_saved": max(0, original_segments - normalized_segments),
    }


def analyze_personalized_sms_blast(body: str | None, recipients: list[dict] | None) -> dict[str, object]:
    prepared_body = body or ""
    prepared_recipients = recipients or []

    if not prepared_recipients:
        return {
            "unique_recipients": 0,
            "min_segment_count": 0,
            "max_segment_count": 0,
            "total_segments": 0,
            "encodings": [],
            "per_recipient": [],
        }

    per_recipient: list[dict[str, object]] = []
    total_segments = 0
    min_segments: int | None = None
    max_segments = 0
    encodings: set[str] = set()

    for recipient in prepared_recipients:
        personalized_body = render_message_template(prepared_body, recipient)
        analysis = analyze_sms_body(personalized_body, apply_normalization=False)
        segment_count = int(analysis["segment_count"])
        total_segments += segment_count
        min_segments = segment_count if min_segments is None else min(min_segments, segment_count)
        max_segments = max(max_segments, segment_count)
        encodings.add(str(analysis["encoding"]))
        per_recipient.append(
            {
                "phone": recipient.get("phone"),
                "name": recipient.get("name"),
                "segment_count": segment_count,
                "encoding": analysis["encoding"],
                "characters_used": analysis["characters_used"],
            }
        )

    return {
        "unique_recipients": len(prepared_recipients),
        "min_segment_count": min_segments or 0,
        "max_segment_count": max_segments,
        "total_segments": total_segments,
        "encodings": sorted(encodings),
        "per_recipient": per_recipient,
    }


def normalize_phone(phone: object) -> str:
    """
    Normalize phone number to E.164-ish format.
    Removes non-digit characters and ensures it starts with +.
    """
    if phone is None:
        return ''

    raw = str(phone).strip()
    if not raw:
        return ''
    # Reject alphabetic content (e.g. "ext", vanity numbers) so we do not
    # accidentally merge extensions/words into a valid SMS destination.
    if re.search(r'[A-Za-z]', raw):
        return ''
    digits = re.sub(r'[^0-9]', '', raw)
    if not digits:
        return ''

    # If caller already provided an international prefix, preserve it.
    if raw.startswith('+'):
        return f'+{digits}'

    # Assume US format for 10-digit local numbers.
    if len(digits) == 10:
        return f'+1{digits}'

    # Preserve 11-digit US numbers that already include country code.
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'

    # Fall back to prefixed digits for non-US inputs without '+'
    return f'+{digits}'


def phone_digits_sql(column):
    normalized = func.replace(column, '+', '')
    for token in ('(', ')', '-', ' ', '.'):
        normalized = func.replace(normalized, token, '')
    return normalized


def phone_lookup_variants(phone: str) -> list[str]:
    normalized_phone = normalize_phone(phone)
    digits = ''.join(char for char in normalized_phone if char.isdigit())
    if not digits:
        return []

    variants: list[str] = [digits]
    if len(digits) == 11 and digits.startswith('1'):
        variants.append(digits[1:])
    elif len(digits) == 10:
        variants.append(f'1{digits}')
    return list(dict.fromkeys(variants))


def validate_phone(phone: str) -> bool:
    """
    Basic validation for E.164 phone format.
    Returns True if phone looks valid.
    """
    if not phone:
        return False
    
    normalized = normalize_phone(phone)
    # E.164: + followed by 7-15 digits
    return bool(re.match(r'^\+[0-9]{7,15}$', normalized))


def get_first_name(name: Optional[str]) -> str:
    if not name:
        return ''
    parts = name.strip().split()
    return parts[0] if parts else ''


def render_message_template(template: str, recipient: dict, fallback: str = 'there') -> str:
    if not template:
        return template

    full_name = (recipient.get('name') or '').strip()
    first_name = get_first_name(full_name) or fallback

    def _replace(match: re.Match) -> str:
        token = match.group(1).lower()
        if token in {'name', 'full_name'}:
            return full_name or fallback
        return first_name

    return _TEMPLATE_TOKEN_RE.sub(_replace, template)


def find_invalid_template_tokens(template: str) -> list[str]:
    if not template:
        return []

    invalid_tokens = []
    seen = set()
    for match in _TEMPLATE_TOKEN_SCAN_RE.finditer(template):
        token = match.group(1)
        normalized = token.lower()
        if normalized not in ALLOWED_TEMPLATE_TOKENS:
            raw = match.group(0)
            if raw not in seen:
                invalid_tokens.append(raw)
                seen.add(raw)
    return invalid_tokens


def sanitize_csv_cell(value: object) -> str:
    """
    Prevent CSV/formula injection when opening exports in spreadsheet clients.

    Any cell beginning with characters interpreted as formulas is prefixed with
    a single quote so it is treated as literal text.
    """
    if value is None:
        return ""

    text = str(value)
    if not text:
        return text

    stripped = text.lstrip(" \t\r\n")
    if stripped and stripped[0] in _CSV_FORMULA_PREFIXES:
        return f"'{text}"

    if text[0] in ("\t", "\r", "\n"):
        return f"'{text}"
    return text


def _looks_like_phone(value: str) -> bool:
    """Check if a string looks like a phone number (has 7+ digits)."""
    digits = re.sub(r'[^0-9]', '', value)
    return len(digits) >= 7


def parse_recipients_csv(
    file_content: str,
    max_rows: int,
    max_columns: int,
    max_cell_chars: int,
) -> list[dict[str, str | None]]:
    """
    Parse CSV content for recipients.
    Supports formats:
    - Single column: phone only (e.g., 720-383-2388)
    - Two columns: name, phone OR phone, name (auto-detected)
    - Three columns: first_name, last_name, phone (e.g., Vardan,Hovsepyan,(323) 630-0201)
    
    Returns list of dicts with 'name' and 'phone' keys.
    """
    if max_rows < 1 or max_columns < 1 or max_cell_chars < 1:
        raise ValueError("CSV limits must all be positive integers.")
    recipients: list[dict[str, str | None]] = []
    
    # Try to parse as CSV
    reader = csv.reader(io.StringIO(file_content))
    rows: list[list[str]] = []
    for row_number, row in enumerate(reader, start=1):
        if row_number > max_rows + 1:
            raise ValueError(f"CSV exceeds the maximum of {max_rows} data rows.")
        if len(row) > max_columns:
            raise ValueError(
                f"CSV row {row_number} has {len(row)} columns; the limit is {max_columns}."
            )
        oversized_cell = next((cell for cell in row if len(cell) > max_cell_chars), None)
        if oversized_cell is not None:
            raise ValueError(
                f"CSV row {row_number} contains a cell longer than {max_cell_chars} characters."
            )
        rows.append(row)
    
    if not rows:
        return recipients
    
    # Check if first row is a header
    first_row = rows[0]
    has_header = False
    
    if first_row:
        first_cell = first_row[0].lower().strip()
        if first_cell in ('name', 'phone', 'number', 'mobile', 'cell', 'first', 'firstname', 'first_name'):
            has_header = True
    
    start_idx = 1 if has_header else 0
    if len(rows) - start_idx > max_rows:
        raise ValueError(f"CSV exceeds the maximum of {max_rows} data rows.")
    
    for row in rows[start_idx:]:
        if not row or not any(cell.strip() for cell in row):
            continue
        
        name = None
        phone = None
        
        if len(row) == 1:
            # Single column: phone only
            phone = normalize_phone(row[0])
        
        elif len(row) == 2:
            # Two columns: detect which is phone
            col1, col2 = row[0].strip(), row[1].strip()
            col1_is_phone = _looks_like_phone(col1)
            col2_is_phone = _looks_like_phone(col2)
            
            if col2_is_phone and not col1_is_phone:
                # name, phone format
                name = col1 if col1 else None
                phone = normalize_phone(col2)
            elif col1_is_phone:
                # phone, name format or phone only
                phone = normalize_phone(col1)
                name = col2 if col2 and not col2_is_phone else None
            else:
                continue
        
        elif len(row) >= 3:
            # Three+ columns: first_name, last_name, phone format
            # Find which column has the phone number
            phone_col_idx = None
            for i, cell in enumerate(row):
                if _looks_like_phone(cell.strip()):
                    phone_col_idx = i
                    break
            
            if phone_col_idx is not None:
                phone = normalize_phone(row[phone_col_idx].strip())
                # Combine non-phone columns as name
                name_parts = []
                for i, cell in enumerate(row):
                    if i != phone_col_idx and cell.strip() and not _looks_like_phone(cell.strip()):
                        name_parts.append(cell.strip())
                name = ' '.join(name_parts) if name_parts else None
            else:
                continue
        
        if phone and validate_phone(phone):
            recipients.append({'name': name, 'phone': phone})
    
    return recipients


def _normalize_sms_body_with_details(value: str) -> dict[str, object]:
    normalized_parts: list[str] = []
    replacements: Counter[tuple[str, str]] = Counter()

    for char in value:
        replacement = _SMS_NORMALIZATION_REPLACEMENTS.get(char)
        if replacement is None:
            normalized_parts.append(char)
            continue
        normalized_parts.append(replacement)
        replacements[(char, replacement)] += 1

    normalized_body = ''.join(normalized_parts)
    replacement_rows = [
        {"from": source, "to": target, "count": count}
        for (source, target), count in sorted(replacements.items(), key=lambda item: (item[0][0], item[0][1]))
    ]
    return {
        "normalized_body": normalized_body,
        "normalization_applied": normalized_body != value,
        "normalized_character_delta": len(normalized_body) - len(value),
        "normalized_character_savings": max(0, len(value) - len(normalized_body)),
        "replacement_count": sum(replacements.values()),
        "replacements": replacement_rows,
    }


def _sms_body_metrics(value: str) -> dict[str, object]:
    body = value or ""
    if not body:
        return {
            "encoding": "gsm-7",
            "segment_count": 0,
            "characters_used": 0,
            "characters_to_next_segment": 160,
            "segment_limit": 160,
        }

    encoding = "gsm-7"
    character_units = 0
    for char in body:
        if char in _GSM_7_BASIC_CHARSET:
            character_units += 1
            continue
        if char in _GSM_7_EXTENDED_CHARSET:
            character_units += 2
            continue
        encoding = "ucs-2"
        break

    if encoding == "ucs-2":
        character_units = len(body)
        single_segment_limit = 70
        multi_segment_limit = 67
    else:
        single_segment_limit = 160
        multi_segment_limit = 153

    if character_units <= single_segment_limit:
        segment_count = 1
        segment_limit = single_segment_limit
    else:
        segment_count = ((character_units - 1) // multi_segment_limit) + 1
        segment_limit = segment_count * multi_segment_limit

    return {
        "encoding": encoding,
        "segment_count": segment_count,
        "characters_used": character_units,
        "characters_to_next_segment": max(0, segment_limit - character_units),
        "segment_limit": segment_limit,
    }


def parse_phones_csv(file_content: str) -> list:
    """
    Parse CSV for phone numbers only.
    Returns list of normalized phone strings.
    """
    phones = []
    reader = csv.reader(io.StringIO(file_content))
    
    for row in reader:
        for cell in row:
            cell = cell.strip()
            if cell and re.search(r'[0-9]{7,}', re.sub(r'[^0-9]', '', cell)):
                normalized = normalize_phone(cell)
                if validate_phone(normalized):
                    phones.append(normalized)
    
    return phones
