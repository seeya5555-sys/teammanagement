#!/usr/bin/env python3
"""Dock 구매 — **품목별 견적 결함 사전 인폼**을 잠그는 테스트.

형 요청(2026-08-04): "별건 처리하자. 이런경우 종종있어. 그냥 trmt에 어떤 항목 견적 미제출 이렇게
인폼뜨게 개선". 계기 = S7(BGBBES2608A11) 상신 실패 — (주)딘텍이 SEQ 0040 을 단가 0 으로 제출해서
맥 워커의 pre-read 게이트가 막았는데, **화면은 조용해서 형이 버튼을 누른 뒤에야 알았다.**

이 파일이 잠그는 핵심:
  🔴 ① 게이트(`submit_watch._purchase_select_parts`)와 화면 스냅샷(`dock_sync._purchase_quotes`)이
       **같은 판정 함수**(`dock_items.item_gaps`)를 쓴다. 여기가 갈리면 "화면은 조용한데 상신만
       실패"(지금 고치는 그 버그) 또는 그 반대(화면만 경고)가 재발한다.
  🔴 ② `dock_items.py` 가 워커 지문 감시목록에 있다 — 빠지면 상주 워커가 옛 판정으로 계속 돈다
       (실사고 #15: KeepAlive 상주라 push 만으론 새 코드가 안 붙는다).
  🔴 ③ 서버 정규화(`_dockproc_norm_quotes`)가 `gap_n`/`gaps` 를 **버리지 않고** 형태만 방어한다
       (whitelist 층이라 키를 안 더하면 화면까지 도달하지 못한다).
  🔴 ④ 인폼은 **막지 않는다** — 상신 후보 라디오는 결함이 있어도 선택 가능해야 한다(스냅샷 시점
       기준이라, 형이 SVMS 에서 고친 직후 다음 동기화까지 상신이 잠기면 안 됨. 최종 차단은 워커).

실행: ~/.venvs/trmt-test/bin/python tests/test_dock_item_gaps.py
  ⚠️ `/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
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


# ── 워커 소스는 이 repo 밖(ws repo) ── 없는 머신에서는 크게 SKIP 하고 서버/화면 계약만 본다.
WS = os.path.expanduser('~/.openclaw/workspace/automation/dock-procure')
HAVE_WS = os.path.isdir(WS)
if HAVE_WS:
    sys.path.insert(0, os.path.expanduser('~/.openclaw/workspace/automation/svms-soa-opex'))
    sys.path.insert(0, WS)
    import dock_items
    import submit_watch
else:
    print('  ⚠️ SKIP — 워커 소스 경로 없음(%s). 판정함수 검사는 건너뜀.' % WS)


def row(seq, nm, *, odr='N', vndr='V1', status='U', qty=9, price=1200, **kw):
    """SVMS `P_RS_D` 한 줄 모양. 업체는 슬롯1에 넣고 슬롯2는 다른 업체로 채운다."""
    r = {'SEQ': seq, 'PART_NM': nm, 'TYPE_NM': None, 'MFG_PART_NO': None,
         'EQ_NM': 'MAIN ENGINE', 'REQ_QTY': qty, 'PUNIT_CD': 'PC', 'ODR_YN': odr,
         'VNDR_CD1': vndr, 'VNDR_STATUS1': status, 'ODR_QTY1': qty, 'ODR_PRICE1': price,
         'VNDR_CD2': 'OTHER', 'VNDR_STATUS2': 'U', 'ODR_QTY2': qty, 'ODR_PRICE2': 999}
    r.update(kw)
    return r


if HAVE_WS:
    print('# 1) 🔴 형 S7 실측 모양 — DM_YN=N 인데 P_RS_D 로 내려오고, SEQ 0040 단가가 0')
    #   실측: SAPS/BGBB 는 DM_YN='N' 이면서 P_RS_D_CS 를 비우고 P_RS_D 를 채운다. 규칙만 따르면
    #   빈 배열을 보고 "결함 0건" 이라 답해 인폼이 영원히 안 뜬다.
    S7 = [row('0010', 'O-RING'), row('0040', 'GASKET', price=0)]
    src = dock_items.item_source({'DM_YN': 'N'}, S7, [])
    chk(src is S7, 'DM_YN=N + P_RS_D_CS 빈 배열이면 P_RS_D 로 폴백(실측 케이스)')
    chk(dock_items.item_source({'DM_YN': 'Y'}, S7, [{'SEQ': 'X'}]) is S7, 'DM_YN=Y 는 P_RS_D')
    chk(dock_items.item_source({'DM_YN': 'N'}, S7, [{'SEQ': 'X'}])[0]['SEQ'] == 'X',
        'CS 에 행이 있으면 DM_YN=N 은 CS 를 쓴다(폴백은 빈 경우만)')

    g = dock_items.item_gaps(S7, 'V1')
    chk(len(g) == 1 and g[0]['seq'] == '0040' and g[0]['why'] == 'zero',
        '단가 0 품목 1건만 결함으로 잡는다', g)
    chk('0040' in dock_items.gap_label(g[0]) and 'GASKET' in dock_items.gap_label(g[0]),
        '라벨에 SEQ + 품목명이 들어간다(SEQ 만으론 어떤 자재인지 모른다)', dock_items.gap_label(g[0]))
    chk(dock_items.item_gaps(S7, 'OTHER') == [], '결함 없는 업체는 빈 리스트(= 전량 발주 가능)')

    print()
    print('# 2) 결함 사유 3종 + 이미 발주된 품목 제외')
    chk(dock_items.item_gaps([row('0010', 'A', vndr='ZZZ')], 'V1')[0]['why'] == 'no_quote',
        '업체 슬롯 자체가 없으면 no_quote(견적 미제출)')
    chk(dock_items.item_gaps([row('0010', 'A', status='O')], 'V1')[0]['why'] == 'not_submitted',
        '슬롯은 있는데 Submitted(U) 아니면 not_submitted')
    chk(dock_items.item_gaps([row('0010', 'A', qty=0)], 'V1')[0]['why'] == 'zero', '수량 0 도 zero')
    chk(dock_items.item_gaps([row('0010', 'A', price='')], 'V1')[0]['why'] == 'zero', '단가 빈값도 zero')
    chk(dock_items.item_gaps([row('0010', 'A', odr='Y', price=0)], 'V1') == [],
        '🔴 이미 발주된 품목(ODR_YN=Y)은 결함 대상이 아니다(옛 발주분이 영구 경고로 남으면 안 됨)')
    chk(dock_items.item_gaps([None, 'junk', row('0010', 'A')], 'V1') == [],
        '리스트에 섞인 쓰레기 원소는 무시하고 죽지 않는다')

    print()
    print('# 3) 🔴 게이트 == 화면 — 같은 입력에 같은 판정 (여기가 갈리면 이번 버그가 재발)')
    try:
        submit_watch._purchase_select_parts(S7, 'V1')
        chk(False, '게이트가 결함 품목에서 상신을 막는다', '예외 없이 통과함')
    except ValueError as e:
        msg = str(e)
        chk('0040' in msg and 'GASKET' in msg,
            '게이트 실패 메시지가 어떤 품목인지 말한다(옛 메시지는 SEQ 만 말했다)', msg)
        chk(msg == dock_items.gap_reason(dock_items.item_gaps(S7, 'V1')),
            '게이트 메시지 == gap_reason(같은 함수 결과) — 판정 이중구현 없음', msg)
    ok = submit_watch._purchase_select_parts(S7, 'OTHER')
    chk(len(ok) == 2 and all(r.get('VNDR_CHK2') == 'Y' for r in ok),
        '결함 없는 업체는 그대로 봉투가 조립된다(슬롯2 체크)')
    chk(all(r.get('ODR_PRICE') == 999 for r in ok), '선택 슬롯 값이 평면키로 복사된다')
    try:
        submit_watch._purchase_select_parts([row('0010', 'A', odr='Y')], 'V1')
        chk(False, '미발주 0건은 따로 막는다', '예외 없음')
    except ValueError as e:
        chk('미발주 품목 0건' in str(e), '미발주 0건 메시지는 그대로 유지(결함 판정에 먹히지 않음)', str(e))

    print()
    print('# 3-1) 이상 입력에도 게이트가 판정 메시지로 죽는다 (AttributeError 로 죽으면 이유가 안 보임)')
    #   올마이트 지적: `item_gaps` 는 쓰레기 원소를 무시하는데 게이트만 터지면 계약이 갈린다.
    junk = ['junk', None, row('0040', 'GASKET', price=0)]
    try:
        submit_watch._purchase_select_parts(junk, 'V1')
        chk(False, '쓰레기 원소가 섞여도 결함 메시지로 막는다', '예외 없음')
    except ValueError as e:
        chk('0040' in str(e), '쓰레기 원소가 섞여도 결함 메시지로 막는다(타입에러 아님)', str(e))
    except AttributeError as e:
        chk(False, '쓰레기 원소가 섞여도 결함 메시지로 막는다', f'AttributeError: {e}')
    chk(dock_items.item_source({'DM_YN': 'N'}, {'SEQ': '0010'}, []) == [],
        'list 아닌 품목값은 빈 것으로 본다(dict 를 돌며 조용히 "결함 0건" 답하는 경로 차단)')
    numeric = dict(row('0010', 'x', price=0), PART_NM=1234, PART_CD=None)
    chk(dock_items.item_gaps([numeric], 'V1')[0]['nm'] == '1234',
        '품목명이 숫자로 와도 죽지 않고 문자열로 읽는다')

    print()
    print('# 3-2) 🔴 화면 스냅샷 실경로 — dock_sync._purchase_quotes 를 실제로 호출해 게이트와 대조')
    import dock_sync
    dock_sync._order_header = lambda odr: None       # 발주헤더 조회(네트워크)만 차단 — 나머지는 실코드
    DETAIL = {'P_RS': [{'DM_YN': 'N'}], 'P_RS_D': S7, 'P_RS_D_CS': [],
              'P_RS_VNDR': [{'VNDR_CD': 'V1', 'VNDR_NM': 'DINTECH', 'STATUS_NM': 'Submitted',
                             'TAMT': '10,800', 'USD_TAMT': 10800, 'CUR_CD': 'USD', 'FILE_CNT': 1},
                            {'VNDR_CD': 'OTHER', 'VNDR_NM': 'OTHER CO', 'STATUS_NM': 'Submitted',
                             'TAMT': '9,000', 'USD_TAMT': 9000, 'CUR_CD': 'USD', 'FILE_CNT': 1},
                            {'VNDR_CD': 'ZZZ', 'VNDR_NM': 'NOT SUBMITTED CO', 'STATUS_NM': 'Opened'}]}
    qs = dock_sync._purchase_quotes(DETAIL)
    by = {q['cd']: q for q in qs}
    chk(len(qs) == 2, 'Submitted 업체만 후보로 나온다(Opened 제외)', [q['cd'] for q in qs])
    chk(by['V1']['gap_n'] == 1 and by['V1']['gaps'][0]['seq'] == '0040',
        '결함 업체에 gap_n/gaps 가 실린다', by.get('V1'))
    chk(by['OTHER']['gap_n'] == 0 and by['OTHER']['gaps'] == [],
        '정상 업체는 gap_n=0 (경고 안 뜸)', by.get('OTHER'))
    #   🔴 이 한 줄이 이번 개선의 핵심 계약 — 화면 스냅샷과 상신 게이트가 같은 수를 말해야 한다.
    for cd in ('V1', 'OTHER'):
        gate = dock_items.item_gaps(dock_items.item_source(DETAIL['P_RS'][0],
                                                          DETAIL['P_RS_D'], DETAIL['P_RS_D_CS']), cd)
        chk(by[cd]['gap_n'] == len(gate),
            f'[{cd}] 화면 스냅샷 gap_n == 게이트 판정 건수', f"snapshot={by[cd]['gap_n']} gate={len(gate)}")
    #   sync → 서버 정규화 → preview 까지 값이 살아서 도달하는지(중간 whitelist 층에서 죽는 게 실제 위험)
    e2e = json.loads(A._dockproc_norm_quotes(qs))
    chk(next(q for q in e2e if q['cd'] == 'V1')['gap_n'] == 1,
        'sync 산출물이 서버 정규화를 통과해도 gap_n 이 남는다')

    print()
    print('# 4) 🔴 판정 모듈이 워커 지문 감시목록에 있다 (실사고 #15 재발 방지)')
    chk(os.path.join(WS, 'dock_items.py') in submit_watch._src_paths(),
        'dock_items.py 가 _src_paths() 에 있다 — 없으면 상주 워커가 옛 판정으로 돈다')
    chk(len(submit_watch.src_fingerprint()) == 12, '지문은 12자 해시(계약 유지)')

    print()
    print('# 5) 요약 형태 — 화면이 바로 쓰는 최소 계약')
    many = [row(f'00{i}0', f'P{i}', price=0) for i in range(1, 6)]
    s = dock_items.gap_summary(dock_items.item_gaps(many, 'V1'))
    chk(s['gap_n'] == 5, 'gap_n 은 잘리지 않은 전체 건수', s['gap_n'])
    chk(len(s['gaps']) == 3, 'gaps 는 앞 3건만(카드가 길어지지 않게)', len(s['gaps']))
    chk(set(s['gaps'][0]) == {'seq', 'why', 'label'}, 'gaps 원소 키는 seq/why/label 고정', s['gaps'][0])
    chk(dock_items.gap_summary([]) == {'gap_n': 0, 'gaps': []}, '결함 0건은 gap_n=0')

print()
print('# 6) 🔴 서버 정규화가 gap_n/gaps 를 버리지 않는다 (whitelist 층)')
Q = [{'nm': 'DINTECH', 'cd': 'V1', 'amt': 1000, 'cur': 'USD', 'st': 'Submitted',
      'gap_n': 5, 'gaps': [{'seq': '0040', 'why': 'zero', 'label': '0040 GASKET — 단가 0'}]}]
norm = json.loads(A._dockproc_norm_quotes(Q))
chk(norm[0].get('gap_n') == 5, 'gap_n 통과', norm[0])
chk(norm[0].get('gaps') and norm[0]['gaps'][0]['seq'] == '0040', 'gaps 통과', norm[0].get('gaps'))
chk(json.loads(A._dockproc_norm_quotes(
    [{'nm': 'A', 'cd': 'V1', 'amt': 1, 'cur': 'USD', 'st': 'Submitted'}]))[0]['gap_n'] == 0,
    '구버전 폴러(키 없음)는 gap_n=0 — 경고 안 뜸(하위호환)')
big = json.loads(A._dockproc_norm_quotes([dict(Q[0], gap_n=99, gaps=[
    {'seq': str(i), 'why': 'zero', 'label': 'x' * 400} for i in range(20)])]))[0]
chk(len(big['gaps']) == 5, '표시 줄 수 상한 5(전체 건수는 gap_n 이 말한다)', len(big['gaps']))
chk(len(big['gaps'][0]['label']) == 200, '라벨 길이 캡 200')
chk(big['gap_n'] == 99, '캡은 표시 줄만 — 전체 건수는 보존')
odd = json.loads(A._dockproc_norm_quotes([dict(Q[0], gap_n='junk')]))[0]
chk(odd['gap_n'] == 1, '쓰레기 gap_n 은 0 으로 떨어지되 라벨 수(1)까지는 올라간다("외 −1건" 방지)', odd)
chk(json.loads(A._dockproc_norm_quotes([dict(Q[0], gaps='notalist')]))[0]['gaps'] == [],
    'gaps 가 리스트가 아니면 빈 배열')
zero = json.loads(A._dockproc_norm_quotes([dict(Q[0], gap_n=0, gaps=[])]))[0]
chk(zero['gap_n'] == 0 and zero['gaps'] == [], '결함 없음은 gap_n=0 + 빈 배열')

print()
print('# 7) 상신 초안 preview 가 결함을 내려주고, **선택은 막지 않는다**')
A.app.app_context().push()
c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'
A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")
A.execute(
    "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, svms_req_no, sub_quotes) "
    "VALUES('TEST VESSEL','TSTV','G1','S','[DOCK][TSTV G1]subject','BGBBES2608A11',?)",
    (json.dumps(norm),))
lid = A.query("SELECT id FROM dock_procure WHERE req_no='G1'", one=True)['id']
pv = c.get(f'/api/dock_submit/preview?rid={lid}').get_json()
cand = (pv.get('candidates') or [{}])[0]
chk(cand.get('gap_n') == 5, 'preview 후보에 gap_n 이 실린다', cand)
chk(cand.get('gaps') and cand['gaps'][0]['label'], 'preview 후보에 결함 라벨이 실린다', cand.get('gaps'))
chk(cand.get('ok') is True,
    '🔴 결함이 있어도 ok=True — 인폼만 하고 막지 않는다(스냅샷으로 상신을 잠그면 안 됨)', cand)

print()
print('# 8) 화면 계약 — 웹/iOS 가 같은 값을 읽고 스스로 판정하지 않는다')
tpl = open('templates/dock_procure.html', encoding='utf-8').read()
chk('const qGapN' in tpl and 'q.gap_n' in tpl, '웹은 서버가 준 gap_n 을 읽는다')
chk('견적 결함' in tpl, '웹에 결함 문구 존재')
chk('const gapRows' in tpl, '상신 모달 업체행에 결함 표시 있음')
#   🔴 스냅샷 성격을 문구에 밝힌다(SVMS 에서 방금 고친 직후엔 옛 사실일 수 있음).
chk('마지막 동기화 기준' in tpl, '웹 경고 문구가 스냅샷 기준임을 밝힌다')
#   🔴 표시업체(최저) 하나만 보면 다른 업체로 발주할 때 결함이 숨는다 — 결함 업체 전부를 근거로.
chk('const gapVs = qs.filter(q=>qGapN(q)>0)' in tpl and 'gapVs.map(qGapLine)' in tpl,
    '카드 배지가 결함 업체 전부를 근거로 뜬다(표시업체만 보지 않는다)')
#   🔴 인폼만 — gapRows 본문(정의부터 const opts 앞까지)에 비활성 코드가 없어야 한다.
gapbody = tpl.split('const gapRows')[1].split('const opts')[0]
chk('disabled' not in gapbody, '🔴 gapRows 가 라디오를 비활성하지 않는다(인폼만)', gapbody[:200])

ios = os.path.expanduser('~/.openclaw/workspace/trmt-mobile/ios/TRMT/Sources/')
if not os.path.isdir(ios):
    print('  ⚠️ SKIP — iOS 소스 경로 없음(%s). 이 머신에선 서버/웹 계약만 검사함.' % ios)
else:
    m = open(ios + 'Models/DockProcure.swift', encoding='utf-8').read()
    v = open(ios + 'Features/More/DockProcureView.swift', encoding='utf-8').read()
    chk('struct DockQuoteGap' in m, 'iOS 결함 모델 존재')
    chk('let gap_n: Int?' in m and 'let gaps: [DockQuoteGap]?' in m, 'iOS 가 gap_n/gaps 를 디코드한다')
    chk(m.count('DockQuoteGapCarrying') >= 3, '제출견적·상신후보가 같은 읽기 규칙을 공유한다')
    chk('var gapMoreCount: Int' in m, '잘린 건수를 "외 N건" 으로 말할 수 있다')
    #   🔴 업체 1곳이어도 펼쳐야 한다 — 형 S7 이 정확히 제출업체 1곳이었다.
    chk('private var canExpand: Bool { quotes.count > 1 || quotes.contains { $0.gapCount > 0 } }' in v,
        '업체가 1곳뿐이어도 결함이 있으면 상세를 펼칠 수 있다')
    chk('.disabled(!canExpand)' in v, '펼침 비활성 조건이 canExpand 로 교체됨')
    chk('private var gapVendors: [DockSubQuote] { quotes.filter { $0.gapCount > 0 } }' in v,
        '카드 배지가 결함 업체 전부를 근거로 뜬다(웹과 같은 규칙)')
    chk('"⚠품목 \\(best.gapCount)"' in v and '"⚠업체 \\(gapVendors.count)곳"' in v,
        '표시업체가 결함이면 품목 수, 아니면 업체 수로 말한다')
    chk('마지막 동기화 기준' in v, 'iOS 경고 문구도 스냅샷 기준임을 밝힌다')
    chk('이대로면 상신 실패함' in v, '상신 시트 업체행에 결함 경고')
    #   🔴 인폼만 — 후보 선택 비활성은 서버 ok 플래그만 본다(결함으로 막지 않는다).
    seg = v.split('candidate.gapCount > 0')[1][:1200]
    chk('disabled' not in seg, '결함 표시 블록이 선택을 막지 않는다')

print()
print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과'
      + ('' if HAVE_WS else ' (워커 판정함수 검사 SKIP)'))
sys.exit(1 if fails else 0)
