#!/usr/bin/env python3
"""업무요약은 Open/진행중만 Gemini 갱신하고 완료 본문은 저장본을 보존한다.

실행: ~/.venvs/trmt-test/bin/python tests/test_issue_summary_active_only.py
"""
import json
import os
import sys
import tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
A.app.app_context().push()

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  FAIL  ') + name + (f' - {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


sup = A.execute("INSERT INTO supervisors(name) VALUES('SUMMARY SUP')")
vsl = A.execute("INSERT INTO vessels(name) VALUES('SUMMARY VESSEL')")


def issue(topic, status, desc, progress):
    actions = json.dumps([{'date': '2026-08-24', 'progress': progress}], ensure_ascii=False)
    return A.execute(
        "INSERT INTO issues(supervisor_id,vessel_id,issue_date,item_topic,description,actions,priority,status) "
        "VALUES(?,?,'2026-08-01',?,?,?,?,?)",
        (sup, vsl, topic, desc, actions, 'Normal', status))


open_id = issue('OPEN ITEM', 'Open', 'open raw', 'open action')
progress_id = issue('PROGRESS ITEM', 'InProgress', 'progress raw', 'progress action')
closed_id = issue('CLOSED ITEM', 'Closed', 'closed changed raw', 'closed changed action')

previous_issue = '[8/1] CLOSED ITEM\n1) 이전 완료 요약\n2) 이전 조치 요약'
all_scope_issue = '[8/1] CLOSED ITEM\n1) 전체 scope의 더 오래된 요약'
shared_ns._ensure_summary_table()
A.execute(
    "INSERT INTO issue_summaries(scope,data,generated_at) VALUES(?,?,?)",
    (str(sup), json.dumps([{
        'no': 1, 'issue_id': closed_id, 'item': 'CLOSED ITEM',
        'supervisor_id': sup, 'vessel_id': vsl, 'vessel_name': 'SUMMARY VESSEL',
        'vessel_type': '', 'issue': previous_issue, 'priority': 'Urgent',
        'status_raw': 'Closed', 'status': 'Closed',
    }], ensure_ascii=False), '2026-08-23 18:00'))
A.execute(
    "INSERT INTO issue_summaries(scope,data,generated_at) VALUES('all',?,?)",
    (json.dumps([{'issue_id': closed_id, 'issue': all_scope_issue}], ensure_ascii=False),
     '2026-08-22 18:00'))

seen = []
original = shared_ns._gen_issue_summaries


def fake_gemini(payload):
    seen.extend(payload)
    return {p['i']: {'desc': 'NEW ' + p['description'], 'action': 'NEW ' + p['action']}
            for p in payload}


shared_ns._gen_issue_summaries = fake_gemini
try:
    rows, _, counts = shared_ns._run_summary_generate(str(sup))
finally:
    shared_ns._gen_issue_summaries = original

print('\n[1] Gemini 입력 범위')
chk(len(seen) == 2, 'Open + InProgress 2건만 전달', seen)
chk({p['description'] for p in seen} == {'open raw', 'progress raw'}, 'Closed 원문은 입력 0건', seen)

print('\n[2] 저장·응답 병합')
by_id = {r['issue_id']: r for r in rows}
chk(by_id[open_id]['issue'].find('NEW open raw') >= 0, 'Open 새 요약 반영')
chk(by_id[progress_id]['issue'].find('NEW progress raw') >= 0, 'InProgress 새 요약 반영')
chk(by_id[closed_id]['issue'] == previous_issue, 'Closed 이전 본문 글자 그대로 보존', by_id[closed_id]['issue'])
chk(by_id[closed_id]['issue'] != all_scope_issue, '현재 감독 scope 저장본이 all fallback보다 우선')
chk(by_id[closed_id]['priority'] == 'Normal', 'Closed 표시 메타데이터는 현재값')
chk(by_id[closed_id]['status_raw'] == 'Closed', 'Closed 현재 상태 유지')
chk(counts == {str(sup): 3}, 'count 계약 유지', counts)

stored = A.query('SELECT data FROM issue_summaries WHERE scope=?', (str(sup),), one=True)
stored_rows = json.loads(stored['data'])
chk([r['no'] for r in stored_rows] == [1, 2, 3], '저장 No 재번호 계약 유지')
chk(next(r for r in stored_rows if r['issue_id'] == closed_id)['issue'] == previous_issue,
    'DB에도 Closed 이전 본문 보존')

print('\n[3] 이전 저장본이 없는 Closed는 AI 없이 원문 fallback')
new_closed = issue('NEW CLOSED', 'Closed', 'first closed raw', 'first closed action')
seen.clear()
shared_ns._gen_issue_summaries = fake_gemini
try:
    rows2, _, _ = shared_ns._run_summary_generate(str(sup))
finally:
    shared_ns._gen_issue_summaries = original
new_row = next(r for r in rows2 if r['issue_id'] == new_closed)
chk(all(p['description'] != 'first closed raw' for p in seen), '신규 Closed도 Gemini 입력 제외')
chk('first closed raw' in new_row['issue'], '저장본 없으면 결정적 원문 fallback')

print('\n[4] 감독 재배정 Closed는 all scope 저장본으로 보존')
sup2 = A.execute("INSERT INTO supervisors(name) VALUES('SUMMARY SUP 2')")
moved_id = issue('MOVED CLOSED', 'Closed', 'moved raw', 'moved action')
A.execute('UPDATE issues SET supervisor_id=? WHERE id=?', (sup2, moved_id))
moved_previous = '[8/1] MOVED CLOSED\n1) 재배정 전 완료 요약'
all_rows = json.loads(A.query("SELECT data FROM issue_summaries WHERE scope='all'", one=True)['data'])
all_rows.append({'issue_id': moved_id, 'issue': moved_previous})
A.execute("UPDATE issue_summaries SET data=? WHERE scope='all'",
          (json.dumps(all_rows, ensure_ascii=False),))
seen.clear()
shared_ns._gen_issue_summaries = fake_gemini
try:
    moved_rows, _, _ = shared_ns._run_summary_generate(str(sup2))
finally:
    shared_ns._gen_issue_summaries = original
chk(seen == [], '재배정 Closed도 Gemini 입력 0건', seen)
chk(len(moved_rows) == 1 and moved_rows[0]['issue'] == moved_previous,
    '새 감독 scope에 all 저장본 복원', moved_rows)

print('\n[5] 변경 없는 항목은 이전 요약 재사용, 변경 항목만 재요약')


def run(scope):
    seen.clear()
    shared_ns._gen_issue_summaries = fake_gemini
    try:
        out, _, _ = shared_ns._run_summary_generate(scope)
    finally:
        shared_ns._gen_issue_summaries = original
    return {r['issue_id']: r for r in out}


base = run(str(sup))
base = run(str(sup))
chk(seen == [], '변경 0건이면 Gemini 호출 0건', seen)
chk(base[open_id]['issue'].find('NEW open raw') >= 0, '재사용 요약 유지')

A.execute('UPDATE issues SET actions=? WHERE id=?',
          (json.dumps([{'date': '2026-08-25', 'progress': 'open action 2'}]), open_id))
after = run(str(sup))
chk([p['description'] for p in seen] == ['open raw'], '진행사항 추가 항목만 재요약', seen)
chk(after[progress_id]['issue'] == base[progress_id]['issue'], '미변경 항목 본문 그대로')

A.execute("UPDATE issues SET description='progress raw v2' WHERE id=?", (progress_id,))
run(str(sup))
chk([p['description'] for p in seen] == ['progress raw v2'], '상세내용 수정 항목만 재요약', seen)

A.execute("UPDATE issues SET status='InProgress' WHERE id=?", (open_id,))
run(str(sup))
chk([p['description'] for p in seen] == ['open raw'], 'Status 변경 항목만 재요약', seen)

new_open = issue('NEW OPEN', 'Open', 'brand new raw', 'brand new action')
run(str(sup))
chk([p['description'] for p in seen] == ['brand new raw'], '신규 항목만 요약', seen)

A.execute("UPDATE issues SET priority='Urgent' WHERE id=?", (progress_id,))
pr = run(str(sup))
chk(seen == [] and pr[progress_id]['priority'] == 'Urgent', '우선순위만 변경=재요약 없이 표시값 갱신', seen)

failed_id = issue('FAIL ITEM', 'Open', 'fail raw', 'fail action')
seen.clear()
shared_ns._gen_issue_summaries = lambda payload: (seen.extend(payload), {})[1]
try:
    shared_ns._run_summary_generate(str(sup))
finally:
    shared_ns._gen_issue_summaries = original
run(str(sup))
chk([p['description'] for p in seen] == ['fail raw'], 'AI 실패 항목은 다음 클릭에 재시도', seen)

print('\n[6] scope 간 저장본: 지문 일치본 선택·현재 scope 본문 우선')
own = run(str(sup))
rows_all = json.loads(A.query("SELECT data FROM issue_summaries WHERE scope='all'", one=True)['data'])
rows_all = [r for r in rows_all if r.get('issue_id') != progress_id]
rows_all.append({'issue_id': progress_id, 'issue': 'STALE', 'src_hash': 'old'})
rows_all.append({'issue_id': progress_id, 'issue': 'OTHER SAME HASH',
                 'src_hash': own[progress_id]['src_hash']})
A.execute("UPDATE issue_summaries SET data=? WHERE scope='all'",
          (json.dumps(rows_all, ensure_ascii=False),))
again = run(str(sup))
chk(seen == [], '다른 scope의 오래된 지문 때문에 재요약하지 않음', seen)
chk(again[progress_id]['issue'] == own[progress_id]['issue'], '같은 지문이면 현재 scope 본문 유지')

print('\n[7] 재요약/유지 건수 반환')
st = {}
shared_ns._gen_issue_summaries = fake_gemini
try:
    out7, _, _ = shared_ns._run_summary_generate(str(sup), st)
finally:
    shared_ns._gen_issue_summaries = original
chk(st == {'resummarized': 0, 'reused': len(out7)}, '변경 0건이면 재요약 0', st)
A.execute("UPDATE issues SET description='progress raw v3' WHERE id=?", (progress_id,))
st = {}
shared_ns._gen_issue_summaries = fake_gemini
try:
    out7, _, _ = shared_ns._run_summary_generate(str(sup), st)
finally:
    shared_ns._gen_issue_summaries = original
chk(st == {'resummarized': 1, 'reused': len(out7) - 1}, '변경 1건이면 재요약 1', st)


print('\n[8] scope별 건수 DB 저장')
row8 = A.query('SELECT stats FROM issue_summaries WHERE scope=?', (str(sup),), one=True)
chk(row8 and json.loads(row8['stats']) == st, 'scope stats 저장=반환값', row8 and row8['stats'])

try:
    os.unlink(DB)
except OSError:
    pass

if fails:
    raise SystemExit(f'{len(fails)} failed: {fails}')
print('\nALL PASS')
