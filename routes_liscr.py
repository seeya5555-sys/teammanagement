"""routes_liscr — 기국(LISCR) 인보이스 PDF 업로드 → SVMS 신규 인보이스 생성(Case 2).

기존 `/invoice` 탭(invoice_draft)과 헷갈리지 말 것:
  · `/invoice`  = 이미 SVMS에 있는 인보이스를 컨펌하는 자동화 (Case 1). 이 모듈은 거기 손대지 않는다.
  · `/liscr`    = SVMS에 아직 없는 인보이스를 PDF 한 장으로 만들어내는 자동화 (Case 2).
생성이 끝나면 SVMS 상태가 'S'(상신)이 되고, 그 순간부터 Case 1 러너가 그 인보이스를
평소처럼 집어간다. 두 파이프라인은 코드로 엮이지 않고 SVMS 상태로만 이어진다.

왜 서버가 SVMS를 직접 부르지 않는가:
  SVMS 자격증명은 맥 로컬(`~/.openclaw/secrets/`)에만 두는 게 확정 방침이고, 인보이스
  생성은 금전 행위다. 그래서 이 서버는 **큐와 화면만** 갖고, 실제 SVMS 쓰기는 맥 러너가
  `/api/ext/liscr/*` 로 claim 해서 수행한다. 서버가 털려도 SVMS 쓰기 권한은 새지 않는다.

사람 승인 게이트:
  파싱(러너) → 사람이 화면에서 값 확인·수정 → [승인] → 그때서야 러너가 SVMS에 만든다.
  승인 없이는 어떤 인보이스도 생성되지 않는다.
"""
from flask import Blueprint

import hashlib
import json
import os
import re
import uuid

from flask import abort, jsonify, render_template, request, send_file, session

from app_core import LISCR_PDF_DIR, app, execute, execute_rc, query
from helpers_shared import admin_required, api_key_required

bp = Blueprint('routes_liscr', __name__)

# 러너가 claim 한 뒤 죽어버린 건을 되살리는 lease. 파싱은 읽기라 짧게 회수해도 안전하다.
LISCR_PARSE_LEASE_SEC = 600
# 🔴 creating(=SVMS 쓰기 진행중)은 자동 회수하지 않는다. 러너가 SVMS 저장을 마치고 결과를
#    돌려주기 직전에 죽으면, 회수해서 재시도하는 순간 인보이스가 두 번 만들어진다.
#    금전 행위는 미탐(사람이 손으로 확인)이 중복실행보다 낫다. 그래서 lease 자체를 두지 않는다.

LISCR_MAX_BYTES = 20 * 1024 * 1024

# 화면에서 사람이 고칠 수 있는 필드 → (컬럼, 헤더키). 여기 없는 필드는 어떤 요청이 와도 안 바뀐다.
# 🔴 `inv_user_id`(Invoice PIC) 는 일부러 뺐다 — 형 지시로 SS0059 고정이고, 화면에서 고칠 수
#    있게 두면 "고정"이 사실상 기본값으로 내려앉는다. Vendor·Expense·Oversea 도 같은 이유로 없다.
_EDITABLE = {
    'inv_no':      ('inv_no', 'INV_NO'),
    'inv_dt':      ('inv_dt', 'INV_DT'),
    'amt':         ('amt', 'AMT'),
    'pay_dt':      ('pay_dt', 'PAY_DT'),
    'subject':     ('subject', None),      # 라인 적요 — 헤더가 아니라 lines[0].SUBJ 로 간다
    'sup_user_id': ('sup_user_id', 'SUP_USER_ID'),   # 담당 감독(사람) — 과거 인보이스에서 추정하므로 수정 여지를 남긴다
}

# 금액 형식 — 부호 없는 십진수, 소수 2자리까지. float() 만 쓰면 'nan'/'inf' 가 통과해
# 금액칸에 NaN 이 실린다(비교·합계가 전부 조용히 무너짐).
_AMT_RE = re.compile(r'^\d{1,15}(\.\d{1,2})?$')


def _liscr_pdf_path(jid):
    """업로드 PDF 경로. 파일명이 id 고정이라 경로주입이 성립하지 않는다."""
    return os.path.join(LISCR_PDF_DIR, '%d.pdf' % int(jid))


