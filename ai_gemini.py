# ─── 보고서 → 항목 자동 추출 (Gemini + 엑셀 파서) ─────────────
def _findings_workbook(title, subtitle, headers, rows, wrap_cols, widths):
    """검사 findings → 스타일된 1시트 워크북 BytesIO 반환."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook(); ws = wb.active; ws.title = 'List'
    F = 'Malgun Gothic'
    N = len(headers)
    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w

    title_fill = PatternFill('solid', start_color='1F3A5F')
    sub_fill   = PatternFill('solid', start_color='2C5282')
    hdr_fill   = PatternFill('solid', start_color='34495E')
    def_fill   = PatternFill('solid', start_color='FCE8E6')   # Defect 행 연한 적색
    thin = Side(style='thin', color='BBBBBB')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=N)
    c = ws.cell(row=1, column=1, value=title)
    c.font = Font(name=F, size=14, bold=True, color='FFFFFF'); c.fill = title_fill
    c.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=N)
    c = ws.cell(row=2, column=1, value=subtitle)
    c.font = Font(name=F, size=10, italic=True, color='ECF0F1'); c.fill = sub_fill
    c.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws.row_dimensions[2].height = 18
    ws.row_dimensions[3].height = 6

    HDR = 4
    for ci, h in enumerate(headers, start=1):
        cc = ws.cell(row=HDR, column=ci, value=h)
        cc.font = Font(name=F, size=11, bold=True, color='FFFFFF'); cc.fill = hdr_fill
        cc.alignment = Alignment(horizontal='center', vertical='center'); cc.border = border
    ws.row_dimensions[HDR].height = 24

    body = Font(name=F, size=10)
    top_wrap = Alignment(horizontal='left', vertical='top', wrap_text=True)
    center = Alignment(horizontal='center', vertical='top')
    r_idx = HDR + 1
    for row in rows:
        max_len = 1
        for ci, val in enumerate(row, start=1):
            cell = ws.cell(row=r_idx, column=ci, value=val)
            cell.font = body; cell.border = border
            cell.alignment = top_wrap if ci in wrap_cols else center
            if ci in wrap_cols and val:
                w = widths[ci - 1]
                max_len = max(max_len, sum((len(ln) // max(int(w / 1.6), 1)) + 1
                                           for ln in str(val).split('\n')))
        # Defect 행 살짝 음영
        if 'Category' in headers:
            cat_col = headers.index('Category') + 1
            if ws.cell(row=r_idx, column=cat_col).value == 'Defect':
                for ci in range(1, N + 1):
                    ws.cell(row=r_idx, column=ci).fill = def_fill
        ws.row_dimensions[r_idx].height = max(20, min(120, 15 * max_len + 4))
        r_idx += 1

    ws.freeze_panes = f'A{HDR + 1}'
    if r_idx - 1 > HDR:
        ws.auto_filter.ref = f'A{HDR}:{get_column_letter(N)}{r_idx - 1}'
    ws.print_options.horizontalCentered = True
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1; ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f'{HDR}:{HDR}'

    bio = BytesIO(); wb.save(bio); bio.seek(0)
    return bio


def _gemini_call_json(parts, model=None):
    """parts(list) → Gemini generateContent → 파싱된 JSON dict 또는 {'error':...}."""
    if not GEMINI_API_KEY:
        return {'error': 'NO_API_KEY'}
    import urllib.request, urllib.error
    mdl = model or GEMINI_MODEL
    body = {'contents': [{'parts': parts}],
            'generationConfig': {'response_mime_type': 'application/json'}}
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{mdl}:generateContent')
    req = urllib.request.Request(
        url, data=json.dumps(body).encode('utf-8'),
        headers={'content-type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as he:
        try:
            detail = he.read().decode('utf-8')[:300]
        except Exception:
            app.logger.exception('gemini-call-json')
            detail = str(he)
        return {'error': 'API_CALL_FAILED', 'detail': detail}
    except Exception as e:
        app.logger.exception('gemini-call-json')
        return {'error': 'API_CALL_FAILED', 'detail': str(e)}
    text = ''
    try:
        cands = data.get('candidates') or []
        if not cands:
            return {'error': 'API_CALL_FAILED', 'detail': json.dumps(data)[:300]}
        for part in (cands[0].get('content', {}).get('parts') or []):
            if isinstance(part.get('text'), str):
                text += part['text']
    except Exception as e:
        app.logger.exception('gemini-call-json')
        return {'error': 'PARSE_FAILED', 'raw': str(e)}
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text[:4].lower() == 'json':
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        app.logger.exception('gemini-call-json')
        return {'error': 'PARSE_FAILED', 'raw': text[:300]}


def _coerce_translation_items(res):
    """Gemini 응답을 [{'i':int,'en':str}] 리스트로 정규화. list/dict/다양한 키 모두 수용."""
    if isinstance(res, dict):
        if res.get('error'):
            return None  # 호출 자체 실패
        arr = (res.get('translations') or res.get('items')
               or res.get('results') or res.get('data'))
        if arr is None:
            # 단일 객체이거나 {i:en} 매핑일 수 있음
            if 'i' in res and ('en' in res or 'text' in res):
                arr = [res]
            else:
                arr = []
    elif isinstance(res, list):
        arr = res
    else:
        arr = []
    return arr if isinstance(arr, list) else []


def _translate_batch_en(texts, group):
    """group(인덱스 리스트) 한 묶음 번역 → {원본인덱스: 영문}. 실패 시 None."""
    payload = json.dumps([{'i': i, 'text': texts[i]} for i in group], ensure_ascii=False)
    prompt = (
        "너는 선박 기술 감독(ship superintendent)이다. 아래 JSON 배열의 각 한국어(또는 한영 혼용) "
        "텍스트를 선박 관리 현업에서 자연스럽게 쓰는 영어로 번역하라.\n"
        "- 장비명·약어·단위·수치(예: BRG, RPM, S/W pump, LT cooler, EGCS, °C, kts)는 그대로 둔다.\n"
        "- 줄바꿈과 번호 매김(1. 2. ...) 구조를 그대로 보존한다.\n"
        "- 이미 영어인 부분은 그대로 둔다. 의미를 바꾸거나 내용을 덧붙이지 마라.\n"
        "반드시 {\"translations\":[...]} 형태의 JSON 객체로만 답하라. 입력의 i를 그대로 사용하라.\n"
        '형식: {"translations":[{"i":0,"en":"..."}]}\n\n[입력]\n' + payload)
    res = _gemini_call_json([{'text': prompt}], model=_model_for('translate'))
    arr = _coerce_translation_items(res)
    if arr is None:
        return None  # API 호출 실패 → 상위에서 분할 재시도
    out = {}
    for tr in arr:
        if not isinstance(tr, dict):
            continue
        try:
            i = int(tr.get('i'))
        except (TypeError, ValueError):
            continue
        en = tr.get('en') if isinstance(tr.get('en'), str) else tr.get('text')
        if isinstance(en, str) and en.strip():
            out[i] = en
    return out


def _gen_issue_summaries(payload_items):
    """payload_items: [{'i':int,'description':str,'action':str}] →
    {i: {'desc':str, 'action':str}} (한국어 요약). 키 없음/실패 시 빈 dict 부분 반환."""
    result = {}
    if not GEMINI_API_KEY or not payload_items:
        return result

    def run(group, depth=0):
        if not group:
            return
        sub = [payload_items[k] for k in group]
        prompt = (
            "너는 선박 기술 감독(ship superintendent)이다. 아래 JSON 배열의 각 업무 항목에 대해 "
            "두 가지를 한국어로 작성하라.\n"
            "- desc: description의 핵심 문제를 1문장(최대 2문장)으로 짧게 요약\n"
            "- action: action(최신 조치내용)을 한 줄로 짧게 요약 (내용 없으면 빈 문자열)\n"
            "■ 매우 중요: 요약은 원문(description/action)에 실제로 쓰인 단어와 표현을 그대로 사용해 "
            "압축하라. 동의어로 바꾸거나 새 표현을 지어내지 말고, 불필요한 부분만 덜어내라. "
            "원문에 있는 장비명·기술용어·약어·표현(예: EGCS, Pump, Auto mode, Maker Trouble Shooting, BRG, RPM, LT cooler)은 "
            "그대로 보존한다. 과장/추측/내용 추가 금지.\n"
            "입력의 i를 그대로 사용해 JSON 객체로만 답하라.\n"
            '형식: {"items":[{"i":0,"desc":"...","action":"..."}]}\n\n[입력]\n'
            + json.dumps(sub, ensure_ascii=False))
        res = _gemini_call_json([{'text': prompt}], model=_model_for('summary'))
        arr = _coerce_translation_items(res)  # translations/items/results/data 모두 수용
        if arr is None:
            if len(group) > 1 and depth < 6:
                mid = len(group) // 2
                run(group[:mid], depth + 1); run(group[mid:], depth + 1)
            return
        got = set()
        for o in arr:
            if not isinstance(o, dict):
                continue
            try:
                i = int(o.get('i'))
            except (TypeError, ValueError):
                continue
            result[i] = {
                'desc':   (o.get('desc') or o.get('desc_summary') or '').strip(),
                'action': (o.get('action') or o.get('action_summary') or '').strip(),
            }
            got.add(i)
        missing = [k for k in group if k not in got]
        if missing and len(group) > 1 and depth < 6:
            mid = max(1, len(missing) // 2)
            run(missing[:mid], depth + 1); run(missing[mid:], depth + 1)

    CHUNK = 12
    idxs = list(range(len(payload_items)))
    for s in range(0, len(idxs), CHUNK):
        run(idxs[s:s + CHUNK])
    return result


def _latest_action_progress(acts):
    if not acts:
        return ''
    try:
        best = sorted(acts, key=lambda a: (a.get('date') or ''))[-1]
    except Exception as e:
        app.logger.warning('latest-action-progress: %s', e)
        best = acts[-1]
    return (best.get('progress') or '').strip()


def _latest_action(acts):
    """최신 action(날짜 최댓값)의 (date, progress) 반환."""
    if not acts:
        return '', ''
    try:
        best = sorted(acts, key=lambda a: (a.get('date') or ''))[-1]
    except Exception as e:
        app.logger.warning('latest-action: %s', e)
        best = acts[-1]
    return (best.get('date') or '').strip(), (best.get('progress') or '').strip()


def _md_label(d):
    try:
        y, m, dd = d.split('-')
        return f'[{int(m)}/{int(dd)}]'
    except Exception as e:
        app.logger.warning('md-label: %s', e)
        return f'[{d}]' if d else ''


def _translate_texts_en(texts):
    """한국어(한영 혼용) 문자열 리스트 → 선박 감독 현업 영어. 키 없음/실패 시 원문 유지.
    묶음 실패 시 절반→1:1로 분할 재시도하여 '일부 누락'을 방지."""
    if not GEMINI_API_KEY:
        return list(texts)
    out = list(texts)
    idxs = [i for i, t in enumerate(texts) if t and str(t).strip()]

    def run(group, depth=0):
        if not group:
            return
        res = _translate_batch_en(texts, group)
        if res is None:
            # 호출 실패 → 분할 재시도
            if len(group) > 1 and depth < 6:
                mid = len(group) // 2
                run(group[:mid], depth + 1)
                run(group[mid:], depth + 1)
            return
        missing = [i for i in group if i not in res]
        for i, en in res.items():
            out[i] = en
        # 일부만 응답에 빠진 경우도 분할 재시도
        if missing and len(group) > 1 and depth < 6:
            mid = max(1, len(missing) // 2)
            run(missing[:mid], depth + 1)
            run(missing[mid:], depth + 1)

    CHUNK = 12
    for s in range(0, len(idxs), CHUNK):
        run(idxs[s:s + CHUNK])
    return out


def _translate_rows_en(rows):
    """이슈 행들의 item_topic/description/actions[].progress 를 영문으로 치환(제자리)."""
    bucket, texts = [], []
    for r in rows:
        if r.get('item_topic'):
            bucket.append((r, 'item_topic', None)); texts.append(r['item_topic'])
        if r.get('description'):
            bucket.append((r, 'description', None)); texts.append(r['description'])
        for ai, a in enumerate(r.get('actions') or []):
            if a.get('progress'):
                bucket.append((r, 'actions', ai)); texts.append(a['progress'])
    if not texts:
        return
    tr = _translate_texts_en(texts)
    for (r, field, ai), en in zip(bucket, tr):
        if field == 'actions':
            r['actions'][ai]['progress'] = en
        else:
            r[field] = en


_MARITIME_TERMS = (
    " 요약은 선박 현업(감독/기관부) 용어로 옮긴다. 일반어 → 현업어 매핑: "
    "repair=수리(※'보수'로 쓰지 말 것), cleaning/clean=소제, replace/renew/renewal=신환, "
    "install/fitting=설치, overhaul=O/H(분해점검), inspection/survey=수검, maintenance=정비, "
    "check/verify=확인, adjust/adjustment=조정, calibration=교정, test=시험, crack=균열, "
    "corrosion/rust=부식, leak/leakage=누설(누유/누수), wear/weardown=마모, deformation=변형, "
    "spare parts=예비품, weld/welding=용접, coating/painting=도장, submit=제출, "
    "place onboard=본선 비치. "
    "목록에 없어도 선박에서 통용되는 자연스러운 표현을 우선 사용한다. "
)


def _findings_prompt(kind):
    if kind == 'cs':
        return (
            "다음은 선박 컨디션 서베이(상태검사) 보고서다. 보고서에 적힌 지적/관찰 항목을 "
            "빠짐없이 추출해 지정한 JSON으로만 답하라. 각 항목 필드:\n"
            "- category: 'Defect' 또는 'Observation' (시정이 필요한 지적은 Defect, 권고/관찰사항은 Observation)\n"
            "- item: 짧은 제목 한 줄 (예: 'Main deck 부식')\n"
            "- description: 지적 상세 내용을 원문 그대로 복사한다(영문이면 영문 그대로). 요약·변형 금지.\n"
            "- remark: description의 핵심 지적사항을 한국어로 1~2문장으로 간결하게 요약한다(전체 직역 금지). 문장은 '~함/~됨/~음' 형태의 음슴체(개조식)로 끝맺는다. "
            "기술 명칭·장비명·약어(예: ECDIS, DCP, DRS, smoke detector, high-high level alarm 등)는 번역하지 말고 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
            "없는 내용을 지어내지 말 것. 항목이 하나도 없으면 items를 빈 배열로.\n"
            '형식: {"items":[{"category":"Defect","item":"","description":"","remark":""}]}'
        )
    return (
        "다음은 선박 SIRE 2.0 점검 보고서다. 지적(결함) 사항만 추출한다.\n"
        "■ 포함 대상: 'Observable or detectable deficiency' 또는 'Not as expected'로 표시된 부정적 지적 "
        "(보고서에서 빨간색 글씨로 적힌 항목). 또한 'Photograph' 분류의 지적(예: 'Photo not representative', "
        "'Photograph supplied: ...' 아래 빨간 이탤릭 설명)처럼 사진 증빙이 부적절·불일치하다는 지적도 반드시 포함한다.\n"
        "■ 제외 대상: 'Exceeded normal expectation' 등 칭찬/긍정 평가(초록색 글씨)는 절대 포함하지 마라.\n"
        "각 지적 항목의 필드:\n"
        "- item: 항목 왼쪽에 표시된 분류 라벨을 괄호로 먼저 붙이고, 그 뒤에 굵게 표시된 "
        "지적 제목을 그대로 이어 붙인다. 분류 라벨은 보고서에 나온 그대로 쓴다 — "
        "Hardware · Human · Photograph · Process · Other 등 무엇이든. 예: "
        "'(Hardware)Misc Nautical Equipment – Maintenance deferred, awaiting spares', "
        "'(Human)Senior Engineer Officer – Not as expected', '(Photograph)Photo not representative'.\n"
        "- description: 제목 아래의 상세 본문(설명/이탤릭 문장 포함)을 영어 원문 그대로 복사한다. 요약·변형 금지.\n"
        "- remark: description의 핵심 지적사항을 한국어로 1~2문장으로 간결하게 요약한다(전체 직역 금지). 문장은 '~함/~됨/~음' 형태의 음슴체(개조식)로 끝맺는다. "
        "기술 명칭·장비명·약어(예: ECDIS, DCP, DRS, smoke detector, high-high level alarm, turn table 등)는 번역하지 말고 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
        "없는 내용을 지어내지 말 것. 지적이 하나도 없으면 items를 빈 배열로.\n"
        '형식: {"items":[{"item":"","description":"","remark":""}]}'
    )


def _normalize_findings(parsed, kind):
    out = []
    if isinstance(parsed, list):
        arr = parsed
    elif isinstance(parsed, dict):
        arr = parsed.get('items') or parsed.get('findings') or []
    else:
        arr = []
    for it in (arr or []):
        if not isinstance(it, dict):
            continue
        rec = {
            'item':        (it.get('item') or '').strip(),
            'description': (it.get('description') or '').strip(),
            'remark':      (it.get('remark') or '').strip(),
        }
        if kind == 'cs':
            cat = it.get('category')
            rec['category'] = cat if cat in ('Defect', 'Observation') else 'Observation'
        if rec['item'] or rec['description']:
            out.append(rec)
    return out


def _xlsx_extract(raw_bytes, kind):
    """엑셀: 헤더가 명확하면 직접 매핑(AI 불필요), 자유양식이면 텍스트화 후 Gemini.
    반환: ('items', [...])  또는  ('text', '<탭구분 텍스트>')."""
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for r in ws.iter_rows(values_only=True):
        rows.append(['' if c is None else str(c).strip() for c in r])
    if not rows:
        return ('items', [])

    KEY = {
        'category':    ['category', '구분', '분류', 'type', 'def/obs'],
        'item':        ['item', '항목', 'title', 'subject', '제목', 'short gen name', 'gen name', 'short name'],
        'description': ['description', 'detail', 'details', '내용', '상세', 'finding', 'observation', 'remarks/finding'],
        'remark':      ['remark', 'remarks', '비고', 'note', 'notes', 'comment', 'action', '조치'],
    }
    header_idx, colmap = None, {}
    for i, row in enumerate(rows[:6]):
        m = {}
        for ci, cell in enumerate(row):
            lc = cell.lower()
            for field, keys in KEY.items():
                if field in m:
                    continue
                if any(k == lc or k in lc for k in keys):
                    m[field] = ci
        if 'description' in m or ('item' in m and len(m) >= 2):
            header_idx, colmap = i, m
            break

    if header_idx is not None:
        items = []
        for row in rows[header_idx + 1:]:
            if not any(row):
                continue
            def g(f):
                ci = colmap.get(f)
                return row[ci] if ci is not None and ci < len(row) else ''
            rec = {'item': g('item'), 'description': g('description'), 'remark': g('remark')}
            if kind == 'cs':
                cat = (g('category') or '').strip().lower()
                rec['category'] = 'Defect' if cat.startswith('def') or '지적' in cat else 'Observation'
            if not rec['description'] and rec['item']:
                rec['description'] = rec['item']
            if rec['item'] or rec['description']:
                items.append(rec)
        return ('items', items)

    # 자유 양식 → 텍스트(TSV)로 변환
    lines = ['\t'.join(r) for r in rows if any(r)]
    return ('text', '\n'.join(lines[:400]))


def _summarize_remarks(items, kind):
    """엑셀 직접매핑 항목들의 remark를, 각 description의 한글 요약으로 채운다(배치 1회 호출).
    GEMINI 키 없거나 실패 시 기존 remark 값을 그대로 유지."""
    if not GEMINI_API_KEY or not items:
        return items
    payload = json.dumps(
        [{'i': idx, 'description': (it.get('description') or '')} for idx, it in enumerate(items)],
        ensure_ascii=False)
    prompt = (
        "아래는 선박 점검 지적 항목들의 description 목록(JSON 배열)이다. 각 항목의 description을 "
        "한국어로 1~2문장으로 간결하게 요약하라(전체 직역 금지). 문장은 '~함/~됨/~음' 형태의 음슴체(개조식)로 끝맺어라. 기술 명칭·장비명·약어"
        "(예: ECDIS, DCP, DRS, smoke detector, high-high level alarm 등)는 번역하지 말고 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
        "입력의 i 값을 그대로 사용해 JSON으로만 답하라.\n"
        '형식: {"summaries":[{"i":0,"remark":"요약문"}]}\n\n[입력]\n' + payload)
    res = _gemini_call_json([{'text': prompt}], model=_model_for('remark'))
    if isinstance(res, dict):
        if res.get('error'):
            return items
        arr = res.get('summaries') or res.get('items') or res.get('translations') or []
    elif isinstance(res, list):
        arr = res
    else:
        arr = []
    by_i = {}
    for s in arr:
        if not isinstance(s, dict):
            continue
        try:
            by_i[int(s.get('i'))] = (s.get('remark') or s.get('en') or '').strip()
        except (TypeError, ValueError):
            pass
    for idx, it in enumerate(items):
        if by_i.get(idx):
            it['remark'] = by_i[idx]
    return items


def _extract_findings_from_upload(f, kind):
    """업로드 FileStorage → 항목 리스트. (items, err) 반환."""
    name = (f.filename or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    raw = f.read()
    size_mb = len(raw) / (1024 * 1024)

    if ext in ('xlsx', 'xls'):
        try:
            mode, data = _xlsx_extract(raw, kind)
        except Exception as e:
            app.logger.exception('extract-findings-from-upload')
            return None, {'reason': 'XLSX_PARSE_FAILED', 'message': f'엑셀을 읽지 못했습니다: {e}'}
        if mode == 'items':
            return _summarize_remarks(data, kind), None
        parsed = _gemini_call_json([{'text': _findings_prompt(kind) + '\n\n[보고서 표 내용]\n' + data}], model=_model_for('findings'))
    elif ext == 'pdf':
        if size_mb > 15:
            return None, {'reason': 'TOO_LARGE', 'message': f'PDF가 너무 큽니다({size_mb:.1f}MB). 15MB 이하로 줄이거나 페이지를 나눠 올려주세요.'}
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': 'application/pdf', 'data': b64}},
            {'text': _findings_prompt(kind)},
        ], model=_model_for('findings'))
    elif ext in ('png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'):
        if size_mb > 15:
            return None, {'reason': 'TOO_LARGE', 'message': f'이미지가 너무 큽니다({size_mb:.1f}MB).'}
        import mimetypes
        media = mimetypes.guess_type(name)[0] or 'image/jpeg'
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': media, 'data': b64}},
            {'text': _findings_prompt(kind)},
        ], model=_model_for('findings'))
    else:
        return None, {'reason': 'BAD_TYPE', 'message': 'PDF, 이미지, 엑셀(xlsx) 파일만 지원합니다.'}

    if isinstance(parsed, dict) and parsed.get('error') == 'NO_API_KEY':
        return None, {'reason': 'no_api_key', 'message': 'AI 자동추출이 설정되지 않았습니다(키 미설정).'}
    if isinstance(parsed, dict) and parsed.get('error'):
        return None, {'reason': parsed['error'], 'message': '자동 추출에 실패했습니다.',
                      'detail': parsed.get('detail') or parsed.get('raw')}
    return _normalize_findings(parsed, kind), None


@app.route('/api/cs/surveys/<int:sid>/extract-report', methods=['POST'])
@login_required
def api_cs_extract_report(sid):
    if not query('SELECT id FROM cs_surveys WHERE id=?', (sid,), one=True):
        abort(404)
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'ok': False, 'message': '파일이 없습니다.'}), 400
    items, err = _extract_findings_from_upload(request.files['file'], 'cs')
    if err:
        return jsonify({'ok': False, **err}), 200
    return jsonify({'ok': True, 'items': items, 'count': len(items)})


@app.route('/api/cs/surveys/<int:sid>/export')
@login_required
def api_cs_survey_export(sid):
    from flask import send_file
    s = query('''SELECT cs.*, v.name AS vessel_name
                   FROM cs_surveys cs JOIN vessels v ON v.id = cs.vessel_id
                  WHERE cs.id=?''', (sid,), one=True)
    if not s:
        abort(404)
    fr = query('''SELECT category, no, item, description, remark, status
                    FROM cs_findings WHERE survey_id=?
                   ORDER BY CASE category WHEN 'Defect' THEN 0 ELSE 1 END, no, id''', (sid,))
    # RECTIFICATION·PHOTO 2열은 공란으로 출력(현장기입용). REMARK는 export에서 제외.
    rows = [[r['category'], r['no'], r['item'] or '', r['description'] or '',
             '', '', r['status'] or ''] for r in fr]
    vessel = s['vessel_name']
    title = f"Condition Survey — {vessel}  {s['year']} Q{s['quarter']}"
    sub_bits = [f"수검일: {s['inspection_date'] or '-'}", f"Vendor: {s['vendor'] or '-'}",
                f"총 {len(rows)}건 (Defect {sum(1 for r in fr if r['category']=='Defect')} / "
                f"Observation {sum(1 for r in fr if r['category']=='Observation')})"]
    headers = ['Category', 'No.', 'ITEM', 'DESCRIPTION', 'RECTIFICATION', 'PHOTO', 'STATUS']
    bio = _findings_workbook(title, '   │   '.join(sub_bits), headers, rows,
                             wrap_cols={3, 4, 5, 6}, widths=[12, 6, 28, 50, 40, 30, 10])
    fname = f"CS_{_safe_filename(vessel)}_{s['year']}Q{s['quarter']}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ----- CS 첨부파일 -----

@app.route('/api/cs/surveys/<int:sid>/attachments', methods=['GET'])
@login_required
def api_cs_attachments_list(sid):
    rows = query(
        'SELECT * FROM cs_attachments WHERE survey_id=? ORDER BY id DESC',
        (sid,),
    )
    return jsonify([dict(r) for r in rows])


@app.route('/api/cs/surveys/<int:sid>/attachments', methods=['POST'])
@login_required
def api_cs_attachment_upload(sid):
    if not query('SELECT id FROM cs_surveys WHERE id=?', (sid,), one=True):
        abort(404)
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다.'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '파일명이 없습니다.'}), 400
    if not _ext_allowed(f.filename):
        return jsonify({'error': '허용되지 않는 파일 형식입니다.'}), 400

    ext = os.path.splitext(f.filename)[1]
    stored = f"cs_{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(UPLOAD_DIR, stored)
    f.save(save_path)
    size = os.path.getsize(save_path)

    aid = execute("""
        INSERT INTO cs_attachments
            (survey_id, filename, stored_name, file_size, mime_type, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (sid, f.filename, stored, size, f.mimetype, session.get('username')))
    return jsonify({'id': aid, 'filename': f.filename, 'file_size': size}), 201


