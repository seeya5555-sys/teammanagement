#!/usr/bin/env python3
"""Dock 발주현황 — 발주완료(rank3) fail-closed 게이트 테스트.

배경(2026-07-31 실측): SVMS 헤더 상태만 믿고 rank3 을 켜면 "발주완료인데 금액 —" 이 생긴다.
  · 수리 'Submit' = 벤더 견적제출(VNDR_STATS='Submitted', ODR_YN='N') → 발주 아님 → rank 2
  · 실발주 근거 = 수리 VNDR_STATS=='Ordered' 또는 ODR_YN=='Y' / 구매 발주서번호 ODR_NO 존재
게이트: ordered_evidence True/False/None(미확정).
  False → 강등 / None → **이미 발주완료인 행만 유지**(신규 승격 차단) / True → 그대로.

실행: /tmp/soavenv/bin/python tests/test_dockproc_ordered_evidence.py
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

KEY = 'testkey-dockproc-evidence'
A._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}

A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def mkrow(req_no, stg_order=0, quote_amt=None, quote_src='auto'):
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
        "stg_quote, stg_vendor, stg_order, quote_amt, quote_src) "
        "VALUES('TEST VESSEL','TSTV',?,?,?,?,?,?,?,?)",
        (req_no, 'R', f'[DOCK][TSTV {req_no}]subject', 0, 0, stg_order, quote_amt, quote_src))
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def sync(req_no, status, evidence='OMIT', amt=None, cur=None, vendor=None):
    it = {'vsl_cd': 'TSTV', 'subject': f'[DOCK][TSTV {req_no}]subject', 'status': status,
          'amt': amt, 'cur': cur, 'vendor': vendor}
    if evidence != 'OMIT':
        it['ordered_evidence'] = evidence
    r = c.post('/api/ext/dock_procure/sync', json={'items': [it]}, headers=HDR)
    assert r.status_code == 200, r.status_code
    return r.get_json()


def stages(req_no):
    row = A.query("SELECT stg_quote, stg_vendor, stg_order, quote_amt, quote_src FROM dock_procure "
                  "WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,), one=True)
    return (row['stg_quote'], row['stg_vendor'], row['stg_order']), row['quote_amt'], row['quote_src']

print('# 1) 상태맵 — Submit 은 벤더제출(2), 실발주만 3')
chk(A._dockproc_status_rank('Submit') == 2, "rank('Submit') == 2", A._dockproc_status_rank('Submit'))
chk(A._dockproc_status_rank('HQ Ordered') == 3, "rank('HQ Ordered') == 3")
chk(A._dockproc_status_rank('Ordered') == 3, "rank('Ordered') == 3")
chk(A._dockproc_status_rank('Vendor confirmed') == 3, "rank('Vendor confirmed') == 3")
chk(A._dockproc_status_rank('Quotation Inquiry') == 2, "rank('Quotation Inquiry') == 2")
chk(A._dockproc_status_rank('HQ Canceled') == 0, "rank('HQ Canceled') == 0(무시)")

print('# 2) Submit = 발주완료 안 켜지고 금액도 안 들어감 (형이 신고한 그 케이스)')
mkrow('R19')
sync('R19', 'Submit', evidence=False, amt=55396000, cur='KRW')
st, amt, src = stages('R19')
chk(st == (1, 1, 0), 'Submit → (1,1,0)', st)
chk(amt is None, 'Submit 이면 제출견적액을 발주금액에 넣지 않음', amt)

print('# 3) 실발주(근거 True) = 발주완료 + 금액 자동입력')
mkrow('R20')
sync('R20', 'HQ Ordered', evidence=True, amt=18225000, cur='KRW')
st, amt, src = stages('R20')
chk(st == (1, 1, 1), 'HQ Ordered+근거 → (1,1,1)', st)
chk(amt == 18225000 and src == 'auto', '금액 자동입력', (amt, src))

print('# 4) 근거 False = rank3 상태여도 강등')
mkrow('R21')
sync('R21', 'HQ Ordered', evidence=False, amt=999, cur='USD')
st, amt, src = stages('R21')
chk(st == (1, 1, 0), '근거 없으면 발주완료 안 켜짐', st)
chk(amt is None, '강등된 행에 금액 자동입력 안 함', amt)

print('# 5) 근거 None(조회실패) — 신규는 승격 차단, 이미 발주완료면 유지')
mkrow('R22', stg_order=0)
sync('R22', 'HQ Ordered', evidence=None, amt=500, cur='USD')
st, amt, _ = stages('R22')
chk(st == (1, 1, 0), 'None + 기존 rank<3 → 승격 차단', st)
chk(amt is None, '근거 미확정이면 금액도 안 들어감', amt)

mkrow('R23', stg_order=1, quote_amt=7000)
sync('R23', 'HQ Ordered', evidence=None, amt=7000, cur='USD')
st, amt, _ = stages('R23')
chk(st == (1, 1, 1), 'None + 이미 발주완료 → 유지', st)
chk(amt == 7000, '기존 금액 보존', amt)

print('# 6) ordered_evidence 키 자체가 없을 때(구버전 폴러) = None 과 동일 취급')
mkrow('R24', stg_order=0)
sync('R24', 'HQ Ordered')                                # 키 미전송
st, _, _ = stages('R24')
chk(st == (1, 1, 0), '키 미전송 + 신규 → 승격 차단', st)
mkrow('R25', stg_order=1)
sync('R25', 'HQ Ordered')
st, _, _ = stages('R25')
chk(st == (1, 1, 1), '키 미전송 + 이미 발주완료 → 유지', st)

print('# 7) 강등돼도 기존 금액은 지우지 않음 (auto/manual 둘 다)')
mkrow('R27', stg_order=1, quote_amt=12345.0, quote_src='auto')
sync('R27', 'Submit', evidence=False)
st, amt, src = stages('R27')
chk(st == (1, 1, 0) and amt == 12345.0, '강등 + auto 금액 보존', (st, amt))
mkrow('R28', stg_order=1, quote_amt=999.0, quote_src='manual')
sync('R28', 'HQ Ordered', evidence=False, amt=111.0, cur='USD')
st, amt, src = stages('R28')
chk(amt == 999.0 and src == 'manual', 'manual 금액은 어떤 경우에도 덮어쓰지 않음', (amt, src))

print('# 8) manual 우선 — 근거 True 인 정상 발주에서도 manual 은 보존')
mkrow('R29', stg_order=0, quote_amt=888.0, quote_src='manual')
sync('R29', 'HQ Ordered', evidence=True, amt=222.0, cur='USD')
st, amt, src = stages('R29')
chk(st == (1, 1, 1) and amt == 888.0 and src == 'manual', 'manual 보존 + 단계는 전진', (st, amt, src))

print('# 9) 근거 True 인데 금액 없음 = 단계만 켜짐(허용, 금액 0 저장 금지)')
mkrow('R30')
sync('R30', 'HQ Ordered', evidence=True, amt=None)
st, amt, _ = stages('R30')
chk(st == (1, 1, 1) and amt is None, '금액 없으면 단계만', (st, amt))

print('# 10) 취소는 게이트 이전에 무시 — evidence 와 무관하게 행을 아예 안 건드림')
mkrow('R31', stg_order=1, quote_amt=1000.0)
before = stages('R31')                       # 단계 재계산조차 일어나면 안 되므로 스냅샷 비교
j = sync('R31', 'HQ Canceled', evidence=True, amt=5.0)
after = stages('R31')
chk(j['canceled_skipped'] == 1 and j['updated'] == 0 and after == before,
    'HQ Canceled 는 행 무변경', (j['canceled_skipped'], j['updated'], before, after))

print('# 11) 구매 통화 이상값 방어 + 정상 통화')
mkrow('S1')
sync('S1', 'Ordered', evidence=True, amt=100.0, cur='kr')
row = A.query("SELECT quote_cur FROM dock_procure WHERE req_no='S1' AND vsl_nm='TEST VESSEL'", one=True)
chk(row['quote_cur'] == 'USD', "3글자 아닌 통화는 USD 로 방어", row['quote_cur'])
mkrow('S2')
sync('S2', 'Vendor confirmed', evidence=True, amt=100.0, cur='jpy')
row = A.query("SELECT quote_cur FROM dock_procure WHERE req_no='S2' AND vsl_nm='TEST VESSEL'", one=True)
chk(row['quote_cur'] == 'JPY', '정상 통화는 대문자로 저장', row['quote_cur'])

print('# 12) 멱등 — 같은 패킷 두 번이면 두 번째는 변경 0')
mkrow('R32')
sync('R32', 'HQ Ordered', evidence=True, amt=100.0, cur='USD')
j2 = sync('R32', 'HQ Ordered', evidence=True, amt=100.0, cur='USD')
chk(j2['updated'] == 0, '두 번째 동기화는 updated=0', j2['updated'])

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
