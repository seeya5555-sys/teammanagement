"""TRMT 수리신청서 — 일반수리와 Dock 수리 공용 작성/저장 진입점."""
import re
import sqlite3
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request, session

from app_core import execute, execute_rc, get_db, query
from helpers_shared import _automation_enabled, admin_required, api_key_required, login_required

bp = Blueprint("routes_repair_request", __name__)
_EDITABLE = ('pending', 'failed')


def _queue_save(user):
    if not _automation_enabled():
        return None
    busy = query("SELECT run_id FROM automation_run WHERE task='reqgen_save' AND status='queued' "
                 "ORDER BY id DESC LIMIT 1", one=True)
    if busy:
        return busy['run_id']
    run_id = uuid.uuid4().hex[:12]
    execute("INSERT INTO automation_run(run_id,task,mode,status,requested_by) "
            "VALUES(?,'reqgen_save','live','queued',?)", (run_id, user))
    return run_id


def _text(d, key, required=True, limit=10000):
    v = d.get(key)
    if not isinstance(v, str):
        v = '' if v is None else str(v)
    v = v.strip()
    if required and not v:
        raise ValueError(f'{key} 필수')
    if len(v) > limit:
        raise ValueError(f'{key} 너무 김(최대 {limit}자)')
    return v


def _payload(d):
    try:
        vessel_id = int(d.get('vessel_id') or 0)
    except (TypeError, ValueError):
        raise ValueError('선박 선택 필수')
    vessel = query('SELECT id,name,vsl_cd FROM vessels WHERE id=? AND active=1', (vessel_id,), one=True)
    if not vessel or not (vessel['vsl_cd'] or '').strip():
        raise ValueError('SVMS 코드가 등록된 선박을 선택하세요')
    stock = _text(d, 'stock')
    if stock not in ('owner', 'vendor'):
        raise ValueError('Stock of Spare는 owner/vendor 중 선택')
    app_dt = re.sub(r'[^0-9]', '', _text(d, 'app_dt'))
    if not re.fullmatch(r'\d{8}', app_dt):
        raise ValueError('예정일은 YYYY-MM-DD 형식')
    try:
        datetime.strptime(app_dt, '%Y%m%d')
    except ValueError:
        raise ValueError('유효한 예정일을 입력하세요')
    yn = lambda key: 'Y' if d.get(key) in (True, 1, '1', 'Y', 'y') else 'N'
    return {
        'vessel_id': vessel_id, 'vsl_cd': vessel['vsl_cd'].strip().upper(), 'vsl_nm': vessel['name'],
        'subject': _text(d, 'subject', limit=300), 'category': _text(d, 'category', limit=120),
        'equipment': _text(d, 'equipment', limit=120), 'maker': _text(d, 'maker', False, 120),
        'type_nm': _text(d, 'type_nm', False, 120), 'app_voy': _text(d, 'app_voy', limit=40),
        'app_port_cd': _text(d, 'app_port_cd', limit=20).upper(), 'app_dt': app_dt,
        'cause': _text(d, 'cause', limit=10000), 'inspection': _text(d, 'inspection', limit=10000),
        'detail': _text(d, 'detail', limit=30000), 'stock': stock,
        'reason_cd': _text(d, 'reason_cd', limit=10).upper(),
        'dept_cd': _text(d, 'dept_cd', limit=10).upper(),
        'dock_yn': yn('dock_yn'), 'urgent_yn': yn('urgent_yn'), 'critical_yn': yn('critical_yn'),
    }