@app.route('/api/cs/attachments/<int:aid>', methods=['GET'])
@login_required
def api_cs_attachment_get(aid):
    a = query('SELECT * FROM cs_attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    inline = request.args.get('inline')
    return send_from_directory(
        UPLOAD_DIR, a['stored_name'],
        as_attachment=not inline,
        download_name=a['filename'],
    )


@app.route('/api/cs/attachments/<int:aid>', methods=['DELETE'])
@login_required
def api_cs_attachment_delete(aid):
    a = query('SELECT * FROM cs_attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    p = os.path.join(UPLOAD_DIR, a['stored_name'])
    if os.path.exists(p):
        try: os.remove(p)
        except OSError:
            app.logger.exception('cs-attachment-delete')
    execute('DELETE FROM cs_attachments WHERE id=?', (aid,))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  API — Vetting Status (비정기, 선박당 0~N건, CNTR 제외)
# ═════════════════════════════════════════════════════════════════
VETTING_TYPES = ('VLCC', 'AFRAMAX', 'LR', 'MR')


def _vetting_with_counts(v):
    """vetting dict에 카운트 추가. manual override 적용."""
    vid = v['id']
    rows = query("""
        SELECT status, COUNT(*) AS n
          FROM vt_findings
         WHERE vetting_id = ?
         GROUP BY status
    """, (vid,))
    auto_open = auto_closed = 0
    for r in rows:
        if r['status'] == 'Closed': auto_closed = r['n']
        else: auto_open = r['n']
    auto_total = auto_open + auto_closed

    d = dict(v)
    d['observation_count'] = v['manual_observation_count'] if v['manual_observation_count'] is not None else auto_total
    d['close_count']       = v['manual_close_count']       if v['manual_close_count']       is not None else auto_closed
    d['open_count']        = v['manual_open_count']        if v['manual_open_count']        is not None else max(0, d['observation_count'] - d['close_count'])
    d['observation_manual'] = v['manual_observation_count'] is not None
    d['open_manual']        = v['manual_open_count']        is not None
    d['close_manual']       = v['manual_close_count']       is not None
    # 첨부 카운트
    ar = query('SELECT COUNT(*) AS n FROM vt_attachments WHERE vetting_id=?',
               (vid,), one=True)
    d['attach_count'] = ar['n'] if ar else 0
    return d


def _vetting_display_order(rows):
    """선박 1척의 vetting 행 표시 순서 정본 (맨 앞 = 상단표시 기준 행).

    🔴 검사일 내림차순만 쓰면 **날짜 미입력 행이 항상 맨 밑**으로 밀린다. 새 Vetting 을
       추가해 'Next Plan'(계획된 다음 검사)으로 지정해도 검사일을 아직 모르면 목록 끝에
       숨어버려서, 정작 제일 먼저 봐야 할 계획을 못 본다(손유석 지시 2026-07-31).
       그래서 상태를 1순위로 두고 날짜는 그 안에서만 본다:
       ① 'Next Plan' 을 항상 위. 여러 개면 새로 만든 것(id 최신) 우선
          — `_vetting_pick` 의 latest 선정과 같은 규칙이라 rows[0] == latest 가 성립한다.
       ② 나머지(Report)는 기존대로 검사일 내림차순, 같은 날짜면 id 내림차순.
    """
    nexts  = [r for r in rows if (r.get('valid') or '') == 'Next Plan']
    others = [r for r in rows if (r.get('valid') or '') != 'Next Plan']
    nexts.sort(key=lambda r: r.get('id') or 0, reverse=True)
    others.sort(key=lambda r: ((r.get('inspection_date') or ''), r.get('id') or 0),
                reverse=True)
    return nexts + others


def _vetting_pick(vessel_id):
    """선박 1척의 vetting 중 (상단표시 기준 행, 전체) 를 고른다.

    🔴 이 선정 규칙은 **정본이 1곳이어야 한다** — 웹 프론트 `vt.js vettingDigest`,
       `/api/ext/vetting-digests`, 위젯이 서로 다른 숫자를 보여주면 형이 못 믿는다.
       'Next Plan'(계획된 다음 검사)이 있으면 검사일 미입력이어도 그것을 상단으로.
       여러 개면 새로 만든 것(id 최신) 우선.

    🔴 요약행의 모든 값(OBS/OPEN 포함)은 **상단행 그 자체**에서 나온다 — 손유석 지시
       2026-08-11("Next Plan 일 경우 해당 Next Plan 의 OBS 및 OPEN 숫자가 표시되게").
       상단이 Next Plan 일 때 OBS 만 직전 Report 에서 끌어오던 `obs_src` 폴백은 이때
       폐기했다. 상태는 계획인데 숫자는 지난 수검 것이라 한 줄 안에서 출처가 갈렸고,
       형이 화면에서 그걸 오독으로 지목했다. **되살리지 말 것** — 살리려면 요약행을
       두 줄로 나누는 설계부터 다시 받아야 한다.
    반환: (latest, enr). vetting 이 없으면 (None, []).
    """
    vts = query("SELECT * FROM vettings WHERE vessel_id=? "
                "ORDER BY inspection_date DESC, id DESC", (vessel_id,))
    if not vts:
        return None, []
    enr = _vetting_display_order([_vetting_with_counts(v) for v in vts])
    return enr[0], enr


# ----- Vettings (vessel별 그룹) -----

@app.route('/api/vettings', methods=['GET'])
@login_required
def api_vettings_list():
    """선박별 vetting 그룹 응답.
    Query: ?year=2026&supervisor_id=N
    응답: [ { vessel: {...}, vettings: [...with findings...] } ]
    """
    year = request.args.get('year', type=int)
    sup_id = request.args.get('supervisor_id', type=int)

    # 대상 선박: VLCC/AFRAMAX/LR/MR만
    placeholders = ','.join('?' * len(VETTING_TYPES))
    sql = f'SELECT v.* FROM vessels v WHERE v.active=1 AND v.vessel_type IN ({placeholders})'
    params = list(VETTING_TYPES)
    if sup_id:
        sql += ' AND EXISTS (SELECT 1 FROM supervisor_vessels sv WHERE sv.vessel_id=v.id AND sv.supervisor_id=?)'
        params.append(sup_id)
    sql += ' ORDER BY v.name'
    vessels = query(sql, tuple(params))

    # vetting 한번에
    # vetting 필터:
    #  - 검사일이 있는 것은 해당 연도와 일치할 때만
    #  - 검사일이 없는 것 (방금 + 새 Vetting 추가 한 빈 행)은 모든 연도에 항상 표시
    if year:
        vettings = query('SELECT * FROM vettings')
        vettings = [v for v in vettings
                    if (not v['inspection_date'])
                    or (v['inspection_date'].startswith(str(year)))]
    else:
        vettings = query('SELECT * FROM vettings')

    # findings 한번에
    vids = [v['id'] for v in vettings]
    findings_by_vid = {vid: [] for vid in vids}
    if vids:
        ph = ','.join('?' * len(vids))
        all_f = query(
            f'SELECT * FROM vt_findings WHERE vetting_id IN ({ph}) ORDER BY vetting_id, no',
            tuple(vids),
        )
        for f in all_f:
            findings_by_vid[f['vetting_id']].append(dict(f))

    by_vessel = {}
    for v in vettings:
        d = _vetting_with_counts(v)
        d['findings'] = findings_by_vid.get(v['id'], [])
        by_vessel.setdefault(v['vessel_id'], []).append(d)

    # 표시 순서 = _vetting_display_order 정본 ('Next Plan' 먼저, 그 다음 검사일 내림차순).
    # 웹 상세 테이블 행 순서와 iOS 앱의 대표행(vettings.first) 이 모두 이 순서를 그대로 쓴다.
    for vid in by_vessel:
        by_vessel[vid] = _vetting_display_order(by_vessel[vid])

    # 선박별 담당 감독 ID 매핑 (Daily 이슈 등록 시 필요)
    sv_map = {}
    if vessels:
        v_ids = [v['id'] for v in vessels]
        ph2 = ','.join('?' * len(v_ids))
        rows = query(
            f'SELECT vessel_id, supervisor_id FROM supervisor_vessels WHERE vessel_id IN ({ph2})',
            tuple(v_ids),
        )
        for r in rows:
            sv_map.setdefault(r['vessel_id'], []).append(r['supervisor_id'])

    # 선박별 last_updated (해당 선박의 모든 vettings 중 가장 최근 updated_at)
    last_by_vessel = {}
    for v in vettings:
        u = v['updated_at']
        if u and (v['vessel_id'] not in last_by_vessel or u > last_by_vessel[v['vessel_id']]):
            last_by_vessel[v['vessel_id']] = u

    out = []
    for ves in vessels:
        vd = dict(ves)
        vd['supervisor_ids'] = sv_map.get(ves['id'], [])
        out.append({
            'vessel': vd,
            'vettings': by_vessel.get(ves['id'], []),
            'last_updated': last_by_vessel.get(ves['id']),
        })
    return jsonify(out)


@app.route('/api/vettings', methods=['POST'])
@login_required
def api_vetting_create():
    """단일 vetting 생성. 선박 ID만 필수, 나머지는 선택."""
    d = request.get_json() or {}
    vid = d.get('vessel_id')
    if not vid:
        return jsonify({'error': 'vessel_id 가 필요합니다.'}), 400
    v = query('SELECT vessel_type FROM vessels WHERE id=?', (vid,), one=True)
    if not v:
        return jsonify({'error': '선박을 찾을 수 없습니다.'}), 404
    if v['vessel_type'] not in VETTING_TYPES:
        return jsonify({'error': f'Vetting은 {", ".join(VETTING_TYPES)} 선박에만 적용됩니다.'}), 400

    st = d.get('sire_type') or None
    if st and st not in ('Idle', 'Bunkering', 'Discharge'):
        st = None
    valid = d.get('valid') or None
    if valid and valid not in ('Next Plan', 'Last Result'):
        valid = None

    new_id = execute("""
        INSERT INTO vettings
            (vessel_id, report_number, inspection_date, inspection_company,
             inspector, port, sire_type, valid, overall_remark, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vid,
          d.get('report_number') or '',
          d.get('inspection_date') or None,
          d.get('inspection_company') or '',
          d.get('inspector') or '',
          d.get('port') or '',
          st,
          valid,
          d.get('overall_remark') or '',
          session.get('username')))
    row = query('SELECT * FROM vettings WHERE id=?', (new_id,), one=True)
    return jsonify(_vetting_with_counts(row)), 201


@app.route('/api/vettings/<int:vid>', methods=['GET'])
@login_required
def api_vetting_get(vid):
    v = query('SELECT * FROM vettings WHERE id=?', (vid,), one=True)
    if not v:
        abort(404)
    d = _vetting_with_counts(v)
    d['findings'] = [dict(f) for f in query(
        'SELECT * FROM vt_findings WHERE vetting_id=? ORDER BY no', (vid,))]
    return jsonify(d)


@app.route('/api/vettings/<int:vid>', methods=['PUT'])
@login_required
def api_vetting_update(vid):
    if not query('SELECT id FROM vettings WHERE id=?', (vid,), one=True):
        abort(404)
    d = request.get_json() or {}
    sets, params = [], []
    for f in ('report_number','inspection_date','inspection_company','inspector',
              'port','sire_type','valid','overall_remark',
              'manual_observation_count','manual_open_count','manual_close_count'):
        if f in d:
            sets.append(f'{f} = ?')
            v = d[f]
            params.append(None if v == '' else v)
    if not sets:
        return jsonify({'ok': True})
    sets.append("updated_at = datetime('now','localtime')")
    execute(f'UPDATE vettings SET {", ".join(sets)} WHERE id=?', tuple(params + [vid]))
    return jsonify({'ok': True})


@app.route('/api/vettings/<int:vid>', methods=['DELETE'])
@login_required
def api_vetting_delete(vid):
    # 첨부 파일도 같이 삭제 (CASCADE는 DB만, 파일은 직접)
    atts = query('SELECT stored_name FROM vt_attachments WHERE vetting_id=?', (vid,))
    for a in atts:
        p = os.path.join(UPLOAD_DIR, a['stored_name'])
        if os.path.exists(p):
            try: os.remove(p)
            except OSError as e:
                app.logger.warning('vetting-delete: %s', e)
    execute('DELETE FROM vettings WHERE id=?', (vid,))
    return jsonify({'ok': True})


# ----- Findings -----

def _vt_next_no(vid):
    r = query('SELECT COALESCE(MAX(no), 0) + 1 AS next FROM vt_findings WHERE vetting_id=?',
              (vid,), one=True)
    return r['next']


@app.route('/api/vettings/<int:vid>/findings', methods=['POST'])
@login_required
def api_vt_findings_create(vid):
    """단건 또는 배치(items 배열) 생성."""
    if not query('SELECT id FROM vettings WHERE id=?', (vid,), one=True):
        abort(404)
    d = request.get_json() or {}
    items = d.get('items')
    if items is None:
        items = [{
            'item':        d.get('item'),
            'description': d.get('description'),
            'remark':      d.get('remark'),
            'user_remark': d.get('user_remark'),
            'status':      d.get('status') or 'Open',
        }]

    next_no = _vt_next_no(vid)
    created = []
    for it in items:
        st = it.get('status') or 'Open'
        if st not in ('Open','Closed'): st = 'Open'
        fid = execute("""
            INSERT INTO vt_findings (vetting_id, no, item, description, remark, user_remark, priority, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (vid, next_no,
              it.get('item') or '',
              it.get('description') or '',
              it.get('remark') or '',
              it.get('user_remark') or '',
              1 if it.get('priority') else 0,
              st))
        created.append(fid)
        next_no += 1
    return jsonify({'ids': created, 'count': len(created)}), 201


@app.route('/api/vt-findings/<int:fid>', methods=['PUT'])
@login_required
def api_vt_finding_update(fid):
    cur = query('SELECT vetting_id, status FROM vt_findings WHERE id=?', (fid,), one=True)
    if not cur:
        abort(404)
    d = request.get_json() or {}
    sets, params = [], []
    for f in ('item','description','remark','user_remark','status'):
        if f in d:
            sets.append(f'{f} = ?')
            params.append(d[f] or '')
    if 'priority' in d:
        sets.append('priority = ?')
        params.append(1 if d.get('priority') else 0)
    if not sets:
        return jsonify({'ok': True})
    sets.append("updated_at = datetime('now','localtime')")
    execute(f'UPDATE vt_findings SET {", ".join(sets)} WHERE id=?', tuple(params + [fid]))

    # status 변경 시 vettings.updated_at 갱신 (선박 헤더의 Last update에 반영)
    if 'status' in d and d['status'] != cur['status']:
        execute(
            "UPDATE vettings SET updated_at = datetime('now','localtime') WHERE id=?",
            (cur['vetting_id'],),
        )
    return jsonify({'ok': True})


@app.route('/api/vt-findings/<int:fid>', methods=['DELETE'])
@login_required
def api_vt_finding_delete(fid):
    f = query('SELECT vetting_id FROM vt_findings WHERE id=?', (fid,), one=True)
    if not f:
        abort(404)
    vid = f['vetting_id']
    execute('DELETE FROM vt_findings WHERE id=?', (fid,))
    # No 재정렬
    rows = query('SELECT id FROM vt_findings WHERE vetting_id=? ORDER BY no', (vid,))
    for new_no, r in enumerate(rows, start=1):
        execute('UPDATE vt_findings SET no=? WHERE id=?', (new_no, r['id']))
    return jsonify({'ok': True})


# ----- Attachments -----

def _vetting_full_prompt():
    return (
        "다음은 선박 SIRE 2.0 점검(Vetting Inspection) 보고서다. 두 가지를 추출해 지정한 JSON으로만 답하라.\n"
        "■ meta: 보고서 표지/상단의 점검 메타정보. 보고서에 해당 정보가 없으면 반드시 빈 문자열로 둔다(지어내지 말 것).\n"
        "- report_number: Report No / Report # / 보고서 번호\n"
        "- inspection_date: 점검 실시일 (반드시 YYYY-MM-DD 형식. 다른 형식이면 YYYY-MM-DD로 변환)\n"
        "- inspection_company: 점검 주체 / Oil Major / 제출사 (예: VIVA ENERGY, BP, SHELL, TOTAL)\n"
        "- inspector: 점검관(Inspector) 성명\n"
        "- port: 점검 항구명만 추출한다(도시/항구 이름). 국가명·UNLOCODE 코드(예: [SGSIN])·중복 표기는 제거. "
        "예: 'Singapore - Singapore [SGSIN]' → 'Singapore', 'Fujairah - UAE [AEFJR]' → 'Fujairah'.\n"
        "- sire_type: 점검 시 운항 상태. 반드시 'Idle' · 'Bunkering' · 'Discharge' 중 하나로만. 식별 불가 시 빈 문자열.\n"
        "■ items: 지적(결함) 사항만 추출한다.\n"
        "■ 포함: 'Observable or detectable deficiency' / 'Not as expected'로 표시된 부정적 지적(빨간 글씨). "
        "또한 'Photograph' 분류의 지적(예: 'Photo not representative', 'Photograph supplied: ...' 아래 빨간 이탤릭 설명)처럼 "
        "사진 증빙이 부적절·불일치하다는 지적도 반드시 포함한다.\n"
        "■ 제외: 'Exceeded normal expectation' 등 칭찬/긍정 평가(초록 글씨)는 절대 포함하지 마라.\n"
        "- item: 항목 왼쪽에 표시된 분류 라벨을 괄호로 먼저 붙이고, 그 뒤 굵게 표시된 지적 제목을 그대로 이어 붙인다. "
        "분류 라벨은 보고서에 나온 그대로 쓴다 — Hardware · Human · Photograph · Process · Other 등 무엇이든. "
        "예: '(Hardware)Misc Nautical Equipment – Maintenance deferred', '(Human)Senior Engineer Officer – Not as expected', "
        "'(Photograph)Photo not representative'.\n"
        "- description: 제목 아래 상세 본문(설명/이탤릭 문장 포함)을 영어 원문 그대로 복사. 요약·변형 금지.\n"
        "- remark: description의 핵심 지적사항을 한국어 1~2문장으로 간결하게 요약(전체 직역 금지). 문장은 '~함/~됨/~음' 음슴체(개조식). "
        "기술 명칭·장비명·약어(예: ECDIS, DCP, DRS, smoke detector, high-high level alarm 등)는 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
        "없는 내용을 지어내지 말 것. 지적이 하나도 없으면 items를 빈 배열로.\n"
        '형식: {"meta":{"report_number":"","inspection_date":"","inspection_company":"","inspector":"",'
        '"port":"","sire_type":""},"items":[{"item":"","description":"","remark":""}]}'
    )


def _clean_port(p):
    """'Singapore - Singapore [SGSIN]' → 'Singapore'. 국가/코드/중복 제거, 항구명만."""
    s = (p or '').strip()
    if not s:
        return ''
    s = _re_cls.sub(r'\[[^\]]*\]', '', s)      # [SGSIN] 등 코드 제거
    s = s.split(' - ')[0]                       # ' - ' 앞 항구명만
    s = s.split(' / ')[0].split('/')[0]         # '/' 구분도 첫 토큰
    s = _re_cls.sub(r'\s+', ' ', s).strip(' -,')
    return s


def _norm_vetting_meta(m):
    m = m if isinstance(m, dict) else {}
    g = lambda k: (m.get(k) or '').strip()
    sire = g('sire_type')
    return {
        'report_number':      g('report_number'),
        'inspection_date':    g('inspection_date'),
        'inspection_company': g('inspection_company'),
        'inspector':          g('inspector'),
        'port':               _clean_port(g('port')),
        'sire_type':          sire if sire in ('Idle', 'Bunkering', 'Discharge') else '',
        'valid':              '',   # '상태'(Next Plan/Last Result)는 수동 입력 — 보고서에서 추출하지 않음
    }


def _extract_vetting_from_upload(f):
    """SIRE 보고서 업로드 → (items, meta, err). 헤더 메타 + 지적 항목을 한 번에 추출."""
    name = (f.filename or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    raw = f.read()
    size_mb = len(raw) / (1024 * 1024)
    prompt = _vetting_full_prompt()

    if ext == 'pdf':
        if size_mb > 15:
            return None, None, {'reason': 'TOO_LARGE', 'message': f'PDF가 너무 큽니다({size_mb:.1f}MB). 15MB 이하로 줄여주세요.'}
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': 'application/pdf', 'data': b64}},
            {'text': prompt},
        ], model=_model_for('findings'))
    elif ext in ('png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'):
        if size_mb > 15:
            return None, None, {'reason': 'TOO_LARGE', 'message': f'이미지가 너무 큽니다({size_mb:.1f}MB).'}
        import mimetypes
        media = mimetypes.guess_type(name)[0] or 'image/jpeg'
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': media, 'data': b64}},
            {'text': prompt},
        ], model=_model_for('findings'))
    elif ext in ('xlsx', 'xls'):
        try:
            txt = _xlsx_to_text(raw)
        except Exception as e:
            app.logger.exception('extract-vetting-from-upload')
            return None, None, {'reason': 'XLSX_PARSE_FAILED', 'message': f'엑셀을 읽지 못했습니다: {e}'}
        parsed = _gemini_call_json([{'text': prompt + '\n\n[보고서 표 내용]\n' + txt}],
                                   model=_model_for('findings'))
    else:
        return None, None, {'reason': 'BAD_TYPE', 'message': 'PDF · 이미지 · 엑셀(xlsx) 파일만 지원합니다.'}

    if isinstance(parsed, dict) and parsed.get('error') == 'NO_API_KEY':
        return None, None, {'reason': 'no_api_key', 'message': 'AI 자동추출이 설정되지 않았습니다(키 미설정).'}
    if isinstance(parsed, dict) and parsed.get('error'):
        return None, None, {'reason': parsed['error'], 'message': '자동 추출에 실패했습니다.',
                            'detail': parsed.get('detail') or parsed.get('raw')}
    items = _normalize_findings(parsed, 'sire')
    meta = _norm_vetting_meta(parsed.get('meta') if isinstance(parsed, dict) else None)
    return items, meta, None


