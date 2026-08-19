#!/usr/bin/env python3
"""`/liscr` 전체 승인 · 다시 읽기 계약 테스트.

형 지시(2026-08-19): "여러 인보이스 라인업해놓고 한방에 전체 컨펌할 수 있는 버튼 만들어줘".

핵심 계약(여기가 깨지면 인보이스가 두 번 만들어지거나, 확인 안 한 건이 SVMS 로 나간다):
  · 🔴 **일괄이 단건보다 느슨하면 안 된다.** 한 장씩 눌렀을 때 막히던 건이 일괄로는
        통과하는 것이 제일 위험한 종류의 차이다 — 같은 `_approve_blockers()` 를 쓴다.
  · 🔴 **`items` 는 필수.** "전체 승인" 을 서버가 알아서 고르면, 그 사이 러너가 올려놓은
        보지도 않은 건이 승인될 수 있다. 화면이 본 id 만 승인한다.
  · 🔴 **화면이 본 값 그대로만 승인한다.** id 만 받으면 [다시 읽기]/러너 재파싱으로 같은
        id 의 금액·Invoice No 가 바뀐 뒤 승인될 수 있다(TOCTOU) — 형이 확인한 1,490 이
        아닌 값이 SVMS 로 나간다. 화면이 보여준 `inv_no`·`amt` 를 지문으로 같이 받는다.
  · 🔴 **이중집행 금지.** 상태 게이트는 UPDATE 의 WHERE 안에 있어야 한다
        (`status='parsed'`). 같은 요청을 두 번 보내도 두 번째는 0건이어야 한다.
  · 🔴 **빈 칸을 대신 채우지 않는다.** 덜 채워진 건은 승인하지 않고 사유를 돌려준다.
  · 🔴 **다시 읽기는 SVMS 를 이미 건드린 뒤에는 못 한다** — created/failed 는 INV_CD 가
        남아 있을 수 있어, 다시 승인하면 같은 인보이스를 두 번 만든다.
  · 건너뛴 건은 조용히 사라지지 않고 사유와 함께 올라온다.

실행: ~/.venvs/trmt-test/bin/python tests/test_liscr_bulk_reparse.py
"""
import json, os, sys, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A  # noqa: E402

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

A.execute("INSERT OR IGNORE INTO supervisors (id, name) VALUES (1,'감독A')")


def mkuser(username, role):
    A.execute("INSERT INTO users (username, password_hash, display_name, supervisor_id, "
              "role, active) VALUES (?,'x',?,1,?,1)", (username, username, role))
    return A.query("SELECT id FROM users WHERE username=?", (username,), one=True)['id']


U_ADMIN = mkuser('admin1', 'admin')
U_MEM = mkuser('member1', 'member')


def login(uid, role, username='admin1'):
    # 🔴 실제 로그인이 넣는 키와 같아야 한다(`routes_core.py`: user_id/username/role/…).
    #    여기서 키 이름을 대충 맞추면 `decided_by` 같은 감사 흔적의 계약을 못 잰다.
    with c.session_transaction() as s:
        s['user_id'] = uid
        s['username'] = username
        s['role'] = role


import app_core  # noqa: E402
PDF_DIR = app_core.LISCR_PDF_DIR
os.makedirs(PDF_DIR, exist_ok=True)

# 승인 가능한 완전한 헤더 한 벌 — `_REQUIRED_HEADER` 전 항목이 채워져 있다.
FULL_HEADER = {'VSL_CD': 'SAPS', 'VNDR_CD': 'V25081', 'SUP_USER_ID': 'SS0094',
               'INV_NO': '9146057', 'INV_DT': '20260805', 'CUR_CD': 'USD',
               'AMT': 1490.0, 'PAY_DT': '20260930'}
FULL_LINES = [{'SUBJ': 'ANNUAL FEE', 'AMT': 1490.0, 'EXP_CD': '070205'}]


def mkjob(status='parsed', gate='READY', header=None, lines=None, with_pdf=True, inv_cd=None):
    h = FULL_HEADER if header is None else header
    ln = FULL_LINES if lines is None else lines
    jid = A.execute(
        "INSERT INTO liscr_job (filename, status, gate, header_json, lines_json, inv_no, amt, "
        "vsl_nm, cur_cd, inv_cd) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ('x.pdf', status, gate, json.dumps(h), json.dumps(ln),
         h.get('INV_NO'), h.get('AMT'), 'SAMOA PROSPERITY', h.get('CUR_CD'), inv_cd))
    if with_pdf:
        with open(os.path.join(PDF_DIR, '%d.pdf' % jid), 'wb') as f:
            f.write(b'%PDF-1.4 test')
    return jid


