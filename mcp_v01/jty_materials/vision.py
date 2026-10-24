"""Vision recognition chain: the model looks at the ORIGINAL page pixels.

This chain is independent of OCR by construction — the image bytes sent to the
provider are rendered/read from the immutable original, never from any OCR
derivative. The two chains may only meet again in the consensus engine.

PDF originals: only the pages actually needed are rasterized, in memory,
and released after the request (spec #9). Nothing is persisted.
"""
import json

from .common import now
from .fields import ALLOWED, validate_value, field_spec, allowed_field
from .normalize import canonical_value
from . import providers

PROMPT_VERSION = 'v0.7-prompt-1'
SCHEMA_VERSION = 'v0.7-fields-1'
MAX_EXTRACT_PAGES = 3
EXTRACTOR_PREFIX = 'vision'


def _type_fields(type_code):
    return sorted(ALLOWED.get(type_code, ()))


def classify_original(config, provider, original_path):
    """(type_code|None, confidence, basis) — None means no usable answer."""
    pages = providers.render_pages_for_vision(original_path, max_pages=1)
    try:
        code, confidence = provider.classify(pages, sorted(ALLOWED), purpose='material.classify')
    except providers.ProviderError as error:
        config.log('vision.classify_failed', error=str(error))
        return None, 0.0, 'provider_error:' + error.kind
    return (None, 0.0, 'unknown_type') if code == 'unknown' else (code, confidence, 'vision_page_1')


def extract_fields(config, provider, original_path, type_code):
    """Vision structured extraction constrained to the type's closed fields.
    Returns (candidates, run_info): candidates is a list of dicts with
    field/raw/canonical/page; invalid or disallowed answers are dropped."""
    fields = _type_fields(type_code)
    if not fields:
        return [], {'status': 'skipped', 'note': 'type has no extractable fields'}
    pages = providers.render_pages_for_vision(original_path, max_pages=MAX_EXTRACT_PAGES)
    answer = provider.extract(pages, type_code, fields, purpose='material.extract')
    raw_fields = answer.get('fields')
    if not isinstance(raw_fields, dict):
        return [], {'status': 'failed', 'note': 'vision response missing fields object'}
    candidates = []
    rejected = []
    for field, payload in raw_fields.items():
        if field not in fields or not allowed_field(type_code, field):
            rejected.append((field, 'not allowed for type'))
            continue
        entry = payload if isinstance(payload, dict) else {'value': payload}
        value = entry.get('value')
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        page = entry.get('page', 1)
        if type(page) is not int or not 1 <= page <= len(pages):
            page = 1
        try:
            canonical = canonical_value(field, value)
            validate_value(field, canonical)
        except (ValueError, TypeError) as error:
            # keep the raw answer for diagnosis; document content, never a secret
            rejected.append((field, str(error)[:60], str(value)[:60]))
            continue
        spec = field_spec(field, type_code)
        value_type = 'amount' if spec[2] in ('amount', 'signed_amount') else \
            'int' if spec[2] in ('int', 'year', 'grade') else 'text' if spec[2] == 'credit' else spec[2]
        candidates.append({'field': field, 'raw': str(value), 'canonical': str(canonical),
                           'value_type': value_type, 'page': page})
    info = {'status': 'succeeded', 'pages': len(pages), 'rejected': rejected[:20],
            'prompt_version': PROMPT_VERSION, 'schema_version': SCHEMA_VERSION}
    return candidates, info


def run_vision_extraction(config, db, material_id, original, *, provider, purpose='dual_engine'):
    """Record a vision extraction_run + candidates for the current original.
    db: an open writable Store db. Never raises for provider failures — a
    failed/deferred vision run must not block ingestion (spec #40)."""
    from pathlib import Path
    run_info = {'purpose': purpose}
    status = 'succeeded'
    error_text = None
    candidates = []
    try:
        if isinstance(provider, providers.NullProvider):
            raise providers.ProviderError('unavailable', provider.reason)
        candidates, info = extract_fields(config, provider, Path(original['path']), original['code'])
        run_info.update(info)
    except providers.ProviderError as error:
        status = 'failed' if error.kind != 'auth' else 'failed'
        error_text = (error.kind + ': ' + str(error))[:500]
        run_info['error_kind'] = error.kind
    except Exception as error:  # rendering/validation problems: deferred, not fatal
        status = 'failed'
        error_text = type(error).__name__ + ': ' + str(error)[:500]
    extractor = f'{EXTRACTOR_PREFIX}:{getattr(provider, "provider", "none")}:{getattr(provider, "vision_model", "none")}'
    params = json.dumps(dict(run_info, original_sha256=original['sha256'], prompt_version=PROMPT_VERSION,
                             schema_version=SCHEMA_VERSION), ensure_ascii=False)
    run_id = db.execute(
        """INSERT INTO extraction_run(material_id,source_file_id,extractor,extractor_version,params_json,run_at,status,error_text)
           VALUES(?,?,?,?,?,?,?,?) RETURNING run_id""",
        (material_id, original['file_id'], extractor, '0.7.0', params, now(), status, error_text)).fetchone()[0]
    if status != 'succeeded':
        return {'run_id': run_id, 'status': status, 'error': error_text, 'candidates': []}
    added = []
    for row in candidates:
        span = json.dumps({'extractor': extractor, 'human_verified': False, 'page': row['page'],
                           'basis': 'vision direct read of original page %d' % row['page'],
                           'original_file_id': original['file_id'], 'original_sha256': original['sha256']},
                          ensure_ascii=False)
        duplicate = db.execute(
            """SELECT 1 FROM extracted_fact e JOIN extraction_run r USING(run_id)
               WHERE r.source_file_id=? AND e.field_name=? AND e.normalized_value=? AND e.evidence_span=?""",
            (original['file_id'], row['field'], row['canonical'], span)).fetchone()
        if duplicate:
            continue
        extracted_id = db.execute(
            """INSERT INTO extracted_fact(run_id,material_id,field_name,value_type,raw_value,normalized_value,page_no,evidence_span)
               VALUES(?,?,?,?,?,?,?,?) RETURNING extracted_id""",
            (run_id, material_id, row['field'], row['value_type'], row['raw'], row['canonical'], row['page'], span)).fetchone()[0]
        added.append(dict(row, extracted_id=extracted_id))
    return {'run_id': run_id, 'status': 'succeeded', 'candidates': added,
            'extractor': extractor, 'pages': run_info.get('pages')}