@app.route('/api/vettings/<int:vid>/extract-report', methods=['POST'])
@login_required
def api_vt_extract_report(vid):
    if not query('SELECT id FROM vettings WHERE id=?', (vid,), one=True):
        abort(404)
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'ok': False, 'message': '파일이 없습니다.'}), 400
    items, meta, err = _extract_vetting_from_upload(request.files['file'])
    if err:
        return jsonify({'ok': False, **err}), 200
    # 헤더 메타 자동 반영: 추출값이 있는 필드만 갱신 (없으면 기존값 유지)
    applied = {}
    sets, params = [], []
    for col in ('report_number', 'inspection_date', 'inspection_company',
                'inspector', 'port', 'sire_type', 'valid'):
        val = (meta or {}).get(col, '')
        if val:
            sets.append(f'{col}=?'); params.append(val); applied[col] = val
    if sets:
        sets.append("updated_at=datetime('now','localtime')")
        params.append(vid)
        execute(f'UPDATE vettings SET {", ".join(sets)} WHERE id=?', tuple(params))
    return jsonify({'ok': True, 'items': items, 'count': len(items),
                    'meta': meta, 'applied': applied})


def _md_from_date(d):
    """'2026-04-30' → '4/30'. 파싱 실패 시 원문."""
    try:
        y, m, dd = (d or '').split('-')
        return f'{int(m)}/{int(dd)}'
    except Exception:
        app.logger.exception('md-from-date')
        return (d or '').strip()


