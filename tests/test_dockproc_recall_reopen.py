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
chk('VSL APPROVED' in A._DOCKPROC_PRE_INQUIRY,
    "'VSL APPROVED' 등재 — 2026-08-04 구매 회수 실측(B41 회수 → REQ B4 가 STATUS='N' 로 복귀)")
chk(A._DOCKPROC_PRE_INQUIRY == {'HQ RECEIVED', 'VSL APPROVED'},
    "실측 라벨 2개만 — 'Approved' 는 회수 경로 미확인이라 여전히 제외",
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

print("# 6) 'VSL Approved' 는 되돌리고, 미등재 rank0 라벨('Approved')은 종전대로 보존한다")
#   2026-08-04 변경: 'VSL Approved' 가 구매 회수의 도착지로 실측돼 allowlist 에 들어왔다.
mkrow('R26', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
sync('R26', 'VSL Approved')
chk(stages('R26') == (0, 0, 0, 0), "'VSL Approved' 는 등재 → 단계 되돌림", stages('R26'))
mkrow('R27', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
sync('R27', 'Approved')
chk(stages('R27') == (1, 1, 0, 0), "'Approved' 는 미등재 → 단계 보존(닫힘 쪽)", stages('R27'))

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
chk(stages('R32') == (1, 1, 0, 0),
    'svms_submit 이 미지 형식이면 fail-closed 로 단계 유지', stages('R32'))

print('# 7-1a) 🔴 svms_submit 은 존재 여부가 아니라 **제출수>0** 으로 본다 (2026-08-04 실사고)')
#   견적요청이 나가면 이 칸은 곧바로 "0/0"·"0/1" 로 채워진다 ⇒ 존재 여부로 보면 회수 되돌림이 영구 무력화.
for _raw, _want, _why in (
        (None, False, 'None = 흔적 없음'),
        ('', False, '빈 문자열 = 흔적 없음'),
        ('   ', False, '공백만 = 흔적 없음'),
        ('0/0', False, '"0/0" = 제출 0 (S14 실측값)'),
        ('(0/1)', False, '"(0/1)" = 요청 1·제출 0 (정상 회수건 실측값)'),
        ('(0/4)', False, '"(0/4)" = 제출 0'),
        ('1/1', True, '"1/1" = 제출 1 → 흔적 있음'),
        ('(3/4)', True, '"(3/4)" = 제출 3 → 흔적 있음'),
        ('2026-07-31', True, '미지 형식 = fail-closed(True)'),
        ('submitted', True, '미지 문자열 = fail-closed(True)'),
        ('(0/1', True, '반쪽 괄호 = 미지 형식 → fail-closed (올마이트 지적)'),
        ('0/1)', True, '반쪽 괄호(닫는쪽) = fail-closed'),
        ('((0/1))', True, '이중 괄호 = fail-closed'),
        ('0/', True, '분모 없음 = fail-closed'),
        ('/1', True, '분자 없음 = fail-closed'),
        ('0/1/2', True, '슬래시 2개 = fail-closed'),
        ('-1/2', True, '음수 = fail-closed(정규식 미매칭)'),
        ('( 0 / 4 )', False, '괄호쌍+공백은 정상 형식으로 인정'),
        ('10/12', True, '두자리 분자 > 0')):
    chk(A._dockproc_submit_has_quotes(_raw) is _want,
        f'_dockproc_submit_has_quotes({_raw!r}) is {_want} — {_why}',
        A._dockproc_submit_has_quotes(_raw))

print('# 7-1b) e2e — 제출 0("0/0") 회수건은 되돌아가고, 제출 있는 행은 그대로 막힌다')
mkrow('R47', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
A.execute("UPDATE dock_procure SET svms_submit='0/0' WHERE req_no='R47' AND vsl_nm='TEST VESSEL'")
sync('R47', 'VSL Approved')
chk(stages('R47') == (0, 0, 0, 0),
    "제출 0 + allowlist 라벨 → 되돌림 (S14 실사고 경로)", stages('R47'))
mkrow('R48', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
A.execute("UPDATE dock_procure SET svms_submit='(3/4)' WHERE req_no='R48' AND vsl_nm='TEST VESSEL'")
sync('R48', 'VSL Approved')
chk(stages('R48') == (1, 1, 0, 0),
    '제출 3건 보유 행은 fail-closed 로 단계 유지', stages('R48'))

print("# 7-1c) 원인 분리 — 'HQ Received'(기존 등재 라벨) + 0/0 만으로도 되돌아간다")
#   올마이트 지적: #7-1b 는 svms_submit 수정과 allowlist 추가를 함께 태워 원인이 섞인다.
#   이 케이스는 allowlist 를 안 건드리고 svms_submit 수정만 검증한다(수정 되돌리면 여기서 먼저 빨개짐).
mkrow('R51', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
A.execute("UPDATE dock_procure SET svms_submit='0/0' WHERE req_no='R51' AND vsl_nm='TEST VESSEL'")
sync('R51', 'HQ Received')
chk(stages('R51') == (0, 0, 0, 0),
    "'HQ Received' + 제출 0 → 되돌림 (svms_submit 수정 단독 효과)", stages('R51'))
mkrow('R52', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
A.execute("UPDATE dock_procure SET svms_submit='(0/1' WHERE req_no='R52' AND vsl_nm='TEST VESSEL'")
sync('R52', 'HQ Received')
chk(stages('R52') == (1, 1, 0, 0),
    '반쪽 괄호는 미지 형식 → 단계 유지(닫힘 쪽)', stages('R52'))

print('# 7-1d) 🔴 하류 스냅샷(sub_quotes/att_files)은 되돌림을 막지 않는다 — 의도된 판정')
#   올마이트 대안(하류 evidence 를 가드에 넣기)에 **반대**한 지점. 그 두 칸은 폴러 payload 3상태 계약이
#   정본이고 저장값은 지난 sync 스냅샷일 뿐 → 가드에 넣으면 회수건이 옛 스냅샷 때문에 영구 잠긴다.
#   라이브 실측(161행): 제출0/NULL + 하류데이터 + 발주흔적없음 조합 = 0행.
#   여기서는 **현재 동작을 못박아** 나중에 조용히 바뀌는 걸 막는다.
for _rq, _sql, _why in (
        ('R53', "sub_quotes='[{\"nm\":\"V\",\"amt\":1}]'", 'sub_quotes 스냅샷만 있음'),
        ('R54', "att_files='[{\"nm\":\"a.pdf\"}]'", 'att_files 스냅샷만 있음'),
        ('R55', "sub_quotes='[{\"nm\":\"V\",\"amt\":1}]', att_files='[{\"nm\":\"a.pdf\"}]'", '둘 다 있음')):
    mkrow(_rq, stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry')
    A.execute("UPDATE dock_procure SET svms_submit='0/1', %s "
              "WHERE req_no=? AND vsl_nm='TEST VESSEL'" % _sql, (_rq,))
    sync(_rq, 'HQ Received')
    chk(stages(_rq) == (0, 0, 0, 0), f'{_why} → 되돌림 (가드 대상 아님)', stages(_rq))
    _r = row_of(_rq)
    chk(_r['sub_quotes'] is not None or 'sub_quotes' not in _sql,
        f'{_why} — quotes 키 미전송이므로 스냅샷 자체는 보존', _r['sub_quotes'])
    chk(_r['att_files'] is not None or 'att_files' not in _sql,
        f'{_why} — files 키 미전송이므로 첨부 스냅샷 보존', _r['att_files'])

print("# 7-1e) 'VSL Approved' 가 정상 진행 행을 되돌리지 않는다 (신규·stale 시나리오)")
#   ① 처음부터 VSL Approved 로 들어오는 신규 행 = 단계 0 이라 되돌릴 것이 없다(no-op).
mkrow('R56', stages_on=(0, 0, 0, 0), svms_status=None)
sync('R56', 'VSL Approved')
chk(stages('R56') == (0, 0, 0, 0), '신규 행은 no-op', stages('R56'))
chk(row_of('R56')['svms_status'] == 'VSL Approved', '라벨은 채워짐', row_of('R56')['svms_status'])
#   ② 발주완료 행에 stale VSL Approved 가 도착 = fail-closed 로 방어(유일한 안전선).
mkrow('R57', stages_on=(1, 1, 1, 1), svms_status='HQ Ordered', quote_amt=999)
sync('R57', 'VSL Approved')
chk(stages('R57') == (1, 1, 1, 1), 'stale 라벨이 발주완료를 지우지 못한다', stages('R57'))
chk(row_of('R57')['quote_amt'] == 999, '발주금액도 보존', row_of('R57')['quote_amt'])
#   ③ 제출 있는 진행 행(제출수>0)도 방어.
mkrow('R58', stages_on=(1, 1, 1, 0), svms_status='Submit')
A.execute("UPDATE dock_procure SET svms_submit='2/4' WHERE req_no='R58' AND vsl_nm='TEST VESSEL'")
sync('R58', 'VSL Approved')
chk(stages('R58') == (1, 1, 1, 0), '제출 2건 진행 행도 유지', stages('R58'))

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

print('# 13) 🔴 회수 되돌림이 직전 견적요청 이력을 recalled 로 무효화한다 (형 지시: 재적재 시 초기화)')
#   웹/앱은 `status=='submitted'` 를 '견적요청됨 ✓' 으로 그린다 → 남아 있으면 실제와 불일치.
rid40 = mkrow('R40', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
              svms_req_no='TSTVME26073140')
mkdraft(rid40, 'TSTVME26073140', status='submitted')
sync('R40', 'HQ Received', inq_no='TSTVME26073140')
d40 = A.query("SELECT status, result FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073140'", one=True)
chk(d40['status'] == 'recalled', "submitted 이력 → 'recalled'", d40['status'])
chk('회수로 무효화' in (d40['result'] or ''), '무효화 사유가 result 에 남는다', d40['result'])
chk(A.query("SELECT COUNT(*) n FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073140'",
            one=True)['n'] == 1, '행은 지우지 않고 보존(append-only 이력)')
chk(A._dock_inq_blocked(row_of('R40')) is None, '게이트도 열린 상태', A._dock_inq_blocked(row_of('R40')))

print('# 13-1) failed 이력도 같이 무효화 — 회수 후엔 옛 실패표시도 실제와 무관')
rid41 = mkrow('R41', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
              svms_req_no='TSTVME26073141')
mkdraft(rid41, 'TSTVME26073141', status='failed')
sync('R41', 'HQ Received', inq_no='TSTVME26073141')
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073141'",
            one=True)['status'] == 'recalled', "failed 이력 → 'recalled'")

print('# 13-2) 🔴 활성 큐(approved/submitting)는 절대 건드리지 않는다 — 워커 소유·전송 중일 수 있음')
for st in ('approved', 'submitting'):
    rq = 'R42' if st == 'approved' else 'R43'
    rep = 'TSTVME2607314' + ('2' if st == 'approved' else '3')
    rid_ = mkrow(rq, stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry', svms_req_no=rep)
    mkdraft(rid_, rep, status=st)
    sync(rq, 'HQ Received', inq_no=rep)
    got = A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd=?", (rep,), one=True)['status']
    chk(got == st, f"'{st}' 큐는 그대로 유지", got)
    blk = A._dock_inq_blocked(row_of(rq))
    chk(blk is not None and '큐에 있음' in blk, f"'{st}' 는 '이미 큐에 있음'으로 계속 차단", blk)

print('# 13-3) 되돌림이 아닌 경로(link_only / 정상 진행)는 이력을 건드리지 않는다')
rid44 = mkrow('R44', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
              svms_req_no='TSTVME26073144')
mkdraft(rid44, 'TSTVME26073144', status='submitted')
sync('R44', 'Some Unknown Status', inq_no='TSTVME26073144')   # 미지 라벨 = link_only
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073144'",
            one=True)['status'] == 'submitted', '미지 라벨(link_only)은 이력 유지')
sync('R44', 'Submit', inq_no='TSTVME26073144')                # 정상 진행(rank 2)
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073144'",
            one=True)['status'] == 'submitted', '정상 진행도 이력 유지')

print('# 13-4) fail-closed 로 되돌림이 막힌 행은 이력도 건드리지 않는다(일관성)')
rid45 = mkrow('R45', stages_on=(1, 1, 1, 1), svms_status='HQ Ordered',
              svms_req_no='TSTVME26073145', quote_amt=100)
mkdraft(rid45, 'TSTVME26073145', status='submitted')
sync('R45', 'HQ Received', inq_no='TSTVME26073145')
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073145'",
            one=True)['status'] == 'submitted', '발주근거 보유 → 단계·이력 모두 유지')

print('# 13-5) 🔴 이력 무효화도 제출수>0 기준을 쓴다 (2군데 가드 일관성)')
#   갈래 ②(_had_post) 가드가 `svms_submit` 존재 여부를 보던 탓에 회수건 이력이 영구히 submitted 로 남았다.
rid49 = mkrow('R49', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
              svms_req_no='TSTVME26073149')
A.execute("UPDATE dock_procure SET svms_submit='(0/1)' WHERE req_no='R49' AND vsl_nm='TEST VESSEL'")
mkdraft(rid49, 'TSTVME26073149', status='submitted')
sync('R49', 'HQ Received', inq_no='TSTVME26073149')
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073149'",
            one=True)['status'] == 'recalled', "제출 0 회수건 → 이력도 'recalled'")