def _job_dict(r):
    d = dict(r)
    for k in ('reasons', 'header_json', 'lines_json', 'parsed_json', 'edited_json'):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = None
    d['has_pdf'] = os.path.exists(_liscr_pdf_path(d['id']))
    return d


# ───────────────────────────── 사람용 화면/API ─────────────────────────────

@bp.route('/liscr')
@admin_required
def liscr_page():
    return render_template('liscr.html')


@bp.route('/api/liscr/jobs')
@admin_required
def api_liscr_jobs():
    rows = query("SELECT * FROM liscr_job ORDER BY CASE status "
                 "WHEN 'parsed' THEN 0 WHEN 'hold' THEN 1 WHEN 'queued' THEN 2 "
                 "WHEN 'parsing' THEN 3 WHEN 'approved' THEN 4 WHEN 'creating' THEN 5 "
                 "ELSE 6 END, id DESC LIMIT 200")
    waiting = query("SELECT COUNT(*) c FROM liscr_job WHERE status='parsed'", one=True)
    return jsonify({'jobs': [_job_dict(r) for r in rows], 'waiting': waiting['c']})


@bp.route('/api/liscr/upload', methods=['POST'])
@admin_required
def api_liscr_upload():
    """PDF 업로드 → 큐 적재. 여기서는 파싱도 SVMS 호출도 하지 않는다(맥 러너 담당)."""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': '파일이 없음'}), 400
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'PDF 파일만 업로드 가능'}), 400
    data = f.read(LISCR_MAX_BYTES + 1)
    if len(data) > LISCR_MAX_BYTES:
        return jsonify({'error': '파일이 20MB를 넘음'}), 400
    if not data.startswith(b'%PDF'):
        return jsonify({'error': 'PDF 형식이 아님'}), 400
    sha = hashlib.sha256(data).hexdigest()

    # 같은 PDF가 아직 처리중이면 새 잡을 만들지 않는다(같은 인보이스 두 번 생성 방지의 1차선).
    # IN 목록은 리터럴로 적는다 — 상태 집합은 이 파일이 정하는 고정값이고, 문자열로 조립하면
    # `test_sql_construction_contract` 의 검토 대상이 되기 때문이다.
    dup = query("SELECT id, status FROM liscr_job WHERE sha256=? AND status IN "
                "('uploading','queued','parsing','parsed','hold','approved','creating') "
                "ORDER BY id DESC LIMIT 1", (sha,), one=True)
    if dup:
        return jsonify({'error': '같은 PDF가 이미 처리중임 (#%d, %s)' % (dup['id'], dup['status']),
                        'dup_id': dup['id']}), 409
    created = query("SELECT id, inv_cd FROM liscr_job WHERE sha256=? AND status='created' "
                    "ORDER BY id DESC LIMIT 1", (sha,), one=True)
    if created:
        return jsonify({'error': '같은 PDF로 이미 SVMS 인보이스를 만들었음 (%s)' % created['inv_cd'],
                        'dup_id': created['id']}), 409

    # 🔴 'uploading' 으로 넣고 파일을 쓴 뒤에 'queued' 로 올린다. 곧바로 'queued' 로 넣으면
    #    러너가 파일이 다 쓰이기 전에 claim 해 반쯤 쓰인 PDF 를 파싱할 수 있다.
    #    claim 은 'queued' 만 보므로, 이 순서가 파일 완성과 큐 진입을 묶어준다.
    jid = execute("INSERT INTO liscr_job (filename, sha256, status) VALUES (?,?,'uploading')",
                  (os.path.basename(f.filename)[:200], sha))
    try:
        with open(_liscr_pdf_path(jid), 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        app.logger.exception('liscr-pdf-save')
        execute("UPDATE liscr_job SET status='failed', error='PDF 저장 실패' WHERE id=?", (jid,))
        return jsonify({'error': 'PDF 저장 실패'}), 500
    execute("UPDATE liscr_job SET status='queued' WHERE id=? AND status='uploading'", (jid,))
    return jsonify({'id': jid, 'status': 'queued'})


@bp.route('/api/liscr/jobs/<int:jid>/pdf')
@admin_required
def api_liscr_pdf(jid):
    p = _liscr_pdf_path(jid)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype='application/pdf', as_attachment=False,
                     download_name='liscr_%d.pdf' % jid, conditional=True)


