#!/usr/bin/env python3
"""Phase ③ — 수리·구매 견적 상신 큐 fail-closed 테스트.

이 큐의 1행 = 맥 워커의 **실제 SVMS write 2회**(SP_SET_ODR_INFO Save → SP_SET_SBM 상신)다.
따라서 검증 대상은 "잘 되는 길"이 아니라 **잘못 나가는 길이 다 막혔는지**:
  · ext(api_key) 로는 큐를 만들 수 없다 (사람 없는 상신 차단)
  · 클라이언트가 rep_cd/금액을 못 보낸다 (화면 A · 봉투 B 조작 차단)
  · 업체는 그 건의 sub_quotes 에 cd 로 실재 + 제출상태여야 한다
  · 이미 발주완료 / 이미 큐에 있음 → 거부
  · claim 은 CAS 1회 (같은 행 두 번 안 나감), 6h stale → failed 이며 재큐 없음

실행: /tmp/soavenv/bin/python tests/test_dockproc_submit_queue.py
"""
import os, sys, json, tempfile

os.chdir(os.path.expanduser('~/projects/teammanagement'))
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

KEY = 'testkey-dock-submit'
A._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}

A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")

SUBMITTED = {'cd': 'A1J43', 'nm': 'ETMARINE', 'amt': 4800, 'usd': 4800, 'cur': 'USD',
             'st': 'Submitted', 'att': 1, 'best': 1}