def _reserve_rows(p, client_id, who):
    """신청서와 downstream MARP rid를 한 transaction에서 만든다."""
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        if client_id:
            ex = db.execute('SELECT id FROM repair_request WHERE client_request_id=?', (client_id,)).fetchone()
            if ex:
                db.rollback(); return ex['id'], False
        cols = ','.join(p); qs = ','.join('?' for _ in p)
        cur = db.execute(f"INSERT INTO repair_request(client_request_id,{cols},created_by) "
                         f"VALUES(?,{qs},?)", (client_id, *p.values(), who))
        rrid = cur.lastrowid
        # 일반수리도 기존 MARP 견적·상신 엔진을 공용 사용한다. 그 엔진의 목록은
        # dock_procure_vessel 를 선박 선택기의 정본으로 삼으므로, 신청서+downstream 행과
        # 같은 transaction 안에서 선박 엔트리를 보장한다. 기존 입거 메타는 덮지 않는다.
        db.execute("INSERT INTO dock_procure_vessel(vsl_nm,vsl_cd,updated_at) "
                   "VALUES(?,?,datetime('now','localtime')) "
                   "ON CONFLICT(vsl_nm) DO UPDATE SET "
                   "vsl_cd=COALESCE(dock_procure_vessel.vsl_cd,excluded.vsl_cd), "
                   "updated_at=excluded.updated_at", (p['vsl_nm'], p['vsl_cd']))
        if p['dock_yn'] == 'Y':
            nums = [int(x[0][1:]) for x in db.execute(
                "SELECT req_no FROM dock_procure WHERE vsl_nm=? AND req_no GLOB 'R[0-9]*'", (p['vsl_nm'],))
                if re.fullmatch(r'R\d+', x[0] or '')]
            req_no = f"R{max(nums, default=0)+1}"
        else:
            req_no = f"RR{rrid}"
        cur = db.execute("INSERT INTO dock_procure(vsl_nm,vsl_cd,req_no,cat_code,category,equipment,subject,"
                         "prepared_by,source,content_hash) VALUES(?,?,?,'R','SHORE REPAIR',?,?,"
                         "'OWNER','SVMS',?)", (p['vsl_nm'], p['vsl_cd'], req_no, p['equipment'],
                                               p['subject'], f"repair-request:{rrid}"))
        db.execute('UPDATE repair_request SET dock_rid=? WHERE id=?', (cur.lastrowid, rrid))
        db.commit(); return rrid, True
    except Exception:
        db.rollback(); raise


@bp.route('/repair-requests')
@login_required
def repair_request_page():
    procure_id = request.args.get('procure_id', type=int)
    if procure_id:
        row = query('SELECT id FROM repair_request WHERE id=?', (procure_id,), one=True)
        if not row:
            return '수리신청서를 찾을 수 없습니다.', 404
        return render_template('dock_procure.html', repair_mode=True, repair_id=procure_id)
    return render_template('repair_requests.html')


@bp.route('/api/repair-requests')
@login_required
def repair_request_list():
    rows = query("SELECT r.*,d.req_no,d.svms_status,d.svms_submit,d.sub_quotes,d.att_files,"
                 "d.stg_quote,d.stg_vendor,d.stg_confirm,d.stg_order FROM repair_request r "
                 "LEFT JOIN dock_procure d ON d.id=r.dock_rid ORDER BY r.id DESC LIMIT 300")
    return jsonify({'requests': [dict(x) for x in rows]})


@bp.route('/api/repair-requests', methods=['POST'])
@admin_required
def repair_request_create():
    d = request.get_json(silent=True) or {}
    try:
        p = _payload(d); cid = _text(d, 'client_request_id', False, 100) or None
        rid, made = _reserve_rows(p, cid, session.get('username') or 'web')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except sqlite3.IntegrityError as e:
        return jsonify({'error': f'중복 생성 차단: {e}'}), 409
    row = query('SELECT id,status,dock_rid,version FROM repair_request WHERE id=?', (rid,), one=True)
    # `replayed` = 같은 client_request_id 로 이미 접수돼 이번 body 는 **저장되지 않았다**는 뜻.
    # 응답만 유실된 재시도(선상 회선)에서 클라이언트가 "저장됨"으로 오인해 사용자의 수정분을
    # 조용히 버리는 것을 막기 위한 additive 필드다(웹 화면은 이 응답을 읽지 않으므로 영향 없음).
    out = dict(row); out['replayed'] = (not made)
    return jsonify(out), (201 if made else 200)