def _company_abbr(c):
    """'VIVA ENERGY' → 'VIVA' (첫 토큰 대문자). 빈 값이면 ''."""
    c = (c or '').strip()
    if not c:
        return ''
    return c.split()[0].upper()


def _sire_abbr(s):
    return {'Bunkering': 'BUNKER', 'Discharge': 'DISCHARGE', 'Idle': 'IDLE'}.get(
        (s or '').strip(), (s or '').strip().upper())


def _condense_obs(items):
    """[{i,summary,description,user_remark}] → {i: short}. 선박 약어체 한 줄.
    GEMINI 키 없거나 실패 시 빈 dict (상위에서 번역요약으로 폴백)."""
    out = {}
    if not GEMINI_API_KEY or not items:
        return out
    payload = json.dumps([{'i': it['i'], 'summary': it.get('summary', ''),
                           'description': it.get('description', '')} for it in items],
                         ensure_ascii=False)
    prompt = (
        "아래는 선박 SIRE 점검 지적 항목들이다(JSON 배열). 각 항목의 핵심 결함을 "
        "선박 현업 약어체로 아주 짧게 한 줄로 요약하라.\n"
        "- 장비명은 선박 약어로 대문자 표기: Cargo Oil Tank→COT, Ballast Water Treatment System→BWTS, "
        "Main Engine→M/E, Auxiliary Engine→A/E, pressure→PRESS., No.3 Port→3P, Vapour return manifold→VAP. RETURN MANIFOLD 등.\n"
        "- 결함은 '불량/파손/누설/마모/고장' 등 한 단어로 압축. 군더더기·서술 제거.\n"
        "- 예: 'Cargo tank high level alarm display 결함으로 상시 점등됨' → 'COT HIGH LEVEL ALARM DISPLAY 불량', "
        "'3 Port cargo tank 압력 센서 결함' → '3P COT PRESS. SENSOR 불량'.\n"
        + _MARITIME_TERMS +
        "입력의 i를 그대로 사용해 JSON으로만 답하라.\n"
        '형식: {"items":[{"i":0,"short":"..."}]}\n\n[입력]\n' + payload)
    res = _gemini_call_json([{'text': prompt}], model=_model_for('summary'))
    arr = _coerce_translation_items(res)
    for o in (arr or []):
        if not isinstance(o, dict):
            continue
        try:
            i = int(o.get('i'))
        except (TypeError, ValueError):
            continue
        sh = (o.get('short') or o.get('en') or '').strip()
        if sh:
            out[i] = sh
    return out


