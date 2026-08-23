"""Superintendent's Daily DD report(.pdf) 읽기 — 좌표 기반 표 복원.

왜 별 모듈인가 (형 지시 2026-08-23 "word파일만 됨 pdf도 읽어오기 가능하게")
--------------------------------------------------------------------------
판정 규칙(일자 포함/제외·`Date finish` 신뢰·진행 중 문구)은 `.docx` 와 **완전히
같아야** 하므로 `dock_daily_docx` 에서 그대로 가져다 쓴다.  이 파일은 PDF 에만
있는 문제, 즉 **표가 문서구조로 남아 있지 않다**는 문제만 다룬다.

PDF 는 표가 아니라 "선과 글자"다 — 실측으로 드러난 함정 3개
-----------------------------------------------------------
라이브 파일 `TDF-04.7c - Dry-dock Daily Progress Report 22.08.pdf`(10쪽) 로 재고,
셋 다 조용히 틀리는 종류였다.

1. **표가 페이지마다 끊긴다.**  Deck 표는 3쪽에서 7행까지 그리고 4쪽에서 8~12행이
   **헤더 없이** 이어진다.  `.docx` 처럼 "첫 행에 Description 헤더가 있는 표" 만
   받으면 이어진 쪽이 통째로 사라진다(실측 5행 유실).  그래서 헤더가 없는 표는
   **직전 작업표의 컬럼 좌표와 왼쪽 경계가 일치할 때만** 그 표의 연속으로 잇는다.
   좌표를 안 보고 그냥 이으면 비용표(3칸, `Description` 헤더가 있다!)까지 붙는다.

2. **칸 수가 쪽마다 다르다.**  헤더 쪽은 6칸(테두리 때문에 폭 5pt 짜리 유령 칸이
   하나 낀다), 이어진 쪽은 5칸이다.  그래서 **인덱스로 읽으면 날짜가 옆 칸으로
   밀린다.**  게다가 셀 단위로 읽으면 병합된 넓은 셀 하나가 두 컬럼에 걸쳐
   `Scheduled finish` 값이 `Date finish` 에도 복사되고(5행), 반대로 좁은 셀에서는
   두 번째 날짜가 사라졌다(7행: `20/08/2026` 유실).  둘 다 완료 판정을 뒤집는다.
   → 셀은 **행의 y 구간**과 헤더 좌표를 얻는 데만 쓰고, 값은 **글자의 x 중심**으로
   컬럼에 넣는다.  이러면 5행·7행 실측이 문서 그대로 나온다.

3. **페이지 넘김에 설명이 잘린다.**  6쪽 첫 행은 S/N 도 날짜도 없이
   `stair tower hold access.` 만 있다 — 5쪽 마지막 행의 뒷부분이다.  새 행으로
   두면 뜻 모를 한 줄이 카드에 들어가고 원래 문장은 잘린 채 남는다.  S/N·날짜가
   전부 비고 설명만 있는 행은 **앞 행에 이어 붙인다**(`row_key` 도 다시 만든다).

🔴 제목이 `.docx` 와 다르다 (실측)
---------------------------------
같은 보고서인데 PDF 템플릿의 제목은 `Leading Engine Works done by the 3rd party`,
`Leading Engine Works done by the Crew` 다 — `.docx` 의 `Leading Works done by 3rd
party` 와 달리 **중간에 `Engine` 이 낀다**.  옛 규칙은 `leading works` 를 붙여
읽어서 3rd party·Crew 표가 통째로 비고 섹션으로 흘렀다.  게다가 `3rd` 는
윗주(superscript)라 다른 y 좌표로 뽑혀서, 줄을 y 값으로만 묶으면 제목이
`... done by the party` 가 된다.  그래서 줄 묶기는 **세로 겹침**으로 한다.

한계 (정직하게)
--------------
· 텍스트가 없는 스캔 PDF(이미지만)는 이 방식으로 읽을 수 없다.  행이 0건으로
  나오고, 그 사실이 화면의 `포함 0 / 제외 0` 으로 그대로 보인다.
· 사진 쪽(7~10쪽)은 작업표가 아니라 캡션 격자라 읽지 않는다(Phase 3 대상).
· 🔴 **같은 보고서를 `.docx` 로도 `.pdf` 로도 올리면 `row_key` 가 갈릴 수 있다.**
  키는 `rules.row_key(라벨, 설명)` 이고 라벨은 같은 규칙표에서 오지만, 설명 문자열은
  python-docx 와 pdfplumber 가 서로 다르게 뽑을 수 있다(구두점·유니코드·줄바꿈 위치).
  키가 갈리면 같은 작업이 카드 두 줄로 남는다.  **검증하지 못했다** -- 라이브에 같은
  보고서의 두 포맷 쌍이 없다(docx 는 20.08 판, pdf 는 22.08 판).  안전망은 스캔
  응답의 `stale_applied`(이 보고서 카드에 있는데 지금 파일에 없는 줄)로, 화면이
  "문장이 수정되었을 수 있음 -- 중복으로 남으면 직접 지우세요" 라고 말해 준다.
"""
import hashlib
import re

