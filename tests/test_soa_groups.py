#!/usr/bin/env python3
"""SOA 그룹 편집 스모크 — 임시 DB에 init_db 후 실제 endpoint 를 test_client 로 두드린다.

실행: /tmp/soavenv/bin/python tests/test_soa_groups.py  (flask+werkzeug 있는 인터프리터면 됨)
검사: 시드 동일성 · task 파생 · dynamic 편입선 표시 · CRUD · 불변식 5종 · 롤백 ·
      category 불변 · 실행중 비활성화 금지 · ext 읽기 API fail-closed(500).
"""
import os, sys, tempfile, json

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
from source_bundle import shared_ns  # noqa: E402
A.DATABASE = DB            # init_db 는 모듈 전역을 씀(app.config 아님)
A.app.config['DATABASE'] = DB
A.app.config['TESTING'] = True
A.init_db(drop=False)
A._auto_migrate()

fails = []
def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)

A.app.app_context().push()      # A.query/A.execute 직접호출용(요청은 자체 컨텍스트 사용)
c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

# 1) 시드 = 현행 하드코딩 동일
r = c.get('/api/automation/soa/groups')
j = r.get_json()
chk(r.status_code == 200 and j['ok'], 'GET groups 200')
keys = [g['key'] for g in j['groups']]
chk(keys == ['G1', 'G2', 'G3', 'SKRT'], '시드 그룹 4개', keys)
g1 = next(g for g in j['groups'] if g['key'] == 'G1')
chk(g1['vessels'] == ['ATBG', 'ATGR', 'ATGV', 'ATMT'], 'G1 시드 선박', g1['vessels'])
chk(j['config_version'] == 1, 'config_version=1', j['config_version'])
# 편집 모달이 pool·예약키를 서버에서 받아 쓴다(프론트 하드코딩 금지)
chk(j.get('category_owner') == shared_ns.SOA_CATEGORY_OWNER, 'category_owner 내려줌', j.get('category_owner'))
chk('VESSEL' in (j.get('reserved_keys') or []) and 'RESEND' in j['reserved_keys'],
    'reserved_keys 내려줌', j.get('reserved_keys'))
chk(all(k not in (j.get('reserved_keys') or []) for k in ('G1', 'SKRT')),
    'reserved_keys 에 실제 그룹 key 는 없음', j.get('reserved_keys'))
chk(isinstance(j.get('owners'), dict), 'owners 맵 내려줌')

# 공통 loader는 그룹 수가 늘어도 membership을 행마다 다시 읽지 않는다.
trace = []
db_trace = A.get_db()
db_trace.set_trace_callback(trace.append)
loaded = shared_ns._soa_groups_load(active_only=False)
db_trace.set_trace_callback(None)
soa_selects = [sql for sql in trace if sql.lstrip().upper().startswith('SELECT')
               and 'SOA_GROUP' in sql.upper()]
chk(len(loaded) == 4 and len(soa_selects) == 1, '그룹 loader SQL 1회 고정', soa_selects)

# 2) task 목록이 그룹에서 파생되는지
tasks = shared_ns.automation_tasks()
chk(list(tasks)[:4] == ['soa_g1', 'soa_g2', 'soa_g3', 'soa_skrt'], 'task 파생', list(tasks)[:4])
chk('jeonja' in tasks and 'soa_vessel' in tasks, '정적 task 보존')

# 3) owner 스냅샷 push(러너 역할) → dynamic 그룹 멤버 표시
OWN = {'CPPS': '001', 'GNPS': '001', 'BGBB': '001',
       'ATBG': '037', 'ATGR': '037', 'ATGV': '037', 'ATMT': '037',
       'ATNH': '037', 'ATSH': '037', 'ATSL': '037', 'JATX': '037',
       'PCBJ': '037', 'PCGV': '037'}
key = A.query("SELECT v FROM api_settings WHERE k='api_key'", one=True)
if not key:
    A.execute("INSERT OR REPLACE INTO api_settings (k,v) VALUES ('api_key','smokekey')")
    apikey = 'smokekey'
else:
    apikey = key['v']