@app.route('/api/vettings/<int:vid>/obs-summary', methods=['POST'])
@login_required
def api_vt_obs_summary(vid):
    """Priority 체크 + Open 항목 기준으로 '지적 상세' 요약을 생성해 overall_remark에 기록."""
    v = query('SELECT * FROM vettings WHERE id=?', (vid,), one=True)
    if not v:
        abort(404)
    findings = query('SELECT * FROM vt_findings WHERE vetting_id=? ORDER BY no, id', (vid,))
    open_f = [f for f in findings if (f['status'] or 'Open') == 'Open']
    def _is_prio(f):
        try:
            return bool(f['priority'])
        except (KeyError, IndexError):
            return False
    prio = [f for f in open_f if _is_prio(f)]
    total_open = len(open_f)
    minor = total_open - len(prio)

    header_bits = [b for b in (_md_from_date(v['inspection_date']),
                               _company_abbr(v['inspection_company']),
                               _sire_abbr(v['sire_type'])) if b]
    header = (' '.join(header_bits) + ' ' if header_bits else '') + \
             f'SIRE OBS 잔여 {total_open}건 조치 중'

    shorts = _condense_obs([
        {'i': i, 'summary': f['remark'] or '', 'description': f['description'] or '',
         'user_remark': f['user_remark'] or ''}
        for i, f in enumerate(prio)
    ])

    lines = [header]
    for i, f in enumerate(prio):
        short = shorts.get(i) or (f['remark'] or f['item'] or '').strip()
        ur = (f['user_remark'] or '').strip()
        lines.append(f'{i + 1}. {short}' + (f' - {ur}' if ur else ''))
    if minor > 0:
        lines.append(f'그 외 Minor 지적 {minor}건')
    text = '\n'.join(lines)

    execute("UPDATE vettings SET overall_remark=?, updated_at=datetime('now','localtime') WHERE id=?",
            (text, vid))
    return jsonify({'ok': True, 'summary': text,
                    'total_open': total_open, 'priority_open': len(prio), 'minor': minor})


