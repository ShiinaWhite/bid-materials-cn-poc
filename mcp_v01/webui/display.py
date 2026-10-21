"""Friendly display names for the UI.

Original titles and files stay untouched everywhere; this layer only derives
what people read. Preference order: typed business info (year / project /
patent / person …) → cleaned-up existing title. Internal naming decorations
(JTY-* codes, hashes, MASTER marks, format suffixes) are stripped from the
FALLBACK text only — the stored title and files are never modified.
"""
import re

from . import catalog

_STRIP_PREFIXES = ("真实脱敏验收：", "首批正式材料：", "正式素材：", "正式材料：")
_INTERNAL_HEAD = re.compile(r"^JTY[-_][A-Za-z]+[-_]\d*[0-9A-Za-z-]*_")
_TAIL_MARKS = re.compile(r"_(MASTER|FINAL|入库册版本|原始扫描件?|扫描件)$")
_HASH_SEG = re.compile(r"^[0-9a-fA-F]{12,64}$")
_FMT_SUFFIX = re.compile(r"(docx|xlsx|doc|xls|pdf|jpg|jpeg|png)$", re.IGNORECASE)
_NUMERIC_SEG = re.compile(r"^\d{1,4}$")

# Source-directory placeholder words that must never be shown as a person's
# name, and generic filing segments that carry no meaning for readers.
_NOISE_WORDS = {"不用", "勿用", "不要用", "不使用", "弃用", "作废", "待定", "待分类",
                "待整理", "其他", "其它", "未知", "无名", "无", "临时", "测试", "样本",
                "公司", "资料", "材料", "文件", "资质材料", "证明材料", "相关资料",
                "扫描件", "复印件", "照片"}
_HUMAN_NAME_MIN = 2

_HINT_FIELDS = ("fiscal_year", "year", "specialty", "project_name",
                "patent_name", "software_name", "rating")


def valid_person_name(name):
    """Placeholder words from source folders must never become a person."""
    text = (name or "").strip()
    return (len(text) >= _HUMAN_NAME_MIN and text not in _NOISE_WORDS
            and not _NUMERIC_SEG.match(text))


def _clean_segments(title):
    text = (title or "").strip()
    for prefix in _STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = _INTERNAL_HEAD.sub("", text)
    while True:
        mark = _TAIL_MARKS.search(text)
        if not mark:
            break
        text = text[:mark.start()]
    segments = []
    for raw in text.split("_"):
        seg = raw.strip()
        if not seg or _HASH_SEG.match(seg):
            continue
        if seg.lower() in ("master", "final"):
            continue
        if len(seg) > 4:
            seg = _FMT_SUFFIX.sub("", seg)
        if seg == "公司" and segments:
            continue  # generic holder tag, meaningful only when it is the whole name
        segments.append(seg)
    # drop noise/placeholder segments unless they are all there is
    meaningful = [s for s in segments if s not in _NOISE_WORDS]
    if meaningful:
        segments = meaningful
    folded = []
    for seg in segments:
        if _NUMERIC_SEG.match(seg) and folded:
            folded[-1] = folded[-1] + " " + seg
        else:
            folded.append(seg)
    while folded and _NUMERIC_SEG.match(folded[0]):
        folded.pop(0)  # leading batch numbers are internal ordering marks
    if len(folded) > 1 and folded[0] == "公司":
        folded.pop(0)  # generic holder tag before a real description
    return [s for s in folded if s]


# Fallback names that stay too terse get one natural-word touch-up, based on
# the source folder context (08_设备车辆, V4资质目录待分类) — no data changes.
_GENERIC_REWRITE = {"车辆资料": "公司车辆资料"}


def clean_title(title):
    """Fallback name: the existing title without internal decorations."""
    segments = _clean_segments(title)
    if segments:
        text = " · ".join(segments)
    else:
        text = (title or "").strip()
        for prefix in _STRIP_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
    for source, natural in _GENERIC_REWRITE.items():
        if text == source:
            text = natural
    if "公司资质" in text:
        text = text.replace("公司资质", "公司资质证书")
    return text


def _hints_from_detail(detail):
    """First verified-then-pending value per hint field."""
    hints = {}
    for field in _HINT_FIELDS:
        value = None
        verified = (detail.get("verified_facts") or {}).get(field)
        if verified is not None:
            value = verified.get("value")
        if value is None:
            cands = (detail.get("candidate_fields") or {}).get(field)
            if cands:
                value = cands[0].get("value")
        if value is not None and str(value).strip():
            hints[field] = value
    return hints


def display_name(material_type, title, *, persons=(), hints=None, cert_no=None):
    """Human-readable name for lists and page titles."""
    mtype = material_type or ""
    hints = hints or {}
    clean = clean_title(title)
    year = hints.get("fiscal_year") or hints.get("year")
    year = str(year) if year else None

    if mtype == "audit_report" and year:
        return f"{year}年度审计报告"
    if mtype == "financial_statement" and year:
        return f"{year}年度财务报表"
    if mtype == "tax_credit" and year:
        rating = hints.get("rating")
        return f"{year}年度纳税信用" + (f"{rating}级" if rating else "")
    if mtype == "tax_payment_cert" and year:
        return f"{year}年度完税证明"
    if mtype == "patent" and hints.get("patent_name"):
        return str(hints["patent_name"]).strip()
    if mtype == "software_copyright" and hints.get("software_name"):
        return str(hints["software_name"]).strip()
    if mtype in ("contract", "award_notice", "acceptance_doc", "owner_evaluation") and hints.get("project_name"):
        project = str(hints["project_name"]).strip()
        label = catalog.TYPE_LABELS.get(mtype, "")
        return project if label and label in project else f"{project} · {label}"
    if mtype.startswith("person"):
        names = [p for p in (persons or ()) if valid_person_name(p)]
        label = catalog.TYPE_LABELS.get(mtype, "")
        if mtype == "person_id_card" and names:
            return f"{names[0]} · 身份证"
        # when the cleaned title already carries the person's name, it usually
        # carries the specific document wording too — keep it
        if names and names[0] not in clean:
            specialty = hints.get("specialty")
            if specialty and len(str(specialty)) <= 20:
                return f"{names[0]} · {str(specialty).strip()}"
            if label:
                return f"{names[0]} · {label}"
    if not clean and cert_no:
        return f"{catalog.TYPE_LABELS.get(mtype, '')} {cert_no}".strip()
    return clean


def display_from_detail(detail):
    """Display name for a full get_material dict."""
    verified_cert = (detail.get("verified_facts") or {}).get("certificate_no")
    cert_no = verified_cert.get("value") if verified_cert else None
    return display_name(
        detail.get("material_type"),
        detail.get("title", ""),
        persons=[p.get("name") for p in (detail.get("persons") or ())],
        hints=_hints_from_detail(detail),
        cert_no=cert_no,
    )