import dock_daily_docx as rules

#: 컬럼 좌표를 맞대볼 때 허용하는 오차(pt).  실측에서 같은 컬럼의 왼쪽 경계는
#: 쪽이 바뀌어도 0~4pt 안에서만 흔들렸다(`Scheduled finish` 469 ↔ 473).
EDGE_TOL = 6.0


def _lines(words):
    """글자 목록 → `[(top, text)]`.  **세로 겹침**으로 한 줄을 묶는다.

    🔴 `top` 값으로 묶으면 안 된다.  윗주 `3rd` 는 같은 줄인데 top 이 0.6pt 높아서
    별 줄로 떨어지고, 그러면 제목이 `Leading Engine Works done by the party` 가 돼
    3rd party 표가 비고로 흐른다(실측).
    """
    out = []
    for word in sorted(words, key=lambda w: (w['top'], w['x0'])):
        height = max(1.0, word['bottom'] - word['top'])
        placed = None
        for line in out:
            overlap = min(line['bottom'], word['bottom']) - max(line['top'], word['top'])
            if overlap >= 0.5 * min(height, line['bottom'] - line['top']):
                placed = line
                break
        if placed is None:
            out.append({'top': word['top'], 'bottom': word['bottom'], 'words': [word]})
        else:
            placed['top'] = min(placed['top'], word['top'])
            placed['bottom'] = max(placed['bottom'], word['bottom'])
            placed['words'].append(word)
    lines = []
    for line in out:
        text = ' '.join(w['text'] for w in sorted(line['words'], key=lambda w: w['x0']))
        lines.append((line['top'], re.sub(r'\s+', ' ', text).strip()))
    return [(top, text) for top, text in sorted(lines) if text]


def _inside(word, boxes):
    cx = (word['x0'] + word['x1']) / 2.0
    cy = (word['top'] + word['bottom']) / 2.0
    return any(b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1 for b in boxes)


def _ranges(row):
    """행의 셀 → `[(x0, x1)]`.  `None`(병합으로 사라진 칸)은 건너뛴다."""
    return [(c[0], c[2]) for c in row['cells'] if c]


def _band_words(words, row):
    """그 행의 y 구간에 걸린 글자.  여러 줄 셀은 한 행 안에 여러 줄로 들어온다."""
    top, bottom = row['bbox'][1], row['bbox'][3]
    return [w for w in words
            if (w['top'] + w['bottom']) / 2.0 >= top - 1
            and (w['top'] + w['bottom']) / 2.0 <= bottom + 1]


def _center(word):
    return (word['x0'] + word['x1']) / 2.0


def _in_range(rng, x):
    """`[x0, x1)` — **반열린 구간**.

    🔴 양쪽을 닫으면 안 된다(올마이트 지적 2026-08-23).  표의 칸은 경계를 **공유**해서
    (`Description.x1 == Date start.x0`) 중심이 정확히 경계에 앉은 글자는 두 컬럼에
    동시에 들어간다 — 날짜가 설명에도 복사되거나 그 반대가 되고, 그 한 글자가 완료
    판정을 뒤집는다.  라이브 파일(10쪽)에서 경계 정확일치는 0건이라 지금 틀린 값은
    없지만, 조건이 갖춰지면 조용히 틀리는 종류라 구간 자체를 겹치지 않게 만든다.
    오른쪽 끝 경계에 앉은 글자는 어느 구간에도 안 들어가고 `_column_of` 가 가장
    가까운 컬럼(=그 컬럼)으로 보낸다.
    """
    return rng[0] <= x < rng[1]


