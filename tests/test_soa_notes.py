#!/usr/bin/env python3
"""SOA 적요 (new) 대기·불인정 목록 스모크 (2026-11-12) — ext push/pull·불인정·해제·검증."""
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
it = lambda seq, **kw: dict({'src_sx': 'CPPSCX2610270002', 'src_seq': seq, 'vsl': 'CPPS', 'pool': 'SKRT+CSM',
                             'exp_cd': '070309', 'summary': 'Starlink 월 사용료', 'sl_dm': '202610', 'sim': 0.3}, **kw)

chk(c.get('/api/automation/soa/notes').status_code in (302, 401, 403), '비로그인 조회 거부')
chk(c.post('/api/ext/soa/notes/pending', json={'items': []}).status_code in (401, 403), 'API 키 없으면 거부')
chk(c.post('/api/ext/soa/notes/pending', headers=H, json={'items': [it('0001', pool='SKRT+SKSM')]}).status_code == 400, 'pool 형식 위반 거부')
r = c.post('/api/ext/soa/notes/pending', headers=H, json={'items': [it('0001'), it('0002', summary='Turbo overhaul')]})
chk(r.status_code == 200 and r.get_json()['count'] == 2, '대기 2건 push')
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'
j = c.get('/api/automation/soa/notes').get_json()
chk(len(j['pending']) == 2 and j['rejected'] == [], '조회: 대기 2·불인정 0')
r = c.post('/api/automation/soa/notes/reject', json={'src_sx': 'CPPSCX2610270002', 'src_seq': '0002', 'reason': '선주 부담 아님'})
chk(r.status_code == 200, '불인정 등록')
j = c.get('/api/automation/soa/notes').get_json()
chk(len(j['pending']) == 1 and len(j['rejected']) == 1 and j['rejected'][0]['status'] == 'active', '대기→불인정 이동')
p = c.get('/api/ext/soa/notes/rejected', headers=H).get_json()
chk(p['ok'] and len(p['items']) == 1 and p['items'][0]['summary'] == 'Turbo overhaul', '러너 pull 활성 1건')
chk(c.post('/api/automation/soa/notes/reject', json={'src_sx': 'X', 'src_seq': '9'}).status_code == 404, '없는 대기 불인정 404')
rid = j['rejected'][0]['id']
chk(c.post(f'/api/automation/soa/notes/rejected/{rid}/release').status_code == 200, '해제')
chk(c.get('/api/ext/soa/notes/rejected', headers=H).get_json()['items'] == [], '해제 후 pull 0건')
chk(c.post(f'/api/automation/soa/notes/rejected/{rid}/release').status_code == 404, '중복 해제 404')
r = c.post('/api/ext/soa/notes/pending', headers=H, json={'items': []})
chk(c.get('/api/automation/soa/notes').get_json()['pending'] == [], '빈 push = 대기 전량 교체')

os.remove(DB)
print('FAIL' if fails else 'ALL OK', fails)
sys.exit(1 if fails else 0)