rid50 = mkrow('R50', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
              svms_req_no='TSTVME26073150')
A.execute("UPDATE dock_procure SET svms_submit='(2/4)' WHERE req_no='R50' AND vsl_nm='TEST VESSEL'")
mkdraft(rid50, 'TSTVME26073150', status='submitted')
sync('R50', 'HQ Received', inq_no='TSTVME26073150')
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073150'",
            one=True)['status'] == 'submitted', '제출 2건 보유 → 이력 유지(닫힘 쪽)')

print('# 14) 이력 여러 건 / result 서식 / dry-run / 멱등 (올마이트 지적 갭)')
rid46 = mkrow('R46', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
              svms_req_no='TSTVME26073146')
for i, (st, res) in enumerate((('submitted', None), ('failed', 'pre-read 차단'), ('submitted', ''))):
    A.execute(
        "INSERT INTO dock_inquiry_draft(rid, vsl_nm, vsl_cd, req_no, rep_cd, vndr_json, vndr_names, "
        "envelope_json, status, result, done_at) VALUES(?,'TEST VESSEL','TSTV','R46',"
        "'TSTVME26073146','[]','t','{}',?,?,datetime('now','localtime'))", (rid46, st, res))
sync('R46', 'HQ Received', inq_no='TSTVME26073146')
ds = A.query("SELECT status, result FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073146' ORDER BY id")
chk(len(ds) == 3 and all(d['status'] == 'recalled' for d in ds),
    '같은 rep_cd 의 종료 이력 3건 모두 전이', [d['status'] for d in ds])
