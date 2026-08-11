#!/usr/bin/env python3
"""현안 진행경과 **원자적 1건 추가**(`POST /api/issues/<id>/actions`).

왜 별도 경로인가: 앱은 지금까지 `PUT /api/issues/<id>` 로 actions **배열 전체**를 덮어
진행을 추가했다. 온라인에선 읽고 곧바로 쓰니 티가 잘 안 났지만, 오프라인 보관함은
읽은 시점과 보내는 시점이 몇 시간~며칠 벌어진다 — 그 사이 웹/다른 기기가 추가한 진행이
**조용히 사라진다**. 그래서 추가는 서버에서 append 하고, 되돌려쓰기 사이의 변경은 CAS 로 막는다.

잠그는 것:
  ① 추가는 기존 진행을 하나도 건드리지 않고 **맨 뒤에** 붙는다.
  ② 오프라인 갭 재현 — 클라가 모르는 사이 다른 곳이 진행을 추가해도 그 진행이 살아남는다
     (예전 PUT 전체덮기였다면 사라졌을 경로).
  ③ 읽고 쓰는 사이 원문이 바뀌면 CAS 재시도로 흡수하고, 계속 밀리면 409(무한루프 없음).
  ④ actions 가 NULL/깨진 JSON 인 기존 행에도 안전하게 붙는다.
  ⑤ 빈 내용 = 400, 날짜 형식 위반 = 400, 없는 현안 = 404 — 그리고 그때 DB 는 무변경.
  ⑥ 날짜 생략 시 오늘로 채운다.

실행: ~/.venvs/trmt-test/bin/python tests/test_issue_action_append.py
  ⚠️ `/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import os, sys, json, tempfile
from datetime import date

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
from source_bundle import shared_ns
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
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

SUP = A.execute("INSERT INTO supervisors(name) VALUES('TEST SUP')")
VSL = A.execute("INSERT INTO vessels(name) VALUES('TEST VESSEL')")

BASE = [
    {'date': '2026-08-01', 'progress': '첫 진행', 'important': False},
    {'date': '2026-08-03', 'progress': '둘째 진행', 'important': True},
]


def mkissue(actions=BASE):
    raw = None if actions is None else (actions if isinstance(actions, str)
                                        else json.dumps(actions, ensure_ascii=False))
    return A.execute(
        "INSERT INTO issues(supervisor_id, vessel_id, issue_date, item_topic, description, "
        "actions, priority, status) VALUES(?,?,'2026-08-01','TEST TOPIC','',?, 'Normal','Open')",
        (SUP, VSL, raw))


def acts_of(iid):
    raw = shared_ns.query('SELECT actions FROM issues WHERE id=?', (iid,), one=True)['actions']
    return json.loads(raw) if raw else []


def post(iid, body):
    return c.post(f'/api/issues/{iid}/actions', json=body)


print('\n[1] 추가는 맨 뒤에 붙고 기존 진행은 불변')
iid = mkissue()
r = post(iid, {'progress': '셋째 진행', 'date': '2026-08-07', 'important': True})
chk(r.status_code == 200, '200', r.status_code)
got = acts_of(iid)
chk(len(got) == 3, '3건', len(got))
chk(got[:2] == BASE, '기존 2건 글자 그대로', got[:2])
chk(got[2] == {'date': '2026-08-07', 'progress': '셋째 진행', 'important': True}, '새 항목', got[2:])
chk(r.get_json().get('actions') == got, '응답 = DB 확정본')

print('\n[2] 오프라인 갭 — 그 사이 다른 곳이 추가한 진행이 살아남는다')
iid = mkissue()
# 앱이 오프라인에서 읽어 둔 스냅샷(2건)을 들고 있는 동안, 웹이 진행을 하나 더 붙였다고 가정.
web = BASE + [{'date': '2026-08-05', 'progress': '웹에서 추가한 진행', 'important': False}]
A.execute('UPDATE issues SET actions=? WHERE id=?', (json.dumps(web, ensure_ascii=False), iid))
r = post(iid, {'progress': '기내에서 적어둔 진행', 'date': '2026-08-06'})
got = acts_of(iid)
chk(r.status_code == 200, '200', r.status_code)
chk(len(got) == 4, '4건 = 웹 진행이 안 사라짐', got)
chk(any(a['progress'] == '웹에서 추가한 진행' for a in got), '웹 진행 보존')
chk(got[-1]['progress'] == '기내에서 적어둔 진행', '내 진행은 맨 뒤')

print('\n[3] 읽고 쓰는 사이 변경 — CAS 재시도로 흡수, 계속 밀리면 409')
iid = mkissue()
orig_query = shared_ns.query
state = {'n': 0}


def racing_query(sql, args=(), one=False):
    row = orig_query(sql, args, one=one)
    # 뷰가 actions 를 읽은 **직후**에만 끼어든다(첫 1회). UPDATE 의 CAS 가 빗나가고 재시도가 돈다.
    if one and 'SELECT actions FROM issues' in sql and state['n'] == 0:
        state['n'] = 1
        A.execute('UPDATE issues SET actions=? WHERE id=?',
                  (json.dumps(BASE + [{'date': '2026-08-04', 'progress': '끼어든 진행',
                                       'important': False}], ensure_ascii=False), args[0]))
    return row


shared_ns.query = racing_query
try:
    r = post(iid, {'progress': '재시도로 붙는 진행', 'date': '2026-08-07'})
finally:
    shared_ns.query = orig_query
got = acts_of(iid)
chk(r.status_code == 200, '재시도 1회는 흡수 → 200', r.status_code)
chk(any(a['progress'] == '끼어든 진행' for a in got), '끼어든 진행 보존', got)
chk(got[-1]['progress'] == '재시도로 붙는 진행', '내 진행도 붙음', got)

iid = mkissue()
state2 = {'n': 0}


def always_racing(sql, args=(), one=False):
    row = orig_query(sql, args, one=one)
    if one and 'SELECT actions FROM issues' in sql:
        state2['n'] += 1
        A.execute('UPDATE issues SET actions=? WHERE id=?',
                  (json.dumps(BASE + [{'date': '2026-08-04', 'progress': f'경합{state2["n"]}',
                                       'important': False}], ensure_ascii=False), args[0]))
    return row


shared_ns.query = always_racing
try:
    r = post(iid, {'progress': '영원히 밀리는 진행'})
finally:
    shared_ns.query = orig_query
chk(r.status_code == 409, '계속 밀리면 409(무한루프 없음)', r.status_code)
chk(not any(a['progress'] == '영원히 밀리는 진행' for a in acts_of(iid)), '409 면 DB 무변경')

print('\n[4] actions 가 NULL / 깨진 JSON 인 기존 행')
iid = mkissue(actions=None)
r = post(iid, {'progress': 'NULL 행에 첫 진행', 'date': '2026-08-07'})
chk(r.status_code == 200 and len(acts_of(iid)) == 1, 'NULL → 1건', r.status_code)
iid = mkissue(actions='{이건 JSON 이 아님')
r = post(iid, {'progress': '깨진 행에 진행', 'date': '2026-08-07'})
chk(r.status_code == 200 and len(acts_of(iid)) == 1, '깨진 JSON → 빈 배열로 보고 1건', r.status_code)

print('\n[5] 거부 경로 — 그리고 그때 DB 무변경')
iid = mkissue()
for body, why in [({'progress': '   '}, '빈 내용'),
                  ({}, '키 없음'),
                  ({'progress': 'x', 'date': '2026-13-99'}, '없는 날짜'),
                  ({'progress': 'x', 'date': '08/07/2026'}, '형식 위반')]:
    r = post(iid, body)
    chk(r.status_code == 400, f'{why} → 400', r.status_code)
chk(acts_of(iid) == BASE, '거부 후 DB 무변경', acts_of(iid))
r = post(iid, 'not-json')
chk(r.status_code == 400, '바디가 JSON 아님 → 400', r.status_code)
r = post(999999, {'progress': 'x'})
chk(r.status_code in (403, 404), '없는 현안 → 404(스코프 거부면 403)', r.status_code)

print('\n[6] 날짜 생략 = 오늘')
iid = mkissue()
r = post(iid, {'progress': '날짜 없이'})
chk(r.status_code == 200 and acts_of(iid)[-1]['date'] == date.today().isoformat(),
    '오늘로 채움', acts_of(iid)[-1] if acts_of(iid) else None)
chk(acts_of(iid)[-1]['important'] is False, 'important 기본 False')

print('\n' + ('❌ 실패 %d: %s' % (len(fails), fails) if fails else '✅ 전부 통과'))
sys.exit(1 if fails else 0)