@bp.route('/api/repair-requests/<int:rid>', methods=['PATCH'])
@admin_required
def repair_request_patch(rid):
    row = query('SELECT * FROM repair_request WHERE id=?', (rid,), one=True)
    if not row: return jsonify({'error': 'not found'}), 404
    if row['status'] not in _EDITABLE or row['rep_cd']:
        return jsonify({'error': 'SVMS 저장 전 초안만 수정 가능', 'status': row['status']}), 409
    d = request.get_json(silent=True) or {}
    try:
        p = _payload(d); version = int(d.get('version') or 0)
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    if p['dock_yn'] != row['dock_yn']:
        return jsonify({'error': 'Dock 여부는 생성 후 변경할 수 없습니다. 새 초안을 작성해 주세요.'}), 409
    if row['dock_yn'] == 'Y' and p['vessel_id'] != row['vessel_id']:
        return jsonify({'error': 'Dock 수리는 R번호 예약 후 선박을 변경할 수 없습니다. 새 초안을 작성해 주세요.'}), 409
    sets = ','.join(f'{k}=?' for k in p)
    rc = execute_rc(f"UPDATE repair_request SET {sets},status='pending',result=NULL,version=version+1,"
                    "updated_at=datetime('now','localtime') WHERE id=? AND version=? AND rep_cd IS NULL",
                    (*p.values(), rid, version))
    if not rc: return jsonify({'error': '다른 화면에서 수정됨. 새로고침 후 다시 시도'}), 409
    execute("UPDATE dock_procure SET vsl_nm=?,vsl_cd=?,equipment=?,subject=?,updated_at=datetime('now','localtime') "
            "WHERE id=?", (p['vsl_nm'], p['vsl_cd'], p['equipment'], p['subject'], row['dock_rid']))
    return jsonify({'id': rid, 'version': version + 1, 'status': 'pending'})


@bp.route('/api/repair-requests/<int:rid>/approve', methods=['POST'])
@admin_required
def repair_request_approve(rid):
    if not _automation_enabled(): return jsonify({'error': 'killswitch ON — 자동화 정지중'}), 409
    who = session.get('username') or 'web'
    rc = execute_rc("UPDATE repair_request SET status='approved',decided_by=?,"
                    "decided_at=datetime('now','localtime') WHERE id=? AND status IN ('pending','failed') "
                    "AND rep_cd IS NULL", (who, rid))
    if not rc: return jsonify({'error': '저장 가능한 초안이 아님'}), 409
    run = _queue_save(who)
    if not run:
        execute("UPDATE repair_request SET status='failed',result='저장 큐 적재 실패' "
                "WHERE id=? AND status='approved'", (rid,))
        return jsonify({'error': '저장 큐 적재 실패'}), 409
    return jsonify({'id': rid, 'status': 'approved', 'save_run': run})


@bp.route('/api/repair-requests/<int:rid>', methods=['DELETE'])
@admin_required
def repair_request_delete(rid):
    row = query('SELECT status,rep_cd,dock_rid FROM repair_request WHERE id=?', (rid,), one=True)
    if not row: return jsonify({'error': 'not found'}), 404
    if row['rep_cd'] or row['status'] not in _EDITABLE:
        return jsonify({'error': 'SVMS 저장 전 초안만 삭제 가능'}), 409
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('DELETE FROM repair_request WHERE id=?', (rid,))
        db.execute('DELETE FROM dock_procure WHERE id=?', (row['dock_rid'],)); db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify({'ok': True, 'id': rid, 'deleted': True})


@bp.route('/api/ext/repair-requests/approved')
@api_key_required
def repair_request_claim():
    peek = request.args.get('peek') in ('1', 'true')
    rows = query("SELECT r.*,d.req_no FROM repair_request r LEFT JOIN dock_procure d ON d.id=r.dock_rid "
                 "WHERE r.status='approved' ORDER BY r.id")
    out = []
    for row in rows:
        if peek or execute_rc("UPDATE repair_request SET status='saving',done_at=datetime('now','localtime') "
                              "WHERE id=? AND status='approved'", (row['id'],)):
            out.append(dict(row))
    return jsonify({'count': len(out), 'requests': out, 'peek': peek})


