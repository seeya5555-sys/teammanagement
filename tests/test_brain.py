"""브레인: 소유자만 열람, 다른 계정 404, ext 적재는 API 키, 마음 기록은 제목만(본문 비노출)."""
import os
import tempfile
import unittest

import app as appmod
from app_core import execute


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (appmod.DATABASE, appmod.app.config['DATABASE'], appmod.app.config.get('TESTING'))
        db = os.path.join(self.tmp.name, 't.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        with appmod.app.app_context():
            appmod.init_db(False)
            execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)", ('secret',))
            self.owner = execute("INSERT INTO users(username, password_hash, role, active) VALUES('SS0094','x','admin',1)")
            self.other = execute("INSERT INTO users(username, password_hash, role, active, app_scope) VALUES('hyezzi','y','member',1,'family')")
            execute("INSERT INTO personal_journal(entry_date, kind, title, body) VALUES('2026-10-30','day','법원 일정','비밀본문')")
        self.c = appmod.app.test_client()

    def tearDown(self):
        appmod.DATABASE, appmod.app.config['DATABASE'], appmod.app.config['TESTING'] = self.old
        self.tmp.cleanup()

    def _login(self, uid, username):
        with self.c.session_transaction() as s:
            s['user_id'] = uid
            s['username'] = username
            s['role'] = 'admin'

    def _put(self, payload, key='secret'):
        return self.c.put('/api/ext/brain', headers={'X-API-Key': key}, json=payload)

    def test_put_requires_key_and_validates(self):
        ok = {'nodes': [{'id': 'a', 'title': 'A', 'cat': 'project'}], 'links': []}
        self.assertIn(self._put(ok, key='bad').status_code, (401, 403))
        self.assertEqual(400, self._put({'nodes': [], 'links': []}).status_code)
        self.assertEqual(400, self._put({'nodes': [{'id': 'personal:1', 'title': 'x', 'cat': 'project'}], 'links': []}).status_code)
        self.assertEqual(400, self._put({'nodes': [{'id': 'a', 'title': 'A', 'cat': 'personal'}], 'links': []}).status_code)

    def test_owner_reads_graph_with_journal_titles_only(self):
        r = self._put({'nodes': [{'id': 'a', 'title': 'A', 'cat': 'project', 'body': 'b', 'evil': 1},
                                 {'id': 'b', 'title': 'B', 'cat': 'area'}],
                       'links': [{'source': 'a', 'target': 'b'}, {'source': 'a', 'target': 'ghost'}]})
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertEqual(1, r.get_json()['links'])
        self._login(self.owner, 'SS0094')
        g = self.c.get('/api/brain/graph')
        self.assertEqual(200, g.status_code)
        d = g.get_json()
        ids = {n['id'] for n in d['nodes']}
        self.assertTrue({'a', 'b', 'personal:hub'} <= ids)
        self.assertNotIn('evil', next(n for n in d['nodes'] if n['id'] == 'a'))
        self.assertNotIn('비밀본문', g.get_data().decode('unicode_escape'))
        self.assertTrue(all('body' not in n for n in d['nodes'] if n['cat'] == 'personal'))
        self.assertIn('법원 일정', {n['title'] for n in d['nodes']})
        self.assertIn('no-store', g.headers.get('Cache-Control', ''))
        self.assertEqual(200, self.c.get('/brain').status_code)

    def test_other_account_gets_404(self):
        self._login(self.other, 'hyezzi')
        self.assertIn(self.c.get('/api/brain/graph').status_code, (403, 404))
        self.assertIn(self.c.get('/brain').status_code, (302, 403, 404))

    def _bearer(self, uid, pw):
        with appmod.app.app_context():
            from token_auth import _issue_token
            return {'Authorization': 'Bearer ' + _issue_token({'id': uid, 'password_hash': pw})}

    def test_bearer_owner_ok_other_blocked(self):
        self._put({'nodes': [{'id': 'a', 'title': 'A', 'cat': 'project'}], 'links': []})
        self.assertEqual(200, self.c.get('/api/brain/graph', headers=self._bearer(self.owner, 'x')).status_code)
        c2 = appmod.app.test_client()
        self.assertIn(c2.get('/api/brain/graph', headers=self._bearer(self.other, 'y')).status_code, (403, 404))

    def test_malformed_rejected_and_previous_kept(self):
        self._put({'nodes': [{'id': 'a', 'title': 'A', 'cat': 'project'}], 'links': []})
        for bad in ([1], None, {'nodes': [{'id': 'a', 'cat': ['x']}], 'links': []},
                    {'nodes': [{'id': 'a', 'cat': 'area'}, {'id': 'a', 'cat': 'area'}], 'links': []}):
            self.assertEqual(400, self._put(bad).status_code, bad)
        r = self._put({'nodes': [{'id': 'b', 'title': 5, 'cat': 'area'}], 'links': [{'source': ['b'], 'target': 'b'}]})
        self.assertEqual(200, r.status_code)
        self._login(self.owner, 'SS0094')
        n = self.c.get('/api/brain/graph').get_json()['nodes'][0]
        self.assertEqual('5', n['title'])

    def test_deku_layers_and_personal_hub_link(self):
        r = self._put({'nodes': [{'id': 'deku', 'title': '데쿠', 'cat': 'hub'},
                                 {'id': 'auto:x', 'title': 'x', 'cat': 'automation', 'status': 'fail'},
                                 {'id': 'day:2026-11-04', 'title': 'd', 'cat': 'update'}],
                       'links': [{'source': 'deku', 'target': 'auto:x'}]})
        self.assertEqual(200, r.status_code, r.get_json())
        self._login(self.owner, 'SS0094')
        d = self.c.get('/api/brain/graph').get_json()
        self.assertEqual('fail', next(n for n in d['nodes'] if n['id'] == 'auto:x')['status'])
        self.assertIn({'source': 'deku', 'target': 'personal:hub'}, d['links'])

    def test_trmt_vessels_and_open_issues(self):
        with appmod.app.app_context():
            v = execute("INSERT INTO vessels(name, short_name) VALUES('BELGIUM BRIDGE','BELGIUM B')")
            sup = execute("INSERT INTO supervisors(name) VALUES('t')")
            execute("INSERT INTO issues(supervisor_id, vessel_id, issue_date, due_date, item_topic, status) "
                    "VALUES(?,?,'2026-10-01','2020-01-01','BWTS 클레임','Open')", (sup, v))
            execute("INSERT INTO issues(supervisor_id, vessel_id, issue_date, item_topic, status) "
                    "VALUES(?,?,'2026-10-01','끝난 건','Closed')", (sup, v))
        self._put({'nodes': [{'id': 'deku', 'title': '데쿠', 'cat': 'hub'},
                             {'id': 'note', 'title': 'n', 'cat': 'project', 'body': 'belgium b dock'}], 'links': []})
        self._login(self.owner, 'SS0094')
        d = self.c.get('/api/brain/graph').get_json()
        iss = [n for n in d['nodes'] if n['cat'] == 'issue']
        self.assertEqual(['BWTS 클레임'], [n['title'] for n in iss])
        self.assertEqual(['끝난 건'], [n['title'] for n in d['nodes'] if n['cat'] == 'closed'])
        self.assertEqual('fail', iss[0]['status'])
        self.assertIn({'source': f'vessel:{v}', 'target': 'note'}, d['links'])

    def test_mail_nodes_titles_only(self):
        with appmod.app.app_context():
            v = execute("INSERT INTO vessels(name) VALUES('GHANA PROSPERITY')")
            execute("INSERT INTO class_mail_runs(vessel_id, iso_week, state, tag, subject, body, to_emails, cc_emails, "
                    "snapshot, reply_rows, excel_sha256, sent_at) VALUES(?, '2026W45', 'sent', 't', 'Class 상태 요청', "
                    "'비밀메일본문', 'a@x.com', '', '{}', '[]', 'h', datetime('now','localtime'))", (v,))
        self._put({'nodes': [{'id': 'deku', 'title': '데쿠', 'cat': 'hub'}], 'links': []})
        self._login(self.owner, 'SS0094')
        g = self.c.get('/api/brain/graph')
        d = g.get_json()
        m = [n for n in d['nodes'] if n['cat'] == 'mail']
        self.assertEqual(['Class 상태 요청'], [n['title'] for n in m])
        self.assertEqual('disabled', m[0]['status'])
        self.assertNotIn('비밀메일본문', g.get_data().decode('unicode_escape'))
        self.assertIn({'source': f'vessel:{v}', 'target': m[0]['id']}, d['links'])

    def test_mobile_entry_bearer_only_owner(self):
        r = self.c.get('/api/brain/mobile-entry', headers=self._bearer(self.owner, 'x'))
        self.assertEqual(302, r.status_code)
        self.assertTrue(r.headers['Location'].endswith('/brain?embed=1'))
        pg = self.c.get('/brain?embed=1')
        self.assertEqual(200, pg.status_code)
        self.assertIn('class=" embedded"', pg.get_data(as_text=True))
        # stale 소유자 쿠키 + 다른 계정 Bearer → 세션이 다른 계정으로 교체돼 거부
        self.assertIn(self.c.get('/api/brain/mobile-entry', headers=self._bearer(self.other, 'y')).status_code, (403, 404))
        self.assertIn(self.c.get('/api/brain/graph').status_code, (403, 404))
        c2 = appmod.app.test_client()
        self.assertIn(c2.get('/api/brain/mobile-entry', headers=self._bearer(self.other, 'y')).status_code, (403, 404))
        c3 = appmod.app.test_client()
        with c3.session_transaction() as s:
            s['user_id'] = self.owner; s['username'] = 'SS0094'; s['role'] = 'admin'
        self.assertEqual(401, c3.get('/api/brain/mobile-entry').status_code)

    def test_ext_graph_requires_key(self):
        with appmod.app.app_context():
            v = execute("INSERT INTO vessels(name) VALUES('GHANA PROSPERITY')")
            execute("INSERT INTO class_mail_runs(vessel_id, iso_week, state, tag, subject, body, to_emails, cc_emails, "
                    "snapshot, reply_rows, excel_sha256, sent_at) VALUES(?, '2026W45', 'sent', 't', 'S', "
                    "'메일본문비밀', 'a@x.com', '', '{}', '[]', 'h', datetime('now','localtime'))", (v,))
            sup = execute("INSERT INTO supervisors(name) VALUES('t2')")
            acts = '[' + ','.join('{"date":"2026-01-%02d","progress":"p%d"}' % (i % 28 + 1, i) for i in range(30)) + ']'
            execute("INSERT INTO issues(supervisor_id, vessel_id, issue_date, item_topic, status, actions) "
                    "VALUES(?,?,'2026-01-01','옛 건','Closed',?)", (sup, v, acts))
        self._put({'nodes': [{'id': 'deku', 'title': '데쿠', 'cat': 'hub'}], 'links': []})
        self.assertIn(self.c.get('/api/ext/brain/graph', headers={'X-API-Key': 'bad'}).status_code, (401, 403))
        r = self.c.get('/api/ext/brain/graph', headers={'X-API-Key': 'secret'})
        self.assertEqual(200, r.status_code)
        self.assertNotIn('비밀본문', r.get_data().decode('unicode_escape'))
        raw = r.get_data().decode('unicode_escape')
        self.assertNotIn('메일본문비밀', raw)
        d = r.get_json()
        self.assertIn('법원 일정', {n['title'] for n in d['nodes']})          # 마음기록 제목은 있음
        old = next(n for n in d['nodes'] if n['cat'] == 'closed')
        self.assertIn('p29', old['body']); self.assertIn('p0', old['body'])  # 진행 이력 전체

    def test_anonymous_redirected(self):
        self.assertIn(self.c.get('/api/brain/graph').status_code, (302, 401))


if __name__ == '__main__':
    unittest.main()