@bp.route('/api/liscr/jobs/<int:jid>/approve', methods=['POST'])
@admin_required
def api_liscr_approve(jid):
    """사람 승인 = SVMS 생성 허가. 화면에서 고친 값이 있으면 여기서 헤더/라인에 반영한다."""
    row = query("SELECT * FROM liscr_job WHERE id=?", (jid,), one=True)
    if not row:
        abort(404)
    if row['status'] != 'parsed':
        return jsonify({'error': "승인 가능한 상태가 아님 (현재 %s)" % row['status']}), 409
    if (row['gate'] or '') != 'READY':
        return jsonify({'error': 'HOLD 상태는 승인할 수 없음'}), 409

    d = request.get_json(silent=True) or {}
    header = json.loads(row['header_json'] or '{}')
    lines = json.loads(row['lines_json'] or '[]')
    edits, cols = {}, {}
    for key, (col, hkey) in _EDITABLE.items():
        if key not in d:
            continue
        val = d[key]
        if key == 'amt':
            if not _AMT_RE.match(str(val or '').strip()):
                return jsonify({'error': '금액 형식이 잘못됨 (숫자, 소수 2자리까지)'}), 400
            val = float(val)
            if val <= 0:
                return jsonify({'error': '금액은 0보다 커야 함'}), 400
        else:
            val = (str(val or '')).strip()
            if not val:
                return jsonify({'error': '%s 는 비울 수 없음' % key}), 400
        if val == row[col]:
            continue
        edits[key] = {'from': row[col], 'to': val}
        cols[col] = val
        if hkey:
            header[hkey] = val

    if 'subject' in cols and lines:
        lines[0]['SUBJ'] = cols['subject']
    if 'amt' in cols:
        # 금액을 고쳤는데 라인이 1줄이면 라인 금액도 같이 맞춘다. 2줄 이상이면 어느 줄을
        # 고쳐야 하는지 알 수 없으므로 건드리지 않고 거부한다(헤더-라인 합 불일치 방지).
        if len(lines) == 1:
            lines[0]['AMT'] = cols['amt']
        else:
            return jsonify({'error': '라인이 2줄 이상이라 금액 수정은 SVMS 화면에서 해야 함'}), 400

    # 컬럼 목록은 고정 SQL 로 적는다(수정 안 된 필드는 기존 값을 그대로 다시 쓴다).
    # 조립식 SET 절이면 편집 가능한 컬럼이 요청 내용에 따라 달라져 검토가 어려워진다.
    rc = execute_rc(
        "UPDATE liscr_job SET inv_no=?, inv_dt=?, amt=?, pay_dt=?, subject=?, "
        "sup_user_id=?, inv_user_id=?, header_json=?, lines_json=?, edited_json=?, "
        "status='approved', decided_at=datetime('now','localtime'), decided_by=? "
        "WHERE id=? AND status='parsed'",
        (cols.get('inv_no', row['inv_no']), cols.get('inv_dt', row['inv_dt']),
         cols.get('amt', row['amt']), cols.get('pay_dt', row['pay_dt']),
         cols.get('subject', row['subject']), cols.get('sup_user_id', row['sup_user_id']),
         cols.get('inv_user_id', row['inv_user_id']),
         json.dumps(header, ensure_ascii=False), json.dumps(lines, ensure_ascii=False),
         json.dumps(edits, ensure_ascii=False) if edits else None,
         session.get('user') or 'admin', jid))
    if not rc:
        return jsonify({'error': '다른 처리와 겹쳐 승인되지 않음'}), 409
    return jsonify({'id': jid, 'status': 'approved', 'edited': list(edits)})


@bp.route('/api/liscr/jobs/<int:jid>/reject', methods=['POST'])
@admin_required
def api_liscr_reject(jid):
    """사람이 취소. 아직 SVMS에 안 만든 것만 취소할 수 있다."""
    rc = execute_rc("UPDATE liscr_job SET status='rejected', decided_at=datetime('now','localtime'), "
                    "decided_by=? WHERE id=? AND status IN ('parsed','hold','queued')",
                    (session.get('user') or 'admin', jid))
    if not rc:
        return jsonify({'error': '취소 가능한 상태가 아님'}), 409
    return jsonify({'id': jid, 'status': 'rejected'})