def row(jid):
    return A.query("SELECT * FROM liscr_job WHERE id=?", (jid,), one=True)


def status_of(jid):
    r = row(jid)
    return r['status'] if r else None


def bulk(ids):
    """정상 경로 — **화면이 방금 본 값**을 지문으로 실어 보낸다.

    지금 DB 값을 그대로 읽어 보내는 것이 "화면이 stale 하지 않은 상태" 다. 값이 어긋난
    경우는 §8 에서 따로 만든다.
    """
    items = []
    for jid in ids:
        r = row(jid)
        items.append({'id': jid,
                      'inv_no': (r['inv_no'] if r else None),
                      'amt': (r['amt'] if r else None)})
    return c.post('/api/liscr/jobs/approve-bulk', json={'items': items})


login(U_ADMIN, 'admin')

print('# 1) READY/FIX 는 승인, HOLD 는 거부 — 게이트가 그대로 살아 있다')
r_ready = mkjob(gate='READY')
r_fix = mkjob(gate='FIX')
r_hold = mkjob(gate='HOLD')
j = bulk([r_ready, r_fix, r_hold]).get_json()
chk(sorted(j.get('approved') or []) == sorted([r_ready, r_fix]), 'READY·FIX 2건 승인', j)
chk(status_of(r_hold) == 'parsed', '🔴 HOLD 는 승인되지 않음(상태 그대로)', status_of(r_hold))
chk(any(s['id'] == r_hold and 'HOLD' in s['reason'] for s in j.get('skipped') or []),
    'HOLD 건너뛴 사유가 올라옴', j)
chk(row(r_ready)['decided_by'] == 'admin1' and row(r_ready)['decided_at'],
    '승인자·승인시각이 기록됨', dict(row(r_ready)))

print('\n# 2) 🔴 값이 덜 채워진 건은 승인하지 않는다(대신 채워주지 않는다)')
for k, label in (('INV_NO', 'Invoice No'), ('AMT', '금액'), ('PAY_DT', 'Pay Date')):
    h = dict(FULL_HEADER)
    h[k] = None
    jid = mkjob(header=h)
    j = bulk([jid]).get_json()
    chk(j.get('approved') == [] and status_of(jid) == 'parsed',
        '%s 빈 건은 승인 안 됨' % label, (j, status_of(jid)))
    chk(any(label in s['reason'] for s in j.get('skipped') or []),
        '사유에 "%s" 가 나옴' % label, j)
jid = mkjob(lines=[])
j = bulk([jid]).get_json()
chk(status_of(jid) == 'parsed' and any('명세' in s['reason'] for s in j.get('skipped') or []),
    '명세 라인 0건은 승인 안 됨', j)
jid = mkjob(lines=[{'SUBJ': '  ', 'AMT': 1.0}])
j = bulk([jid]).get_json()
chk(status_of(jid) == 'parsed' and any('적요' in s['reason'] for s in j.get('skipped') or []),
    '적요 빈 건은 승인 안 됨', j)

print('\n# 3) 🔴 일괄은 단건보다 느슨하지 않다 — 같은 건에 두 경로가 같은 판정')
h = dict(FULL_HEADER)
h['INV_DT'] = None
jid = mkjob(header=h)
single = c.post('/api/liscr/jobs/%d/approve' % jid, json={})
chk(single.status_code == 400, '단건 승인 = 400(거부)', single.status_code)
chk(bulk([jid]).get_json().get('approved') == [], '일괄도 같은 건 거부', status_of(jid))
chk(status_of(jid) == 'parsed', '어느 경로로도 상태가 안 바뀜')

print('\n# 4) 🔴 이중집행 금지 — 이미 진행/완료된 건은 다시 승인되지 않는다')
for st in ['approved', 'creating', 'created', 'failed', 'rejected', 'hold', 'queued', 'parsing']:
    jid = mkjob(status=st)
    j = bulk([jid]).get_json()
    chk(j.get('approved') == [] and status_of(jid) == st,
        '%s 는 일괄 승인 대상 아님' % st, (j, status_of(jid)))

print('\n# 5) 🔴 같은 요청 재전송 — 두 번째는 0건(WHERE status=\'parsed\' 가 막는다)')
jid = mkjob()
first = bulk([jid]).get_json()
second = bulk([jid]).get_json()
chk(first.get('n') == 1, '1차 = 1건 승인', first)
chk(second.get('n') == 0, '2차 = 0건', second)
chk(status_of(jid) == 'approved', '상태는 approved 한 번뿐')

print('\n# 6) 같은 id 를 두 번 넣어도 한 번만 센다')
jid = mkjob()
j = bulk([jid, jid, jid]).get_json()
chk(j.get('n') == 1 and j.get('approved') == [jid], '중복 id = 1건', j)

