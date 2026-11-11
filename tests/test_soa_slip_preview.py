#!/usr/bin/env python3
"""[전자결재] 미리보기·상신 게이트 스모크(2026-11-11) — 미리보기 push/조회, slip_submit 은 30분 이내 미리보기의 전표만."""
import os, sys, tempfile

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

fails = []
def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)

A.app.app_context().push()
c = A.app.test_client()
A.execute("INSERT OR REPLACE INTO api_settings (k,v) VALUES ('api_key','smokekey')")
H = {'X-API-Key': 'smokekey'}
ROW = {'sl_cd': 'SKRTCS2611110005', 'sx_cd': 'INPSCX2608040002', 'usd': 192534.45, 'subj': '2026-05, [INPS] SOA-Technical'}

chk(c.get('/api/automation/soa/slip-preview?group=SKRT').status_code in (302, 401, 403), '비로그인 조회 거부')
chk(c.post('/api/ext/soa/slip-preview', json={'group': 'SKRT', 'rows': []}).status_code in (401, 403), 'API 키 없으면 거부')
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

j = c.get('/api/automation/soa/slip-preview?group=SKRT').get_json()
chk(j['ok'] and j['rows'] == [] and not j['fresh'], '미리보기 없음 = fresh False')
for name, body in (('전표번호 형식', {'group': 'SKRT', 'rows': [dict(ROW, sl_cd='X;1')]}),
                   ('SOA번호 형식', {'group': 'SKRT', 'rows': [dict(ROW, sx_cd='bad')]}),
                   ('group 형식', {'group': 'S K', 'rows': []})):
    chk(c.post('/api/ext/soa/slip-preview', headers=H, json=body).status_code == 400, f'거부: {name}')
r = c.post('/api/ext/soa/slip-preview', headers=H, json={'group': 'SKRT', 'rows': [ROW]})
chk(r.status_code == 200 and r.get_json()['count'] == 1, '미리보기 push')
j = c.get('/api/automation/soa/slip-preview?group=SKRT').get_json()
chk(j['fresh'] and len(j['rows']) == 1 and j['total_usd'] == 192534.45, '조회: fresh·1건·합계')

def run(params=None, mode='slip_submit', task='soa_skrt'):
    return c.post('/api/automation/run', json={'task': task, 'mode': mode, **({'params': params} if params is not None else {})})
chk(run().status_code == 400, '목록 없이 상신 거부')
chk(run({'sl_cds': ['SKRTCS9999999999']}).status_code == 409, '미리보기에 없는 전표 거부')
EXP = {'SKRTCS2611110005': {'usd': 192534.45, 'sx_cd': 'INPSCX2608040002'}}
chk(run({'sl_cds': ['SKRTCS2611110005']}).status_code == 409, '확인 금액 없이 상신 거부')
chk(run({'sl_cds': ['SKRTCS2611110005'], 'expect': {'SKRTCS2611110005': {'usd': 1.0, 'sx_cd': 'INPSCX2608040002'}}}).status_code == 409, '확인 금액 불일치 거부')
chk(c.post('/api/ext/soa/slip-preview', headers=H, json={'group': 'X1', 'rows': [ROW] * 51}).status_code == 400, '미리보기 51건 거부(상한 50)')
chk(run({'sl_cds': ['bad']}).status_code == 400, '형식 위반 전표 거부')
chk(run(mode='slip_preview', task='soa_vessel').status_code == 400, '선박별 task 에 전자결재 금지')
r = run({'sl_cds': ['SKRTCS2611110005'], 'expect': EXP})
chk(r.status_code in (200, 409), '미리보기 전표 상신 요청 통과(킬스위치·락 409 허용)', r.get_data(as_text=True)[:120])
if r.status_code == 200:
    row = A.query("SELECT params FROM automation_run WHERE mode='slip_submit' ORDER BY rowid DESC LIMIT 1", one=True)
    chk(row is not None and 'SKRTCS2611110005' in (row['params'] or ''), '상신 run 에 확인 목록 저장')
# 30분 지난 미리보기는 거부
A.execute("UPDATE soa_slip_preview SET at='2000-01-01T00:00:00' WHERE group_key='SKRT'")
chk(run({'sl_cds': ['SKRTCS2611110005'], 'expect': EXP}).status_code == 409, '오래된 미리보기 거부')

print()
print(('❌ 실패 %d건: ' % len(fails)) + ', '.join(fails) if fails else '✅ 스모크 전항 통과')
os.unlink(DB)
sys.exit(1 if fails else 0)
