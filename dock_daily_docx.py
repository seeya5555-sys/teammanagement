"""Superintendent's Daily DD report(.docx) 읽기 — 순수 파서 + 일자 판정.

무엇을 하는가
-------------
조선소 감독이 매일 보내는 `Superintendets_Daily_DD_report_<날짜>.docx` 를 읽어
입거 Daily Report 카드로 넣을 **행 목록**을 만든다.  Flask·DB·번역·네트워크를
전혀 모르는 leaf 모듈이라 표 파싱과 날짜 판정만 단위테스트로 잠글 수 있다.

왜 문서의 날짜를 안 쓰는가 (형 지시 2026-08-23)
----------------------------------------------
라이브 문서의 `Reporting date-S/N:` 칸은 **비어 있다**(실측).  본문 날짜는 형식이
섞여 있고 오타도 실재한다 — `20/08.2026`(구분자 혼재), 초기미팅 줄에 전년도가
그대로 남은 `19/12/2025`.  그래서 "문서에서 날짜를 뽑아 보고서를 찾는" 방향은
**틀린 보고서를 덮는 사고**로 끝난다.  구조를 뒤집었다: 사람이 그 날짜의 빈
보고서를 먼저 만들고, 그 보고서가 기준일 `D` 를 준다.  파서는 문서에서 날짜를
**읽되 절대 결정하지 않는다.**

🔴 `Date finish` 는 완료 기록이 아니다 (실측)
--------------------------------------------
8/20 라이브 문서에서 날짜가 있는 행 **전부** `Date finish` 가 `Scheduled finish
date` 와 글자까지 같다 — `01.09/01.09`, `21.08/21.08`, `28.08/28.08`,
`25.08/25.08`.  즉 감독은 계획일을 두 칸에 같이 적고 있고, `finish` 를 완료로
읽으면 아직 안 끝난 작업에 **"금일 완료"** 를 찍어 형이 그대로 사내 메일로
보낸다.  그래서 두 칸이 **다를 때만** `finish` 를 실제 완료로 신뢰한다
(`actual`).  같으면 계획(`plan`)일 뿐이다.

이 템플릿이 이렇게 채워지는 동안 "금일 완료" 표시는 거의 안 나온다.  그게 맞다 —
문서에 완료 사실이 안 적혀 있으니 완료는 형이 손으로 쓰는 게 정직하다.

일자 판정 (이 템플릿은 행이 매일 누적되므로 이게 곧 중복 방지다)
--------------------------------------------------------------
`start` / `plan` / `actual` 로 정한다 (`D` = 보고서 일자).

  · `actual == D`                             → 포함 (당일 완료)
  · `start == D`                              → 포함 (당일 착수)
  · `start > D`                               → 제외 (미래)
  · `start < D`, `actual` 있고 `< D`           → 제외 (이미 끝남)
  · `start < D`, `actual` 있고 `> D`           → 포함 (완료 예정일이 아직 안 옴)
  · `start < D`, `actual` 없고 `plan >= D`     → 포함 (진행 중)
  · `start < D`, `actual` 없고 `plan < D`      → 제외 (계획일 지남, 기록 없음)
  · `start < D`, `actual`·`plan` 없음          → 제외 (단발 과거).
       단 본문에 진행 문구(in progress/ongoing/…)가 있으면 포함
  · 날짜를 하나도 못 읽음                       → `unknown`.  **버리지 않는다.**

🔴 `plan`·`actual` 빈칸을 곧바로 "진행 중"으로 보면 안 된다.  실측 8/20 문서의
19.08 행 4개(도착·입국수속·초기미팅·자재적재)가 전부 빈칸이고, 그걸 진행 중으로
읽으면 형이 지운 전날 내용이 매일 되살아난다.  계획완료일이 있는 행만 다일
작업으로 신뢰한다.

🔴 `unknown` 을 조용히 버리지 않는 이유: 실측에 `UTM Service` 처럼 3칸이 전부 빈
행이 있다.  버리면 형은 문서에 있던 작업이 사라진 걸 모르고 메일을 보낸다.
"""
import datetime as dt
import hashlib
import re

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

#: 표 **위의 제목 문단** → (섹션 key, 화면 라벨).  문서 템플릿의 고정 문자열이다.
#: 정규식으로 보는 이유는 감독이 대소문자·공백·`3rd`/`third` 를 흔들기 때문이다.
HEADING_RULES = (
    (re.compile(r'leading\s+deck\s+works.*yard', re.I), 'shipyard', 'Deck (Yard)'),
    (re.compile(r'leading\s+engine\s+works.*yard', re.I), 'shipyard', 'Engine (Yard)'),
    (re.compile(r'leading\s+works.*(3rd|third)\s*part', re.I), 'vendor', '3rd party'),
    (re.compile(r'leading\s+works.*crew', re.I), 'crew', 'Crew'),
)

