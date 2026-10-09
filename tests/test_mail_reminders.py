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
        with patch.object(R, 'now_kst', return_value=datetime(2026,10,5,8,29)):
            self.assertEqual(409, self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':1}).status_code)
        with patch.object(R, 'now_kst', return_value=datetime(2026,10,5,8,30)):
            first = self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':1})
            self.assertEqual(200, first.status_code, first.get_json())
            self.assertEqual(200,self.post(f'/runs/{rid}/reminder',{'kind':'check','n':1,'token':first.get_json()['token']}).status_code)
            self.assertEqual(409, self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':1}).status_code)
            self.assertEqual(409, self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':2}).status_code)
            self.assertEqual(200, self.post(f'/runs/{rid}/reminder', {'kind':'result','n':1,'token':first.get_json()['token'],'state':'sent','sent_at':'2026-10-05 08:30:00'}).status_code)
            self.assertEqual(409, self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':2}).status_code)
        with self.app_context():
            execute("INSERT INTO class_mail_replies(run_id,message_id,sender,updated) VALUES(?,'receipt','captain',0)",(rid,))
        with patch.object(R, 'now_kst', return_value=datetime(2026,10,7,8,30)):
            second = self.post(f'/runs/{rid}/reminder', {'kind':'claim','n':2})
            self.assertEqual(200,second.status_code,second.get_json())
            with self.app_context():
                execute("INSERT INTO class_mail_replies(run_id,message_id,sender,updated) VALUES(?,'action','captain',1)",(rid,))
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'check','n':2,'token':second.get_json()['token']}).status_code)
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'notify'}).status_code)
            # Persist actual outcome after late reply, without requiring continued eligibility.
            self.assertEqual(200,self.post(f'/runs/{rid}/reminder',{'kind':'result','n':2,'token':second.get_json()['token'],'state':'failed'}).status_code)

    def test_interpretation_hold_not_treated_as_no_reply(self):
        rid=self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-10-01 18:00:00' WHERE id=?",(rid,))
            execute("INSERT INTO class_mail_replies(run_id,message_id,sender,updated,note) VALUES(?,'hold','captain',0,'Unmatched action needs review')",(rid,))
        with patch.object(R,'now_kst',return_value=datetime(2026,10,5,8,30)):
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':1}).status_code)

    def test_confirmed_two_sends_notify_once_and_disable_blocks(self):
        rid = self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-10-01 18:00:00' WHERE id=?", (rid,))
        for n, day in [(1,6),(2,7)]:
            with patch.object(R,'now_kst',return_value=datetime(2026,10,day,8,30)):
                claim = self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':n}).get_json()
                response = self.post(f'/runs/{rid}/reminder',{'kind':'result','n':n,'token':claim['token'],'state':'sent','sent_at':f'2026-10-{day:02d} 08:30:00'})
                self.assertEqual(200,response.status_code,response.get_json())
        with patch.object(R,'now_kst',return_value=datetime(2026,10,8,8,29)):
            self.assertEqual([],self.get('/reminders').get_json()['runs'])
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'notify'}).status_code)
        with patch.object(R,'now_kst',return_value=datetime(2026,10,8,8,30)):
            self.assertEqual(2,self.get('/reminders').get_json()['runs'][0]['reminder_count'])
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
        with patch.object(R,'now_kst',return_value=datetime(2026,10,5,8,30)):
            response = self.client.post(f'/api/ext/vetting-mail/runs/{rid}/reminder',headers=self.headers,json={'kind':'claim','n':1})
            self.assertEqual(200,response.status_code,response.get_json())
            self.assertEqual('captain@example.com',response.get_json()['run']['to_emails'])
            with self.app_context():
                execute("INSERT INTO vetting_mail_events(run_id,kind,message_id) VALUES(?,'reply','action')",(rid,))
            self.assertEqual(409,self.client.post(f'/api/ext/vetting-mail/runs/{rid}/reminder',headers=self.headers,json={'kind':'notify'}).status_code)

    def test_due_queue_and_ambiguous_claim_hold(self):
        rid = self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-10-01 18:00:00' WHERE id=?",(rid,))
        with patch.object(R,'now_kst',return_value=datetime(2026,10,5,8,29)):
            self.assertEqual([],self.get('/reminders').get_json()['runs'])
        with patch.object(R,'now_kst',return_value=datetime(2026,10,5,8,30)):
            self.assertEqual([rid],[r['id'] for r in self.get('/reminders').get_json()['runs']])
            claim=self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':1}).get_json()
            queue=self.get('/reminders').get_json()
            self.assertEqual([],queue['runs'])
            self.assertEqual('claimed',queue['blocked'][0]['state'])
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'result','n':1,'token':'bad','state':'unknown'}).status_code)
            self.assertEqual(200,self.post(f'/runs/{rid}/reminder',{'kind':'result','n':1,'token':claim['token'],'state':'unknown'}).status_code)
            self.assertEqual('unknown',self.get('/reminders').get_json()['blocked'][0]['state'])
            self.assertEqual(409,self.post(f'/runs/{rid}/reminder',{'kind':'claim','n':2}).status_code)

    def test_expired_invalid_timestamp_and_superseded_run(self):
        rid=self.sent_run()
        with self.app_context():
            execute("UPDATE class_mail_runs SET sent_at='2026-09-01 18:00:00' WHERE id=?",(rid,))
        with patch.object(R,'now_kst',return_value=datetime(2026,10,5,8,30)):
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

if __name__ == '__main__':
    import unittest
    unittest.main()
