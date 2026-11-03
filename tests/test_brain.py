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

    def test_anonymous_redirected(self):
        self.assertIn(self.c.get('/api/brain/graph').status_code, (302, 401))


if __name__ == '__main__':
    unittest.main()