@bp.route('/api/liscr/jobs/<int:jid>', methods=['DELETE'])
@admin_required
def api_liscr_delete(jid):
    """행 삭제.

    🔴 SVMS 에 인보이스가 생겼거나 생겼을 수 있는 행은 지우지 않는다. 이 행의 sha256 이
       "같은 PDF 재업로드" 를 막는 유일한 표식이라, 지우는 순간 같은 인보이스를 한 번 더
       만들 수 있게 된다. created 는 물론, inv_cd 가 남은 failed(=저장은 됐고 상신에서
       실패)도 같은 이유로 남긴다. 러너가 잡고 있는 상태도 당연히 금지.
    """
    rc = execute_rc("DELETE FROM liscr_job WHERE id=? AND inv_cd IS NULL AND status IN "
                    "('failed','rejected','hold','parsed')", (jid,))
    if not rc:
        return jsonify({'error': 'SVMS 인보이스가 생겼거나 처리중인 건은 삭제할 수 없음 '
                                 '(재업로드 중복생성 방지)'}), 409
    try:
        p = _liscr_pdf_path(jid)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        app.logger.exception('liscr-pdf-delete')
    return jsonify({'id': jid, 'deleted': True})


# ───────────────────────────── 맥 러너용 API ─────────────────────────────

@bp.route('/api/ext/liscr/pending')
@api_key_required
def api_ext_liscr_pending():
    """러너: 단계별 대기 건수(읽기 전용).

    🔴 이게 있어야 러너가 **claim 하기 전에** SVMS 로그인을 끝낼 수 있다. 로그인을 뒤로
       미루면 create 를 claim 해놓고 로그인에서 죽는 회차가 생기는데, create 단계는
       중복생성을 막으려고 lease 회수를 일부러 안 넣었으므로 그 잡이 'SVMS 생성 중'으로
       영구히 굳는다. 큐가 빌 땐 매 회차 로그인하지 않으려는 목적도 겸한다.
    """
    p = query("SELECT COUNT(*) c FROM liscr_job WHERE status='queued' OR (status='parsing' "
              "AND (claimed_at IS NULL OR claimed_at < datetime('now','localtime',?)))",
              ('-%d seconds' % LISCR_PARSE_LEASE_SEC,), one=True)
    c = query("SELECT COUNT(*) c FROM liscr_job WHERE status='approved'", one=True)
    return jsonify({'parse': p['c'], 'create': c['c']})


@bp.route('/api/ext/liscr/claim', methods=['POST'])
@api_key_required
def api_ext_liscr_claim():
    """러너: 처리할 잡 1건 claim.

    stage="parse"  → queued(또는 lease 만료된 parsing) 1건 → parsing
    stage="create" → approved 1건 → creating   (사람이 승인한 것만. lease 회수 없음)
    """
    stage = ((request.get_json(silent=True) or {}).get('stage') or '').strip()
    if stage == 'parse':
        row = query("SELECT id, status, claim_token FROM liscr_job WHERE status='queued' "
                    "OR (status='parsing' AND (claimed_at IS NULL OR claimed_at < "
                    "datetime('now','localtime',?))) ORDER BY id ASC LIMIT 1",
                    ('-%d seconds' % LISCR_PARSE_LEASE_SEC,), one=True)
        nxt = 'parsing'
    elif stage == 'create':
        row = query("SELECT id, status, claim_token FROM liscr_job WHERE status='approved' "
                    "ORDER BY id ASC LIMIT 1", one=True)
        nxt = 'creating'
    else:
        return jsonify({'error': 'stage 는 parse 또는 create'}), 400
    if not row:
        return jsonify({'job': None})

    token = uuid.uuid4().hex
    # CAS — 두 러너가 동시에 붙어도 한쪽만 이긴다. 토큰 비교까지 넣어 회수 경합도 막는다.
    rc = execute_rc("UPDATE liscr_job SET status=?, claim_token=?, "
                    "claimed_at=datetime('now','localtime') WHERE id=? AND status=? "
                    "AND ((claim_token IS ?) OR (claim_token = ?))",
                    (nxt, token, row['id'], row['status'], row['claim_token'], row['claim_token']))
    if not rc:
        return jsonify({'job': None})
    r = query("SELECT * FROM liscr_job WHERE id=?", (row['id'],), one=True)
    job = _job_dict(r)
    job['claim_token'] = token
    return jsonify({'job': job})


@bp.route('/api/ext/liscr/jobs/<int:jid>/pdf')
@api_key_required
def api_ext_liscr_pdf(jid):
    """러너: 원본 PDF 다운로드(파싱 + SVMS 첨부용)."""
    p = _liscr_pdf_path(jid)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype='application/pdf', as_attachment=True,
                     download_name='liscr_%d.pdf' % jid, conditional=True)


