"""Human review form support.

The web UI never writes facts directly. Everything here builds a review record
for the same manager RPC the offline CLI uses (manager -> review_fact ->
Store.verify), which enforces: closed field set per type, current-original
evidence binding, reviewer + reason, verification_event + audit_log appends.
"""
from jty_materials.fields import ALLOWED, field_spec, qualification_name, validate_value
from jty_materials.manager import request

from . import catalog


def reviewable_fields(config, material_type):
    """Closed reviewable field set for a type, plus existing qualification items.

    Type 'other' has no certificate_detail row (v04 trigger forbids creating
    one), so its title review is part of the deferred phase-one leftover
    "basic metadata correction entry" and is not offered by the UI either.
    """
    if material_type == "other":
        return [], []
    fields = sorted(ALLOWED.get(material_type, ()))
    quals = []
    if material_type == "construction_qual":
        from jty_materials.store import Store
        with Store(config, readonly=True) as s:
            quals = [r[0] for r in s.db.execute(
                "SELECT DISTINCT qualification_name FROM construction_qual_item ORDER BY 1")]
    return fields, quals


def field_kind(field):
    spec = field_spec(field)
    if spec is not None:
        return spec[2]
    if qualification_name(field) is not None:
        return "text"
    return catalog.FIELD_KIND.get(field, "text")


def convert_value(field, raw):
    """Form value -> canonical value (or None to retract). Raises ValueError."""
    kind = field_kind(field)
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "":
            return None
    if kind == "amount":
        return _to_cents(raw)
    if kind == "signed_amount":
        return _to_cents(raw, signed=True)
    if kind in ("int", "year", "grade"):
        if isinstance(raw, str):
            if not raw.lstrip("-").isdigit():
                raise ValueError("请输入整数")
            raw = int(raw)
        if kind == "year" and not 1900 <= raw <= 2200:
            raise ValueError("年份应在 1900-2200 之间")
        if kind == "grade" and not 1 <= raw <= 5:
            raise ValueError("等级应为 1-5")
        if kind == "int" and raw < 0:
            raise ValueError("不能为负数")
        return raw
    if kind == "bool":
        if isinstance(raw, str):
            if raw in ("true", "1", "是", "yes"):
                return True
            if raw in ("false", "0", "否", "no"):
                return False
            raise ValueError("长期有效取值无效")
        return bool(raw)
    return raw  # date / text / credit strings are validated by validate_value below


def _to_cents(raw, signed=False):
    if isinstance(raw, (int, float)):
        value = raw
    else:
        cleaned = raw.replace(",", "").replace("，", "").replace("¥", "").replace("元", "").strip()
        try:
            value = float(cleaned)
        except ValueError:
            raise ValueError("金额格式无效，请以元为单位填写，例如 1234567.89")
    cents = round(value * 100)
    if not signed and cents < 0:
        raise ValueError("金额不能为负")
    return cents


def submit_review(config, payload):
    """payload: material_id, field, value, actor, reason, evidence_file_id, evidence_sha256, candidate_id?"""
    field = payload["field"]
    value = convert_value(field, payload.get("value"))
    record = {"material_id": int(payload["material_id"]), "field": field, "value": value,
              "actor": (payload.get("actor") or "").strip(),
              "reason": (payload.get("reason") or "").strip(),
              "evidence_file_id": int(payload["evidence_file_id"]),
              "evidence_sha256": payload["evidence_sha256"]}
    if payload.get("candidate_id"):
        record["candidate_id"] = int(payload["candidate_id"])
    validate_value(field, value)
    result = request(config, "review", record, timeout=300)
    return result
