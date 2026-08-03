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
A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def row(cat, rep=None, pc=None):
    """실제 저장 경로를 거친 sqlite3.Row 로 판정한다 — dict 리터럴만 쓰면 컬럼 누락을 못 잡는다."""
    A.execute("DELETE FROM dock_procure WHERE vsl_nm='TEST VESSEL'")
    A.execute("INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
              "stg_quote, stg_vendor, stg_order, svms_req_no, svms_pc_req_no) "
              "VALUES('TEST VESSEL','TSTV','X1',?,'[DOCK][TSTV X1]s',0,0,0,?,?)", (cat, rep, pc))
    return A.query("SELECT * FROM dock_procure WHERE vsl_nm='TEST VESSEL'", one=True)


T = A._dockproc_inq_target

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
src = open('app.py', encoding='utf-8').read()
chk("r['inq_doc'], r['inq_key'] = _dockproc_inq_target(r)" in src,
    'api_dockproc_lines 가 inq_doc/inq_key 를 채움')

print()
print('# 8) 게이트·미리보기·큐등록이 전부 같은 함수를 쓴다(경로별 자체 판정 금지)')
chk(src.count('_dockproc_inq_target(row)') >= 4,
    'blocked/vendor_search/preview/create 가 공용 판정 사용', str(src.count('_dockproc_inq_target(row)')))

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