print('\n# 7) items 검증 — 빈/누락/형식오류/과다는 400, 아무것도 승인 안 됨')
jid = mkjob()
FP = {'id': jid, 'inv_no': FULL_HEADER['INV_NO'], 'amt': FULL_HEADER['AMT']}
for body, label in (
        ({}, 'items 누락'),
        ({'items': []}, '빈 목록'),
        ({'ids': [jid]}, '구 형식(ids) 은 안 받는다'),
        ({'items': 'all'}, '문자열'),
        ({'items': [jid]}, '정수만 든 목록(객체 아님)'),
        ({'items': [{'inv_no': '1', 'amt': 1.0}]}, 'id 누락'),
        ({'items': [dict(FP, id='3')]}, 'id 가 문자열'),
        # 🔴 `int()` 로 받으면 1.9→1, True→1 이 조용히 통과한다. 금전 경로라 형태를 그대로 본다.
        ({'items': [dict(FP, id=float(jid))]}, 'id 가 실수'),
        ({'items': [dict(FP, id=True)]}, 'id 가 bool'),
        ({'items': [dict(FP, amt='1490')]}, 'amt 가 문자열'),
        ({'items': [dict(FP, amt=True)]}, 'amt 가 bool'),
        ({'items': [dict(FP, inv_no=9146057)]}, 'inv_no 가 숫자'),
        ({'items': [dict(FP, id=i) for i in range(101)]}, '101건')):
    resp = c.post('/api/liscr/jobs/approve-bulk', json=body)
    chk(resp.status_code == 400, '%s = 400' % label, (resp.status_code, resp.get_json()))
chk(status_of(jid) == 'parsed', '🔴 거부된 요청은 아무것도 승인하지 않음')
chk(bulk(list(range(1, 101))).status_code == 200, '100건은 허용(경계)')

print('\n# 8) 🔴 화면이 본 값과 지금 값이 다르면 승인하지 않는다(TOCTOU)')
jid = mkjob()
# 형이 확인창을 보는 사이 [다시 읽기]/러너 재파싱으로 금액이 바뀐 상황.
A.execute("UPDATE liscr_job SET amt=? WHERE id=?", (9999.0, jid))
j = c.post('/api/liscr/jobs/approve-bulk',
           json={'items': [{'id': jid, 'inv_no': FULL_HEADER['INV_NO'],
                            'amt': FULL_HEADER['AMT']}]}).get_json()
chk(j.get('approved') == [] and status_of(jid) == 'parsed',
    '🔴 금액이 바뀐 건은 승인 안 됨', (j, status_of(jid)))
chk(any(s['id'] == jid and '달라' in s['reason'] for s in j.get('skipped') or []),
    '사유에 "화면과 달라짐" 이 나옴', j)
chk(bulk([jid]).get_json().get('approved') == [jid],
    '새로고침(현재 값)으로는 승인됨', status_of(jid))

jid = mkjob()
A.execute("UPDATE liscr_job SET inv_no=? WHERE id=?", ('9146058', jid))
j = c.post('/api/liscr/jobs/approve-bulk',
           json={'items': [{'id': jid, 'inv_no': '9146057', 'amt': FULL_HEADER['AMT']}]}).get_json()
chk(j.get('approved') == [] and status_of(jid) == 'parsed',
    '🔴 Invoice No 가 바뀐 건도 승인 안 됨', (j, status_of(jid)))

# 값이 원래 비어 있던 카드 — `=` 로 비교하면 NULL 때문에 영영 승인이 안 된다(SQLite `IS` 계약).
h = dict(FULL_HEADER)
h['AMT'] = None
h['PAY_DT'] = h['PAY_DT']
jid = A.execute(
    "INSERT INTO liscr_job (filename, status, gate, header_json, lines_json, inv_no, amt, "
    "vsl_nm, cur_cd) VALUES (?,?,?,?,?,?,?,?,?)",
    ('x.pdf', 'parsed', 'READY', json.dumps(FULL_HEADER), json.dumps(FULL_LINES),
     FULL_HEADER['INV_NO'], None, 'SAMOA PROSPERITY', 'USD'))
j = c.post('/api/liscr/jobs/approve-bulk',
           json={'items': [{'id': jid, 'inv_no': FULL_HEADER['INV_NO'], 'amt': None}]}).get_json()
chk(j.get('approved') == [jid], '🔴 amt 가 NULL 인 카드도 승인됨(`IS` 비교)', j)

