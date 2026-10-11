"""routes_rpcost — SVMS 수리비용·구매비용 컨펌 2단게이트 (`/rpcost`, 인보이스탭 미러).

흐름(인보이스탭과 같음):
  · 맥 prep 엔진(automation/svms-repair-purchase/ingest.py)이 카드 적재 — POST /api/ext/rpcost/drafts
  · 사람이 /rpcost 탭에서 opt-out 승인(approved) / 리젝(TRMT 표시만) / 수리 카드 INV_DT·EXP_CD 교정
  · [일괄승인+컨펌] → `rpcost_confirm` live → 맥 confirm.py 가 approved claim → SVMS 재조회·검증·저장 → result

범위: SS0094 My Vessel 만. 한 표(rp_cost_draft)에 kind=repair|purchase, dedup 키 = kind + svms_key
  (수리 = INV_CD, 구매 = SVMS INV_NO 키 예 `…AA1`).

🔴 v1 리젝은 **TRMT 표시만**이다. SVMS 에는 아무것도 보내지 않는다(SVMS 반려는 사람이 SVMS 에서 직접).
🔴 수리 컨펌(SP_SET_INV CU)은 러너가 RP_CONFIRM_LIVE=1 AND RP_REPAIR_LIVE_ENABLED=1 일 때만 저장(골든 캡처 반영).
🔴 result 는 claim_token 일치 시에만 반영. SVMS 에는 서버측 CAS 가 없어 러너가 저장 직전 재조회로만 막는다(잔여위험).
🔴 EXP_CD 제안(suggest)은 표시·사전입력용일 뿐 gate 를 PASS 로 올리지 않는다. 사람 편집도 gate 를 안 바꾼다.

SQL 은 전부 인라인 리터럴(tests/test_sql_construction_contract.py 의 동적 SQL 게이트 대상 아님).
"""
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime

from flask import Blueprint, abort, jsonify, render_template, request, send_file, session

from app_core import RPCOST_PDF_DIR, execute, execute_rc, query
from helpers_shared import _annotate_drafts_with_vessel, _automation_enabled, admin_required, api_key_required

bp = Blueprint('routes_rpcost', __name__)

RPCOST_KINDS = ('repair', 'purchase')
RPCOST_SLOTS = ('01', '02', '03')
RPCOST_PDF_MAX = 25 * 1024 * 1024
_RPCOST_AUDIT_KEYS = ('original_inv_dt', 'inv_dt_override', 'inv_dt_override_by', 'inv_dt_override_at',
                      'original_date_match')
# 러너 상태로 '살아있는' 카드 — 재적재 dedup 대상. failed 는 빠진다(다음 적재가 새 카드로 다시 세운다).
# 수리 IU / 구매 U = SVMS 가 아직 컨펌 대기. 로컬 submitted 인데 이 상태로 다시 보이면 재개건.
_RPCOST_REOPEN_STATUS = {'repair': 'IU', 'purchase': 'U'}
# SVMS 에서 사람이 직접 처리한 상태 → 카드 종결 상태.
_RPCOST_EXT_CLOSE = {('repair', 'CU'): 'submitted', ('repair', 'CJ'): 'rejected',
                     ('purchase', 'A'): 'submitted', ('purchase', 'R'): 'rejected'}


def _rpcost_pdf_path(did):
    """미리보기 PDF 경로. 파일명=id.pdf 라 경로주입 불가."""
    return os.path.join(RPCOST_PDF_DIR, '%d.pdf' % int(did))


