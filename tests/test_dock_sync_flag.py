#!/usr/bin/env python3
"""Dock — 견적요청 성공 직후 dock_sync 온디맨드 트리거(`dock_sync_flag`) 가 서는지.

배경(2026-08-04 형 실관측, S14/`BGBBES2607B41`): 견적요청은 LIVE 로 성공했고 SVMS 에도
정상 등록됐는데 TRMT 카드에 `0/1`(제출수/요청수)도 `🔗 … Quotation Inquiry` 도 안 보였다.
원인은 버그가 아니라 **공백** — 서버는 성공 시 단계(`stg_quote`/`stg_vendor`)만 낙관적으로 켜고,
`svms_submit`·`svms_req_no`·SVMS 실단계 라벨은 **폴러(dock_sync)만** 채우는데 자동 폴러가
launchd `ai.openclaw.dock-sync` = `StartInterval 3600`(1시간) 이라 최대 1시간 늦었다.

수정: 이미 있는 '수동 SVMS 발주 새로고침' flag(`api_settings.dock_sync_flag`, 맥 watcher
`ai.openclaw.dock-sync-watch` 60초 폴링)를 **견적요청 성공 콜백에서도** 세운다 → ~1분 내 반영.
새 스케줄러·새 칸 0개.

실행: ~/.venvs/trmt-test/bin/python tests/test_dock_sync_flag.py
  ⚠️`/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import os, sys, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
from source_bundle import shared_ns
from flask import session
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

KEY = 'testkey-dock-sync-flag'
A._ensure_api_table()
shared_ns.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}

shared_ns.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def setting(k):
    r = A.query("SELECT v FROM api_settings WHERE k=?", (k,), one=True)
    return r['v'] if r else None


def set_setting(k, v):
    shared_ns.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES(?, ?)", (k, v))


def flag():
    return setting('dock_sync_flag')


def pending():
    """맥 watcher 가 실제로 보는 값 — flag>done 일 때만 flag 를 준다."""
    r = c.get('/api/ext/dock_procure/sync/pending', headers=HDR)
    assert r.status_code == 200, r.status_code
    return r.get_json().get('flag')


def mkrow(req_no):
    shared_ns.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    shared_ns.execute("INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject) "
              "VALUES('TEST VESSEL','TSTV',?,'R',?)", (req_no, f'[DOCK][TSTV {req_no}]subject'))
    return A.query("SELECT id FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def mkdraft(rid, req_no, status='submitting'):
    shared_ns.execute("INSERT INTO dock_inquiry_draft(rid, vsl_nm, vsl_cd, req_no, rep_cd, doc_type, "
              "vndr_json, status, decided_at, decided_by) "
              "VALUES(?, 'TEST VESSEL','TSTV',?,?, 'REP', '[]', ?, "
              "datetime('now','localtime'), 'tester')", (rid, req_no, req_no, status))
    return A.query("SELECT id FROM dock_inquiry_draft ORDER BY id DESC", one=True)['id']


def clear_flags():
    shared_ns.execute("DELETE FROM api_settings WHERE k IN ('dock_sync_flag','dock_sync_done','dock_sync_result')")


# ══════════════════════════════════════════════════════════════════════════
print('\n#1 _dock_sync_flag_bump 기본 — 빈 상태')
clear_flags()
f1 = shared_ns._dock_sync_flag_bump()
chk(bool(f1), 'flag 가 세워진다', repr(f1))
chk(flag() == f1, '반환값 == 저장값', f'{f1} vs {flag()}')
chk(pending() == f1, 'watcher 가 pending 으로 본다', repr(pending()))

print('\n#2 flag 는 전진만 한다(연타)')
f2 = shared_ns._dock_sync_flag_bump()
chk(f2 >= f1, '두 번째 flag >= 첫 번째', f'{f1} → {f2}')
chk(pending() == f2, '여전히 pending', repr(pending()))

print('\n#3 🔴 done 이 이미 같은 시각으로 찍혀 있어도 pending 이 된다(조용한 유실 회귀)')
clear_flags()
now = A.query("SELECT datetime('now','localtime') t", one=True)['t']
set_setting('dock_sync_flag', now)
set_setting('dock_sync_done', now)                     # 버튼→sync→done 직후 = pending 아님
chk(pending() is None, '전제: 지금은 pending 아님', repr(pending()))
f3 = shared_ns._dock_sync_flag_bump()
chk(f3 > now, 'flag 가 done 보다 엄격히 크다', f'flag={f3} done={now}')
chk(pending() == f3, 'watcher 가 새 sync 를 집는다', repr(pending()))

print('\n#4 done 이 미래(시계 왜곡)여도 pending 이 된다')
clear_flags()
fut = A.query("SELECT datetime('now','localtime','+1 hour') t", one=True)['t']
set_setting('dock_sync_done', fut)
f4 = shared_ns._dock_sync_flag_bump()
chk(f4 > fut, 'flag > 미래 done', f'flag={f4} done={fut}')
chk(pending() == f4, 'pending 성립', repr(pending()))

print('\n#5 pending 인 flag 가 이미 있어도 새 시각으로 민다(진행 중 sync 가 우리 write 를 놓치는 경합)')
clear_flags()
set_setting('dock_sync_flag', now)                     # watcher 가 now 를 들고 sync 중일 수 있다
f5 = shared_ns._dock_sync_flag_bump()
chk(f5 > now, 'flag 가 전진했다', f'{now} → {f5}')
# watcher 가 처리 중이던 flag(now) 로 done 을 찍어도 우리 flag 는 살아남아 한 번 더 돈다
c.post('/api/ext/dock_procure/sync/done', json={'flag': now, 'result': 'x'}, headers=HDR)
chk(pending() == f5, '이전 sync 의 done 이 우리 요청을 덮지 않는다', repr(pending()))

print('\n#6 쓰레기 flag 값은 비교대상에서 제거되고 정상 시각으로 대체된다')
clear_flags()
set_setting('dock_sync_flag', 'zzz-not-a-date')
f6 = shared_ns._dock_sync_flag_bump()
chk(f6 and f6 != 'zzz-not-a-date', 'flag 가 정상 시각으로 대체된다', repr(f6))
chk(flag() == f6 and flag() is not None, 'flag 가 NULL/쓰레기로 남지 않는다', repr(flag()))
chk(pending() == f6, 'pending 성립(조건부 upsert 가 쓰레기에 막히지 않는다)', repr(pending()))

print('\n#6-1 🔴 쓰레기 flag + 정상 미래 done 조합(올마이트 지적) — 그래도 pending 이 된다')
clear_flags()
fut2 = A.query("SELECT datetime('now','localtime','+30 minutes') t", one=True)['t']
set_setting('dock_sync_flag', 'zzz-not-a-date')       # lexical high 쓰레기
set_setting('dock_sync_done', fut2)                   # 정상 미래값
f61 = shared_ns._dock_sync_flag_bump()
chk(f61 > fut2, 'flag > done (쓰레기가 floor 를 오염시키지 않는다)', f'flag={f61} done={fut2}')
# ⚠️문자열 비교라 'zzz' 도 lexically 크다 — 실제 시각인지 따로 못박는다(그래야 위 체크가 공허하지 않다)
chk(bool(A.query("SELECT datetime(?) t", (f61,), one=True)['t']), 'flag 가 실제 시각 형식', repr(f61))
chk(pending() == f61, 'watcher 가 집는다', repr(pending()))

print('\n#6-2 🔴 쓰레기 done 은 지우고 경고 — 기능 영구사망 방지')
clear_flags()
set_setting('dock_sync_done', 'zzz-broken')           # 남겨두면 done<flag 가 영구 false
f62 = shared_ns._dock_sync_flag_bump()
chk(setting('dock_sync_done') is None, '쓰레기 done 제거됨', repr(setting('dock_sync_done')))
chk(pending() == f62, 'pending 회복', repr(pending()))

print('\n#6-3 🔴 조건부 upsert = flag 는 후퇴하지 않는다(늦게 도착한 stale writer)')
clear_flags()
big = A.query("SELECT datetime('now','localtime','+10 minutes') t", one=True)['t']
set_setting('dock_sync_flag', big)
small = A.query("SELECT datetime('now','localtime','-10 minutes') t", one=True)['t']
shared_ns.execute("INSERT INTO api_settings (k, v) VALUES ('dock_sync_flag', ?) "
          "ON CONFLICT(k) DO UPDATE SET v=excluded.v WHERE excluded.v > api_settings.v", (small,))
chk(flag() == big, '더 작은 값은 무시된다(경합 후퇴 차단)', f'{big} vs {flag()}')
f63 = shared_ns._dock_sync_flag_bump()
chk(f63 > big and flag() == f63, 'bump 는 전진시킨다', f'{big} → {f63}')

print('\n#6-4 🔴 실제 경합 주입 — 읽기~쓰기 사이에 다른 writer 가 flag 를 밀어도 후퇴하지 않는다')
clear_flags()
big2 = A.query("SELECT datetime('now','localtime','+20 minutes') t", one=True)['t']
_execute = shared_ns.execute
_hit = {'n': 0}


def racing_execute(sql, params=()):
    """우리 upsert 가 실행되기 **직전**에 다른 호출이 더 큰 flag 를 넣은 상황을 만든다."""
    if 'ON CONFLICT' in sql and _hit['n'] == 0:
        _hit['n'] = 1
        _execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('dock_sync_flag', ?)", (big2,))
    return _execute(sql, params)


shared_ns.execute = racing_execute
try:
    f64 = shared_ns._dock_sync_flag_bump()
finally:
    shared_ns.execute = _execute
chk(_hit['n'] == 1, '경합 주입이 실제로 걸렸다(테스트 자체 검증)')
chk(flag() == big2, '저장된 flag 가 후퇴하지 않았다', f'expect={big2} got={flag()}')
chk(f64 == big2, '반환값도 실효 flag(더 큰 쪽)', f'{f64} vs {big2}')
chk(pending() == big2, 'pending 은 실효 flag', repr(pending()))

print('\n#7 빈 문자열 flag/done 도 안전')
clear_flags()
set_setting('dock_sync_flag', '')
set_setting('dock_sync_done', '')
f7 = shared_ns._dock_sync_flag_bump()
chk(bool(f7) and pending() == f7, "빈값 상태에서도 pending 성립", repr(f7))

# ══════════════════════════════════════════════════════════════════════════
print('\n#8 🔴 견적요청 성공(ok=true) → flag 가 선다')
clear_flags()
rid = mkrow('R901')
did = mkdraft(rid, 'R901')
r = c.post(f'/api/ext/dock_inquiry/drafts/{did}/result',
           json={'ok': True, 'result': 'Confirm+VendorSubmit=ok'}, headers=HDR)
j = r.get_json()
chk(r.status_code == 200 and j.get('applied') is True, 'result 반영됨', repr(j))
row = A.query("SELECT * FROM dock_procure WHERE id=?", (rid,), one=True)
chk((row['stg_quote'], row['stg_vendor']) == (1, 1), '낙관적 단계는 종전대로 켜진다',
    f"{row['stg_quote']},{row['stg_vendor']}")
chk((row['stg_confirm'] or 0, row['stg_order'] or 0) == (0, 0), '컨펌·발주는 건드리지 않는다')
chk(bool(flag()) and pending() == flag(), 'dock_sync 트리거 flag 가 pending', repr(flag()))
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE id=?", (did,), one=True)['status']
    == 'submitted', 'draft 는 submitted')

print('\n#9 같은 draft 재POST(연타) → applied False, flag 불변')
before = flag()
r = c.post(f'/api/ext/dock_inquiry/drafts/{did}/result', json={'ok': True}, headers=HDR)
chk(r.get_json().get('applied') is False, '이미 submitted → 미반영', repr(r.get_json()))
chk(flag() == before, 'flag 가 다시 서지 않는다', f'{before} → {flag()}')

print('\n#10 실패(ok=false) → flag 안 섬 · 단계 안 켜짐')
clear_flags()
rid2 = mkrow('R902')
did2 = mkdraft(rid2, 'R902')
r = c.post(f'/api/ext/dock_inquiry/drafts/{did2}/result',
           json={'ok': False, 'result': 'readback 불일치'}, headers=HDR)
chk(r.get_json().get('applied') is True and r.get_json().get('ok') is False, 'failed 로 기록', repr(r.get_json()))
chk(flag() is None, '실패는 sync 를 부르지 않는다', repr(flag()))
row2 = A.query("SELECT * FROM dock_procure WHERE id=?", (rid2,), one=True)
chk((row2['stg_quote'] or 0, row2['stg_vendor'] or 0) == (0, 0), '단계 미변경')

print('\n#11 submitting 아닌 draft(approved) + ok=true → 아무것도 안 함')
clear_flags()
rid3 = mkrow('R903')
did3 = mkdraft(rid3, 'R903', status='approved')
r = c.post(f'/api/ext/dock_inquiry/drafts/{did3}/result', json={'ok': True}, headers=HDR)
chk(r.get_json().get('applied') is False, 'CAS 미통과', repr(r.get_json()))
chk(flag() is None, 'flag 안 섬', repr(flag()))
row3 = A.query("SELECT * FROM dock_procure WHERE id=?", (rid3,), one=True)
chk((row3['stg_quote'] or 0, row3['stg_vendor'] or 0) == (0, 0), '단계 미변경')

print('\n#12 rid 가 이미 삭제된 행을 가리켜도 flag 는 선다(SVMS 상태는 바뀌었으므로)')
clear_flags()
rid4 = mkrow('R904')
did4 = mkdraft(rid4, 'R904')
shared_ns.execute("DELETE FROM dock_procure WHERE id=?", (rid4,))    # 단계 UPDATE 는 0행이 된다
r = c.post(f'/api/ext/dock_inquiry/drafts/{did4}/result', json={'ok': True}, headers=HDR)
chk(r.get_json().get('applied') is True, 'result 반영됨', repr(r.get_json()))
chk(bool(flag()), 'flag 가 선다', repr(flag()))

# ══════════════════════════════════════════════════════════════════════════
print('\n#13 수동 버튼 경로 회귀 — 같은 단일 writer 를 쓴다')
clear_flags()
with c.session_transaction() as s:
    s['user_id'] = 1
    s['username'] = 'admin'
    s['role'] = 'admin'
r = c.post('/api/dock_procure/sync/trigger')
j = r.get_json()
chk(r.status_code == 200 and j.get('ok') is True, '버튼 200', repr(j))
chk(j.get('flagged_at') == flag(), '응답 flagged_at == 저장 flag', f"{j.get('flagged_at')} vs {flag()}")
st = c.get('/api/dock_procure/sync/status').get_json()
chk(st.get('pending') is True, 'UI 상태 pending', repr(st))
c.post('/api/ext/dock_procure/sync/done', json={'flag': flag(), 'result': 'dock_sync=OK'}, headers=HDR)
st = c.get('/api/dock_procure/sync/status').get_json()
chk(st.get('pending') is False, 'done 콜 후 pending 해제', repr(st))
chk(pending() is None, 'watcher 도 not-pending')
r = c.post('/api/dock_procure/sync/trigger')
chk(c.get('/api/dock_procure/sync/status').get_json().get('pending') is True,
    '재트리거 가능(버튼이 죽지 않는다)')

print('\n#13-1 🔴 stale — pending 이 5분 넘게 안 닫히면 버튼을 다시 열어준다(watcher 정지 대비)')
clear_flags()
set_setting('dock_sync_flag', A.query("SELECT datetime('now','localtime','-6 minutes') t", one=True)['t'])
st = c.get('/api/dock_procure/sync/status').get_json()
chk(st.get('pending') is True and st.get('stale') is True, '6분 미완 = pending+stale', repr(st))
set_setting('dock_sync_flag', A.query("SELECT datetime('now','localtime','-1 minutes') t", one=True)['t'])
st = c.get('/api/dock_procure/sync/status').get_json()
chk(st.get('pending') is True and st.get('stale') is False, '1분 미완 = pending·stale 아님', repr(st))
c.post('/api/ext/dock_procure/sync/done', json={'flag': flag(), 'result': 'ok'}, headers=HDR)
st = c.get('/api/dock_procure/sync/status').get_json()
chk(st.get('pending') is False and st.get('stale') is False, '완료면 stale 아님', repr(st))

print('\n#13-2 flag bump 가 터져도 견적요청 결과기록·단계는 유지된다(격리)')
clear_flags()
rid6 = mkrow('R906')
did6 = mkdraft(rid6, 'R906')
orig = shared_ns._dock_sync_flag_bump


def boom():
    raise RuntimeError('bump 고장 주입')


shared_ns._dock_sync_flag_bump = boom
try:
    r = c.post(f'/api/ext/dock_inquiry/drafts/{did6}/result', json={'ok': True}, headers=HDR)
finally:
    shared_ns._dock_sync_flag_bump = orig
chk(r.status_code == 200 and r.get_json().get('applied') is True, 'result 는 정상 200/반영', repr(r.get_json()))
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE id=?", (did6,), one=True)['status']
    == 'submitted', 'draft submitted 유지')
row6 = A.query("SELECT * FROM dock_procure WHERE id=?", (rid6,), one=True)
chk((row6['stg_quote'], row6['stg_vendor']) == (1, 1), '낙관적 단계 유지')
chk(flag() is None, 'flag 는 안 섬(고장이 조용히 성공으로 위장되지 않는다)', repr(flag()))

print('\n#14 견적요청 성공이 done 직후에 와도 버튼 UI 가 pending 으로 살아난다(#3 의 e2e)')
clear_flags()
now2 = A.query("SELECT datetime('now','localtime') t", one=True)['t']
set_setting('dock_sync_flag', now2)
set_setting('dock_sync_done', now2)
rid5 = mkrow('R905')
did5 = mkdraft(rid5, 'R905')
c.post(f'/api/ext/dock_inquiry/drafts/{did5}/result', json={'ok': True}, headers=HDR)
chk(pending() is not None and pending() > now2, 'watcher 가 집는다', repr(pending()))

print('\n' + ('❌ FAIL: ' + ', '.join(fails) if fails else '✅ ALL PASS'))
try:
    os.unlink(DB)
except OSError:
    pass
sys.exit(1 if fails else 0)
