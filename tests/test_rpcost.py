"""/rpcost(수리·구매비용 컨펌) 큐 계약 — ingest dedup, 수리 편집 CAS/audit, 일괄승인, claim/result, reconcile."""
import json
import os
import tempfile
import unittest

import app as appmod
import routes_rpcost

appmod.app.config['CSRF_PROTECT'] = False
from source_bundle import shared_ns  # noqa: E402

KEY = {'X-API-Key': 'secret'}


def repair_payload(**over):
    raw = {'kind': 'repair', 'inv_cd': 'ATNHMI26100901', 'rep_cd': 'R1', 'cpl_cd': 'C1', 'inv_no': 'YY1',
           'inv_dt': '20261009', 'grd_amt': 12500, 'tot_amt': 12500, 'cur': 'USD',
           'slots': [{'slot': '01', 'inv_amt': 0}, {'slot': '02', 'inv_amt': 12500}, {'slot': '03', 'inv_amt': 0}],
           'exp_sel': {'02': {'exp_cd': '090305', 'exp_nm': '기타', 'source': 'suggest', 'conf': 0.3}}}
    raw.update(over.pop('raw', {}))
    p = {'kind': 'repair', 'svms_key': 'ATNHMI26100901', 'ref_key': 'R1', 'vsl_cd': 'ATNH', 'vsl_nm': 'ATLANTIC NORTH',
         'vndr_nm': 'YI YANG', 'inv_no': 'YY1', 'inv_dt': '20261009', 'cur_cd': 'USD', 'amt': 12500,
         'pay_dt': '20261130', 'inv_no_match': 1, 'amt_match': 1, 'date_match': 0, 'gate': 'HOLD',
         'hold_reasons': ['EXP_CD 누락 slot 02'], 'flags': [], 'attachments': ['MANI: a.pdf'],
         'svms_status': 'IU', 'raw_card': raw}
    p.update(over)
    return p


def purchase_payload(**over):
    p = {'kind': 'purchase', 'svms_key': 'BGBBES2607B21AA1', 'ref_key': 'BGBBES2607B21A', 'vsl_cd': 'BGBB',
         'inv_no': 'INV-77', 'inv_dt': '20261001', 'cur_cd': 'USD', 'amt': 14930, 'pay_dt': '20261130',
         'gate': 'PASS', 'svms_status': 'U',
         'raw_card': {'kind': 'purchase', 'odr_no': 'BGBBES2607B21A', 'tamt': 14930, 'amt': 14930}}
    p.update(over)
    return p


class RpCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db, self.old_cfg = appmod.DATABASE, appmod.app.config['DATABASE']
        self.old_pdf = routes_rpcost.RPCOST_PDF_DIR
        routes_rpcost.RPCOST_PDF_DIR = os.path.join(self.tmp.name, 'rpcost_pdfs')
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            shared_ns._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", ('secret',))
            appmod.execute("INSERT INTO expense_code(code,name) VALUES('090305','기타'),('030205','F.O PUMP')")
        self.c = appmod.app.test_client()
        with self.c.session_transaction() as s:
            s['user_id'], s['role'], s['username'] = 1, 'admin', 'tester'

    def tearDown(self):
        appmod.DATABASE, appmod.app.config['DATABASE'] = self.old_db, self.old_cfg
        routes_rpcost.RPCOST_PDF_DIR = self.old_pdf
        self.tmp.cleanup()

    def ingest(self, p):
        return self.c.post('/api/ext/rpcost/drafts', json=p, headers=KEY)

    def row(self, did):
        with appmod.app.app_context():
            r = dict(appmod.query('SELECT * FROM rp_cost_draft WHERE id=?', (did,), one=True))
        r['rc'] = json.loads(r['raw_card'])
        return r

    def set_status(self, did, st):
        with appmod.app.app_context():
            appmod.execute('UPDATE rp_cost_draft SET status=? WHERE id=?', (st, did))

    # ---- ingest ----
    def test_ingest_requires_key_and_kind(self):
        self.assertEqual(401, self.c.post('/api/ext/rpcost/drafts', json=repair_payload()).status_code)
        self.assertEqual(400, self.ingest(repair_payload(kind='other')).status_code)
        self.assertEqual(400, self.ingest(repair_payload(svms_key='')).status_code)

    def test_ingest_dedup_by_kind_and_key(self):
        r1 = self.ingest(repair_payload())
        self.assertEqual(201, r1.status_code)
        did = r1.get_json()['id']
        r2 = self.ingest(repair_payload(amt=12600))
        self.assertEqual((200, True), (r2.status_code, r2.get_json().get('updated')))
        self.assertEqual(12600, self.row(did)['amt'])
        # 같은 svms_key 라도 kind 가 다르면 별개 카드
        r3 = self.ingest(purchase_payload(svms_key='ATNHMI26100901'))
        self.assertEqual(201, r3.status_code)
        # 진행·결정 건은 손대지 않음
        for st in ('approved', 'submitting', 'rejected'):
            self.set_status(did, st)
            r = self.ingest(repair_payload(amt=1))
            self.assertTrue(r.get_json().get('dedup'), st)
            self.assertEqual(12600, self.row(did)['amt'])

    def test_ingest_failed_and_reopen(self):
        did = self.ingest(repair_payload()).get_json()['id']
        self.set_status(did, 'failed')
        r = self.ingest(repair_payload())
        self.assertEqual(201, r.status_code)
        new = r.get_json()['id']
        self.set_status(new, 'submitted')
        self.assertTrue(self.ingest(repair_payload(svms_status='CU')).get_json().get('dedup'))
        r = self.ingest(repair_payload(svms_status='IU'))
        self.assertEqual((201, new), (r.status_code, r.get_json()['reopened_from']))
        self.assertIn('[재개]', self.row(new)['result'])

    def test_gate_normalized_and_audit_injection_stripped(self):
        p = repair_payload(gate='weird', raw={'inv_dt_override': '20200101', 'original_inv_dt': '20261009',
                                              'exp_sel': {'02': {'exp_cd': '999999', 'source': 'user'}}})
        did = self.ingest(p).get_json()['id']
        r = self.row(did)
        self.assertEqual('HOLD', r['gate'])
        self.assertNotIn('inv_dt_override', r['rc'])
        self.assertNotIn('02', r['rc']['exp_sel'])

    # ---- edit ----
    def test_edit_inv_dt_audit_and_reingest_preserves(self):
        did = self.ingest(repair_payload()).get_json()['id']
        r = self.c.post('/api/rpcost/drafts/%d/edit' % did, json={'inv_dt': '20260722', 'exp': {'02': '030205'}})
        self.assertEqual(200, r.status_code, r.get_json())
        row = self.row(did)
        self.assertEqual(('20260722', 1), (row['inv_dt'], row['date_match']))
        self.assertEqual(('20261009', '20260722', 'tester'),
                         (row['rc']['original_inv_dt'], row['rc']['inv_dt_override'], row['rc']['inv_dt_override_by']))
        self.assertEqual({'exp_cd': '030205', 'exp_nm': 'F.O PUMP', 'source': 'user'},
                         {k: row['rc']['exp_sel']['02'][k] for k in ('exp_cd', 'exp_nm', 'source')})
        self.assertEqual('HOLD', row['gate'])            # 편집은 gate 를 올리지 않는다
        self.ingest(repair_payload())                    # 재적재: 사람 교정 보존
        row = self.row(did)
        self.assertEqual(('20260722', '030205'), (row['inv_dt'], row['rc']['exp_sel']['02']['exp_cd']))
        # SVMS INV_DT 가 바뀌면 날짜 교정은 버림(EXP 사람 지정은 유지)
        self.ingest(repair_payload(inv_dt='20261010', raw={'inv_dt': '20261010'}))
        row = self.row(did)
        self.assertEqual('20261010', row['inv_dt'])
        self.assertNotIn('inv_dt_override', row['rc'])
        self.assertEqual('030205', row['rc']['exp_sel']['02']['exp_cd'])

    def test_edit_restore_original_clears_override(self):
        did = self.ingest(repair_payload()).get_json()['id']
        self.c.post('/api/rpcost/drafts/%d/edit' % did, json={'inv_dt': '20260722'})
        self.c.post('/api/rpcost/drafts/%d/edit' % did, json={'inv_dt': '20261009'})
        row = self.row(did)
        self.assertEqual(('20261009', 0), (row['inv_dt'], row['date_match']))
        self.assertNotIn('original_inv_dt', row['rc'])

    def test_edit_validation_and_scope(self):
        did = self.ingest(repair_payload()).get_json()['id']
        for body in ({'inv_dt': '20260231'}, {'inv_dt': '2026-07-22'}, {'exp': {'04': '090305'}},
                     {'exp': {'02': 'NOPE01'}}, {'exp': ['x']}):
            self.assertEqual(400, self.c.post('/api/rpcost/drafts/%d/edit' % did, json=body).status_code, body)
        pid = self.ingest(purchase_payload()).get_json()['id']
        self.assertEqual(400, self.c.post('/api/rpcost/drafts/%d/edit' % pid, json={'inv_dt': '20260722'}).status_code)
        self.set_status(did, 'approved')
        self.assertEqual(409, self.c.post('/api/rpcost/drafts/%d/edit' % did, json={'inv_dt': '20260722'}).status_code)

    def test_edit_cas_conflict(self):
        did = self.ingest(repair_payload()).get_json()['id']
        orig = routes_rpcost.query

        def racing_query(sql, params=(), one=False):
            res = orig(sql, params, one)
            if sql.startswith('SELECT kind, status, inv_dt'):   # 편집이 읽은 직후 재적재가 끼어든 상황
                appmod.execute("UPDATE rp_cost_draft SET raw_card=? WHERE id=?", ('{"kind":"repair"}', did))
            return res
        routes_rpcost.query = racing_query
        try:
            r = self.c.post('/api/rpcost/drafts/%d/edit' % did, json={'inv_dt': '20260722'})
        finally:
            routes_rpcost.query = orig
        self.assertEqual(409, r.status_code)
        self.assertIn('겹쳤', r.get_json()['error'])

    # ---- decisions ----
    def test_approve_bulk_reject_reset_delete(self):
        a = self.ingest(repair_payload()).get_json()['id']
        b = self.ingest(purchase_payload()).get_json()['id']
        r = self.c.post('/api/rpcost/drafts/approve-bulk', json={'ids': [a, b, 999, 'x']}).get_json()
        self.assertEqual((2, 2), (r['approved'], r['skipped']))
        self.assertEqual(409, self.c.delete('/api/rpcost/drafts/%d' % a).status_code)   # approved 삭제 금지
        self.assertEqual(200, self.c.post('/api/rpcost/drafts/%d/reset' % a).status_code)
        self.assertEqual(400, self.c.post('/api/rpcost/drafts/%d/reject' % a, json={}).status_code)
        r = self.c.post('/api/rpcost/drafts/%d/reject' % a, json={'reason': '금액 확인'})
        self.assertEqual(200, r.status_code)
        row = self.row(a)
        self.assertEqual(('rejected', '금액 확인'), (row['status'], row['reject_reason']))
        self.assertIn('SVMS 반려는 안 함', row['result'])
        self.assertEqual(409, self.c.post('/api/rpcost/drafts/%d/approve' % a).status_code)
        self.assertEqual(200, self.c.post('/api/rpcost/drafts/%d/reset' % a).status_code)
        self.assertEqual(200, self.c.delete('/api/rpcost/drafts/%d' % a).status_code)

    def test_admin_guard(self):
        with self.c.session_transaction() as s:
            s['role'] = 'user'
        self.assertEqual(403, self.c.get('/api/rpcost/drafts').status_code)

    # ---- runner ext ----
    def test_claim_peek_result_and_pdf(self):
        a = self.ingest(purchase_payload()).get_json()['id']
        pdf = b'%PDF-1.4 test'
        self.assertEqual(400, self.c.post('/api/ext/rpcost/drafts/%d/pdf' % a, data=b'nope', headers=KEY).status_code)
        self.assertEqual(200, self.c.post('/api/ext/rpcost/drafts/%d/pdf' % a, data=pdf, headers=KEY).status_code)
        resp = self.c.get('/api/rpcost/drafts/%d/pdf' % a)
        self.assertEqual(pdf, resp.data)
        resp.close()
        self.c.post('/api/rpcost/drafts/%d/approve' % a)
        peek = self.c.get('/api/ext/rpcost/approved?peek=1', headers=KEY).get_json()
        self.assertEqual((1, 'approved'), (peek['count'], self.row(a)['status']))
        got = self.c.get('/api/ext/rpcost/approved', headers=KEY).get_json()
        self.assertEqual([a], [d['id'] for d in got['drafts']])
        tok = got['drafts'][0]['claim_token']
        self.assertEqual(self.row(a)['raw_card'], got['drafts'][0]['raw_card'])     # 정확히 claim 한 스냅샷
        self.assertEqual('submitting', self.row(a)['status'])
        self.assertEqual(0, self.c.get('/api/ext/rpcost/approved', headers=KEY).get_json()['count'])  # 재서빙 없음
        self.assertEqual(409, self.c.post('/api/rpcost/drafts/%d/reset' % a).status_code)
        self.assertEqual(409, self.c.post('/api/ext/rpcost/drafts/%d/pdf' % a, data=pdf, headers=KEY).status_code)
        r = self.c.post('/api/ext/rpcost/drafts/%d/result' % a, json={'ok': True, 'result': '승인완료'}, headers=KEY)
        self.assertEqual((409, False), (r.status_code, r.get_json()['applied']))     # 토큰 없음 = 미반영
        r = self.c.post('/api/ext/rpcost/drafts/%d/result' % a,
                        json={'ok': True, 'result': '승인완료', 'claim_token': tok}, headers=KEY)
        self.assertTrue(r.get_json()['applied'])
        self.assertEqual('submitted', self.row(a)['status'])
        self.assertEqual(404, self.c.get('/api/rpcost/drafts/%d/pdf' % a).status_code)   # 성공 시 PDF 정리
        self.assertFalse(self.c.post('/api/ext/rpcost/drafts/%d/result' % a, json={'ok': False, 'claim_token': tok},
                                     headers=KEY).get_json()['applied'])

    def test_result_failure_and_stale_claim(self):
        a = self.ingest(repair_payload()).get_json()['id']
        self.c.post('/api/rpcost/drafts/%d/approve' % a)
        tok = self.c.get('/api/ext/rpcost/approved', headers=KEY).get_json()['drafts'][0]['claim_token']
        self.c.post('/api/ext/rpcost/drafts/%d/result' % a,
                    json={'ok': False, 'result': '수리 컨펌 캡처 대기', 'claim_token': tok}, headers=KEY)
        self.assertEqual(('failed', '수리 컨펌 캡처 대기'), (self.row(a)['status'], self.row(a)['result']))
        b = self.ingest(purchase_payload()).get_json()['id']
        with appmod.app.app_context():
            appmod.execute("UPDATE rp_cost_draft SET status='submitting', done_at=datetime('now','localtime','-7 hours') "
                           "WHERE id=?", (b,))
        self.c.get('/api/ext/rpcost/approved', headers=KEY)
        self.assertEqual('failed', self.row(b)['status'])

    def test_open_and_reconcile(self):
        a = self.ingest(repair_payload()).get_json()['id']
        b = self.ingest(purchase_payload()).get_json()['id']
        o = self.c.get('/api/ext/rpcost/open', headers=KEY).get_json()
        self.assertEqual({a, b}, {d['id'] for d in o['drafts']})
        r = self.c.post('/api/ext/rpcost/reconcile', headers=KEY, json={'items': [
            {'id': a, 'kind': 'repair', 'svms_key': 'ATNHMI26100901', 'svms_status': 'CU'},
            {'id': b, 'kind': 'purchase', 'svms_key': 'WRONG', 'svms_status': 'A'},
            {'id': b, 'kind': 'purchase', 'svms_key': 'BGBBES2607B21AA1', 'svms_status': 'U'}]}).get_json()
        self.assertEqual((1, 2), (r['closed_n'], r['skipped_n']))
        self.assertEqual(('submitted', 'pending'), (self.row(a)['status'], self.row(b)['status']))

    # ---- hardening ----
    def test_ingest_requires_vsl_cd(self):
        self.assertEqual(400, self.ingest(repair_payload(vsl_cd='')).status_code)
        self.assertEqual(400, self.ingest(purchase_payload(vsl_cd=None)).status_code)

    def test_concurrent_duplicate_ingest_no_500(self):
        orig = routes_rpcost.query

        def racing_query(sql, params=(), one=False):
            if sql.startswith('SELECT id, status, raw_card FROM rp_cost_draft WHERE kind=?'):
                # 이 요청이 '없음'을 본 직후 다른 ingest 가 먼저 INSERT 한 상황
                appmod.execute("INSERT INTO rp_cost_draft(kind, svms_key, vsl_cd, raw_card) VALUES('repair',?, 'ATNH','{}')",
                               (params[1],))
                return None
            return orig(sql, params, one)
        routes_rpcost.query = racing_query
        try:
            r = self.ingest(repair_payload())
        finally:
            routes_rpcost.query = orig
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json()['dedup'])
        with appmod.app.app_context():
            n = appmod.query("SELECT COUNT(*) c FROM rp_cost_draft WHERE svms_key='ATNHMI26100901'", one=True)['c']
        self.assertEqual(1, n)
        for _ in range(3):                                   # 순차 중복도 활성카드 1장
            self.assertIn(self.ingest(repair_payload()).status_code, (200, 201))
        with appmod.app.app_context():
            n = appmod.query("SELECT COUNT(*) c FROM rp_cost_draft WHERE svms_key='ATNHMI26100901' "
                             "AND status IN ('pending','approved','submitting')", one=True)['c']
        self.assertEqual(1, n)

    def test_reset_collision_409(self):
        a = self.ingest(repair_payload()).get_json()['id']
        self.c.post('/api/rpcost/drafts/%d/reject' % a, json={'reason': 'x'})
        b = self.ingest(repair_payload()).get_json()['id']   # rejected 는 dedup 대상이라 새 카드 안 생김
        self.assertEqual(a, b)
        with appmod.app.app_context():                       # 다른 열린 카드가 있는 상태를 직접 구성
            appmod.execute("INSERT INTO rp_cost_draft(kind, svms_key, vsl_cd, raw_card) VALUES('repair','ATNHMI26100901','ATNH','{}')")
        r = self.c.post('/api/rpcost/drafts/%d/reset' % a)
        self.assertEqual(409, r.status_code)
        self.assertEqual('rejected', self.row(a)['status'])

    def test_claim_only_and_aba_token(self):
        a = self.ingest(repair_payload()).get_json()['id']
        b = self.ingest(purchase_payload()).get_json()['id']
        self.c.post('/api/rpcost/drafts/approve-bulk', json={'ids': [a, b]})
        got = self.c.get('/api/ext/rpcost/approved?only=BGBBES2607B21AA1', headers=KEY).get_json()
        self.assertEqual([b], [d['id'] for d in got['drafts']])
        self.assertEqual('approved', self.row(a)['status'])
        pk = self.c.get('/api/ext/rpcost/approved?peek=1&only=ATNHMI26100901', headers=KEY).get_json()
        self.assertEqual([a], [d['id'] for d in pk['drafts']])
        t1 = self.c.get('/api/ext/rpcost/approved?only=ATNHMI26100901', headers=KEY).get_json()['drafts'][0]['claim_token']
        # ABA: 같은 카드가 approved 로 되돌아가 reset→재적재→재승인→재claim
        self.set_status(a, 'approved')
        self.assertEqual(200, self.c.post('/api/rpcost/drafts/%d/reset' % a).status_code)
        self.assertIsNone(self.row(a)['claim_token'])
        self.ingest(repair_payload(amt=99))
        self.c.post('/api/rpcost/drafts/%d/approve' % a)
        t2 = self.c.get('/api/ext/rpcost/approved', headers=KEY).get_json()['drafts'][0]['claim_token']
        self.assertNotEqual(t1, t2)
        r = self.c.post('/api/ext/rpcost/drafts/%d/result' % a, json={'ok': True, 'claim_token': t1}, headers=KEY)
        self.assertEqual((409, False, 'submitting'), (r.status_code, r.get_json()['applied'], self.row(a)['status']))
        r = self.c.post('/api/ext/rpcost/drafts/%d/result' % a, json={'ok': True, 'claim_token': t2}, headers=KEY)
        self.assertEqual((True, 'submitted'), (r.get_json()['applied'], self.row(a)['status']))

    def test_claim_cas_on_raw_card(self):
        a = self.ingest(purchase_payload()).get_json()['id']
        self.c.post('/api/rpcost/drafts/%d/approve' % a)
        orig = routes_rpcost.query

        def racing_query(sql, params=(), one=False):
            res = orig(sql, params, one)
            if "WHERE status='approved' AND (?=''" in sql:
                appmod.execute("UPDATE rp_cost_draft SET raw_card='{\"kind\":\"purchase\",\"tamt\":1}' WHERE id=?", (a,))
            return res
        routes_rpcost.query = racing_query
        try:
            got = self.c.get('/api/ext/rpcost/approved', headers=KEY).get_json()
        finally:
            routes_rpcost.query = orig
        self.assertEqual(0, got['count'])
        self.assertEqual('approved', self.row(a)['status'])

    def test_auth_guards(self):
        a = self.ingest(purchase_payload()).get_json()['id']
        anon = appmod.app.test_client()
        for m, url in (('get', '/api/rpcost/drafts'), ('post', '/api/rpcost/drafts/%d/approve' % a),
                       ('post', '/api/rpcost/drafts/approve-bulk'), ('post', '/api/rpcost/drafts/%d/reject' % a),
                       ('post', '/api/rpcost/drafts/%d/reset' % a), ('post', '/api/rpcost/drafts/%d/edit' % a),
                       ('delete', '/api/rpcost/drafts/%d' % a), ('get', '/api/rpcost/drafts/%d/pdf' % a)):
            self.assertEqual(401, getattr(anon, m)(url).status_code, url)
        with self.c.session_transaction() as s:
            s['role'] = 'user'
        for m, url in (('get', '/api/rpcost/drafts'), ('post', '/api/rpcost/drafts/%d/approve' % a),
                       ('post', '/api/rpcost/drafts/%d/reset' % a), ('delete', '/api/rpcost/drafts/%d' % a)):
            self.assertEqual(403, getattr(self.c, m)(url).status_code, url)
        for m, url in (('post', '/api/ext/rpcost/drafts'), ('get', '/api/ext/rpcost/approved'),
                       ('get', '/api/ext/rpcost/approved?peek=1'), ('post', '/api/ext/rpcost/drafts/%d/result' % a),
                       ('post', '/api/ext/rpcost/drafts/%d/pdf' % a), ('get', '/api/ext/rpcost/open'),
                       ('post', '/api/ext/rpcost/reconcile')):
            self.assertEqual(401, getattr(anon, m)(url).status_code, url)
            self.assertEqual(401, getattr(anon, m)(url, headers={'X-API-Key': 'wrong'}).status_code, url)
        self.assertEqual('pending', self.row(a)['status'])

    def test_task_registered(self):
        from helpers_shared import AUTOMATION_TASKS_BASE
        self.assertIn('rpcost_confirm', AUTOMATION_TASKS_BASE)
        r = self.c.post('/api/automation/run', json={'task': 'rpcost_confirm', 'mode': 'live'})
        self.assertEqual(200, r.status_code, r.get_json())

    def test_claim_kind_filter(self):
        a = self.ingest(purchase_payload()).get_json()['id']
        self.set_status(a, 'approved')
        self.assertEqual(400, self.c.get('/api/ext/rpcost/approved?kind=bogus', headers=KEY).status_code)
        self.assertEqual(0, self.c.get('/api/ext/rpcost/approved?peek=1&kind=repair', headers=KEY).get_json()['count'])
        self.assertEqual(0, self.c.get('/api/ext/rpcost/approved?kind=repair', headers=KEY).get_json()['count'])
        self.assertEqual('approved', self.row(a)['status'])
        self.assertEqual(1, self.c.get('/api/ext/rpcost/approved?peek=1&kind=purchase', headers=KEY).get_json()['count'])
        self.assertEqual(1, self.c.get('/api/ext/rpcost/approved?kind=purchase', headers=KEY).get_json()['count'])
        self.assertEqual('submitting', self.row(a)['status'])

    def test_run_kind_param(self):
        r = self.c.post('/api/automation/run', json={'task': 'rpcost_confirm', 'mode': 'verify', 'params': {'kind': 'x'}})
        self.assertEqual(400, r.status_code)
        r = self.c.post('/api/automation/run', json={'task': 'rpcost_confirm', 'mode': 'verify', 'params': {'kind': 'repair'}})
        self.assertEqual(200, r.status_code, r.get_json())
        with appmod.app.app_context():
            row = appmod.query("SELECT params FROM automation_run WHERE run_id=?", (r.get_json()['run_id'],), one=True)
        self.assertEqual({'kind': 'repair'}, json.loads(row['params']))

    def test_page_renders(self):
        r = self.c.get('/rpcost')
        self.assertEqual(200, r.status_code)
        self.assertIn('TRMT 에만 표시', r.get_data(as_text=True))