chk(not ds[0]['result'].startswith(' · ') and not ds[2]['result'].startswith(' · '),
    "result 가 비어 있으면 선행 ' · ' 안 붙음", [d['result'] for d in ds])
chk(ds[1]['result'].startswith('pre-read 차단 · '), '기존 result 는 보존하고 뒤에 append',
    ds[1]['result'])

print('# 14-1) dry-run 은 이력을 바꾸지 않는다')
rid47 = mkrow('R47', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
              svms_req_no='TSTVME26073147')
mkdraft(rid47, 'TSTVME26073147', status='submitted')
r = c.post('/api/ext/dock_procure/sync',
           json={'dry': True, 'items': [{'vsl_cd': 'TSTV', 'subject': '[DOCK][TSTV R47]subject',
                                         'status': 'HQ Received'}]}, headers=HDR)
chk(r.get_json()['dry'] is True and A.query(
    "SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073147'",
    one=True)['status'] == 'submitted', 'dry 는 이력·단계 모두 불변')

print('# 14-2) 반복 sync 멱등 — 두 번째엔 전이할 게 없다(이미 recalled)')
sync('R47', 'HQ Received')                                     # 1차: 실제 전이
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073147'",
            one=True)['status'] == 'recalled', '1차 sync 에서 전이')
j2 = sync('R47', 'HQ Received')
chk(j2['updated'] == 0, '2차 sync 는 변경 0(단계가 이미 0 → 전이 조건 미성립)', j2['updated'])

