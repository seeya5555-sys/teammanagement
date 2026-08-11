#!/usr/bin/env python3
"""Dock 발주현황 — 분할발주 업체별 발주서 스냅샷(ord_vendors) 계약 테스트.

배경(2026-08-05 형 요청): 자재(S)·스토어(ST)는 **한 청구를 업체 2곳으로 나눠 발주**할 수 있다.
실측 [BGBB S1] = 딘텍(KRW, 결재 진행중) + 에버런스코리아(USD 42,523.32, 발주완료) → SVMS `SP_GET_PC_PRO`
가 **발주서(ODR_NO)마다 한 행**을 준다. 그런데 서버는 같은 dock_procure 행에 붙는 여러 item 중
**최고 rank 1건만** 채택하고, `quote_amt`·`vendor` 는 칸이 하나뿐이다 → 그대로 두면 나머지 업체와
금액이 통째로 사라진다(형 화면에는 "발주완료 · EVERLLENCE · USD 42,523.32" 만 남고 딘텍 KRW 는 소실).

핵심 계약:
  · `orders` 키 미전송 → **기존값 유지** / `[]` → 발주 0건 확정(clear) / 내용 → canonical JSON 교체
  · 정규화 = ODR_NO 없으면 버림 · 중복 ODR_NO 제거 · amt 0/음수/비숫자 → None(미확정, 0 아님)
    · cur 는 3글자 대문자만 · `ordered` 는 `is True` 만(문자열 'true'/1 은 발주완료 아님)
  · **부분완료 게이트**(형 확인 기준 = 전부 발주돼야 완료): 업체 2곳 이상인데 전원 발주가 아니면
    rank 4 → 3 (= 벤더컨펌까지만, 발주완료 OFF). 이때 발주금액 자동입력도 일어나지 않는다.
  · 🔴 **이미 발주완료(stg_order=1)인 행은 내리지 않는다** — stale sync 로 완료 이력이 되돌아가면 안 됨
  · 업체 1곳(len==1)은 기존 동작 그대로(회귀 없음)
  · 같은 패킷 두 번이면 updated=0 (canonical JSON 이라 문자열 비교가 안정적)

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_ord_vendors.py
"""
import os, sys, json, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
from source_bundle import shared_ns  # noqa: E402

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

KEY = 'testkey-dockproc-ordvendors'
shared_ns._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}
A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def mkrow(req_no, ord_vendors=None, stg_order=0, quote_amt=None, quote_src='auto'):
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
        "stg_quote, stg_vendor, stg_confirm, stg_order, quote_amt, quote_src, ord_vendors) "
        "VALUES('TEST VESSEL','TSTV',?,'S',?,0,0,0,?,?,?,?)",
        (req_no, f'[DOCK][TSTV {req_no}]subject', stg_order, quote_amt, quote_src, ord_vendors))


def sync(req_no, status='Ordered', orders='OMIT', evidence=True, amt=None, cur=None, vendor=None):
    it = {'vsl_cd': 'TSTV', 'subject': f'[DOCK][TSTV {req_no}]subject', 'status': status,
          'amt': amt, 'cur': cur, 'ordered_evidence': evidence, 'vendor': vendor}
    if orders != 'OMIT':
        it['orders'] = orders
    r = c.post('/api/ext/dock_procure/sync', json={'items': [it]}, headers=HDR)
    assert r.status_code == 200, r.status_code
    return r.get_json()


def rowof(req_no):
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)


def ords(req_no):
    row = rowof(req_no)
    raw = row['ord_vendors']
    return (json.loads(raw) if raw else None), raw


# 실측값을 그대로 쓴다 — 통화 혼재(KRW+USD)와 '금액 미확정' 둘 다 이 한 건에 들어 있다.
S1 = [{'odr_no': 'BGBBES2607A11B', 'nm': 'EVERLLENCE KOREA', 'cd': '86378', 'st': 'Ordered',
       'amt': 42523.32, 'cur': 'USD', 'ordered': True},
      {'odr_no': 'BGBBES2607A11A', 'nm': '주식회사 딘텍', 'cd': '72801', 'st': 'Approval(Procssing)',
       'amt': None, 'cur': None, 'ordered': False}]

