#!/usr/bin/env python3
"""Dock 발주현황 — 벤더 '제출견적' 스냅샷(sub_quotes) 계약 테스트.

배경(2026-07-31 형 요청): 벤더가 견적을 제출하면 그 금액을 웹/앱에서 바로 보고 싶다.
제출견적 ≠ 발주금액 — SVMS P_RS_D_VNDR 는 VNDR_QUO_AMT(제출)와 VNDR_ODR_AMT(발주)를 따로 들고 있고,
발주금액 자동입력은 발주완료(rank4 + 근거)에서만 일어난다. 그래서 저장 칸을 분리했다(sub_quotes).

핵심 계약:
  · `quotes` 키 미전송(상세조회 실패 등) → **기존값 유지**(화면에서 값이 사라지지 않음)
  · `quotes: []` → 제출 0건으로 갱신(clear)
  · 정규화 = 개수 캡 20 · 통화 3글자 아니면 None · 숫자 아니면 amt None · att 0~99
  · 제출견적이 quote_amt(발주금액)을 절대 건드리지 않음
  · 같은 패킷 두 번이면 updated=0 (canonical JSON 이라 문자열 비교가 안정적)

실행: /tmp/trmt-test-venv/bin/python tests/test_dockproc_sub_quotes.py
      (시스템 python3.9 는 hashlib.scrypt 없어서 app import 단계에서 죽음)
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

KEY = 'testkey-dockproc-subquotes'
A._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}
A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def mkrow(req_no, sub_quotes=None, quote_amt=None, quote_src='auto'):
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
        "stg_quote, stg_vendor, stg_order, quote_amt, quote_src, sub_quotes) "
        "VALUES('TEST VESSEL','TSTV',?,?,?,0,0,0,?,?,?)",
        (req_no, 'R', f'[DOCK][TSTV {req_no}]subject', quote_amt, quote_src, sub_quotes))


def sync(req_no, status='Submit', quotes='OMIT', evidence=False, amt=None, cur=None):
    it = {'vsl_cd': 'TSTV', 'subject': f'[DOCK][TSTV {req_no}]subject', 'status': status,
          'amt': amt, 'cur': cur, 'ordered_evidence': evidence}
    if quotes != 'OMIT':
        it['quotes'] = quotes
    r = c.post('/api/ext/dock_procure/sync', json={'items': [it]}, headers=HDR)
    assert r.status_code == 200, r.status_code
    return r.get_json()


def subq(req_no):
    row = A.query("SELECT sub_quotes, quote_amt FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                  (req_no,), one=True)
    raw = row['sub_quotes']
    return (json.loads(raw) if raw else None), row['quote_amt'], raw


print('# 1) 정규화 함수 단위 — 형태 방어')
chk(A._dockproc_norm_quotes(None) is None, '리스트 아니면 None')
chk(A._dockproc_norm_quotes('x') is None, '문자열이면 None')
chk(A._dockproc_norm_quotes([]) is None, '빈 리스트면 None')
chk(A._dockproc_norm_quotes(['x', 3, None]) is None, 'dict 아닌 원소만 있으면 None')
one = json.loads(A._dockproc_norm_quotes(
    [{'nm': 'A CO', 'cd': 'a1jfj', 'amt': '15,800', 'usd': 15800, 'cur': 'usd',
      'gross_amt': '16,000', 'dc_rate': 45, 'final_amt': '8,800', 'final_usd': 8800,
      'att': '2', 'st': 'Submitted'}]))
chk(one == [{'nm': 'A CO', 'cd': 'A1JFJ', 'amt': 15800.0, 'usd': 15800.0,
             'gross_amt': 16000.0, 'dc_rate': 45.0, 'final_amt': 8800.0, 'final_usd': 8800.0,
             'cur': 'USD', 'att': 2,
             'st': 'Submitted', 'best': 1}],
    'DC전/할인/DC후 금액·통화·첨부·업체코드 정규화(+단독이면 best)', one)
bad = json.loads(A._dockproc_norm_quotes([{'nm': 'B', 'amt': 'abc', 'cur': 'kr', 'att': 'x', 'st': ''}]))
chk(bad == [{'nm': 'B', 'cd': None, 'amt': None, 'usd': None,
             'gross_amt': None, 'dc_rate': None, 'final_amt': None, 'final_usd': None,
             'cur': None, 'att': 0, 'st': ''}],
    '파싱불가 → amt/usd None·cur None·att 0·cd None', bad)

print('# 1b) cd = SVMS VNDR_CD (Phase 3 SELETED_VDR 의 정본 식별자)')
def _cd(v):
    return json.loads(A._dockproc_norm_quotes([{'nm': 'X', 'cd': v}]))[0]['cd']
chk(_cd(None) is None, 'cd 미전송(구버전 폴러) → None (상신 대상에서 제외됨)')
chk(_cd('') is None, 'cd 빈문자 → None')
chk(_cd('  a1jfj ') == 'A1JFJ', '공백 제거 + 대문자화', _cd('  a1jfj '))
chk(_cd('A1-JF') is None, "형식 위반(하이픈) → None — 봉투에 쓰레기 코드가 안 들어감", _cd('A1-JF'))
chk(_cd('한글업체') is None, '비ASCII → None', _cd('한글업체'))
chk(_cd('A' * 30) == 'A' * 20, '길이 캡 20 (잘린 값이 형식검증을 통과)', _cd('A' * 30))
inf = json.loads(A._dockproc_norm_quotes([{'nm': 'C', 'amt': float('inf'), 'usd': float('nan')}]))
chk(inf[0]['amt'] is None and inf[0]['usd'] is None, 'inf/nan 은 None(JSON 직렬화 불가 방지)', inf)
mix = json.loads(A._dockproc_norm_quotes(
    [{'nm': 'KR', 'amt': 35421000, 'usd': 24794, 'cur': 'KRW'},
     {'nm': 'US', 'amt': 23480, 'usd': 23480, 'cur': 'USD'}]))
chk(min(mix, key=lambda x: x['usd'])['nm'] == 'US',
    '통화 혼재 시 최저가 판단 근거(usd)가 보존됨 — 원화 숫자로 비교하면 오답', mix)
cap = json.loads(A._dockproc_norm_quotes([{'nm': f'V{i}', 'amt': i} for i in range(40)]))
chk(len(cap) == A._DOCKPROC_QUOTE_MAX, f'개수 캡 {A._DOCKPROC_QUOTE_MAX}', len(cap))
clamp = json.loads(A._dockproc_norm_quotes([{'nm': 'D', 'att': 500}, {'nm': 'E', 'att': -3}]))
chk([x['att'] for x in clamp] == [99, 0], 'att 0~99 clamp', clamp)
chk(A._dockproc_norm_quotes([{'nm': 'x' * 300}])[:20].startswith('[{"amt"'), 'canonical=키 정렬(sort_keys)')
chk(len(json.loads(A._dockproc_norm_quotes([{'nm': 'x' * 300}]))[0]['nm']) == 120, '업체명 120자 절단')

print("# 1b) '최저' 판정(best 플래그) — 통화 혼재 오답 차단 (JS 대신 여기서 판정)")
def _best(lst):
    return [x['nm'] for x in json.loads(A._dockproc_norm_quotes(lst)) if x.get('best')]
chk(_best([{'nm': 'KR', 'amt': 35421000, 'usd': 24794, 'cur': 'KRW'},
           {'nm': 'US', 'amt': 23480, 'usd': 23480, 'cur': 'USD'}]) == ['US'],
    '전원 usd 보유 → usd 기준 최저(원화 숫자 크기로 비교하면 KR 오답)')
chk(_best([{'nm': 'A', 'amt': 300, 'cur': 'USD'}, {'nm': 'B', 'amt': 100, 'cur': 'USD'}]) == ['B'],
    'usd 없고 통화 단일 → 원표시금액 최저')
chk(_best([{'nm': 'A', 'amt': 300, 'cur': 'KRW'}, {'nm': 'B', 'amt': 100, 'cur': 'USD'}]) == [],
    '🔴 통화 혼재 + usd 없음 → best 없음(비교 포기 = 화면에 최저 안 씀)')
chk(_best([{'nm': 'A', 'amt': 300, 'usd': 300, 'cur': 'USD'},
           {'nm': 'B', 'amt': 100, 'cur': 'KRW'}]) == [],
    '일부만 usd 보유 → 부분비교 금지(usd 없는 건이 조용히 빠지면 오답)')
chk(_best([{'nm': 'A', 'cur': 'USD'}, {'nm': 'B', 'st': 'Submitted'}]) == [],
    '금액 미제출만 있으면 best 없음')

print('# 2) 제출견적 저장 — Submit 단계에서 금액이 화면에 뜬다')
mkrow('R40')
sync('R40', 'Submit', quotes=[{'nm': 'HYUNDAI', 'amt': 15800, 'cur': 'USD', 'att': 1, 'st': 'Submitted'}])
q, qamt, _ = subq('R40')
chk(q and q[0]['amt'] == 15800.0 and q[0]['att'] == 1, '제출견적 저장됨', q)
chk(qamt is None, '🔴 제출견적은 발주금액(quote_amt)을 건드리지 않음', qamt)

print('# 3) 키 미전송(상세조회 실패) = 기존값 유지')
prev = A._dockproc_norm_quotes([{'nm': 'KEEP', 'amt': 1, 'cur': 'USD'}])
mkrow('R41', sub_quotes=prev)
sync('R41', 'Submit')                                    # quotes 키 없음
q, _, raw = subq('R41')
chk(raw == prev, '미전송이면 기존 스냅샷 그대로', raw)

print('# 4) 빈 리스트 = 제출 0건으로 갱신(clear)')
mkrow('R42', sub_quotes=prev)
sync('R42', 'Submit', quotes=[])
q, _, raw = subq('R42')
chk(raw is None, '빈 리스트면 clear', raw)

print('# 5) 리스트가 아닌 값은 미전송과 동일 취급(오염된 패킷이 값을 지우지 않게)')
mkrow('R43', sub_quotes=prev)
sync('R43', 'Submit', quotes={'nm': 'X'})
_, _, raw = subq('R43')
chk(raw == prev, 'dict 이면 기존 유지', raw)
mkrow('R44', sub_quotes=prev)
sync('R44', 'Submit', quotes='oops')
_, _, raw = subq('R44')
chk(raw == prev, '문자열이면 기존 유지', raw)

print('# 6) 미제출 업체는 제외됨(폴러가 Submitted/Ordered 만 담지만 서버도 값 그대로 받음)')
mkrow('R45')
sync('R45', 'Submit', quotes=[{'nm': 'A', 'amt': 100, 'cur': 'USD', 'st': 'Submitted'},
                              {'nm': 'B', 'amt': 90, 'cur': 'USD', 'st': 'Ordered'}])
q, _, _ = subq('R45')
chk(len(q) == 2 and {x['st'] for x in q} == {'Submitted', 'Ordered'}, '복수 업체 그대로 보존', q)

print('# 7) 멱등 — 같은 패킷 두 번이면 두 번째는 updated=0')
mkrow('R46')
pkt = [{'nm': 'ZZ', 'amt': 5, 'cur': 'USD', 'att': 1, 'st': 'Submitted'}]
sync('R46', 'Submit', quotes=pkt)
j2 = sync('R46', 'Submit', quotes=pkt)
chk(j2['updated'] == 0, '두 번째 동기화 updated=0', j2['updated'])
j3 = sync('R46', 'Submit', quotes=[{'st': 'Submitted', 'cur': 'USD', 'amt': 5, 'att': 1, 'nm': 'ZZ'}])
chk(j3['updated'] == 0, '키 순서 달라도 canonical 이라 변경 아님', j3['updated'])

print('# 7b) 벤더 배열 순서가 바뀌어도 같은 견적집합 → 변경 아님 (올마이트 지적)')
mkrow('R49')
a = {'nm': 'AAA', 'amt': 10, 'cur': 'USD', 'att': 1, 'st': 'Submitted'}
b = {'nm': 'BBB', 'amt': 20, 'cur': 'USD', 'att': 0, 'st': 'Submitted'}
sync('R49', 'Submit', quotes=[a, b])
j = sync('R49', 'Submit', quotes=[b, a])                 # SVMS 가 순서를 바꿔 줘도
chk(j['updated'] == 0, '순서만 다르면 updated=0 (매 폴링 UPDATE 방지)', j['updated'])

print('# 7c) 내용은 있는데 전부 쓰레기 = 계약 위반 패킷 → 기존값 유지(clear 아님)')
mkrow('R50', sub_quotes=prev)
sync('R50', 'Submit', quotes=['x', 3, None])
_, _, raw = subq('R50')
chk(raw == prev, '정규화 결과가 비면 미전송과 동일 취급', raw)
mkrow('R51', sub_quotes=prev)
sync('R51', 'Submit', quotes=[{'nm': 'INF', 'att': float('inf')}])
_, _, raw = subq('R51')
chk(raw is not None and json.loads(raw)[0]['att'] == 0, 'att=inf 도 500 없이 처리', raw)

print('# 7d) 제출견적은 수동입력 발주금액·단계 플래그를 침범하지 않음')
mkrow('R52', quote_amt=777, quote_src='manual')
sync('R52', 'Submit', quotes=[{'nm': 'Q', 'amt': 5, 'cur': 'USD', 'st': 'Submitted'}])
row = A.query("SELECT quote_amt, quote_src, stg_quote, stg_vendor, stg_confirm, stg_order, sub_quotes "
              "FROM dock_procure WHERE req_no='R52' AND vsl_nm='TEST VESSEL'", one=True)
chk(row['quote_amt'] == 777.0 and row['quote_src'] == 'manual', '수동 발주금액 보존', dict(row))
chk((row['stg_quote'], row['stg_vendor'], row['stg_confirm'], row['stg_order']) == (1, 1, 1, 0),
    "Submit=벤더컨펌(rank3) — 발주완료 안 켜짐", dict(row))
chk(row['sub_quotes'] is not None, '제출견적은 정상 저장')

print('# 8) 발주완료 + 근거 True 에서는 발주금액과 제출견적이 공존')
mkrow('R47')
sync('R47', 'HQ Ordered', quotes=[{'nm': 'W', 'amt': 200, 'cur': 'USD', 'st': 'Ordered'}],
     evidence=True, amt=190, cur='USD')
q, qamt, _ = subq('R47')
chk(qamt == 190.0 and q[0]['amt'] == 200.0, '발주금액 190 / 제출견적 200 각각 저장', (qamt, q))

print('# 9) 취소행은 여기서도 무변경')
mkrow('R48', sub_quotes=prev)
j = sync('R48', 'HQ Canceled', quotes=[{'nm': 'NO', 'amt': 1, 'cur': 'USD'}])
_, _, raw = subq('R48')
chk(j['canceled_skipped'] == 1 and raw == prev, '취소는 sub_quotes 도 안 건드림', raw)

print()
if fails:
    print(f'❌ FAIL {len(fails)}건: {fails}')
    sys.exit(1)
print('✅ 전부 통과')
