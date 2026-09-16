import hashlib, json, os, tempfile, unittest, urllib.parse
import app as appmod
appmod.app.config['CSRF_PROTECT']=False
from source_bundle import shared_ns

PDF=b'%PDF-1.4\nall attachments\n%%EOF'

class InvoiceAllAttachmentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old_db=appmod.DATABASE; self.old_cfg=appmod.app.config['DATABASE']; self.old_dir=shared_ns.INVOICE_PDF_DIR
        db=os.path.join(self.tmp.name,'t.db'); appmod.DATABASE=db; appmod.app.config['DATABASE']=db
        shared_ns.INVOICE_PDF_DIR=os.path.join(self.tmp.name,'invoice_pdfs'); os.makedirs(shared_ns.INVOICE_PDF_DIR)
        with appmod.app.app_context():
            appmod.init_db(False); shared_ns._ensure_api_table(); appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','secret')")
            self.did=appmod.execute("INSERT INTO invoice_draft(inv_cd,status,attachments,match_src,raw_card) VALUES(?,?,?,?,?)",('TSTCI1','pending',json.dumps(['invoice.pdf','delivery.pdf','note.txt']),'invoice.pdf','{}'))
        self.c=appmod.app.test_client()
        with self.c.session_transaction() as s:s['user_id']=1;s['role']='admin'
    def tearDown(self):
        appmod.DATABASE=self.old_db; appmod.app.config['DATABASE']=self.old_cfg; shared_ns.INVOICE_PDF_DIR=self.old_dir; self.tmp.cleanup()
    def upload(self,idx,data=PDF):
        names=['invoice.pdf','delivery.pdf','note.txt']
        fp=hashlib.sha256(json.dumps(names,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        q=urllib.parse.urlencode({'name':names[idx] if 0<=idx<len(names) else 'x','fp':fp})
        return self.c.post(f'/api/ext/invoice/drafts/{self.did}/attachments/{idx}?{q}',data=data,headers={'X-API-Key':'secret','Content-Type':'application/pdf'})
    def test_all_pdf_attachments_upload_list_and_serve(self):
        self.assertEqual(200,self.upload(0).status_code); self.assertEqual(200,self.upload(1).status_code)
        d=self.c.get('/api/invoice/drafts').get_json()['drafts'][0]
        self.assertEqual([0,1],d['attachment_preview_indices'])
        r=self.c.get(f'/api/invoice/drafts/{self.did}/attachments/0'); self.assertEqual(200,r.status_code); self.assertEqual('nosniff',r.headers.get('X-Content-Type-Options')); r.close()
        r=self.c.get(f'/api/invoice/drafts/{self.did}/attachments/1'); self.assertEqual(200,r.status_code); r.close()
    def test_non_pdf_and_out_of_range_are_rejected(self):
        self.assertEqual(400,self.upload(2).status_code); self.assertEqual(409,self.upload(9).status_code)
    def test_cleanup_removes_all_cached_attachments(self):
        self.upload(0); self.upload(1)
        with appmod.app.app_context(): shared_ns._invoice_pdf_delete(self.did)
        self.assertEqual(404,self.c.get(f'/api/invoice/drafts/{self.did}/attachments/0').status_code)
    def test_stale_identity_is_rejected(self):
        self.assertEqual(409,self.c.post(f'/api/ext/invoice/drafts/{self.did}/attachments/0?name=wrong.pdf&fp=bad',data=PDF,headers={'X-API-Key':'secret'}).status_code)
    def test_reingest_attachment_change_invalidates_index_cache(self):
        self.upload(0); self.upload(1)
        r=self.c.post('/api/ext/invoice/drafts',headers={'X-API-Key':'secret'},json={'inv_cd':'TSTCI1','attachments':['new.pdf'],'raw_card':{}})
        self.assertEqual(200,r.status_code)
        self.assertEqual([],self.c.get('/api/invoice/drafts').get_json()['drafts'][0]['attachment_preview_indices'])

if __name__=='__main__':unittest.main()
