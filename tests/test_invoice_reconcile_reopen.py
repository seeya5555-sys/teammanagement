"""인보이스 카드 ↔ SVMS 실측상태 동기화 회귀 테스트 (2026-08-06 형 지적 2건).

① SVMS 에서 사람이 직접 컨펌/반려한 건 → /api/ext/invoice/reconcile 로 카드 종결
   (안 하면 승인대기에 영구 잔류 = KWPSCI2608060001/0002 사고).
② 컨펌 후 SVMS 가 다시 STATUS=S 로 돌아온 재개건 → 새 카드로 재적재
   (안 하면 dedup 에 막혀 다시 안 뜸 = KWPSCI2608050001/0002 사고).
③ in-flight(submitting/reject_submitting) 는 러너와의 이중처리 방지로 절대 안 건드림.
"""
import os
import tempfile
import unittest

import app as appmod


class InvoiceReconcileReopenTests(unittest.TestCase):
    INV = 'KWPSCI2608060001'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            appmod._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", ('secret',))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['role'] = 'admin'
            s['username'] = 'tester'

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    # ---- helpers ----
    def _payload(self, inv_cd=None, svms_status='S'):
        inv_cd = inv_cd or self.INV
        return {
            'inv_cd': inv_cd, 'vsl_cd': 'KWPS', 'vsl_nm': 'KUWAIT PROSPERITY',
            'vndr_cd': 'V001', 'vndr_nm': 'CHINA MARINE', 'amt': 58638.2,
            'cur_cd': 'USD', 'vat': 0, 'inv_no': 'INV-1', 'inv_dt': '20260805',
            'gate': 'PASS', 'svms_status': svms_status,
            'raw_card': {'inv_cd': inv_cd, 'svms_status': svms_status},
        }

    def _post(self, payload):
        return self.client.post('/api/ext/invoice/drafts', json=payload,
                                headers={'X-API-Key': 'secret'})

    def _set_status(self, did, status):
        with appmod.app.app_context():
            appmod.execute('UPDATE invoice_draft SET status=? WHERE id=?', (status, did))

    def _row(self, did):
        with appmod.app.app_context():
            return dict(appmod.query('SELECT * FROM invoice_draft WHERE id=?', (did,), one=True))

    def _reconcile(self, items):
        # API는 /open이 준 정확한 카드 id를 요구한다. 테스트도 실제 러너 계약을
        # 그대로 사용한다.
        enriched = []
        with appmod.app.app_context():
            for item in items:
                x = dict(item)
                if x.get('id') is None and x.get('inv_cd'):
                    row = appmod.query('SELECT id FROM invoice_draft WHERE inv_cd=? ORDER BY id DESC LIMIT 1',
                                       (x['inv_cd'],), one=True)
                    if row:
                        x['id'] = row['id']
                enriched.append(x)
        return self.client.post('/api/ext/invoice/reconcile', json={'items': enriched},
                                headers={'X-API-Key': 'secret'})

    # ---- ① 외부 직접처리 카드 종결 ----
    def test_external_confirm_closes_pending_card(self):
        did = self._post(self._payload()).get_json()['id']
        r = self._reconcile([{'inv_cd': self.INV, 'svms_status': 'A'}])
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertEqual(1, body['closed_n'])
        self.assertEqual('pending', body['closed'][0]['from'])
        self.assertEqual('submitted', body['closed'][0]['to'])
        row = self._row(did)
        self.assertEqual('submitted', row['status'])
        self.assertIn('[외부]', row['result'])
        self.assertEqual('svms-direct', row['decided_by'])
        self.assertIsNotNone(row['done_at'])

    def test_external_reject_closes_approved_card(self):
        did = self._post(self._payload()).get_json()['id']
        self._set_status(did, 'approved')
        self.assertEqual(1, self._reconcile(
            [{'inv_cd': self.INV, 'svms_status': 'R'}]).get_json()['closed_n'])
        self.assertEqual('rejected', self._row(did)['status'])

    def test_reconcile_never_touches_inflight_or_unknown_status(self):
        did = self._post(self._payload()).get_json()['id']
        self._set_status(did, 'submitting')          # 러너 in-flight
        body = self._reconcile([{'inv_cd': self.INV, 'svms_status': 'A'}]).get_json()
        self.assertEqual(0, body['closed_n'])
        self.assertEqual('submitting', self._row(did)['status'])

        self._set_status(did, 'pending')
        for bad in ('S', 'D', '', None):             # 확신 없는 값은 종결 금지(fail-closed)
            body = self._reconcile([{'inv_cd': self.INV, 'svms_status': bad}]).get_json()
            self.assertEqual(0, body['closed_n'], bad)
            self.assertEqual('pending', self._row(did)['status'])

    def test_reconcile_requires_items_array(self):
        self.assertEqual(400, self.client.post('/api/ext/invoice/reconcile', json={'items': 'A'},
                                               headers={'X-API-Key': 'secret'}).status_code)

    def test_reconcile_requires_exact_id_and_invoice_pair(self):
        did = self._post(self._payload()).get_json()['id']
        h = {'X-API-Key': 'secret'}
        missing = self.client.post('/api/ext/invoice/reconcile', json={
            'items': [{'inv_cd': self.INV, 'svms_status': 'A'}]}, headers=h).get_json()
        self.assertEqual(0, missing['closed_n'])
        self.assertEqual('pending', self._row(did)['status'])
        wrong = self.client.post('/api/ext/invoice/reconcile', json={
            'items': [{'id': did + 99, 'inv_cd': self.INV, 'svms_status': 'A'}]}, headers=h).get_json()
        self.assertEqual(0, wrong['closed_n'])
        mismatch = self.client.post('/api/ext/invoice/reconcile', json={
            'items': [{'id': did, 'inv_cd': 'OTHER', 'svms_status': 'A'}]}, headers=h).get_json()
        self.assertEqual(0, mismatch['closed_n'])
        self.assertEqual('pending', self._row(did)['status'])

    def test_reconcile_is_idempotent(self):
        did = self._post(self._payload()).get_json()['id']
        item = [{'id': did, 'inv_cd': self.INV, 'svms_status': 'A'}]
        self.assertEqual(1, self._reconcile(item).get_json()['closed_n'])
        second = self._reconcile(item).get_json()
        self.assertEqual(0, second['closed_n'])
        self.assertEqual('submitted', self._row(did)['status'])

    def test_open_list_excludes_inflight_and_closed(self):
        p_did = self._post(self._payload()).get_json()['id']
        a_did = self._post(self._payload(inv_cd='KWPSCI2608060002')).get_json()['id']
        s_did = self._post(self._payload(inv_cd='KWPSCI2608060003')).get_json()['id']
        self._set_status(a_did, 'approved')
        self._set_status(s_did, 'submitting')
        body = self.client.get('/api/ext/invoice/open',
                               headers={'X-API-Key': 'secret'}).get_json()
        got = {d['inv_cd'] for d in body['drafts']}
        self.assertEqual({self.INV, 'KWPSCI2608060002'}, got)
        self.assertEqual(2, body['count'])
        self.assertTrue(p_did and a_did)

    # ---- ② 재개건(종착기록 stale) 재적재 ----
    def test_submitted_card_reopens_when_svms_back_to_s(self):
        did = self._post(self._payload()).get_json()['id']
        self._set_status(did, 'submitted')           # 우리가 컨펌 → 종착
        r = self._post(self._payload(svms_status='S'))   # SVMS 는 다시 S(결재반려 복귀)
        self.assertEqual(201, r.status_code)
        body = r.get_json()
        self.assertEqual(did, body['reopened_from'])
        self.assertNotEqual(did, body['id'])
        self.assertEqual('pending', self._row(body['id'])['status'])
        self.assertEqual('submitted', self._row(did)['status'])   # 이력 보존

    def test_locally_rejected_card_does_not_duplicate_when_svms_back_to_s(self):
        did = self._post(self._payload()).get_json()['id']
        self._set_status(did, 'rejected')
        body = self._post(self._payload(svms_status='S')).get_json()
        self.assertTrue(body['dedup'])
        self.assertEqual(did, body['id'])
        self.assertEqual('rejected', self._row(did)['status'])

    def test_submitted_card_reopens_when_svms_rejected(self):
        did = self._post(self._payload()).get_json()['id']
        self._set_status(did, 'submitted')
        body = self._post(self._payload(svms_status='R')).get_json()
        self.assertEqual(did, body['reopened_from'])
        self.assertEqual('pending', self._row(body['id'])['status'])

    def test_repeated_svms_rejected_ingest_updates_one_pending_card(self):
        first = self._post(self._payload('KWPSCI2608050002', svms_status='R')).get_json()
        second = self._post(self._payload('KWPSCI2608050002', svms_status='R'))
        self.assertEqual(200, second.status_code)
        self.assertTrue(second.get_json()['updated'])
        with appmod.app.app_context():
            n = appmod.query('SELECT COUNT(*) c FROM invoice_draft WHERE inv_cd=?',
                             ('KWPSCI2608050002',), one=True)['c']
        self.assertEqual(1, n)
        self.assertEqual(first['id'], second.get_json()['id'])

    def test_svms_rejected_invoice_can_be_loaded_as_new_pending_card(self):
        # 반려건은 기존 카드가 없어도 S/R ingest 경로에서 승인대기로 재적재돼야 한다.
        r = self._post(self._payload('KWPSCI2608050001', svms_status='R'))
        self.assertEqual(201, r.status_code)
        self.assertIsNone(r.get_json()['reopened_from'])
        self.assertEqual('pending', self._row(r.get_json()['id'])['status'])

    def test_no_reopen_for_inflight_or_missing_svms_status(self):
        did = self._post(self._payload()).get_json()['id']
        for st in ('approved', 'submitting', 'rejecting', 'reject_submitting'):
            self._set_status(did, st)
            r = self._post(self._payload(svms_status='S'))
            self.assertEqual(200, r.status_code, st)
            self.assertTrue(r.get_json()['dedup'], st)

        self._set_status(did, 'submitted')           # 구버전 러너(svms_status 없음) = 종전 dedup
        p = self._payload()
        p.pop('svms_status')
        r = self._post(p)
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json()['dedup'])

    def test_pending_card_is_updated_not_duplicated(self):
        did = self._post(self._payload()).get_json()['id']
        r = self._post(self._payload(svms_status='S'))
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json()['updated'])
        with appmod.app.app_context():
            n = appmod.query('SELECT COUNT(*) c FROM invoice_draft WHERE inv_cd=?',
                             (self.INV,), one=True)['c']
        self.assertEqual(1, n)
        self.assertEqual(did, r.get_json()['id'])


if __name__ == '__main__':
    unittest.main()