print('# 1) 정규화 함수 단위 — 형태 방어')
chk(shared_ns._dockproc_norm_orders(None) is None, '리스트 아니면 None')
chk(shared_ns._dockproc_norm_orders('x') is None, '문자열이면 None')
chk(shared_ns._dockproc_norm_orders([]) is None, '빈 리스트는 None(호출부가 clear 로 해석)')
chk(shared_ns._dockproc_norm_orders([1, 'a', None]) is None, 'dict 아닌 원소만이면 None')
chk(shared_ns._dockproc_norm_orders([{'nm': 'X', 'amt': 5}]) is None, 'ODR_NO 없으면 버림 → None')

v = json.loads(shared_ns._dockproc_norm_orders([{'odr_no': 'a1', 'nm': 'X'}, {'odr_no': 'A1', 'nm': 'Y'}]))
chk(len(v) == 1 and v[0]['odr_no'] == 'A1', 'ODR_NO 대문자화 + 중복 1건으로 축약', v)

v = json.loads(shared_ns._dockproc_norm_orders([
    {'odr_no': 'X1', 'amt': '1,234.5', 'cur': 'usd', 'cd': 'ab-12', 'ordered': 'true'},
    {'odr_no': 'X2', 'amt': 0, 'cur': 'DOLLAR', 'ordered': 1},
    {'odr_no': 'X3', 'amt': -5, 'ordered': True},
    {'odr_no': 'X4', 'amt': 'zzz'}]))
by = {o['odr_no']: o for o in v}
chk(by['X1']['amt'] == 1234.5 and by['X1']['cur'] == 'USD', '콤마 숫자 파싱 + 통화 대문자화', by['X1'])
chk(by['X1']['cd'] is None, "영숫자 아닌 벤더코드는 None('ab-12')", by['X1'])
chk(by['X1']['ordered'] is False and by['X2']['ordered'] is False,
    "🔴 'true'/1 은 발주완료 아님(is True 만) — 닫힘 쪽 실패", [by['X1'], by['X2']])
chk(by['X2']['amt'] is None and by['X3']['amt'] is None and by['X4']['amt'] is None,
    '0·음수·비숫자 금액은 None(미확정 — 0원 발주 표시 방지)', by)
chk(by['X2']['cur'] is None, '3글자 아닌 통화는 None', by['X2'])

big = shared_ns._dockproc_norm_orders([{'odr_no': f'N{i}', 'nm': 'x'} for i in range(30)])
chk(len(json.loads(big)) == shared_ns._DOCKPROC_ORDER_MAX, f'개수 캡 {shared_ns._DOCKPROC_ORDER_MAX}', len(json.loads(big)))

a = shared_ns._dockproc_norm_orders(S1)
b = shared_ns._dockproc_norm_orders(list(reversed(S1)))
chk(a == b, '🔴 순서가 뒤바뀐 같은 내용 → 같은 문자열(canonical = 멱등의 근거)')

print('# 2) 저장값 읽기(_dockproc_orders_of)')
chk(shared_ns._dockproc_orders_of(a) and len(shared_ns._dockproc_orders_of(a)) == 2, 'JSON → 2건')
chk(shared_ns._dockproc_orders_of('{bad') == [], '깨진 JSON 은 빈 목록(카드 전체를 죽이지 않음)')
chk(shared_ns._dockproc_orders_of(None) == [] and shared_ns._dockproc_orders_of('[1,2]') == [],
    'None·dict 아닌 원소는 빈 목록')

print('# 3) 3상태 계약')
mkrow('S60')
sync('S60', orders=S1)
got, raw = ords('S60')
chk(got and len(got) == 2, '내용 전송 → 2건 저장', got)
j = sync('S60', orders='OMIT')
_, raw2 = ords('S60')
chk(raw2 == raw and j['updated'] == 0, '키 미전송 → 기존값 유지(값이 사라지지 않음)', (j, raw2))
sync('S60', orders=[])
got, raw3 = ords('S60')
chk(raw3 is None, '[] → 발주 0건 확정(clear)', raw3)

