#!/usr/bin/env python3
"""Dock 견적요청 대상 판정 `_dockproc_inq_target()` — 웹·iOS 버튼 게이트의 **단일 정본**.

왜 이 테스트가 필요한가 (실사고 2026-08-03):
  서버는 수리(MARP)/구매(PCRQ) 견적요청을 모두 지원하도록 배포됐는데, 웹 `iqBtn()` 과 iOS
  `canShowInquiry()` 가 각자 `cat_code=='R' && svms_req_no` 를 하드코딩하고 있어서 **구매 버튼이
  영구히 안 열렸다**(형 제보 "웹이나 앱에 관련 버튼이 없는데?"). 프론트를 서버 판정
  (`inq_doc`/`inq_key`)만 보게 바꿨으므로, 이제 이 함수가 틀리면 두 클라이언트가 동시에 틀린다.
  🔴 그런데 프론트 쪽은 JS·Swift 라 이 repo 에 테스트 타깃이 없다 ⇒ 방어선이 이 파일뿐이다.

🔴 가장 중요한 회귀 방지: **구매 키는 `svms_req_no` 가 아니라 `svms_pc_req_no`(REQ_NO)** 다.
   구매행의 `svms_req_no` 에는 견적요청 **후** 발급되는 INQ_NO 가 들어간다(라이브 실측:
   REQ_NO 'SAPSES2606A3' vs svms_req_no 'SAPSES2606A31'). 두 칸을 섞으면 견적요청이 엉뚱한
   번호로 나가고 Phase ③ 상신이 INQ_NO 를 잃는다.

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_inq_target.py
      (`/tmp/*venv` 는 깨져 있음 · 시스템 python3.9 는 hashlib.scrypt 없어 app import 단계에서 죽음)
"""
import os, sys, sqlite3, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
from source_bundle import read_app_sources, shared_ns

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
A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def row(cat, rep=None, pc=None):
    """실제 저장 경로를 거친 sqlite3.Row 로 판정한다 — dict 리터럴만 쓰면 컬럼 누락을 못 잡는다."""
    A.execute("DELETE FROM dock_procure WHERE vsl_nm='TEST VESSEL'")
    A.execute("INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
              "stg_quote, stg_vendor, stg_order, svms_req_no, svms_pc_req_no) "
              "VALUES('TEST VESSEL','TSTV','X1',?,'[DOCK][TSTV X1]s',0,0,0,?,?)", (cat, rep, pc))
    return A.query("SELECT * FROM dock_procure WHERE vsl_nm='TEST VESSEL'", one=True)


T = shared_ns._dockproc_inq_target

print('# 1) 수리(R) — 키는 svms_req_no(REP_CD)')
chk(T(row('R', rep='BGBBME26073116')) == ('MARP', 'BGBBME26073116'), 'R → MARP + REP_CD')
chk(T(row('R', rep=None, pc='SAPSES2606A3')) == ('MARP', ''),
    '수리는 구매칸을 절대 안 읽음(키 없음으로 닫힘)', str(T(row('R', rep=None, pc='SAPSES2606A3'))))

print()
print('# 2) 🔴 구매(S/ST) — 키는 svms_pc_req_no(REQ_NO). INQ_NO 칸을 쓰면 안 된다')
r = row('S', rep='SAPSES2606A31', pc='SAPSES2606A3')      # 라이브 실측 조합(둘 다 채워진 상태)
chk(T(r) == ('PCRQ', 'SAPSES2606A3'), 'S → PCRQ + REQ_NO', str(T(r)))
chk(T(r)[1] != 'SAPSES2606A31', '구매 키로 INQ_NO 가 새어나오지 않음', str(T(r)))
chk(T(row('ST', pc='SAPSEC2606B1')) == ('PCRQ', 'SAPSEC2606B1'), 'ST → PCRQ + REQ_NO')
chk(T(row('S', rep='SAPSES2606A31', pc=None)) == ('PCRQ', ''),
    '구매인데 REQ_NO 미채움 → 키 없음(버튼 닫힘) · INQ_NO 로 대체 안 함')

print()
print('# 3) 봉투 없는 종류는 종류 자체가 빈값 — 버튼·태그 둘 다 닫힌다')
for cat, why in (('P', '페인트'), ('SY', '기타'), ('', '빈값'), (None, 'NULL'), ('ZZ', '미지 코드')):
    chk(T(row(cat, rep='ANY1', pc='ANY2')) == ('', ''), f'{why}({cat!r}) → 종류·키 모두 빈값')

print()
print('# 4) 표기 흔들림 정규화 — 폴러·수동입력이 소문자/공백을 섞어도 같게 판정')
chk(T(row(' s ', pc='K1')) == ('PCRQ', 'K1'), "' s ' → PCRQ")
chk(T(row('r', rep='K2')) == ('MARP', 'K2'), "'r' → MARP")
chk(T(row('St', pc='K3')) == ('PCRQ', 'K3'), "'St' → PCRQ")

