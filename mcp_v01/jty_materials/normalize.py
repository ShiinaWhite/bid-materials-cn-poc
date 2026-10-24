"""Deterministic, testable canonical normalization. Conservative by design.

Two recognition chains may only be called "equal" when their canonical forms
are byte-equal. There is deliberately no fuzzy or semantic matching here:
- 山西XX建筑工程有限公司 and 山西XX建筑工程有限责任公司 are NEVER equal.
- Only formatting noise (Unicode compatibility forms, whitespace, display
  separators, date spellings, currency units) is removed.
"""
import datetime as dt
import re
import unicodedata

from .fields import validate_value


def name_key(value):
    """Canonical short-text/name form: NFKC, drop all whitespace and
    zero-width characters (OCR layout artifacts, never semantic content)."""
    if value is None:
        return None
    return re.sub(r'[\s\u200b\u200c\u200d\ufeff]+', '', unicodedata.normalize('NFKC', str(value)))


def canonical_credit(value):
    """NFKC + uppercase + drop whitespace/display separators."""
    if value is None:
        return None
    text = unicodedata.normalize('NFKC', str(value)).upper()
    return re.sub(r'[\s\-–—]', '', text)


def canonical_date(value):
    """Accept the common Chinese/original spellings, return ISO YYYY-MM-DD.
    Raises ValueError for anything that is not a real calendar date."""
    if value is None:
        return None
    text = unicodedata.normalize('NFKC', str(value)).strip()
    match = re.fullmatch(r'(\d{4})[年./\-]\s*(\d{1,2})[月./\-]\s*(\d{1,2})\s*日?', text)
    if match:
        return dt.date(*map(int, match.groups())).isoformat()
    return dt.date.fromisoformat(text).isoformat()


def canonical_amount_cents(value):
    """'1,234,567.89' / '1234567.89元' / '123.45万元' -> integer cents.
    Raises ValueError when the text is not a plain number."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = unicodedata.normalize('NFKC', str(value)).strip().replace(',', '').replace('，', '')
    unit = 1
    if text.endswith('万元'):
        unit, text = 1000000, text[:-2]
    text = text.rstrip('元圆人民币¥￥').strip()
    number = float(text)
    if abs(number) > 10**13:
        raise ValueError('amount out of range')
    return round(number * unit)


def canonical_value(field, value):
    """Canonical form per field kind; the output must satisfy the same closed
    validation as every other candidate, otherwise it is not comparable."""
    from .fields import field_spec
    spec = field_spec(field)
    if spec is None:
        raise ValueError('unsupported field: ' + str(field))
    kind = spec[2]
    if kind == 'credit':
        return canonical_credit(value)
    if kind == 'date':
        return canonical_date(value)
    if kind in ('amount', 'signed_amount'):
        return canonical_amount_cents(value)
    if kind in ('int', 'year', 'grade'):
        number = int(re.sub(r'[\s,，]', '', unicodedata.normalize('NFKC', str(value))))
        return number
    if kind == 'bool':
        if isinstance(value, bool):
            return int(value)
        text = unicodedata.normalize('NFKC', str(value)).strip().lower()
        if text in ('1', 'true', '是', '长期', '长期有效', 'yes'):
            return 1
        if text in ('0', 'false', '否', 'no'):
            return 0
        raise ValueError('not a boolean: ' + repr(value)[:40])
    return name_key(value)


# ---------------------------------------------------------------------------
# 统一社会信用代码 (GB 32100-2015)
# ---------------------------------------------------------------------------

USCC_CHARSET = '0123456789ABCDEFGHJKLMNPQRTUWXY'  # no I/O/S/V/Z
_USCC_VALUES = {char: index for index, char in enumerate(USCC_CHARSET)}
_USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)


def uscc_check_digit(first17):
    """Check character for the first 17 canonical characters (GB 32100-2015)."""
    if not isinstance(first17, str) or len(first17) != 17:
        raise ValueError('need exactly 17 characters')
    total = 0
    for char, weight in zip(first17.upper(), _USCC_WEIGHTS):
        total += _USCC_VALUES[char] * weight
    return USCC_CHARSET[(31 - total % 31) % 31]


def uscc_complete(first17):
    return first17.upper() + uscc_check_digit(first17)


def uscc_valid(code):
    """True only for a structurally correct, checksum-valid 18-character code.
    Candidate compatibility is unchanged: weaker legacy validation still
    accepts historical candidates; this gate is only for machine verification."""
    if not isinstance(code, str):
        return False
    code = canonical_credit(code)
    if len(code) != 18 or any(char not in _USCC_VALUES for char in code):
        return False
    return uscc_check_digit(code[:17]) == code[17]


# Deterministic field validators for machine verification. Every entry must
# be able to reject; nothing here "repairs" a value.
_STRONG_TEXT_PATTERNS = {
    'certificate_no': re.compile(r'^[\wA-Za-z0-9\-—·（）()/\s]{4,60}$', re.UNICODE),
    'registration_no': re.compile(r'^[0-9A-Za-z\-—·（）()/第号]{4,60}$'),
    'patent_no': re.compile(r'^(?:ZL\s*)?20\d{10}[.．]?\d?$'),
}


def machine_validator(field, value):
    """Deterministic validator for machine verification (beyond type checks).
    Returns True when the canonical value passes its structural validator.
    USCC additionally requires the GB 32100-2015 check digit."""
    from .fields import field_spec
    kind = field_spec(field)[2]
    if kind == 'credit':
        return uscc_valid(value)
    pattern = _STRONG_TEXT_PATTERNS.get(field)
    if pattern is not None:
        return bool(pattern.match(name_key(value)))
    return True