print('# 14-3) svms_req_no 가 비고 inq_no 로만 매칭되는 행도 이력을 전이한다')
rid48 = mkrow('R48', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry', svms_req_no=None)
mkdraft(rid48, 'TSTVME26073148', status='submitted')
sync('R48', 'HQ Received', inq_no='TSTVME26073148')
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073148'",
            one=True)['status'] == 'recalled', 'inq_no 로 rep_cd 를 찾아 전이')

print("# 14-4) '완료건 지우기' 가 recalled 도 지운다(큐 목록에 영구 잔류 방지)")
n = c.delete('/api/dock_inquiry/drafts/decided')
chk(n.status_code in (200, 302, 401, 403), '라우트 응답', n.status_code)
if n.status_code == 200:
    left = A.query("SELECT COUNT(*) n FROM dock_inquiry_draft WHERE status='recalled'", one=True)['n']
    chk(left == 0, 'recalled 가 남지 않는다', left)
else:
    chk(A._DOCK_SUBMIT_DONE + ('recalled',) == ('submitted', 'failed', 'canceled', 'recalled'),
        "인증 없이 호출 불가 → 종료상태 집합으로 대신 확인")

print('# 15) 🔴 구매 회수 2차 실사고(형 제보 BGBB S14) — 라벨 재해석·키·전이조건 3중 갭')
# 실측 상태: cat_code='S', svms_status='HQ Confirmed', svms_req_no=NULL,
#   svms_pc_req_no='BGBBES2607B4', 단계 (1,0,0,0), draft(status='failed', rep_cd=pc키).
# ① 화면이 'HQ Confirmed' 를 'confirmed' 부분일치로 '견적요청 이후'로 오판 → 실패 이력이 초록 ✓
# ② 전이 조건이 '단계 전부 꺼짐' 이라 rank1(견적작성 잔존)인 구매 회수에 안 걸림
# ③ 전이 SQL 의 키가 `svms_req_no` 뿐이라 구매 draft(rep_cd=REQ_NO)와 절대 안 맞음
chk(A._dockproc_inq_posted('PCRQ', 'HQ Confirmed') is False,
    "구매 'HQ Confirmed' = 견적요청 **전**(denylist 부분일치보다 _DOCK_INQ_PRE 우선)")
chk(A._dockproc_inq_posted('PCRQ', '  hq confirmed ') is False, '대소문자·공백 정규화')
chk(A._dockproc_inq_posted('PCRQ', 'VSL Approved') is False, "구매 'VSL Approved' = 요청 전")
chk(A._dockproc_inq_posted('MARP', 'HQ Confirmed') is True,
    "수리는 종전 denylist 그대로 — 회수 라벨 미실측이라 확장하지 않음")
chk(A._dockproc_inq_posted('PCRQ', 'Quotation Inquiry') is True, "'Quotation Inquiry' = 요청 이후")
chk(A._dockproc_inq_posted('PCRQ', 'Ordered') is True, "'Ordered' = 요청 이후")
chk(A._dockproc_inq_posted('PCRQ', '') is False, '빈 라벨 = 판정 불가 → False(실패는 실패로 보인다)')
chk(A._dockproc_inq_posted('PCRQ', None) is False, 'None 안전')
chk(A._dockproc_inq_posted('', 'HQ Confirmed') is True,
    '문서종류 미상은 예외 없음 = 종전 denylist (닫힘 쪽)')

print('# 15-1) `_dock_inq_prior` 리팩터 동등성 — 구매는 재개방, 수리는 계속 차단')
mkdraft(999, 'TSTVES2607PRIOR', status='submitted')
chk(A._dock_inq_prior('PCRQ', 'TSTVES2607PRIOR', 'HQ Confirmed') is None,
    "구매 'HQ Confirmed' + submitted 이력 → 재개방")
chk(A._dock_inq_prior('PCRQ', 'TSTVES2607PRIOR', 'Quotation Inquiry') is not None,
    "요청 이후 라벨이면 계속 차단")
chk(A._dock_inq_prior('MARP', 'TSTVES2607PRIOR', 'HQ Confirmed') is not None,
    "수리는 종전대로 차단(동등성)")
A.execute("DELETE FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607PRIOR'")