r = c.post('/api/ext/soa/vessel-owners', json={'owners': OWN}, headers={'X-API-Key': apikey})
chk(r.status_code == 200, 'owner push 200', r.get_data(as_text=True)[:120])
r = c.get('/api/automation/soa/groups')
skrt = next(g for g in r.get_json()['groups'] if g['key'] == 'SKRT')
chk(skrt['current_members'] == ['BGBB', 'CPPS', 'GNPS'], 'SKRT 현재 편입 표시', skrt['current_members'])
g3 = next(g for g in r.get_json()['groups'] if g['key'] == 'G3')
chk(g3['owner_mismatch'] == ['PCBS', 'PCMC'], 'G3 owner 불일치 표면화', g3['owner_mismatch'])
chk('BGBB·CPPS·GNPS' in shared_ns.automation_tasks()['soa_skrt'], 'SKRT 라벨에 편입선 노출', shared_ns.automation_tasks()['soa_skrt'])

# 4) 신규 그룹 생성
r = c.post('/api/automation/soa/groups',
           json={'key': 'g4', 'label': 'SOA 실버 G4', 'category': 'silver',
                 'mode': 'explicit', 'vessels': 'pcnw, atnw', 'sort_order': 35, 'active': True})
chk(r.status_code == 201, 'G4 생성 201', r.get_data(as_text=True)[:160])
chk(r.get_json()['config_version'] == 2, 'version bump=2', r.get_json())
chk('soa_g4' in shared_ns.automation_tasks(), 'soa_g4 task 등장')
g4 = next(g for g in c.get('/api/automation/soa/groups').get_json()['groups'] if g['key'] == 'G4')
chk(g4['vessels'] == ['ATNW', 'PCNW'], '소문자 입력 → 대문자 정규화', g4['vessels'])

# 4b) 선박 이동은 2스텝(빼고 → 넣기). 한 번에 넣으면 중복배정으로 거부되는 게 정상.
r = c.put('/api/automation/soa/groups/G4', json={'label': 'SOA 실버 G4', 'mode': 'explicit',
          'vessels': 'ATNW PCNW PCMC', 'sort_order': 35, 'active': True})
chk(r.status_code == 422, '이동: 먼저 빼지 않으면 거부')
r = c.put('/api/automation/soa/groups/G3', json={'label': 'SOA 실버 G3', 'mode': 'explicit',
          'vessels': 'PCBJ PCBS PCGV', 'sort_order': 30, 'active': True})
chk(r.status_code == 200, '이동 1: G3 에서 PCMC 제거')
r = c.put('/api/automation/soa/groups/G4', json={'label': 'SOA 실버 G4', 'mode': 'explicit',
          'vessels': 'ATNW PCNW PCMC', 'sort_order': 35, 'active': True})
chk(r.status_code == 200, '이동 2: G4 에 PCMC 추가')

# 5) 불변식 — 같은 owner 안에서 선박 중복배정 거부
VBEFORE = c.get('/api/automation/soa/groups').get_json()['config_version']
r = c.put('/api/automation/soa/groups/G1',
          json={'label': 'SOA 실버 G1', 'mode': 'explicit', 'vessels': 'ATBG ATGR ATGV ATMT PCBS',
                'sort_order': 10, 'active': True})
chk(r.status_code == 422 and 'PCBS' in r.get_json()['error'], '중복배정 422', r.get_data(as_text=True)[:160])
r = c.get('/api/automation/soa/groups')
g1 = next(g for g in r.get_json()['groups'] if g['key'] == 'G1')
chk(g1['vessels'] == ['ATBG', 'ATGR', 'ATGV', 'ATMT'], '422 후 롤백(원상)', g1['vessels'])
chk(r.get_json()['config_version'] == VBEFORE, '422 는 version 안 올림', r.get_json()['config_version'])

# 6) 불변식 — 같은 owner 에 dynamic 2개 금지
r = c.post('/api/automation/soa/groups',
           json={'key': 'SKRT2', 'label': '장금2', 'category': 'skrt',
                 'mode': 'dynamic_owner', 'vessels': '', 'sort_order': 50, 'active': True})
