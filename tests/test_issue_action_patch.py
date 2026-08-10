#!/usr/bin/env python3
"""현안 진행경과 **1건 인라인 수정**(`PATCH /api/issues/<id>/actions/<idx>`).

모바일에서 편집 모달을 열지 않고 그 줄에서 내용·발생일·중요표시를 고치는 경로다.
배열 전체를 덮는 `PUT /api/issues/<id>` 와 달리 **대상 1건만** 바꿔야 하므로,
되돌려쓰기 사이의 변경을 두 겹으로 fail-closed 한다.

잠그는 것:
  ① 대상 index 만 바뀌고 나머지 진행은 글자 하나 안 변한다.
  ② `important` 단독 토글이 내용·발생일을 건드리지 않는다(별 탭 = 토글 전용).
  ③ `prev`(편집 시작 시점 값)가 어긋나면 409 + DB 무변경 — 목록이 밀렸을 때
     **엉뚱한 줄이 고쳐지는** 사고를 막는다.
  ④ 읽고 쓰는 사이 다른 write 가 들어와도 원문 CAS 로 409(lost-update 차단).
  ⑤ 범위 밖 index = 409, 빈 내용 = 400, 날짜 형식 위반 = 400.

실행: ~/.venvs/trmt-test/bin/python tests/test_issue_action_patch.py
  ⚠️ `/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import os, sys, json, tempfile

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


A.app.app_context().push()
c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

SUP = A.execute("INSERT INTO supervisors(name) VALUES('TEST SUP')")
VSL = A.execute("INSERT INTO vessels(name) VALUES('TEST VESSEL')")

BASE = [
    {'date': '2026-08-01', 'progress': '첫 진행', 'important': False},
    {'date': '2026-08-03', 'progress': '둘째 진행', 'important': False},
    {'date': '2026-08-06', 'progress': 'AOR 컨펌. 대체품 보급 진행', 'important': True},
]


def mkissue(actions=None):
    return A.execute(
        "INSERT INTO issues(supervisor_id, vessel_id, issue_date, item_topic, description, "
        "actions, priority, status) VALUES(?,?,'2026-08-01','TEST TOPIC','',?, 'Normal','Open')",
        (SUP, VSL, json.dumps(actions if actions is not None else BASE, ensure_ascii=False)))


def acts_of(iid):
    raw = A.query('SELECT actions FROM issues WHERE id=?', (iid,), one=True)['actions']
    return json.loads(raw) if raw else []


def patch(iid, idx, body):
    return c.patch(f'/api/issues/{iid}/actions/{idx}', json=body)


print('① 대상 1건만 수정 — 나머지 진행은 무변경')
iid = mkissue()
r = patch(iid, 1, {'progress': '둘째 진행(수정)', 'date': '2026-08-04', 'important': True,
                   'prev': {'date': '2026-08-03', 'progress': '둘째 진행'}})
chk(r.status_code == 200, '200', f'got {r.status_code} {r.get_data(as_text=True)[:200]}')
a = acts_of(iid)
chk(len(a) == 3, '행 수 유지', str(len(a)))
chk(a[1] == {'date': '2026-08-04', 'progress': '둘째 진행(수정)', 'important': True},
    '대상 행이 내용·발생일·중요표시 모두 반영', str(a[1]))
chk(a[0] == BASE[0] and a[2] == BASE[2], '나머지 두 행은 글자 하나 안 변함', str([a[0], a[2]]))
chk((r.get_json() or {}).get('actions') == a, '응답 actions = 서버 정본')

print('② important 단독 토글은 내용·발생일을 건드리지 않는다')
iid = mkissue()
r = patch(iid, 0, {'important': True, 'prev': {'date': '2026-08-01', 'progress': '첫 진행'}})
a = acts_of(iid)
chk(r.status_code == 200 and a[0] == {'date': '2026-08-01', 'progress': '첫 진행', 'important': True},
    '별만 켜짐', f'{r.status_code} {a[0]}')
r = patch(iid, 0, {'important': False, 'prev': {'date': '2026-08-01', 'progress': '첫 진행'}})
a = acts_of(iid)
chk(r.status_code == 200 and a[0]['important'] is False, '다시 끄기도 동작', str(a[0]))

print('③ prev 어긋나면 409 + DB 무변경 (다른 줄 오편집 차단)')
iid = mkissue()
before = acts_of(iid)
r = patch(iid, 1, {'progress': '엉뚱한 수정',
                   'prev': {'date': '2026-08-03', 'progress': '사라진 옛 문구'}})
chk(r.status_code == 409, 'prev 불일치 = 409', f'got {r.status_code}')
chk(acts_of(iid) == before, 'DB 무변경')
chk((r.get_json() or {}).get('actions') == before, '409 본문에 서버 정본 actions 동봉')

print('④ 읽고 쓰는 사이 다른 write = 409 (lost-update 차단)')
iid = mkissue()
orig_query = A.query
state = {'raced': False}


def racing_query(sql, params=(), one=False):
    out = orig_query(sql, params, one=one)
    # 라우트가 actions 를 읽은 직후, 쓰기 전에 다른 클라이언트가 진행을 추가한 상황을 만든다.
    if not state['raced'] and 'SELECT actions FROM issues' in sql:
        state['raced'] = True
        extra = BASE + [{'date': '2026-08-07', 'progress': '웹에서 추가된 진행', 'important': False}]
        orig_execute('UPDATE issues SET actions=? WHERE id=?',
                     (json.dumps(extra, ensure_ascii=False), iid))
    return out


orig_execute = A.execute
A.query = racing_query
try:
    r = patch(iid, 1, {'progress': '둘째 진행(수정)',
                       'prev': {'date': '2026-08-03', 'progress': '둘째 진행'}})
finally:
    A.query = orig_query
a = acts_of(iid)
chk(r.status_code == 409, 'CAS 실패 = 409', f'got {r.status_code}')
chk(len(a) == 4 and a[3]['progress'] == '웹에서 추가된 진행',
    '끼어든 진행이 유실되지 않는다', str(len(a)))
chk(a[1] == BASE[1], '우리 수정은 적용되지 않았다(무변경)', str(a[1]))

print('⑤ 입력 가드')
iid = mkissue()
before = acts_of(iid)
PV0 = {'date': '2026-08-01', 'progress': '첫 진행'}
chk(patch(iid, 9, {'progress': 'x', 'prev': PV0}).status_code == 409, '범위 밖 index = 409')
chk(patch(iid, 0, {'progress': '   ', 'prev': PV0}).status_code == 400, '빈 내용 = 400')
chk(patch(iid, 0, {'date': '2026/08/09', 'prev': PV0}).status_code == 400, '날짜 형식 위반 = 400')
chk(acts_of(iid) == before, '거부된 요청은 DB 를 바꾸지 않는다')
#   date 빈 문자열은 무시(발생일 없는 행으로 떨어지지 않게) — 나머지 필드만 반영.
r = patch(iid, 0, {'date': '', 'important': True, 'prev': PV0})
chk(r.status_code == 200 and acts_of(iid)[0]['date'] == '2026-08-01',
    '빈 date 는 무시하고 기존 발생일 유지', str(acts_of(iid)[0]))

print('⑥ actions 가 NULL 인 옛 행 — 편집 대상이 없으니 409(500 아님)')
iid = A.execute(
    "INSERT INTO issues(supervisor_id, vessel_id, issue_date, item_topic, description, "
    "actions, priority, status) VALUES(?,?,'2026-08-01','NO ACTIONS','',NULL,'Normal','Open')",
    (SUP, VSL))
chk(patch(iid, 0, {'progress': 'x'}).status_code == 409, 'NULL actions = 409')

print('⑦ 없는 이슈 = 404')
chk(patch(999999, 0, {'progress': 'x'}).status_code == 404, '404')

print('⑧ prev 는 필수 — index 만 믿고 고치는 길을 막는다 (올마이트 지적)')
iid = mkissue()
before = acts_of(iid)
for body, label in ((({'progress': 'x'}), 'prev 없음'),
                    ({'progress': 'x', 'prev': {'date': '2026-08-01'}}, 'prev.progress 누락'),
                    ({'progress': 'x', 'prev': {'progress': '첫 진행'}}, 'prev.date 누락'),
                    ({'progress': 'x', 'prev': '첫 진행'}, 'prev 가 dict 아님'),
                    ({'progress': 'x', 'prev': {'date': 1, 'progress': '첫 진행'}}, 'prev.date 타입 위반')):
    chk(patch(iid, 0, body).status_code == 400, f'{label} = 400')
chk(acts_of(iid) == before, 'prev 가드에 걸린 요청은 DB 무변경')

print('⑨ prev.important 대조 — 다른 기기가 켠 별을 옛 값으로 되돌리지 않는다')
iid = mkissue()
PREV0 = {'date': '2026-08-01', 'progress': '첫 진행'}
# 다른 기기가 별을 켠 상태
r = patch(iid, 0, {'important': True, 'prev': dict(PREV0, important=False)})
chk(r.status_code == 200 and acts_of(iid)[0]['important'] is True, '먼저 켜기 성공')
# 옛 스냅샷(important=False)을 들고 온 요청은 거부
r = patch(iid, 0, {'progress': '첫 진행(수정)', 'prev': dict(PREV0, important=False)})
chk(r.status_code == 409 and acts_of(iid)[0]['progress'] == '첫 진행',
    'stale important 스냅샷 = 409 + 무변경', f'{r.status_code} {acts_of(iid)[0]}')
# 최신 값을 들고 오면 통과
r = patch(iid, 0, {'progress': '첫 진행(수정)', 'prev': dict(PREV0, important=True)})
chk(r.status_code == 200 and acts_of(iid)[0]['progress'] == '첫 진행(수정)', '최신 스냅샷은 통과')

print('⑩ 타입/날짜 가드 — 500 대신 400 (본문이 dict 아님·Bool 위장·불가능한 날짜)')
iid = mkissue()
before = acts_of(iid)
P0 = {'date': '2026-08-01', 'progress': '첫 진행'}
chk(c.patch(f'/api/issues/{iid}/actions/0', json=['x']).status_code == 400, 'body 가 배열 = 400')
chk(patch(iid, 0, {'important': 'false', 'prev': P0}).status_code == 400,
    'important 문자열 = 400(문자열 "false" 가 True 로 저장되던 구멍)')
chk(patch(iid, 0, {'progress': 123, 'prev': P0}).status_code == 400, 'progress 숫자 = 400')
chk(patch(iid, 0, {'date': '2026-02-31', 'prev': P0}).status_code == 400,
    '존재하지 않는 날짜 = 400(정규식만으론 통과하던 값)')
chk(acts_of(iid) == before, '타입 가드에 걸린 요청은 DB 무변경')

print('⑪ 권한 — 남의 담당 현안은 못 고친다 / 비로그인 차단')
OTHER = A.execute("INSERT INTO supervisors(name) VALUES('OTHER SUP')")
iid = mkissue()
with c.session_transaction() as s:
    s['role'] = 'member'; s['supervisor_id'] = OTHER
r = patch(iid, 0, {'progress': '남의 현안 수정', 'prev': P0})
chk(r.status_code == 403, '다른 감독 현안 = 403', f'got {r.status_code}')
chk(acts_of(iid)[0]['progress'] == '첫 진행', '남의 현안은 DB 무변경')
with c.session_transaction() as s:
    s['role'] = 'member'; s['supervisor_id'] = SUP
r = patch(iid, 0, {'progress': '내 현안 수정', 'prev': P0})
chk(r.status_code == 200 and acts_of(iid)[0]['progress'] == '내 현안 수정',
    '본인 담당 현안은 수정 가능', f'{r.status_code}')
anon = A.app.test_client()
r = anon.patch(f'/api/issues/{iid}/actions/0', json={'progress': 'x', 'prev': P0})
chk(r.status_code in (302, 401, 403), '비로그인 차단', f'got {r.status_code}')
with c.session_transaction() as s:
    s['role'] = 'admin'; s['supervisor_id'] = None

os.unlink(DB)
print()
if fails:
    print(f'❌ {len(fails)} 실패: ' + ', '.join(fails))
    sys.exit(1)
print('✅ 전부 통과')