def _text_in(words, rng):
    """`rng` 안에 x 중심이 들어오는 글자를 읽는 순서대로 이어 붙인다."""
    picked = [w for w in words if _in_range(rng, _center(w))]
    picked.sort(key=lambda w: (round(w['top'], 1), w['x0']))
    return re.sub(r'\s+', ' ', ' '.join(w['text'] for w in picked)).strip()


def _column_of(cols, x):
    """x 를 품는 컬럼, 없으면 가장 가까운 컬럼."""
    for name, rng in cols.items():
        if _in_range(rng, x):
            return name
    return min(cols, key=lambda n: min(abs(cols[n][0] - x), abs(cols[n][1] - x))) if cols else None


def _cells(cols, words, row):
    """`{컬럼명: 글자}`.  글자 하나는 **정확히 한 컬럼**에만 들어간다.

    컬럼 밖 글자는 버리지 않고 가장 가까운 컬럼에 붙인다 — 조용히 버리면 문서에
    있던 값이 화면에서 사라진다.
    """
    out = {name: [] for name in cols}
    for word in sorted(_band_words(words, row), key=lambda w: (round(w['top'], 1), w['x0'])):
        name = _column_of(cols, _center(word))
        if name:
            out[name].append(word['text'])
    return {name: re.sub(r'\s+', ' ', ' '.join(parts)).strip() for name, parts in out.items()}


def _header_columns(words, row):
    """헤더 행이면 `{컬럼명: (x0, x1)}`, 아니면 `None`.

    `.docx` 와 **같은 별칭표**(`COL_ALIASES`)를 쓴다 — 컬럼 이름 인식 규칙이 갈리면
    두 경로의 판정이 조용히 달라진다.
    """
    band = _band_words(words, row)
    found = {}
    for rng in _ranges(row):
        text = _text_in(band, rng)
        if not text:
            continue
        for name, pattern in rules.COL_ALIASES:
            if name not in found and pattern.search(text):
                found[name] = rng
                break
    return found if 'desc' in found else None


def _row_continues(cols, row):
    """이 **행**의 칸 좌표가 `cols` 와 맞는가.

    🔴 왼쪽·오른쪽 경계가 **모든 컬럼에서** 맞아야 한다.  겹침 비율로만 보면
    3칸짜리 비용표(`Description | Estimated | Actual`)가 Description 컬럼에 72%
    겹쳐서 통과하고, 예산 숫자가 작업 카드로 들어간다.
    """
    ranges = _ranges(row)
    if len(ranges) < len(cols):
        return False
    for x0, x1 in cols.values():
        if not any(abs(r0 - x0) <= EDGE_TOL and abs(r1 - x1) <= EDGE_TOL * 2 for r0, r1 in ranges):
            return False
    return True


def _continues(cols, rows):
    """헤더 없는 이 **표**가 `cols` 를 가진 표의 연속인가.

    🔴 첫 행만 보면 안 된다(올마이트 지적 2026-08-23).  이 템플릿은 병합 때문에
    칸이 `None` 으로 사라지는 행이 실제로 있고(1쪽 정보표에서 실측: 4칸 행 사이에
    1칸 행), 그게 이어진 쪽의 첫 행이면 표 **전체가 조용히 사라진다**.  한 행이라도
    좌표가 맞으면 그 표는 같은 표의 연속이다 — 비용표는 어느 행도 못 맞춘다.
    """
    return any(_row_continues(cols, row) for row in rows)


def _append_desc(group, text):
    """페이지 넘김에 잘린 뒷부분을 앞 행에 이어 붙이고 키를 다시 만든다."""
    last = group['rows'][-1]
    last['desc'] = re.sub(r'\s+', ' ', '%s %s' % (last['desc'], text)).strip()
    last['row_key'] = rules.row_key(group['label'] or group['heading'], last['desc'])