def mkpc(req_no, stages_on, svms_status, pc_key):
    """구매(S) 행 — 견적요청 키는 `svms_pc_req_no` 다(`svms_req_no` 아님)."""
    rid = mkrow(req_no, stages_on=stages_on, svms_status=svms_status, cat_code='S')
    A.execute("UPDATE dock_procure SET svms_pc_req_no=? WHERE id=?", (pc_key, rid))
    return rid


print('# 15-2) 🔴 구매 회수(→HQ Confirmed, rank1) 가 견적요청 이력을 초기화한다')
rid_s14 = mkpc('S14', (1, 1, 1, 0), 'Quotation Inquiry', 'TSTVES2607B4')
mkdraft(rid_s14, 'TSTVES2607B4', status='submitted')
sync('S14', 'HQ Confirmed')
chk(stages('S14') == (1, 0, 0, 0), '단계는 견적작성만 남는다(rank 1)', stages('S14'))
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607B4'",
            one=True)['status'] == 'recalled',
    '견적요청 이력 → recalled (구매 키 `svms_pc_req_no` 로 매칭)')
chk(A._dock_inq_blocked(row_of('S14')) is None, '재요청 게이트도 열려 있다',
    A._dock_inq_blocked(row_of('S14')))

print("# 15-3) 형 지시 '초기화' — failed 이력도 같이 무효화한다(사유가 이미 낡았다)")
rid_s15 = mkpc('S15', (1, 1, 0, 0), 'Quotation Inquiry', 'TSTVES2607B5')
mkdraft(rid_s15, 'TSTVES2607B5', status='failed')
sync('S15', 'HQ Confirmed')
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607B5'",
            one=True)['status'] == 'recalled', 'failed → recalled')

print('# 15-4) 과잉전이 방지 — 견적요청 이후 단계가 애초에 없던 행은 안 건드린다')
rid_s16 = mkpc('S16', (1, 0, 0, 0), 'VSL Approved', 'TSTVES2607B6')
mkdraft(rid_s16, 'TSTVES2607B6', status='failed')
sync('S16', 'HQ Confirmed')
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607B6'",
            one=True)['status'] == 'failed', '되돌림이 아니므로 이력 유지')

print('# 15-5) 활성 큐(approved/submitting)는 절대 건드리지 않는다 — 워커 소유')
rid_s17 = mkpc('S17', (1, 1, 1, 0), 'Quotation Inquiry', 'TSTVES2607B7')
mkdraft(rid_s17, 'TSTVES2607B7', status='approved')
sync('S17', 'HQ Confirmed')
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607B7'",
            one=True)['status'] == 'approved', '전송 대기 행은 그대로')

print('# 15-6) 라벨 allowlist 는 문서종류별 — 수리에서 HQ Confirmed 는 전이 라벨이 아니다')
rid_r49 = mkrow('R49', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
                svms_req_no='TSTVME26073149')
mkdraft(rid_r49, 'TSTVME26073149', status='submitted')
sync('R49', 'HQ Confirmed')
chk(stages('R49') == (1, 0, 0, 0), '단계는 rank 대로 되돌아간다', stages('R49'))
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVME26073149'",
            one=True)['status'] == 'submitted',
    '수리 이력은 유지 — 미실측 라벨로 이력을 지우지 않는다(추측 금지)')

print('# 15-7) 🔴 갈래 ② fail-closed — 발주 흔적이 있으면 이력을 무효화하지 않는다(올마이트 지적)')
for _tag, _kw in (('S18', "stg_order"), ('S19', 'quote_amt'), ('S20', 'svms_submit')):
    _rid = mkpc(_tag, (1, 1, 1, 1 if _kw == 'stg_order' else 0), 'Ordered', f'TSTVES2607{_tag}')
    if _kw == 'quote_amt':
        A.execute("UPDATE dock_procure SET quote_amt=1234.0 WHERE id=?", (_rid,))
    elif _kw == 'svms_submit':
        A.execute("UPDATE dock_procure SET svms_submit='SUB-1' WHERE id=?", (_rid,))
    mkdraft(_rid, f'TSTVES2607{_tag}', status='submitted')
    sync(_tag, 'HQ Confirmed')                                  # stale·순서역전 sync 가정
    chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd=?",
                (f'TSTVES2607{_tag}',), one=True)['status'] == 'submitted',
        f'{_kw} 있으면 이력 보존(닫힘 쪽 실패)')

print('# 15-8) 같은 rep_cd 의 이력 여러 건이면 종료상태 전부 전이 · 활성건은 보호')
rid_s21 = mkpc('S21', (1, 1, 1, 0), 'Quotation Inquiry', 'TSTVES2607B21')
for _st in ('submitted', 'failed', 'submitting'):
    A.execute(
        "INSERT INTO dock_inquiry_draft(rid, vsl_nm, vsl_cd, req_no, rep_cd, vndr_json, vndr_names, "
        "envelope_json, status, decided_at, decided_by, done_at) "
        "VALUES(?,'TEST VESSEL','TSTV','S21','TSTVES2607B21','[]','t','{}',?,"
        "datetime('now','localtime'),'SS0094',datetime('now','localtime'))", (rid_s21, _st))
sync('S21', 'HQ Confirmed')
_sts = sorted(r['status'] for r in A.query(
    "SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607B21'"))
