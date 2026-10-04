import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import app as A
import class_mail_service as S
from app_core import execute, query


class ClassCadenceTests(unittest.TestCase):
    def items(self, *dates):
        return [{'due_date': d} for d in dates]

    def test_inclusive_thirty_days_overdue_and_mixed_dates_weekly(self):
        for dates in [('2026-11-04',), ('2026-10-04',), ('2026-09-01', '2027-01-01')]:
            self.assertEqual('weekly', S.cadence(self.items(*dates), date(2026, 10, 5))['cadence'])
            self.assertTrue(S.cadence(self.items(*dates), date(2026, 10, 12))['eligible'])

    def test_first_third_monday_not_second_fourth_or_fifth(self):
        items = self.items('2027-03-01')
        for day, eligible in [(1, True), (8, False), (15, True), (22, False), (29, False)]:
            self.assertEqual(eligible, S.cadence(items, date(2026, 6, day))['eligible'])
        self.assertFalse(S.cadence(items, date(2026, 6, 2))['eligible'])

    def test_unknown_due_and_no_findings_never_send(self):
        for dates in [(), ('',), ('2026-02-30',), ('2026-10-05', 'TBD')]:
            self.assertFalse(S.cadence(self.items(*dates), date(2026, 10, 5))['eligible'])

    def test_next_send_escalates_to_weekly_on_due_boundary(self):
        self.assertEqual('2026-10-12 07:30', S.next_send(self.items('2026-11-10'), date(2026, 10, 6)))


class ClassMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db, self.old_cfg = A.DATABASE, A.app.config['DATABASE']
        self.old_csrf = A.app.config.get('CSRF_PROTECT')
        A.DATABASE = os.path.join(self.temp.name, 'test.db')
        A.app.config['DATABASE'] = A.DATABASE
        A.app.config['CSRF_PROTECT'] = False
        with A.app.app_context(), contextlib.redirect_stdout(io.StringIO()):
            A.init_db(False)
            from helpers_shared import _get_api_key
            self.key = _get_api_key(create=True)
            sup = query("SELECT id FROM supervisors WHERE name='손유석'", one=True)
            sid = sup['id'] if sup else execute("INSERT INTO supervisors(name) VALUES('손유석')")
            self.vid = execute("INSERT INTO vessels(name,vsl_cd,active) VALUES('TEST STAR','TSTR',1)")
            execute('INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES(?,?)', (sid, self.vid))
            execute("INSERT INTO daily_mail_settings(vessel_id,to_emails,cc_emails,dear_name,enabled) VALUES(?,'captain@example.com','team@example.com','Capt. Test',0)", (self.vid,))
            execute('CREATE TABLE IF NOT EXISTS class_status(id INTEGER PRIMARY KEY,vessel_id INTEGER,updated_at TEXT)')
            execute("CREATE TABLE IF NOT EXISTS class_status_items(id INTEGER PRIMARY KEY,cs_id INTEGER,category TEXT,no INTEGER,issued_date TEXT,description TEXT,due_date TEXT,action_taken TEXT,importance TEXT,updated_at TEXT)")
            execute("INSERT INTO class_status(id,vessel_id,updated_at) VALUES(1,?,'2026-10-01')", (self.vid,))
            execute("INSERT INTO class_status_items(id,cs_id,category,no,issued_date,description,due_date,action_taken,importance,updated_at) VALUES(1,1,'COC',1,'2026-09-01','Repair pipe','2026-10-30','Existing action','','')")
        self.client = A.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, role='admin', username='admin')
        self.headers = {'X-API-Key': self.key}
        self.clock = patch.object(S, 'today_kst', return_value=date(2026, 10, 5))
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        A.DATABASE, A.app.config['DATABASE'] = self.old_db, self.old_cfg
        A.app.config['CSRF_PROTECT'] = self.old_csrf
        self.temp.cleanup()

    def get(self, path):
        return self.client.get('/api/ext/class-mail' + path, headers=self.headers)

    def post(self, path, body):
        return self.client.post('/api/ext/class-mail' + path, headers=self.headers, json=body)

    def prepare_and_claim(self):
        self.client.post(f'/api/class-mail/settings/{self.vid}/enabled', json={'enabled': True})
        prepared = self.get(f'/vessels/{self.vid}/prepare').get_json()
        return self.post('/runs', {**prepared, 'vessel_id': self.vid}), prepared

    def sent_run(self):
        response, prepared = self.prepare_and_claim()
        self.assertEqual(201, response.status_code, response.get_json())
        rid = response.get_json()['id']
        self.assertEqual(200, self.post(f'/runs/{rid}/state', {'state': 'sent'}).status_code)
        return rid

    def test_defaults_off_contacts_shared_and_template_preview(self):
        body = self.client.get('/api/class-mail/status').get_json()
        vessel = next(v for v in body['vessels'] if v['vessel_id'] == self.vid)
        self.assertEqual(0, vessel['enabled'])
        self.assertEqual('captain@example.com', vessel['to_emails'])
        self.assertIn('Dear Capt. Test,', vessel['body'])
        self.assertIn('Action Taken', vessel['body'])
        self.assertIn('completion date and schedule', vessel['body'])
        self.assertEqual([], self.get('/config').get_json()['vessels'])

    def test_duplicate_claim_and_server_owned_recipients(self):
        response, prepared = self.prepare_and_claim()
        self.assertEqual(201, response.status_code)
        self.assertEqual('captain@example.com', response.get_json()['to_emails'])
        second = self.post('/runs', {**prepared, 'vessel_id': self.vid, 'to_emails': 'attacker@example.com'})
        self.assertEqual(409, second.status_code)

    def test_stale_snapshot_fails_claim_and_pre_send_checks(self):
        self.client.post(f'/api/class-mail/settings/{self.vid}/enabled', json={'enabled': True})
        prepared = self.get(f'/vessels/{self.vid}/prepare').get_json()
        with A.app.app_context():
            execute("UPDATE class_status_items SET due_date='2026-11-01' WHERE id=1")
        self.assertEqual(409, self.post('/runs', {**prepared, 'vessel_id': self.vid}).status_code)
        response, _ = self.prepare_and_claim()
        rid = response.get_json()['id']
        with A.app.app_context():
            execute("UPDATE daily_mail_settings SET cc_emails='changed@example.com' WHERE vessel_id=?", (self.vid,))
        self.assertEqual(409, self.get(f'/runs/{rid}/check').status_code)

    def test_roster_and_auth_are_fail_closed(self):
        self.assertEqual(401, A.app.test_client().get('/api/class-mail/status').status_code)
        self.assertEqual(401, A.app.test_client().get('/api/ext/class-mail/config').status_code)
        with A.app.app_context():
            execute("DELETE FROM supervisors WHERE name='손유석'")
        self.assertEqual([], self.get('/config').get_json()['vessels'])

    def test_reply_identity_rebind_dedup_preserves_manual_history(self):
        rid = self.sent_run()
        with A.app.app_context():
            execute('UPDATE class_status_items SET id=99 WHERE id=1')
        body = {'message_id': 'message1', 'sender': 'delegate@example.com', 'received_at': '2026-10-05 10:00:00', 'updates': [{'item_id': 1, 'remark': 'Permanent repair planned by 30 Oct'}]}
        response = self.post(f'/runs/{rid}/reply', body)
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual(1, response.get_json()['updated'])
        self.assertTrue(self.post(f'/runs/{rid}/reply', body).get_json()['duplicate'])
        with A.app.app_context():
            self.assertEqual('Existing action\n\nPermanent repair planned by 30 Oct', query('SELECT action_taken FROM class_status_items WHERE id=99', one=True)['action_taken'])
            self.assertEqual(1, query('SELECT COUNT(*) n FROM class_status_items', one=True)['n'])
        followup = {**body, 'message_id': 'message2', 'updates': [{'item_id': 1, 'remark': 'Repair completed'}]}
        self.assertEqual(1, self.post(f'/runs/{rid}/reply', followup).get_json()['updated'])
        pending = self.get('/runs/pending').get_json()['runs'][0]
        self.assertEqual('Existing action\n\nPermanent repair planned by 30 Oct\n\nRepair completed', pending['reply_rows'][0]['action'])

    def test_concurrent_manual_edit_blocks_reply_without_overwrite(self):
        rid = self.sent_run()
        with A.app.app_context():
            execute("UPDATE class_status_items SET action_taken='Manual changed' WHERE id=1")
        response = self.post(f'/runs/{rid}/reply', {'message_id': 'm1', 'sender': 'x@example.com', 'updates': [{'item_id': 1, 'remark': 'Complete'}]})
        self.assertEqual(409, response.status_code)
        with A.app.app_context():
            self.assertEqual('Manual changed', query('SELECT action_taken FROM class_status_items WHERE id=1', one=True)['action_taken'])
            self.assertEqual(0, query('SELECT COUNT(*) n FROM class_mail_replies', one=True)['n'])

    def test_no_date_no_findings_or_off_cannot_claim(self):
        _, prepared = self.prepare_and_claim()
        # Separate vessel-week cannot be forced via requested week.
        self.assertEqual(409, self.post('/runs', {**prepared, 'vessel_id': self.vid, 'iso_week': '2026W42'}).status_code)
        with A.app.app_context():
            execute('DELETE FROM class_mail_runs')
            execute("UPDATE class_status_items SET due_date='' WHERE id=1")
        self.assertEqual(409, self.post('/runs', {**prepared, 'vessel_id': self.vid}).status_code)

    def test_korean_actions_and_original_reply_baseline_are_separate(self):
        rid=self.sent_run()
        first={'message_id':'ko1','sender':'delegate@example.com','updates':[{'item_id':1,'remark':'Pipe 30 Oct까지 영구수리 예정','remark_original':'Pipe permanent repair planned by 30 Oct'}]}
        self.assertEqual(1,self.post(f'/runs/{rid}/reply',first).get_json()['updated'])
        self.assertTrue(self.post(f'/runs/{rid}/reply',first).get_json()['duplicate'])
        pending=self.get('/runs/pending').get_json()['runs'][0]
        self.assertEqual('Existing action\n\nPipe permanent repair planned by 30 Oct',pending['reply_rows'][0]['action'])
        second={**first,'message_id':'ko2','updates':[{'item_id':1,'remark':'Pipe 30 Oct 영구수리 완료함','remark_original':'Pipe permanent repair completed on 30 Oct'}]}
        self.assertEqual(1,self.post(f'/runs/{rid}/reply',second).get_json()['updated'])
        with A.app.app_context():
            row=query('SELECT * FROM class_status_items WHERE id=1',one=True)
            self.assertEqual('Existing action\n\nPipe 30 Oct까지 영구수리 예정\n\nPipe 30 Oct 영구수리 완료함',row['action_taken'])
            self.assertEqual('2026-10-30',row['due_date']);self.assertEqual('COC',row['category'])
        pending=self.get('/runs/pending').get_json()['runs'][0]
        self.assertTrue(pending['reply_rows'][0]['action'].endswith('Pipe permanent repair completed on 30 Oct'))

    def test_untranslated_or_invalid_original_is_rejected_without_write(self):
        rid=self.sent_run()
        for ko,origin in [('Repair completed','Repair completed'),('수리 완료함',''),('수리 완료함',42)]:
            body={'message_id':'invalid','sender':'delegate@example.com','updates':[{'item_id':1,'remark':ko,'remark_original':origin}]}
            self.assertEqual(400,self.post(f'/runs/{rid}/reply',body).status_code)
        with A.app.app_context():
            self.assertEqual('Existing action',query('SELECT action_taken FROM class_status_items WHERE id=1',one=True)['action_taken'])
            self.assertEqual(0,query('SELECT COUNT(*) n FROM class_mail_replies',one=True)['n'])

    def test_sunday_canary_only_claims_one_ship_and_reserves_next_monday(self):
        self.client.post(f'/api/class-mail/settings/{self.vid}/enabled', json={'enabled': True})
        with patch.object(S, 'today_kst', return_value=date(2026, 10, 4)):
            prepared = self.get(f'/vessels/{self.vid}/prepare').get_json()
            payload = {**prepared, 'vessel_id': self.vid}
            self.assertEqual(409, self.post('/runs', payload).status_code)
            self.assertEqual(400, self.post('/canary', payload).status_code)
            self.assertEqual(401, A.app.test_client().post('/api/ext/class-mail/canary', json={**payload, 'confirmed_send': True}).status_code)
            response = self.post('/canary', {**payload, 'confirmed_send': True})
            self.assertEqual(201, response.status_code, response.get_json())
            run = response.get_json()
            self.assertEqual('C2026W41', run['iso_week'])
            self.assertIn('[TRMT-CS 2026W41', run['subject'])
            self.assertEqual(200, self.get(f"/runs/{run['id']}/check").status_code)
            self.assertEqual(409, self.post('/canary', {**payload, 'confirmed_send': True}).status_code)
            with A.app.app_context():
                execute("UPDATE daily_mail_settings SET cc_emails='changed@example.com' WHERE vessel_id=?", (self.vid,))
            self.assertEqual(409, self.get(f"/runs/{run['id']}/check").status_code)
        with patch.object(S, 'today_kst', return_value=date(2026, 10, 5)):
            monday = self.get(f'/vessels/{self.vid}/prepare').get_json()
            rejected = self.post('/runs', {**monday, 'vessel_id': self.vid})
            self.assertEqual(409, rejected.status_code)
            self.assertEqual('이번 주 발송 시도 이미 있음', rejected.get_json()['error'])
            with A.app.app_context():
                self.assertTrue(next(v for v in S.settings(date(2026,10,5)) if v['vessel_id']==self.vid)['run_this_week'])
        with A.app.app_context():
            self.assertEqual(1, query('SELECT COUNT(*) n FROM class_mail_runs', one=True)['n'])

    def test_admin_recovery_preserves_week_lock_and_allows_reply(self):
        response, prepared = self.prepare_and_claim()
        rid = response.get_json()['id']
        path = f'/api/class-mail/runs/{rid}/recover-sent'
        self.assertEqual(401, A.app.test_client().post(path, json={}).status_code)
        self.assertEqual(400, self.client.post(path, json={'sent_at': '2026-10-04 10:00:00'}).status_code)
        self.assertEqual(400, self.client.post(path, json={'confirmed_sent': True, 'sent_at': 'invalid'}).status_code)
        self.assertEqual(200, self.post(f'/runs/{rid}/state', {'state': 'failed', 'error': 'SendUnknown'}).status_code)
        body = {'confirmed_sent': True, 'sent_at': '2026-10-04 10:00:00'}
        self.assertEqual(200, self.client.post(path, json=body).status_code)
        self.assertEqual(409, self.client.post(path, json=body).status_code)
        self.assertEqual(409, self.post('/runs', {**prepared, 'vessel_id': self.vid}).status_code)
        self.assertEqual(1, self.post(f'/runs/{rid}/reply', {'message_id': 'recovered', 'sender': 'delegate@example.com', 'updates': [{'item_id': 1, 'remark': 'Repair completed'}]}).get_json()['updated'])
        with A.app.app_context():
            row = query('SELECT * FROM class_mail_runs WHERE id=?', (rid,), one=True)
            self.assertEqual('2026-10-04 10:00:00', row['sent_at'])
            self.assertIn('admin', row['error'])

    def test_class_admin_writes_require_cookie_csrf_token(self):
        A.app.config['CSRF_PROTECT'] = True
        path = f'/api/class-mail/settings/{self.vid}/enabled'
        self.assertEqual(403, self.client.post(path, json={'enabled': True}).status_code)
        page = self.client.get('/class-mail')
        self.assertEqual(200, page.status_code)
        with self.client.session_transaction() as session:
            token = session['_csrf_token']
        self.assertEqual(200, self.client.post(path, json={'enabled': True}, headers={'X-CSRF-Token': token}).status_code)
        self.assertEqual(200, self.get('/config').status_code)

    def test_template_requires_english_and_known_variables(self):
        for body in ('한글', '{unknown}'):
            self.assertEqual(400, self.client.put('/api/class-mail/template', json={'subject_tpl': 'Class {vessel}', 'body_tpl': body}).status_code)

    def test_runner_send_reply_roundtrip_isolated_and_no_external_send(self):
        import importlib.util
        from pathlib import Path
        from types import SimpleNamespace
        import openpyxl
        source = Path.home() / '.openclaw/workspace/automation/class-mail/class_mail_runner.py'
        spec = importlib.util.spec_from_file_location('class_runner_e2e', source)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        self.client.post(f'/api/class-mail/settings/{self.vid}/enabled', json={'enabled': True})
        def http(method, path, body=None, **kwargs):
            response = self.client.open(path, method=method, headers=self.headers, json=body)
            return response.status_code, response.get_json()
        def capture(subject, body, to, cc, attachment):
            self.assertIn('[TRMT-CS', subject)
            self.assertEqual(['captain@example.com'], to)
            self.assertEqual(['team@example.com'], cc)
            self.assertIn('Dear Capt. Test,', body)
            self.assertEqual('자동화', runner.dm.SIGNATURE)
            book = openpyxl.load_workbook(attachment)
            book.active.cell(5, 6, 'Existing action\n\nRepairs planned by 30 Oct')
            book.save(os.path.join(self.temp.name, 'reply.xlsx'))
        with patch.object(runner, 'STATE', Path(self.temp.name)), patch.object(runner.dm, 'http', side_effect=http), patch.object(runner.dm, 'outlook_send', side_effect=capture) as send:
            result = runner.send_weekly(SimpleNamespace(dry=False))
            self.assertTrue(result['ok'], result)
            self.assertEqual(1, len(result['sent']))
            self.assertTrue(runner.send_weekly(SimpleNamespace(dry=False))['ok'])
            self.assertEqual(1, send.call_count)
            run = self.get('/runs/pending').get_json()['runs'][0]
            record = dict(sender='delegate@example.com', received_at='2026-10-05 11:00:00', subject='RE: ' + run['subject'], body='Please find attached', message_id='local1', attachments=[os.path.join(self.temp.name, 'reply.xlsx')])
            with patch.object(runner.dm, 'outlook_scan', return_value=([record], '')), patch.object(runner.dm, 'to_korean', return_value=['30 Oct까지 수리 예정']) as translator:
                reply = runner.poll(SimpleNamespace(dry=False))
                self.assertTrue(reply['ok'], reply)
                self.assertEqual(1, reply['replies'][0]['updated'])
                record['message_id'] = 'different-local-id'
                self.assertEqual([], runner.poll(SimpleNamespace(dry=False))['replies'])
                self.assertEqual(1, translator.call_count)
                pending = self.get('/runs/pending').get_json()['runs'][0]
                self.assertEqual('Existing action\n\nRepairs planned by 30 Oct', pending['reply_rows'][0]['action'])
        with A.app.app_context():
            self.assertEqual('Existing action\n\n30 Oct까지 수리 예정', query('SELECT action_taken FROM class_status_items WHERE id=1', one=True)['action_taken'])
            self.assertEqual(1, query('SELECT COUNT(*) n FROM class_mail_runs', one=True)['n'])
            self.assertEqual(1, query('SELECT COUNT(*) n FROM class_mail_replies', one=True)['n'])


if __name__ == '__main__':
    unittest.main()
