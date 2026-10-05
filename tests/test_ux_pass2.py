#!/usr/bin/env python3
"""UX 2차 패스(2026-10-05) 서버 계약.

잠그는 것:
  ① PUT /api/daily-mail/settings/<vid> 부분 수정 — 보낸 키만 바뀌고 CC·enabled·To 는 유지
     (예전엔 enabled 누락 = OFF, cc 누락 = 비움).
  ② POST /api/shipwiki/cards/<cid>/decide partial=true — 보낸 키만 갱신, 결정·나머지 유지.
     전체 모드(partial 없음)는 기존 동작 그대로.

실행: ./run_tests.sh  (또는 PYTHONPATH=. python tests/test_ux_pass2.py)
"""
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
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

print('① Daily mail 설정 부분 수정')
VSL = A.execute("INSERT INTO vessels(name, active) VALUES('UX2 VESSEL', 1)")
URL = f'/api/daily-mail/settings/{VSL}'
r = c.put(URL, json={'to_emails': 'a@x.com', 'cc_emails': 'cc@x.com', 'enabled': True, 'dear_name': 'Capt'})
chk(r.status_code == 200, '전체 저장 200', r.get_data(as_text=True))


def row():
    return A.query('SELECT * FROM daily_mail_settings WHERE vessel_id=?', (VSL,), one=True)


r = c.put(URL, json={'to_emails': 'b@x.com'})
chk(r.status_code == 200, 'To 만 보내도 200', r.get_data(as_text=True))
x = row()
chk(x['to_emails'] == 'b@x.com', 'To 갱신')
chk(x['cc_emails'] == 'cc@x.com', 'CC 유지')
chk(x['enabled'] == 1, 'enabled 유지(누락 ≠ OFF)')
chk(x['dear_name'] == 'Capt', 'Dear 유지')

r = c.put(URL, json={'cc_emails': ''})
x = row()
chk(r.status_code == 200 and x['cc_emails'] == '' and x['to_emails'] == 'b@x.com' and x['enabled'] == 1,
    "명시적 cc '' 만 비움, 나머지 유지")

r = c.put(URL, json={'enabled': False})
x = row()
chk(x['enabled'] == 0 and x['to_emails'] == 'b@x.com', '명시적 enabled=false 는 OFF, To 유지')

r = c.put(URL, json={'to_emails': ''})
chk(r.status_code == 200 and row()['to_emails'] == '', 'OFF 상태에서 To 비우기 허용')
r = c.put(URL, json={'enabled': True})
chk(r.status_code == 400, 'To 없이 ON 은 여전히 400')

print('② Shipwiki decide partial')
CID = A.execute("INSERT INTO shipwiki_card(slug, ship_nm, fname, tier, title, category, card_status) "
                "VALUES('ux2-ship','UX2','f1','pending','원제목','DEFECT','open')")
DURL = f'/api/shipwiki/cards/{CID}/decide'
r = c.post(DURL, json={'decision': 'promote', 'new_title': '승격제목', 'new_category': 'AOR',
                       'new_conf': 'high', 'decided_judgment': '판단'})
chk(r.status_code == 200, '전체 결정 200', r.get_data(as_text=True))


def card():
    return A.query('SELECT * FROM shipwiki_card WHERE id=?', (CID,), one=True)


r = c.post(DURL, json={'partial': True, 'new_title': '고친제목'})
k = card()
chk(r.status_code == 200, 'partial 200', r.get_data(as_text=True))
chk(k['new_title'] == '고친제목', 'partial: 제목 갱신')
chk(k['new_category'] == 'AOR' and k['new_conf'] == 'high' and k['decided_judgment'] == '판단',
    'partial: 카테고리·신뢰도·판단 유지')
chk(k['decision'] == 'promote' and k['card_status'] == 'decided', 'partial: 결정·상태 유지')

r = c.post(DURL, json={'partial': True, 'new_category': 'DOCK'})
k = card()
chk(k['new_category'] == 'DOCK' and k['new_title'] == '고친제목', 'partial: 카테고리만 갱신')

r = c.post(DURL, json={'partial': True})
chk(r.status_code == 400, 'partial 빈 요청 400')
r = c.post(DURL, json={'partial': True, 'decision': 'bogus'})
chk(r.status_code == 400 and card()['decision'] == 'promote', 'partial 잘못된 decision 400 + 무변경')

r = c.post(DURL, json={'decision': 'promote'})
k = card()
chk(k['new_title'] == '원제목' and k['new_category'] == 'DEFECT' and k['new_conf'] == 'medium',
    '전체 모드는 기존대로 덮어씀(누락 = 원값/기본)')

# CAS: 읽은 스냅샷과 DB가 다르면(동시 저장·맥 상태전이) 덮어쓰지 않고 409
import routes_dock_submit as RDS
_orig_q = RDS.query
_stale = dict(card())
A.execute("UPDATE shipwiki_card SET new_category='RACE', card_status='applying' WHERE id=?", (CID,))
RDS.query = lambda sql, params=(), one=False: _stale if (one and 'SELECT * FROM shipwiki_card' in sql) else _orig_q(sql, params, one=one)
try:
    r = c.post(DURL, json={'partial': True, 'new_title': '경합'})
finally:
    RDS.query = _orig_q
k = card()
chk(r.status_code == 409 and k['new_category'] == 'RACE' and k['card_status'] == 'applying' and k['new_title'] == '원제목',
    'partial CAS 충돌 409 + 다른 변경·상태전이 보존')
A.execute("UPDATE shipwiki_card SET card_status='applied' WHERE id=?", (CID,))
r = c.post(DURL, json={'partial': True, 'new_title': 'x'})
chk(r.status_code == 409 and card()['new_title'] == '원제목', '적용완료 카드 partial = 409')

os.unlink(DB)
print()
if fails:
    print(f'❌ {len(fails)} 실패: ' + ', '.join(fails))
    sys.exit(1)
print('✅ 전부 통과')
