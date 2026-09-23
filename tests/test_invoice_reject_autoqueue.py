"""인보이스 리젝 = 즉시 실행 계약 (2026-09-23 형 지시).

이전: POST /api/invoice/drafts/<id>/reject 는 status='rejecting' 마킹만 하고, 실제 SVMS 리젝은
웹 [일괄승인+컨펌] 버튼(invoice_confirm live)을 눌러야 러너가 rejecting 큐를 처리했다.
iOS 에서 리젝하면 아무도 실행하지 않아 "로컬 표시"에 머물렀다.

지금: reject 라우트가 `invoice_reject` automation_run 을 자동 큐잉한다(AOR reject 패턴).
  · 리젝 전용 run — approved(승인 대기) 카드는 절대 이 run 에 실리지 않는다(러너 INV_REJECT_ONLY).
  · queued run 이 이미 있으면 재사용(claim 전이라 새 카드도 실림), running 이면 새 run(fresh).
  · killswitch ON → 마킹은 되지만 run 은 없고 reject_run=None 으로 명시.
  · 상태 게이트(pending/approved 만 리젝 가능, 사유 필수, raw_card 필수)는 그대로.
"""
import json
import os
import tempfile
import unittest

import app as appmod
from source_bundle import shared_ns  # noqa: E402


class InvoiceRejectAutoQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        self.old_testing = appmod.app.config.get('TESTING')
        db = os.path.join(self.tmp.name, 'inv_reject.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        with appmod.app.app_context():
            appmod.init_db(False)
            shared_ns._ensure_api_table()
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=1, username='tester', display_name='T', role='admin', supervisor_id=None)

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        appmod.app.config['TESTING'] = self.old_testing
        self.tmp.cleanup()

    # ---- helpers ----
    def _draft(self, status='pending', raw_card='{"inv_cd":"X"}'):
        with appmod.app.app_context():
            return appmod.execute(
                "INSERT INTO invoice_draft(inv_cd, status, raw_card, created_at) "
                "VALUES(?,?,?,datetime('now','localtime'))",
                (f'INV-{os.urandom(3).hex()}', status, raw_card))

    def _row(self, did):
        with appmod.app.app_context():
            return appmod.query('SELECT * FROM invoice_draft WHERE id=?', (did,), one=True)

    def _runs(self, task='invoice_reject'):
        with appmod.app.app_context():
            return [dict(r) for r in appmod.query(
                'SELECT run_id, task, mode, status, requested_by FROM automation_run WHERE task=? ORDER BY id',
                (task,))]

    def _set_run_status(self, run_id, status):
        with appmod.app.app_context():
            appmod.execute('UPDATE automation_run SET status=? WHERE run_id=?', (status, run_id))

    def _killswitch(self, on):
        with appmod.app.app_context():
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('automation_enabled',?)",
                           ('0' if on else '1',))

    def _reject(self, did, reason='증빙파일 첨부 바랍니다'):
        return self.client.post(f'/api/invoice/drafts/{did}/reject', json={'reason': reason})

    # ---- contract ----
    def test_reject_marks_and_queues_live_invoice_reject_run(self):
        did = self._draft()
        r = self._reject(did)
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual('rejecting', j['status'])
        self.assertTrue(j['reject_run'])
        self.assertIn('통보메일', j['message'])
        row = self._row(did)
        self.assertEqual('rejecting', row['status'])
        self.assertEqual('증빙파일 첨부 바랍니다', row['reject_reason'])
        self.assertEqual('tester', row['decided_by'])
        runs = self._runs()
        self.assertEqual(1, len(runs))
        self.assertEqual({'task': 'invoice_reject', 'mode': 'live', 'status': 'queued', 'requested_by': 'tester'},
                         {k: runs[0][k] for k in ('task', 'mode', 'status', 'requested_by')})
        self.assertEqual(j['reject_run'], runs[0]['run_id'])

    def test_reject_never_queues_invoice_confirm(self):
        """리젝이 approved 카드를 컨펌하는 invoice_confirm 을 건드리면 iOS 로컬승인건이 의도치 않게 컨펌된다."""
        self._draft(status='approved')
        did = self._draft()
        self.assertEqual(200, self._reject(did).status_code)
        self.assertEqual([], self._runs('invoice_confirm'))
        self.assertEqual(1, len(self._runs('invoice_reject')))

    def test_second_reject_reuses_queued_run(self):
        a, b = self._draft(), self._draft()
        ra = self._reject(a).get_json()
        rb = self._reject(b).get_json()
        self.assertEqual(ra['reject_run'], rb['reject_run'])
        self.assertEqual(1, len(self._runs()))

    def test_reject_while_running_creates_fresh_run(self):
        """running run 은 시작 시 rejecting 을 이미 claim 했으니 재사용하면 새 카드가 안 실린다."""
        a, b = self._draft(), self._draft()
        ra = self._reject(a).get_json()
        self._set_run_status(ra['reject_run'], 'running')
        rb = self._reject(b).get_json()
        self.assertNotEqual(ra['reject_run'], rb['reject_run'])
        self.assertEqual(['running', 'queued'], [r['status'] for r in self._runs()])

    def test_killswitch_marks_but_does_not_queue(self):
        self._killswitch(on=True)
        did = self._draft()
        j = self._reject(did).get_json()
        self.assertEqual('rejecting', j['status'])
        self.assertIsNone(j['reject_run'])
        self.assertIn('killswitch', j['message'])
        self.assertEqual([], self._runs())

    def test_reason_required_and_no_queue_on_reject_failure(self):
        did = self._draft()
        self.assertEqual(400, self.client.post(f'/api/invoice/drafts/{did}/reject', json={}).status_code)
        self.assertEqual(400, self._reject(did, reason='   ').status_code)
        self.assertEqual('pending', self._row(did)['status'])
        self.assertEqual([], self._runs())

    def test_already_decided_returns_409_without_queue(self):
        for st in ('rejecting', 'reject_submitting', 'submitting', 'submitted', 'rejected', 'failed'):
            did = self._draft(status=st)
            r = self._reject(did)
            self.assertEqual(409, r.status_code, st)
            self.assertEqual(st, self._row(did)['status'])
        self.assertEqual([], self._runs())

    def test_missing_raw_card_is_400_without_queue(self):
        did = self._draft(raw_card=None)
        self.assertEqual(400, self._reject(did).status_code)
        self.assertEqual([], self._runs())

    def test_approved_card_can_still_be_rejected(self):
        did = self._draft(status='approved')
        self.assertEqual(200, self._reject(did).status_code)
        self.assertEqual('rejecting', self._row(did)['status'])

    # ---- 올마이트 지적 보강: 동시성·busy·자기치유 ----
    def test_reject_with_running_and_queued_reuses_the_queued_run(self):
        a, b, c = self._draft(), self._draft(), self._draft()
        ra = self._reject(a).get_json()
        self._set_run_status(ra['reject_run'], 'running')
        rb = self._reject(b).get_json()          # fresh queued
        rc = self._reject(c).get_json()          # queued 재사용 — 폭증 없음
        self.assertEqual(rb['reject_run'], rc['reject_run'])
        self.assertEqual(['running', 'queued'], [r['status'] for r in self._runs()])

    def test_automation_run_button_is_busy_while_reject_run_queued(self):
        """허브 버튼(/api/automation/run)과 자동큐가 같은 task 를 두 번 쌓지 않는다."""
        did = self._draft()
        self.assertEqual(200, self._reject(did).status_code)
        r = self.client.post('/api/automation/run', json={'task': 'invoice_reject', 'mode': 'live'})
        self.assertEqual(409, r.status_code)
        self.assertEqual(1, len(self._runs()))

    def test_list_self_heals_orphan_rejecting_without_run(self):
        """마킹 뒤 큐 적재가 실패했거나 killswitch 를 나중에 켠 경우 — 목록 조회가 run 을 만든다."""
        self._draft(status='rejecting')          # run 없이 rejecting 만 존재(orphan)
        self.assertEqual([], self._runs())
        r = self.client.get('/api/invoice/drafts')
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json()['reject_run'])
        self.assertEqual(1, len(self._runs()))
        # 반복 조회해도 queued 1개 재사용
        self.client.get('/api/invoice/drafts')
        self.assertEqual(1, len(self._runs()))

    def test_list_does_not_queue_when_nothing_is_rejecting(self):
        self._draft(status='pending'); self._draft(status='approved'); self._draft(status='reject_submitting')
        r = self.client.get('/api/invoice/drafts')
        self.assertIsNone(r.get_json()['reject_run'])
        self.assertEqual([], self._runs())

    def test_list_respects_killswitch_and_then_heals_after_reenable(self):
        self._killswitch(on=True)
        did = self._draft()
        self.assertIsNone(self._reject(did).get_json()['reject_run'])
        self.assertIsNone(self.client.get('/api/invoice/drafts').get_json()['reject_run'])
        self.assertEqual([], self._runs())
        self._killswitch(on=False)
        self.assertTrue(self.client.get('/api/invoice/drafts').get_json()['reject_run'])
        self.assertEqual(1, len(self._runs()))

    def test_queue_failure_keeps_marking_and_returns_none(self):
        """_queue_aor 예외 → 500 이 아니라 마킹 유지 + reject_run=None(목록 조회가 나중에 치유)."""
        import routes_calendar_dock as rcd
        old = rcd._queue_aor
        def boom(*a, **k): raise RuntimeError('db locked')
        rcd._queue_aor = boom
        try:
            did = self._draft()
            r = self._reject(did)
        finally:
            rcd._queue_aor = old
        self.assertEqual(200, r.status_code)
        self.assertIsNone(r.get_json()['reject_run'])
        self.assertEqual('rejecting', self._row(did)['status'])

    def test_task_registered_for_hub_and_run_endpoint(self):
        from helpers_shared import automation_tasks
        with appmod.app.app_context():
            self.assertIn('invoice_reject', automation_tasks())


if __name__ == '__main__':
    unittest.main()
