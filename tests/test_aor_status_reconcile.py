import os
import tempfile
import unittest

import hashlib
if not hasattr(hashlib, 'scrypt'):
    import werkzeug.security
    werkzeug.security.generate_password_hash = lambda value, *a, **k: 'test-hash'

import app as appmod
import routes_calendar_dock as r
from source_bundle import shared_ns


class AorStatusReconcileRuleTest(unittest.TestCase):
    def test_pending_moved_upstream_alerts(self):
        self.assertIn('STATUS=C', r._aor_reconcile_alert('pending', 'C'))

    def test_hold_rejected_upstream_alerts(self):
        self.assertIn('STATUS=R', r._aor_reconcile_alert('hold', 'R'))

    def test_pending_still_submitted_is_clean(self):
        self.assertEqual('', r._aor_reconcile_alert('pending', 'S'))

    def test_completed_local_and_upstream_is_clean(self):
        self.assertEqual('', r._aor_reconcile_alert('submitted', 'C'))

    def test_never_infers_issue_close(self):
        for upstream in ('C','D','R','S','P','U','Z','E'):
            self.assertEqual('', r._aor_reconcile_alert('failed', upstream))


class AorStatusReconcileEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old_db=appmod.DATABASE; self.old_cfg=appmod.app.config['DATABASE']
        db=os.path.join(self.tmp.name,'test.db'); appmod.DATABASE=db; appmod.app.config['DATABASE']=db
        with appmod.app.app_context():
            appmod.init_db(False); shared_ns._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)",('secret',))
            appmod.execute("INSERT INTO aor_draft(aor_cd,status) VALUES(?,?)",('TESTCA1','pending'))
        self.client=appmod.app.test_client()
        with self.client.session_transaction() as s: s['user_id']=1; s['role']='admin'

    def tearDown(self):
        appmod.DATABASE=self.old_db; appmod.app.config['DATABASE']=self.old_cfg; self.tmp.cleanup()

    def post(self,items,auth=True):
        h={'X-API-Key':'secret'} if auth else {}
        return self.client.post('/api/ext/aor/status-sync',json={'items':items},headers=h)

    def test_auth_bound_and_per_item_invalid(self):
        self.assertIn(self.post([],False).status_code,(401,403))
        self.assertEqual(400,self.post([{}]*31).status_code)
        body=self.post([{'aor_cd':'TESTCA1','status':'C'},{'aor_cd':'X','status':'NEW'}]).get_json()
        self.assertEqual((1,1),(body['checked'],body['invalid']))

    def test_alert_is_derived_and_clears_on_recovery(self):
        self.assertEqual(1,self.post([{'aor_cd':' testca1 ','status':'C'}]).get_json()['alerts'])
        first=self.client.get('/api/aor/drafts').get_json()['drafts'][0]
        self.assertIn('STATUS=C',first['reconcile_alert'])
        self.assertEqual(0,self.post([{'aor_cd':'TESTCA1','status':'S'}]).get_json()['alerts'])
        second=self.client.get('/api/aor/drafts').get_json()['drafts'][0]
        self.assertEqual('',second['reconcile_alert'])

    def test_outlook_evidence_single_ingest_is_bounded_and_pii_minimized(self):
        body={'aor_cd':'TESTCA1','match_conf':99,
              'outlook_evidence':[{'date':'x'*900,'sender':'private@example.com','subject':'S','fact':'F'}]*12,
              'outlook_match_keys':['vessel','subject','evil']}
        self.assertEqual(200,self.client.post('/api/ext/aor/drafts',json=body,headers={'X-API-Key':'secret'}).status_code)
        with appmod.app.app_context(): row=appmod.query('SELECT outlook_evidence,outlook_match_keys FROM aor_draft WHERE id=1',one=True)
        saved=__import__('json').loads(row['outlook_evidence']); keys=__import__('json').loads(row['outlook_match_keys'])
        self.assertEqual(10,len(saved)); self.assertEqual(500,len(saved[0]['date']))
        self.assertNotIn('sender',saved[0]); self.assertEqual(['subject','vessel'],keys)

    def test_outlook_evidence_requires_high_confidence_and_two_keys(self):
        ev=[{'subject':'S','fact':'F'},{'subject':'S2','fact':'F2'}]
        got=r._sanitize_outlook_evidence(ev,['vessel'],99)
        self.assertEqual(([],[]),got)
        got=r._sanitize_outlook_evidence(ev,['vessel','subject'],79)
        self.assertEqual(([],[]),got)


if __name__ == '__main__':
    unittest.main()
