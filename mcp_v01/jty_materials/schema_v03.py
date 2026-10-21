"""One construction detail table; reuse certificate scope and ledger evidence."""
from .common import now
from .schema_v02 import upgrade as upgrade_v02, fact_trigger
from .fields import CERTIFICATES

VERSION='jty-mcp-0.3.0'

def upgrade(con):
    version=con.execute("SELECT meta_value FROM schema_meta WHERE meta_key='schema_version'").fetchone()[0]
    if version==VERSION:return False
    if version=='jty-mcp-0.1.0':upgrade_v02(con)
    elif version!='jty-mcp-0.2.0':raise ValueError('v0.3 migration requires v0.1 or v0.2')
    con.execute('ALTER TABLE certificate_detail ADD COLUMN standard TEXT')
    con.execute('''CREATE TABLE construction_qual_item(
      material_id INTEGER NOT NULL REFERENCES certificate_detail(material_id),
      qualification_name TEXT NOT NULL CHECK(length(qualification_name) BETWEEN 1 AND 120 AND trim(qualification_name)=qualification_name),
      grade TEXT CHECK(grade IS NULL OR (length(trim(grade))>0 AND length(grade)<=4000)),
      PRIMARY KEY(material_id,qualification_name)) STRICT''')
    con.execute('DROP TRIGGER v02_certificate_type')
    mapping="(SELECT code FROM material_type WHERE type_id=(SELECT material_type_id FROM material WHERE material_id=NEW.material_id))"
    combos=' OR '.join(f"({mapping}='{t}' AND NEW.certificate_kind='{k}')" for t,k in CERTIFICATES.items())
    con.execute(f"CREATE TRIGGER v03_certificate_type BEFORE INSERT ON certificate_detail WHEN NOT ({combos}) BEGIN SELECT RAISE(ABORT,'certificate type/kind mismatch'); END")
    con.execute('''CREATE TRIGGER v03_qual_insert BEFORE INSERT ON construction_qual_item
      WHEN NEW.grade IS NOT NULL OR review_actor() IS NULL
        OR review_material() IS NOT NEW.material_id OR review_field() IS NOT ('qualification:'||NEW.qualification_name)
        OR EXISTS(SELECT 1 FROM construction_qual_item WHERE material_id=NEW.material_id AND qualification_name=NEW.qualification_name)
        OR NOT EXISTS(SELECT 1 FROM material m JOIN material_type t ON t.type_id=m.material_type_id WHERE m.material_id=NEW.material_id AND t.code='construction_qual')
      BEGIN SELECT RAISE(ABORT,'create empty qualification only during explicit construction review; no replace'); END''')
    con.execute('''CREATE TRIGGER v03_qual_identity BEFORE UPDATE ON construction_qual_item
      WHEN NEW.material_id<>OLD.material_id OR NEW.qualification_name<>OLD.qualification_name
      BEGIN SELECT RAISE(ABORT,'withdraw old qualification and review a new name'); END''')
    con.execute("CREATE TRIGGER v03_qual_delete BEFORE DELETE ON construction_qual_item BEGIN SELECT RAISE(ABORT,'retain qualification history'); END")
    fact_trigger(con,'v03_review_qual_grade','construction_qual_item','UPDATE','NEW.grade IS NOT OLD.grade',"'qualification:'||NEW.qualification_name",'CAST(OLD.grade AS TEXT)','NEW.grade','NEW.material_id')
    for op in ('INSERT','UPDATE'):
        fact_trigger(con,'v03_review_standard_'+op,'certificate_detail',op,'NEW.standard IS NOT NULL' if op=='INSERT' else 'NEW.standard IS NOT OLD.standard',"'standard'",'NULL' if op=='INSERT' else 'OLD.standard','NEW.standard','NEW.material_id')
    view=con.execute("SELECT sql FROM sqlite_schema WHERE name='v_current_facts'").fetchone()[0]
    con.execute('DROP VIEW v_current_facts')
    con.execute(view+''' UNION ALL SELECT m.material_id,m.material_code,'certificate_detail','standard','text',c.standard,NULL,NULL,NULL
      FROM material m JOIN certificate_detail c USING(material_id) WHERE c.standard IS NOT NULL
      UNION ALL SELECT m.material_id,m.material_code,'construction_qual_item','qualification:'||q.qualification_name,'text',q.grade,NULL,NULL,NULL
      FROM material m JOIN construction_qual_item q USING(material_id) WHERE q.grade IS NOT NULL''')
    con.execute("UPDATE schema_meta SET meta_value=?,updated_at=? WHERE meta_key='schema_version'",(VERSION,now()))
    return True