chk(_sts == ['recalled', 'recalled', 'submitting'],
    'submitted·failed 는 recalled, submitting 은 보호', _sts)

print('# 15-9) 구매 갈래 멱등 — 2차 sync 는 전이할 게 없다')
j3 = sync('S21', 'HQ Confirmed')
chk(j3['updated'] == 0, '변경 0(단계가 이미 되돌아가 있음)', j3['updated'])
chk(sorted(r['status'] for r in A.query(
    "SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607B21'"))
    == ['recalled', 'recalled', 'submitting'], '상태도 그대로')

print("# 15-10) `_dock_inq_prior` 빈 라벨 직접 동등성 — 라벨 없으면 열어준다")
mkdraft(998, 'TSTVES2607EMPTY', status='submitted')
for _lbl in ('', '   ', None):
    chk(A._dock_inq_prior('PCRQ', 'TSTVES2607EMPTY', _lbl) is None, f'빈 라벨({_lbl!r}) → 재개방')
    chk(A._dock_inq_prior('MARP', 'TSTVES2607EMPTY', _lbl) is None, f'수리도 동일({_lbl!r})')
A.execute("DELETE FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607EMPTY'")

print('# 15-11) 목록 API 가 `inq_posted` 를 실제로 내려준다(화면이 라벨을 다시 안 읽는 전제)')
with c.session_transaction() as s:
    s['user_id'] = 1
    s['role'] = 'admin'
lr = c.get('/api/dock_procure/lines?vsl=TEST VESSEL')
# 🔴 401/302 를 통과로 봐주지 않는다(올마이트 지적) — 그러면 payload 검증이 조용히 생략된다.
chk(lr.status_code == 200, '세션 심어 목록 조회 200', lr.status_code)
if lr.status_code == 200:
    lns = {x['req_no']: x for x in (lr.get_json() or {}).get('lines', [])}
    s14 = lns.get('S14') or {}
    chk('inq_posted' in s14, '행 payload 에 키 존재', sorted(s14.keys())[:8])
    chk(s14.get('inq_posted') is False, "구매 'HQ Confirmed' 는 False 로 내려간다",
        s14.get('inq_posted'))
    chk(s14.get('inq_doc') == 'PCRQ' and s14.get('inq_key') == 'TSTVES2607B4',
        '문서종류·키도 서버 판정 그대로', (s14.get('inq_doc'), s14.get('inq_key')))
    r49 = lns.get('R49') or {}
    chk(r49.get('inq_posted') is True,
        "수리 'HQ Confirmed' 는 종전 denylist 대로 True", r49.get('inq_posted'))
    # 페인트/기타처럼 `inq_doc` 이 없는 행은 버튼 자체가 안 그려지므로 `inq_posted` 값이 소비되지 않는다.
    _p = mkrow('P9', stages_on=(1, 0, 0, 0), svms_status='HQ Confirmed', cat_code='P')
    lr2 = c.get('/api/dock_procure/lines?vsl=TEST VESSEL')
    p9 = {x['req_no']: x for x in (lr2.get_json() or {}).get('lines', [])}.get('P9') or {}
    chk((p9.get('inq_key') or '') == '', '문서종류 없는 행은 키가 비어 버튼이 닫힌다', p9.get('inq_key'))

print('# 16) 🔴 회수 후 죽은 INQ_NO(`svms_req_no`) 가 남아 새 번호를 막던 갭 (2026-08-04)')
#   구매 회수는 SVMS 에서 INQ_NO 자체를 삭제한다(`SP_SET_INQ_RTN`, B41 실측). 그런데 본문 UPDATE 가
#   `COALESCE(svms_req_no,?)` 라 한 번 박힌 값은 영영 안 덮여서 ①죽은 번호를 계속 표시하고
#   ②재요청으로 나온 새 INQ_NO 가 그 자리에 못 들어오며 ③Phase ③ 상신이 그 죽은 번호를 rep_cd 로 쓴다.


def mkpc2(req_no, stages_on, svms_status, pc_key, inq_no, **kw):
    """구매 행 — 요청키(`svms_pc_req_no`)와 발급된 INQ_NO(`svms_req_no`)를 **둘 다** 채운다."""
    rid = mkpc(req_no, stages_on, svms_status, pc_key)
    A.execute("UPDATE dock_procure SET svms_req_no=?, svms_submit=? WHERE id=?",
              (inq_no, kw.get('svms_submit', '0/1'), rid))
    if kw.get('quote_amt') is not None:
        A.execute("UPDATE dock_procure SET quote_amt=? WHERE id=?", (kw['quote_amt'], rid))
    return rid


rid_s30 = mkpc2('S30', (1, 1, 1, 0), 'Quotation Inquiry', 'TSTVES2607C1', 'TSTVES2607C11')
sync('S30', 'HQ Confirmed')                                     # 회수 = INQ_NO 사라진 상태로 돌아옴
r30 = row_of('S30')
chk((r30['svms_req_no'] or '') == '', '구매 회수 → 죽은 INQ_NO 비움', r30['svms_req_no'])
chk(r30['svms_pc_req_no'] == 'TSTVES2607C1',
    '요청키(`svms_pc_req_no`)는 보존 — 이게 없으면 재요청 버튼이 죽는다', r30['svms_pc_req_no'])
