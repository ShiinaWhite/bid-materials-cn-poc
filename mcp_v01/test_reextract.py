import io,json,unittest
from unittest import mock
from PIL import Image
import test_v01 as old
from jty_materials.common import atomic_write,digest,safe_path
from jty_materials.query import Query
from jty_materials.store import Store
from jty_materials.reextract import reextract
from jty_materials.phase_ocr import PhaseOcr

class ReextractTests(unittest.TestCase):
 setUp=old.V01Tests.setUp
 def record(self):
  m=Query(self.c).get_material(1);o=next(f for f in m['files'] if f['role']=='original' and f['is_current']);s=next(f for f in m['files'] if f['role']=='search_text' and f['is_current'])
  return dict(material_id=1,evidence_file_id=o['file_id'],evidence_sha256=o['sha256'],expected_search_file_id=s['file_id'],operation_id='fixture-retry-1',actor='fixture machine',reason='same original, append OCR only')
 def test_append_derivatives_preserves_manual_facts_original_and_replay(self):
  old.V01Tests.confirm(self);before=Query(self.c).get_material(1);r=self.record();n=reextract(self.c,r,ocr_factory=lambda c:old.FakeOcr(c,'重新识别 全角３５ｋＶ'))
  after=Query(self.c).get_material(1);self.assertEqual(before['verified_facts'],after['verified_facts']);self.assertEqual(len(after['files']),7)
  self.assertEqual([f for f in before['files'] if f['role']=='original'],[f for f in after['files'] if f['role']=='original'])
  self.assertTrue(all(f['integrity_ok'] for f in after['files']));self.assertIn('35kV',Query(self.c).get_material_text(1)['text'])
  self.assertEqual(reextract(self.c,r,ocr_factory=lambda c:None),n)
  with Store(self.c,readonly=True) as s:
   self.assertEqual(s.db.execute('SELECT count(*) FROM verification_event').fetchone()[0],1)
   self.assertEqual(s.db.execute("SELECT count(*) FROM audit_log WHERE action='material.reextract.commit'").fetchone()[0],1)
   self.assertEqual(s.db.execute("SELECT count(*) FROM material_file WHERE role='original'").fetchone()[0],1)
  for f in before['files']:self.assertEqual(digest(safe_path(self.c.repository_root,f['relative_path'])),f['sha256'])
  with self.assertRaises(ValueError):reextract(self.c,dict(r,operation_id='another'),ocr_factory=lambda c:None)
 def test_empty_or_changed_evidence_does_not_switch_current(self):
  r=self.record();before=Query(self.c).get_material(1)
  with self.assertRaises(ValueError):reextract(self.c,dict(r,evidence_sha256='0'*64),ocr_factory=lambda c:None)
  with self.assertRaises(ValueError):reextract(self.c,r,ocr_factory=lambda c:old.FakeOcr(c,''))
  self.assertEqual(before,Query(self.c).get_material(1))
 def test_engine_start_failure_does_not_poison_next_document(self):
  engine=mock.Mock();engine.start.side_effect=[TimeoutError('fixture startup failure'),None];engine.request.return_value={'code':100,'data':[{'text':'第二次成功'}]}
  buf=io.BytesIO();Image.new('RGB',(20,20),'white').save(buf,format='PNG');ocr=PhaseOcr(self.c)
  with mock.patch('jty_materials.phase_ocr.Ocr',return_value=engine):
   with self.assertRaises(TimeoutError):ocr.recognize(buf.getvalue())
   self.assertIsNone(ocr.engine);self.assertEqual(ocr.recognize(buf.getvalue())[0],'第二次成功');self.assertEqual(engine.start.call_count,2)
  ocr.close()
 def test_successful_no_text_attempt_is_explicit_metadata_not_engine_failure(self):
  class NoText(old.FakeOcr):
   def document(self,p):
    r=super().document(p);atomic_write(r['search'],'[METADATA ONLY: content not extracted]\nphoto.jpg');r['options']={'text_status':'metadata_only','errors':[]};return r
  r=reextract(self.c,self.record(),ocr_factory=lambda c:NoText(c,''))
  self.assertEqual(r['text_status'],'metadata_only')
  with Store(self.c,readonly=True) as s:
   latest=s.db.execute('SELECT params_json,status FROM extraction_run ORDER BY run_id DESC LIMIT 1').fetchone()
   self.assertEqual(json.loads(latest['params_json'])['errors'],[]);self.assertEqual(latest['status'],'partial')
 def test_committed_reply_lost_does_not_stop_supervisor(self):
  from jty_materials.manager import serve
  conn1=mock.MagicMock();conn1.__enter__.return_value=conn1;conn1.poll.return_value=True;conn1.recv_bytes.return_value=b'{"action":"backup"}';conn1.send_bytes.side_effect=BrokenPipeError('fixture disconnected client')
  conn2=mock.MagicMock();conn2.__enter__.return_value=conn2;conn2.poll.return_value=True;conn2.recv_bytes.return_value=b'{"action":"shutdown"}'
  listener=mock.MagicMock();listener.__enter__.return_value=listener;listener.accept.side_effect=[conn1,conn2]
  supervisor=mock.Mock();supervisor.phase_ocr=None;supervisor.dispatch.return_value={'ok':True}
  with mock.patch('jty_materials.manager.Listener',return_value=listener),mock.patch('jty_materials.manager.Supervisor',return_value=supervisor):serve(self.c)
  self.assertEqual(supervisor.dispatch.call_count,2);supervisor.d.close.assert_called_once()
