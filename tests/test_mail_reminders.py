import unittest
from datetime import datetime
from unittest.mock import patch
from tests.test_class_mail import ClassMailTests
from app_core import execute
import mail_reminder_service as R


class ReminderTests(ClassMailTests):
    def test_atomic_claim_outcome_due_and_substantive_reply(self):
        rid = self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-10-01 18:00:00' WHERE id=?", (rid,))
        with patch.object(R, 'now_kst', return_value=datetime(2026,10,4,8,29)):
            self.assertEqual(409, self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':1}).status_code)
        with patch.object(R, 'now_kst', return_value=datetime(2026,10,4,8,30)):
            first = self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':1})
            self.assertEqual(200, first.status_code, first.get_json())
            self.assertIn('excel_sha256',first.get_json()['run'])
            self.assertEqual(200,self.post(f'/runs/{rid}/reminder',{'kind':'check','n':1,'token':first.get_json()['token']}).status_code)
            self.assertEqual(409, self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':1}).status_code)
            self.assertEqual(409, self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':2}).status_code)
            self.assertEqual(200, self.post(f'/runs/{rid}/reminder', {'kind':'result','n':1,'token':first.get_json()['token'],'state':'sent','sent_at':'2026-10-04 08:30:00'}).status_code)
            self.assertEqual(409, self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':2}).status_code)
        with self.app_context():
            execute("INSERT INTO class_mail_replies(run_id,message_id,sender,updated) VALUES(?,'receipt','captain',0)",(rid,))
        with patch.object(R, 'now_kst', return_value=datetime(2026,10,7,8,30)):
            second = self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':2})
            self.assertEqual(409,second.status_code,second.get_json())
            self.assertEqual(1,self.get('/reminders').get_json()['runs'][0]['reminder_count'])
            with self.app_context():
                execute("INSERT INTO class_mail_replies(run_id,message_id,sender,updated) VALUES(?,'action','captain',1)",(rid,))
            self.assertEqual([],self.get('/reminders').get_json()['runs'])
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'notify'}).status_code)
            # An already-recorded actual first outcome remains idempotent after a late reply.
            self.assertTrue(self.post(f'/runs/{rid}/reminder',{'kind':'result','n':1,'token':first.get_json()['token'],'state':'sent','sent_at':'2026-10-04 08:30:00'}).get_json()['duplicate'])

    def test_interpretation_hold_not_treated_as_no_reply(self):
        rid=self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-10-01 18:00:00' WHERE id=?",(rid,))
            execute("INSERT INTO class_mail_replies(run_id,message_id,sender,updated,note) VALUES(?,'hold','captain',0,'Unmatched action needs review')",(rid,))
        with patch.object(R,'now_kst',return_value=datetime(2026,10,4,8,30)):
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':1}).status_code)

    def test_confirmed_one_send_notify_once_and_disable_blocks(self):
        rid = self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-10-01 18:00:00' WHERE id=?", (rid,))
        # 같은 주(W40) 안에서만 리마인드(형 지시 10-18): 다음 주 화요일 claim 은 409
        with patch.object(R,'now_kst',return_value=datetime(2026,10,6,8,30)):
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':1}).status_code)
            self.assertEqual([],self.get('/reminders').get_json()['runs'])
        for n, day in [(1,4)]:
            with patch.object(R,'now_kst',return_value=datetime(2026,10,day,8,30)):
                claim = self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':n}).get_json()
                response = self.post(f'/runs/{rid}/reminder',{'kind':'result','n':n,'token':claim['token'],'state':'sent','sent_at':f'2026-10-{day:02d} 08:30:00'})
                self.assertEqual(200,response.status_code,response.get_json())
        with patch.object(R,'now_kst',return_value=datetime(2026,10,5,8,29)):
            self.assertEqual([],self.get('/reminders').get_json()['runs'])
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'notify'}).status_code)
        with patch.object(R,'now_kst',return_value=datetime(2026,10,5,8,30)):   # 미회신 통보(내부)는 주 경계 무관
            self.assertEqual(1,self.get('/reminders').get_json()['runs'][0]['reminder_count'])
            self.assertFalse(self.post(f'/runs/{rid}/reminder',{'kind':'notify'}).get_json()['duplicate'])
            self.assertTrue(self.post(f'/runs/{rid}/reminder',{'kind':'notify'}).get_json()['duplicate'])
            self.assertEqual([],self.get('/reminders').get_json()['runs'])
        with self.app_context():
            execute('UPDATE class_mail_settings SET enabled=0 WHERE vessel_id=?',(self.vid,))
        self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':2}).status_code)

    def test_vetting_current_contacts_and_reply(self):
        with self.app_context():
            vt = execute('INSERT INTO vettings(vessel_id,report_number) VALUES(?,?)',(self.vid,'TEST'))
            execute('INSERT INTO vetting_mail_settings(vessel_id,enabled) VALUES(?,1)',(self.vid,))
            fid = execute("INSERT INTO vt_findings(vetting_id,no,status) VALUES(?,1,'Open')",(vt,))
            rid = execute("INSERT INTO vetting_mail_runs(vetting_id,vessel_id,iso_week,state,subject,to_emails,cc_emails,sent_at) VALUES(?,?,'2026W40','sent','English subject','old@example.com','','2026-10-01 18:00:00')",(vt,self.vid))
            execute('UPDATE vetting_mail_runs SET finding_ids=? WHERE id=?',('['+str(fid)+']',rid))
        # Vetting 은 수·금 08시대만(형 지시 2026-10-18): 목 18:00 발송 → 금 08:00 슬롯. 슬롯 밖은 409.
        for off in (datetime(2026,10,2,7,59), datetime(2026,10,2,9,5), datetime(2026,10,4,8,30)):
            with patch.object(R,'now_kst',return_value=off):
                self.assertEqual(409,self.client.post(f'/api/ext/vetting-mail/runs/{rid}/reminder',headers=self.headers,json={'kind':'claim','n':1}).status_code, off)
                self.assertEqual([],self.client.get('/api/ext/vetting-mail/reminders',headers=self.headers).get_json()['runs'])
        with patch.object(R,'now_kst',return_value=datetime(2026,10,2,8,30)):
            self.assertEqual([rid],[r['id'] for r in self.client.get('/api/ext/vetting-mail/reminders',headers=self.headers).get_json()['runs']])
            response = self.client.post(f'/api/ext/vetting-mail/runs/{rid}/reminder',headers=self.headers,json={'kind':'claim','n':1})
            self.assertEqual(200,response.status_code,response.get_json())
            self.assertEqual('captain@example.com',response.get_json()['run']['to_emails'])
            self.assertIn('excel_sha256',response.get_json()['run'])
            with self.app_context():
                execute("INSERT INTO vetting_mail_events(run_id,kind,message_id) VALUES(?,'reply','action')",(rid,))
            self.assertEqual(409,self.client.post(f'/api/ext/vetting-mail/runs/{rid}/reminder',headers=self.headers,json={'kind':'notify'}).status_code)

    def test_due_queue_and_ambiguous_claim_hold(self):
        rid = self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-10-01 18:00:00' WHERE id=?",(rid,))
        with patch.object(R,'now_kst',return_value=datetime(2026,10,4,8,29)):
            self.assertEqual([],self.get('/reminders').get_json()['runs'])
        with patch.object(R,'now_kst',return_value=datetime(2026,10,4,8,30)):
            self.assertEqual([rid],[r['id'] for r in self.get('/reminders').get_json()['runs']])
            self.assertIn('excel_sha256',self.get('/reminders').get_json()['runs'][0])
            claim=self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':1}).get_json()
            queue=self.get('/reminders').get_json()
            self.assertEqual([],queue['runs'])
            self.assertEqual('claimed',queue['blocked'][0]['state'])
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'result','n':1,'token':'bad','state':'unknown'}).status_code)
            self.assertEqual(200,self.post(f'/runs/{rid}/reminder',{'kind':'result','n':1,'token':claim['token'],'state':'unknown'}).status_code)
            self.assertEqual('unknown',self.get('/reminders').get_json()['blocked'][0]['state'])
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':2}).status_code)
        with patch.object(R,'now_kst',return_value=datetime(2026,10,6,8,30)):   # 다음 ISO 주: 경보 제외, 기록 보존
            queue=self.get('/reminders').get_json()
            self.assertEqual([],queue['blocked']); self.assertEqual([],queue['runs'])
            with self.app_context():
                self.assertEqual('unknown',R._run('class',rid)['reminder_attempts'][0]['state'])

    def test_expired_invalid_timestamp_and_superseded_run(self):
        rid=self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-09-01 18:00:00' WHERE id=?",(rid,))
        with patch.object(R,'now_kst',return_value=datetime(2026,10,4,8,30)):
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':1}).status_code)
            with self.app_context():
                execute('UPDATE class_mail_runs SET sent_at=NULL WHERE id=?',(rid,))
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':1}).status_code)
            with self.app_context():
                execute("UPDATE class_mail_runs SET sent_at='2026-10-01 18:00:00' WHERE id=?",(rid,))
                execute("INSERT INTO class_mail_runs(vessel_id,iso_week,state,tag,subject,body,to_emails,cc_emails,snapshot,reply_rows,excel_sha256) SELECT vessel_id,'2026W42','sending',tag,subject,body,to_emails,cc_emails,snapshot,reply_rows,excel_sha256 FROM class_mail_runs WHERE id=?",(rid,))
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':1}).status_code)

    def app_context(self):
        import app as A
        return A.app.app_context()

