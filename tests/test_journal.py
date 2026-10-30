"""개인 마음 기록: 소유자 Bearer 만 열람, 다른 계정·웹 쿠키는 404, ext 적재는 API 키."""
import os
import tempfile
import unittest

import app as appmod
from app_core import execute


class JournalTests(unittest.TestCase):
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
            self.member = execute("INSERT INTO users(username, password_hash, role, active) VALUES('CJLEE','z','member',1)")
            self.other = execute("INSERT INTO users(username, password_hash, role, active, app_scope) VALUES('hyezzi','y','member',1,'family')")
        self.c = appmod.app.test_client()

    def tearDown(self):
        appmod.DATABASE, appmod.app.config['DATABASE'], appmod.app.config['TESTING'] = self.old
        self.tmp.cleanup()

    def _bearer(self, uid, pw):
        with appmod.app.app_context():
            from token_auth import _issue_token
            return {'Authorization': 'Bearer ' + _issue_token({'id': uid, 'password_hash': pw})}

    def test_put_and_owner_read(self):
        r = self.c.put('/api/ext/journal', headers={'X-API-Key': 'secret'},
                       json={'entries': [{'entry_date': '2026-10-30', 'kind': 'day', 'title': '법원 일정', 'body': '본문'}]})
        self.assertEqual(200, r.status_code, r.get_json())
        self.c.put('/api/ext/journal', headers={'X-API-Key': 'secret'},
                   json={'entries': [{'entry_date': '2026-10-30', 'kind': 'day', 'title': '법원 일정', 'body': '수정'}]})
        lst = self.c.get('/api/journal', headers=self._bearer(self.owner, 'x'))
        self.assertEqual(200, lst.status_code)
        es = lst.get_json()['entries']
        self.assertEqual(1, len(es))
        one = self.c.get(f"/api/journal/{es[0]['id']}", headers=self._bearer(self.owner, 'x')).get_json()
        self.assertEqual('수정', one['body'])
        self.assertIn('no-store', lst.headers.get('Cache-Control', ''))
        self.assertIsNone(lst.headers.get('ETag'))

    def test_other_account_and_cookie_hidden(self):
        # 가족 자산 계정은 family scope 가드가 먼저 403, 그 외 계정은 404 — 어느 쪽이든 내용 노출 없음
        self.assertIn(self.c.get('/api/journal', headers=self._bearer(self.other, 'y')).status_code, (403, 404))
        r = self.c.get('/api/journal', headers=self._bearer(self.member, 'z'))
        self.assertEqual(404, r.status_code)
        self.assertEqual(404, self.c.get('/api/journal/1', headers=self._bearer(self.member, 'z')).status_code)
        self.assertEqual(404, self.c.get('/api/journal/1').status_code)
        with self.c.session_transaction() as s:
            s['user_id'] = self.owner; s['username'] = 'SS0094'; s['role'] = 'admin'
        self.assertEqual(404, self.c.get('/api/journal').status_code)   # 웹 쿠키 세션 거부

    def test_ext_requires_key_and_validates(self):
        self.assertEqual(401, self.c.put('/api/ext/journal', json={'entries': []}).status_code)
        bad = self.c.put('/api/ext/journal', headers={'X-API-Key': 'secret'},
                         json={'entries': [{'entry_date': '10/30', 'title': 't', 'body': 'b'}]})
        self.assertEqual(400, bad.status_code)


if __name__ == '__main__':
    unittest.main()