chk(r.status_code == 422, 'dynamic 중복 422', r.get_data(as_text=True)[:160])
# 비활성으로는 허용(불변식은 활성만)
r = c.post('/api/automation/soa/groups',
           json={'key': 'SKRT2', 'label': '장금2', 'category': 'skrt',
                 'mode': 'dynamic_owner', 'vessels': '', 'sort_order': 50, 'active': False})
chk(r.status_code == 201, '비활성 dynamic 은 허용', r.get_data(as_text=True)[:160])
chk('soa_skrt2' not in shared_ns.automation_tasks(), '비활성 그룹은 task 미노출')
# 재활성화 시 다시 검증
r = c.put('/api/automation/soa/groups/SKRT2',
          json={'label': '장금2', 'mode': 'dynamic_owner', 'vessels': '', 'sort_order': 50, 'active': True})
chk(r.status_code == 422, '재활성화 시 불변식 재검증 422', r.get_data(as_text=True)[:160])

# 7) 형식 방어
for body, name in [
    ({'key': 'g5!', 'label': 'x', 'category': 'silver', 'mode': 'explicit', 'vessels': '', 'sort_order': 1}, 'key 형식'),
    ({'key': 'G6', 'label': 'x', 'category': 'gold', 'mode': 'explicit', 'vessels': '', 'sort_order': 1}, 'category enum'),
    ({'key': 'G7', 'label': 'x', 'category': 'silver', 'mode': 'weird', 'vessels': '', 'sort_order': 1}, 'mode enum'),
    ({'key': 'G8', 'label': 'x', 'category': 'silver', 'mode': 'explicit', 'vessels': 'TOOLONG', 'sort_order': 1}, 'vsl_cd 형식'),
    ({'key': 'G9', 'label': 'x', 'category': 'skrt', 'mode': 'dynamic_owner', 'vessels': 'CPPS', 'sort_order': 1}, 'dynamic+선박 금지'),
    ({'key': 'G1', 'label': 'x', 'category': 'silver', 'mode': 'explicit', 'vessels': '', 'sort_order': 1}, 'key 중복'),
]:
    r = c.post('/api/automation/soa/groups', json=body)
    chk(r.status_code == 422, f'거부: {name}', r.get_data(as_text=True)[:120])

# 8) category 는 수정 불가(PUT payload 에 넣어도 무시)
r = c.put('/api/automation/soa/groups/G1',
          json={'label': 'SOA 실버 G1', 'category': 'skrt', 'mode': 'explicit',
                'vessels': 'ATBG ATGR ATGV ATMT', 'sort_order': 10, 'active': True})
g1 = next(g for g in c.get('/api/automation/soa/groups').get_json()['groups'] if g['key'] == 'G1')
chk(r.status_code == 200 and g1['category'] == 'silver', 'category 불변', g1['category'])

# 9) 실행중 그룹 비활성화 금지
import uuid
A.execute("INSERT INTO automation_run (run_id,task,mode,status,requested_by) VALUES (?,?,?,?,?)",
          (uuid.uuid4().hex[:12], 'soa_g4', 'verify', 'running', 'smoke'))
r = c.put('/api/automation/soa/groups/G4',
          json={'label': 'SOA 실버 G4', 'mode': 'explicit', 'vessels': 'PCBS PCMC',
                'sort_order': 35, 'active': False})
chk(r.status_code == 422 and '진행중' in r.get_json()['error'], '실행중 비활성화 거부', r.get_data(as_text=True)[:160])
A.execute("UPDATE automation_run SET status='done' WHERE task='soa_g4'")

# 10) 새 그룹 실행 요청이 서버 검증을 통과하는지 / 없는 그룹은 거부
r = c.post('/api/automation/run', json={'task': 'soa_g4', 'mode': 'verify'})
chk(r.status_code == 200, 'soa_g4 실행 적재 200', r.get_data(as_text=True)[:160])
r = c.post('/api/automation/run', json={'task': 'soa_zz', 'mode': 'verify'})
chk(r.status_code == 400, '미등록 그룹 400', r.get_data(as_text=True)[:120])

# 11) ext 읽기 API(러너 sync 소스)
r = c.get('/api/ext/soa/groups', headers={'X-API-Key': apikey})
j = r.get_json()
chk(r.status_code == 200 and j['ok'] and j['schema'] == 1, 'ext groups 200')
chk([g['key'] for g in j['groups']] == ['G1', 'G2', 'G3', 'G4', 'SKRT'],
    'ext 활성 그룹만·순서', [g['key'] for g in j['groups']])
