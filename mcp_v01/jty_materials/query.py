import datetime as dt
from pathlib import Path

from .common import digest, normalize, safe_path, consistent_read
from .rpc import Rpc
from .store import Store
from .fields import CERTIFICATES,ALLOWED,FIELDS,ORG_FIELDS,validate_value,allowed_field,qualification_name


class Query:
    def __init__(self, config):
        self.c = config

    @staticmethod
    def integer(value, name, minimum=0, maximum=2**63-1):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer in [{minimum},{maximum}]")

    @consistent_read
    def get_material(self, material_id):
        self.c.assert_readable()
        self.integer(material_id, "material_id", 1)
        with Store(self.c, readonly=True) as s:
            m = s.db.execute("SELECT m.*,t.code AS material_type FROM material m JOIN material_type t ON t.type_id=m.material_type_id WHERE material_id=?",
                             (material_id,)).fetchone()
            if m is None:
                raise ValueError("material not found")
            entity=s.db.execute('SELECT org_id,org_code FROM org WHERE org_id=?',(m['subject_org_id'],)).fetchone()
            certificate=s.db.execute('SELECT certificate_kind FROM certificate_detail WHERE material_id=?',(material_id,)).fetchone()
            files = [dict(r) for r in s.db.execute("SELECT file_id,role,relative_path,sha256,size_bytes,is_current,version_id FROM material_file WHERE material_id=?", (material_id,))]
            for f in files:
                p = safe_path(self.c.repository_root, f["relative_path"])
                f["integrity_ok"] = p.is_file() and digest(p) == f["sha256"]
            originals = {f["file_id"] for f in files if f["role"] == "original" and f["is_current"] and f["integrity_ok"]}
            facts = {}
            sql = """SELECT cf.*,v.accepted_value,v.verification_method,v.verified_by,v.last_verified_at,
                v.evidence_file_id,v.last_action
                FROM v_current_facts cf JOIN v_field_verification v
                ON v.material_id=cf.material_id AND v.field_name=cf.field_name
                WHERE cf.material_id=? AND v.verification_method IN ('manual_original','dual_engine_consensus')
                  AND v.last_action IN ('set','verify','correct')"""
            for f in s.db.execute(sql, (material_id,)):
                if not allowed_field(m['material_type'],f['field_name']):continue
                value = next((f[k] for k in ("value_text", "value_date", "value_int", "value_amount_cents") if f[k] is not None), None)
                if f["evidence_file_id"] in originals and str(value) == f["accepted_value"]:
                    evidence=next(x for x in files if x['file_id']==f['evidence_file_id'])
                    facts[f["field_name"]] = {"value": bool(value) if f['value_type']=='bool' else value, "value_type": f["value_type"],
                        "verified_by": f["verified_by"], "verified_at": f["last_verified_at"],
                        "verification_method": f["verification_method"],
                        "evidence_file_id": f["evidence_file_id"], 'evidence_sha256':evidence['sha256'],
                        'evidence_relative_path':evidence['relative_path']}
            from .provenance import inherited_facts
            inherited = inherited_facts(self.c, s.db, material_id, m['material_type'], files)
            from .business import business_metadata
            business=business_metadata(s.db,material_id,m['material_type'],originals)
            inherited_expiry=inherited.get('valid_until',{}).get('value')
            inherited_validity={'status':'unknown','term':'unknown','basis':'no inherited date'}
            if inherited_expiry:
                inherited_validity={'status':'expired' if inherited_expiry<dt.date.today().isoformat() else 'valid','term':'dated','basis':'historical_manual_with_machine_consistency','valid_until':inherited_expiry}
            expiry = facts.get("valid_until", {}).get("value")
            today=dt.date.today().isoformat();starts=facts.get('valid_from',{}).get('value')
            term='long_term' if facts.get('long_term_valid',{}).get('value') is True else 'dated' if expiry is not None else 'unknown'
            status='unknown' if term=='unknown' else 'not_yet_valid' if starts and starts>today else 'expired' if term=='dated' and expiry<today else 'valid'
            if m['material_type']=='power_facility_license' and not starts:status='unknown'
            qual_items=[dict(qualification_name=qualification_name(field),grade=fact['value'],**{k:v for k,v in fact.items() if k!='value'}) for field,fact in facts.items() if qualification_name(field) is not None]
            required={'safety_prod_license':{'valid_until'},'business_license':set(ORG_FIELDS),
                      'construction_qual':{'certificate_no','issuing_authority','valid_until'},
                      'iso_cert':{'certificate_no','issuing_authority','standard','scope','valid_until'},
                      'power_facility_license':{'certificate_no','issuing_authority','valid_from','valid_until','scope','install_grade','repair_grade','test_grade'}}.get(m['material_type'])
            eligible=bool(required is not None and required.issubset(facts) and status=='valid' and m['business_status']=='active' and m['verification_status']=='human_verified')
            if m['material_type']=='construction_qual' and not qual_items:eligible=False
            return {"material_id": m["material_id"], "material_code": m["material_code"], "title": m["title"],
                "material_type": m["material_type"], "business_status": m["business_status"],
                "verification_status": m["verification_status"], "files": files, "verified_facts": facts,
                'subject_org':dict(entity) if entity else None,'certificate_kind':certificate[0] if certificate else None,
                'inherited_facts':inherited,
                'inherited_validity':inherited_validity,**business,
                'inherited_fact_policy':'Historical manual verification plus machine consistency only; not a new human review of this original. Separate from verified_facts; excluded from verified-only filters and bid eligibility.',
                'construction_qual_items':qual_items,
                'certification_scope':facts.get('scope') if m['material_type']=='iso_cert' else None,
                'validity':{'term':term,'status':status,'as_of_date':today},
                "candidates": [dict(r) for r in s.db.execute("SELECT extracted_id,field_name,raw_value,normalized_value,review_status,evidence_span FROM extracted_fact WHERE material_id=?", (material_id,))],
                "eligible_for_bid": bool(eligible), "as_of_date": dt.date.today().isoformat(),
                "eligibility_rule": "current human-verified type-specific fields, active, explicitly known valid term; unknown is excluded; project-specific qualification is not assessed",
                "fixture_only": self.c.raw.get("fixture_only", False)}

    @consistent_read
    def get_material_text(self, material_id):
        material = self.get_material(material_id)
        f = next((f for f in material["files"] if f["role"] == "search_text" and f["is_current"]), None)
        if f is None or not f["integrity_ok"]:
            raise ValueError("current search text missing or checksum changed")
        p = safe_path(self.c.repository_root, f["relative_path"])
        if p.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("text exceeds v0.1's 20 MiB memory budget; no truncated text returned")
        return {"material_id": material_id, "relative_path": f["relative_path"], "sha256": f["sha256"],
                "text": p.read_text(encoding="utf-8"), "complete": True, "source": "nfkc_search_sidecar"}

    def text_hits(self, query):
        import os
        hits = []
        with Rpc([str(self.c.dowse_exe), "mcp"], env=dict(os.environ, DOWSE_INDEX_DIR=str(self.c.index_dir))) as rpc:
            offset = 0
            while True:
                result = rpc.tool("search", {"query": normalize(query), "ext": "txt", "limit": 100, "offset": offset})
                hits.extend(result["hits"])
                if len(hits) >= result["total_hits"]:
                    return hits
                if not result["hits"] or len(hits) > 10000:
                    raise RuntimeError("dowse results incomplete; narrow the query")
                offset += len(result["hits"])

    @consistent_read
    def search_ids(self, query=None, material_type=None, min_amount_cents=None,
                   expires_within_days=None, fact_status='verified',person_name=None,
                   person_id=None,material_id=None):
        """Resolve matching material IDs (SQLite filters + dowse relevance) without
        reading per-material files. Shared by MCP search and the Web UI list pages."""
        self.c.assert_readable()
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 1000):
            raise ValueError("query must be nonempty text of at most 1000 characters")
        if material_type not in (None,'certificate',*ALLOWED):
            raise ValueError('unsupported material_type')
        if fact_status not in ('verified','inherited','candidate','any'):raise ValueError('invalid fact_status')
        if person_id is not None:self.integer(person_id,'person_id',1)
        if person_name is not None and not isinstance(person_name,str):raise ValueError('person_name must be text')
        if material_id is not None:self.integer(material_id,'material_id',1)
        clauses = ["1=1"]; params = []
        if material_id is not None:
            clauses.append('m.material_id=?');params.append(material_id)
        if material_type:
            if material_type=='certificate':clauses.append('t.code IN ('+','.join('?' for _ in CERTIFICATES)+')');params.extend(CERTIFICATES)
            else:clauses.append('t.code=?');params.append(material_type)
        if person_name is not None or person_id is not None:
            person_clauses=['mp.material_id=m.material_id',"o.material_id=m.material_id AND o.role='original' AND o.is_current=1"]
            if person_name is not None:person_clauses.append('instr(pe.name,?)>0');params.append(person_name)
            if person_id is not None:person_clauses.append('pe.person_id=?');params.append(person_id)
            if fact_status not in ('any','candidate'):person_clauses.append("mp.relation_status='human_verified'")
            clauses.append('EXISTS (SELECT 1 FROM material_person mp JOIN person pe USING(person_id) JOIN material_file o ON o.file_id=mp.evidence_file_id WHERE '+' AND '.join(person_clauses)+')')
        if min_amount_cents is not None:
            self.integer(min_amount_cents, "min_amount_cents")
            if fact_status=='verified':clauses.append("p.contract_amount_cents>=?"); params.append(min_amount_cents)
        if expires_within_days is not None:
            self.integer(expires_within_days, "expires_within_days", 0, 3650)
            today = dt.date.today()
            if fact_status=='verified':
                clauses.append("c.valid_until BETWEEN ? AND ?")
                params.extend([today.isoformat(), (today + dt.timedelta(days=expires_within_days)).isoformat()])
        with Store(self.c, readonly=True) as s:
            sql = """SELECT m.material_id FROM material m JOIN material_type t ON t.type_id=m.material_type_id
                LEFT JOIN certificate_detail c USING(material_id) LEFT JOIN performance_detail p USING(material_id)
                WHERE """ + " AND ".join(clauses) + " ORDER BY m.material_id"
            ids = [r[0] for r in s.db.execute(sql, params)]
            unmapped = []
            if query is not None:
                ordered = []
                for hit in self.text_hits(query):
                    raw = hit["path"]
                    path = Path(raw[4:] if raw.startswith("\\\\?\\") else raw).resolve()
                    if not path.is_relative_to(self.c.repository_root):
                        unmapped.append(raw)
                        continue
                    relative = path.relative_to(self.c.repository_root).as_posix()
                    safe_path(self.c.repository_root, relative)
                    row = s.db.execute("""SELECT p.material_id,p.sha256 FROM search_projection p JOIN material_file f ON f.file_id=p.search_file_id
                        WHERE p.rel_path_key=lower(?) AND f.role='search_text' AND f.is_current=1 AND f.sha256=p.sha256""", (relative,)).fetchone()
                    if row is None:
                        unmapped.append(relative)
                    elif not path.is_file() or digest(path) != row[1]:
                        raise RuntimeError("search projection checksum mismatch; run managed index to repair")
                    elif row[0] in ids and row[0] not in ordered:
                        ordered.append(row[0])
                ids = ordered
        return {"ids": ids, "unmapped": unmapped}

    @consistent_read
    def search_materials(self, query=None, material_type=None, min_amount_cents=None,
                         expires_within_days=None, eligible_only=False, limit=20, offset=0,
                         enterprise_name=None,unified_social_credit_code=None,established_on=None,
                         certificate_no=None,issuing_authority=None,scope_contains=None,
                         install_grade=None,repair_grade=None,test_grade=None,long_term_valid=None,validity_status=None,
                         qualification_name=None,qualification_grade=None,standard_contains=None,
                         certification_scope_contains=None,valid_from=None,valid_until=None,
                         fact_status='verified',person_name=None,person_id=None,material_id=None,**business_filters):
        self.c.assert_readable()
        self.integer(limit, "limit", 1, 100)
        self.integer(offset, "offset")
        if type(eligible_only) is not bool:
            raise ValueError("eligible_only must be boolean")
        for field,value in business_filters.items():
            if field=='legal_status':
                if value not in ('unknown','valid','expired','terminated','pledged','abandoned'):raise ValueError('invalid legal status')
            else:validate_value(field,value)
        filters={k:v for k,v in dict(enterprise_name=enterprise_name,unified_social_credit_code=unified_social_credit_code,
            established_on=established_on,certificate_no=certificate_no,issuing_authority=issuing_authority,
            install_grade=install_grade,repair_grade=repair_grade,test_grade=test_grade,long_term_valid=long_term_valid,
            valid_from=valid_from,valid_until=valid_until).items() if v is not None}
        for f,v in filters.items():validate_value(f,v)
        if scope_contains is not None:validate_value('scope',scope_contains)
        for field,value in [('scope',certification_scope_contains),('standard',standard_contains),('scope',qualification_name),('scope',qualification_grade)]:
            if value is not None:validate_value(field,value)
        if validity_status not in (None,'unknown','valid','expired','not_yet_valid'):raise ValueError('invalid validity_status')
        resolved = self.search_ids(query=query, material_type=material_type, min_amount_cents=min_amount_cents,
                                   expires_within_days=expires_within_days, fact_status=fact_status,
                                   person_name=person_name, person_id=person_id, material_id=material_id)
        ids, unmapped = resolved["ids"], resolved["unmapped"]
        materials = [self.get_material(i) for i in ids]
        if min_amount_cents is not None:
            if fact_status=='verified':materials = [m for m in materials if m["verified_facts"].get("contract_amount", {}).get("value", -1) >= min_amount_cents]
        if expires_within_days is not None:
            if fact_status=='verified':materials = [m for m in materials if "valid_until" in m["verified_facts"]]
        if eligible_only:
            materials = [m for m in materials if m["eligible_for_bid"]]
        def values(m,f):
            result=[]
            if fact_status in ('verified','any') and f in m['verified_facts']:result.append(m['verified_facts'][f]['value'])
            if fact_status in ('inherited','any') and f in m.get('inherited_facts',{}):result.append(m['inherited_facts'][f]['value'])
            if fact_status in ('candidate','any'):result.extend(c['value'] for c in m.get('candidate_fields',{}).get(f,[]))
            if f=='legal_status' and m.get('legal_status'):result.append(m['legal_status']['value'])
            return result
        materials=[m for m in materials if all(v in values(m,f) for f,v in filters.items())]
        for field,value in business_filters.items():
            materials=[m for m in materials if any((str(value).casefold() in str(v).casefold() if field in ('project_name','client','patent_name','software_name','owner','patentee','audit_firm','title','specialty','location') else v==value) for v in values(m,field))]
        if person_name is not None or person_id is not None:
            materials=[m for m in materials if any((person_name is None or person_name in p['name']) and (person_id is None or person_id==p['person_id']) and (fact_status in ('any','candidate') or p['relation_status']=='human_verified') for p in m.get('persons',[]))]
        if min_amount_cents is not None and fact_status!='verified':materials=[m for m in materials if any(v>=min_amount_cents for v in values(m,'contract_amount'))]
        if expires_within_days is not None and fact_status!='verified':
            end=(dt.date.today()+dt.timedelta(days=expires_within_days)).isoformat();today=dt.date.today().isoformat()
            materials=[m for m in materials if any(today<=v<=end for v in values(m,'valid_until'))]
        if scope_contains is not None:materials=[m for m in materials if any(scope_contains in v for v in values(m,'scope'))]
        if certification_scope_contains is not None:materials=[m for m in materials if m['material_type']=='iso_cert' and any(certification_scope_contains in v for v in values(m,'scope'))]
        if standard_contains is not None:materials=[m for m in materials if m['material_type']=='iso_cert' and any(standard_contains in v for v in values(m,'standard'))]
        if qualification_name is not None or qualification_grade is not None:
            def qualification_match(m):
                names={field.split(':',1)[1] for section in ['verified_facts','inherited_facts','candidate_fields'] for field in m.get(section,{}) if field.startswith('qualification:')}
                return any((qualification_name is None or name==qualification_name) and any(qualification_grade is None or grade==qualification_grade for grade in values(m,'qualification:'+name)) for name in names)
            materials=[m for m in materials if qualification_match(m)]
        if validity_status is not None:materials=[m for m in materials if (m['inherited_validity']['status'] if fact_status=='inherited' or (fact_status=='any' and m['validity']['status']=='unknown') else m['validity']['status'])==validity_status]
        self.c.assert_readable()
        return {"materials": materials[offset:offset+limit], "total": len(materials), "offset": offset,
                "limit": limit, "as_of_date": dt.date.today().isoformat(), "unmapped_text_hits": unmapped,
                'fact_status':fact_status,'candidate_warning':'candidate and inherited results stay separate from current manual verification; never infer bid eligibility'}