@bp.route('/api/ext/liscr/jobs/<int:jid>/parsed', methods=['POST'])
@api_key_required
def api_ext_liscr_parsed(jid):
    """러너: 파싱 결과 적재. gate=READY 면 사람 승인 대기(parsed), 아니면 hold."""
    d = request.get_json(silent=True) or {}
    token = (d.get('claim_token') or '').strip()
    gate = 'READY' if d.get('gate') == 'READY' else 'HOLD'
    header = d.get('header') or {}
    lines = d.get('lines') or []
    # READY = "사람이 승인만 하면 SVMS 에 그대로 쓴다"는 뜻이다. 라인이 없으면 승인 화면에
    # 금액/적요가 안 뜨고 생성 단계에서야 터진다 — 여기서 HOLD 로 떨어뜨리는 게 맞다.
    if gate == 'READY' and (not header or not lines):
        return jsonify({'error': 'READY 인데 header 또는 lines 가 비었음'}), 400
    line0 = lines[0] if lines else {}
    rc = execute_rc(
        "UPDATE liscr_job SET status=?, gate=?, reasons=?, vsl_cd=?, vsl_nm=?, inv_no=?, inv_dt=?, "
        "cur_cd=?, amt=?, pay_dt=?, exp_cd=?, subject=?, sup_user_id=?, sup_user_nm=?, "
        "inv_user_id=?, oversea_tp=?, header_json=?, lines_json=?, parsed_json=?, error=NULL "
        "WHERE id=? AND status='parsing' AND claim_token=?",
        ('parsed' if gate == 'READY' else 'hold', gate,
         json.dumps(d.get('reasons') or [], ensure_ascii=False),
         header.get('VSL_CD'), header.get('VSL_NM'), header.get('INV_NO'), header.get('INV_DT'),
         header.get('CUR_CD'), header.get('AMT'), header.get('PAY_DT'),
         line0.get('EXP_CD'), line0.get('SUBJ'),
         header.get('SUP_USER_ID'), header.get('SUP_USER_NM'), header.get('INV_USER_ID'),
         header.get('OVERSEA_TP'),
         json.dumps(header, ensure_ascii=False), json.dumps(lines, ensure_ascii=False),
         json.dumps(d.get('parsed') or {}, ensure_ascii=False), jid, token))
    if not rc:
        return jsonify({'error': 'claim 불일치 또는 상태 변경됨'}), 409
    return jsonify({'id': jid, 'gate': gate})


@bp.route('/api/ext/liscr/jobs/<int:jid>/result', methods=['POST'])
@api_key_required
def api_ext_liscr_result(jid):
    """러너: SVMS 생성 결과 반영.

    🔴 inv_cd 가 왔으면 ok 여부와 무관하게 반드시 기록한다. 저장은 됐는데 상신에서
       실패한 경우 inv_cd 를 버리면 SVMS에 주인 없는 인보이스가 남고, 사람이 그걸
       찾을 단서가 사라진다.
    """
    d = request.get_json(silent=True) or {}
    token = (d.get('claim_token') or '').strip()
    inv_cd = (d.get('inv_cd') or '').strip() or None
    ok = bool(d.get('ok'))
    err = (d.get('error') or '알 수 없는 실패')
    # 🔴 ok=true 인데 INV_CD 가 없으면 '생성 완료'로 찍지 않는다. 화면상 초록으로 끝나버리면
    #    아무도 SVMS 를 확인하지 않는데, 실제로는 인보이스가 생겼는지조차 모르는 상태다.
    if ok and not inv_cd:
        ok, err = False, 'SVMS 가 INV_CD 를 주지 않음 — 생성 여부 직접 확인 필요'
    rc = execute_rc("UPDATE liscr_job SET status=?, inv_cd=?, error=?, "
                    "done_at=datetime('now','localtime') WHERE id=? AND status='creating' "
                    "AND claim_token=?",
                    ('created' if ok else 'failed', inv_cd,
                     None if ok else err[:2000], jid, token))
    if not rc:
        return jsonify({'error': 'claim 불일치 또는 상태 변경됨'}), 409
    return jsonify({'id': jid, 'ok': ok, 'inv_cd': inv_cd})
