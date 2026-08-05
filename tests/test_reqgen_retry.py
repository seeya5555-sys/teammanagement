"""reqgen 실패카드 [재시도] 회귀 — 2026-08-05 S33.

`/reset` 이 approved 만 처리해서 failed 카드의 [재시도] 버튼이 항상 409 로 죽어 있었다
(템플릿은 같은 버튼을 approved=취소 / failed=재시도 로 쓴다).
재시도는 **REQ_NO 가 아직 없을 때만** — 번호가 붙었으면 SVMS 에 절반 저장됐을 수 있어
자동 재저장이 이중저장이 된다(fail-closed, 사람이 SVMS 확인).
"""
import os
import tempfile
import unittest

import app as appmod


class ReqGenRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        with appmod.app.app_context():
            appmod.init_db(False)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'retry-test'
            session['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def mk(self, status, req_no=None, result='저장 실패: ORA-06502'):
        with appmod.app.app_context():
            return appmod.execute(
                "INSERT INTO reqgen_draft(batch,doc_type,sheet,vsl_cd,vsl_nm,subj,header_json,"
                "lines_json,status,req_no,result) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ('b1', 'PC', 'S33', 'BGBB', 'M/T BELGIUM B', '[DOCK][BGBB S33]',
                 '{"VSL_CD":"BGBB"}', '[]', status, req_no, result))

    def row(self, did):
        with appmod.app.app_context():
            return appmod.query('SELECT * FROM reqgen_draft WHERE id=?', (did,), one=True)

    def test_failed_card_requeues_for_save(self):
        did = self.mk('failed')
        r = self.client.post(f'/api/reqgen/drafts/{did}/reset')
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertEqual('approved', r.get_json()['status'])
        self.assertTrue(r.get_json()['save_run'], '저장큐(run) 적재돼야 러너가 다시 집어간다')
        got = self.row(did)
        self.assertEqual('approved', got['status'])
        self.assertIsNone(got['result'], '지난 실패 메시지는 지운다(카드에 옛 에러 잔상 금지)')
        with appmod.app.app_context():
            run = appmod.query("SELECT * FROM automation_run WHERE task='reqgen_save'", one=True)
        self.assertEqual('queued', run['status'])

    def test_failed_with_req_no_is_refused(self):
        """절반 저장 의심 건은 자동 재시도 금지 — 사람이 SVMS 보고 판단."""
        did = self.mk('failed', req_no='BGBBES2608A9')
        r = self.client.post(f'/api/reqgen/drafts/{did}/reset')
        self.assertEqual(409, r.status_code, r.get_json())
        self.assertIn('BGBBES2608A9', r.get_json()['error'])
        self.assertEqual('failed', self.row(did)['status'])
        with appmod.app.app_context():
            self.assertIsNone(appmod.query("SELECT * FROM automation_run WHERE task='reqgen_save'",
                                           one=True), '거부 시 큐도 안 남는다')

    def test_approved_cancel_still_works(self):
        did = self.mk('approved', result=None)
        r = self.client.post(f'/api/reqgen/drafts/{did}/reset')
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertEqual('pending', r.get_json()['status'])
        self.assertEqual('pending', self.row(did)['status'])

    def test_saving_card_is_untouched(self):
        """러너가 물고 있는 건(saving)을 되돌리면 이중저장 — 거부해야 한다."""
        did = self.mk('saving', result=None)
        r = self.client.post(f'/api/reqgen/drafts/{did}/reset')
        self.assertEqual(409, r.status_code, r.get_json())
        self.assertEqual('saving', self.row(did)['status'])

    def test_running_run_is_not_reused(self):
        """러너는 프로세스 시작 시 approved 를 1회만 claim → running run 재사용하면 새 카드가 안 실린다."""
        with appmod.app.app_context():
            appmod.execute("INSERT INTO automation_run(run_id,task,mode,status,requested_by) "
                           "VALUES('oldrun','reqgen_save','live','running','x')")
        did = self.mk('failed')
        r = self.client.post(f'/api/reqgen/drafts/{did}/reset')
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertNotEqual('oldrun', r.get_json()['save_run'], 'running run 재사용 = 재시도 무동작')
        with appmod.app.app_context():
            self.assertEqual(1, appmod.query("SELECT COUNT(*) c FROM automation_run "
                                             "WHERE task='reqgen_save' AND status='queued'",
                                             one=True)['c'])

    def test_queued_run_is_reused(self):
        """아직 claim 전(queued)이면 그 run 에 실리므로 새로 만들지 않는다(중복 run 방지)."""
        with appmod.app.app_context():
            appmod.execute("INSERT INTO automation_run(run_id,task,mode,status,requested_by) "
                           "VALUES('pending1','reqgen_save','live','queued','x')")
        did = self.mk('failed')
        r = self.client.post(f'/api/reqgen/drafts/{did}/reset')
        self.assertEqual('pending1', r.get_json()['save_run'])

    def test_whitespace_req_no_is_treated_as_numbered(self):
        """공백 REQ_NO 도 채번된 것으로 본다 — 파이썬/SQL 두 가드가 같은 뜻이어야 한다(fail-closed)."""
        did = self.mk('failed', req_no='  ')
        r = self.client.post(f'/api/reqgen/drafts/{did}/reset')
        self.assertEqual(409, r.status_code, r.get_json())
        self.assertIn('자동 재시도 불가', r.get_json()['error'])
        self.assertEqual('failed', self.row(did)['status'])

    def test_killswitch_blocks_retry(self):
        did = self.mk('failed')
        with appmod.app.app_context():
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('automation_enabled','0')")
        r = self.client.post(f'/api/reqgen/drafts/{did}/reset')
        self.assertEqual(409, r.status_code, r.get_json())
        self.assertIn('killswitch', r.get_json()['error'])
        self.assertEqual('failed', self.row(did)['status'], 'killswitch 면 상태도 안 바뀐다')


if __name__ == '__main__':
    unittest.main()
