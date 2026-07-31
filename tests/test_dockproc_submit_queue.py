#!/usr/bin/env python3
"""Phase ③ — 수리 견적 상신 큐 fail-closed 테스트.

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

print('# 7) 대상 자격 — 수리(R) · 문서번호 有 · 미발주')
chk(create(mkrow('S20', cat='S')).status_code == 400, '구매(S) 건 거부')
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

print('# 10) 사람 승인 흔적 없는 행은 claim 대상 아님')
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

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
