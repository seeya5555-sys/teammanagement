import hashlib, os, tempfile, unittest
if not hasattr(hashlib,'scrypt'):
 import werkzeug.security; werkzeug.security.generate_password_hash=lambda value,*a,**k:'test-hash'
import app as A
import migration_steps
from source_bundle import shared_ns

class T(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.od=A.DATABASE;self.oc=A.app.config['DATABASE'];db=os.path.join(self.t.name,'t.db');A.DATABASE=db;A.app.config['DATABASE']=db
  with A.app.app_context():
   A.init_db(False);shared_ns._ensure_api_table();A.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','secret')")
   A.execute('CREATE TABLE IF NOT EXISTS class_status(id INTEGER PRIMARY KEY,vessel_id INTEGER)')
   A.execute('CREATE TABLE IF NOT EXISTS class_status_items(id INTEGER PRIMARY KEY,cs_id INTEGER,category TEXT,description TEXT,remark TEXT,due_date TEXT,action_taken TEXT)')
   migration_steps._class_followup_evidence(A.get_db())
   vid=A.execute("INSERT INTO vessels(name,active) VALUES('SHIP',1)");A.execute('INSERT INTO class_status(id,vessel_id) VALUES(1,?)',(vid,));A.execute("INSERT INTO class_status_items(id,cs_id,category,description,due_date,action_taken) VALUES(1,1,'STATUTORY','IOPP renewal','2026-10-01','')")
  self.c=A.app.test_client();self.h={'X-API-Key':'secret'}
 def tearDown(self):A.DATABASE=self.od;A.app.config['DATABASE']=self.oc;self.t.cleanup()
 def test_retired_candidates_are_empty_without_db_scan(self):
  from unittest.mock import patch
  import routes_tail
  with patch.object(routes_tail, 'query', side_effect=AssertionError('No scanner query')):
   body=self.c.get('/api/ext/class-followup/candidates',headers=self.h).get_json()
  self.assertTrue(body['disabled']);self.assertEqual([],body['items'])
 def test_legacy_post_cannot_write(self):
  from unittest.mock import patch
  import routes_tail
  with patch.object(routes_tail, 'execute', side_effect=AssertionError('No scanner write')):
   self.assertEqual(410,self.c.post('/api/ext/class-followup/1',json={'state':'candidate'},headers=self.h).status_code)
  with A.app.app_context():
   row=A.query('SELECT action_taken,evidence_state FROM class_status_items WHERE id=1',one=True)
   self.assertEqual('',row['action_taken']);self.assertIsNone(row['evidence_state'])
if __name__=='__main__':unittest.main()
