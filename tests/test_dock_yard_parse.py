#!/usr/bin/env python3
"""조선소 견적 규칙파서 — 양식별 섹션 판정·총액 교차검증·결정적 remark(LLM 미사용).

실사고(2026-10-02, SOUTH AFRICA PROSPERITY YiuLian 견적): 같은 조선소 새 양식은 섹션 번호가
1=General·2=Paint·3=Steel·4=Deck·5=Engine·6=Electric 인데, 옛 양식용 프로파일 번호표(2/3=General,
4=Paint …)를 그대로 써서 금액이 엉뚱한 카테고리로 들어갔고, remark 는 Gemini 호출 실패에 막혔다.

잠그는 것:
  ① 번호표가 맞는 옛 양식은 번호표 그대로(기존 검증 결과 불변).
  ② 대분류 제목이 번호표와 어긋나면 제목/소항목 다수결로 판정(새 양식).
  ③ Final discount·Normal Total·Net 을 견적서에서 읽고, 합계가 맞으면 경고 없음 / 어긋나면 경고.
  ④ remark 는 고액 소항목순 결정적 요약, General 은 Cover 수리기간만 채운다(상가일정 추정 금지).

실행: ~/.venvs/trmt-test/bin/python tests/test_dock_yard_parse.py
"""
import io, os, sys, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())
os.environ['TRMT_DB'] = tempfile.mktemp(suffix='.db')

import app as A  # noqa: F401,E402
import routes_dock_submit as M  # noqa: E402
from openpyxl import Workbook  # noqa: E402

PROFILE = {"yard_name": "Y", "discount_rate": 0.5,
           "section_map": {"2": "General", "3": "General", "4": "Paint", "5": "Deck", "6": "Steel"}}


def book(rows, cover_days=None, normal=None, disc=0.32, net=None):
    wb = Workbook()
    cv = wb.active; cv.title = 'Cover'
    if cover_days:
        cv.append(['1) Total repair period:', None, None, cover_days, 'good weatherdays;'])
    ws = wb.create_sheet('Quotation')
    hdr = [None] * 21
    hdr[0], hdr[2], hdr[16], hdr[19] = 'Item No.', 'Work Description', "Q'ty", 'Net   Total'
    ws.append(hdr)
    for itm, desc, amt in rows:
        r = [None] * 21
        r[0], r[2], r[19] = itm, desc, amt
        if amt:
            r[16] = 1
        ws.append(r)
    for label, val in (('Normal Total Price/USD', normal), ('Final discount', disc),
                       ('Total Price after dicount/USD Net', net)):
        if val is not None:
            r = [None] * 21; r[16] = label; r[19] = val; ws.append(r)
    bio = io.BytesIO(); wb.save(bio); bio.seek(0)
    return bio


fails = 0
def check(name, cond):
    global fails
    print(('PASS ' if cond else 'FAIL ') + name)
    fails += 0 if cond else 1


# ① 옛 양식: 대분류 제목이 번호표와 일치 → 번호표 사용, 소항목 Propeller 도 번호표(Deck) 그대로
old = [(2, 'General Service', 0), (2.1, 'Fire', 100), (3, '3. DOCKING', 0), (3.1, 'Dock', 50),
       (4, '4. HULL PAINTINGS', 0), (4.1, 'Hull', 200), (5.1, 'Seachests', 30), (6.1, 'Preamble Steel', 70)]
r = M._yard_parse_quote(book(old, normal=450, net=306), PROFILE)
check('① 옛 양식 번호표 유지', r['categories']['General'] == 150 and r['categories']['Paint'] == 200
      and r['categories']['Deck'] == 30 and r['categories']['Steel'] == 70)
check('③ 견적서 할인율 우선(0.32)', r['categories']['Discount'] == -144.0 and r['checks'] == [])

# ② 새 양식: 1=General,2=Paint,3(제목없음)=Steel,4=deck,5(제목없음)=Engine+Propeller→Deck,6=Electric
new = [(1, 'General Service', 0), (1.1, 'Fire isolation', 10),
       (2, ' HULL PAINTINGS', 0), (2.1, 'Hull Treatment', 20),
       (3.1, 'Preamble for Structural Steelwork', 0), (3.2, 'Steel Repair Works', 30),
       (4, 'deck', 0), (4.1, 'Anchors & Anchor chains', 40), (4.2, 'Deck Pipe', 45),
       (5.1, 'Propeller', 5), (5.2, 'Tail shaft & Stern tube', 6), (5.3, 'Aux. Boiler', 7),
       (5.4, 'M/E Air Cooler', 8), (5.5, 'E/R Pipe', 9),
       (6.1, 'General yard tariff for Electric motor', 0), (6.2, 'Electric Motor', 11)]
r = M._yard_parse_quote(book(new, cover_days=30, normal=191, net=129.88), PROFILE)
c = r['categories']
check('② 새 양식 제목 판정', (c['General'], c['Paint'], c['Steel'], c['Deck'], c['Engine'], c['Electric'])
      == (10, 20, 30, 90, 30, 11))
check('② 미매핑 없음·합계 일치', not r['unmapped'] and r['checks'] == [] and r['final_total'] == 129.88)
check('④ Deck remark 고액순', r['remarks']['Deck'] == 'Deck Pipe, Anchors & Anchor chains, Propeller')
check('④ General=Cover 수리기간, 상가일정 공란', r['remarks']['General'] == '입거 예상일정 : 30일, 상가일정 : ')

# ③ 합계 불일치 → 경고
r = M._yard_parse_quote(book(new, normal=999, net=1), PROFILE)
check('③ 합계 불일치 경고', any('Normal Total' in w for w in r['checks']))

# 프로파일 없이도 헤더 자동탐지 + 제목 규칙으로 파싱
r = M._yard_parse_quote(book(new, normal=191, net=129.88), {"yard_name": None})
check('프로파일 없음 기본규칙', r['categories']['Engine'] == 30 and r['checks'] == [])

# 올마이트 지적: 대분류 제목 없는 새 양식 + 옛 번호표 → 번호표 쓰지 않고 소항목 다수결 / 근거 없으면 unmapped
sub_only = [(5.1, 'Aux. Boiler', 7), (5.2, 'M/E Air Cooler', 8), (6.1, 'Electric Motor', 11), (7.1, 'Misc stuff', 4)]
r = M._yard_parse_quote(book(sub_only, normal=30, net=20.4), PROFILE)
check('대분류 제목 없음 → 다수결(번호표 미사용)', r['categories']['Engine'] == 15 and r['categories']['Deck'] == 0
      and r['categories']['Electric'] == 11)
check('판정 근거 없는 섹션 → unmapped 경고', r['unmapped'] == {'7': 4})
# 명시적 Final discount 0 은 프로파일 할인율(0.5)보다 우선
r = M._yard_parse_quote(book(old, normal=450, disc=0, net=450), PROFILE)
check('명시적 할인 0% 존중', r['categories']['Discount'] == 0 and r['final_total'] == 450)

print('FAILS', fails)
sys.exit(1 if fails else 0)
