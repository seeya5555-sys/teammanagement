import os, tempfile, unittest

os.environ.setdefault('SECRET_KEY','test')
os.environ.setdefault('TRMT_API_KEY','testkey')
os.environ.setdefault('DATABASE', tempfile.mktemp(suffix='.db'))
import app as appmod
appmod.app.config['CSRF_PROTECT'] = False
from source_bundle import shared_ns

class ApprovalRejectionSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old_db=appmod.DATABASE; self.old_cfg=appmod.app.config['DATABASE']
        db=os.path.join(self.tmp.name,'test.db'); appmod.DATABASE=db; appmod.app.config['DATABASE']=db
        with appmod.app.app_context():
            appmod.init_db(False); shared_ns._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)",('secret',))
            self.did=appmod.execute("INSERT INTO invoice_draft(inv_cd,status,raw_card) VALUES(?,?,?)",('TESTCI2609160001','pending','{}'))
        self.client=appmod.app.test_client()
        with self.client.session_transaction() as s: s['user_id']=1; s['role']='admin'
    def tearDown(self):
        appmod.DATABASE=self.old_db; appmod.app.config['DATABASE']=self.old_cfg; self.tmp.cleanup()
    def test_sync_annotates_without_changing_decision_status(self):
        r=self.client.post('/api/ext/approval-rejections/sync',headers={'X-API-Key':'secret'},json={'items':[{'kind':'invoice','ref_no':'TESTCI2609160001','upstream_status':'S','remark':'Wrong invoice date','corrective_action':'TRMT INV_DT 수정'}]})
        self.assertEqual(200,r.status_code)
        with appmod.app.app_context(): row=appmod.query('SELECT * FROM invoice_draft WHERE id=?',(self.did,),one=True)
        self.assertEqual('pending',row['status'])
        self.assertEqual('Wrong invoice date',row['upstream_reject_remark'])
    def test_unknown_kind_is_invalid(self):
        r=self.client.post('/api/ext/approval-rejections/sync',headers={'X-API-Key':'secret'},json={'items':[{'kind':'x','ref_no':'1','upstream_status':'R','remark':'x'}]})
        self.assertEqual(1,r.get_json()['invalid'])
    def test_repeat_is_idempotent_and_exact_ref_only(self):
        item={'kind':'invoice','ref_no':'TESTCI2609160001','upstream_status':'S','remark':'<script>x</script> Wrong invoice date','corrective_action':'not applied'}
        for _ in range(2): self.client.post('/api/ext/approval-rejections/sync',headers={'X-API-Key':'secret'},json={'items':[item]})
        with appmod.app.app_context(): row=appmod.query('SELECT * FROM invoice_draft WHERE id=?',(self.did,),one=True)
        self.assertIn('<script>',row['upstream_reject_remark'])  # stored verbatim; UI esc() is mandatory
        before=row['upstream_rejected_at']
        r=self.client.post('/api/ext/approval-rejections/sync',headers={'X-API-Key':'secret'},json={'items':[dict(item,ref_no='OTHER')]})
        self.assertEqual(1,r.get_json()['missing'])
        with appmod.app.app_context(): self.assertEqual(before,appmod.query('SELECT upstream_rejected_at FROM invoice_draft WHERE id=?',(self.did,),one=True)['upstream_rejected_at'])
    def test_payload_cap(self):
        r=self.client.post('/api/ext/approval-rejections/sync',headers={'X-API-Key':'secret'},json={'items':[{}]*501})
        self.assertEqual(400,r.status_code)

if __name__=='__main__': unittest.main()