def parse_pages(pages):
    """좌표만 받아 `.docx` 파서와 **같은 모양**을 만든다(테스트 입구).

    `pages` = `[{'words': [{'text','x0','x1','top','bottom'}],
                 'tables': [{'bbox': (x0,top,x1,bottom),
                             'rows': [{'bbox': ..., 'cells': [(x0,top,x1,bottom)|None]}]}]}]`
    pdfplumber 객체를 그대로 받지 않는 이유: 이 판정을 **PDF 파일 없이** 단위테스트로
    잠글 수 있어야 한다.
    """
    groups, unmapped = [], []
    pending = None          # (heading, label, section_key)
    cols = None             # 진행 중인 작업표의 컬럼 좌표
    group = None            # 그 표가 채우고 있는 group
    for page in pages:
        words = page['words']
        tables = sorted(page['tables'], key=lambda t: t['bbox'][1])
        boxes = [t['bbox'] for t in tables]
        outside = [w for w in words if not _inside(w, boxes)]
        items = [(top, 'para', text) for top, text in _lines(outside)]
        items += [(t['bbox'][1], 'table', t) for t in tables]
        for _, kind, value in sorted(items, key=lambda i: (i[0], 0 if i[1] == 'para' else 1)):
            if kind == 'para':
                key, label = rules.heading_target(value)
                if key:
                    pending, cols, group = (value, label, key), None, None
                elif rules.HEADING_HINT.search(value):
                    pending, cols, group = (value, value, None), None, None
                    unmapped.append(value)
                continue
            rows = value['rows']
            if not rows:
                continue
            header = _header_columns(words, rows[0])
            if header and pending:
                heading, label, section_key = pending
                group = {'heading': heading, 'label': label,
                         'section_key': section_key, 'rows': []}
                groups.append(group)
                cols, pending, body, fragment_ok = header, None, rows[1:], False
            elif header and cols and group and _continues(cols, rows):
                # 머리글을 **되풀이하는** 연속 표.  실측 템플릿은 되풀이하지 않지만
                # (4·6쪽 다 머리글 없이 시작), 되풀이하는 문서에서 표를 통째로 버리면
                # 조용한 손실이다(올마이트 지적).  좌표가 다 맞을 때만 잇는다 -- 비용표
                # 같은 남의 표는 `_continues` 를 통과하지 못한다.
                body, fragment_ok = rows[1:], False
            elif header:
                # 제목도 없고 앞 표의 연속도 아닌 머리글 표는 작업표가 아니다(비용표 등).
                # 제목을 물고 있으면 다음 표로 새므로 여기서 비운다 -- `.docx` 와 같다.
                cols, group = None, None
                continue
            elif cols and group and _continues(cols, rows):
                body, fragment_ok = rows, True
            else:
                cols, group, pending = None, None, None
                continue
            for index, row in enumerate(body):
                cell = _cells(cols, words, row)
                desc = cell.get('desc', '')
                if not desc:
                    continue
                dates = (cell.get('start', ''), cell.get('sched', ''), cell.get('finish', ''))
                # 🔴 잘린 조각 붙이기는 **머리글 없이 이어진 표의 첫 행**에만 쓴다
                #    (올마이트 지적 2026-08-23).  표 중간의 번호·날짜 없는 행까지 앞
                #    행에 붙이면 서로 무관한 두 작업이 한 문장으로 합쳐진다.  페이지
                #    넘김에 잘린 조각은 정의상 새 표의 첫 행이다(실측 6쪽 `stair tower
                #    hold access.`).  아니면 그냥 자기 행으로 남긴다 -- 어느 쪽도 잃지 않는다.
                if (fragment_ok and index == 0 and not cell.get('sn')
                        and not any(dates) and group['rows']):
                    _append_desc(group, desc)
                    continue
                group['rows'].append({
                    'row_key': rules.row_key(group['label'] or group['heading'], desc),
                    'desc': desc, 'start': dates[0], 'sched': dates[1], 'finish': dates[2],
                })
    return {'groups': [g for g in groups if g['rows']], 'unmapped_headings': unmapped}


def _pdf_pages(path):
    """pdfplumber 객체 → `parse_pages` 가 먹는 순수 좌표 딕셔너리."""
    import pdfplumber                      # 지연 import: PDF 를 안 읽는 요청엔 부담 0
    out = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = []
            for table in page.find_tables():
                tables.append({'bbox': tuple(table.bbox),
                               'rows': [{'bbox': tuple(r.bbox),
                                         'cells': [tuple(c) if c else None for c in r.cells]}
                                        for r in table.rows]})
            words = [{'text': w['text'], 'x0': w['x0'], 'x1': w['x1'],
                      'top': w['top'], 'bottom': w['bottom']} for w in page.extract_words()]
            out.append({'words': words, 'tables': tables})
    return out