mkrow('S61', ord_vendors=a)
sync('S61', orders=[{'nm': 'no odr'}])
_, raw = ords('S61')
chk(raw == a, '내용은 왔는데 전부 무효 → 계약위반으로 보고 기존값 유지', raw)

print('# 4) 멱등 — 같은 패킷 두 번')
mkrow('S62')
sync('S62', orders=S1, amt=42523.32, cur='USD')
j = sync('S62', orders=list(reversed(S1)), amt=42523.32, cur='USD')
chk(j['updated'] == 0, '두 번째 sync 는 updated=0(순서만 다른 SVMS 응답도 무변경)', j)

print('# 5) 🔴 부분완료 게이트 — 전부 발주돼야 발주완료')
mkrow('S63')
sync('S63', status='Ordered', orders=S1, evidence=True, amt=42523.32, cur='USD', vendor='EVERLLENCE KOREA')
r = rowof('S63')
chk(r['stg_confirm'] == 1 and r['stg_order'] == 0,
    '업체 2곳 중 1곳만 발주 → 벤더컨펌까지만(발주완료 OFF)',
    (r['stg_confirm'], r['stg_order']))
chk(r['quote_amt'] is None,
    '🔴 발주금액 자동입력도 안 함 — 한쪽 업체 금액만 대표값으로 박히면 그게 오보다', r['quote_amt'])
got, _ = ords('S63')
chk(got and {o['odr_no'] for o in got} == {'BGBBES2607A11A', 'BGBBES2607A11B'},
    '업체 2곳은 스냅샷에 **둘 다** 남는다(최고 rank 1건 채택으로도 사라지지 않음)', got)

print('# 6) 전원 발주 → 발주완료 ON')
mkrow('S64')
allord = [dict(o, ordered=True, amt=(o['amt'] or 100.0), cur=(o['cur'] or 'USD')) for o in S1]
sync('S64', status='Ordered', orders=allord, evidence=True, amt=142523.32, cur='USD')
r = rowof('S64')
# 금액은 폴러 합산값(142,523.32)이 아니라 **발주서 스냅샷 합계**(42,523.32 + 100 = 42,623.32) — #12 참조.
chk(r['stg_order'] == 1 and r['quote_amt'] == 42623.32, '전원 발주면 발주완료 + 스냅샷 합계 입력',
    (r['stg_order'], r['quote_amt']))

print('# 7) 🔴 이미 발주완료인 행은 내리지 않는다(stale sync 방어)')
mkrow('S65', stg_order=1, quote_amt=999.0)
sync('S65', status='Ordered', orders=S1, evidence=True, amt=42523.32, cur='USD')
r = rowof('S65')
chk(r['stg_order'] == 1, '완료 이력은 부분완료 판정으로도 되돌리지 않음', r['stg_order'])

print('# 8) 업체 1곳(len==1) 은 기존 동작 그대로 — 회귀 없음')
mkrow('S66')
one = [{'odr_no': 'ONE1', 'nm': 'SOLO', 'st': 'Ordered', 'amt': 500.0, 'cur': 'USD', 'ordered': False}]
sync('S66', status='Ordered', orders=one, evidence=True, amt=500.0, cur='USD')
r = rowof('S66')
chk(r['stg_order'] == 1 and r['quote_amt'] == 500.0,
    "업체 1곳은 `ordered=False` 여도 기존 evidence 규칙만 본다(게이트는 len>1 에만)",
    (r['stg_order'], r['quote_amt']))

print('# 9) 발주금액·제출견적 칸은 분할발주 스냅샷과 서로 안 섞임')
mkrow('S67')
sync('S67', status='Ordered', orders=allord, evidence=True, amt=142523.32, cur='USD')
r = rowof('S67')
chk(r['sub_quotes'] is None and r['ord_vendors'] is not None,
    "`orders` 는 `sub_quotes`(제출견적)를 건드리지 않는다", (r['sub_quotes'], r['ord_vendors']))

print('# 10) 취소행은 여기서도 무변경')
mkrow('S68', ord_vendors=a)
j = sync('S68', status='HQ Canceled', orders=[{'odr_no': 'ZZZ', 'nm': 'NO'}])
_, raw = ords('S68')
chk(j['canceled_skipped'] == 1 and raw == a, '취소는 ord_vendors 도 안 건드림', raw)

