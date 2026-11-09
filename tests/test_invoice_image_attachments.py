"""인보이스 Reference 이미지 첨부(JPG/PNG) 미리보기 — '미리보기 없음' 수정(2026-11-09)."""
import hashlib, json, os, tempfile, unittest, urllib.parse
import app as appmod
appmod.app.config['CSRF_PROTECT']=False
from source_bundle import shared_ns

PDF=b'%PDF-1.4\nx\n%%EOF'
JPG=b'\xff\xd8\xff\xe0' + b'\x00'*32
PNG=b'\x89PNG\r\n\x1a\n' + b'\x00'*32
NAMES=['INVOICE-1.PDF','9077463077.JPG','photo.png','note.txt']

class InvoiceImageAttachmentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old_db=appmod.DATABASE; self.old_cfg=appmod.app.config['DATABASE']; self.old_dir=shared_ns.INVOICE_PDF_DIR
        db=os.path.join(self.tmp.name,'t.db'); appmod.DATABASE=db; appmod.app.config['DATABASE']=db
        shared_ns.INVOICE_PDF_DIR=os.path.join(self.tmp.name,'invoice_pdfs'); os.makedirs(shared_ns.INVOICE_PDF_DIR)
        with appmod.app.app_context():
            appmod.init_db(False); shared_ns._ensure_api_table(); appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','secret')")
            self.did=appmod.execute("INSERT INTO invoice_draft(inv_cd,status,attachments,match_src,raw_card) VALUES(?,?,?,?,?)",('TSTCI2','pending',json.dumps(NAMES),'INVOICE-1.PDF','{}'))
        self.c=appmod.app.test_client()
        with self.c.session_transaction() as s:s['user_id']=1;s['role']='admin'
    def tearDown(self):
        appmod.DATABASE=self.old_db; appmod.app.config['DATABASE']=self.old_cfg; shared_ns.INVOICE_PDF_DIR=self.old_dir; self.tmp.cleanup()
    def upload(self,idx,data):
        fp=hashlib.sha256(json.dumps(NAMES,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        q=urllib.parse.urlencode({'name':NAMES[idx],'fp':fp})
        return self.c.post(f'/api/ext/invoice/drafts/{self.did}/attachments/{idx}?{q}',data=data,headers={'X-API-Key':'secret'})
    def test_jpg_png_upload_list_and_serve_with_image_mime(self):
        for i,d in ((0,PDF),(1,JPG),(2,PNG)): self.assertEqual(200,self.upload(i,d).status_code)
        self.assertEqual([0,1,2],self.c.get('/api/invoice/drafts').get_json()['drafts'][0]['attachment_preview_indices'])
        for i,m in ((0,'application/pdf'),(1,'image/jpeg'),(2,'image/png')):
            r=self.c.get(f'/api/invoice/drafts/{self.did}/attachments/{i}')
            self.assertEqual(200,r.status_code); self.assertEqual(m,r.mimetype); self.assertEqual('nosniff',r.headers.get('X-Content-Type-Options')); r.close()
    def test_content_must_match_declared_type(self):
        self.assertEqual(400,self.upload(1,PDF).status_code)   # .JPG 이름에 PDF 내용
        self.assertEqual(400,self.upload(1,b'<html>').status_code)
        self.assertEqual(400,self.upload(0,JPG).status_code)   # .PDF 이름에 JPG 내용
        self.assertEqual(400,self.upload(3,b'text').status_code)  # 미지원 형식

if __name__=='__main__':unittest.main()
