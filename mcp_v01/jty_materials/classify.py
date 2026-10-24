"""Automatic material classification for zero-fill upload.

Normal path: the user never selects a type. Deterministic signals from the
OCR text decide; the Vision engine (when available) is an independent second
signal. Agreement → final type. Conflict or no confident signal → the material
imports as 'other' with a classification exception (the only safe neutral
bucket, since material type is immutable afterwards).

Signals are exact document-title/header markers — never guessed from loose
keywords. No signal for a type means that type is NOT auto-classifiable in
v0.7 and stays exception-only.
"""
import re

from .common import normalize

# (type_code, header regex on NFKC text, required secondary signal or None)
_TEXT_SIGNALS = [
    ('business_license', re.compile(r'营业执照')),
    ('safety_prod_license', re.compile(r'安全生产许可证')),
    ('power_facility_license', re.compile(r'承装[（(]修、?试[）)]电力设施许可证|承装修试电力设施许可证')),
    ('construction_qual', re.compile(r'建筑业企业资质证书')),
    ('person_id_card', re.compile(r'居民身份证')),
    ('software_copyright', re.compile(r'计算机软件著作权登记证书')),
    ('patent', re.compile(r'(实用新型|发明|外观设计)专利证书')),
    ('audit_report', re.compile(r'审计报告')),
    ('tax_credit', re.compile(r'纳税信用')),
    ('tax_payment_cert', re.compile(r'(税收)?完税证明')),
    ('award_notice', re.compile(r'中标通知书')),
    ('credit_report', re.compile(r'企业信用报告')),
]
# financial_statement is only claimed when audit_report does not match.
_FALLBACK_SIGNALS = [
    ('financial_statement', re.compile(r'财务报表|资产负债表|利润表|现金流量表')),
    ('iso_cert', re.compile(r'体系认证证书|管理体系认证')),
]


def ocr_classify(text):
    """Deterministic OCR-text classification. Returns (type_code, basis) or
    (None, reason). Only one confident document header may match; two rival
    headers are a conflict, not a guess."""
    if not text or not text.strip():
        return None, 'no_ocr_text'
    text = normalize(text)[:20000]
    matches = []
    for code, pattern in _TEXT_SIGNALS:
        if pattern.search(text):
            matches.append(code)
    if not matches:
        for code, pattern in _FALLBACK_SIGNALS:
            if pattern.search(text):
                matches.append(code)
                break
    if not matches:
        return None, 'no_confident_signal'
    if len(set(matches)) > 1:
        return None, 'rival_headers:' + ','.join(sorted(set(matches)))
    return matches[0], 'ocr_header'


def combine(ocr_signal, vision_signal):
    """ocr_signal: (type|None, basis, confidence) ; vision_signal: same or None.
    Returns (type_code|None, basis, conflict:bool).

    A typed material_type is strong structural identity: only a deterministic
    OCR header signal — alone or confirmed by vision — may bind it. Vision-only
    classification NEVER binds a type; it becomes an exception whose suggested
    type stays visible in the basis (review blocker #5)."""
    ocr_type, ocr_basis, ocr_confidence = ocr_signal
    if vision_signal is None:
        return ocr_type, 'ocr_only:' + ocr_basis, False
    vision_type, _, vision_confidence = vision_signal
    if ocr_type is not None and vision_type == ocr_type:
        return ocr_type, 'ocr_vision_agree', False
    if ocr_type is not None and vision_type in (None, 'unknown'):
        return ocr_type, 'ocr_only:' + ocr_basis, False
    if ocr_type is None:
        suggested = vision_type if vision_type not in (None, 'unknown') else ''
        basis = ('vision_only_suggested:' + suggested) if suggested else 'unclassified'
        return None, basis, True
    return None, f'classification_conflict:{ocr_type}|{vision_type}', True
