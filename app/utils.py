import re
import csv
import io


def normalize_phone(phone: str) -> str:
    """
    Normalize phone number to E.164-ish format.
    Removes non-digit characters and ensures it starts with +.
    """
    if not phone:
        return ''
    
    # Remove all non-digit characters except +
    cleaned = re.sub(r'[^\d+]', '', phone.strip())
    
    # If it starts with +, keep it; otherwise assume US number
    if cleaned.startswith('+'):
        return cleaned
    
    # Remove leading 1 if present for US numbers, then add +1
    if cleaned.startswith('1') and len(cleaned) == 11:
        return '+' + cleaned
    elif len(cleaned) == 10:
        return '+1' + cleaned
    
    # For other formats, just add + if not present
    return '+' + cleaned


def validate_phone(phone: str) -> bool:
    """
    Basic validation for E.164 phone format.
    Returns True if phone looks valid.
    """
    if not phone:
        return False
    
    normalized = normalize_phone(phone)
    # E.164: + followed by 7-15 digits
    return bool(re.match(r'^\+\d{7,15}$', normalized))


def _looks_like_phone(value: str) -> bool:
    """Check if a string looks like a phone number (has 7+ digits)."""
    digits = re.sub(r'\D', '', value)
    return len(digits) >= 7


def parse_recipients_csv(file_content: str) -> list:
    """
    Parse CSV content for recipients.
    Supports formats:
    - Single column: phone only (e.g., 720-383-2388)
    - Two columns: name, phone OR phone, name (auto-detected)
    - Three columns: first_name, last_name, phone (e.g., Vardan,Hovsepyan,(323) 630-0201)
    
    Returns list of dicts with 'name' and 'phone' keys.
    """
    recipients = []
    
    # Try to parse as CSV
    reader = csv.reader(io.StringIO(file_content))
    rows = list(reader)
    
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
            if cell and re.search(r'\d{7,}', re.sub(r'\D', '', cell)):
                normalized = normalize_phone(cell)
                if validate_phone(normalized):
                    phones.append(normalized)
    
    return phones