#: 제목처럼 보이지만 규칙에 안 맞을 때 쓰는 신호.  이 단어들이 있으면 작업표 제목
#: 후보로 보고 `unmapped` 로 올린다(조용히 무시하지 않는다).
HEADING_HINT = re.compile(r'leading\s+works|works\s+done\s+by', re.I)

#: 진행 중임을 본문이 직접 말하는 경우.  날짜칸이 비어도 이건 신뢰한다.
PROGRESS_RE = re.compile(r'in\s*progress|ongoing|continu|under\s*way|underway|to\s+be\s+cont', re.I)

#: 구분자 혼재(`20/08.2026`)와 2자리 연도를 관용한다.  형식을 못 읽으면 `BAD`.
_DATE_RE = re.compile(r'(\d{1,2})\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{2,4})')

BAD_DATE = 'BAD'

COL_ALIASES = (
    ('desc', re.compile(r'descrip', re.I)),
    ('start', re.compile(r'date\s*start|start\s*date', re.I)),
    ('sched', re.compile(r'schedul', re.I)),
    ('finish', re.compile(r'date\s*finish|finish\s*date', re.I)),
)


def tolerant_date(text):
    """`None`(빈칸) · `BAD_DATE`(못 읽음) · `datetime.date` 중 하나.

    🔴 `BAD_DATE` 와 `None` 을 합치면 안 된다.  빈칸은 계약상 의미가 있고(진행 중
    판정에 쓰인다) 오타는 사람이 봐야 하는 것이다.
    """
    s = (text or '').strip()
    if not s:
        return None
    m = _DATE_RE.search(s)
    if not m:
        return BAD_DATE
    day, month, year = (int(x) for x in m.groups())
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return BAD_DATE


def row_key(scope, description):
    """같은 작업을 재업로드에서도 같은 것으로 알아보는 안정 키.

    행 번호를 쓰지 않는다 — 감독이 행을 지우거나 끼워 넣으면 번호가 전부 밀려서
    같은 작업이 새 카드로 또 들어온다.

    🔴 `scope` 는 섹션 key 가 아니라 **표 라벨**이다(올마이트 지적 2026-08-23).
    Deck 표와 Engine 표는 둘 다 `shipyard` 로 가므로, 섹션 key 로 묶으면 두 표에
    같은 문장이 있을 때 키가 겹쳐 뒤 행이 앞 행 카드를 덮고 **한 건이 조용히
    사라진다.**  라벨(`Deck (Yard)`/`Engine (Yard)`)은 규칙표에서 오는 고정값이라
    감독이 제목 문구를 흔들어도 키가 안 변한다.
    """
    norm = re.sub(r'\s+', ' ', (description or '')).strip().lower()
    raw = '%s|%s' % (re.sub(r'\s+', ' ', (scope or '')).strip().lower(), norm)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]


def _iter_blocks(doc):
    """문단과 표를 **문서 순서대로**.  제목→표 짝짓기가 순서에 달려 있다."""
    for child in doc.element.body.iterchildren():
        if child.tag == qn('w:p'):
            yield Paragraph(child, doc)
        elif child.tag == qn('w:tbl'):
            yield Table(child, doc)


def _heading_target(text):
    for pattern, key, label in HEADING_RULES:
        if pattern.search(text):
            return key, label
    return None, None


def _columns(header_cells):
    """헤더 텍스트로 컬럼 위치를 찾는다.

    위치를 못박지 않는 이유: 번호 컬럼이 있는 표와 없는 표가 같은 문서에 섞여
    있다(실측).  위치로 세면 3rd party 표에서 Description 을 번호칸으로 읽는다.
    """
    found = {}
    for idx, cell in enumerate(header_cells):
        for name, pattern in COL_ALIASES:
            if name not in found and pattern.search(cell):
                found[name] = idx
    return found if 'desc' in found else None


def parse(path):
    """`{'groups': [...], 'unmapped_headings': [...]}`.

    각 group = `{'heading', 'label', 'section_key', 'rows': [...]}`,
    각 row = `{'row_key', 'no', 'desc', 'start', 'sched', 'finish'}` (문자열 원본).
    판정은 `classify()` 가 따로 한다 — 파싱과 판정을 섞으면 기준일을 바꿔 가며
    테스트할 수 없다.
    """
    doc = Document(path)
    groups, unmapped = [], []
    pending = None  # (heading, label, section_key) — 직전에 본 제목
    for block in _iter_blocks(doc):
        if isinstance(block, Paragraph):
            text = re.sub(r'\s+', ' ', block.text).strip()
            if not text:
                continue
            key, label = _heading_target(text)
            if key:
                pending = (text, label, key)
            elif HEADING_HINT.search(text):
                pending = (text, text, None)
                unmapped.append(text)
            continue
        if not pending:
            continue
        rows = [[c.text.strip() for c in r.cells] for r in block.rows]
        if not rows:
            continue
        cols = _columns(rows[0])
        if not cols:
            # 제목 바로 뒤인데 Description 헤더가 없는 표는 작업표가 아니다
            # (날씨·비용 표). 제목을 소비해 다음 표로 새지 않게 한다.
            pending = None
            continue
        heading, label, section_key = pending
        pending = None
        out_rows = []
        for cells in rows[1:]:
            def cell(name):
                idx = cols.get(name)
                return cells[idx] if idx is not None and idx < len(cells) else ''
            desc = cell('desc')
            if not desc.strip():
                continue
            out_rows.append({
                'row_key': row_key(label or heading, desc),
                'desc': re.sub(r'\s+', ' ', desc).strip(),
                'start': cell('start'), 'sched': cell('sched'), 'finish': cell('finish'),
            })
        if out_rows:
            groups.append({'heading': heading, 'label': label,
                           'section_key': section_key, 'rows': out_rows})
    return {'groups': groups, 'unmapped_headings': unmapped}