@app.route('/api/vettings/<int:vid>/export')
@login_required
def api_vt_export(vid):
    from flask import send_file
    v = query('''SELECT vt.*, ve.name AS vessel_name
                   FROM vettings vt JOIN vessels ve ON ve.id = vt.vessel_id
                  WHERE vt.id=?''', (vid,), one=True)
    if not v:
        abort(404)
    fr = query('''SELECT no, item, description, remark, user_remark, status
                    FROM vt_findings WHERE vetting_id=? ORDER BY no, id''', (vid,))
    # RECTIFICATION·PHOTO 2열은 공란으로 출력(현장기입용). 번역요약·Remark는 export에서 제외.
    rows = [[r['no'], r['item'] or '', r['description'] or '',
             '', '', r['status'] or ''] for r in fr]
    vessel = v['vessel_name']
    rno = v['report_number'] or ''
    title = f"SIRE Observation List — {vessel}"
    sub_bits = [f"검사일: {v['inspection_date'] or '-'}", f"Port: {v['port'] or '-'}"]
    if rno:
        sub_bits.append(f"Report: {rno}")
    sub_bits.append(f"총 {len(rows)}건")
    headers = ['No.', 'ITEM', 'DESCRIPTION', 'RECTIFICATION', 'PHOTO', 'STATUS']
    bio = _findings_workbook(title, '   │   '.join(sub_bits), headers, rows,
                             wrap_cols={2, 3, 4, 5}, widths=[6, 26, 46, 40, 30, 10])
    date_tag = (v['inspection_date'] or '').replace('-', '')
    fname = f"SIRE_{_safe_filename(vessel)}_{date_tag or vid}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/vettings/<int:vid>/attachments', methods=['GET'])
