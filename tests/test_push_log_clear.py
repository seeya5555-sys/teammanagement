#!/usr/bin/env python3
"""발송기록 감추기(`POST /api/ios/push/log/clear`) 계약 테스트.

형 지시(2026-08-06): "로그 섹션 접기 기능 + 로그 삭제 버튼(아이콘만) 추가해줘".

핵심 계약(여기가 깨지면 알림이 두 번 간다):
  · 🔴 **행을 지우지 않는다.** `push_log.event_key` 는 화면 이력이자 **중복발송 차단 claim**.
        하드 DELETE 하면 지운 직후 같은 이벤트가 다시 발송된다(캘린더 슬롯 재발송·outbox 재시도).
        그래서 `hidden_at` 만 찍는다 → **지운 뒤에도 같은 event_key 는 `already_sent`**.
  · 발송기록엔 계정 컬럼이 없다(전사 공용) → 지우면 남의 화면에서도 사라지므로 **admin 만**.
  · 화면은 서버가 내려주는 `can_clear` 만 보고 버튼을 그린다(role 자체판정 금지).
  · `hidden_at` 은 **기존 테이블에 ALTER 로 붙는다** — schema.sql 만 고치면 라이브가
        "no such column" 으로 죽는다(`CREATE TABLE IF NOT EXISTS` 재적용은 컬럼을 안 붙임).

실행: ~/.venvs/trmt-test/bin/python tests/test_push_log_clear.py
"""
import os, sys, tempfile

os.chdir(os.path.expanduser('~/projects/teammanagement'))
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A

A.DATABASE = DB
A.app.config['DATABASE'] = DB
A.app.config['TESTING'] = True
A.init_db(drop=False)
A._auto_migrate()

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


A.app.app_context().push()
c = A.app.test_client()

A.execute("INSERT OR IGNORE INTO supervisors (id, name) VALUES (1,'감독A')")


def mkuser(username, role):
    A.execute("INSERT INTO users (username, password_hash, display_name, supervisor_id, "
              "role, active) VALUES (?,'x',?,1,?,1)", (username, username, role))
    return A.query("SELECT id FROM users WHERE username=?", (username,), one=True)['id']


U_ADMIN = mkuser('admin1', 'admin')
U_MEM = mkuser('member1', 'member')


def login(uid, role):
    with c.session_transaction() as s:
        s['user_id'] = uid
        s['role'] = role


def mklog(event_key, title='알림'):
    A.execute("INSERT INTO push_log (event_key, kind, title, body, sent_n) "
              "VALUES (?, 'test', ?, 'b', 1)", (event_key, title))


print('# 1) 마이그레이션 — 기존 테이블에 hidden_at 이 ALTER 로 붙는가')
# CREATE TABLE IF NOT EXISTS 재적용으로는 컬럼이 안 붙는다. 컬럼을 떼고 _auto_migrate 를
# 다시 돌려 **구버전 DB 에서 올라오는 실제 경로**를 재현한다.
A.execute("DROP INDEX IF EXISTS idx_push_log_visible")   # 구버전엔 index 도 없다
A.execute("ALTER TABLE push_log DROP COLUMN hidden_at")
# push_log 뒤에 정의된 테이블. 이게 재생성돼야 "schema 재적용이 중간에 안 끊겼다" 는 증거가 된다.
A.execute("DROP TABLE IF EXISTS push_outbox")
cols = [r[1] for r in A.get_db().execute('PRAGMA table_info(push_log)').fetchall()]
chk('hidden_at' not in cols, '전제: 컬럼 제거됨', cols)
A._auto_migrate()
cols = [r[1] for r in A.get_db().execute('PRAGMA table_info(push_log)').fetchall()]
chk('hidden_at' in cols, '_auto_migrate 가 hidden_at 을 추가함', cols)
idx = A.query("SELECT name FROM sqlite_master WHERE type='index' "
              "AND name='idx_push_log_visible'", one=True)
chk(idx is not None, 'partial index 도 같이 생성됨(컬럼 ALTER 뒤에)', idx)
# 🔴 컬럼 없는 구버전 DB 에서 partial index 를 schema.sql 에 두면 그 줄에서 executescript 가
#    끊겨 **뒤쪽 문장이 통째로 스킵**된다(그래서 index 생성을 _auto_migrate 로 옮겼다).
tail = A.query("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
               "AND name='push_outbox'", one=True)['n']
chk(tail == 1, 'schema.sql 뒤쪽 문장도 재적용됨(index 문에서 안 끊김)', tail)
A._auto_migrate()                                        # 반복 실행 idempotent
chk(A.query("SELECT COUNT(*) n FROM sqlite_master WHERE type='index' "
            "AND name='idx_push_log_visible'", one=True)['n'] == 1,
    '_auto_migrate 반복 실행해도 안전')