print('\n# 9) 🔴 저장된 파싱 결과가 깨져 있어도 배치가 죽지 않는다(그 건만 건너뛴다)')
bad = A.execute(
    "INSERT INTO liscr_job (filename, status, gate, header_json, lines_json, inv_no, amt, "
    "vsl_nm, cur_cd) VALUES (?,?,?,?,?,?,?,?,?)",
    ('x.pdf', 'parsed', 'READY', '{not json', '[', '9146099', 1.0, 'SAMOA PROSPERITY', 'USD'))
good = mkjob()
j = c.post('/api/liscr/jobs/approve-bulk', json={'items': [
    {'id': bad, 'inv_no': '9146099', 'amt': 1.0},
    {'id': good, 'inv_no': FULL_HEADER['INV_NO'], 'amt': FULL_HEADER['AMT']}]}).get_json()
chk(j.get('approved') == [good] and status_of(bad) == 'parsed',
    '🔴 깨진 건은 건너뛰고 나머지는 승인(500 으로 배치 중단 안 됨)', (j, status_of(bad)))
chk(any(s['id'] == bad and '읽을 수 없' in s['reason'] for s in j.get('skipped') or []),
    '깨진 건 사유가 올라옴', j)

print('\n# 10) 없는 번호는 조용히 사라지지 않고 사유로 올라온다')
j = bulk([999999]).get_json()
chk(j.get('approved') == [] and any(s['id'] == 999999 and '없는' in s['reason']
                                    for s in j.get('skipped') or []), '없는 건 = skipped 사유', j)

print('\n# 11) 다시 읽기 — hold/parsed 만 되돌아간다')
for st in ['hold', 'parsed']:
    jid = mkjob(status=st, gate='HOLD')
    A.execute("UPDATE liscr_job SET reasons=?, hard_json=?, error=?, decided_by=? WHERE id=?",
              ('["x"]', '["x"]', 'boom', 'admin1', jid))
    resp = c.post('/api/liscr/jobs/%d/reparse' % jid)
    r = row(jid)
    chk(resp.status_code == 200 and r['status'] == 'queued', '%s → queued' % st, resp.status_code)
    chk(r['gate'] is None and r['reasons'] is None and r['hard_json'] is None
        and r['error'] is None and r['decided_by'] is None,
        '%s 파싱 결과·결재 흔적이 초기화됨' % st, dict(r))

print('\n# 12) 🔴 SVMS 를 건드렸거나 러너가 잡은 상태는 다시 읽기 금지')
for st in ['approved', 'creating', 'created', 'failed', 'queued', 'parsing', 'uploading',
           'rejected']:
    jid = mkjob(status=st, inv_cd='SAPSCI2608190001' if st in ('created', 'failed') else None)
    resp = c.post('/api/liscr/jobs/%d/reparse' % jid)
    chk(resp.status_code == 409 and status_of(jid) == st,
        '%s 다시 읽기 = 409(차단), 상태 그대로' % st, (resp.status_code, status_of(jid)))

print('\n# 13) PDF 가 없으면 다시 읽을 수 없다(409) · 없는 번호는 404')
jid = mkjob(status='hold', with_pdf=False)
resp = c.post('/api/liscr/jobs/%d/reparse' % jid)
chk(resp.status_code == 409 and status_of(jid) == 'hold', 'PDF 없음 = 409', resp.status_code)
chk('PDF' in (resp.get_json().get('error') or ''), '사유가 PDF 없음', resp.get_json())
resp = c.post('/api/liscr/jobs/999999/reparse')
chk(resp.status_code == 404, '없는 jid = 404 (409 와 섞지 않는다)', resp.status_code)

print('\n# 14) 권한 — member 403 · 미로그인 401, 상태는 그대로')
A.execute("DELETE FROM liscr_job")
jid = mkjob()
hid = mkjob(status='hold')
login(U_MEM, 'member', 'member1')
chk(bulk([jid]).status_code == 403, 'member 일괄승인 = 403')
chk(c.post('/api/liscr/jobs/%d/reparse' % hid).status_code == 403, 'member 다시읽기 = 403')
with c.session_transaction() as s:
    s.clear()
chk(bulk([jid]).status_code == 401, '미로그인 일괄승인 = 401')
chk(c.post('/api/liscr/jobs/%d/reparse' % hid).status_code == 401, '미로그인 다시읽기 = 401')
chk(status_of(jid) == 'parsed' and status_of(hid) == 'hold', '거부된 요청은 상태를 안 바꿈')

print('\n' + ('❌ 실패 %d건: %s' % (len(fails), fails) if fails else '✅ 전부 통과'))
for r in A.query("SELECT id FROM liscr_job"):
    try:
        os.unlink(os.path.join(PDF_DIR, '%d.pdf' % r['id']))
    except OSError:
        pass
try:
    os.unlink(DB)
except OSError:
    pass
sys.exit(1 if fails else 0)