def _dates(row):
    """`(start, plan, actual, bad)`.

    `actual` 은 `Date finish` 가 `Scheduled finish date` 와 **다를 때만** 채운다.
    같으면 계획을 두 칸에 복사한 것이라 완료 근거가 못 된다(모듈 docstring 실측).
    """
    start = tolerant_date(row.get('start'))
    sched = tolerant_date(row.get('sched'))
    finish = tolerant_date(row.get('finish'))
    bad = BAD_DATE in (start, sched, finish)
    if bad:
        return None, None, None, True
    actual = finish if (finish is not None and finish != sched) else None
    return start, sched, actual, False


def classify(row, report_date):
    """`('include'|'exclude'|'unknown', 사유코드)`."""
    start, plan, actual, bad = _dates(row)
    if bad:
        return 'unknown', 'date_unreadable'
    if actual == report_date:
        return 'include', 'finished_today'
    if start == report_date:
        return 'include', 'started_today'
    if start is None:
        return 'unknown', 'no_start_date'
    if start > report_date:
        return 'exclude', 'future'
    # start < report_date
    if actual is not None:
        # 🔴 완료일이 기준일보다 **뒤**면 그 작업은 이 날 아직 돌고 있다(올마이트 지적
        # 2026-08-23).  옛 코드는 `actual` 이 있다는 이유만으로 "이미 끝남" 으로 빼서,
        # 진행 중인 공사가 보고서에서 통째로 누락됐다.
        if actual > report_date:
            return 'include', 'finish_in_future'
        return 'exclude', 'finished_earlier'
    if plan is not None:
        if plan >= report_date:
            return 'include', 'in_progress_scheduled'
        return 'exclude', 'plan_date_passed'
    if PROGRESS_RE.search(row.get('desc') or ''):
        return 'include', 'in_progress_text'
    return 'exclude', 'past_one_off'


def marker(row, report_date):
    """카드 문장 끝에 붙일 상태 표시.  없으면 빈 문자열.

    🔴 계획일만 있는 행에 "완료" 라고 쓰지 않는다.  형이 이 문장을 그대로 사내
    메일로 보내기 때문에 문서에 없는 사실을 만들면 안 된다.
    """
    start, plan, actual, bad = _dates(row)
    if bad:
        return ''
    if actual == report_date:
        return '금일 완료'
    # 완료칸이 기준일보다 뒤면 그건 예정이다. `계획 완료`(Scheduled) 와 구분해서 쓴다 --
    # 두 칸의 날짜가 다를 때 어느 쪽을 적은 건지 형이 알아야 한다.
    if isinstance(actual, dt.date) and actual > report_date:
        return '진행 중, 완료 예정 %02d.%02d' % (actual.month, actual.day)
    plan_txt = '계획 완료 %02d.%02d' % (plan.month, plan.day) if isinstance(plan, dt.date) else ''
    if start == report_date:
        return plan_txt if plan != start else ''
    if isinstance(start, dt.date) and start < report_date:
        return '진행 중, %s' % plan_txt if plan_txt else '진행 중'
    return plan_txt


def scan(path, report_date):
    """파싱 + 판정을 합친 미리보기 페이로드.

    `report_date` 는 `datetime.date`.  결과의 모든 행은 `verdict`/`reason`/
    `marker` 를 갖고, 제외된 행도 **버리지 않고** 그대로 실려 온다 — 형이 화면에서
    "왜 안 들어왔는지" 를 보고 필요하면 직접 고를 수 있어야 한다.
    """
    parsed = parse(path)
    counts = {'include': 0, 'exclude': 0, 'unknown': 0}
    for group in parsed['groups']:
        for row in group['rows']:
            verdict, reason = classify(row, report_date)
            row['verdict'], row['reason'] = verdict, reason
            row['marker'] = marker(row, report_date) if verdict == 'include' else ''
            counts[verdict] += 1
    parsed['counts'] = counts
    parsed['report_date'] = report_date.isoformat()
    return parsed