print()
print('# 5) 공백만 든 키는 "연결 안 됨"으로 본다 — 공백을 SVMS 로 보내면 안 된다')
chk(T(row('R', rep='   ')) == ('MARP', ''), '수리 공백키 → 빈값')
chk(T(row('S', pc='  ')) == ('PCRQ', ''), '구매 공백키 → 빈값')

print()
print('# 6) dict 행도 받는다(폴러 sync 경로가 dict 를 넘김)')
chk(T({'cat_code': 'S', 'svms_pc_req_no': 'D1', 'svms_req_no': 'D1_INQ'}) == ('PCRQ', 'D1'),
    'dict 구매행 → PCRQ + REQ_NO')
chk(T({'cat_code': 'R', 'svms_req_no': 'D2', 'svms_pc_req_no': None}) == ('MARP', 'D2'),
    'dict 수리행 → MARP + REP_CD')

print()
print('# 7) 목록 API 가 판정 결과를 실제로 실어보낸다(프론트가 이 두 키만 본다)')
src = read_app_sources()
chk("r['inq_doc'], r['inq_key'] = _dockproc_inq_target(r)" in src,
    'api_dockproc_lines 가 inq_doc/inq_key 를 채움')

print()
print('# 8) 게이트·미리보기·큐등록이 전부 같은 함수를 쓴다(경로별 자체 판정 금지)')
chk(src.count('_dockproc_inq_target(row)') >= 4,
    'blocked/vendor_search/preview/create 가 공용 판정 사용', str(src.count('_dockproc_inq_target(row)')))

print()
print('# 9) 단계 라벨 게이트 `_dockproc_inq_stage_block()` — 버튼 회색처리의 단일 정본')
# 형 제보(2026-08-03): 자재 S16 을 눌렀더니 "pre-read 검증 실패: 헤더 상태가 HQ Confirmed(C)
# 아님(STATUS=N / VSL Approved)". 버튼이 멀쩡히 파랬기 때문에 누를 수밖에 없었다.
# 🔴 정본은 워커 pre-read 다. 이 게이트는 그 거부를 **미리 화면에 비추는** 표시층이라,
#    확실히 아닌 라벨만 닫고 모르는 라벨은 연다(반대로 하면 가능한 건이 영구히 막힌다).
S = shared_ns._dockproc_inq_stage_block
# 🔴 2026-08-03 형 요청("컨펌 버튼 누르는 기능까지 포함")으로 **이 판정이 뒤집혔다.**
#    어제는 "구매 VSL Approved → 회색"이 정답이었고 그렇게 못박아 뒀지만, 이제 워커가 견적요청
#    직전에 SVMS Confirm(`SP_SET_REQ_INFO`+STATUS='C', 화면 `fnConfirm` verbatim)을 대신 누른다.
#    → 구매만 열고 수리는 그대로 닫는다(수리 쪽 대응 코드 미실측).
chk(S('PCRQ', 'VSL Approved') is None,
    '구매 VSL Approved → 열림(워커가 Confirm 을 대신 누름, PC_CONFIRM_STATUS=N)')
chk(S('PCRQ', ' vsl approved ') is None, '구매 VSL Approved 열림 판정도 대소문자·공백 흡수')
chk(S('PCRQ', 'HQ Confirmed') is None, '구매 HQ Confirmed → 열림(워커 PC_OPEN_STATUS=C)')
chk(S('MARP', 'HQ Received') is None, '수리 HQ Received → 열림(워커 OPEN_STATUS AP)')
chk(S('MARP', 'HQ Confirmed') is None, '수리 HQ Confirmed → 열림(워커 OPEN_STATUS RC)')
chk(S('MARP', 'VSL Approved') is not None, '수리 VSL Approved → 회색(AP/RC 아님)')
chk(S('MARP', 'HQ Ordered') is not None, '수리 HQ Ordered → 회색(발주 이후)')
chk(S('PCRQ', 'Ordered') is not None and S('PCRQ', 'Vendor confirmed') is not None,
    '구매 Ordered/Vendor confirmed → 회색(라이브 실측 라벨)')
chk(S('PCRQ', 'Quotation Inquiry') is not None, '이미 견적요청된 라벨 → 회색')
# 🔴 fail-open 이 의도임을 못박는다 — 이게 깨지면 라벨이 안 채워진 행의 버튼이 통째로 죽는다.
chk(S('PCRQ', None) is None and S('PCRQ', '') is None and S('PCRQ', '   ') is None,
    '빈 라벨 → 열어둠(폴러 미관측일 뿐 불가가 아님)')
chk(S('PCRQ', 'Something New') is None, '처음 보는 라벨 → 열어둠(추측으로 닫지 않음)')
chk(S('', 'VSL Approved') is None and S(None, 'HQ Ordered') is None,
    '문서종류 없음(페인트·기타) → 버튼 자체가 없으므로 사유도 없음')