@login_required
def api_vt_attachments_list(vid):
    rows = query(
        'SELECT * FROM vt_attachments WHERE vetting_id=? ORDER BY id DESC',
        (vid,),
    )
    return jsonify([dict(r) for r in rows])


@app.route('/api/vettings/<int:vid>/attachments', methods=['POST'])
@login_required
def api_vt_attachment_upload(vid):
    if not query('SELECT id FROM vettings WHERE id=?', (vid,), one=True):
        abort(404)
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다.'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '파일명이 없습니다.'}), 400
    if not _ext_allowed(f.filename):
        return jsonify({'error': '허용되지 않는 파일 형식입니다.'}), 400

    ext = os.path.splitext(f.filename)[1]
    stored = f"vt_{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(UPLOAD_DIR, stored)
    f.save(save_path)
    size = os.path.getsize(save_path)

    aid = execute("""
        INSERT INTO vt_attachments
            (vetting_id, filename, stored_name, file_size, mime_type, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (vid, f.filename, stored, size, f.mimetype, session.get('username')))
    return jsonify({'id': aid, 'filename': f.filename, 'file_size': size}), 201


@app.route('/api/vt-attachments/<int:aid>', methods=['GET'])
@login_required
def api_vt_attachment_get(aid):
    a = query('SELECT * FROM vt_attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    inline = request.args.get('inline')
    return send_from_directory(
        UPLOAD_DIR, a['stored_name'],
        as_attachment=not inline,
        download_name=a['filename'],
    )


@app.route('/api/vt-attachments/<int:aid>', methods=['DELETE'])
@login_required
def api_vt_attachment_delete(aid):
    a = query('SELECT * FROM vt_attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    p = os.path.join(UPLOAD_DIR, a['stored_name'])
    if os.path.exists(p):
        try: os.remove(p)
        except OSError:
            app.logger.exception('vt-attachment-delete')
    execute('DELETE FROM vt_attachments WHERE id=?', (aid,))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  API — Calendar Events (일정 모듈)
# ═════════════════════════════════════════════════════════════════
CAL_VALID_COLORS = ('gray','red','amber','yellow','green','blue','purple','pink')

