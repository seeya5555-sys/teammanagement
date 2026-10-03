"""Vetting OBS 자동화 — Close report 자동판정 가드 + 메일 run 불변식."""
import json
import os
import tempfile
import unittest
from unittest import mock

import app as appmod
import ai_gemini

appmod.app.config['CSRF_PROTECT'] = False

DOC = ("1. Observation EPIRB test. Corrective Action: The lifebuoy rope was replaced with a new one on 28 August 2026 "
       "and the crew were briefed accordingly.\n"
       "2. Observation ballast. Corrective Action: Spare PCB has been ordered and will be installed upon delivery at next port.\n"
       "3. Observation COW. Corrective Action: The crude oil washing procedure was amended and officers trained on board.")


class VettingObsAutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db, self.old_cfg = appmod.DATABASE, appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            from helpers_shared import _get_api_key
            from app_core import execute
            self.key = _get_api_key(create=True)
            self.ves = execute("INSERT INTO vessels(name, vsl_cd, vessel_type) VALUES('GHANA TEST','GHTS','VLCC')")
            self.cntr = execute("INSERT INTO vessels(name, vsl_cd, vessel_type) VALUES('BOX TEST','BXTS','CONTAINER')")
            self.cvt = execute("INSERT INTO vettings(vessel_id, report_number, svms_close_report_yn) VALUES(?,?,'Y')",
                               (self.cntr, 'CNTR-1'))
            execute("INSERT INTO vt_findings(vetting_id,no,item,description,status) VALUES(?,1,'x','y','Open')", (self.cvt,))
            execute("INSERT INTO vt_attachments(vetting_id, filename, stored_name, source, source_type, sha256) "
                    "VALUES(?,'c.docx','c.docx','svms','close','cd')", (self.cvt,))
            execute("INSERT INTO daily_mail_settings(vessel_id,to_emails,enabled) VALUES(?,'b@x.com',1)", (self.cntr,))
            self.vt = execute("INSERT INTO vettings(vessel_id, report_number, inspection_date, inspection_company, port, "
                              "sire_type, valid, svms_close_report_yn) VALUES(?,?,?,?,?,?,?,'Y')",
                              (self.ves, 'LVKX-0383-3966-7845', '2026-08-06', 'PETROVIETNAM', 'Rotterdam',
                               'Discharge', 'Last Result'))
            mk = lambda no, st='Open': execute(
                "INSERT INTO vt_findings(vetting_id,no,item,description,status) VALUES(?,?,?,?,?)",
                (self.vt, no, f'item{no}', f'desc {no}', st))
            self.f1, self.f2, self.f3, self.f4 = mk(1), mk(2), mk(3), mk(4, 'Closed')
            execute("INSERT INTO vt_attachments(vetting_id, filename, stored_name, source, source_type, sha256) "
                    "VALUES(?,?,?,'svms','close','ab')", (self.vt, 'resp.docx', 'x.docx'))
            execute("INSERT INTO daily_mail_settings(vessel_id,to_emails,cc_emails,dear_name,enabled) "
                    "VALUES(?,?,?,?,1)", (self.ves, 'sup@csm.com', 'cc@x.com', 'Charalampos'))
        self.c = appmod.app.test_client()
        with self.c.session_transaction() as s:
            s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'
        self.h = {'X-API-Key': self.key}

    def tearDown(self):
        appmod.DATABASE, appmod.app.config['DATABASE'] = self.old_db, self.old_cfg
        self.tmp.cleanup()

    def _ai(self, items):
        return mock.patch.multiple(ai_gemini, _close_doc_text=mock.Mock(return_value=DOC),
                                   _gemini_call_json=mock.Mock(return_value={'items': items}),
                                   _condense_obs=mock.Mock(return_value={}), GEMINI_API_KEY='k')

    def _items(self):
        return [
            {'finding_id': self.f1, 'status': 'Closed', 'action_ko': '로프 교체 완료',
             'evidence': 'The lifebuoy rope was replaced with a new one on 28 August 2026 and the crew were briefed accordingly.'},
            # AI 가 Closed 라 해도 근거에 예정 표현 → Open 유지
            {'finding_id': self.f2, 'status': 'Closed', 'action_ko': 'PCB 수령 후 교체 예정',
             'evidence': 'Spare PCB has been ordered and will be installed upon delivery at next port.'},
            # 원문에 없는 근거(환각) → Open 유지, remark 도 안 씀
            {'finding_id': self.f3, 'status': 'Closed', 'action_ko': '완료',
             'evidence': 'All corrective actions were completed and verified by the superintendent.'},
            # 이미 Closed 인 항목/모르는 id 는 무시
            {'finding_id': self.f4, 'status': 'Open', 'evidence': 'x', 'action_ko': 'x'},
        ]

    def _status(self):
        with appmod.app.app_context():
            from app_core import query
            return {r['id']: (r['status'], r['full_report_remark']) for r in
                    query('SELECT id,status,full_report_remark FROM vt_findings WHERE vetting_id=?', (self.vt,))}

    def test_guard_rules(self):
        norm = ai_gemini._close_norm(DOC)
        self.assertEqual('Closed', ai_gemini._close_guard('Closed', self._items()[0]['evidence'], norm)[0])
        self.assertEqual('evidence_has_pending_wording', ai_gemini._close_guard('Closed', self._items()[1]['evidence'], norm)[1])
        self.assertEqual('evidence_not_in_document', ai_gemini._close_guard('Closed', self._items()[2]['evidence'], norm)[1])
        self.assertEqual('evidence_has_pending_wording', ai_gemini._close_guard(
            'Closed', 'The lifebuoy rope has not been replaced with a new one on 28 August 2026', ai_gemini._close_norm(
                'The lifebuoy rope has not been replaced with a new one on 28 August 2026'))[1])
        self.assertEqual('ai_not_closed', ai_gemini._close_guard('Open', self._items()[0]['evidence'], norm)[1])

    def test_dry_judges_without_writing(self):
        with self._ai(self._items()):
            r = self.c.post(f'/api/ext/vettings/{self.vt}/close-auto', headers=self.h, json={'dry': True}).get_json()
        self.assertEqual(1, r['closed_count'])
        self.assertEqual('Open', self._status()[self.f1][0])

    def test_apply_closes_only_guarded_and_rebuilds_summary_and_is_idempotent(self):
        with self._ai(self._items()):
            r = self.c.post(f'/api/ext/vettings/{self.vt}/close-auto', headers=self.h, json={}).get_json()
            st = self._status()
            self.assertEqual('Closed', st[self.f1][0])
            self.assertEqual('로프 교체 완료', st[self.f1][1])
            self.assertEqual(('Open', 'PCB 수령 후 교체 예정'), st[self.f2])
            self.assertEqual('Open', st[self.f3][0])
            self.assertFalse(st[self.f3][1])
            self.assertEqual('Closed', st[self.f4][0])
            self.assertEqual(2, r['open_after'])
            self.assertIn('SIRE OBS 잔여 2건 조치 중', r['overall_remark'])
            # 같은 입력(첨부·Open 집합 동일) 재실행 = 판정 생략 — 단 Open 집합이 바뀌었으므로 새 입력
            r2 = self.c.post(f'/api/ext/vettings/{self.vt}/close-auto', headers=self.h, json={}).get_json()
            r3 = self.c.post(f'/api/ext/vettings/{self.vt}/close-auto', headers=self.h, json={}).get_json()
        self.assertNotIn('skipped', r2)
        self.assertEqual('already_judged_same_input', r3.get('skipped'))
        with appmod.app.app_context():
            from app_core import query
            a = query("SELECT * FROM vt_full_report_audit WHERE vetting_id=?", (self.vt,))
            self.assertEqual('auto:svms-close', a[0]['applied_by'])

    def test_candidates_and_no_close_flag(self):
        cands = self.c.get('/api/ext/vettings/close-auto/candidates', headers=self.h).get_json()['candidates']
        self.assertEqual([self.vt], [c['id'] for c in cands])
        with appmod.app.app_context():
            from app_core import execute
            execute("UPDATE vettings SET svms_close_report_yn='N' WHERE id=?", (self.vt,))
        r = self.c.post(f'/api/ext/vettings/{self.vt}/close-auto', headers=self.h, json={}).get_json()
        self.assertEqual('no_close_report_flag', r['skipped'])

    def test_open_export_only_open_items(self):
        r = self.c.get(f'/api/ext/vettings/{self.vt}/open-export.xlsx', headers=self.h)
        self.assertEqual(200, r.status_code)
        self.assertEqual(sorted([self.f1, self.f2, self.f3]), sorted(int(x) for x in r.headers['X-Finding-Ids'].split(',')))
        self.assertIn('SIRE_GHANA TEST_20260806.xlsx', r.headers['Content-Disposition'])

    def test_mail_off_by_default_then_claim_once(self):
        cfg = self.c.get('/api/ext/vetting-mail/config?week=2026W41', headers=self.h).get_json()
        v = cfg['vettings'][0]
        self.assertEqual('mail_off', v['skip'])
        self.assertEqual('GHANA TEST - Discharge SIRE Inspection Status (06th Aug 2026)', v['subject'])
        self.assertTrue(v['body'].startswith('Dear Charalampos,\nGood day.\n'))
        self.assertIn('3 open observations (out of 4', v['body'])
        claim = {'vetting_id': self.vt, 'iso_week': '2026W41', 'finding_ids': v['open_ids']}
        self.assertEqual(409, self.c.post('/api/ext/vetting-mail/runs', headers=self.h, json=claim).status_code)
        self.assertEqual(200, self.c.post(f'/api/vetting-mail/settings/{self.ves}/enabled', json={'enabled': True}).status_code)
        stale = dict(claim, finding_ids=v['open_ids'][:1])
        self.assertEqual(409, self.c.post('/api/ext/vetting-mail/runs', headers=self.h, json=stale).status_code)
        r = self.c.post('/api/ext/vetting-mail/runs', headers=self.h, json=claim)
        self.assertEqual(201, r.status_code)
        self.assertEqual('sup@csm.com', r.get_json()['to_emails'])
        self.assertEqual(409, self.c.post('/api/ext/vetting-mail/runs', headers=self.h, json=claim).status_code)
        rid = r.get_json()['id']
        self.assertEqual('sent', self.c.post(f'/api/ext/vetting-mail/runs/{rid}/state', headers=self.h,
                                             json={'state': 'sent'}).get_json()['state'])
        self.assertEqual(409, self.c.post(f'/api/vetting-mail/runs/{rid}/release').status_code)
        cfg = self.c.get('/api/ext/vetting-mail/config?week=2026W41', headers=self.h).get_json()
        self.assertEqual('sent', cfg['vettings'][0]['run_this_week']['state'])

    def test_vlcc_only(self):
        self.assertEqual(400, self.c.post(f'/api/vetting-mail/settings/{self.cntr}/enabled', json={'enabled': True}).status_code)
        names = [v['name'] for v in self.c.get('/api/vetting-mail/status').get_json()['vessels']]
        self.assertNotIn('BOX TEST', names)
        cfg = self.c.get('/api/ext/vetting-mail/config?week=2026W41', headers=self.h).get_json()
        self.assertEqual([self.vt], [v['vetting_id'] for v in cfg['vettings']])
        r = self.c.post(f'/api/ext/vettings/{self.cvt}/close-auto', headers=self.h, json={}).get_json()
        self.assertEqual('not_vlcc', r['skipped'])

    def test_on_requires_to(self):
        with appmod.app.app_context():
            from app_core import execute
            execute("UPDATE daily_mail_settings SET to_emails='' WHERE vessel_id=?", (self.ves,))
        self.assertEqual(400, self.c.post(f'/api/vetting-mail/settings/{self.ves}/enabled', json={'enabled': True}).status_code)

    def test_ext_requires_key(self):
        self.assertIn(self.c.get('/api/ext/vetting-mail/config').status_code, (401, 403))
        self.assertIn(self.c.post(f'/api/ext/vettings/{self.vt}/close-auto', json={}).status_code, (401, 403))

    def _sent_run(self):
        self.c.post(f'/api/vetting-mail/settings/{self.ves}/enabled', json={'enabled': True})
        ids = sorted([self.f1, self.f2, self.f3])
        rid = self.c.post('/api/ext/vetting-mail/runs', headers=self.h,
                          json={'vetting_id': self.vt, 'iso_week': '2026W41', 'finding_ids': ids}).get_json()['id']
        self.c.post(f'/api/ext/vetting-mail/runs/{rid}/state', headers=self.h, json={'state': 'sent'})
        return rid

    def test_reply_close_update_review_dedup_and_summary(self):
        rid = self._sent_run()
        p = self.c.get('/api/ext/vetting-mail/runs/pending', headers=self.h).get_json()['runs']
        self.assertEqual([rid], [r['id'] for r in p])
        self.assertEqual(3, len(p[0]['findings']))
        u = f'/api/ext/vetting-mail/runs/{rid}/findings/%d/reply'
        m = {'message_id': 'outlook:1', 'date': '2026-10-06'}
        r = self.c.post(u % self.f1, headers=self.h, json=dict(m, kind='close', text='로프 교체 완료',
                                                               evidence='The lifebuoy rope was replaced with a new one.')).get_json()
        self.assertEqual('Closed', r['status'])
        self.assertTrue(self.c.post(u % self.f1, headers=self.h, json=dict(m, kind='close')).get_json()['duplicate'])
        # 서버 재검증: 부정/예정 근거의 close 는 needs_review 로 강등
        r = self.c.post(u % self.f2, headers=self.h, json=dict(m, kind='close', text='x', message_id='outlook:0',
                        evidence='The PCB has not been replaced yet onboard.')).get_json()
        self.assertEqual(('needs_review', 'Open'), (r['applied'], r['status']))
        self.c.post(u % self.f2, headers=self.h, json=dict(m, kind='update', text='PCB 수령 대기'))
        self.c.post(u % self.f3, headers=self.h, json=dict(m, kind='needs_review', text='항목 불명확'))
        # 이 메일로 보내지 않은 항목(f4)은 거부
        self.assertEqual(409, self.c.post(u % self.f4, headers=self.h, json=dict(m, kind='close')).status_code)
        st = self._status()
        self.assertEqual('Closed', st[self.f1][0])
        self.assertEqual(('Open', '회신(10/06) PCB 수령 대기'), st[self.f2])
        self.assertTrue(st[self.f3][1].startswith('[확인 필요] '))
        with self._ai([]):
            d = self.c.post(f'/api/ext/vetting-mail/runs/{rid}/reply-done', headers=self.h,
                            json={'message_id': 'outlook:1', 'changed': True}).get_json()
        self.assertIn('SIRE OBS 잔여 2건', d['overall_remark'])
        p = self.c.get('/api/ext/vetting-mail/runs/pending', headers=self.h).get_json()['runs']
        self.assertEqual(['outlook:1'], p[0]['processed_message_ids'])
        with appmod.app.app_context():
            from app_core import query
            ev = query("SELECT kind, before_json FROM vetting_mail_events WHERE run_id=? AND kind='close'", (rid,))
            self.assertIn('"status": "Open"', ev[0]['before_json'])

    def test_reply_requires_sent_run(self):
        self.c.post(f'/api/vetting-mail/settings/{self.ves}/enabled', json={'enabled': True})
        ids = sorted([self.f1, self.f2, self.f3])
        rid = self.c.post('/api/ext/vetting-mail/runs', headers=self.h,
                          json={'vetting_id': self.vt, 'iso_week': '2026W41', 'finding_ids': ids}).get_json()['id']
        r = self.c.post(f'/api/ext/vetting-mail/runs/{rid}/findings/{self.f1}/reply', headers=self.h,
                        json={'message_id': 'x', 'kind': 'close'})
        self.assertEqual(409, r.status_code)
    def test_manual_request_separate_from_weekly_and_followup_latest(self):
        ids = sorted([self.f1, self.f2, self.f3])
        # 수동은 OFF 여도 가능, 자동 주간 claim 과 별개
        q = self.c.post(f'/api/vetting-mail/manual/{self.ves}', json={})
        self.assertEqual(201, q.status_code)
        self.assertEqual(409, self.c.post(f'/api/vetting-mail/manual/{self.ves}', json={}).status_code)  # 대기 중복
        self.assertEqual(400, self.c.post(f'/api/vetting-mail/manual/{self.cntr}', json={}).status_code)  # VLCC 아님
        qid = q.get_json()['id']
        self.assertEqual([qid], [r['id'] for r in self.c.get('/api/ext/vetting-mail/manual', headers=self.h).get_json()['requests']])
        self.assertEqual(409, self.c.post('/api/ext/vetting-mail/runs', headers=self.h, json={   # claim 전 거부
            'vetting_id': self.vt, 'iso_week': '2026W41', 'finding_ids': ids, 'request_id': qid}).status_code)
        self.assertEqual(200, self.c.post(f'/api/ext/vetting-mail/manual/{qid}/claim', headers=self.h).status_code)
        self.assertEqual(409, self.c.post(f'/api/ext/vetting-mail/manual/{qid}/claim', headers=self.h).status_code)
        m = self.c.post('/api/ext/vetting-mail/runs', headers=self.h, json={
            'vetting_id': self.vt, 'iso_week': '2026W41', 'finding_ids': ids, 'request_id': qid})
        # 주차가 바뀌어도 같은 요청×SIRE 재claim 불가(키=M<id>)
        self.assertEqual(409, self.c.post('/api/ext/vetting-mail/runs', headers=self.h, json={
            'vetting_id': self.vt, 'iso_week': '2026W42', 'finding_ids': ids, 'request_id': qid}).status_code)
        self.assertEqual(201, m.status_code)
        self.assertTrue(m.get_json()['manual'])
        self.assertTrue(m.get_json()['body'].startswith('Dear Charalampos,'))
        mid = m.get_json()['id']
        self.c.post(f'/api/ext/vetting-mail/runs/{mid}/state', headers=self.h, json={'state': 'sent', 'sent_at': '2026-10-05 10:00:00'})
        self.assertEqual('done', self.c.post(f'/api/ext/vetting-mail/manual/{qid}/state', headers=self.h,
                                             json={'state': 'done'}).get_json()['state'])
        # 같은 주 자동 발송은 여전히 가능(별개)
        cfg = self.c.get('/api/ext/vetting-mail/config?week=2026W41', headers=self.h).get_json()
        self.assertIsNone(cfg['vettings'][0]['run_this_week'])
        self.c.post(f'/api/vetting-mail/settings/{self.ves}/enabled', json={'enabled': True})
        a = self.c.post('/api/ext/vetting-mail/runs', headers=self.h, json={
            'vetting_id': self.vt, 'iso_week': '2026W41', 'finding_ids': ids}).get_json()['id']
        self.c.post(f'/api/ext/vetting-mail/runs/{a}/state', headers=self.h, json={'state': 'sent', 'sent_at': '2026-10-06 09:30:00'})
        # 팔로우업 = 최신 발송분 1건만
        p = self.c.get('/api/ext/vetting-mail/runs/pending', headers=self.h).get_json()['runs']
        self.assertEqual([a], [r['id'] for r in p])

    def test_template_edit_and_validation(self):
        t = self.c.get('/api/vetting-mail/template').get_json()
        self.assertIn('{open}', t['template']['body_tpl'])
        self.assertEqual(400, self.c.put('/api/vetting-mail/template', json={'subject_tpl': 'x {bad}', 'body_tpl': 'y'}).status_code)
        self.assertEqual(200, self.c.put('/api/vetting-mail/template', json={
            'subject_tpl': '{vessel} OBS ({open})', 'body_tpl': 'Dear {dear},\n{open} of {total}'}).status_code)
        v = self.c.get('/api/ext/vetting-mail/config?week=2026W41', headers=self.h).get_json()['vettings'][0]
        self.assertEqual('GHANA TEST OBS (3)', v['subject'])
        self.assertEqual('Dear Charalampos,\n3 of 4', v['body'])

    def test_page_renders(self):
        r = self.c.get('/vetting-mail')
        self.assertEqual(200, r.status_code)
        self.assertIn('수동 발송', r.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
