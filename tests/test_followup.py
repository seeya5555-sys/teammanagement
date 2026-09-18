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

    def complete(self,kind='aor',quote='Will inspect on Monday'):
        base,jid,fp=self.create(kind)
        p=self.payload(fp);p['result']['items'][0]['quote']=quote
        self.assertEqual(200,self.c.post('/api/ext/followup/jobs/'+jid,headers=self.h,json=p).status_code)
        with A.app.app_context():A.execute("UPDATE automation_run SET status='done' WHERE run_id=?",(jid,))
        return base,jid,fp
    def test_new_repeat_and_subject_isolation(self):
        base,jid,fp=self.complete()
        self.assertEqual(1,self.c.get(base).get_json()['job']['changes']['first'])
        self.c.post(base+'/reviewed',headers=self.ch,json={'job_id':jid})
        _,jid,_=self.complete()
        j=self.c.get(base).get_json()['job']
        self.assertEqual(1,j['changes']['repeat']);self.assertIsNone(j['result']['items'][0]['received_after_baseline'])
        self.assertEqual('review',j['comparison_baseline_kind'])
        self.complete(quote='New quotation excludes travel')
        self.assertEqual(1,self.c.get(base).get_json()['job']['changes']['new'])
        response=self.c.post(base+'/scan',headers=self.ch,json={'fingerprint':fp,'search_subject':'TEST SHIP other scope'})
        jid=response.get_json()['job_id']
        self.c.post('/api/ext/followup/jobs/'+jid,headers=self.h,json=self.payload(fp))
        self.assertEqual(1,self.c.get(base).get_json()['job']['changes']['first'])
    def test_item_reviews_and_daily_append_fail_closed(self):
        base,jid,fp=self.complete('daily')
        iid=self.c.get(base).get_json()['job']['result']['items'][0]['item_id']
        data={'job_id':jid,'item_id':iid,'decision':'confirmed'}
        self.assertEqual(403,self.c.post(base+'/items/review',json=data).status_code)
        self.assertEqual(200,self.c.post(base+'/items/review',headers=self.ch,json=data).status_code)
        data['decision']='excluded'
        self.assertEqual(200,self.c.post(base+'/items/review',headers=self.ch,json=data).status_code)
        data['decision']='applied'
        self.assertEqual(200,self.c.post(base+'/items/review',headers=self.ch,json=data).status_code)
        self.assertEqual(409,self.c.post(base+'/items/review',headers=self.ch,json=data).status_code)
        with A.app.app_context():
            row=A.query('SELECT actions,status FROM issues WHERE id=?',(self.iid,),one=True)
            self.assertEqual(1,len(json.loads(row['actions'])));self.assertEqual('Open',row['status'])
        base,jid,fp=self.complete('cs');data['job_id']=jid
        self.assertEqual(400,self.c.post(base+'/items/review',headers=self.ch,json=data).status_code)
    def test_item_review_wrong_job_or_item(self):
        base,jid,fp=self.complete()
        data={'job_id':jid,'item_id':'wrong','decision':'confirmed'}
        self.assertEqual(404,self.c.post(base+'/items/review',headers=self.ch,json=data).status_code)
        self.assertEqual(409,self.c.post('/api/followup/vt/'+str(self.vtid)+'/items/review',headers=self.ch,json=data).status_code)
    def test_metadata_requires_timezone_and_provenance(self):
        base,jid,fp=self.create()
        p=self.payload(fp)
        for meta in ({'received_at':'2026-01-01'}, {'received_at':'2026-01-01T00:00:00Z','message_id':'x'}, {'received_at':'2999-01-01T00:00:00Z','message_id':'x','timestamp_source':'outlook-message-header'}):
            p['result']['items'][0]['source']=meta
            self.assertEqual(400,self.c.post('/api/ext/followup/jobs/'+jid,headers=self.h,json=p).status_code)
    def enable(self,kind='aor'):
        base=f'/api/followup/{kind}/{self.ids[kind]}'
        fp=self.c.get(base).get_json()['context']['fingerprint']
        data={'fingerprint':fp,'enabled':True,'search_subject':'TEST SHIP pump seal'}
        self.assertEqual(200,self.c.post(base+'/tracking',headers=self.ch,json=data).status_code)
        return base,data
    def test_tracking_optin_cadence_and_stale_pause(self):
        with A.app.app_context():
            F.enqueue_tracked();self.assertEqual(0,A.query('SELECT COUNT(*) n FROM followup_job',one=True)['n'])
        base,data=self.enable()
        with A.app.app_context():
            F.enqueue_tracked();F.enqueue_tracked()
            self.assertEqual(1,A.query('SELECT COUNT(*) n FROM followup_job',one=True)['n'])
            A.execute("UPDATE automation_run SET status='done'")
            A.execute("UPDATE followup_tracking SET next_check='2000-01-01'")
            A.execute("UPDATE aor_draft SET amt=123 WHERE id=?",(self.ids['aor'],))
            F.enqueue_tracked()
        self.assertEqual(0,self.c.get(base).get_json()['tracking']['enabled'])
    def test_tracking_revoked_owner_and_disable(self):
        base,data=self.enable();data['enabled']=False
        self.assertEqual(200,self.c.post(base+'/tracking',headers=self.ch,json=data).status_code)
        with A.app.app_context():
            F.enqueue_tracked();self.assertEqual(0,A.query('SELECT COUNT(*) n FROM followup_job',one=True)['n'])
        self.enable()
        with A.app.app_context():
            A.execute('UPDATE users SET active=0 WHERE id=?',(self.uid,))
            F.enqueue_tracked();self.assertEqual(0,A.query('SELECT enabled FROM followup_tracking',one=True)['enabled'])
    def test_tracking_killswitch_and_queue_cap(self):
        self.enable()
        with A.app.app_context(),patch.object(F,'_automation_enabled',return_value=False):
            F.enqueue_tracked();self.assertEqual(0,A.query('SELECT COUNT(*) n FROM followup_job',one=True)['n'])
        with A.app.app_context():
            for i in range(10):A.execute("INSERT INTO automation_run(run_id,task,mode,status) VALUES(?,'followup_scan','verify','queued')",(str(i),))
            F.enqueue_tracked();self.assertEqual(0,A.query('SELECT COUNT(*) n FROM followup_job',one=True)['n'])

    def test_verified_timestamp_compared_not_quote_date(self):
        base,jid,fp=self.complete()
        with A.app.app_context():
            A.execute("UPDATE followup_job SET reviewed_at='2026-01-01 09:00:00' WHERE job_id=?",(jid,))
        _,jid,fp=self.create('aor')
        p=self.payload(fp)
        p['result']['items'][0]['source']={'received_at':'2026-01-02T00:00:00Z','message_id':'test-message','timestamp_source':'outlook-message-header'}
        self.assertEqual(200,self.c.post('/api/ext/followup/jobs/'+jid,headers=self.h,json=p).status_code)
        j=self.c.get(base).get_json()['job']
        self.assertTrue(j['result']['items'][0]['received_after_baseline'])
        self.assertEqual(1,j['changes']['received_after'])
    def test_indicators_and_claim_hook(self):
        base,jid,fp=self.complete()
        d=self.c.get('/api/followup/indicators').get_json()
        self.assertEqual(1,d['items'][0]['count'])
        iid=self.c.get(base).get_json()['job']['result']['items'][0]['item_id']
        self.c.post(base+'/items/review',headers=self.ch,json={'job_id':jid,'item_id':iid,'decision':'excluded'})
        self.assertEqual([],self.c.get('/api/followup/indicators').get_json()['items'])
        self.enable('daily')
        with patch('routes_dock_submit._automation_enabled',return_value=True):
            response=self.c.post('/api/ext/automation/claim',headers=self.h,json={})
        self.assertEqual(200,response.status_code)
        self.assertEqual('followup_scan',response.get_json()['run']['task'])

    def test_unreviewed_repeat_stays_visible_and_decisions_persist(self):
        base,jid,fp=self.complete()
        self.complete()
        self.assertEqual(1,self.c.get('/api/followup/indicators').get_json()['items'][0]['count'])
        j=self.c.get(base).get_json()['job'];iid=j['result']['items'][0]['item_id']
        self.c.post(base+'/items/review',headers=self.ch,json={'job_id':j['job_id'],'item_id':iid,'decision':'excluded'})
        self.complete()
        self.assertEqual('excluded',self.c.get(base).get_json()['job']['result']['items'][0]['review']['decision'])
        self.assertEqual([],self.c.get('/api/followup/indicators').get_json()['items'])
    def test_applied_citation_cannot_duplicate_after_rescan(self):
        base,jid,fp=self.complete('daily')
        iid=self.c.get(base).get_json()['job']['result']['items'][0]['item_id']
        self.c.post(base+'/items/review',headers=self.ch,json={'job_id':jid,'item_id':iid,'decision':'applied'})
        _,new,_=self.complete('daily')
        response=self.c.post(base+'/items/review',headers=self.ch,json={'job_id':new,'item_id':iid,'decision':'applied'})
        self.assertTrue(response.get_json()['already_applied'])
        with A.app.app_context():self.assertEqual(1,len(json.loads(A.query('SELECT actions FROM issues WHERE id=?',(self.iid,),one=True)['actions'])))

    def test_applied_dedup_survives_more_than_100_scans(self):
        base,jid,fp=self.complete('daily')
        iid=self.c.get(base).get_json()['job']['result']['items'][0]['item_id']
        self.c.post(base+'/items/review',headers=self.ch,json={'job_id':jid,'item_id':iid,'decision':'applied'})
        with A.app.app_context():
            for i in range(101):
                A.execute("INSERT INTO followup_job(job_id,kind,target_id,fingerprint,context_json,requested_by,state,result) SELECT ?,kind,target_id,fingerprint,context_json,requested_by,state,result FROM followup_job WHERE job_id=?",('history-'+str(i),jid))
        _,new,_=self.complete('daily')
        r=self.c.post(base+'/items/review',headers=self.ch,json={'job_id':new,'item_id':iid,'decision':'applied'})
        self.assertTrue(r.get_json()['already_applied'])
        with A.app.app_context():self.assertEqual(1,len(json.loads(A.query('SELECT actions FROM issues WHERE id=?',(self.iid,),one=True)['actions'])))
    def test_tracking_and_reviewed_csrf_required(self):
        base,jid,fp=self.complete()
        self.assertEqual(403,self.c.post(base+'/tracking',json={'enabled':True,'fingerprint':fp,'search_subject':'TEST SHIP pump seal'}).status_code)
        self.assertEqual(403,self.c.post(base+'/reviewed',json={'job_id':jid}).status_code)
    def test_runner_item_id_is_accepted_and_recomputed(self):
        base,jid,fp=self.create()
        p=self.payload(fp);p['result']['items'][0]['item_id']='untrusted-id'
        self.assertEqual(200,self.c.post('/api/ext/followup/jobs/'+jid,headers=self.h,json=p).status_code)
        self.assertEqual(F.evidence_id(p['result']['items'][0]['quote']),self.c.get(base).get_json()['job']['result']['items'][0]['item_id'])

if __name__=='__main__':unittest.main()
