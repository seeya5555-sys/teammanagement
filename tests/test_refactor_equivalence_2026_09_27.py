#!/usr/bin/env python3
"""2026-09-27 기능불변 리팩터링 동등성 계약.

잠그는 것:
  ① /api/cs/surveys 일괄 집계 = 단건 _cs_survey_with_counts 결과와 동일, 쿼리 수가 survey 수와 무관.
  ② Dock Daily 목록 _project_responses = 프로젝트별 _project_response 와 동일(섹션 순서 포함).
  ③ 러너 claim 훅: routes_followup.enqueue_tracked 가 helpers_shared 훅 목록에 1회만 등록.
  ④ ext 진행경과 추가: 읽고-쓰는 사이 다른 곳 변경이 있으면 최신 원문 위에 append(삭제한 줄 부활 금지).
  ⑥ 보고서 편집권한 통합 함수 = 기존 dock/boarding 판정과 동일.
  ⑦ execute_rc 도 explicit transaction 플래그를 따른다(중간 commit 금지).
"""
import os, sys, json, tempfile, time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
A.DATABASE = DB
A.app.config['DATABASE'] = DB
A.app.config['TESTING'] = True
A.init_db(drop=False)
A._auto_migrate()

import helpers_shared
# 앱 기동(import app)만으로 훅이 등록돼 있어야 한다 — 테스트가 routes_followup 을 직접 import 하기 전에 확인
_COLD_HOOKS = [(h.__module__, h.__name__) for h in helpers_shared.AUTOMATION_CLAIM_HOOKS]
import routes_core, routes_dock_daily, routes_calendar_dock, routes_followup, app_core
from flask import g, session

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


A.app.app_context().push()
c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

VSL = A.execute("INSERT INTO vessels(name, active) VALUES('EQ VESSEL', 1)")

print('① CS 목록 일괄 집계')
sids = []
for q in (1, 2, 3, 4):
    sid = A.execute("INSERT INTO cs_surveys(vessel_id, year, quarter) VALUES(?, 2026, ?)", (VSL, q))
    sids.append(sid)
    for i, (cat, st) in enumerate([('Defect', 'Open'), ('Defect', 'Closed'), ('Observation', 'Open')][:q]):
        A.execute("INSERT INTO cs_findings(survey_id, category, no, status) VALUES(?,?,?,?)", (sid, cat, i + 1, st))
    for k in range(q - 1):
        A.execute("INSERT INTO cs_attachments(survey_id, filename, stored_name) VALUES(?,?,?)",
                  (sid, f'f{k}.pdf', f'eq-{sid}-{k}'))
A.execute("UPDATE cs_surveys SET manual_close_count=7 WHERE id=?", (sids[3],))

calls = {'n': 0}
_orig_q = routes_core.query
def _counting(*a, **k):
    calls['n'] += 1
    return _orig_q(*a, **k)
routes_core.query = _counting
r = c.get('/api/cs/surveys?year=2026')
list_calls = calls['n']
routes_core.query = _orig_q
chk(r.status_code == 200, '목록 200', r.status_code)
row = next(x for x in r.get_json() if x['vessel']['id'] == VSL)
for q, sid in zip((1, 2, 3, 4), sids):
    got = dict(row['surveys'][str(q)]); got.pop('findings', None)
    exp = dict(routes_core._cs_survey_with_counts(A.query('SELECT * FROM cs_surveys WHERE id=?', (sid,), one=True)))
    chk(got == exp, f'{q}Q 집계 = 단건 결과', f'{got} != {exp}')
chk(list_calls <= 6, f'쿼리 수 survey 수와 무관(실측 {list_calls})', list_calls)

print('② Dock Daily 목록 섹션 일괄')
pids = []
for t in ('EQ P1', 'EQ P2'):
    pid = A.execute("INSERT INTO dock_daily_project(vessel_id, title) VALUES(?, ?)", (VSL, t))
    pids.append(pid)
    for i, key in enumerate(['b_key', 'a_key', 'c_key']):
        A.execute("INSERT INTO dock_daily_section_def(project_id, section_key, label, sort_order, kind) "
                  "VALUES(?,?,?,?, 'fixed')", (pid, f'{key}{pid}', key, (i * 7) % 3))
rows = A.query("SELECT p.*, v.name vessel_name FROM dock_daily_project p JOIN vessels v ON v.id=p.vessel_id "
               "WHERE p.id IN (?, ?) ORDER BY p.id", tuple(pids))
chk(routes_dock_daily._project_responses(rows) == [routes_dock_daily._project_response(x) for x in rows],
    '일괄 = 프로젝트별(섹션 순서 포함)')
chk(routes_dock_daily._project_responses([]) == [], '빈 목록')

print('③ claim 훅 등록')
chk(helpers_shared.AUTOMATION_CLAIM_HOOKS.count(routes_followup.enqueue_tracked) == 1, 'enqueue_tracked 1회 등록')
chk(_COLD_HOOKS == [('routes_followup', 'enqueue_tracked')], 'import app 만으로 등록(cold start)', _COLD_HOOKS)
KEY = helpers_shared._get_api_key()
A.execute("INSERT INTO automation_run(run_id, task, mode, status) VALUES('eq-run-1', 'eq_task', 'verify', 'queued')")
def _boom():
    raise RuntimeError('hook failure')
