#!/usr/bin/env python3
"""SOA 자동승인 진행표(soa_gate_state) 스모크 — ext push(그룹 단위 교체)·검증·화면용 조회."""
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
def row(sx, **kw):
    r = {'sx_cd': sx, 'vsl_cd': sx[:4], 'sl_tp': '05', 'sl_dm': '202610', 's1_status': 'ok', 's1_note': '전 라인 Confirm(3)',
         's1_at': '2026-11-10 19:00', 's2_status': 'ok', 's2_note': '지적 없음', 's2_at': '2026-11-10 19:02'}
    r.update(kw); return r

chk(c.get('/api/automation/soa/gate-state').status_code in (302, 401, 403), '비로그인 조회 거부')
chk(c.post('/api/ext/soa/gate-state', json={'group': 'G1', 'rows': []}).status_code in (401, 403), 'API 키 없으면 거부')
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

r = c.post('/api/ext/soa/gate-state', headers=H, json={'group': 'G1', 'rows': [
    row('ATGRCX2610010001', ap_status='approved', ap_note='승인', ap_at='2026-11-10 19:05'),
    row('ATGVCX2610010002', s2_status='finding', s2_note='지적 1라인(s0003)')]})
chk(r.status_code == 200 and r.get_json()['count'] == 2, 'G1 2행 push')
c.post('/api/ext/soa/gate-state', headers=H, json={'group': 'SKRT', 'rows': [row('CPPSCX2610270002', s1_status='human')]})
j = c.get('/api/automation/soa/gate-state').get_json()
chk(j['ok'] and len(j['rows']) == 3, '조회 3행', str(j)[:200])
chk({x['group_key'] for x in j['rows']} == {'G1', 'SKRT'}, '그룹 구분')

r = c.post('/api/ext/soa/gate-state', headers=H, json={'group': 'G1', 'rows': [row('ATGVCX2610010002')]})
j = c.get('/api/automation/soa/gate-state').get_json()
sx = sorted(x['sx_cd'] for x in j['rows'])
chk(sx == ['ATGVCX2610010002', 'CPPSCX2610270002'], '그룹 단위 교체(승인된 SOA 제거 · 다른 그룹 보존)', str(sx))
c.post('/api/ext/soa/gate-state', headers=H, json={'group': 'G1', 'rows': []})
chk(len(c.get('/api/automation/soa/gate-state').get_json()['rows']) == 1, '빈 rows = 그 그룹 비움')

for name, body in (('group 형식', {'group': 'g 1;', 'rows': []}), ('rows 누락', {'group': 'G1'}),
                   ('SOA번호 형식', {'group': 'G1', 'rows': [row('x')]}),
                   ('선박코드 불일치', {'group': 'G1', 'rows': [row('ATGRCX2610010001', vsl_cd='ZZZZ')]}),
                   ('상태값 위반', {'group': 'G1', 'rows': [row('ATGRCX2610010001', s1_status='approved')]}),
                   ('승인 상태값 위반', {'group': 'G1', 'rows': [row('ATGRCX2610010001', ap_status='ok')]})):
    chk(c.post('/api/ext/soa/gate-state', headers=H, json=body).status_code == 400, f'거부: {name}')
chk(len(c.get('/api/automation/soa/gate-state').get_json()['rows']) == 1, '거부된 요청은 기존 행 보존')

print()
print(('❌ 실패 %d건: ' % len(fails)) + ', '.join(fails) if fails else '✅ 스모크 전항 통과')
os.unlink(DB)
sys.exit(1 if fails else 0)
