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
 def test_exact_fingerprint_and_bounded_result(self):
  item=self.c.get('/api/ext/class-followup/candidates',headers=self.h).get_json()['items'][0]
  self.assertEqual(409,self.c.post('/api/ext/class-followup/1',json={'fingerprint':'bad','state':'candidate'},headers=self.h).status_code)
  body={'fingerprint':item['fingerprint'],'state':'candidate','subject':'x'*900,'attachments':['a.pdf']*30}
  got=self.c.post('/api/ext/class-followup/1',json=body,headers=self.h).get_json();self.assertEqual(20,got['attachments'])
 def test_action_taken_excludes_candidate(self):
  with A.app.app_context():A.execute("UPDATE class_status_items SET action_taken='done' WHERE id=1")
  self.assertEqual(0,self.c.get('/api/ext/class-followup/candidates',headers=self.h).get_json()['count'])
if __name__=='__main__':unittest.main()