chk(S('MARP', ' vsl approved ') is not None, '대소문자·공백 흔들림 흡수(수리는 여전히 차단)')
# 라이브 실측 분포(2026-08-03 키 有 행): 수리 HQ Ordered 36·Quotation Inquiry 8·HQ Received 1·
# HQ Confirmed 1·Approved 1 / 구매 Vendor confirmed 10·Quotation Inquiry 9·VSL Approved 12·
# Ordered 9·HQ Confirmed 2. 'Approved'(수리 1건)는 SVMS 코드 대응을 실측 못 해 일부러 열어둔다.
chk(S('MARP', 'Approved') is None, "수리 'Approved' 는 코드 미확인이라 열어둠(추측 차단 금지)")

print()
print('# 10) 게이트가 실제 경로에 물려 있다 — 목록 API·생성 게이트 양쪽')
chk("r['inq_block'] = _dockproc_inq_stage_block(r['inq_doc'], r.get('svms_status'))" in src,
    'api_dockproc_lines 가 inq_block 을 실어보냄(프론트 회색처리 입력)')
chk('_dockproc_inq_stage_block(doc, row[\'svms_status\'])' in src,
    '_dock_inq_blocked 도 같은 게이트를 통과(화면만 막고 API 는 뚫리는 일 없음)')
tpl = open('templates/dock_procure.html', encoding='utf-8').read()
chk('l.inq_block' in tpl and 'blk?\' disabled\':\'\'' in tpl,
    '웹 iqBtn 이 inq_block 으로 버튼을 비활성화')
chk('.dp-inq.blocked' in tpl, '회색 스타일 존재')
# 🔴 올마이트 2026-08-03 지적 — 실패 이력을 예외로 두면 정작 사고난 행(형이 누른 S16)이 계속 활성.
#    JS 는 테스트 타깃이 없으므로 조건식 자체를 못박는다(웹·iOS 가 같은 조건이어야 함).
chk("const blk = (!d && !(m && m.ok)) ? (l.inq_block || '') : '';" in tpl,
    '웹 차단 조건이 성공 이력만 예외로 둠(실패 이력은 계속 차단)')

print()
print('# 11) `_dock_inq_blocked` 사유 우선순위 — 단계 게이트가 기존 사유를 가리지 않는다')
r = row('S', pc='TSTPC001')
# 🔴 2026-08-03: 여기 원래 'VSL Approved' 를 썼는데 그 라벨은 이제 **구매에서 열린다**(워커가
#    Confirm 을 대신 누름). 게이트가 API 경로에 물려 있다는 것 자체는 여전히 검증해야 하므로
#    아직 확실히 닫힌 라벨('HQ Rejected')로 바꾼다.
A.execute("UPDATE dock_procure SET svms_status='HQ Rejected' WHERE id=?", (r['id'],))
r = A.query('SELECT * FROM dock_procure WHERE id=?', (r['id'],), one=True)
b = shared_ns._dock_inq_blocked(r)
chk(b is not None and 'HQ Rejected' in b, '단계 게이트가 API 경로에서도 실제로 막는다', str(b))
# 반대편 — 구매 VSL Approved 는 API 경로에서도 열려야 한다(Confirm 대행이 붙은 뒤의 정상 경로)
A.execute("UPDATE dock_procure SET svms_status='VSL Approved' WHERE id=?", (r['id'],))
rv = A.query('SELECT * FROM dock_procure WHERE id=?', (r['id'],), one=True)
chk(shared_ns._dock_inq_blocked(rv) is None,
    '구매 VSL Approved 는 API 경로에서도 열림(워커 Confirm 대행 전제)')
A.execute("UPDATE dock_procure SET svms_status='HQ Rejected' WHERE id=?", (r['id'],))
# 큐에 활성 초안이 있으면 그 사유가 먼저 나와야 한다(단계 문구로 덮이면 사용자가 큐를 못 찾는다)
A.execute("INSERT INTO dock_inquiry_draft(rid, rep_cd, doc_type, vndr_json, status) "
          "VALUES(?,?,'PCRQ','[]','approved')", (r['id'], 'TSTPC001'))
b2 = shared_ns._dock_inq_blocked(r)
chk(b2 is not None and '큐' in b2, '활성 큐 사유가 단계 사유보다 우선', str(b2))
A.execute("DELETE FROM dock_inquiry_draft WHERE rep_cd='TSTPC001'")
# 단계가 맞으면(HQ Confirmed) 통과 — 게이트가 구매를 통째로 막아버리는 회귀 방지
A.execute("UPDATE dock_procure SET svms_status='HQ Confirmed' WHERE id=?", (r['id'],))
r2 = A.query('SELECT * FROM dock_procure WHERE id=?', (r['id'],), one=True)
chk(shared_ns._dock_inq_blocked(r2) is None, '구매 HQ Confirmed 는 끝까지 통과(요청 가능 상태)')
# 라벨이 비어도 통과 — fail-open 이 API 경로에서도 유지되는지
A.execute("UPDATE dock_procure SET svms_status=NULL WHERE id=?", (r['id'],))
r3 = A.query('SELECT * FROM dock_procure WHERE id=?', (r['id'],), one=True)
chk(shared_ns._dock_inq_blocked(r3) is None, '라벨 미관측 행은 API 경로에서도 열려 있음')

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