print('\n# 2) 목록 조회 — hidden_at 필터 · can_clear 권한 플래그')
mklog('ev:1', '첫 알림')
mklog('ev:2', '둘째 알림')

login(U_MEM, 'member')
r = c.get('/api/ios/push/status').get_json()
chk(len(r['recent']) == 2, '기록 2건 노출', r['recent'])
chk(r['can_clear'] is False, 'member 는 can_clear=False', r['can_clear'])
chk(r['recent'][0]['title'] is None, 'member 에겐 title 가림(기존 계약 유지)', r['recent'][0])

login(U_ADMIN, 'admin')
r = c.get('/api/ios/push/status').get_json()
chk(r['can_clear'] is True, 'admin 은 can_clear=True')
chk(r['recent'][0]['title'] == '둘째 알림', 'admin 은 title 보임', r['recent'][0])

print('\n# 3) 권한 — member 는 403, 기록도 그대로')
login(U_MEM, 'member')
resp = c.post('/api/ios/push/log/clear', json={})
chk(resp.status_code == 403, 'member clear = 403', resp.status_code)
n = A.query("SELECT COUNT(*) n FROM push_log WHERE hidden_at IS NULL", one=True)['n']
chk(n == 2, '403 이면 아무것도 안 감춰짐', n)

with c.session_transaction() as s:                  # 로그인 안 한 경우
    s.clear()
chk(c.post('/api/ios/push/log/clear', json={}).status_code == 401,
    '미로그인 clear = 401')

print('\n# 4) admin clear — 감추기만 하고 행은 남는다')
login(U_ADMIN, 'admin')
resp = c.post('/api/ios/push/log/clear', json={})
j = resp.get_json()
chk(resp.status_code == 200 and j.get('ok') is True, 'clear 200/ok', j)
chk(j.get('hidden') == 2, '감춘 건수 반환', j)
chk(A.query("SELECT COUNT(*) n FROM push_log", one=True)['n'] == 2,
    '🔴 행은 삭제되지 않음(claim 보존)')
chk(A.query("SELECT COUNT(*) n FROM push_log WHERE hidden_at IS NOT NULL",
            one=True)['n'] == 2, '두 건 모두 hidden_at 찍힘')
r = c.get('/api/ios/push/status').get_json()
chk(r['recent'] == [], '목록에서 사라짐', r['recent'])

print('\n# 5) 🔴 지운 뒤에도 중복발송 차단이 살아있는가(이 테스트의 존재 이유)')
# 실제 발송 경로를 태우되 APNs 를 때리지 않게 스텁. (개발맥엔 진짜 .p8 이 있어서
# 스텁 없이 돌리면 정크 토큰으로 애플에 실요청이 나간다.)
ap = A._push_module()
_real_conf, _real_send = ap.load_conf, ap.send
sent_calls = []
ap.load_conf = lambda: {'stub': True}
ap.send = lambda token, payload, env='production', conf=None, collapse_id=None: (
    sent_calls.append(token), (True, 200, 'ok'))[1]
A.execute("INSERT INTO ios_device (user_id, token, env, active) VALUES (?,?,?,1)",
          (U_ADMIN, 'tok-clear-test', 'production'))
try:
    res = A._push_dispatch('test', 'ev:1', '첫 알림 재시도', 'b')
    chk(res.get('reason') == 'already_sent',
        '감춘 event_key 재발송 시도 = already_sent (재발송 안 됨)', res)
    chk(sent_calls == [], 'APNs 호출 0건', sent_calls)

    res2 = A._push_dispatch('test', 'ev:new', '새 알림', 'b')
    chk(res2.get('sent') == 1, '새 event_key 는 정상 발송', res2)
    r = c.get('/api/ios/push/status').get_json()
    chk([x['title'] for x in r['recent']] == ['새 알림'],
        'clear 이후 새 알림만 목록에 뜸(과거는 계속 감춤)', r['recent'])
finally:
    ap.load_conf, ap.send = _real_conf, _real_send

print('\n# 6) 기록 0건에서 clear — 에러 없이 0건')
c.post('/api/ios/push/log/clear', json={})
j = c.post('/api/ios/push/log/clear', json={}).get_json()
chk(j.get('ok') is True and j.get('hidden') == 0, '빈 상태 clear = hidden 0', j)

print('\n' + ('❌ 실패 %d건: %s' % (len(fails), fails) if fails else '✅ 전부 통과'))
try:
    os.unlink(DB)
except OSError:
    pass
sys.exit(1 if fails else 0)
