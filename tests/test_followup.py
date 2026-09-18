import io
import contextlib
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
if not hasattr(hashlib, 'scrypt'):
    import werkzeug.security
    werkzeug.security.generate_password_hash=lambda *a,**k:'test-hash'
import app as A
import routes_followup as F
import helpers_shared as shared_ns

class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.od=A.DATABASE;self.oc=A.app.config['DATABASE']
        A.DATABASE=os.path.join(self.tmp.name,'test.db');A.app.config['DATABASE']=A.DATABASE
        with A.app.app_context():
            with contextlib.redirect_stdout(io.StringIO()): A.init_db(False)
            shared_ns._ensure_api_table()
            A.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','test-secret')")
            self.uid=A.execute("INSERT INTO users(username,password_hash,role,app_scope) VALUES('followup-admin','x','admin','business')")
            sid=A.execute("INSERT INTO supervisors(name) VALUES('Test Supervisor')")
            vid=A.execute("INSERT INTO vessels(name,active) VALUES('TEST SHIP',1)")
            self.iid=A.execute("INSERT INTO issues(supervisor_id,vessel_id,issue_date,item_topic,description) VALUES(?,?,'2026-09-19','Pump seal','Seal inspection')",(sid,vid))
            csid=A.execute("INSERT INTO cs_surveys(vessel_id,year,quarter,inspection_date) VALUES(?,2026,3,'2026-09-01')",(vid,))
            self.csid=A.execute("INSERT INTO cs_findings(survey_id,category,no,item) VALUES(?,'Defect',1,'Pump seal')",(csid,))
            vtid=A.execute("INSERT INTO vettings(vessel_id,inspection_date,report_number) VALUES(?,'2026-09-01','TEST-RPT')",(vid,))
            self.vtid=A.execute("INSERT INTO vt_findings(vetting_id,no,item) VALUES(?,1,'Pump seal')",(vtid,))
            self.ids={'daily':self.iid,'cs':self.csid,'vt':self.vtid}
            for kind,key in [('aor','aor_cd'),('fundreq','opex_cd'),('invoice','inv_cd')]:
                self.ids[kind]=A.execute(f"INSERT INTO {kind}_draft({key},vsl_nm,amt,cur_cd) VALUES('TEST-123','TEST SHIP',100,'USD')")
        self.c=A.app.test_client();self.h={'X-API-Key':'test-secret'}
        with self.c.session_transaction() as s:
            s['user_id']=self.uid;s['role']='admin';s['_csrf_token']='csrf-test'
        self.ch={'X-CSRF-Token':'csrf-test'}
        self.enabled=patch.object(F,'_automation_enabled',return_value=True);self.enabled.start()
    def tearDown(self):
        self.enabled.stop();A.DATABASE=self.od;A.app.config['DATABASE']=self.oc;self.tmp.cleanup()
    def create(self,kind='daily'):
        base=f'/api/followup/{kind}/{self.ids[kind]}'
        d=self.c.get(base).get_json()
        r=self.c.post(base+'/scan',headers=self.ch,json={'fingerprint':d['context']['fingerprint'],'search_subject':'TEST SHIP pump seal'})
        self.assertEqual(202,r.status_code,r.get_json())
        jid=r.get_json()['job_id']
        return base,jid,d['context']['fingerprint']
    def payload(self,fp):
        return {'state':'candidate','fingerprint':fp,'result':{'items':[{'label':'후속 약속','quote':'Will inspect on Monday','interpretation':'확인 필요'}],'match_keys':['vessel','subject','reading-pane'],'source_subject':'TEST SHIP pump seal'}}
    def test_all_six_contexts_and_result_business_unchanged(self):
        for kind in self.ids:
            base,jid,fp=self.create(kind)
            job=self.c.get('/api/ext/followup/jobs/'+jid,headers=self.h).get_json()['job']
            self.assertEqual(fp,job['fingerprint']);self.assertEqual('TEST SHIP',job['context']['vessel_name'])
            r=self.c.post('/api/ext/followup/jobs/'+jid,headers=self.h,json=self.payload(fp));self.assertEqual(200,r.status_code,r.get_json())
            self.assertEqual('candidate',self.c.get(base).get_json()['job']['state'])
            self.assertEqual(200,self.c.post(base+'/reviewed',headers=self.ch,json={'job_id':jid}).status_code)
        with A.app.app_context():
            self.assertEqual('Open',A.query('SELECT status FROM issues WHERE id=?',(self.iid,),one=True)['status'])
            self.assertEqual('Open',A.query('SELECT status FROM vt_findings WHERE id=?',(self.vtid,),one=True)['status'])
            self.assertEqual('pending',A.query('SELECT status FROM aor_draft WHERE id=?',(self.ids['aor'],),one=True)['status'])
    def test_stale_rejected_and_hidden(self):
        base,jid,fp=self.create()
        with A.app.app_context():A.execute("UPDATE issues SET description='changed' WHERE id=?",(self.iid,))
        self.assertEqual(409,self.c.post('/api/ext/followup/jobs/'+jid,headers=self.h,json=self.payload(fp)).status_code)
        self.assertEqual('stale',self.c.get(base).get_json()['job']['state'])
        self.assertEqual(409,self.c.get('/api/ext/followup/jobs/'+jid,headers=self.h).status_code)
    def test_web_pages_load_shared_evidence_ui(self):
        from flask import url_for
        for endpoint in ('routes_core.index','routes_core.condition_survey','routes_core.vetting_status','routes_calendar_dock.aor_page','routes_calendar_dock.fundreq_page','routes_calendar_dock.invoice_page'):
            with A.app.test_request_context():path=url_for(endpoint)
            response=self.c.get(path)
            self.assertEqual(200,response.status_code,(endpoint,response.status_code))
            self.assertIn(b'js/followup.js',response.data)
    def test_auth_and_csrf(self):
        base,jid,fp=self.create()
        self.assertEqual(401,self.c.get('/api/ext/followup/jobs/'+jid).status_code)
        self.assertEqual(403,self.c.post(base+'/scan',json={}).status_code)
        with A.app.app_context():A.execute("UPDATE users SET role='member' WHERE id=?",(self.uid,))
        self.assertEqual(403,self.c.get(base).status_code)
    def test_family_admin_denied(self):
        with A.app.app_context():A.execute("UPDATE users SET app_scope='family' WHERE id=?",(self.uid,))
        self.assertIn(self.c.get('/api/followup/daily/'+str(self.iid)).status_code,(403,404))
    def test_dedup_and_failure_visible(self):
        base,jid,fp=self.create();_,again,_=self.create();self.assertEqual(jid,again)
        with A.app.app_context():A.execute("UPDATE automation_run SET status='failed' WHERE run_id=?",(jid,))
        self.assertEqual('error',self.c.get(base).get_json()['job']['state'])
        _,new,_=self.create();self.assertNotEqual(jid,new)
    def test_validation_and_idempotency(self):
        base,jid,fp=self.create();url='/api/ext/followup/jobs/'+jid
        p=self.payload(fp);p['result']['items'][0]['quote']=''
        self.assertEqual(400,self.c.post(url,headers=self.h,json=p).status_code)
        p=self.payload(fp);p['result']['match_keys']=[]
        self.assertEqual(400,self.c.post(url,headers=self.h,json=p).status_code)
        self.assertEqual(200,self.c.post(url,headers=self.h,json=self.payload(fp)).status_code)
        p=self.payload(fp);p['state']='error'
        self.assertTrue(self.c.post(url,headers=self.h,json=p).get_json()['already_saved'])
        self.assertEqual('candidate',self.c.get(base).get_json()['job']['state'])
    def test_content_bound_before_and_after_ingest(self):
        base,jid,fp=self.create('invoice')
        url='/api/ext/followup/jobs/'+jid
        self.assertEqual(200,self.c.post(url,headers=self.h,json=self.payload(fp)).status_code)
        with A.app.app_context():A.execute("UPDATE invoice_draft SET amt=200 WHERE id=?",(self.ids['invoice'],))
        d=self.c.get(base).get_json()
        self.assertEqual('stale',d['job']['state']);self.assertEqual({},d['job']['result'])
        self.assertEqual(409,self.c.post(base+'/reviewed',headers=self.ch,json={'job_id':jid}).status_code)
    def test_large_payload_and_cross_target_review(self):
        base,jid,fp=self.create()
        p=self.payload(fp);p['result']['items'][0]['quote']='x'*1001
        self.assertEqual(400,self.c.post('/api/ext/followup/jobs/'+jid,headers=self.h,json=p).status_code)
        self.assertEqual(404,self.c.get('/api/followup/other/1').status_code)
        self.assertEqual(409,self.c.post('/api/followup/cs/'+str(self.csid)+'/reviewed',headers=self.ch,json={'job_id':jid}).status_code)
    def test_queue_cancel_orphan_and_delayed(self):
        base,jid,fp=self.create()
        with A.app.app_context():A.execute("UPDATE followup_job SET created_at=datetime('now','localtime','-20 minutes') WHERE job_id=?",(jid,))
        self.assertTrue(self.c.get(base).get_json()['job']['delayed'])
        self.assertEqual(200,self.c.post(base+'/cancel',headers=self.ch,json={'job_id':jid}).status_code)
        self.assertEqual('error',self.c.get(base).get_json()['job']['state'])
        _,jid,_=self.create()
        with A.app.app_context():A.execute("UPDATE automation_run SET status='running' WHERE run_id=?",(jid,))
        self.assertEqual(409,self.c.post(base+'/cancel',headers=self.ch,json={'job_id':jid}).status_code)
        with A.app.app_context():A.execute("DELETE FROM automation_run WHERE run_id=?",(jid,))
        self.assertEqual('error',self.c.get(base).get_json()['job']['state'])
    def test_cap_and_input_validation(self):
        base='/api/followup/daily/'+str(self.iid)
        fp=self.c.get(base).get_json()['context']['fingerprint']
        for subject in ('short','x'*301,'test\nsubject'):
            self.assertEqual(400,self.c.post(base+'/scan',headers=self.ch,json={'fingerprint':fp,'search_subject':subject}).status_code)
        with A.app.app_context():
            for i in range(10):A.execute("INSERT INTO automation_run(run_id,task,mode,status) VALUES(?,'followup_scan','verify','queued')",(str(i),))
        self.assertEqual(429,self.c.post(base+'/scan',headers=self.ch,json={'fingerprint':fp,'search_subject':'TEST SHIP pump seal'}).status_code)
    def test_killswitch_and_source_revision(self):
        base='/api/followup/daily/'+str(self.iid)
        self.assertEqual(409,self.c.post(base+'/scan',headers=self.ch,json={'fingerprint':'wrong','search_subject':'TEST SHIP Pump'}).status_code)
        with patch.object(F,'_automation_enabled',return_value=False):
            self.assertEqual(409,self.c.post(base+'/scan',headers=self.ch,json={}).status_code)

if __name__=='__main__':unittest.main()
