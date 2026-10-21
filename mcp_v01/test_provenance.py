"""Safety checks for inherited provenance and EXIF-only OCR derivatives."""
import base64,io,json,sys,unittest
from pathlib import Path
from unittest import mock
from PIL import Image,ImageOps
import test_v01 as old
from jty_materials.common import digest,now,atomic_write
from jty_materials.workflow import import_material,review_fact
from jty_materials.store import Store
from jty_materials.query import Query
from jty_materials.ocr import Ocr
from jty_materials.provenance import record_comparison

class ProvenanceTests(unittest.TestCase):
 setUp=old.V01Tests.setUp
 def prepare(self):
  old.V01Tests.confirm(self)
  oldmeta=Query(self.c).get_material(1)
  oldfile=next(f for f in oldmeta['files'] if f['role']=='original')
  with Store(self.c,readonly=True) as s:ev=s.db.execute('SELECT event_id FROM verification_event').fetchone()[0]
  source=self.base/'v2.pdf';source.write_bytes(b'%PDF-new-source')
  receipt=import_material(self.c,source,material_id=1,actor='test',ocr_factory=lambda c:old.FakeOcr(c,self.text))
  proof=dict(material_id=1,old_original_file_id=oldfile['file_id'],old_original_sha256=oldfile['sha256'],
   new_original_file_id=receipt['files']['original'],new_original_sha256=receipt['original_sha256'],
   fields={'valid_until':dict(value=old.EXPIRY,status='consistent',historical_event_id=ev,check='fixture exact comparison')})
  return proof,receipt
 def test_inheritance_is_not_a_new_human_verification_or_bid_eligibility(self):
  p,_=self.prepare();record_comparison(self.c,'batches/B-test/consistency.json',p)
  m=Query(self.c).get_material(1)
  self.assertEqual(m['verified_facts'],{})
  self.assertEqual(m['inherited_facts']['valid_until']['value'],old.EXPIRY)
  self.assertFalse(m['inherited_facts']['valid_until']['human_verified_current_original'])
  self.assertFalse(m['eligible_for_bid']);self.assertEqual(m['validity']['status'],'unknown')
  record_comparison(self.c,'batches/B-test/consistency.json',p)
  with Store(self.c,readonly=True) as s:
   self.assertEqual(s.db.execute('SELECT count(*) FROM verification_event').fetchone()[0],1)
   self.assertEqual(s.db.execute("SELECT count(*) FROM audit_log WHERE action='fact.inherit_consistency'").fetchone()[0],1)
 def test_later_withdrawal_hides_inheritance(self):
  p,r=self.prepare();record_comparison(self.c,'batches/B-test/consistency.json',p)
  review_fact(self.c,dict(material_id=1,field='valid_until',value=None,actor='fixture human',reason='withdrawn',evidence_file_id=r['files']['original'],evidence_sha256=r['original_sha256']))
  self.assertEqual(Query(self.c).get_material(1)['inherited_facts'],{})
 def test_new_version_invalidates_inherited_binding(self):
  p,_=self.prepare();record_comparison(self.c,'batches/B-test/consistency.json',p)
  source=self.base/'v3.pdf';source.write_bytes(b'%PDF-third-source')
  import_material(self.c,source,material_id=1,actor='fixture',ocr_factory=lambda c:old.FakeOcr(c,self.text))
  self.assertEqual(Query(self.c).get_material(1)['inherited_facts'],{})
 def test_tampered_comparison_rejected_and_no_manual_event_fabricated(self):
  p,_=self.prepare();record_comparison(self.c,'batches/B-test/consistency.json',p)
  atomic_write(self.c.repository_root/'batches/B-test/consistency.json','{}')
  with self.assertRaises(ValueError):Query(self.c).get_material(1)
  with Store(self.c,readonly=True) as s:self.assertEqual(s.db.execute('SELECT count(*) FROM verification_event').fetchone()[0],1)
 def test_exif_rotates_only_ocr_derivative(self):
  source=self.base/'orientation.jpg';image=Image.new('RGB',(9,5),'white');image.putpixel((0,0),(255,0,0));exif=Image.Exif();exif[274]=3;image.save(source,exif=exif)
  before=source.read_bytes();ocr=Ocr(self.c);seen={}
  def request(route,payload,**kwargs):
   seen['bytes']=base64.b64decode(payload['base64']);return {'code':100,'data':[{'text':'方向测试 ３５ｋＶ'}]}
  with mock.patch.object(ocr,'request',request):result=ocr.image(source)
  with Image.open(io.BytesIO(before)) as original,Image.open(io.BytesIO(seen['bytes'])) as derived:
   self.assertEqual(original.getexif()[274],3)
   self.assertEqual(derived.tobytes(),ImageOps.exif_transpose(original).tobytes())
  self.assertEqual(source.read_bytes(),before)
  self.assertIn('35kV',Path(result['search']).read_text(encoding='utf-8'))
  self.assertEqual(result['options']['input_transform']['orientation'],3)

if __name__=='__main__':unittest.main(verbosity=2)