chk(all(g['owner_comp_id'] in ('037', '001') for g in j['groups']), 'owner_comp_id 코드상수 부여')

# 13) 예약 key — 파생 task 가 기존 정적 task 를 가리는 그룹은 생성 거부(올마이트 R3)
for rk in ('VESSEL', 'RESEND'):
    r = c.post('/api/automation/soa/groups',
               json={'key': rk, 'label': '충돌', 'category': 'silver',
                     'mode': 'explicit', 'vessels': '', 'sort_order': 90})
    chk(r.status_code == 422 and '예약' in r.get_json()['error'], f'예약 key 거부: {rk}',
        r.get_data(as_text=True)[:140])
chk(shared_ns.automation_tasks()['soa_vessel'] == shared_ns.AUTOMATION_TASKS_BASE['soa_vessel'],
    '정적 soa_vessel 라벨 보존')

# 14) 감사 흔적 — 누가 언제 고쳤나
r = c.put('/api/automation/soa/groups/G2', json={'label': 'SOA 실버 G2', 'mode': 'explicit',
          'vessels': 'ATNH ATSH ATSL JATX', 'sort_order': 20, 'active': True})
g2 = next(g for g in c.get('/api/automation/soa/groups').get_json()['groups'] if g['key'] == 'G2')
chk(r.status_code == 200 and g2['updated_by'] == 'smoke', '수정자 기록', g2.get('updated_by'))
chk(bool(g2.get('updated_at')), '수정시각 기록', g2.get('updated_at'))

# 15) 삭제(비활성화와 별개) — 없는 key·실행중 차단·orphans 계산·task/ext 에서 제거
r = c.delete('/api/automation/soa/groups/NOPE')
chk(r.status_code == 422 and '찾을 수 없음' in r.get_json()['error'], '없는 그룹 삭제 422',
    r.get_data(as_text=True)[:140])

# 10)에서 soa_g4 를 queued 로 적재해 뒀음 → 삭제도 비활성화와 같은 게이트로 막혀야 함
v_blocked = c.get('/api/automation/soa/groups').get_json()['config_version']
n_blocked = A.query('SELECT COUNT(*) n FROM soa_group_vessel', one=True)['n']
r = c.delete('/api/automation/soa/groups/G4')
chk(r.status_code == 422 and '대기/진행중' in r.get_json()['error'], '실행 대기중 그룹 삭제 금지',
    r.get_data(as_text=True)[:140])
chk(any(g['key'] == 'G4' for g in c.get('/api/automation/soa/groups').get_json()['groups']),
    '차단된 삭제는 그룹 보존')
chk(c.get('/api/automation/soa/groups').get_json()['config_version'] == v_blocked,
    '차단된 삭제는 version 불변')
chk(A.query('SELECT COUNT(*) n FROM soa_group_vessel', one=True)['n'] == n_blocked,
    '차단된 삭제는 멤버십 불변')

# running 상태도 같은 게이트로 막히는지(러너가 이미 집어든 그룹)
r = c.post('/api/automation/soa/groups', json={'key': 'G8', 'label': '삭제 테스트2', 'category': 'silver',
           'mode': 'explicit', 'vessels': 'ATBG', 'sort_order': 96, 'active': False})
chk(r.get_json().get('ok') is True, 'G8(비활성) 생성', r.get_data(as_text=True)[:140])
A.execute("INSERT INTO automation_run (run_id,task,mode,status) VALUES ('t-run','soa_g8','verify','running')")
r = c.delete('/api/automation/soa/groups/G8')
chk(r.status_code == 422 and '대기/진행중' in r.get_json()['error'], 'running 그룹 삭제 금지',
    r.get_data(as_text=True)[:140])
A.execute("UPDATE automation_run SET status='done' WHERE run_id='t-run'")
# 비활성 그룹 삭제 = 이미 실행 대상이 아니었으므로 orphans 는 빈 리스트여야 함(false positive 금지)
r = c.delete('/api/automation/soa/groups/G8')
j = r.get_json()
chk(r.status_code == 200 and j['orphans'] == [], '비활성 그룹 삭제는 orphans 없음', j.get('orphans'))

