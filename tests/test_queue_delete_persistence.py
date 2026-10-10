"""Explicit user deletion permanently suppresses the same SVMS queue identity."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as appmod
import routes_calendar_dock as routes
from source_bundle import shared_ns


class QueueDeletePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = appmod.app.config['DATABASE']
        self.old_testing = appmod.app.config.get('TESTING')
        appmod.app.config['TESTING'] = True
        self.old_constant = appmod.DATABASE
        appmod.DATABASE = os.path.join(self.tmp.name, 'db.sqlite')
        appmod.app.config['DATABASE'] = appmod.DATABASE
        with appmod.app.app_context():
            appmod.init_db()
            shared_ns._ensure_api_table()
            shared_ns.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','test-secret')")
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            uid, username = self.sql("SELECT id,username FROM users WHERE active=1 AND role='admin' LIMIT 1")[0]
            s.update(user_id=uid, username=username, role='admin')
        self.headers = {'X-API-Key': 'test-secret'}
        self.patches = [mock.patch.object(routes, name) for name in ('_aor_pdf_delete', '_fundreq_att_delete')]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        appmod.app.config['DATABASE'] = self.old
        appmod.app.config['TESTING'] = self.old_testing
        appmod.DATABASE = self.old_constant
        self.tmp.cleanup()

    def post(self, kind, code='DOC-1', **extra):
        key = 'aor_cd' if kind == 'aor' else 'opex_cd'
        return self.client.post('/api/ext/'+kind+'/drafts', headers=self.headers,
                                json={key: code, 'subj': 'synthetic', 'amt': 10, **extra})

    def sql(self, sql, args=()):
        with sqlite3.connect(appmod.app.config['DATABASE']) as db:
            return db.execute(sql, args).fetchall()

    def test_single_delete_blocks_new_content_and_new_case_key(self):
        for kind in ('aor', 'fundreq'):
            with self.subTest(kind=kind):
                made = self.post(kind).get_json(); did = made['id']
                self.assertEqual(200, self.client.delete(f'/api/{kind}/drafts/{did}').status_code)
                for payload in ({}, {'subj': 'changed', 'amt': 999}, {'auto_submitted': True}):
                    response = self.post(kind, ' doc-1 ', **payload)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual('deleted', response.get_json()['status'])
                    self.assertNotIn('id', response.get_json())
                self.assertEqual([], self.sql(f'SELECT id FROM {kind}_draft'))
                self.assertEqual(201, self.post(kind, 'DOC-2').status_code)
        self.assertEqual(2, len(self.sql('SELECT * FROM draft_queue_deleted')))

    def test_aor_bulk_and_both_decided_delete_record_only_removed_rows(self):
        ids = [self.post('aor', 'BULK-'+str(i)).get_json()['id'] for i in range(2)]
        self.sql("UPDATE aor_draft SET status='approved' WHERE id=?", (ids[1],))
        r = self.client.post('/api/aor/drafts/bulk-delete', json={'ids': ids})
        self.assertEqual(1, r.get_json()['deleted'])
        self.assertEqual('deleted', self.post('aor', 'BULK-0').get_json()['status'])
        self.assertEqual('approved', self.post('aor', 'BULK-1').get_json()['status'])
        for kind in ('aor', 'fundreq'):
            did = self.post(kind, 'DECIDED').get_json()['id']
            self.sql(f"UPDATE {kind}_draft SET status='failed' WHERE id=?", (did,))
            r = self.client.delete(f'/api/{kind}/drafts/decided')
            self.assertEqual(1, r.get_json()['deleted'])
            self.assertEqual('deleted', self.post(kind, 'DECIDED').get_json()['status'])

    def test_protected_delete_and_not_found_never_create_tombstone(self):
        for kind in ('aor', 'fundreq'):
            did = self.post(kind).get_json()['id']
            self.sql(f"UPDATE {kind}_draft SET status='submitting' WHERE id=?", (did,))
            self.assertEqual(409, self.client.delete(f'/api/{kind}/drafts/{did}').status_code)
            self.assertEqual(404, self.client.delete(f'/api/{kind}/drafts/9999').status_code)
        self.assertEqual([], self.sql('SELECT * FROM draft_queue_deleted'))

    def test_db_guard_stops_ingest_race_and_record_survives_restart(self):
        for kind in ('aor', 'fundreq'):
            did = self.post(kind).get_json()['id']
            self.client.delete(f'/api/{kind}/drafts/{did}')
            key = 'aor_cd' if kind == 'aor' else 'opex_cd'
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'draft document deleted'):
                self.sql(f'INSERT INTO {kind}_draft({key}) VALUES(?)', (' doc-1 ',))
        with appmod.app.app_context(): appmod.init_db()
        self.assertEqual(2, len(self.sql('SELECT * FROM draft_queue_deleted')))
        self.assertEqual('deleted', self.post('aor').get_json()['status'])

    def test_stale_ingest_lookup_race_returns_deleted_not_error(self):
        original = routes._draft_deleted_response
        for kind in ('aor', 'fundreq'):
            did = self.post(kind).get_json()['id']
            self.client.delete(f'/api/{kind}/drafts/{did}')
            calls = []
            def stale_once(k, code):
                calls.append(k)
                return None if len(calls) == 1 else original(k, code)
            with mock.patch.object(routes, '_draft_deleted_response', side_effect=stale_once):
                result = self.post(kind)
            self.assertEqual(200, result.status_code)
            self.assertEqual('deleted', result.get_json()['status'])
            self.assertEqual(2, len(calls))
            self.assertEqual([], self.sql(f'SELECT id FROM {kind}_draft'))

    def test_document_kind_isolation_and_non_ui_purge_not_suppressed(self):
        did = self.post('aor').get_json()['id']
        self.client.delete(f'/api/aor/drafts/{did}')
        self.assertEqual(201, self.post('fundreq').status_code)
        other = self.post('aor', 'PURGE').get_json()['id']
        self.sql('DELETE FROM aor_draft WHERE id=?', (other,))
        self.assertEqual(201, self.post('aor', 'PURGE').status_code)

    def test_transaction_failure_rolls_back_deletion_record(self):
        did = self.post('aor').get_json()['id']
        self.sql("CREATE TRIGGER stop_delete BEFORE DELETE ON aor_draft BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with appmod.app.test_request_context():
            with self.assertRaises(sqlite3.IntegrityError):
                shared_ns._delete_queue_documents('aor', 'id=?', (did,))
        self.assertEqual([], self.sql('SELECT * FROM draft_queue_deleted'))
        self.assertEqual(1, len(self.sql('SELECT * FROM aor_draft')))

    def test_unauthorized_delete_is_rejected(self):
        did = self.post('aor').get_json()['id']
        with self.client.session_transaction() as s: s.clear()
        self.assertEqual(401, self.client.delete(f'/api/aor/drafts/{did}').status_code)
        self.assertEqual([], self.sql('SELECT * FROM draft_queue_deleted'))

if __name__ == '__main__': unittest.main()