chk(A._dockproc_inq_target(row_of('S30')) == ('PCRQ', 'TSTVES2607C1'),
    '견적요청 대상 판정 그대로', A._dockproc_inq_target(row_of('S30')))

print('# 16-1) 🔴 이 수정의 목적 — 재요청으로 나온 **새 INQ_NO** 가 실제로 적재된다')
sync('S30', 'Quotation Inquiry', inq_no='TSTVES2607C12')        # 재요청 → SVMS 가 새 번호 발급
chk(row_of('S30')['svms_req_no'] == 'TSTVES2607C12',
    '비워진 칸에 새 번호가 COALESCE 로 들어옴', row_of('S30')['svms_req_no'])

print('# 16-1a) 대조군 — 안 비우면 새 번호가 들어오지 못한다(COALESCE 특성 자체를 못박음)')
rid_s31 = mkpc2('S31', (1, 1, 1, 0), 'Quotation Inquiry', 'TSTVES2607C2', 'TSTVES2607C21')
sync('S31', 'Quotation Inquiry', inq_no='TSTVES2607C22')        # 회수 없이 번호만 바뀐 척
chk(row_of('S31')['svms_req_no'] == 'TSTVES2607C21',
    '회수 전이가 없으면 옛 번호 유지(=이 갭의 원인)', row_of('S31')['svms_req_no'])

print('# 16-2) 수리는 절대 안 비운다 — `svms_req_no` 가 REP_CD = 견적요청 키 그 자체')
mkrow('R40', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
      svms_req_no='TSTVME2607D11', cat_code='R')
sync('R40', 'HQ Received')                                      # 수리 회수 실측 경로(단계 통째 되돌림)
chk(stages('R40') == (0, 0, 0, 0), '수리 회수 전이는 종전대로 동작', stages('R40'))
chk(row_of('R40')['svms_req_no'] == 'TSTVME2607D11',
    '수리 키는 보존(비우면 재요청 버튼이 죽는다)', row_of('R40')['svms_req_no'])
chk(A._dock_inq_blocked(row_of('R40')) is None, '수리 재요청 게이트도 열려 있다',
    A._dock_inq_blocked(row_of('R40')))

print('# 16-3) 이번 sync 가 INQ_NO 를 실어오면(= SVMS 에 살아있음) 안 비운다')
mkpc2('S32', (1, 1, 1, 0), 'Quotation Inquiry', 'TSTVES2607C3', 'TSTVES2607C31')
sync('S32', 'HQ Confirmed', inq_no='TSTVES2607C31')
chk(row_of('S32')['svms_req_no'] == 'TSTVES2607C31',
    'SVMS 가 아직 번호를 주면 손대지 않음', row_of('S32')['svms_req_no'])

print('# 16-4) 🔴 fail-closed — 발주 흔적이 있으면 비우지 않는다')
for _tag, _kw, _why in (
        ('S33', {'quote_amt': 1234.0}, '발주금액 보유'),
        ('S34', {'svms_submit': '3/4'}, '제출수>0'),
        ('S35', {'svms_submit': '2026-07-31'}, '미지 형식(fail-closed)')):
    mkpc2(_tag, (1, 1, 1, 0), 'Quotation Inquiry', f'TSTVES2607{_tag}', f'TSTVES2607{_tag}1', **_kw)
    sync(_tag, 'HQ Confirmed')
    chk(row_of(_tag)['svms_req_no'] == f'TSTVES2607{_tag}1', f'{_why} → 번호 보존')

print('# 16-5) 발주완료 행은 회수 전이 자체가 없다(중복 방어 확인)')
#   ⚠️단계는 라벨 rank 로 다시 계산되므로 (1,0,0,0) 이 되는 게 **정상**이다(회수 전이와 무관한 종전 동작).
#     여기서 확인할 것은 ①번호 보존 ②견적요청 이력이 `recalled` 로 무효화되지 않음 두 가지다.
rid_s36 = mkpc2('S36', (1, 1, 1, 1), 'Ordered', 'TSTVES2607C6', 'TSTVES2607C61', svms_submit=None)
mkdraft(rid_s36, 'TSTVES2607C6', status='submitted')
sync('S36', 'HQ Confirmed')
chk(row_of('S36')['svms_req_no'] == 'TSTVES2607C61', '발주완료 → 번호 보존',
    row_of('S36')['svms_req_no'])
chk(A.query("SELECT status FROM dock_inquiry_draft WHERE rep_cd='TSTVES2607C6'",
            one=True)['status'] == 'submitted', '견적요청 이력도 보존(fail-closed)')

print("# 16-6) 문서종류 미상(페인트 P)은 손대지 않는다")
_pid = mkrow('P10', stages_on=(1, 1, 0, 0), svms_status='Quotation Inquiry',
             svms_req_no='TSTVPA2607E11', cat_code='P')
sync('P10', 'HQ Received')
chk(row_of('P10')['svms_req_no'] == 'TSTVPA2607E11',
    '봉투 없는 종류는 비우지 않음', row_of('P10')['svms_req_no'])

print('# 16-7) 🔴 실제 위험 차단 — 비운 뒤 Phase ③ 상신이 죽은 번호로 나가지 못한다')
with c.session_transaction() as s:
    s['user_id'] = 1
    s['username'] = 'SS0094'
    s['role'] = 'admin'