def parse(path):
    return parse_pages(_pdf_pages(path))


def scan(path, report_date):
    """`.docx` 의 `scan()` 과 **같은 페이로드**.  판정은 그쪽 규칙을 그대로 쓴다."""
    return rules.judge(parse(path), report_date)


# ─────────────────────────────────────────────────────────────────────
#  PDF 에 박힌 사진 (형 지시 2026-08-23 "사진도 자동으로")
# ─────────────────────────────────────────────────────────────────────
def photo_pages(path):
    """`[[{'key','data','ext','mime','page','top','x0'}]]` — 쪽별 사진 목록.

    바이트를 못 뽑는 이미지는 `None` 자리를 남기지 않고 세어서 올린다.
    """
    import pdfplumber                      # 지연 import: PDF 를 안 읽는 요청엔 부담 0
    pages, skipped = [], 0
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages):
            found = []
            for image in page.images:
                stream = image.get('stream')
                try:
                    data = stream.get_data() if stream is not None else b''
                except Exception:             # pragma: no cover - 손상 스트림
                    data = b''
                ext, mime = rules.photo_kind(data or b'')
                if not ext:
                    skipped += 1
                    continue
                # 🔴 `key` 는 **full sha256** 이다.  접기·레터헤드 판정을 12자로 하면
                #    앞자리가 겹친 서로 다른 사진이 조용히 하나로 접힌다.
                found.append({'key': hashlib.sha256(data).hexdigest(), 'data': data,
                              'ext': ext, 'mime': mime, 'page': index,
                              'top': float(image.get('top') or 0),
                              'x0': float(image.get('x0') or 0)})
            pages.append(found)
    return pages, skipped


def photos(path):
    """PDF 사진.  `.docx` 의 `photos()` 와 **같은 모양**을 돌려준다.

    🔴 캡션을 못 읽는다 -- 만들지 않는다(`captions: False`).  실측 22.08 파일의
       사진쪽은 3장씩 쌓인 두 열이고, 그 아래 글줄은 **두 열의 캡션이 한 줄로
       뭉쳐**(`'ME Overhauling by Cat Asea Marine Rope guard removal'`) 뽑힌다.
       어느 반쪽이 어느 사진인지 좌표로 가릴 수 없으므로, 반을 잘라 붙이면 절반의
       사진에 **틀린 설명**이 달린 채 형이 그대로 조선소에 보낸다.  캡션은 빈칸으로
       두고 형이 앱에서 쓴다.
    🔴 **모든 쪽에 반복되는 그림은 레터헤드**다(실측: 같은 10,697 byte 그림이 10쪽
       전부에 있다 = 회사 로고).  두 쪽 이상에 같은 바이트가 나오면 사진이 아니다.
       "쪽마다 나오는 걸 한 장으로 접기" 로 짜면 로고가 사진 1장으로 들어온다.
    """
    return fold_photos(*photo_pages(path))


def fold_photos(pages, skipped=0):
    """쪽별 이미지 목록 → 사진 목록.  `photos()` 의 판정부만 떼어 낸 것(테스트용).

    실제 PDF 를 fixture 로 두지 않기 위해 좌표·바이트만 먹여 잠근다(이 모듈의 다른
    파서와 같은 이유 -- 라이브 파일은 선박 공사 내용이라 저장소에 못 넣는다).
    """
    page_count = {}
    for found in pages:
        for key in {p['key'] for p in found}:
            page_count[key] = page_count.get(key, 0) + 1
    out, seen, keys, dupes, furniture, over = [], set(), set(), 0, 0, 0
    for found in pages:
        for photo in sorted(found, key=lambda p: (p['top'], p['x0'])):
            if page_count.get(photo['key'], 0) > 1:
                furniture += 1
                continue
            if photo['key'] in seen:
                dupes += 1
                continue
            seen.add(photo['key'])
            if len(out) >= rules.PHOTO_MAX:
                over += 1
                continue
            short = rules.photo_key(photo['key'], keys)
            keys.add(short)
            out.append({'photo_key': short, 'caption': '', 'data': photo['data'],
                        'ext': photo['ext'], 'mime': photo['mime'],
                        'size': len(photo['data'])})
    return {'photos': out, 'captions': False, 'duplicates': dupes, 'skipped': skipped,
            'photo_limit': over, 'letterhead': furniture}