class RpCostBearerTests(unittest.TestCase):
    """iOS 앱은 쿠키 없이 Bearer 토큰만 보낸다 — 인보이스 큐와 같은 @admin_required 경로로
    /api/rpcost/* (목록·PDF·편집·승인·리젝·결정취소) 가 통과해야 한다(CSRF 면제 포함)."""
    tearDown = RpCostTests.tearDown
    ingest = RpCostTests.ingest
    row = RpCostTests.row

    def setUp(self):
        RpCostTests.setUp(self)
        from werkzeug.security import generate_password_hash
        with appmod.app.app_context():
            appmod.execute("INSERT INTO users(username, password_hash, display_name, role, active) "
                           "VALUES('rpapp', ?, 'rpapp', 'admin', 1)", (generate_password_hash('pw-rp'),))
        self.app_c = appmod.app.test_client()          # 쿠키 세션 없음
        r = self.app_c.post('/api/auth/token', json={'username': 'rpapp', 'password': 'pw-rp'})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True)[:200])
        self.h = {'Authorization': 'Bearer %s' % r.get_json()['token']}

    def test_bearer_full_flow(self):
        old = appmod.app.config.get('CSRF_PROTECT')
        appmod.app.config['CSRF_PROTECT'] = True          # Bearer 는 CSRF 면제여야 함
        try:
            rid = self.ingest(repair_payload()).get_json()['id']
            pid = self.ingest(purchase_payload()).get_json()['id']
            self.assertEqual(401, self.app_c.get('/api/rpcost/drafts').status_code)
            lst = self.app_c.get('/api/rpcost/drafts', headers=self.h)
            self.assertEqual(200, lst.status_code)
            self.assertEqual({rid, pid}, {d['id'] for d in lst.get_json()['drafts']})
            self.assertEqual(404, self.app_c.get('/api/rpcost/drafts/%d/pdf' % rid, headers=self.h).status_code)
            e = self.app_c.post('/api/rpcost/drafts/%d/edit' % rid, headers=self.h,
                                json={'inv_dt': '20261010', 'exp': {'02': '030205'}})
            self.assertEqual(200, e.status_code, e.get_data(as_text=True))
            self.assertEqual('rpapp', self.row(rid)['rc']['exp_sel']['02']['by'])
            self.assertEqual(400, self.app_c.post('/api/rpcost/drafts/%d/edit' % pid, headers=self.h,
                                                  json={'inv_dt': '20261010'}).status_code)
            self.assertEqual(200, self.app_c.post('/api/rpcost/drafts/%d/approve' % rid, headers=self.h,
                                                  json={}).status_code)
            self.assertEqual('rpapp', self.row(rid)['decided_by'])
            self.assertEqual(200, self.app_c.post('/api/rpcost/drafts/%d/reset' % rid, headers=self.h,
                                                  json={}).status_code)
            rj = self.app_c.post('/api/rpcost/drafts/%d/reject' % pid, headers=self.h, json={'reason': 'dup'})
            self.assertEqual(200, rj.status_code)
            self.assertIn('SVMS 는 그대로', rj.get_json()['message'])
        finally:
            appmod.app.config['CSRF_PROTECT'] = old

    def test_approve_expect_rev_cas(self):
        rid = self.ingest(repair_payload()).get_json()['id']
        rev = {d['id']: d['rev'] for d in self.app_c.get('/api/rpcost/drafts', headers=self.h).get_json()['drafts']}[rid]
        # 표시 후 사람 편집(또는 재적재)으로 카드가 바뀜 → 옛 rev 승인은 409, 상태 pending 유지
        self.assertEqual(200, self.app_c.post('/api/rpcost/drafts/%d/edit' % rid, headers=self.h,
                                              json={'exp': {'02': '030205'}}).status_code)
        r = self.app_c.post('/api/rpcost/drafts/%d/approve' % rid, headers=self.h, json={'expect_rev': rev})
        self.assertEqual(409, r.status_code)
        self.assertTrue(r.get_json().get('stale'))
        self.assertEqual('pending', self.row(rid)['status'])
        new = {d['id']: d['rev'] for d in self.app_c.get('/api/rpcost/drafts', headers=self.h).get_json()['drafts']}[rid]
        self.assertNotEqual(rev, new)
        self.assertEqual(200, self.app_c.post('/api/rpcost/drafts/%d/approve' % rid, headers=self.h,
                                              json={'expect_rev': new}).status_code)
        self.assertEqual('approved', self.row(rid)['status'])
        r2 = self.app_c.post('/api/rpcost/drafts/%d/approve' % rid, headers=self.h, json={'expect_rev': new})
        self.assertEqual(409, r2.status_code)          # 이미 결정됨


if __name__ == '__main__':
    unittest.main()
