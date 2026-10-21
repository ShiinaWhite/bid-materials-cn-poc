"""Read-side services for the Web UI.

List pages never load whole materials: ID resolution uses Query.search_ids
(SQLite filters + dowse relevance), then page-sized summaries come from a
single read-only pass over the same database views phase one already exposes.
Material detail reuses Query.get_material unchanged and only annotates it.
"""
import datetime as dt
import json

from jty_materials.common import safe_path
from jty_materials.query import Query
from jty_materials.store import Store

from . import catalog

# v0.1 end-to-end test artifacts (clearly titled, kept in the library with
# their audit history). Until the offline tooling gains a safe archival
# command, ordinary browsing hides them; direct links still work.
TEST_MATERIAL_IDS = {1096, 1097}


def _rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params)]


def type_counts(db):
    counts = {r["code"]: r["n"] for r in _rows(db,
        """SELECT t.code, COUNT(*) n FROM material m JOIN material_type t ON t.type_id=m.material_type_id
           WHERE m.business_status<>'archived' GROUP BY t.code""")}
    groups = {}
    for g in catalog.GROUPS:
        groups[g["key"]] = sum(counts.get(t, 0) for t in g["types"])
    total = sum(counts.values())
    return counts, groups, total


def summarize(config, ids):
    """Page-sized summaries: one read-only connection, small queries.
    Certificate/contract numbers only come from human-maintained detail
    columns, so an empty cell honestly means "not recorded yet"."""
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    with Store(config, readonly=True) as s:
        base = {r["material_id"]: r for r in _rows(s.db, f"""
            SELECT m.material_id, m.material_code, m.title, m.verification_status, m.business_status,
                   m.created_at, t.code AS material_type
            FROM material m JOIN material_type t ON t.type_id=m.material_type_id
            WHERE m.material_id IN ({marks})""", ids)}
        persons = {}
        for r in _rows(s.db, f"""
            SELECT mp.material_id, p.name, mp.relation_status FROM material_person mp
            JOIN person p USING(person_id) WHERE mp.material_id IN ({marks})
            ORDER BY mp.material_id, p.name""", ids):
            persons.setdefault(r["material_id"], []).append(r)
        cand = {r["material_id"]: r["n"] for r in _rows(s.db, f"""
            SELECT material_id, COUNT(*) n FROM extracted_fact
            WHERE material_id IN ({marks}) AND review_status='pending' GROUP BY material_id""", ids)}
        dates = {}
        for r in _rows(s.db, f"""
            SELECT m.material_id, c.valid_until, c.long_term_valid FROM material m
            JOIN certificate_detail c USING(material_id)
            WHERE m.material_id IN ({marks}) AND c.valid_until IS NOT NULL""", ids):
            dates[r["material_id"]] = r
        for r in _rows(s.db, f"""
            SELECT m.material_id, pc.valid_until, pc.long_term_valid FROM material m
            JOIN person_certificate_detail pc USING(material_id)
            WHERE m.material_id IN ({marks}) AND pc.valid_until IS NOT NULL""", ids):
            dates.setdefault(r["material_id"], r)
        originals = {}
        for r in _rows(s.db, f"""
            SELECT material_id, file_id, extension FROM material_file
            WHERE material_id IN ({marks}) AND role='original' AND is_current=1""", ids):
            originals[r["material_id"]] = r
        numbers = {}
        for r in _rows(s.db, f"""
            SELECT m.material_id, c.certificate_no AS no FROM material m
            JOIN certificate_detail c USING(material_id)
            WHERE m.material_id IN ({marks}) AND c.certificate_no IS NOT NULL AND c.certificate_no<>''""", ids):
            numbers[r["material_id"]] = r["no"]
        for r in _rows(s.db, f"""
            SELECT m.material_id, pc.certificate_no AS no FROM material m
            JOIN person_certificate_detail pc USING(material_id)
            WHERE m.material_id IN ({marks}) AND pc.certificate_no IS NOT NULL AND pc.certificate_no<>''""", ids):
            numbers.setdefault(r["material_id"], r["no"])
        for r in _rows(s.db, f"""
            SELECT m.material_id, p.contract_no AS no FROM material m
            JOIN performance_detail p USING(material_id)
            WHERE m.material_id IN ({marks}) AND p.contract_no IS NOT NULL AND p.contract_no<>''""", ids):
            numbers.setdefault(r["material_id"], r["no"])
        # display-name hints: first pending/accepted recognition per field
        hint_fields = ("fiscal_year", "year", "specialty", "project_name",
                       "patent_name", "software_name", "rating")
        marks2 = ",".join("?" for _ in hint_fields)
        hints = {}
        for r in _rows(s.db, f"""
            SELECT material_id, field_name, normalized_value FROM extracted_fact
            WHERE material_id IN ({marks}) AND review_status IN ('pending','accepted')
              AND field_name IN ({marks2}) ORDER BY extracted_id""", list(ids) + list(hint_fields)):
            hints.setdefault((r["material_id"], r["field_name"]), r["normalized_value"])
        # human-maintained business fields for category-specific list columns
        perf = {}
        for r in _rows(s.db, f"""
            SELECT material_id, client_name_text AS client, contract_amount_cents AS amount,
                   contract_date AS cdate FROM performance_detail
            WHERE material_id IN ({marks})""", ids):
            perf[r["material_id"]] = r
        years = {}
        for r in _rows(s.db, f"""
            SELECT material_id, fiscal_year AS y FROM audit_report_detail
            WHERE material_id IN ({marks}) AND fiscal_year IS NOT NULL""", ids):
            years[r["material_id"]] = r["y"]
        for r in _rows(s.db, f"""
            SELECT material_id, tax_year AS y FROM certificate_detail
            WHERE material_id IN ({marks}) AND tax_year IS NOT NULL""", ids):
            years.setdefault(r["material_id"], r["y"])
        ratings = {}
        for r in _rows(s.db, f"""
            SELECT material_id, rating FROM certificate_detail
            WHERE material_id IN ({marks}) AND rating IS NOT NULL AND rating<>''""", ids):
            ratings[r["material_id"]] = r["rating"]
    result = []
    today = dt.date.today().isoformat()
    from . import display as display_names
    for mid in ids:
        row = base.get(mid)
        if row is None:
            continue
        people = persons.get(mid, [])
        validity = dates.get(mid)
        hint_map = {f: hints.get((mid, f)) for f in hint_fields if hints.get((mid, f)) is not None}
        names = [p["name"] for p in people]
        pf = perf.get(mid, {})
        human_year = years.get(mid)
        if human_year:
            hint_map = {"fiscal_year" if row["material_type"] in ("audit_report", "financial_statement") else "year": human_year, **hint_map}
        item = {**row,
                "persons": names,
                "person_status": people[0]["relation_status"] if people else None,
                "candidate_count": cand.get(mid, 0),
                "original": originals.get(mid),
                "cert_no": numbers.get(mid),
                "hints": hint_map,
                "display": display_names.display_name(row["material_type"], row["title"],
                                                      persons=names, hints=hint_map,
                                                      cert_no=numbers.get(mid)),
                "client": pf.get("client"),
                "amount": pf.get("amount"),
                "contract_date": pf.get("cdate"),
                "year": human_year or hint_map.get("fiscal_year") or hint_map.get("year"),
                "rating": ratings.get(mid) or hint_map.get("rating"),
                "valid_until": validity["valid_until"] if validity else None,
                "long_term": bool(validity["long_term_valid"]) if validity else False}
        if item["valid_until"] and not item["long_term"]:
            item["valid_state"] = "expired" if item["valid_until"] < today else "valid"
        else:
            item["valid_state"] = None
        result.append(item)
    return result