def mkrow(req_no, cat='R', rep_cd=None, stg_order=0, quotes=(SUBMITTED,)):
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, svms_req_no, "
        "stg_quote, stg_vendor, stg_order, sub_quotes) "
        "VALUES('TEST VESSEL','TSTV',?,?,?,?,0,0,?,?)",
        (req_no, cat, f'[DOCK][TSTV {req_no}]subject',
         rep_cd if rep_cd is not None else f'BGBBME2607{req_no}', stg_order,
         None if quotes is None else json.dumps(list(quotes))))
    return A.query("SELECT id FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def push_lines(lines):
    return c.post('/api/ext/svms/app_lines', headers=HDR, json={'lines': lines})


def create(rid, vndr_cd='A1J43', app_no='0002', **kw):
    body = {'rid': rid, 'vndr_cd': vndr_cd, 'app_no': app_no}
    body.update(kw)
    return c.post('/api/dock_submit/drafts', json=body)


print('# 1) 결재라인 캐시 — 전량 교체 · 빈 푸시는 캐시 보존')
r = push_lines([{'app_no': '0002', 'app_nm': 'Dock 결재', 'user_id': 'SS0094',
                 'approvers': [{'seq': 1, 'id': 'SS0100', 'nm': 'A'}, {'seq': 2, 'id': 'SS0200', 'nm': 'B'}]},
                {'app_no': '0003', 'app_nm': '기타', 'approvers': []}])
chk(r.status_code == 200 and r.get_json()['count'] == 2, '2건 적재', r.get_json())
r = c.get('/api/dock_submit/app_lines')
j = r.get_json()['lines']
chk(len(j) == 2 and j[0]['app_no'] == '0002' and len(j[0]['approvers']) == 2,
    '드롭다운 조회 + approvers 파싱', j)
chk(push_lines([]).status_code == 400 and len(c.get('/api/dock_submit/app_lines').get_json()['lines']) == 2,
    '빈 푸시 거부 — 드롭다운 안 비움')
chk(push_lines([{'app_no': 'bad-!!', 'app_nm': 'x'}]).status_code == 400,
    'app_no 형식 불량만 담긴 푸시도 거부')
r = push_lines([{'app_no': '0002', 'app_nm': 'Dock 결재2'}])
lines = c.get('/api/dock_submit/app_lines').get_json()['lines']
chk(len(lines) == 1 and lines[0]['app_nm'] == 'Dock 결재2', '전량 교체(0003 사라짐)', lines)
push_lines([{'app_no': '0002', 'app_nm': 'Dock 결재', 'user_id': 'SS0094',
             'approvers': [{'seq': 1, 'id': 'SS0100', 'nm': 'A'}]}])
chk(c.get('/api/ext/svms/app_lines', headers=HDR).status_code in (404, 405),
    'app_lines 는 POST 전용')

print('# 2) 🔴 생성은 세션 admin 만 — ext 키로는 큐를 만들 수 없다')
rid = mkrow('R01')
with c.session_transaction() as s:
    saved = dict(s); s.clear()          # ⚠️ 세션을 반드시 비운 뒤 검증 — 쿠키가 남아 있으면 admin 으로 통과해 통과처럼 보인다
chk(create(rid).status_code in (302, 401, 403), '비로그인 생성 차단')
# api_key 는 세션이 아니므로 admin_required 가 막아야 한다. 201 이면 사람 없는 상신 큐잉 가능 = 치명.
r = c.post('/api/dock_submit/drafts', headers=HDR, json={'rid': rid, 'vndr_cd': 'A1J43', 'app_no': '0002'})
chk(r.status_code not in (200, 201), 'api_key 단독 생성 차단', r.status_code)
chk(A.query("SELECT COUNT(*) n FROM dock_submit_draft", one=True)['n'] == 0, '차단된 시도는 행을 남기지 않음')
with c.session_transaction() as s:
    s.update(saved); s['role'] = 'user'
chk(create(rid).status_code in (302, 401, 403), '일반 user 생성 차단')
with c.session_transaction() as s:
    s['role'] = 'admin'

print('# 2b) 🔴 Bearer 권한 행렬 — 앱 토큰이 세션 admin 을 우회하지 못한다 (올마이트 2026-08-01)')
# `_bearer_auth` 는 서명·만료·active·비번지문(pv) 을 다 통과해야 session['role'] 을 채운다.
# 즉 앱 토큰 = 그 사람의 로그인과 동일 신원이지, 권한 승격 경로가 아니다. 아래로 실측 고정.
for uname, role in (('bu_user', 'member'), ('bu_admin', 'admin')):   # users.role CHECK = admin|member
    A.execute("DELETE FROM users WHERE username=?", (uname,))
    A.execute("INSERT INTO users(username, password_hash, display_name, role, active) "
              "VALUES(?,?,?,?,1)", (uname, 'pbkdf2:sha256:fake$' + uname, uname, role))
BU = {n: A.query("SELECT * FROM users WHERE username=?", (n,), one=True) for n in ('bu_user', 'bu_admin')}
TOK = {n: A._issue_token(u) for n, u in BU.items()}
rid2 = mkrow('R02')
with c.session_transaction() as s:
    saved = dict(s); s.clear()
def bcreate(tok, rid):
    return c.post('/api/dock_submit/drafts', headers={'Authorization': 'Bearer ' + tok},
                  json={'rid': rid, 'vndr_cd': 'A1J43', 'app_no': '0002'})
chk(bcreate(TOK['bu_user'], rid2).status_code in (302, 401, 403), '일반 member Bearer 생성 차단')
chk(bcreate('garbage.token.value', rid2).status_code in (302, 401, 403), '위조 토큰 생성 차단')
A.execute("UPDATE users SET password_hash=? WHERE username='bu_admin'", ('rotated',))
chk(bcreate(TOK['bu_admin'], rid2).status_code in (302, 401, 403),
    '비번 변경된 admin 의 옛 토큰 차단(pv 지문)')
A.execute("UPDATE users SET password_hash=? WHERE username='bu_admin'", (BU['bu_admin']['password_hash'],))
A.execute("UPDATE users SET active=0 WHERE username='bu_admin'")
chk(bcreate(TOK['bu_admin'], rid2).status_code in (302, 401, 403), '비활성 admin 토큰 차단')
A.execute("UPDATE users SET active=1 WHERE username='bu_admin'")
chk(A.query("SELECT COUNT(*) n FROM dock_submit_draft", one=True)['n'] == 0,
    '차단된 Bearer 시도는 행을 남기지 않음')
r = bcreate(TOK['bu_admin'], rid2)
chk(r.status_code == 201, '정상 admin Bearer 는 생성 가능(=본인 로그인과 동일 신원)', r.status_code)
row = A.query("SELECT * FROM dock_submit_draft ORDER BY id DESC LIMIT 1", one=True)
chk(row and row['decided_by'] == 'bu_admin', '승인자에 토큰 주인이 기록됨', dict(row or {}))
A.execute("DELETE FROM dock_submit_draft")
with c.session_transaction() as s:
    s.update(saved); s['role'] = 'admin'

print('# 3) 정상 생성 — rep_cd/금액은 서버가 DB 에서 채운다')
r = create(rid)
j = r.get_json()
chk(r.status_code == 201 and j['status'] == 'approved' and j['rep_cd'] == 'BGBBME2607R01',
    'approved 1행 생성 + rep_cd 서버 유도', j)
row = A.query('SELECT * FROM dock_submit_draft WHERE id=?', (j['id'],), one=True)
chk(row['amt'] == 4800.0 and row['cur'] == 'USD' and row['vndr_nm'] == 'ETMARINE',
    '금액/통화/업체명 서버 채움', dict(row))
chk(row['decided_by'] == 'smoke' and row['decided_at'], '승인자 기록', dict(row))
env = json.loads(row['envelope_json'])
chk(env['rep_cd'] == 'BGBBME2607R01' and env['app_nm'] == 'Dock 결재' and env['approvers'],
    '스냅샷에 결재라인까지 남음', env)

print('# 4) 🔴 클라이언트가 보낸 rep_cd/amt 는 무시된다 (화면 A · 봉투 B 조작 차단)')
rid2 = mkrow('R02')
r = create(rid2, rep_cd='EVILDOC999', amt=1, vndr_nm='HACKER', app_nm='없는라인')
row2 = A.query('SELECT * FROM dock_submit_draft WHERE id=?', (r.get_json()['id'],), one=True)
chk(row2['rep_cd'] == 'BGBBME2607R02' and row2['amt'] == 4800.0 and row2['vndr_nm'] == 'ETMARINE',
    '주입된 rep_cd/amt/업체명 전부 무시', dict(row2))

print('# 5) 이중 큐잉 차단 — 부분 유니크 인덱스')
r = create(rid)
chk(r.status_code == 409, '같은 rep_cd 재큐 409', r.get_json())
chk(A.query("SELECT COUNT(*) n FROM dock_submit_draft WHERE rep_cd='BGBBME2607R01'", one=True)['n'] == 1,
    '행이 늘지 않음')

print('# 6) 업체 검증 — cd 실재 + 제출상태만')
chk(create(mkrow('R10'), vndr_cd='NOPE').status_code == 400, '그 건에 없는 업체코드 거부')
chk(create(mkrow('R11', quotes=[dict(SUBMITTED, st='Requested')])).status_code == 400,
    '제출상태 아닌 업체 거부(st=Requested)')
chk(create(mkrow('R12', quotes=[{'nm': 'ETMARINE', 'amt': 4800, 'st': 'Submitted'}])).status_code == 400,
    '구버전 폴러(cd 없음) 스냅샷 거부')
chk(create(mkrow('R13', quotes=None)).status_code == 400, '제출견적 0건 거부')
A.execute("UPDATE dock_procure SET sub_quotes='{not json' WHERE id=?", (mkrow('R14'),))
bad = A.query("SELECT id FROM dock_procure WHERE req_no='R14'", one=True)['id']
chk(create(bad).status_code == 400, '스냅샷 손상 거부')
chk(create(rid, vndr_cd='a1j43!!').status_code == 400, 'vndr_cd 형식 검증')
chk(create(mkrow('R15'), app_no='9999').status_code == 400, '캐시에 없는 결재라인 거부')
chk(create(mkrow('R16'), app_no='').status_code == 400, '빈 결재라인 거부')

print('# 7) 대상 자격 — R/S/ST · 문서번호 有 · 미발주')
chk(create(mkrow('S20', cat='S')).status_code == 201, '자재(S) 건 허용')
chk(create(mkrow('ST20', cat='ST')).status_code == 201, '스토어(ST) 건 허용')
chk(create(mkrow('P20', cat='P')).status_code == 400, '페인트(P) 건 거부')
chk(create(mkrow('R21', rep_cd='')).status_code == 400, 'SVMS 문서번호 없음 거부')
chk(create(mkrow('R22', stg_order=1)).status_code == 409, '이미 발주완료 거부')
chk(create(999999).status_code == 404, '없는 rid 404')

print('# 8) preview — write 0 · 거절사유 미리보기')
pv = c.get(f'/api/dock_submit/preview?rid={mkrow("R30")}').get_json()
chk(pv['blocked'] is None and pv['candidates'][0]['ok'] is True, '정상건은 blocked None', pv)
pv = c.get(f'/api/dock_submit/preview?rid={mkrow("R31", stg_order=1)}').get_json()
chk(pv['blocked'] == '이미 발주완료', '발주완료 사유 노출', pv)
pv = c.get(f'/api/dock_submit/preview?rid={mkrow("R32", quotes=[dict(SUBMITTED, st="Requested")])}').get_json()
chk(pv['candidates'][0]['ok'] is False and pv['candidates'][0]['why'] == '제출상태 아님',
    '후보별 거절사유', pv)
pv = c.get(f'/api/dock_submit/preview?rid={rid}').get_json()
chk(pv['blocked'] and '큐' in pv['blocked'], '이미 큐에 있음 사유', pv)

print('# 9) claim — CAS 1회 · peek 는 락 안 함')
A.execute("DELETE FROM dock_submit_draft")
rid3 = mkrow('R40'); did = create(rid3).get_json()['id']
pk = c.get('/api/ext/dock_submit/approved?peek=1', headers=HDR).get_json()
chk(pk['peek'] is True and pk['count'] == 1, 'peek 조회', pk)
chk(A.query('SELECT status FROM dock_submit_draft WHERE id=?', (did,), one=True)['status'] == 'approved',
    'peek 는 상태 안 바꿈')
chk(c.get('/api/ext/dock_submit/approved').status_code in (401, 403), 'ext 키 없으면 차단')
g1 = c.get('/api/ext/dock_submit/approved', headers=HDR).get_json()
chk(g1['count'] == 1 and g1['drafts'][0]['id'] == did and g1['drafts'][0]['rep_cd'] == 'BGBBME2607R40',
    '1건 claim + 봉투 재료 반환', g1)
chk(A.query('SELECT status FROM dock_submit_draft WHERE id=?', (did,), one=True)['status'] == 'submitting',
    'submitting 락')
chk(c.get('/api/ext/dock_submit/approved', headers=HDR).get_json()['count'] == 0,
    '🔴 두 번째 호출은 0건 — 같은 건 두 번 상신 안 됨')

print('# 9b) claim 은 limit 만큼만 — 나머지는 approved 로 남는다 (올마이트 2026-08-01 P0)')
# 예전엔 approved 전부를 submitting 으로 잠갔고, 워커는 --max 1 만 처리해서 나머지가
# 아무 일도 안 당한 채 6h 뒤 failed 로 떨어졌다(재큐 없음 = 형의 컨펌이 조용히 사라짐).
A.execute("DELETE FROM dock_submit_draft")
ids = [create(mkrow('R5%d' % i)).get_json()['id'] for i in range(3)]
g = c.get('/api/ext/dock_submit/approved', headers=HDR).get_json()
chk(g['count'] == 1 and g['limit'] == 1, '기본 limit=1 — 1건만 claim', g)
left = [r['status'] for r in A.query('SELECT status FROM dock_submit_draft WHERE id IN (?,?)', tuple(ids[1:]))]
chk(left == ['approved', 'approved'], '🔴 나머지 2건은 approved 그대로(유실 없음)', left)
g2 = c.get('/api/ext/dock_submit/approved?limit=5', headers=HDR).get_json()
chk(g2['count'] == 2, 'limit=5 지만 남은 2건만 claim', g2)
chk(c.get('/api/ext/dock_submit/approved?limit=abc', headers=HDR).get_json()['limit'] == 1,
    'limit 이 숫자가 아니면 1로 안전 폴백')
A.execute("UPDATE dock_submit_draft SET status='approved'")
chk(c.get('/api/ext/dock_submit/approved?limit=999', headers=HDR).get_json()['limit'] == 20,
    'limit 상한 20 — 한 번에 무한정 잠그지 않음')

print('# 9c) 🔴 `?id=N` — [지금 전송] 이 지목한 그 행만, 가드는 동일')
# 버튼 경로가 별도 우회로가 되면 안 된다: 같은 CAS, 같은 승인흔적 조건, 옆 행은 안 건드림.
A.execute("DELETE FROM dock_submit_draft")
ids = [create(mkrow('R6%d' % i)).get_json()['id'] for i in range(3)]
g = c.get(f'/api/ext/dock_submit/approved?id={ids[1]}', headers=HDR).get_json()
chk(g['count'] == 1 and g['drafts'][0]['id'] == ids[1], '지목한 행만 claim', g)
sts = {r['id']: r['status'] for r in A.query('SELECT id, status FROM dock_submit_draft')}
chk(sts[ids[0]] == 'approved' and sts[ids[2]] == 'approved', '옆 행은 approved 그대로', sts)
chk(c.get(f'/api/ext/dock_submit/approved?id={ids[1]}', headers=HDR).get_json()['count'] == 0,
    '🔴 같은 id 두 번째 호출은 0건(이중 상신 차단)')
chk(c.get('/api/ext/dock_submit/approved?id=999999', headers=HDR).get_json()['count'] == 0,
    '없는 id 는 0건')
chk(c.get('/api/ext/dock_submit/approved?id=abc', headers=HDR).status_code == 400, 'id 형식 오류 400')
A.execute("UPDATE dock_submit_draft SET decided_by='' WHERE id=?", (ids[0],))
chk(c.get(f'/api/ext/dock_submit/approved?id={ids[0]}', headers=HDR).get_json()['count'] == 0,
    '🔴 승인흔적 없는 행은 id 로 지목해도 거부')
chk(A.query('SELECT status FROM dock_submit_draft WHERE id=?', (ids[2],), one=True)['status'] == 'approved',
    'id 경로가 다른 행을 잠그지 않음')

print('# 10) 사람 승인 흔적 없는 행은 claim 대상 아님')
A.execute("DELETE FROM dock_submit_draft")
rid3 = mkrow('R40'); did = create(rid3).get_json()['id']
c.get('/api/ext/dock_submit/approved', headers=HDR)
A.execute("UPDATE dock_submit_draft SET status='approved', decided_by='' WHERE id=?", (did,))
chk(c.get('/api/ext/dock_submit/approved', headers=HDR).get_json()['count'] == 0,
    'decided_by 빈 행 claim 거부')
A.execute("UPDATE dock_submit_draft SET decided_by='smoke', decided_at=NULL WHERE id=?", (did,))
chk(c.get('/api/ext/dock_submit/approved', headers=HDR).get_json()['count'] == 0,
    'decided_at NULL 행 claim 거부')
A.execute("UPDATE dock_submit_draft SET decided_at=datetime('now','localtime') WHERE id=?", (did,))
chk(c.get('/api/ext/dock_submit/approved', headers=HDR).get_json()['count'] == 1, '복구되면 claim 됨')

print('# 11) 결과 보고 — submitting 만 전이')
r = c.post(f'/api/ext/dock_submit/drafts/{did}/result', headers=HDR,
           json={'ok': True, 'result': 'readback: RE→SV 확인'})
chk(r.get_json()['applied'] is True, 'submitted 반영')
row = A.query('SELECT * FROM dock_submit_draft WHERE id=?', (did,), one=True)
chk(row['status'] == 'submitted' and 'readback' in (row['result'] or ''), '결과 문자열 저장', dict(row))
r = c.post(f'/api/ext/dock_submit/drafts/{did}/result', headers=HDR, json={'ok': False})
chk(r.get_json()['applied'] is False and
    A.query('SELECT status FROM dock_submit_draft WHERE id=?', (did,), one=True)['status'] == 'submitted',
    '🔴 늦게 온 실패보고가 submitted 를 뒤집지 못함')

print('# 12) 6h stale submitting → failed, 재큐 없음')
rid4 = mkrow('R41'); d4 = create(rid4).get_json()['id']
c.get('/api/ext/dock_submit/approved', headers=HDR)
A.execute("UPDATE dock_submit_draft SET done_at=datetime('now','localtime','-7 hours') WHERE id=?", (d4,))
got = c.get('/api/ext/dock_submit/approved', headers=HDR).get_json()
st = A.query('SELECT status, result FROM dock_submit_draft WHERE id=?', (d4,), one=True)
chk(st['status'] == 'failed' and '6h' in (st['result'] or ''), 'stale → failed', dict(st))
chk(all(x['id'] != d4 for x in got['drafts']), '🔴 stale 행을 다시 안 집어감(이중 상신 방지)')
A.execute("UPDATE dock_submit_draft SET done_at=datetime('now','localtime','-1 hours'), "
          "status='submitting' WHERE id=?", (d4,))
c.get('/api/ext/dock_submit/approved', headers=HDR)
chk(A.query('SELECT status FROM dock_submit_draft WHERE id=?', (d4,), one=True)['status'] == 'submitting',
    '6h 안 지난 submitting 은 유지')

print('# 13) 취소 — approved 만')
rid5 = mkrow('R42'); d5 = create(rid5).get_json()['id']
chk(c.post(f'/api/dock_submit/drafts/{d5}/cancel').get_json()['status'] == 'canceled', 'approved 취소')
chk(c.post(f'/api/dock_submit/drafts/{d5}/cancel').status_code == 409, '이미 취소된 건 409')
chk(create(rid5).status_code == 201, '취소 후에는 재큐 가능(부분 유니크에서 빠짐)')
d5b = A.query("SELECT id FROM dock_submit_draft WHERE rep_cd='BGBBME2607R42' AND status='approved'",
              one=True)['id']
c.get('/api/ext/dock_submit/approved', headers=HDR)
chk(c.post(f'/api/dock_submit/drafts/{d5b}/cancel').status_code == 409,
    '🔴 submitting 은 취소 불가(SVMS 에 이미 나갔을 수 있음)')
chk(c.post(f'/api/dock_submit/drafts/999999/cancel').status_code == 404, '없는 id 404')

print('# 13b) 🔴 [지금 전송] 릴레이 — 스케줄 없음, 누른 그 순간에만 · 터널 없으면 명확히 실패')
# 서버는 SVMS 에 못 붙는다. 이 라우트는 맥 리스너로 넘기는 릴레이일 뿐이므로,
# 검증 대상은 "무엇이 넘어가는가"와 "안 넘어가야 할 때 조용히 넘어가지 않는가"다.
import threading, http.server, socket

SEEN = []


class _FakeMac(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        SEEN.append({'path': self.path, 'tok': self.headers.get('X-Push-Token'),
                     'body': json.loads(self.rfile.read(n) or b'{}')})
        b = json.dumps({'ok': True, 'msg': '상신됨'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


_srv = http.server.HTTPServer(('127.0.0.1', 0), _FakeMac)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
PORT = _srv.server_address[1]
PUSH_TOK = 'tok-abc-123'
os.environ['DOCK_PUSH_URL'] = f'http://127.0.0.1:{PORT}/push'
os.environ['DOCK_PUSH_TOKEN'] = PUSH_TOK

rid6 = mkrow('R70'); d6 = create(rid6).get_json()['id']   # 앞 섹션 행은 지우지 않는다(#14 가 rid5 를 셈)
r = c.post(f'/api/dock_submit/drafts/{d6}/push')
j = r.get_json()
# 가짜 맥은 실제 `/result` readback을 호출하지 않는다. 서버가 맥의 ok만 믿으면
# 성공 오판이므로, 상태가 approved인 동안 ok=False로 fail-closed 판정해야 한다.
chk(r.status_code == 200 and j['ok'] is False and j['msg'] == '상신됨',
    '맥 응답만으로 성공 판정하지 않음(readback 상태 필수)', j)
chk(len(SEEN) == 1 and SEEN[0]['body'] == {'draft_id': d6} and SEEN[0]['tok'] == PUSH_TOK,
    '🔴 draft_id 만 넘김 + 공유토큰 헤더', SEEN)
chk(j.get('status') == 'approved', '상태 전이는 맥이 claim/result 로 한다(라우트가 직접 안 바꿈)', j)

A.execute("UPDATE dock_submit_draft SET status='submitting' WHERE id=?", (d6,))
r = c.post(f'/api/dock_submit/drafts/{d6}/push')
chk(r.status_code == 409 and len(SEEN) == 1, '🔴 submitting 은 재전송 불가 — 맥 호출조차 안 함', r.status_code)
A.execute("UPDATE dock_submit_draft SET status='canceled' WHERE id=?", (d6,))
chk(c.post(f'/api/dock_submit/drafts/{d6}/push').status_code == 409 and len(SEEN) == 1,
    '취소된 건도 전송 불가')
chk(c.post('/api/dock_submit/drafts/999999/push').status_code == 404, '없는 id 404')

A.execute("UPDATE dock_submit_draft SET status='approved' WHERE id=?", (d6,))
with c.session_transaction() as s:
    s['role'] = 'user'
chk(c.post(f'/api/dock_submit/drafts/{d6}/push').status_code in (302, 401, 403) and len(SEEN) == 1,
    '🔴 일반 user 는 전송 버튼을 못 씀')
with c.session_transaction() as s:
    s['role'] = 'admin'

# 터널이 죽었을 때: 조용히 큐에 남겨두면 형의 컨펌이 어디로 갔는지 알 수 없다 → 503 으로 크게 실패.
_dead = socket.socket(); _dead.bind(('127.0.0.1', 0)); DEADP = _dead.getsockname()[1]; _dead.close()
os.environ['DOCK_PUSH_URL'] = f'http://127.0.0.1:{DEADP}/push'
r = c.post(f'/api/dock_submit/drafts/{d6}/push')
chk(r.status_code == 503 and '맥 미연결' in (r.get_json().get('error') or ''),
    '🔴 터널 끊김 = 503 "맥 미연결" (fail-closed)', r.get_json())
chk(A.query('SELECT status FROM dock_submit_draft WHERE id=?', (d6,), one=True)['status'] == 'approved',
    '실패해도 approved 그대로 — 다시 누르면 됨')
os.environ.pop('DOCK_PUSH_URL'); os.environ.pop('DOCK_PUSH_TOKEN')
r = c.post(f'/api/dock_submit/drafts/{d6}/push')
chk(r.status_code == 503 and '미설정' in (r.get_json().get('error') or ''), '설정 없으면 503', r.get_json())
_srv.shutdown()

print('# 14) 목록 · 정리')
lst = c.get('/api/dock_submit/drafts').get_json()['drafts']
chk(lst and 'envelope_json' not in lst[0], '목록엔 봉투 원문 안 실음')
chk(len(c.get(f'/api/dock_submit/drafts?rid={rid5}').get_json()['drafts']) == 2, 'rid 필터')
before = A.query("SELECT COUNT(*) n FROM dock_submit_draft WHERE status IN ('approved','submitting')",
                 one=True)['n']
c.delete('/api/dock_submit/drafts/decided')
after = A.query("SELECT COUNT(*) n FROM dock_submit_draft", one=True)['n']
chk(after == before, '처리완료만 삭제 · 진행중 보존', (before, after))

print('# 15) 웹 컨펌 모달 — admin 만 렌더 · JS 문법')
html = c.get('/dock_procure').get_data(as_text=True)
chk('id="sb-ov"' in html and 'sbmOpen' in html, '모달 + 버튼 핸들러 렌더')
chk('const IS_ADMIN = true;' in html, 'admin 플래그 true', [l for l in html.splitlines() if 'IS_ADMIN =' in l])
chk('SVMS 로 실제 상신되는 것에 동의' in html, '동의 체크 문구(돈경로 고지)')
chk('id="sb-push"' in html and '지금 전송' in html, '[지금 전송] 버튼 렌더')
chk('워커 주기' not in html, '🔴 "다음 워커 주기에 전송" 같은 거짓 문구 없음(자동 전송 스케줄러가 없다)')
with c.session_transaction() as s:
    s['role'] = 'user'
html_u = c.get('/dock_procure').get_data(as_text=True)
chk('id="sb-ov"' not in html_u and 'const IS_ADMIN = false;' in html_u,
    '🔴 일반 user 에겐 모달 자체가 없음')
with c.session_transaction() as s:
    s['role'] = 'admin'
# 템플릿 안 JS 는 브라우저에서만 도니, 최소한 문법이 깨지지 않았는지는 여기서 잡는다.
import re as _re, shutil, subprocess
# 렌더된 HTML 에는 Jinja 블록 표시가 없다 — 이 페이지 스크립트(IS_ADMIN 이 들어간 IIFE)를 집는다.
m = [s for s in _re.findall(r'<script>(.*?)</script>', html, _re.S) if 'IS_ADMIN' in s]
NODE = shutil.which('node') or next((p for p in __import__('glob').glob(
    '/opt/homebrew/Cellar/node/*/bin/node') + ['/opt/homebrew/bin/node'] if os.path.exists(p)), None)
if m and NODE:
    p = subprocess.run([NODE, '--check', '-'], input=m[0], capture_output=True, text=True)
    chk(p.returncode == 0, 'inline JS 문법 통과(node --check)', p.stderr[-400:])
else:
    print('  --  node 없음 → JS 문법검사 건너뜀')

print('# 16) 🔴 재컨펌 차단 — 이미 상신된 건은 버튼 단계에서 막힌다 (2026-08-01 실사고)')
# 실사고: BGBBME26073108 이 12:03 에 실제 상신됐는데 화면은 아직 'Quotation Inquiry' 라
# 형이 같은 건을 다시 컨펌 → [지금 전송] → 워커 pre-read 가 SVMS 헤더 보고 차단(fail-closed).
# 이중 상신은 안 났지만 버튼 단계에서 막았어야 한다. 여기서 그 게이트를 고정한다.
A.execute("DELETE FROM dock_submit_draft")
rid7 = mkrow('R80'); d7 = create(rid7).get_json()['id']
c.get(f'/api/ext/dock_submit/approved?id={d7}', headers=HDR)
r = c.post(f'/api/ext/dock_submit/drafts/{d7}/result', headers=HDR,
           json={'ok': True, 'result': 'readback STATUS=RU(Submit)'})
chk(r.get_json()['applied'] is True, '상신 성공 보고')
pr = A.query('SELECT svms_status, stg_confirm, stg_order FROM dock_procure WHERE id=?', (rid7,), one=True)
chk(pr['svms_status'] == 'Submit', '🔴 성공 즉시 화면 상태가 Submit 로 바뀜(다음 sync 안 기다림)', dict(pr))
chk(pr['stg_confirm'] == 1 and pr['stg_order'] == 0,
    '🔴 벤더컨펌은 즉시 켜고 발주완료는 안 건드림 — Submit 은 rank3', dict(pr))
pv = c.get(f'/api/dock_submit/preview?rid={rid7}').get_json()
chk('이미 상신됨' in (pv.get('blocked') or ''), '모달이 사유를 미리 보여줌', pv.get('blocked'))
r = create(rid7)
chk(r.status_code == 409 and '이미 상신됨' in (r.get_json().get('error') or ''),
    '🔴 서버가 재컨펌을 409 로 거절(정본 게이트)', r.get_json())
# 갱신 키는 rep_cd 가 아니라 그 draft 의 rid — 같은 문서번호가 여러 행에 붙어도 남의 행은 그대로.
A.execute("INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, svms_req_no) "
          "VALUES('TEST VESSEL','TSTV','R80B','R','sibling',?)", ('BGBBME2607R80',))
sib = A.query("SELECT id FROM dock_procure WHERE req_no='R80B'", one=True)['id']
rid7b = mkrow('R82'); d7b = create(rid7b).get_json()['id']
A.execute("UPDATE dock_procure SET svms_req_no='BGBBME2607R80' WHERE id=?", (rid7b,))
A.execute("UPDATE dock_submit_draft SET rep_cd='BGBBME2607R80' WHERE id=?", (d7b,))
c.get(f'/api/ext/dock_submit/approved?id={d7b}', headers=HDR)
c.post(f'/api/ext/dock_submit/drafts/{d7b}/result', headers=HDR, json={'ok': True, 'result': 'ok'})
chk(A.query('SELECT svms_status FROM dock_procure WHERE id=?', (sib,), one=True)['svms_status'] is None,
    '🔴 갱신은 그 draft 의 rid 한 행만 — 같은 문서번호의 남의 행을 덮지 않음')
chk(A.query('SELECT svms_status FROM dock_procure WHERE id=?', (rid7b,), one=True)['svms_status'] == 'Submit',
    '지목된 행은 갱신됨')
A.execute("DELETE FROM dock_submit_draft WHERE id=?", (d7b,))
# 상신 이후 라벨(denylist)은 모두 차단, 그 밖(반려·미지·NULL)은 열어준다.
# allowlist 로 짜면 처음 보는 라벨이 영구 차단이 되어 업무가 멈춘다(올마이트 2026-08-01).
for lbl in ('Submit', 'HQ Progressing', 'HQ Confirmed', 'HQ Ordered', 'Ordered'):
    A.execute("UPDATE dock_procure SET svms_status=? WHERE id=?", (lbl, rid7))
    chk('이미 상신됨' in (c.get(f'/api/dock_submit/preview?rid={rid7}').get_json().get('blocked') or ''),
        f'상신 이후 라벨 차단 — {lbl}')
for lbl in ('Quotation Inquiry', 'HQ Rejected', None, 'Rejected', 'Cost Review'):
    A.execute("UPDATE dock_procure SET svms_status=? WHERE id=?", (lbl, rid7))
    chk(c.get(f'/api/dock_submit/preview?rid={rid7}').get_json().get('blocked') is None,
        f'상신 이후 라벨이 아니면 열림 — {lbl or "NULL"} (최후 방어선은 워커 pre-read)')
chk(create(rid7).status_code == 201, '반려 후에는 다시 컨펌 가능')
A.execute("DELETE FROM dock_submit_draft WHERE rep_cd='BGBBME2607R80'")
rid8 = mkrow('R81'); d8 = create(rid8).get_json()['id']
c.get(f'/api/ext/dock_submit/approved?id={d8}', headers=HDR)
c.post(f'/api/ext/dock_submit/drafts/{d8}/result', headers=HDR, json={'ok': False, 'result': 'pre-read 실패'})
chk(A.query('SELECT svms_status FROM dock_procure WHERE id=?', (rid8,), one=True)['svms_status'] is None,
    '실패 보고는 화면 상태를 바꾸지 않음')
chk(A.query('SELECT stg_confirm FROM dock_procure WHERE id=?', (rid8,), one=True)['stg_confirm'] == 0,
    '상신 실패는 벤더컨펌 단계를 켜지 않음')
chk(c.get(f'/api/dock_submit/preview?rid={rid8}').get_json().get('blocked') is None,
    '실패한 건은 다시 컨펌 가능(막히면 안 됨)')

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