_saved_hooks = list(helpers_shared.AUTOMATION_CLAIM_HOOKS)
helpers_shared.AUTOMATION_CLAIM_HOOKS[:] = [_boom]
r = c.post('/api/ext/automation/claim', headers={'X-API-Key': KEY})
helpers_shared.AUTOMATION_CLAIM_HOOKS[:] = _saved_hooks
j = r.get_json() or {}
st = A.query("SELECT status FROM automation_run WHERE run_id='eq-run-1'", one=True)['status']
chk(r.status_code == 200 and st != 'queued', '훅 예외여도 claim 은 큐를 처리', f'{r.status_code} {j} status={st}')
import importlib
src = open('routes_dock_submit.py', encoding='utf-8').read()
chk('from routes_followup' not in src and 'import routes_followup' not in src, 'Blueprint 간 직접 import 없음')

print('④ ext 진행경과 추가 CAS')
key = helpers_shared._get_api_key() if hasattr(helpers_shared, '_get_api_key') else None
SUP = A.execute("INSERT INTO supervisors(name) VALUES('EQ SUP')")
iid = A.execute("INSERT INTO issues(supervisor_id, vessel_id, issue_date, item_topic, actions, priority, status) "
                "VALUES(?, ?, '2026-09-27', 'EQ', ?, 'Normal', 'Open')",
                (SUP, VSL, json.dumps([{'date': '2026-09-01', 'progress': 'A', 'important': False},
                                  {'date': '2026-09-02', 'progress': 'B', 'important': False}])))
_orig_rc = routes_calendar_dock.execute_rc
state = {'first': True}
def _racy_rc(sql, params=()):
    # 첫 쓰기 직전 "다른 기기"가 B 를 지운 상황 재현 → CAS 가 밀려야 한다
    if state['first'] and 'UPDATE issues SET actions' in sql:
        state['first'] = False
        A.execute("UPDATE issues SET actions=? WHERE id=?",
                  (json.dumps([{'date': '2026-09-01', 'progress': 'A', 'important': False}]), iid))
    return _orig_rc(sql, params)
routes_calendar_dock.execute_rc = _racy_rc
with A.app.test_request_context(f'/api/ext/issues/{iid}/actions', method='POST',
                                json={'progress': 'C', 'date': '2026-09-03'}):
    resp = routes_calendar_dock.api_ext_issue_add_action.__wrapped__(iid) \
        if hasattr(routes_calendar_dock.api_ext_issue_add_action, '__wrapped__') else None
routes_calendar_dock.execute_rc = _orig_rc
acts = json.loads(A.query('SELECT actions FROM issues WHERE id=?', (iid,), one=True)['actions'])
chk(resp is not None, 'ext 핸들러 호출(가드 우회 테스트 경로)')
chk([a['progress'] for a in acts] == ['A', 'C'], '삭제된 B 가 되살아나지 않음', [a['progress'] for a in acts])

print('⑥ 보고서 편집권한 통합')
S5 = A.execute("INSERT INTO supervisors(name) VALUES('EQ S5')")
S6 = A.execute("INSERT INTO supervisors(name) VALUES('EQ S6')")
with A.app.test_request_context('/'):
    for table, fn in (('dock_reports', routes_calendar_dock._can_edit_dock_report),
                      ('boarding_reports', routes_calendar_dock._can_edit_boarding_report)):
        rid = A.execute(f"INSERT INTO {table}(vessel_id, supervisor_id, title, status, created_by) "
                        f"VALUES(?, ?, 'EQ', 'draft', 'x')", (VSL, S5))
        session['role'] = 'member'; session['supervisor_id'] = S5
        chk(fn(rid) is True and fn({'supervisor_id': S5}) is True, f'{table}: 담당 감독 True')
        session['supervisor_id'] = S6
        chk(fn(rid) is False, f'{table}: 타 감독 False')
        chk(fn(999999) is False, f'{table}: 없는 id False')
        session['supervisor_id'] = None
        chk(fn(rid) is False, f'{table}: 감독 미연결 False')
        session['role'] = 'admin'
        chk(fn(rid) is True, f'{table}: admin True')

print('⑦ explicit transaction 플래그')
with A.app.test_request_context('/'):
    db = app_core.get_db()
    g._vessel_purge_transaction = True
    app_core.execute_rc("UPDATE vessels SET name='EQ TX' WHERE id=?", (VSL,))
    chk(db.in_transaction, 'purge 묶음 중 execute_rc 는 commit 하지 않음')
    db.rollback()
    g.pop('_vessel_purge_transaction', None)
    app_core.execute_rc("UPDATE vessels SET name='EQ TX2' WHERE id=?", (VSL,))
    chk(not db.in_transaction, '평소엔 즉시 commit(기존 동작)')

os.unlink(DB)
print()
if fails:
    print(f'❌ {len(fails)} 실패: ' + ', '.join(fails))
    sys.exit(1)
print('✅ 전부 통과')
