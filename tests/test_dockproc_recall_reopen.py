#!/usr/bin/env python3
"""Dock — SVMS **회수**(견적요청/상신 취소) 후 재요청 게이트가 다시 열리는지.

실사고(2026-08-03): BGBBME26073116([BGBB R22]) 견적요청이 LIVE 로 나간 뒤(18:05) 형이 SVMS 에서
회수했다. SVMS 는 목록·상세 모두 STATUS='AP'(HQ Received)·벤더그리드 0행인데 TRMT DB 는
  · `stg_vendor=1` (`api_ext_dock_inquiry_result` 의 낙관적 표시)
  · `svms_status='Quotation Inquiry'`
로 고착 → `_dock_inq_blocked` 가 '이미 벤더제출 이후 단계'로 409 → **큐 재적재 영구 불가**.

원인: sync 가 `link_only = (rank == 0)` 이라 rank 0 라벨은 단계를 **아예 안 건드렸다.**
  회수는 'RE'(rank 2)가 아니라 'AP'(HQ Received, **rank 0**)로 돌아오므로 옛 안전장치가 발동 안 함.
수정: `_DOCKPROC_PRE_INQUIRY` allowlist 의 라벨만 정상 경로로 보내 단계를 0 으로 되돌린다.
  미지 라벨·빈 라벨('')은 종전대로 link_only (수동 체크 보존).

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_recall_reopen.py
  ⚠️`/tmp/*venv` 는 정리되어 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import os, sys, tempfile

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

KEY = 'testkey-dockproc-recall'
A._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}

A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def mkrow(req_no, stages_on=(0, 0, 0, 0), svms_status=None, svms_req_no=None,
          quote_amt=None, remark=None, cat_code='R'):
    """실사고 재현용 행. stages_on = (quote, vendor, confirm, order)."""
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
        "stg_quote, stg_vendor, stg_confirm, stg_order, svms_status, svms_req_no, quote_amt, remark) "
        "VALUES('TEST VESSEL','TSTV',?,?,?,?,?,?,?,?,?,?,?)",
        (req_no, cat_code, f'[DOCK][TSTV {req_no}]subject', *stages_on,
         svms_status, svms_req_no, quote_amt, remark))
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def sync(req_no, status, inq_no=None):
    it = {'vsl_cd': 'TSTV', 'subject': f'[DOCK][TSTV {req_no}]subject', 'status': status,
          'inq_no': inq_no, 'amt': None, 'cur': None, 'vendor': None}
    r = c.post('/api/ext/dock_procure/sync', json={'items': [it]}, headers=HDR)
    assert r.status_code == 200, r.status_code
    return r.get_json()


def row_of(req_no):
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)


def stages(req_no):
    r = row_of(req_no)
    return (r['stg_quote'], r['stg_vendor'], r['stg_confirm'], r['stg_order'])


def mkdraft(rid, rep_cd, status='submitted'):
    A.execute("DELETE FROM dock_inquiry_draft WHERE rep_cd=?", (rep_cd,))
    A.execute(
        "INSERT INTO dock_inquiry_draft(rid, vsl_nm, vsl_cd, req_no, rep_cd, vndr_json, vndr_names, "
        "envelope_json, status, decided_at, decided_by, done_at) "
        "VALUES(?,'TEST VESSEL','TSTV','R22',?,'[{\"cd\":\"A1MM3\",\"nm\":\"t\"}]','t','{}',?,"
        "datetime('now','localtime'),'SS0094',datetime('now','localtime'))",
        (rid, rep_cd, status))


print('# 1) allowlist 자체 — 실측된 pre-inquiry 라벨만 들어있고 빈 라벨은 없다')
chk('HQ RECEIVED' in A._DOCKPROC_PRE_INQUIRY, "'HQ RECEIVED' 등재(회수가 돌아오는 라벨)")
chk(A._DOCKPROC_PRE_INQUIRY == {'HQ RECEIVED'},
    "실측 라벨 1개만 — 'VSL Approved'/'Approved' 는 회수 경로 미확인이라 제외",
    A._DOCKPROC_PRE_INQUIRY)
chk('' not in A._DOCKPROC_PRE_INQUIRY, "빈 라벨은 미등재(수동관리 행 보호)")
chk('QUOTATION INQUIRY' not in A._DOCKPROC_PRE_INQUIRY, "rank>=1 라벨은 미등재")
chk(A._dockproc_status_rank('HQ Received') == 0, "rank('HQ Received') == 0")

print('# 2) 🔴 실사고 재현 — 회수(HQ Received)가 단계를 되돌린다')
rid = mkrow('R22', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
            svms_req_no='TSTVME26073116')
res = sync('R22', 'HQ Received', inq_no='TSTVME26073116')
chk(stages('R22') == (0, 0, 0, 0), '회수 → 단계 전부 해제', stages('R22'))
chk(row_of('R22')['svms_status'] == 'HQ Received', '라벨도 회수 상태로 갱신',
    row_of('R22')['svms_status'])
chk(res['updated'] == 1, 'changes 에 잡힘(link_only 로 새지 않음)', res)

print('# 3) 🔴 게이트가 실제로 다시 열린다 (submitted draft 가 남아 있어도)')
mkdraft(rid, 'TSTVME26073116')
chk(A._dock_inq_blocked(row_of('R22')) is None,
    '회수 후 견적요청 가능', A._dock_inq_blocked(row_of('R22')))

print("# 3-1) 대조군 — 회수 전(Quotation Inquiry) 에는 여전히 막힌다")
mkrow('R23', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
      svms_req_no='TSTVME26073117')
blocked = A._dock_inq_blocked(row_of('R23'))
chk(blocked is not None, '견적요청 나간 상태는 차단 유지', blocked)

print('# 4) 미지 라벨은 종전대로 link_only — 단계 보존(안전한 방향으로 실패)')
mkrow('R24', stages_on=(1, 1, 1, 0), svms_status='Quotation Inquiry',
      svms_req_no='TSTVME26073118')
sync('R24', 'Some Brand New Status', inq_no='TSTVME26073118')
chk(stages('R24') == (1, 1, 1, 0), '미지 라벨은 단계 안 건드림', stages('R24'))

print('# 5) 빈 라벨도 link_only — 사람이 켠 수동 체크 보존')
mkrow('R25', stages_on=(1, 1, 1, 1))
sync('R25', '')
chk(stages('R25') == (1, 1, 1, 1), "빈 라벨은 수동 체크 보존", stages('R25'))

print('# 6) allowlist 밖 rank0 라벨(VSL Approved / Approved)은 되돌리지 않는다 — 종전 link_only')
for rq, lbl in (('R26', 'VSL Approved'), ('R27', 'Approved')):
    mkrow(rq, stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
    sync(rq, lbl)
    chk(stages(rq) == (1, 1, 0, 0), f"'{lbl}' 는 미등재 → 단계 보존(닫힘 쪽)", stages(rq))

print('# 7) 🔴 fail-closed — 발주 흔적(발주완료/발주금액/submit)이 있으면 되돌리지 않는다')
#   stale·순서역전 sync 가 rank0 라벨을 실어와도 발주 이력을 조용히 지우지 못하게 하는 게이트.
mkrow('R28', stages_on=(1, 1, 1, 1), svms_status='HQ Ordered',
      quote_amt=18225000, remark='MARINE CORROSION SERVICE LIMITED')
sync('R28', 'HQ Received')
r28 = row_of('R28')
chk(stages('R28') == (1, 1, 1, 1), '발주완료+금액 보유 행은 단계 유지', stages('R28'))
chk(r28['quote_amt'] == 18225000, '발주금액 보존', r28['quote_amt'])
chk(r28['remark'] == 'MARINE CORROSION SERVICE LIMITED', 'remark 보존', r28['remark'])

print('# 7-1) fail-closed 개별 트리거 — 금액만 / submit만 있어도 되돌림 차단')
mkrow('R31', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry', quote_amt=100)
sync('R31', 'HQ Received')
chk(stages('R31') == (1, 1, 0, 0), '발주금액만 있어도 단계 유지', stages('R31'))
mkrow('R32', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
A.execute("UPDATE dock_procure SET svms_submit='2026-07-31' WHERE req_no='R32' AND vsl_nm='TEST VESSEL'")
sync('R32', 'HQ Received')
chk(stages('R32') == (1, 1, 0, 0), 'svms_submit 보유 행도 단계 유지', stages('R32'))

print('# 7-2) 충돌 입력 — allowlist 라벨 + 발주근거(ordered_evidence=True) 동시')
#   rank 는 라벨로 정하므로 rank0 이고, 발주근거 True 라도 발주완료로 승격되지 않는다.
mkrow('R33', stages_on=(0, 0, 0, 0), svms_status='Quotation Inquiry')
A.execute("UPDATE dock_procure SET stg_quote=1, stg_vendor=1 WHERE req_no='R33' AND vsl_nm='TEST VESSEL'")
it33 = {'vsl_cd': 'TSTV', 'subject': '[DOCK][TSTV R33]subject', 'status': 'HQ Received',
        'ordered_evidence': True, 'inq_no': None}
c.post('/api/ext/dock_procure/sync', json={'items': [it33]}, headers=HDR)
chk(stages('R33') == (0, 0, 0, 0),
    '라벨이 rank0 이면 발주근거가 True 라도 승격 없음 + 되돌림', stages('R33'))

print('# 8) 되돌림 경로도 svms_req_no 연결을 채운다(link_only 가 하던 일 회귀 방지)')
mkrow('R29', stages_on=(0, 0, 0, 0), svms_status=None, svms_req_no=None)
sync('R29', 'HQ Received', inq_no='TSTVME26073119')
chk(row_of('R29')['svms_req_no'] == 'TSTVME26073119', 'REP_CD 연결됨',
    row_of('R29')['svms_req_no'])
chk(row_of('R29')['svms_status'] == 'HQ Received', '라벨도 채움')

print('# 9) 멱등 — 같은 회수 상태를 두 번 sync 하면 두 번째는 변경 없음')
res2 = sync('R22', 'HQ Received', inq_no='TSTVME26073116')
chk(res2['updated'] == 0, '두 번째 sync 는 UPDATE 0', res2['updated'])
chk(stages('R22') == (0, 0, 0, 0), '값도 그대로', stages('R22'))

print('# 10) HQ Canceled 는 종전대로 완전 무시(되돌림 대상 아님)')
mkrow('R30', stages_on=(1, 1, 1, 1), svms_status='HQ Ordered')
res3 = sync('R30', 'HQ Canceled')
chk(stages('R30') == (1, 1, 1, 1), 'HQ Canceled 는 단계 유지', stages('R30'))
chk(res3['canceled_skipped'] == 1, 'canceled 로 집계', res3)

print('# 11) 라벨 변형 — None / 공백패딩 / 소문자')
mkrow('R35', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
r = c.post('/api/ext/dock_procure/sync',
           json={'items': [{'vsl_cd': 'TSTV', 'subject': '[DOCK][TSTV R35]subject', 'status': None}]},
           headers=HDR)
chk(r.status_code == 200, 'status=None 이 500 을 내지 않는다', r.status_code)
chk(stages('R35') == (1, 1, 0, 0), 'None 은 빈 라벨과 같게 link_only(수동 체크 보존)', stages('R35'))
for rq, lbl in (('R36', '  HQ Received  '), ('R37', 'hq received')):
    mkrow(rq, stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
    sync(rq, lbl)
    chk(stages(rq) == (0, 0, 0, 0), f"'{lbl}' 변형도 되돌림(strip/upper 정규화)", stages(rq))

print('# 12) 되돌림이 제출견적·첨부·submit 을 회수 상태로 정리한다 (3상태 계약 유지)')
mkrow('R38', stages_on=(1, 1, 1, 0), svms_status='Submit')
A.execute("UPDATE dock_procure SET sub_quotes='[{\"nm\":\"V\",\"amt\":1}]', att_files='[{\"nm\":\"a.pdf\"}]', "
          "svms_submit='2026-07-31' WHERE req_no='R38' AND vsl_nm='TEST VESSEL'")
c.post('/api/ext/dock_procure/sync',
       json={'items': [{'vsl_cd': 'TSTV', 'subject': '[DOCK][TSTV R38]subject',
                        'status': 'HQ Received'}]}, headers=HDR)
r38 = row_of('R38')
chk(stages('R38') == (1, 1, 1, 0),
    'svms_submit 보유 → fail-closed 로 단계 유지', stages('R38'))
chk(r38['sub_quotes'] == '[{"nm":"V","amt":1}]' and r38['att_files'] == '[{"nm":"a.pdf"}]',
    'link_only 경로는 제출견적·첨부를 건드리지 않는다', (r38['sub_quotes'], r38['att_files']))
mkrow('R39', stages_on=(1, 1, 0, 0), svms_status='Submit')
A.execute("UPDATE dock_procure SET sub_quotes='[{\"nm\":\"V\",\"amt\":1}]' "
          "WHERE req_no='R39' AND vsl_nm='TEST VESSEL'")
c.post('/api/ext/dock_procure/sync',
       json={'items': [{'vsl_cd': 'TSTV', 'subject': '[DOCK][TSTV R39]subject',
                        'status': 'HQ Received', 'quotes': []}]}, headers=HDR)
r39 = row_of('R39')
chk(stages('R39') == (0, 0, 0, 0), '발주근거 없으면 되돌림', stages('R39'))
chk(r39['sub_quotes'] is None, "quotes=[] → '제출 0건 확정' 이므로 clear (회수와 정합)", r39['sub_quotes'])

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