def _validity_ids(config, state):
    """Certificate/person-certificate expiry from review-guarded columns only."""
    today = dt.date.today()
    with Store(config, readonly=True) as s:
        rows = _rows(s.db, """SELECT material_id, expires_on FROM v_expiring""")
    if state == "expiring":
        end = (today + dt.timedelta(days=90)).isoformat()
        return [r["material_id"] for r in rows if today.isoformat() <= r["expires_on"] <= end]
    if state == "expired":
        return [r["material_id"] for r in rows if r["expires_on"] < today.isoformat()]
    return []


def list_materials(config, *, type_code=None, group=None, q=None, person=None,
                   validity=None, page=1, page_size=20, order="new"):
    query = Query(config)
    ids = None
    unmapped = []
    if q or person:
        resolved = query.search_ids(query=(q.strip() or None) if q else None,
                                    material_type=type_code, fact_status="any",
                                    person_name=(person.strip() or None) if person else None)
        ids, unmapped = resolved["ids"], resolved["unmapped"]
    if ids is None or validity:
        validity_ids = _validity_ids(config, validity) if validity else None
        if ids is None:
            clauses, params = [], []
            if type_code:
                clauses.append("t.code=?"); params.append(type_code)
            elif group:
                types = next((g["types"] for g in catalog.GROUPS if g["key"] == group), None)
                if types is None:
                    raise ValueError("unknown group")
                clauses.append("t.code IN (%s)" % ",".join("?" * len(types))); params.extend(types)
            with Store(config, readonly=True) as s:
                sql = "SELECT m.material_id FROM material m JOIN material_type t ON t.type_id=m.material_type_id"
                if clauses:
                    sql += " WHERE " + " AND ".join(clauses)
                sql += " ORDER BY m.material_id " + ("DESC" if order == "new" else "ASC")
                ids = [r["material_id"] for r in s.db.execute(sql, params)]
            if order == "new":
                ids.reverse()
        if validity_ids is not None:
            wanted = set(validity_ids)
            ids = [i for i in ids if i in wanted]
    # test materials from the v0.1 end-to-end run stay reachable by direct link
    # but are never recommended in ordinary browsing until a safe archival
    # command exists (no physical deletion, audit history untouched)
    hidden = set(TEST_MATERIAL_IDS)
    if q or person:
        # relevance search keeps hits, but demotes pure test materials to the end
        visible = [i for i in ids if i not in hidden]
        hidden_hits = [i for i in ids if i in hidden]
        ids = visible + hidden_hits
    else:
        ids = [i for i in ids if i not in hidden]
    total = len(ids)
    pages = max(1, -(-total // page_size))
    page = max(1, min(page, pages))
    slice_ids = ids[(page - 1) * page_size: page * page_size]
    return {"items": summarize(config, slice_ids), "total": total, "page": page,
            "pages": pages, "unmapped": unmapped}


def version_history(config, material_id):
    """Original versions grouped by version_id with the registered time."""
    with Store(config, readonly=True) as s:
        rows = _rows(s.db, """
            SELECT file_id, role, relative_path, filename, extension, sha256, size_bytes,
                   is_current, version_id, imported_at
            FROM material_file WHERE material_id=? AND role IN ('original','search_text')
            ORDER BY imported_at""", (material_id,))
    versions = {}
    for r in rows:
        if r["role"] != "original":
            continue
        entry = versions.setdefault(r["version_id"], {"version_id": r["version_id"], "imported_at": r["imported_at"],
                                                      "is_current": False, "original": r, "search": None})
        entry["is_current"] = entry["is_current"] or bool(r["is_current"])
    for r in rows:
        if r["role"] == "search_text" and r["is_current"] and r["version_id"] in versions:
            versions[r["version_id"]]["search"] = r
    return sorted(versions.values(), key=lambda v: (v["imported_at"] or ""), reverse=True)


def annotate_detail(config, detail):
    """Attach everything the detail page needs beyond Query.get_material."""
    mid = detail["material_id"]
    with Store(config, readonly=True) as s:
        row = s.db.execute("SELECT created_at, updated_at FROM material WHERE material_id=?", (mid,)).fetchone()
        versions = version_history(config, mid)
        titles = {}
        rel_persons = {}
        rel_ids = set()
        for r in detail.get("relations", []):
            rel_ids.add(r["material_id"]); rel_ids.add(r["related_material_id"])
        if rel_ids:
            marks = ",".join("?" for _ in rel_ids)
            titles = {r["material_id"]: (r["title"], r["code"]) for r in _rows(s.db, f"""
                SELECT m.material_id, m.title, t.code FROM material m
                JOIN material_type t ON t.type_id=m.material_type_id WHERE m.material_id IN ({marks})""", list(rel_ids))}
            for r in _rows(s.db, f"""
                SELECT mp.material_id, p.name FROM material_person mp
                JOIN person p USING(person_id) WHERE mp.material_id IN ({marks})
                ORDER BY mp.material_id, p.name""", list(rel_ids)):
                rel_persons.setdefault(r["material_id"], []).append(r["name"])
        audit = _rows(s.db, """SELECT at, actor, action, note FROM audit_log
                               WHERE entity_type='material' AND entity_id=?
                               ORDER BY log_id DESC LIMIT 8""", (mid,))
        extraction = None
        current_original = next((f for f in detail["files"] if f["role"] == "original" and f["is_current"]), None)
        if current_original:
            extract_row = s.db.execute("""SELECT run_id, run_at, status, extractor FROM extraction_run
                                  WHERE material_id=? AND source_file_id=? ORDER BY run_id DESC LIMIT 1""",
                                (mid, current_original["file_id"])).fetchone()
            if extract_row:
                extraction = dict(extract_row)
    relations = []
    from . import display as display_names
    for r in detail.get("relations", []):
        other = r["related_material_id"] if r["material_id"] == mid else r["material_id"]
        title, code = titles.get(other, ("", ""))
        other_display = display_names.display_name(code, title, persons=rel_persons.get(other, ()))
        relations.append({**r, "other_id": other, "other_title": title,
                          "other_display": other_display, "other_type": code})
    detail = dict(detail)
    detail["created_at"] = row["created_at"]
    detail["updated_at"] = row["updated_at"]
    detail["versions"] = versions
    detail["relations_view"] = relations
    detail["recent_audit"] = audit
    detail["extraction"] = extraction
    return detail


def home_stats(config):
    with Store(config, readonly=True) as s:
        counts, groups, total = type_counts(s.db)
        pending_candidates = s.db.execute(
            "SELECT COUNT(DISTINCT material_id) FROM extracted_fact WHERE review_status='pending'").fetchone()[0]
        pending_relations = s.db.execute(
            "SELECT COUNT(*) FROM material_relation WHERE relation_status='relation_pending'").fetchone()[0]
        unverified = s.db.execute(
            "SELECT COUNT(*) FROM material WHERE verification_status<>'human_verified'").fetchone()[0]
        candidate_persons = s.db.execute(
            "SELECT COUNT(DISTINCT person_id) FROM person WHERE identity_status<>'human_verified'").fetchone()[0]
        metadata_only = s.db.execute(
            """SELECT COUNT(DISTINCT e.material_id) FROM extraction_run e
               JOIN material_file f ON f.file_id=e.source_file_id
               WHERE e.status='partial' AND f.is_current=1""").fetchone()[0]
    today = dt.date.today().isoformat()
    expiring = [r for r in _validity_ids(config, "expiring")]
    expired = _validity_ids(config, "expired")
    latest_ids = []
    with Store(config, readonly=True) as s:
        excluded = ",".join(str(i) for i in sorted(TEST_MATERIAL_IDS)) or "0"
        latest_ids = [r[0] for r in s.db.execute(
            f"SELECT material_id FROM material WHERE material_id NOT IN ({excluded}) ORDER BY material_id DESC LIMIT 8")]
    return {"counts": counts, "groups": groups, "total": total,
            "latest": summarize(config, latest_ids),
            "pending_candidates": pending_candidates, "pending_relations": pending_relations,
            "unverified": unverified, "candidate_persons": candidate_persons,
            "metadata_only": metadata_only,
            "expiring": len(expiring), "expired": len(expired)}


def backup_list(config, limit=6):
    out = []
    if config.backup_dir.is_dir():
        for p in sorted(config.backup_dir.iterdir(), reverse=True):
            if not p.is_dir():
                continue
            complete = (p / "COMPLETE").is_file()
            at = None
            manifest = p / "manifest.json"
            if manifest.is_file():
                try:
                    info = json.loads(manifest.read_text(encoding="utf-8"))
                    at, files = info.get("at"), info.get("files")
                except (ValueError, OSError):
                    at = None
            out.append({"name": p.name, "complete": complete, "at": at, "path": str(p)})
            if len(out) >= limit:
                break
    return out


def quick_status(config):
    from jty_materials.manager import request
    status = request(config, "status", start=False)
    counts = {}
    with Store(config, readonly=True) as s:
        counts["materials"] = s.db.execute("SELECT COUNT(*) FROM material").fetchone()[0]
        counts["files"] = s.db.execute("SELECT COUNT(*) FROM material_file").fetchone()[0]
        counts["verification_events"] = s.db.execute("SELECT COUNT(*) FROM verification_event").fetchone()[0]
        counts["schema"] = s.db.execute(
            "SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
    return {"manager": status, "maintenance": config.maintenance_file.exists(),
            "generation": config.generation(), "counts": counts,
            "backups": backup_list(config),
            "index_exists": (config.index_dir / "meta.json").exists()}


def resolve_file(config, material_id, file_id):
    with Store(config, readonly=True) as s:
        row = s.db.execute("""SELECT file_id, material_id, role, relative_path, filename, extension,
                              sha256, size_bytes, is_current FROM material_file
                              WHERE file_id=? AND material_id=?""", (file_id, material_id)).fetchone()
    if row is None:
        return None
    row = dict(row)
    row["path"] = safe_path(config.repository_root, row["relative_path"])
    return row


_sys_state = {"ok": None, "at": 0.0}


def system_ok(config, max_age=15):
    """Footer indicator. Manager busy = alive; missing = maintenance needed."""
    import time
    now = time.time()
    if now - _sys_state["at"] <= max_age:
        return _sys_state["ok"]
    from jty_materials.manager import request
    try:
        status = request(config, "status", start=False, timeout=3)
        ok = bool(status.get("running"))
    except (TimeoutError, RuntimeError):
        ok = True  # alive but busy with a save/upload
    except Exception:
        ok = False
    _sys_state["ok"] = ok
    _sys_state["at"] = now
    return ok


def ocr_text(config, material_id, cap_bytes=2 * 1024 * 1024):
    query = Query(config)
    material = query.get_material(material_id)
    f = next((f for f in material["files"] if f["role"] == "search_text" and f["is_current"]), None)
    if f is None or not f["integrity_ok"]:
        return {"state": "missing", "text": None,
                "note": "当前版本没有可用的检索文本（该原件可能未提取到正文或为照片/占位材料）"}
    path = safe_path(config.repository_root, f["relative_path"])
    size = path.stat().st_size
    if size == 0:
        return {"state": "empty", "text": "", "note": "检索文本为空"}
    text = path.read_text(encoding="utf-8")
    state = "ok"
    note = None
    if len(text.encode("utf-8")) > cap_bytes:
        text = text.encode("utf-8")[:cap_bytes].decode("utf-8", "ignore")
        state, note = "truncated", "文本较长，仅显示前一部分；完整 sidecar 原样保存在库中"
    return {"state": state, "text": text, "note": note, "sha256": f["sha256"]}
