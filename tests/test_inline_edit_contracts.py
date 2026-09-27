#!/usr/bin/env python3
"""화면 클릭 인라인 편집(2026-09-27) 서버 계약.

잠그는 것:
  ① `DELETE /api/issues/<id>/actions/<idx>` — 대상 1건만 삭제, 나머지 진행 무변경.
  ② prev(date/progress[/important]) 불일치 = 409 + DB 무변경(엉뚱한 줄 삭제 차단). prev 없음 = 400.
  ③ 범위 밖 index = 409 · 남의 담당 현안 = 403.
  ④ 편집 모달이 보내는 `is_template` 이 dock/boarding 보고서 PUT 에 반영(예전엔 조용히 무시) +
     해제 시 template_name 비움.
  ⑤ Class Status 부분수정: 한 칸만 보내면 그 칸만 바뀐다(인라인 셀 저장 경로).
  ⑥ 공용 inline_edit.js 가 base.html 에서 로드된다(모든 탭 공용 모듈).

실행: ./run_tests.sh  (또는 PYTHONPATH=. python tests/test_inline_edit_contracts.py)
"""
import os, sys, json, tempfile

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

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


A.app.app_context().push()
c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

SUP = A.execute("INSERT INTO supervisors(name) VALUES('IE SUP')")
VSL = A.execute("INSERT INTO vessels(name) VALUES('IE VESSEL')")
BASE = [
    {'date': '2026-09-01', 'progress': '첫 진행', 'important': False},
    {'date': '2026-09-02', 'progress': '둘째 진행', 'important': True},
    {'date': '2026-09-03', 'progress': '셋째 진행', 'important': False},
]


def mkissue():
    return A.execute(
        "INSERT INTO issues(supervisor_id, vessel_id, issue_date, item_topic, description, "
        "actions, priority, status) VALUES(?,?,'2026-09-01','IE TOPIC','',?, 'Normal','Open')",
        (SUP, VSL, json.dumps(BASE, ensure_ascii=False)))


def acts_of(iid):
    raw = shared_ns.query('SELECT actions FROM issues WHERE id=?', (iid,), one=True)['actions']
    return json.loads(raw) if raw else []


def delete(iid, idx, body):
    return c.delete(f'/api/issues/{iid}/actions/{idx}', json=body)


print('① 1건만 삭제')
iid = mkissue()
r = delete(iid, 1, {'prev': {'date': '2026-09-02', 'progress': '둘째 진행', 'important': True}})
chk(r.status_code == 200, '삭제 200', f'{r.status_code} {r.get_data(as_text=True)[:120]}')
chk(acts_of(iid) == [BASE[0], BASE[2]], '나머지 진행은 글자 그대로', str(acts_of(iid)))
chk((r.get_json() or {}).get('actions') == [BASE[0], BASE[2]], '응답에 서버 정본 actions')

print('② prev 대조 fail-closed')
iid = mkissue()
r = delete(iid, 1, {'prev': {'date': '2026-09-02', 'progress': '다른 내용'}})
chk(r.status_code == 409, 'progress 불일치 = 409', f'{r.status_code}')
r = delete(iid, 1, {'prev': {'date': '2026-09-02', 'progress': '둘째 진행', 'important': False}})
chk(r.status_code == 409, 'important 불일치 = 409', f'{r.status_code}')
r = delete(iid, 1, {})
chk(r.status_code == 400, 'prev 없음 = 400', f'{r.status_code}')
chk(acts_of(iid) == BASE, '거부된 요청은 DB 무변경')

print('②-b 같은 내용 줄 연속 + 다른 곳 삭제 → 개수 대조로 옆 줄 보호')
DUP = [{'date': '2026-09-05', 'progress': '같은 줄', 'important': False}] * 2 + [BASE[2]]
iid2 = A.execute(
    "INSERT INTO issues(supervisor_id, vessel_id, issue_date, item_topic, description, "
    "actions, priority, status) VALUES(?,?,'2026-09-01','IE DUP','',?, 'Normal','Open')",
    (SUP, VSL, json.dumps(DUP[1:], ensure_ascii=False)))   # 다른 기기가 첫 줄을 이미 지운 상태
stale = {'date': '2026-09-05', 'progress': '같은 줄', 'important': False, 'count': 3}
r = delete(iid2, 0, {'prev': stale})
chk(r.status_code == 409, 'stale count 삭제 = 409', f'{r.status_code}')
r = c.patch(f'/api/issues/{iid2}/actions/0', json={'progress': 'x', 'prev': stale})
chk(r.status_code == 409, 'stale count 수정 = 409', f'{r.status_code}')
chk(acts_of(iid2) == DUP[1:], '옆 줄 무변경')
r = delete(iid2, 0, {'prev': dict(stale, count=2)})
chk(r.status_code == 200 and acts_of(iid2) == [BASE[2]], '개수 일치하면 정상 삭제', f'{r.status_code}')

print('③ 범위 밖 / 권한')
r = delete(iid, 9, {'prev': {'date': '', 'progress': ''}})
chk(r.status_code == 409, '범위 밖 index = 409', f'{r.status_code}')
OTHER = A.execute("INSERT INTO supervisors(name) VALUES('IE OTHER')")
A.execute("INSERT INTO users(username,password_hash,display_name,supervisor_id,role,active) "
          "VALUES('ie-member','x','ie-member',?,?,1)", (OTHER, 'member'))
