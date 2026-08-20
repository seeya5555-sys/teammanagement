import os
import tempfile
import unittest
import app as appmod

# 이 파일은 `client.session_transaction()` 으로 로그인해서 CSRF 토큰을 가진
# 적이 없다. TESTING 을 세우지 않는 파일이라 csrf.enforce 의 기본값(=켜짐)에
# 걸리므로, 여기서 명시적으로 끈다. 검사 자체는 tests/test_csrf.py 가 본다.
appmod.app.config['CSRF_PROTECT'] = False
from source_bundle import shared_ns

PDF = b'%PDF-1.4\ninvoice\n%%EOF'
XLSX = b'PK\x03\x04' + b'\x00' * 40          # OOXML(zip) 매직만 맞춘 최소 바이트
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 20


class FundreqAttachmentPreviewTests(unittest.TestCase):
    """비용청구 카드의 SVMS 첨부(인보이스·증빙) 미리보기 — 업로드/서빙/정리."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        self.old_dir = shared_ns.FUNDREQ_FILE_DIR
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        shared_ns.FUNDREQ_FILE_DIR = os.path.join(self.tmp.name, 'fundreq_files')
        os.makedirs(shared_ns.FUNDREQ_FILE_DIR)
        with appmod.app.app_context():
            appmod.init_db(False)
            shared_ns._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", ('secret',))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['role'] = 'admin'
        self.did = self.ingest()

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        shared_ns.FUNDREQ_FILE_DIR = self.old_dir
        self.tmp.cleanup()

    # ── helpers ──
    def ingest(self, names=('DN 26 07.pdf', 'AOR summary.xlsx')):
        r = self.client.post('/api/ext/fundreq/drafts',
                             json={'opex_cd': 'GYPSCO2607270001', 'vsl_cd': 'GYPS', 'amt': 100.0,
                                   'tp': 'O', 'verdict': 'pass', 'raw_row': {'OPEX_CD': 'GYPSCO2607270001'},
                                   'attach_files': list(names)},
                             headers={'X-API-Key': 'secret'})
        self.assertIn(r.status_code, (200, 201))
        return r.get_json()['id']

    def upload(self, idx=0, data=PDF, ext='pdf', did=None):
        url = '/api/ext/fundreq/drafts/%d/attachments/%d' % (did or self.did, idx)
        if ext:
            url += '?ext=' + ext
        return self.client.post(url, data=data, headers={'X-API-Key': 'secret'})

    def indices(self):
        drafts = self.client.get('/api/fundreq/drafts').get_json()['drafts']
        return drafts[0]['attachment_preview_indices']

    # ── tests ──
    def test_pdf_and_xlsx_preview_served_with_own_type(self):
        self.assertEqual(200, self.upload(0, PDF, 'pdf').status_code)
        self.assertEqual(200, self.upload(1, XLSX, 'xlsx').status_code)
        self.assertEqual([0, 1], self.indices())
        r = self.client.get('/api/fundreq/drafts/%d/attachments/0' % self.did)
        self.assertEqual(200, r.status_code)
        self.assertEqual('application/pdf', r.mimetype)
        self.assertEqual('nosniff', r.headers.get('X-Content-Type-Options'))
        self.assertIn('inline', r.headers.get('Content-Disposition', ''))
        r.close()
        r = self.client.get('/api/fundreq/drafts/%d/attachments/1' % self.did)
        self.assertEqual(200, r.status_code)
        self.assertIn('spreadsheetml', r.mimetype)
        self.assertIn('attachment', r.headers.get('Content-Disposition', ''))   # Office 는 inline 안 함
        r.close()

    def test_attach_files_names_survive_ingest(self):
        d = self.client.get('/api/fundreq/drafts').get_json()['drafts'][0]
        self.assertIn('DN 26 07.pdf', d['attach_files'])
        self.assertIn('AOR summary.xlsx', d['attach_files'])

    def test_content_must_match_ext(self):
        self.assertEqual(400, self.upload(0, b'<html>nope</html>', 'pdf').status_code)
        self.assertEqual(400, self.upload(0, PDF, 'xlsx').status_code)
        self.assertEqual(400, self.upload(0, b'', 'pdf').status_code)
        self.assertEqual([], self.indices())

    def test_unknown_ext_falls_back_to_pdf_validation(self):
        # 허용목록 밖 ext 는 pdf 로 취급 → PDF 매직이 아니면 거부
        self.assertEqual(400, self.upload(0, XLSX, 'exe').status_code)
        self.assertEqual(200, self.upload(0, PDF, 'exe').status_code)

    def test_reupload_replaces_other_ext_at_same_index(self):
        self.assertEqual(200, self.upload(0, XLSX, 'xlsx').status_code)
        self.assertEqual(200, self.upload(0, PNG, 'png').status_code)
        stored = sorted(os.listdir(shared_ns.FUNDREQ_FILE_DIR))
        self.assertEqual(['%d_0.png' % self.did], stored)
        self.assertEqual([0], self.indices())

    def test_index_bounds_and_missing_file(self):
        self.assertEqual(400, self.upload(50).status_code)
        self.assertEqual(404, self.client.get('/api/fundreq/drafts/%d/attachments/50' % self.did).status_code)
        self.assertEqual(404, self.client.get('/api/fundreq/drafts/%d/attachments/3' % self.did).status_code)
        self.assertEqual(404, self.client.get('/api/fundreq/drafts/999999/attachments/0').status_code)

    def test_upload_rejected_once_decided(self):
        with appmod.app.app_context():
            appmod.execute("UPDATE fundreq_draft SET status='approved' WHERE id=?", (self.did,))
        self.assertEqual(409, self.upload().status_code)

    def test_reingest_clears_stale_previews(self):
        self.assertEqual(200, self.upload(0, PDF, 'pdf').status_code)
        self.assertEqual([0], self.indices())
        again = self.ingest(names=('other.pdf',))
        self.assertEqual(self.did, again)          # pending 이면 같은 행 갱신
        self.assertEqual([], self.indices())       # 옛 preview 는 이름과 어긋나므로 제거

    def test_delete_and_clear_decided_remove_files(self):
        self.assertEqual(200, self.upload(0, PDF, 'pdf').status_code)
        self.client.delete('/api/fundreq/drafts/%d' % self.did)
        self.assertEqual([], os.listdir(shared_ns.FUNDREQ_FILE_DIR))
        did2 = self.ingest()
        self.assertEqual(200, self.upload(0, PDF, 'pdf', did=did2).status_code)
        with appmod.app.app_context():
            appmod.execute("UPDATE fundreq_draft SET status='submitted' WHERE id=?", (did2,))
        self.client.delete('/api/fundreq/drafts/decided')
        self.assertEqual([], os.listdir(shared_ns.FUNDREQ_FILE_DIR))

    def test_failed_upload_keeps_existing_preview(self):
        # 거부되는 업로드(내용·확장자 불일치)가 이미 안착한 preview 를 지우면 안 된다
        self.assertEqual(200, self.upload(0, PDF, 'pdf').status_code)
        self.assertEqual(400, self.upload(0, b'<html>nope</html>', 'png').status_code)
        self.assertEqual([0], self.indices())
        r = self.client.get('/api/fundreq/drafts/%d/attachments/0' % self.did)
        self.assertEqual(200, r.status_code)
        self.assertEqual('application/pdf', r.mimetype)
        r.close()

    def test_upload_index_beyond_names_rejected(self):
        # 이름 목록은 2개 — idx 2 는 이름 없이 미리보기만 생기는 상태라 받지 않는다
        self.assertEqual(404, self.upload(2, PDF, 'pdf').status_code)
        self.assertEqual([], self.indices())

    def test_malformed_attach_files_normalized(self):
        r = self.client.post('/api/ext/fundreq/drafts',
                             json={'opex_cd': 'GYPSCO2607270002', 'vsl_cd': 'GYPS', 'amt': 1.0,
                                   'tp': 'O', 'verdict': 'pass', 'attach_files': 'DN.pdf'},
                             headers={'X-API-Key': 'secret'})
        self.assertIn(r.status_code, (200, 201))
        did = r.get_json()['id']
        row = [d for d in self.client.get('/api/fundreq/drafts').get_json()['drafts'] if d['id'] == did][0]
        self.assertEqual('[]', row['attach_files'])          # 리스트 아닌 입력 = 빈 목록
        self.assertEqual(404, self.upload(0, PDF, 'pdf', did=did).status_code)

    def test_preview_requires_auth(self):
        self.assertEqual(200, self.upload(0, PDF, 'pdf').status_code)
        anon = appmod.app.test_client()
        r = anon.get('/api/fundreq/drafts/%d/attachments/0' % self.did)
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertEqual(401, appmod.app.test_client().post(
            '/api/ext/fundreq/drafts/%d/attachments/0' % self.did, data=PDF).status_code)

    def test_auto_submit_result_absorbs_stale_active_card_without_creating_one(self):
        r = self.client.post('/api/ext/fundreq/drafts',
                             json={'opex_cd': 'GYPSCO2607270001', 'auto_submitted': True,
                                   'result': '상신OK STATUS=U'},
                             headers={'X-API-Key': 'secret'})
        self.assertEqual(200, r.status_code)
        self.assertEqual(1, r.get_json()['applied'])
        row = self.client.get('/api/fundreq/drafts').get_json()['drafts'][0]
        self.assertEqual('submitted', row['status'])
        self.assertEqual('상신OK STATUS=U', row['result'])

        r = self.client.post('/api/ext/fundreq/drafts',
                             json={'opex_cd': 'NONECO2607270001', 'auto_submitted': True},
                             headers={'X-API-Key': 'secret'})
        self.assertEqual(200, r.status_code)
        self.assertEqual(0, r.get_json()['applied'])
        self.assertEqual(1, len(self.client.get('/api/fundreq/drafts').get_json()['drafts']))

    def test_auto_submit_result_never_overwrites_human_reject_decision(self):
        for status in ('rejecting', 'reject_submitting', 'rejected', 'reject_failed'):
            with appmod.app.app_context():
                appmod.execute("UPDATE fundreq_draft SET status=? WHERE id=?", (status, self.did))
            r = self.client.post('/api/ext/fundreq/drafts',
                                 json={'opex_cd': 'GYPSCO2607270001', 'auto_submitted': True},
                                 headers={'X-API-Key': 'secret'})
            self.assertEqual(409, r.status_code)
            self.assertEqual(status, r.get_json()['status'])

    def test_auto_submit_result_requires_api_key(self):
        r = appmod.app.test_client().post('/api/ext/fundreq/drafts',
                                          json={'opex_cd': 'GYPSCO2607270001',
                                                'auto_submitted': True})
        self.assertEqual(401, r.status_code)


if __name__ == '__main__':
    unittest.main()
