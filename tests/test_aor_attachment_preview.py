import os
import tempfile
import unittest
import app as appmod

class AorAttachmentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old_db=appmod.DATABASE; self.old_cfg=appmod.app.config['DATABASE']; self.old_dir=appmod.AOR_PDF_DIR
        db=os.path.join(self.tmp.name,'test.db'); appmod.DATABASE=db; appmod.app.config['DATABASE']=db; appmod.AOR_PDF_DIR=os.path.join(self.tmp.name,'aor_pdfs'); os.makedirs(appmod.AOR_PDF_DIR)
        with appmod.app.app_context():
            appmod.init_db(False); appmod._ensure_api_table(); appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)",('secret',));
            self.did=appmod.execute("INSERT INTO aor_draft(aor_cd,status,attach_files) VALUES(?,?,?)",('INPSCA2607230001','pending','[\"quote.pdf\"]'))
        self.client=appmod.app.test_client()
        with self.client.session_transaction() as s: s['user_id']=1; s['role']='admin'
    def tearDown(self):
        appmod.DATABASE=self.old_db; appmod.app.config['DATABASE']=self.old_cfg; appmod.AOR_PDF_DIR=self.old_dir; self.tmp.cleanup()
    def upload(self,data=b'%PDF-1.4\nquote\n%%EOF'):
        return self.client.post(f'/api/ext/aor/drafts/{self.did}/attachments/0',data=data,headers={'X-API-Key':'secret','Content-Type':'application/pdf'})
    def test_preview_and_submit_success_cleanup(self):
        self.assertEqual(200,self.upload().status_code)
        d=self.client.get('/api/aor/drafts').get_json()['drafts'][0]; self.assertEqual([0],d['attachment_preview_indices'])
        r=self.client.get(f'/api/aor/drafts/{self.did}/attachments/0'); self.assertEqual(200,r.status_code); r.close()
        with appmod.app.app_context(): appmod.execute("UPDATE aor_draft SET status='submitting' WHERE id=?",(self.did,))
        done=self.client.post(f'/api/ext/aor/drafts/{self.did}/result',json={'ok':True},headers={'X-API-Key':'secret'})
        self.assertTrue(done.get_json()['applied']); self.assertEqual(404,self.client.get(f'/api/aor/drafts/{self.did}/attachments/0').status_code)
    def test_failed_submit_preserves_preview(self):
        self.assertEqual(200,self.upload().status_code)
        with appmod.app.app_context(): appmod.execute("UPDATE aor_draft SET status='submitting' WHERE id=?",(self.did,))
        self.client.post(f'/api/ext/aor/drafts/{self.did}/result',json={'ok':False},headers={'X-API-Key':'secret'})
        r=self.client.get(f'/api/aor/drafts/{self.did}/attachments/0'); self.assertEqual(200,r.status_code); r.close()
    def test_rejects_non_pdf_and_wrong_state(self):
        self.assertEqual(400,self.upload(b'no').status_code)
        with appmod.app.app_context(): appmod.execute("UPDATE aor_draft SET status='submitted' WHERE id=?",(self.did,))
        self.assertEqual(409,self.upload().status_code)

if __name__=='__main__': unittest.main()