MEM = A.query("SELECT id FROM users WHERE username='ie-member'", one=True)['id']
with c.session_transaction() as s:
    s['user_id'] = MEM; s['role'] = 'member'; s['supervisor_id'] = OTHER
r = delete(iid, 0, {'prev': {'date': '2026-09-01', 'progress': '첫 진행'}})
chk(r.status_code == 403, '남의 담당 현안 = 403', f'{r.status_code}')
chk(acts_of(iid) == BASE, '403 은 DB 무변경')
with c.session_transaction() as s:
    s['user_id'] = 1; s['role'] = 'admin'; s['supervisor_id'] = None

print('④ 보고서 is_template 반영')
for table, url in (('dock_reports', '/api/dock-reports'), ('boarding_reports', '/api/boarding-reports')):
    rid = A.execute(f"INSERT INTO {table}(vessel_id, supervisor_id, title, status, is_template, created_by) "
                    f"VALUES(?,?,'IE REPORT','draft',0,'smoke')", (VSL, SUP))
    r = c.put(f'{url}/{rid}', json={'is_template': True, 'template_name': 'IE 템플릿'})
    row = shared_ns.query(f'SELECT is_template, template_name FROM {table} WHERE id=?', (rid,), one=True)
    chk(r.status_code == 200 and row['is_template'] == 1 and row['template_name'] == 'IE 템플릿',
        f'{table}: 템플릿 켜기 반영', f'{r.status_code} {dict(row)}')
    r = c.put(f'{url}/{rid}', json={'is_template': False})
    row = shared_ns.query(f'SELECT is_template, template_name FROM {table} WHERE id=?', (rid,), one=True)
    chk(row['is_template'] == 0 and row['template_name'] is None, f'{table}: 해제 시 이름도 비움', str(dict(row)))
    r = c.put(f'{url}/{rid}', json={'title': 'IE 제목만'})
    row = shared_ns.query(f'SELECT title, is_template FROM {table} WHERE id=?', (rid,), one=True)
    chk(row['title'] == 'IE 제목만' and row['is_template'] == 0, f'{table}: 제목 단독 수정은 템플릿 불변')

print('⑤ Class Status 한 칸 부분수정')
csid = A.execute("INSERT INTO class_status(vessel_id, vessel_name_raw) VALUES(?, 'IE VESSEL')", (VSL,))
itid = A.execute("INSERT INTO class_status_items(cs_id, category, no, description, remark, action_taken) "
                 "VALUES(?, 'COC', '1', 'desc', 'rmk', 'old act')", (csid,))
r = c.put(f'/api/class-status/items/{itid}', json={'action_taken': 'new act'})
row = shared_ns.query('SELECT description, remark, action_taken FROM class_status_items WHERE id=?', (itid,), one=True)
chk(r.status_code == 200 and row['action_taken'] == 'new act' and row['description'] == 'desc' and row['remark'] == 'rmk',
    '조치사항만 바뀜', str(dict(row)))

print('⑤-b 인보이스 수정저장: 적요만 보내면 EXP·INV_DT 보존(돈 경로)')
cols = [r['name'] for r in shared_ns.query('PRAGMA table_info(invoice_draft)')]
if cols:
    need = {'status': 'pending', 'subject': 'old subj', 'exp_cd': 'E100', 'exp_nm': 'OLD', 'inv_dt': '20260901', 'raw_card': '{}'}
    use = {k: v for k, v in need.items() if k in cols}
    for r0 in shared_ns.query('PRAGMA table_info(invoice_draft)'):   # 나머지 NOT NULL 칸은 더미로 채움
        if r0['notnull'] and r0['dflt_value'] is None and not r0['pk'] and r0['name'] not in use:
            use[r0['name']] = 'IE-' + r0['name'] if 'CHAR' in (r0['type'] or 'TEXT').upper() or not r0['type'] or 'TEXT' in r0['type'].upper() else 0
    did = A.execute(f"INSERT INTO invoice_draft({','.join(use)}) VALUES({','.join('?'*len(use))})", tuple(use.values()))
    r = c.post(f'/api/invoice/drafts/{did}/edit', json={'subject': 'new subj'})
    row = shared_ns.query('SELECT subject, exp_cd, inv_dt FROM invoice_draft WHERE id=?', (did,), one=True)
    chk(r.status_code == 200 and row['subject'] == 'new subj' and row['exp_cd'] == 'E100' and row['inv_dt'] == '20260901',
        '적요만 변경 · EXP/INV_DT 그대로', f'{r.status_code} {dict(row)}')
else:
    chk(False, 'invoice_draft 테이블 존재')

print('⑥ 공용 모듈 로드')
base = open('templates/base.html', encoding='utf-8').read()
chk("js/inline_edit.js" in base, 'base.html 이 inline_edit.js 로드')
js = open('static/js/inline_edit.js', encoding='utf-8').read()
chk('isComposing' in js, '한글 조합 중 Enter 무시 가드 존재')

os.unlink(DB)
print()
if fails:
    print(f'❌ {len(fails)} 실패: ' + ', '.join(fails))
    sys.exit(1)
print('✅ 전부 통과')
