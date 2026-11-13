"""AI NEWS: ext 적재(API 키)·mini 모델 지정·점수 필터·중요 표시·핀·24시간 삭제·소유자 전용."""
import os
import tempfile
import unittest
from unittest import mock

import app as appmod
import routes_news
from app_core import execute, query


def _fake_ai(parts, model=None, openai_model=None):
    _fake_ai.models.append(openai_model)
    import re
    nums = [int(m) for m in re.findall(r'^(\d+)\. \[', parts[0]['text'], re.M)]
    scores = [9, 7, 3, 8, 6, 2]
    return {'items': [{'i': i, 'title_ko': f'제목{i}', 'summary_ko': '요약', 'reason_ko': '근거',
                       'score': scores[(i - 1) % len(scores)]} for i in nums]}


class NewsTests(unittest.TestCase):
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
            self.other = execute("INSERT INTO users(username, password_hash, role, active) VALUES('other','y','admin',1)")
        self.c = appmod.app.test_client()
        _fake_ai.models = []

    def tearDown(self):
        appmod.DATABASE, appmod.app.config['DATABASE'], appmod.app.config['TESTING'] = self.old
        self.tmp.cleanup()

    def _login(self, uid, username):
        with self.c.session_transaction() as s:
            s['user_id'] = uid
            s['username'] = username
            s['role'] = 'admin'

    def _items(self, n, prefix='a'):
        return [{'url': f'https://ex.com/{prefix}{i}', 'title': f'T{i}', 'content': 'c', 'source': 'reddit',
                 'meta': {'score': 100}} for i in range(n)]

    def _ingest(self, items, key='secret'):
        with mock.patch.object(routes_news, '_gemini_call_json', _fake_ai):
            return self.c.post('/api/ext/news', headers={'X-API-Key': key}, json={'items': items})

    def test_ingest_requires_key_filters_and_uses_mini(self):
        self.assertIn(self._ingest(self._items(2), key='bad').status_code, (401, 403))
        r = self._ingest(self._items(6) + [{'url': 'ftp://x', 'title': 'bad'}])
        self.assertEqual(200, r.status_code, r.get_json())
        d = r.get_json()
        self.assertEqual(4, d['stored'])                      # 9,7,8,6 통과 / 3,2 탈락
        self.assertTrue(d['ai_ok'])
        self.assertEqual(['gpt-5.4-mini'], _fake_ai.models)
        again = self._ingest(self._items(6)).get_json()      # 같은 URL 재적재 = 무시
        self.assertEqual(0, again['new'])

    def test_keep_max_15_per_batch(self):
        d = self._ingest(self._items(40, 'b')).get_json()
        self.assertEqual(15, d['stored'])

    def test_owner_only_list_pin_and_24h_purge(self):
        self._ingest(self._items(6))
        self._login(self.other, 'other')
        self.assertEqual(404, self.c.get('/api/news').status_code)
        self.assertEqual(404, self.c.get('/news').status_code)
        self._login(self.owner, 'SS0094')
        self.assertEqual(200, self.c.get('/news').status_code)
        rows = self.c.get('/api/news').get_json()['items']
        self.assertEqual([9, 8, 7, 6], [r['score'] for r in rows])
        self.assertEqual([True, True, False, False], [r['important'] for r in rows])
        pin_id, drop_id = rows[0]['id'], rows[1]['id']
        self.assertTrue(self.c.post(f'/api/news/{pin_id}/pin', json={'pinned': True}).get_json()['pinned'])
        with appmod.app.app_context():
            execute("UPDATE news_item SET created_at=datetime('now','localtime','-25 hours')")
        latest = self.c.get('/api/news').get_json()
        self.assertEqual([pin_id], [r['id'] for r in latest['items']])
        self.assertEqual(1, latest['pinned_total'])
        self.assertEqual(404, self.c.post(f'/api/news/{drop_id}/pin', json={'pinned': True}).status_code)
        self.c.post(f'/api/news/{pin_id}/pin', json={'pinned': False})
        with appmod.app.app_context():
            self.assertEqual(1, query("SELECT COUNT(*) c FROM news_item", one=True)['c'])
        self.assertEqual([], self.c.get('/api/news').get_json()['items'])

    def test_ai_failure_keeps_original_flagged(self):
        with mock.patch.object(routes_news, '_gemini_call_json', lambda *a, **k: {'error': 'API_CALL_FAILED'}):
            d = self.c.post('/api/ext/news', headers={'X-API-Key': 'secret'}, json={'items': self._items(8)}).get_json()
        self.assertFalse(d['ai_ok'])
        self.assertEqual(5, d['stored'])


    def test_ai_failure_then_retry_updates_and_partial_seen(self):
        with mock.patch.object(routes_news, '_gemini_call_json', lambda *a, **k: {'error': 'x'}):
            self.c.post('/api/ext/news', headers={'X-API-Key': 'secret'}, json={'items': self._items(3)})
        d = self._ingest(self._items(3)).get_json()          # 다음 회차 재채점
        self.assertEqual(3, d['new'])
        with appmod.app.app_context():
            rows = query("SELECT title_ko, ai_ok, score FROM news_item ORDER BY id")
            self.assertTrue(all(r['ai_ok'] == 1 and r['title_ko'].startswith('제목') for r in rows))

    def test_out_of_chunk_and_missing_indexes_ignored_not_seen(self):
        def partial(parts, model=None, openai_model=None):
            return {'items': [{'i': 1, 'title_ko': 'A', 'score': 9}, {'i': 1, 'title_ko': 'dup', 'score': 1},
                              {'i': 99, 'title_ko': 'evil', 'score': 10}]}
        with mock.patch.object(routes_news, '_gemini_call_json', partial):
            d = self.c.post('/api/ext/news', headers={'X-API-Key': 'secret'}, json={'items': self._items(3)}).get_json()
        self.assertEqual(1, d['scored'])
        with appmod.app.app_context():
            self.assertEqual(['A'], [r['title_ko'] for r in query("SELECT title_ko FROM news_item")])
            self.assertEqual(1, query("SELECT COUNT(*) c FROM news_seen", one=True)['c'])
        self.assertEqual(2, self._ingest(self._items(3)).get_json()['new'])   # 미채점 2건 재시도

    def test_rendered_page_escapes(self):
        self._ingest([{'url': 'javascript:alert(1)', 'title': 'x'}, {'url': 'https://ok.com/<b>', 'title': '<img src=x>'}])
        self._login(self.owner, 'SS0094')
        items = self.c.get('/api/news').get_json()['items']
        self.assertEqual(1, len(items))                       # javascript: URL 거부
        page = self.c.get('/news').get_data(as_text=True)
        self.assertIn('const esc =', page)


if __name__ == '__main__':
    unittest.main()