rid_s37 = mkpc2('S37', (1, 1, 1, 0), 'Quotation Inquiry', 'TSTVES2607C7', 'TSTVES2607C71')
pv0 = c.get(f'/api/dock_submit/preview?rid={rid_s37}')
chk(pv0.status_code == 200, '상신 미리보기 200(회수 전)', pv0.status_code)
chk((pv0.get_json() or {}).get('rep_cd') == 'TSTVES2607C71',
    '회수 전에는 그 번호로 상신 대상이 잡힌다', (pv0.get_json() or {}).get('rep_cd'))
sync('S37', 'HQ Confirmed')
pv1 = c.get(f'/api/dock_submit/preview?rid={rid_s37}')
j37 = pv1.get_json() or {}
chk(j37.get('rep_cd') is None, '회수 후에는 상신 대상 번호가 없다', j37.get('rep_cd'))
chk((j37.get('blocked') or '').startswith('SVMS 문서번호'),
    '삭제된 번호로 상신하는 대신 차단된다', j37.get('blocked'))
cr = c.post('/api/dock_submit/drafts', json={'rid': rid_s37, 'vndr_cd': 'A1MM3', 'app_no': 'AP01'})
chk(cr.status_code == 400, '상신 생성도 400 으로 막힌다', cr.status_code)

print('# 16-8) 멱등 — 같은 sync 를 반복해도 값이 흔들리지 않는다')
#   S30 은 16-1 에서 새 번호(C12)를 받은 상태. 회수 라벨을 두 번 더 보내 최종값을 **정확히** 못박는다.
sync('S30', 'HQ Confirmed')                                     # 1차: C12 가 죽은 번호가 되어 비워짐
chk((row_of('S30')['svms_req_no'] or '') == '', '1차 회수 sync → 비움', row_of('S30')['svms_req_no'])
sync('S30', 'HQ Confirmed')                                     # 2차: 비울 것이 없다
chk((row_of('S30')['svms_req_no'] or '') == '', '2차도 동일(멱등)', row_of('S30')['svms_req_no'])

print('# 16-9) 🔴 dry-run 은 아무것도 안 지운다')
mkpc2('S38', (1, 1, 1, 0), 'Quotation Inquiry', 'TSTVES2607C8', 'TSTVES2607C81')
it = {'vsl_cd': 'TSTV', 'subject': '[DOCK][TSTV S38]subject', 'status': 'HQ Confirmed',
      'inq_no': None, 'amt': None, 'cur': None, 'vendor': None}
jd = c.post('/api/ext/dock_procure/sync', json={'items': [it], 'dry': True}, headers=HDR).get_json()
chk(row_of('S38')['svms_req_no'] == 'TSTVES2607C81', 'dry-run 후에도 번호 그대로',
    row_of('S38')['svms_req_no'])
chk(jd['dry'] is True and jd['updated'] == 1, 'dry 응답 자체는 변경 예정으로 잡힘',
    (jd['dry'], jd['updated']))
jw = sync('S38', 'HQ Confirmed')                                # 진짜로 돌리면 비워진다
chk((row_of('S38')['svms_req_no'] or '') == '', '실행하면 비워짐', row_of('S38')['svms_req_no'])
_ch = [x for x in jw['changes'] if x['req_no'] == 'S38']
chk(len(_ch) == 1 and _ch[0].get('req_no_cleared') == 'TSTVES2607C81',
    '응답 changes 에 지운 번호가 남는다(별개 write 가시화)', _ch)

print('# 16-10) 오판(사실은 살아있던 번호)이어도 다음 sync 가 스스로 복구한다')
#   SVMS 응답 누락으로 INQ_NO 가 빠진 채 회수 라벨이 오면 비울 수 있다. 그래도 self-healing 이다 —
#   번호가 살아있으면 다음 sync 가 그 번호를 실어오고, 빈 칸이므로 COALESCE 가 그대로 채운다.
sync('S38', 'Quotation Inquiry', inq_no='TSTVES2607C81')
chk(row_of('S38')['svms_req_no'] == 'TSTVES2607C81', '다음 sync 가 원래 번호를 되채움',
    row_of('S38')['svms_req_no'])

print('# 16-11) 다른 INQ_NO 가 들어와도 교체하지 않는다(설계 — 한 REQ 에 INQ 공존 실측)')
#   덮으면 Phase ③ rep_cd 가 sync 마다 흔들린다. 대신 서버 로그 warning 으로 관측한다.
import logging as _lg


class _Cap(_lg.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, rec):
        self.msgs.append(rec.getMessage())


_cap = _Cap()
A.app.logger.addHandler(_cap)
sync('S38', 'Quotation Inquiry', inq_no='TSTVES2607C89')        # 같은 REQ 의 다른 INQ
A.app.logger.removeHandler(_cap)
chk(row_of('S38')['svms_req_no'] == 'TSTVES2607C81', '보관값 유지(교체 없음)',
    row_of('S38')['svms_req_no'])
chk(any('구매 INQ_NO 불일치' in m and 'TSTVES2607C89' in m for m in _cap.msgs),
    '불일치는 로그로 관측된다', _cap.msgs[-3:])

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