# ATMT 를 G1 에서 빼 G9 로 옮긴 뒤 G9 삭제 → ATMT 가 어느 그룹에도 없어짐(orphans)
c.put('/api/automation/soa/groups/G1', json={'label': 'SOA 실버 G1', 'mode': 'explicit',
      'vessels': 'ATBG ATGR ATGV', 'sort_order': 10, 'active': True})
r = c.post('/api/automation/soa/groups', json={'key': 'G9', 'label': '삭제 테스트', 'category': 'silver',
           'mode': 'explicit', 'vessels': 'ATMT', 'sort_order': 95})
chk(r.get_json().get('ok') is True, 'G9 생성', r.get_data(as_text=True)[:140])
chk('soa_g9' in shared_ns.automation_tasks(), '삭제 전 task 존재')
# version 대조는 반드시 삭제 *직전* 값으로(앞선 PUT/POST bump 로 가짜 통과하지 않게 — 올마이트 R2)
v_before = c.get('/api/automation/soa/groups').get_json()['config_version']
r = c.delete('/api/automation/soa/groups/G9')
j = r.get_json()
chk(r.status_code == 200 and j['ok'] and j['deleted'] == 'G9', 'G9 삭제 200', r.get_data(as_text=True)[:140])
chk(j['orphans'] == ['ATMT'], '커버 잃는 선박 orphans 로 통보', j.get('orphans'))
chk(j['config_version'] == v_before + 1, '삭제도 config_version bump(직전 대비 +1)',
    (v_before, j.get('config_version')))
gj = c.get('/api/automation/soa/groups').get_json()
chk(all(g['key'] != 'G9' for g in gj['groups']), '목록에서 제거(비활성 아님)',
    [g['key'] for g in gj['groups']])
chk('soa_g9' not in shared_ns.automation_tasks(), '파생 task 제거')
chk(A.query("SELECT COUNT(*) n FROM soa_group_vessel WHERE group_id NOT IN "
            "(SELECT id FROM soa_group)", one=True)['n'] == 0, '멤버십 고아행 없음')
r = c.get('/api/ext/soa/groups', headers={'X-API-Key': apikey})
chk(r.status_code == 200 and all(g['key'] != 'G9' for g in r.get_json()['groups']),
    'ext 스냅샷에서도 제거', r.get_data(as_text=True)[:140])
# owner 불일치 선박만 가진 활성 그룹 삭제 → 실행 대상이 아니었으므로 orphans 없음
r = c.post('/api/automation/soa/groups', json={'key': 'G7', 'label': 'owner 불일치', 'category': 'silver',
           'mode': 'explicit', 'vessels': 'ZZZZ', 'sort_order': 97})
chk(r.get_json().get('ok') is True, 'G7 생성', r.get_data(as_text=True)[:140])
r = c.delete('/api/automation/soa/groups/G7')
chk(r.status_code == 200 and r.get_json()['orphans'] == [], 'owner 불일치 선박은 orphans 아님',
    r.get_data(as_text=True)[:140])

# 원복 — 이후 fail-closed 테스트가 정상 설정에서 출발하도록
c.put('/api/automation/soa/groups/G1', json={'label': 'SOA 실버 G1', 'mode': 'explicit',
      'vessels': 'ATBG ATGR ATGV ATMT', 'sort_order': 10, 'active': True})

# 12) 깨진 설정이면 ext 는 500(fail-closed) — 러너가 스냅샷 유지
db = A.sqlite3.connect(DB)
db.execute("UPDATE soa_group SET active=1 WHERE key='SKRT2'")
db.commit(); db.close()
r = c.get('/api/ext/soa/groups', headers={'X-API-Key': apikey})
chk(r.status_code == 500 and r.get_json()['ok'] is False, 'ext 불변식 위반 500', r.get_data(as_text=True)[:160])

print()
print(('❌ 실패 %d건: ' % len(fails)) + ', '.join(fails) if fails else '✅ 스모크 전항 통과')
os.unlink(DB)
sys.exit(1 if fails else 0)
