import unittest
from unittest.mock import patch
import daily_mail_service
import class_mail_service

class FollowupTests(unittest.TestCase):
    def test_daily_partial_progress_counts_and_open_independent(self):
        S = daily_mail_service
        run={'id':1,'state':'sent','reply_status':'replied','issue_ids':[1,2]}
        events=[{'kind':'reply','payload':'{"sender":"engineer@example.test"}'},{'kind':'update'},{'kind':'close'}]
        with patch.object(S,'query',side_effect=[events,[{'status':'Open'},{'status':'Closed'}]]):
            row=S.daily_rows([run])[0]
        self.assertEqual('replied',row['reply_state']);self.assertEqual(1,row['open_count']);self.assertEqual(2,row['sent_count'])
        self.assertEqual('engineer@example.test',row['sender'])
        self.assertEqual((1,1),(row['update_count'],row['close_count']))

    def test_class_receipt_only_never_replied(self):
        S = class_mail_service
        replies=[{'updated':0,'note':'수신 확인만 — 결과/조치 회신 아님'}]
        with patch.object(S,'query',side_effect=[replies,{'snapshot':'[{}]'}]):
            row=S.class_rows([{'id':1,'vessel_id':1,'state':'sent'}])[0]
        self.assertEqual('pending',row['reply_state']);self.assertIsNone(row['last_reply_at']);self.assertEqual(0,row['review_count'])

    def test_class_latest_run_no_old_reply_leak_and_failed_not_pending(self):
        S = class_mail_service
        with patch.object(S,'query',side_effect=[[],{'snapshot':'[]'}]) as q:
            rows=S.class_rows([{'id':2,'vessel_id':1,'state':'failed'},{'id':1,'vessel_id':1,'state':'sent'}])
        self.assertEqual(1,len(rows));self.assertEqual('not_sent',rows[0]['reply_state']);self.assertEqual(2,q.call_count)

    def test_class_updates_then_receipt_keeps_substantive_sender(self):
        S = class_mail_service
        replies=[{'updated':1,'note':'','sender':'action@example.test','received_at':'2026-10-08','created_at':''}, {'updated':0,'note':'수신 확인만 — 결과/조치 회신 아님'}]
        with patch.object(S,'query',side_effect=[replies,{'snapshot':'[{},{}]'}]):
            row=S.class_rows([{'id':1,'vessel_id':1,'state':'sent'}])[0]
        self.assertEqual('replied',row['reply_state']);self.assertEqual('action@example.test',row['sender']);self.assertEqual(1,row['update_count'])

    def test_real_sql_event_columns_and_receipt_scope(self):
        S = daily_mail_service
        import sqlite3
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.executescript("CREATE TABLE daily_mail_events(id INTEGER,run_id INTEGER,kind TEXT,message_id TEXT,payload TEXT,evidence TEXT,created_at TEXT); CREATE TABLE issues(id INTEGER,status TEXT); INSERT INTO issues VALUES(1,'Closed'); INSERT INTO daily_mail_events VALUES(1,3,'reply','m','{}','sender@example.test · subject','2026-10-08');")
        def query(sql,args=(),one=False):
            cursor=db.execute(sql,args)
            return cursor.fetchone() if one else cursor.fetchall()
        with patch.object(S,'query',side_effect=query):
            row=S.daily_rows([{'id':3,'state':'sent','reply_status':'replied','issue_ids':[1],'last_reply_at':'2026-10-08'}])[0]
        self.assertEqual('sender@example.test',row['sender']);self.assertEqual('2026-10-08',row['last_reply_at']);self.assertEqual(0,row['open_count'])
        db.close()