print('# 11) 🔴 orders 키 미전송이면 **저장된 스냅샷**으로 게이트 — 게이트가 열려버리면 안 됨')
mkrow('S69', ord_vendors=a)                                  # 저장 스냅샷 = 2곳 중 1곳만 발주
sync('S69', status='Ordered', orders='OMIT', evidence=True, amt=42523.32, cur='USD')
r = rowof('S69')
chk(r['stg_order'] == 0 and r['quote_amt'] is None,
    "폴러가 한 번 못 실어보내도 '발주완료' 로 켜지지 않는다(카드에 완료+1/2 모순 방지)",
    (r['stg_order'], r['quote_amt']))
mkrow('S70')                                                 # 스냅샷도 없으면 종전 evidence 규칙만
sync('S70', status='Ordered', orders='OMIT', evidence=True, amt=100.0, cur='USD')
r = rowof('S70')
chk(r['stg_order'] == 1, '스냅샷이 없으면 기존 동작 그대로(분할발주 아닌 건 회귀 없음)', r['stg_order'])

print('# 12) 🔴 분할발주 발주금액 재계산 — 폴러 합산값(통화 혼재 시 첫 건만)을 그대로 쓰지 않는다')
mkrow('S71')
mixed = [{'odr_no': 'M1', 'nm': 'A', 'amt': 42523.32, 'cur': 'USD', 'ordered': True},
         {'odr_no': 'M2', 'nm': 'B', 'amt': 14700100, 'cur': 'KRW', 'ordered': True}]
sync('S71', status='Ordered', orders=mixed, evidence=True, amt=42523.32, cur='USD')
r = rowof('S71')
chk(r['stg_order'] == 1 and r['quote_amt'] is None,
    '전원 발주 + 통화 혼재 → 발주완료는 켜지되 발주금액 칸은 **비운다**(한 업체 금액이 전체로 박히면 오보)',
    (r['stg_order'], r['quote_amt']))
mkrow('S72')
same = [{'odr_no': 'N1', 'nm': 'A', 'amt': 100.0, 'cur': 'USD', 'ordered': True},
        {'odr_no': 'N2', 'nm': 'B', 'amt': 250.0, 'cur': 'USD', 'ordered': True}]
sync('S72', status='Ordered', orders=same, evidence=True, amt=999.0, cur='JPY')
r = rowof('S72')
chk(r['quote_amt'] == 350.0 and r['quote_cur'] == 'USD',
    '단일통화 전원확정 → 발주서 스냅샷 합계(100+250)로 덮어쓴다(폴러 999/JPY 아님)',
    (r['quote_amt'], r['quote_cur']))
mkrow('S73')
half = [dict(same[0]), dict(same[1], amt=None, cur=None)]
sync('S73', status='Ordered', orders=half, evidence=True, amt=100.0, cur='USD')
r = rowof('S73')
chk(r['quote_amt'] is None, '한쪽 금액 미확정이면 합계를 만들지 않는다(확정분만 더하면 과소표시)',
    r['quote_amt'])

print('# 13) 🔴 게이트는 정규화 **후** 값으로 판정 — raw 로 세면 저장값과 판정이 갈린다')
mkrow('S74')
dup = [{'odr_no': 'D1', 'nm': 'A', 'amt': 100.0, 'cur': 'USD', 'ordered': True},
       {'odr_no': 'd1', 'nm': 'A dup', 'amt': 100.0, 'cur': 'USD', 'ordered': True},
       {'nm': 'no odr', 'ordered': False}]
sync('S74', status='Ordered', orders=dup, evidence=True, amt=100.0, cur='USD')
got, _ = ords('S74')
r = rowof('S74')
chk(len(got) == 1 and r['stg_order'] == 1 and r['quote_amt'] == 100.0,
    '중복·번호없음을 걸러 실제 1곳 → 분할발주 아님 → 발주완료 정상(raw 로 3건 세면 잠겼음)',
    (got, r['stg_order'], r['quote_amt']))

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