def _rpcost_pdf_delete(did):
    try:
        p = _rpcost_pdf_path(did)
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def _rpcost_obj(raw):
    if isinstance(raw, dict):
        return dict(raw)
    try:
        v = json.loads(raw or '{}')
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _rpcost_jdump(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def _rpcost_strip_audit(rc):
    """ingest payload 의 사람 편집 audit 키 제거 — 러너는 raw_card 만 보고 교정을 적용하므로
    교정값은 서버(/edit)가 만든 것만 존재해야 한다(외부 주입 차단)."""
    for k in _RPCOST_AUDIT_KEYS:
        rc.pop(k, None)
    sel = rc.get('exp_sel')
    if isinstance(sel, dict):
        for s in list(sel):
            if not isinstance(sel[s], dict) or sel[s].get('source') == 'user':
                sel.pop(s, None)
    return rc


def _rpcost_merge_pending(existing_raw, rc, cols):
    """pending 재적재 시 사람 교정 보존. SVMS INV_DT 가 교정 기준 원본과 달라졌으면 날짜 교정은 버린다
    (SVMS 쪽이 바뀐 것 — 옛 교정값이 새 값을 덮지 않게). gate 는 새 prep 판정 그대로(자동 승격 없음)."""
    old = _rpcost_obj(existing_raw)
    if old.get('inv_dt_override') and old.get('original_inv_dt') \
            and str(rc.get('inv_dt') or '') == str(old['original_inv_dt']):
        for k in _RPCOST_AUDIT_KEYS:
            if k in old:
                rc[k] = old[k]
        rc['original_date_match'] = cols.get('date_match')
        rc['date_match'] = True
        cols['inv_dt'] = old['inv_dt_override']
        cols['date_match'] = 1
    old_sel = old.get('exp_sel') if isinstance(old.get('exp_sel'), dict) else {}
    sel = rc.get('exp_sel') if isinstance(rc.get('exp_sel'), dict) else {}
    for s, v in old_sel.items():
        if isinstance(v, dict) and v.get('source') == 'user':
            sel[s] = v
    if sel or old_sel:
        rc['exp_sel'] = sel
    return rc, cols


@bp.route('/rpcost')
@admin_required
def rpcost_page():
    return render_template('rpcost.html')


@bp.route('/api/rpcost/drafts')
@admin_required
def api_rpcost_list():
    rows = query("SELECT * FROM rp_cost_draft ORDER BY CASE status WHEN 'pending' THEN 0 "
                 "WHEN 'approved' THEN 1 WHEN 'submitting' THEN 2 ELSE 3 END, id DESC")
    pending = query("SELECT COUNT(*) c FROM rp_cost_draft WHERE status='pending'", one=True)
    drafts = _annotate_drafts_with_vessel([dict(r) for r in rows])
    for d in drafts:
        d['has_pdf'] = os.path.exists(_rpcost_pdf_path(d['id']))
        d['rev'] = _rpcost_rev(d)
    return jsonify({'drafts': drafts, 'pending': pending['c'], 'enabled': _automation_enabled()})


@bp.route('/api/rpcost/drafts/<int:did>/pdf')
@admin_required
def api_rpcost_pdf(did):
    p = _rpcost_pdf_path(did)
    if not os.path.exists(p):
        abort(404)
    resp = send_file(p, mimetype='application/pdf', as_attachment=False,
                     download_name='rpcost_%d.pdf' % did, conditional=True)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@bp.route('/api/ext/rpcost/drafts', methods=['POST'])
@api_key_required
def api_ext_rpcost_create():
    """prep 엔진 ingest. 같은 kind+svms_key 가 pending 이면 갱신(사람 교정 보존), 진행/결정건은 dedup."""
    d = request.get_json(silent=True) or {}
    kind = str(d.get('kind') or '').strip().lower()
    key = str(d.get('svms_key') or '').strip()
    if kind not in RPCOST_KINDS or not key:
        return jsonify({'error': 'kind(repair|purchase) and svms_key required'}), 400
    if not str(d.get('vsl_cd') or '').strip():
        return jsonify({'error': 'vsl_cd required'}), 400
    svms_status = str(d.get('svms_status') or '').strip().upper()
    gate = 'PASS' if str(d.get('gate') or '').upper() == 'PASS' else 'HOLD'
    rc = _rpcost_strip_audit(_rpcost_obj(d.get('raw_card')))
    rc['kind'] = kind
    cols = {
        'ref_key': d.get('ref_key'), 'vsl_cd': d.get('vsl_cd'), 'vsl_nm': d.get('vsl_nm'), 'subj': d.get('subj'),
        'vndr_cd': d.get('vndr_cd'), 'vndr_nm': d.get('vndr_nm'), 'inv_no': d.get('inv_no'),
        'inv_dt': d.get('inv_dt'), 'cur_cd': d.get('cur_cd'), 'amt': d.get('amt'), 'pay_dt': d.get('pay_dt'),
        'inv_no_match': d.get('inv_no_match'), 'amt_match': d.get('amt_match'), 'date_match': d.get('date_match'),
        'match_src': d.get('match_src'), 'attachments': _rpcost_jdump(d.get('attachments')),
        'flags': _rpcost_jdump(d.get('flags')), 'hold_reasons': _rpcost_jdump(d.get('hold_reasons')),
        'gate': gate, 'svms_status': svms_status or None,
    }
    ex = query("SELECT id, status, raw_card FROM rp_cost_draft WHERE kind=? AND svms_key=? "
               "AND status IN ('pending','approved','submitting','submitted','rejected') "
               "ORDER BY id DESC LIMIT 1", (kind, key), one=True)
    if ex and ex['status'] == 'pending':
        rc, cols = _rpcost_merge_pending(ex['raw_card'], rc, cols)
        execute("UPDATE rp_cost_draft SET ref_key=?, vsl_cd=?, vsl_nm=?, subj=?, vndr_cd=?, vndr_nm=?, inv_no=?, "
                "inv_dt=?, cur_cd=?, amt=?, pay_dt=?, inv_no_match=?, amt_match=?, date_match=?, match_src=?, "
                "attachments=?, flags=?, hold_reasons=?, gate=?, svms_status=?, raw_card=?, claim_token=NULL, "
                "updated_at=datetime('now','localtime') WHERE id=? AND status='pending'",
                (*cols.values(), _rpcost_jdump(rc), ex['id']))
        return jsonify({'id': ex['id'], 'status': 'pending', 'updated': True}), 200
    reopened_from = None
    if ex and ex['status'] == 'submitted' and svms_status == _RPCOST_REOPEN_STATUS[kind]:
        reopened_from = ex['id']        # 로컬 종착인데 SVMS 가 다시 컨펌대기 = 종착기록 stale → 새 카드
    elif ex:
        return jsonify({'id': ex['id'], 'status': ex['status'], 'dedup': True}), 200
    try:
        did = execute("INSERT INTO rp_cost_draft (kind, svms_key, ref_key, vsl_cd, vsl_nm, subj, vndr_cd, vndr_nm, "
                      "inv_no, inv_dt, cur_cd, amt, pay_dt, inv_no_match, amt_match, date_match, match_src, "
                      "attachments, flags, hold_reasons, gate, svms_status, raw_card) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (kind, key, *cols.values(), _rpcost_jdump(rc)))
    except sqlite3.IntegrityError:
        # 동시 ingest 경합 — 다른 요청이 먼저 '살아있는' 카드를 세웠다(uq_rp_cost_draft_active). 500 대신 dedup.
        cur = query("SELECT id, status FROM rp_cost_draft WHERE kind=? AND svms_key=? "
                    "AND status IN ('pending','approved','submitting') ORDER BY id DESC LIMIT 1", (kind, key), one=True)
        return jsonify({'id': cur['id'] if cur else None, 'status': cur['status'] if cur else None,
                        'dedup': True, 'race': True}), 200
    if reopened_from:
        execute("UPDATE rp_cost_draft SET result=COALESCE(result,'') || ? WHERE id=?",
                ('\n[재개] SVMS %s 로 다시 컨펌대기 — 카드 #%d 로 재적재(%s).'
                 % (svms_status, did, datetime.now().strftime('%Y-%m-%d %H:%M')), reopened_from))
    return jsonify({'id': did, 'status': 'pending', 'reopened_from': reopened_from}), 201


@bp.route('/api/ext/rpcost/drafts/<int:did>/pdf', methods=['POST'])
@api_key_required
def api_ext_rpcost_pdf_upload(did):
    """미리보기 원본 PDF(raw body). 결정 전(pending/approved)만 받는다 — 완료건 지연업로드 부활 race 차단."""
    if request.content_length and request.content_length > RPCOST_PDF_MAX:
        return jsonify({'error': 'too large'}), 413
    row = query('SELECT status FROM rp_cost_draft WHERE id=?', (did,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    if row['status'] not in ('pending', 'approved'):
        return jsonify({'error': 'not accepting', 'status': row['status']}), 409
    data = request.get_data()
    if not data or data[:5] != b'%PDF-':
        return jsonify({'error': 'not pdf'}), 400
    if len(data) > RPCOST_PDF_MAX:
        return jsonify({'error': 'too large'}), 413
    os.makedirs(RPCOST_PDF_DIR, exist_ok=True)
    final = _rpcost_pdf_path(did)
    tmp = final + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.replace(tmp, final)
    return jsonify({'id': did, 'stored': True, 'bytes': len(data)})


def _rpcost_rev(row):
    """카드 표시 리비전 — 앱이 본 카드와 승인 시점 카드가 같은지(CAS) 확인용. raw_card + updated_at."""
    import hashlib
    src = '%s|%s' % (row['raw_card'] or '', row['updated_at'] or '')
    return hashlib.sha256(src.encode('utf-8')).hexdigest()[:32]


def _rpcost_set_approved(did, who):
    return execute_rc("UPDATE rp_cost_draft SET status='approved', decided_at=datetime('now','localtime'), "
                      "decided_by=? WHERE id=? AND status='pending' AND raw_card IS NOT NULL", (who, did))


@bp.route('/api/rpcost/drafts/<int:did>/approve', methods=['POST'])
@admin_required
def api_rpcost_approve(did):
    """선택 `expect_rev`(목록의 rev) 가 오면 표시 후 카드가 바뀌었는지 CAS — 다르면 409(앱 stale 승인 차단)."""
    expect = str((request.get_json(silent=True) or {}).get('expect_rev') or '').strip()
    who = session.get('username') or 'web'
    if expect:
        row = query('SELECT status, raw_card, updated_at FROM rp_cost_draft WHERE id=?', (did,), one=True)
        if not row:
            return jsonify({'error': 'not found'}), 404
        if row['status'] == 'pending' and _rpcost_rev(row) != expect:
            return jsonify({'error': '표시 후 카드가 바뀌었습니다 — 새로고침 후 다시 확인하세요', 'stale': True}), 409
        ok = row['status'] == 'pending' and row['raw_card'] is not None and execute_rc(
            "UPDATE rp_cost_draft SET status='approved', decided_at=datetime('now','localtime'), decided_by=? "
            "WHERE id=? AND status='pending' AND raw_card IS ? AND updated_at IS ?",
            (who, did, row['raw_card'], row['updated_at']))
    else:
        ok = _rpcost_set_approved(did, who)
    if not ok:
        cur = query('SELECT status FROM rp_cost_draft WHERE id=?', (did,), one=True)
        if not cur:
            return jsonify({'error': 'not found'}), 404
        if expect and cur['status'] == 'pending':
            return jsonify({'error': '표시 후 카드가 바뀌었습니다 — 새로고침 후 다시 확인하세요', 'stale': True}), 409
        return jsonify({'error': 'already decided', 'status': cur['status']}), 409
    return jsonify({'id': did, 'status': 'approved'})


@bp.route('/api/rpcost/drafts/approve-bulk', methods=['POST'])
@admin_required
def api_rpcost_approve_bulk():
    d = request.get_json(silent=True) or {}
    ids = d.get('ids') if isinstance(d.get('ids'), list) else []
    who = session.get('username') or 'web'
    approved, skipped = [], []
    for raw_id in ids:
        try:
            did = int(raw_id)
        except (TypeError, ValueError):
            skipped.append(raw_id)
            continue
        (approved if _rpcost_set_approved(did, who) else skipped).append(did)
    return jsonify({'approved': len(approved), 'skipped': len(skipped), 'approved_ids': approved})


@bp.route('/api/rpcost/drafts/<int:did>/reject', methods=['POST'])
@admin_required
def api_rpcost_reject(did):
    """v1 리젝 = TRMT 표시만(SVMS 반려 안 함). 사유 필수."""
    reason = str((request.get_json(silent=True) or {}).get('reason') or '').strip()
    if not reason:
        return jsonify({'error': '리젝 사유(reason) 필수', 'field': 'reason'}), 400
    rc = execute_rc("UPDATE rp_cost_draft SET status='rejected', reject_reason=?, "
                    "decided_at=datetime('now','localtime'), decided_by=?, done_at=datetime('now','localtime'), "
                    "result='TRMT 표시만 — SVMS 반려는 안 함(v1). 필요하면 SVMS 에서 직접 반려' "
                    "WHERE id=? AND status IN ('pending','approved')",
                    (reason[:1000], session.get('username') or 'web', did))
    if not rc:
        cur = query('SELECT status FROM rp_cost_draft WHERE id=?', (did,), one=True)
        if not cur:
            return jsonify({'error': 'not found'}), 404
        return jsonify({'error': 'already decided', 'status': cur['status']}), 409
    _rpcost_pdf_delete(did)
    return jsonify({'id': did, 'status': 'rejected',
                    'message': 'TRMT 에서만 리젝 표시했습니다. SVMS 는 그대로입니다(필요하면 SVMS 에서 직접 반려).'})


@bp.route('/api/rpcost/drafts/<int:did>/reset', methods=['POST'])
@admin_required
def api_rpcost_reset(did):
    """결정 취소 — 실행 전(approved) 또는 TRMT 표시만 한 리젝(rejected)을 pending 으로."""
    try:
        rc = execute_rc("UPDATE rp_cost_draft SET status='pending', reject_reason=NULL, decided_at=NULL, "
                        "decided_by=NULL, done_at=NULL, result=NULL, claim_token=NULL "
                        "WHERE id=? AND status IN ('approved','rejected')", (did,))
    except sqlite3.IntegrityError:
        return jsonify({'error': '같은 SVMS 건의 열린 카드가 이미 있습니다 — 그 카드를 쓰세요', 'id': did}), 409
    if not rc:
        cur = query('SELECT status FROM rp_cost_draft WHERE id=?', (did,), one=True)
        if not cur:
            return jsonify({'error': 'not found'}), 404
        return jsonify({'error': '실행 전(approved)·리젝표시(rejected)만 취소 가능', 'status': cur['status']}), 409
    return jsonify({'id': did, 'status': 'pending'})


@bp.route('/api/rpcost/drafts/<int:did>', methods=['DELETE'])
@admin_required
def api_rpcost_delete(did):
    if not execute_rc("DELETE FROM rp_cost_draft WHERE id=? "
                      "AND status IN ('pending','submitted','rejected','failed')", (did,)):
        cur = query('SELECT status FROM rp_cost_draft WHERE id=?', (did,), one=True)
        if not cur:
            return jsonify({'error': 'not found'}), 404
        return jsonify({'error': '실행 대기·진행중(approved/submitting)은 삭제 불가 — 결정 취소 후 삭제',
                        'status': cur['status']}), 409
    _rpcost_pdf_delete(did)
    return jsonify({'id': did, 'deleted': True})


@bp.route('/api/rpcost/drafts/decided', methods=['DELETE'])
@admin_required
def api_rpcost_clear_decided():
    ids = [r['id'] for r in query("SELECT id FROM rp_cost_draft WHERE status IN ('submitted','rejected','failed')")]
    n = execute_rc("DELETE FROM rp_cost_draft WHERE status IN ('submitted','rejected','failed')")
    for i in ids:
        _rpcost_pdf_delete(i)
    return jsonify({'ok': True, 'deleted': n})


def _rpcost_valid_date(v):
    if not re.fullmatch(r'\d{8}', v):
        return False
    try:
        datetime.strptime(v, '%Y%m%d')
        return True
    except ValueError:
        return False


@bp.route('/api/rpcost/drafts/<int:did>/edit', methods=['POST'])
@admin_required
def api_rpcost_edit(did):
    """수리 카드 교정: inv_dt(YYYYMMDD, audit) + exp {"01"|"02"|"03": 코드 또는 ""}. pending 만, raw_card CAS.
    구매 카드는 편집 항목 없음(v1)."""
    d = request.get_json(silent=True) or {}
    row = query('SELECT kind, status, inv_dt, date_match, raw_card FROM rp_cost_draft WHERE id=?', (did,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    if row['status'] != 'pending':
        return jsonify({'error': '대기(pending) 카드만 편집 가능 — 현재 %s' % row['status']}), 409
    if row['kind'] != 'repair':
        return jsonify({'error': '구매 카드는 편집 항목이 없습니다'}), 400
    rc = _rpcost_obj(row['raw_card'])
    who = session.get('username') or 'web'
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inv_dt, date_match = row['inv_dt'], row['date_match']
    changed = False
    if 'inv_dt' in d:
        v = str(d.get('inv_dt') or '').strip()
        if not _rpcost_valid_date(v):
            return jsonify({'error': 'INV_DT 는 실제 날짜 YYYYMMDD 여야 합니다', 'field': 'inv_dt'}), 400
        if v != str(inv_dt or ''):
            original = rc.get('original_inv_dt') or rc.get('inv_dt') or inv_dt
            if 'original_inv_dt' not in rc:
                rc['original_date_match'] = row['date_match']
            if v == str(original):                        # 원래 SVMS 값으로 되돌림 = override 해제
                restored = rc.get('original_date_match')
                for k in _RPCOST_AUDIT_KEYS:
                    rc.pop(k, None)
                rc['inv_dt'], rc['date_match'] = v, (None if restored is None else bool(restored))
                inv_dt, date_match = v, restored
            else:
                rc.update({'original_inv_dt': original, 'inv_dt_override': v, 'inv_dt_override_by': who,
                           'inv_dt_override_at': now, 'inv_dt': v, 'date_match': True})
                inv_dt, date_match = v, 1
            changed = True
    if 'exp' in d:
        exp = d.get('exp')
        if not isinstance(exp, dict) or any(s not in RPCOST_SLOTS for s in exp):
            return jsonify({'error': 'exp 는 {"01"|"02"|"03": 코드} 형식', 'field': 'exp'}), 400
        sel = rc.get('exp_sel') if isinstance(rc.get('exp_sel'), dict) else {}
        n_master = query('SELECT COUNT(*) c FROM expense_code', one=True)['c']
        for s, code in exp.items():
            code = str(code or '').strip()
            cur = sel.get(s) if isinstance(sel.get(s), dict) else {}
            if code == str(cur.get('exp_cd') or ''):
                continue
            if not code:
                sel[s] = {'exp_cd': None, 'exp_nm': None, 'source': 'user', 'by': who, 'at': now}
            else:
                if not re.fullmatch(r'[0-9A-Za-z]{2,12}', code):
                    return jsonify({'error': 'EXP_CD 형식 오류: %s' % code, 'field': 'exp'}), 400
                m = query('SELECT name FROM expense_code WHERE code=?', (code,), one=True)
                if n_master and not m:
                    return jsonify({'error': 'EXP_CD %s 가 expense code 마스터에 없습니다' % code, 'field': 'exp'}), 400
                sel[s] = {'exp_cd': code, 'exp_nm': m['name'] if m else None, 'source': 'user', 'by': who, 'at': now}
            changed = True
        rc['exp_sel'] = sel
    if not changed:
        return jsonify({'id': did, 'noop': True, 'inv_dt': inv_dt, 'exp_sel': rc.get('exp_sel')})
    n = execute_rc("UPDATE rp_cost_draft SET inv_dt=?, date_match=?, raw_card=?, updated_at=datetime('now','localtime') "
                   "WHERE id=? AND status='pending' AND raw_card IS ?",
                   (inv_dt, date_match, _rpcost_jdump(rc), did, row['raw_card']))
    if not n:
        cur = query('SELECT status FROM rp_cost_draft WHERE id=?', (did,), one=True)
        if cur and cur['status'] == 'pending':
            return jsonify({'error': '다른 수정/재적재와 겹쳤습니다 — 새로고침 후 다시 저장하세요'}), 409
        return jsonify({'error': '대기(pending) 카드만 편집 가능 — 현재 %s' % (cur['status'] if cur else '?')}), 409
    return jsonify({'id': did, 'inv_dt': inv_dt, 'date_match': date_match, 'exp_sel': rc.get('exp_sel')})


# ---- ext (맥 러너) ----
@bp.route('/api/ext/rpcost/approved')
@api_key_required
def api_ext_rpcost_approved():
    """approved → submitting claim(조건부 CAS). ?peek=1 = 상태 안 바꾸고 조회만(DRY 전용). ?kind=repair|purchase = 그 종류만.
    ?only=<svms_key> = 그 건만(카나리). claim 은 `status='approved' AND raw_card IS <읽은 값>` CAS 로
    새 claim_token 을 박고, 응답 스냅샷은 정확히 CAS 가 확인한 그 raw_card 다. result 는 이 토큰이 맞을 때만 반영.
    6h+ submitting = failed(사람 재검토), 자동 재컨펌 안 함(절반성공 이중처리 방지)."""
    only = str(request.args.get('only') or '').strip()
    kind = str(request.args.get('kind') or '').strip().lower()   # repair|purchase 만 처리(미지정=둘 다)
    if kind and kind not in RPCOST_KINDS:
        return jsonify({'error': 'kind must be repair|purchase'}), 400
    if request.args.get('peek'):
        rows = query("SELECT id, kind, svms_key, ref_key, vsl_cd, raw_card FROM rp_cost_draft "
                     "WHERE status='approved' AND (?='' OR svms_key=?) AND (?='' OR kind=?) ORDER BY id ASC",
                     (only, only, kind, kind))
        return jsonify({'count': len(rows), 'drafts': [dict(r) for r in rows], 'peek': True})
    execute("UPDATE rp_cost_draft SET status='failed', "
            "result=COALESCE(result,'')||' [auto:6h+ submitting→failed, 사람 재검토]' "
            "WHERE status='submitting' AND done_at IS NOT NULL AND done_at < datetime('now','localtime','-6 hours')")
    out = []
    for r in query("SELECT id, kind, svms_key, ref_key, vsl_cd, raw_card FROM rp_cost_draft "
                   "WHERE status='approved' AND (?='' OR svms_key=?) AND (?='' OR kind=?) ORDER BY id ASC",
                     (only, only, kind, kind)):
        token = secrets.token_hex(16)
        if execute_rc("UPDATE rp_cost_draft SET status='submitting', claim_token=?, done_at=datetime('now','localtime') "
                      "WHERE id=? AND status='approved' AND raw_card IS ? AND kind=?", (token, r['id'], r['raw_card'], r['kind'])):
            out.append(dict(r, claim_token=token))
    return jsonify({'count': len(out), 'drafts': out})


@bp.route('/api/ext/rpcost/drafts/<int:did>/result', methods=['POST'])
@api_key_required
def api_ext_rpcost_result(did):
    """claim_token 이 현재 claim 과 같을 때만 반영(ABA·지연 회신 차단). 불일치 = 409 applied:false."""
    d = request.get_json(silent=True) or {}
    ok = bool(d.get('ok'))
    token = str(d.get('claim_token') or '').strip()
    rc = 0
    if token:
        rc = execute_rc("UPDATE rp_cost_draft SET status=?, done_at=datetime('now','localtime'), result=? "
                        "WHERE id=? AND status='submitting' AND claim_token=?",
                        ('submitted' if ok else 'failed', str(d.get('result') or '')[:2000], did, token))
    if rc and ok:
        _rpcost_pdf_delete(did)
    return jsonify({'id': did, 'ok': ok, 'applied': bool(rc)}), (200 if rc else 409)


@bp.route('/api/ext/rpcost/open')
@api_key_required
def api_ext_rpcost_open():
    """열린 카드(pending/approved) — 러너가 SVMS 라이브와 대조해 직접처리건을 reconcile 로 닫는 용도. 읽기전용."""
    rows = query("SELECT id, kind, svms_key, status, raw_card FROM rp_cost_draft "
                 "WHERE status IN ('pending','approved') ORDER BY id ASC LIMIT 501")
    return jsonify({'count': min(len(rows), 500), 'drafts': [dict(r) for r in rows[:500]],
                    'truncated': len(rows) > 500})


@bp.route('/api/ext/rpcost/reconcile', methods=['POST'])
@api_key_required
def api_ext_rpcost_reconcile():
    """사람이 SVMS 에서 직접 처리한 건 카드 종결. items=[{id, kind, svms_key, svms_status}].
    id 필수(재적재된 새 카드 오종결 방지), pending/approved 만, 조건부 UPDATE. SVMS 쓰기 0."""
    items = (request.get_json(silent=True) or {}).get('items')
    if not isinstance(items, list):
        return jsonify({'error': 'items array required'}), 400
    closed, skipped = [], []
    for it in items:
        it = it if isinstance(it, dict) else {}
        kind, key = str(it.get('kind') or ''), str(it.get('svms_key') or '').strip()
        sv = str(it.get('svms_status') or '').strip().upper()
        new = _RPCOST_EXT_CLOSE.get((kind, sv))
        try:
            did = int(it.get('id'))
        except (TypeError, ValueError):
            did = None
        if not new or not did or not key:
            skipped.append({'id': did, 'svms_key': key, 'why': '부적격(%s/%s)' % (kind, sv)})
            continue
        note = '[외부] SVMS 에서 사람이 직접 처리(STATUS=%s) — 러너 미개입, 카드만 정리' % sv
        rc = execute_rc("UPDATE rp_cost_draft SET status=?, done_at=datetime('now','localtime'), "
                        "decided_at=COALESCE(decided_at, datetime('now','localtime')), "
                        "decided_by=COALESCE(decided_by, 'svms-direct'), result=? "
                        "WHERE id=? AND kind=? AND svms_key=? AND status IN ('pending','approved')",
                        (new, note, did, kind, key))
        if not rc:
            skipped.append({'id': did, 'svms_key': key, 'why': '열린 카드 아님/불일치'})
            continue
        _rpcost_pdf_delete(did)
        closed.append({'id': did, 'svms_key': key, 'to': new, 'svms_status': sv})
    return jsonify({'closed': closed, 'closed_n': len(closed), 'skipped': skipped, 'skipped_n': len(skipped)})