class VettingSlotTests(unittest.TestCase):
    """형 지시 2026-10-18: Vetting 리마인드 주 2회, 수·금 08:00 KST."""
    def test_slots(self):
        import mail_reminder_service as mr
        from datetime import datetime as D
        mon = D(2026, 10, 19, 9, 30)                      # 월 09:30 최초 발송
        self.assertEqual(D(2026, 10, 21, 8, 0), mr.next_vetting_slot(mon))           # 수 08:00
        self.assertEqual(D(2026, 10, 23, 8, 0), mr.next_vetting_slot(D(2026, 10, 21, 8, 0, 5)))  # 금 08:00
        self.assertEqual(D(2026, 10, 28, 8, 0), mr.next_vetting_slot(D(2026, 10, 23, 8, 1)))     # 다음 수
        self.assertEqual(D(2026, 10, 21, 8, 0), mr.next_vetting_slot(D(2026, 10, 21, 7, 59)))
        self.assertEqual(D(2026, 10, 23, 8, 0), mr.next_vetting_slot(D(2026, 10, 21, 8, 0)))     # 정각 당일 제외
        self.assertEqual(2, mr.max_reminders('vetting'))
        self.assertEqual(1, mr.max_reminders('class'))
        run = {'sent_at': '2026-10-19 09:30:00', 'last_reminder_at': '2026-10-21 08:00:40'}
        self.assertEqual(D(2026, 10, 21, 8, 0), mr.reminder_due('vetting', run, 1))
        self.assertEqual(D(2026, 10, 23, 8, 0), mr.reminder_due('vetting', run, 2))
        self.assertEqual(D(2026, 10, 22, 8, 30), mr.reminder_due('class', run, 1))     # Class 기존 규칙 불변
        self.assertTrue(mr.in_vetting_window(D(2026, 10, 21, 8, 0)))
        self.assertTrue(mr.in_vetting_window(D(2026, 10, 23, 8, 59)))
        self.assertFalse(mr.in_vetting_window(D(2026, 10, 21, 9, 0)))     # 지연·재실행 → 다음 슬롯
        self.assertFalse(mr.in_vetting_window(D(2026, 10, 22, 8, 0)))     # 목요일
        self.assertFalse(mr.in_vetting_window(D(2026, 10, 21, 7, 59)))
        # 같은 주 안에서만: 월 발송 → 같은 주 수·금 OK, 다음 주 수요일 X
        self.assertTrue(mr.week_open(run, D(2026, 10, 23, 8, 0)))
        self.assertFalse(mr.week_open(run, D(2026, 10, 28, 8, 0)))
        self.assertFalse(mr.week_open({'sent_at': '2026-10-14 15:29:28'}, D(2026, 10, 21, 8, 0)))   # Indonesia run10: W42 → W43 미발송


if __name__ == '__main__':
    import unittest
    unittest.main()