@bp.route('/api/ext/repair-requests/<int:rid>/result', methods=['POST'])
@api_key_required
def repair_request_result(rid):
    d = request.get_json(silent=True) or {}; ok = bool(d.get('ok'))
    rep_cd = (d.get('rep_cd') or '').strip() or None
    if ok and not rep_cd:
        return jsonify({'error': '성공 결과에는 REP_CD가 필요합니다'}), 400
    row = query('SELECT dock_rid FROM repair_request WHERE id=?', (rid,), one=True)
    if not row: return jsonify({'error': 'not found'}), 404
    db = get_db(); db.execute('BEGIN IMMEDIATE'); applied = False
    try:
        cur = db.execute("UPDATE repair_request SET status=?,rep_cd=?,result=?,done_at=datetime('now','localtime'),"
                         "updated_at=datetime('now','localtime') WHERE id=? AND status='saving'",
                         ('saved' if ok else 'failed', rep_cd, str(d.get('result') or '')[:2000], rid))
        applied = bool(cur.rowcount)
        if applied and ok and rep_cd:
            db.execute("UPDATE dock_procure SET svms_pushed=1,svms_req_no=?,svms_status='HQ Received',"
                       "stg_quote=1,updated_at=datetime('now','localtime') WHERE id=?", (rep_cd, row['dock_rid']))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify({'id': rid, 'ok': ok, 'applied': applied})


@bp.route('/api/repair-requests/<int:rid>/resolve', methods=['POST'])
@admin_required
def repair_request_resolve(rid):
    """SVMS 결과가 모호해 saving/failed에 멈춘 건을 사람 확인 후에만 복구한다."""
    d = request.get_json(silent=True) or {}
    action = d.get('action')
    row = query('SELECT status,rep_cd,dock_rid FROM repair_request WHERE id=?', (rid,), one=True)
    if not row: return jsonify({'error': 'not found'}), 404
    if row['status'] not in ('saving', 'failed'):
        return jsonify({'error': '복구 대상 상태가 아님'}), 409
    if action == 'mark_saved':
        if d.get('confirmation') != 'SVMS확인':
            return jsonify({'error': 'SVMS확인 문구가 필요합니다'}), 400
        rep_cd = (d.get('rep_cd') or '').strip().upper()
        if not re.fullmatch(r'[A-Z0-9_-]{3,50}', rep_cd):
            return jsonify({'error': '유효한 REP_CD를 입력하세요'}), 400
        db = get_db(); db.execute('BEGIN IMMEDIATE')
        try:
            db.execute("UPDATE repair_request SET status='saved',rep_cd=?,result=?,done_at=datetime('now','localtime'),"
                       "updated_at=datetime('now','localtime') WHERE id=? AND status IN ('saving','failed')",
                       (rep_cd, f'사람 확인으로 저장 확정 · REP_CD {rep_cd}', rid))
            db.execute("UPDATE dock_procure SET svms_pushed=1,svms_req_no=?,svms_status='HQ Received',"
                       "stg_quote=1,updated_at=datetime('now','localtime') WHERE id=?", (rep_cd, row['dock_rid']))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback(); return jsonify({'error': '이미 다른 신청서에 연결된 REP_CD입니다'}), 409
        except Exception:
            db.rollback(); raise
        return jsonify({'id': rid, 'status': 'saved', 'rep_cd': rep_cd})
    if action == 'release':
        if d.get('confirmation') != 'SVMS미생성확인':
            return jsonify({'error': 'SVMS미생성확인 문구가 필요합니다'}), 400
        if row['rep_cd']:
            return jsonify({'error': 'REP_CD가 기록된 건은 재시도 해제할 수 없습니다'}), 409
        execute("UPDATE repair_request SET status='failed',result='사람이 SVMS 미생성을 확인함 · 재시도 가능',"
                "updated_at=datetime('now','localtime') WHERE id=? AND status IN ('saving','failed')", (rid,))
        return jsonify({'id': rid, 'status': 'failed'})
    return jsonify({'error': 'action은 mark_saved/release 중 선택'}), 400
