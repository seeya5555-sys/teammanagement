#!/usr/bin/env python3
"""`GET /api/me` 가 Bearer 요청에 한해 **이 토큰의 만료시각**(`token_expires_at`)을 돌려준다.

왜 필요한가:
  네이티브 앱은 오프라인(기내모드·지하)에서 `/api/me` 를 못 부르면 캐시된 프로필로 진입한다.
  그때 **만료시각을 모르면 진입을 거부**한다(fail-closed) — 앱이 유효기간을 임의로 늘려 잡으면
  폰 분실·권한 회수가 영영 반영되지 않기 때문이다. 그래서 만료시각의 출처는 언제나 서버다.
  로그인 응답(`expires_in`)만으로는 **이미 설치돼 있던 기기**(토큰은 있는데 만료 도장을 못 찍은
  상태)를 못 채운다. 이 엔드포인트가 그 구멍을 메운다.

잠그는 것:
  ① Bearer 요청 → `token_expires_at` 존재, 값 = 발급시각 + 30일(±허용오차).
  ② 쿠키 세션(브라우저) 요청 → **키 자체가 없다**. 발급시각이 없는데 아무 값이나 채우면
     앱이 남의 유효기간으로 오프라인 진입을 열게 된다.
  ③ 기존 필드(user_id/username/role/supervisor_id)는 두 경로 모두 그대로.
  ④ 위조·훼손 토큰은 여전히 401(만료시각 노출 경로가 인증을 무르게 하지 않는다).

실행: ~/.venvs/trmt-test/bin/python tests/test_api_me_token_expiry.py
  ⚠️ `/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import os, sys, time, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
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


from werkzeug.security import generate_password_hash

# ⚠️ 다른 테스트들처럼 app context 를 모듈 전역에 push 해두면 **이 테스트만 거짓 실패**한다.
#    Flask `RequestContext.push()` 는 같은 app 의 context 가 이미 있으면 새로 만들지 않고
#    재사용한다 → `g` 가 요청 사이에 남아, Bearer 요청이 심은 `g._token_issued_at` 을
#    다음 쿠키 요청이 그대로 본다. 실서버(WSGI)는 요청마다 context 가 없는 상태에서 시작하니
#    요청별로 새로 만들어진다 — 즉 프로덕션 결함이 아니라 하네스 결함이다.
#    그래서 준비 작업만 scoped context 로 하고, 요청은 context 없는 상태에서 쏜다.
with A.app.app_context():
    SUP = A.execute("INSERT INTO supervisors(name) VALUES('TEST SUP')")
    UID = A.execute(
        "INSERT INTO users(username, password_hash, display_name, role, supervisor_id, active) "
        "VALUES('offlinetest', ?, '오프라인테스트', 'member', ?, 1)",
        (generate_password_hash('pw-offline-test'), SUP))

c = A.app.test_client()

print('① Bearer 요청 — token_expires_at 존재 + 값 정확')
t0 = int(time.time())
r = c.post('/api/auth/token', json={'username': 'offlinetest', 'password': 'pw-offline-test'})
chk(r.status_code == 200, '로그인 200', f'got {r.status_code} {r.get_data(as_text=True)[:200]}')
tok = (r.get_json() or {}).get('token')
chk(isinstance(tok, str) and tok, '토큰 발급')

r = c.get('/api/me', headers={'Authorization': f'Bearer {tok}'})
chk(r.status_code == 200, '/api/me 200', f'got {r.status_code}')
me = r.get_json() or {}
exp = me.get('token_expires_at')
chk(isinstance(exp, int), 'token_expires_at 는 정수 epoch', repr(exp))
# 발급 직후라 t0 기준 ±5초 안에 들어와야 한다(테스트 실행 지연 허용).
want = t0 + A._TOKEN_MAXAGE
chk(isinstance(exp, int) and abs(exp - want) <= 5,
    '만료 = 발급시각 + _TOKEN_MAXAGE', f'exp={exp} want≈{want}')
chk(me.get('user_id') == UID and me.get('username') == 'offlinetest', '기존 필드 유지(Bearer)',
    repr({k: me.get(k) for k in ('user_id', 'username')}))
chk(me.get('supervisor_id') == SUP, 'supervisor_id 유지', repr(me.get('supervisor_id')))

print('② 쿠키 세션 요청 — 키 자체가 없어야 함(발급시각 없음 = 모름)')
c2 = A.app.test_client()
with c2.session_transaction() as s:
    s['user_id'] = UID; s['username'] = 'offlinetest'
    s['role'] = 'member'; s['supervisor_id'] = SUP; s['display_name'] = '오프라인테스트'
r = c2.get('/api/me')
chk(r.status_code == 200, '쿠키 /api/me 200', f'got {r.status_code}')
me2 = r.get_json() or {}
chk('token_expires_at' not in me2, 'token_expires_at 없음(추측값 금지)', repr(me2.get('token_expires_at')))
chk(me2.get('user_id') == UID and me2.get('role') == 'member', '기존 필드 유지(쿠키)', repr(me2))

print('③ 훼손 토큰 — 여전히 401')
bad = tok[:-3] + ('aaa' if not tok.endswith('aaa') else 'bbb')
r = c.get('/api/me', headers={'Authorization': f'Bearer {bad}'})
chk(r.status_code == 401, '훼손 토큰 401', f'got {r.status_code}')

print('④ 만료된 토큰 — 401 이고 만료시각도 안 흘린다')
# `return_timestamp=True` 로 바꾸면서 만료 검사가 무뎌지지 않았는지 본다
# (앱의 오프라인 진입 판정이 전부 이 만료값 위에 서 있어서, 여기가 무르면 전부 무르다).
_saved = A._TOKEN_MAXAGE
try:
    A._TOKEN_MAXAGE = -1          # 발급 즉시 만료된 것으로 취급
    r = c.get('/api/me', headers={'Authorization': f'Bearer {tok}'})
    chk(r.status_code == 401, '만료 토큰 401', f'got {r.status_code}')
    chk('token_expires_at' not in (r.get_json() or {}), '만료 응답에 만료시각 없음')
finally:
    A._TOKEN_MAXAGE = _saved
r = c.get('/api/me', headers={'Authorization': f'Bearer {tok}'})
chk(r.status_code == 200, '원복 후 다시 200', f'got {r.status_code}')

print()
if fails:
    print(f'❌ FAIL {len(fails)}: ' + ', '.join(fails))
    sys.exit(1)
print('✅ ALL PASS')
