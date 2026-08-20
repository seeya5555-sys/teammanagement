"""오프라인 보관함 재전송 중복방지(X-Idempotency-Key) 단위테스트.

여기서 지키는 것 = **재전송이 두 번 만들지 않는다**, 그리고 **모르면 자동 재실행하지 않는다**.
오프라인에서 보관한 쓰기는 연결 복구 직후 링크가 불안정할 때 재전송되므로,
"서버는 저장했는데 응답만 못 받은" 경우가 실제로 발생한다.

⚠️ 하네스 주의(기존 교훈): 모듈 전역에서 `app_context().push()` 하면 `g` 가 요청 사이에 남아
   거짓 통과/실패를 만든다. 준비만 `with app.app_context():` 로 하고 요청은 context 없이 쏜다.

실행: python -m unittest tests.test_client_idem -v   (repo 루트에서)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as appmod

# 이 파일은 `client.session_transaction()` 으로 로그인해서 CSRF 토큰을 가진
# 적이 없다. TESTING 을 세우지 않는 파일이라 csrf.enforce 의 기본값(=켜짐)에
# 걸리므로, 여기서 명시적으로 끈다. 검사 자체는 tests/test_csrf.py 가 본다.
appmod.app.config['CSRF_PROTECT'] = False
from source_bundle import shared_ns  # noqa: E402


# 500 경로(뷰가 예외로 죽는 상황)를 실제로 태우기 위한 테스트 전용 라우트.
# 첫 요청 전(=모듈 import 시점)에만 등록 가능하므로 실패하면 해당 테스트만 skip 한다.
_BOOM_PATH = '/api/_test_idem_boom'
_BOOM_OK = True
try:
    @appmod.app.route(_BOOM_PATH, methods=['POST'])
    @shared_ns.login_required
    def _test_idem_boom():                       # pragma: no cover - 예외 유발 전용
        appmod.execute("INSERT INTO calendar_events (title,start_date,all_day) VALUES ('부분커밋','2026-01-01',1)")
        raise RuntimeError('boom')
except Exception:                                # 다른 테스트와 한 프로세스로 돌면 등록 불가
    _BOOM_OK = False


class ClientIdemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            appmod._auto_migrate()
            # 스코프 테스트가 쓰는 2번 계정 — `login_required` 가 요청마다 users 를
            # 재확인하므로 실재하지 않는 uid 로는 401 이 되어 스코프 자체를 못 잰다.
            appmod.execute(
                "INSERT OR IGNORE INTO users (id,username,password_hash,display_name,role,active) "
                "VALUES (2,'idem-user2','x','Idem User2','member',1)")
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    # ---- helpers ----
    def _create(self, key=None, title='회의', path='/api/cal/events'):
        h = {'X-Idempotency-Key': key} if key else {}
        return self.client.post(path, json={'title': title, 'start_date': '2026-08-10'}, headers=h)

    def _events(self, title='회의'):
        with appmod.app.app_context():
            return appmod.query('SELECT id FROM calendar_events WHERE title=?', (title,))

    def _idem_rows(self):
        with appmod.app.app_context():
            return appmod.query('SELECT * FROM client_idem')

    # ---- 핵심: 재전송이 두 번 만들지 않는다 ----
    def test_replay_returns_same_body_and_creates_once(self):
        r1 = self._create(key='off-abc-0001')
        self.assertEqual(201, r1.status_code, r1.get_data(as_text=True))
        r2 = self._create(key='off-abc-0001')
        self.assertEqual(r1.status_code, r2.status_code)
        self.assertEqual(r1.get_json(), r2.get_json())        # 같은 id 를 그대로 돌려줌
        self.assertEqual('1', r2.headers.get('X-Idempotent-Replay'))
        self.assertEqual(1, len(self._events()))              # 🔴 한 번만 생성

    def test_no_header_is_unchanged_behaviour(self):
        self._create()
        self._create()
        self.assertEqual(2, len(self._events()))              # 멱등키 없으면 예전 그대로 2건
        self.assertEqual(0, len(self._idem_rows()))           # 원장도 안 남김

    # ---- 키 오용 ----
    def test_bad_key_is_rejected_not_silently_ignored(self):
        r = self._create(key='short')                         # 8자 미만
        self.assertEqual(400, r.status_code)
        self.assertEqual('bad_idempotency_key', r.get_json()['error'])
        self.assertEqual(0, len(self._events()))              # 실행 자체가 안 됨

    def test_same_key_on_different_path_is_conflict(self):
        self._create(key='off-abc-0002')
        r = self.client.put('/api/cal/events/1', json={'title': 'x', 'start_date': '2026-08-10'},
                            headers={'X-Idempotency-Key': 'off-abc-0002'})
        self.assertEqual(409, r.status_code)
        self.assertEqual('idempotency_key_reused', r.get_json()['error'])

    # ---- 실패 처리: 4xx 와 5xx 를 구분한다 ----
    def test_client_error_frees_the_key_for_retry(self):
        bad = self.client.post('/api/cal/events', json={'start_date': '2026-08-10'},
                               headers={'X-Idempotency-Key': 'off-abc-0003'})
        self.assertEqual(400, bad.status_code)
        self.assertEqual(0, len(self._idem_rows()))           # claim 삭제 = 고쳐서 재전송 가능
        ok = self._create(key='off-abc-0003')
        self.assertEqual(201, ok.status_code)
        self.assertEqual(1, len(self._events()))

    @unittest.skipUnless(_BOOM_OK, '테스트 라우트 등록 불가(첫 요청 이후 import)')
    def test_server_error_becomes_unknown_and_never_reruns(self):
        r = self.client.post(_BOOM_PATH, json={}, headers={'X-Idempotency-Key': 'off-abc-0004'})
        self.assertEqual(500, r.status_code)
        rows = self._idem_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual('unknown', rows[0]['status'])        # 🔴 삭제가 아니라 unknown
        before = len(self._events('부분커밋'))
        again = self.client.post(_BOOM_PATH, json={}, headers={'X-Idempotency-Key': 'off-abc-0004'})
        self.assertEqual(409, again.status_code)
        self.assertEqual('idem_unknown', again.get_json()['error'])
        self.assertEqual(before, len(self._events('부분커밋')))  # 뷰가 다시 돌지 않음

    def test_unknown_row_blocks_execution(self):
        with appmod.app.app_context():
            appmod.execute("INSERT INTO client_idem (user_id,idem_key,method,path,status) "
                           "VALUES (1,'off-abc-0005','POST','/api/cal/events','unknown')")
        r = self._create(key='off-abc-0005')
        self.assertEqual(409, r.status_code)
        self.assertEqual('idem_unknown', r.get_json()['error'])
        self.assertEqual(0, len(self._events()))

    def test_in_progress_row_returns_conflict(self):
        with appmod.app.app_context():
            appmod.execute("INSERT INTO client_idem (user_id,idem_key,method,path,status) "
                           "VALUES (1,'off-abc-0006','POST','/api/cal/events','in_progress')")
        r = self._create(key='off-abc-0006')
        self.assertEqual(409, r.status_code)
        self.assertEqual('in_progress', r.get_json()['error'])
        self.assertEqual(0, len(self._events()))

    # ---- 스코프 ----
    def test_key_is_scoped_per_user(self):
        self._create(key='off-abc-0007')
        with self.client.session_transaction() as s:
            s['user_id'] = 2
        r = self._create(key='off-abc-0007', title='다른사람')
        self.assertEqual(201, r.status_code)
        self.assertIsNone(r.headers.get('X-Idempotent-Replay'))   # 남의 응답을 받지 않음
        self.assertEqual(1, len(self._events('다른사람')))

    def test_get_is_untouched(self):
        r = self.client.get('/api/cal/events?start=2026-08-01&end=2026-08-31',
                            headers={'X-Idempotency-Key': 'off-abc-0008'})
        self.assertEqual(200, r.status_code)
        self.assertEqual(0, len(self._idem_rows()))

    def test_ext_api_paths_are_excluded(self):
        """/api/ext/ 는 워커·api_key 경로 — 앱 보관함과 무관하므로 원장에 남지 않는다."""
        self.client.post('/api/ext/automation/nope/progress', json={'progress': 'x'},
                         headers={'X-Idempotency-Key': 'off-abc-0009'})
        self.assertEqual(0, len(self._idem_rows()))

    # ---- 마이그레이션 ----
    def test_auto_migrate_recreates_table_on_legacy_db(self):
        import sqlite3
        conn = sqlite3.connect(appmod.DATABASE)
        try:
            conn.execute('DROP TABLE client_idem')
            conn.commit()
        finally:
            conn.close()
        appmod._auto_migrate()
        with appmod.app.app_context():
            cols = {r[1] for r in appmod.get_db().execute('PRAGMA table_info(client_idem)')}
        self.assertIn('idem_key', cols)
        self.assertEqual(201, self._create(key='off-abc-0010').status_code)


if __name__ == '__main__':
    unittest.main(verbosity=2)
