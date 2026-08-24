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


# 🔴 그룹 키 정규화 규칙(생성·수정 공용). 같은 SVMS 코드의 Dock 엔트리가 이미 있으면
#    **그 표기를 그대로 쓴다**. `vessels.name`('Belgium B')을 그대로 넣으면 SVMS INDEX 표기
#    ('BELGIUM B')와 다른 PK 행이 생겨 Dock 발주현황에 같은 배가 두 장 뜨고,
#    한 장은 어느 경로로도 안 지워졌다(2026-08-15 실사고).
_CANON_VSL_SQL = ("SELECT vsl_nm FROM dock_procure_vessel "
                  "WHERE TRIM(UPPER(COALESCE(vsl_cd,'')))=? AND vsl_nm<>? "
                  "ORDER BY (origin IS NULL) DESC, updated_at DESC LIMIT 1")


def _apply_canon_vsl_nm(p):
    """트랜잭션 밖(PATCH)에서 쓰는 정규화. 생성 경로와 같은 규칙을 적용한다."""
    canon = query(_CANON_VSL_SQL, (p['vsl_cd'], p['vsl_nm']), one=True)
    return {**p, 'vsl_nm': canon['vsl_nm']} if canon else p


def _reserve_rows(p, client_id, who):
    """신청서와 downstream MARP rid를 한 transaction에서 만든다."""
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        if client_id:
            ex = db.execute('SELECT id FROM repair_request WHERE client_request_id=?', (client_id,)).fetchone()
            if ex:
                db.rollback(); return ex['id'], False
        # 그룹 키 정규화 — 규칙은 `_CANON_VSL_SQL` 주석 참조(생성·수정 공용).
        canon = db.execute(_CANON_VSL_SQL, (p['vsl_cd'], p['vsl_nm'])).fetchone()
        if canon:
            p = {**p, 'vsl_nm': canon['vsl_nm']}
        cols = ','.join(p); qs = ','.join('?' for _ in p)
        cur = db.execute(f"INSERT INTO repair_request(client_request_id,{cols},created_by) "
                         f"VALUES(?,{qs},?)", (client_id, *p.values(), who))
        rrid = cur.lastrowid
        # 일반수리도 기존 MARP 견적·상신 엔진을 공용 사용한다. 그 엔진의 목록은
        # dock_procure_vessel 를 선박 선택기의 정본으로 삼으므로, 신청서+downstream 행과
        # 같은 transaction 안에서 선박 엔트리를 보장한다. 기존 입거 메타는 덮지 않는다.
        # 새로 만드는 엔트리는 `origin='repair'` = 입거선박이 아니라 견적엔진용 shim 이라는 표시.
        # 기존 행(Dock 정본)은 origin 을 건드리지 않는다 — 건드리면 진짜 입거선박이 탭에서 사라진다.
        db.execute("INSERT INTO dock_procure_vessel(vsl_nm,vsl_cd,origin,updated_at) "
                   "VALUES(?,?,'repair',datetime('now','localtime')) "
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
    # 🔴 생성 경로와 같은 정규화를 여기서도 건다. 안 걸면 `vessels.name` 원문이 다시 들어가
    #    dock_procure 행이 Dock 정본 표기에서 떨어져 나가고, 같은 배가 두 장 뜨는 실사고가 재현된다.
    p = _apply_canon_vsl_nm(p)
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


# 🔴 SVMS 저장본(REP_CD 있음) 삭제에 필요한 확인 문구. 문구 자체가 실제로 벌어지는 일을
#    말한다 — TRMT 목록에서만 사라지고 **SVMS 문서는 그대로 남는다**. TRMT 가 SVMS 수리신청서를
#    지우는 경로는 없다(있다고 착각하면 형이 SVMS 를 정리했다고 믿게 된다).
_DELETE_CONFIRM = 'TRMT에서만삭제'
# 러너가 **이미 물었다**. 지금 SVMS 와 대화하는 중일 수 있어 문서가 생겼는지 알 수 없고,
# 지우면 `/result` 콜백이 404 로 사라져 **영구히 모른다** → 확인 문구로도 뚫어 주지 않는다.
# 저장이 끝나거나 `/resolve`(사람이 SVMS 확인) 로 정리한 뒤 삭제한다.
_DELETE_INFLIGHT = ('saving',)
# REP_CD 없이 지워도 되는 상태 **whitelist**. 🔴 낯선 상태·`saved` 인데 REP_CD 가 없는 이상행은
# 막는다(fail-closed) — 무엇이 SVMS 에 있는지 모르는 행을 조용히 지우는 쪽이 더 위험하다.
# 🔴 `approved` 는 아직 SVMS 전이다: LIVE 러너는 `peek` 없이 claim 해서 status 를 먼저
#    `saving` 으로 돌린 뒤에만 SVMS 에 쓴다(`automation/svms-soa-opex/reqgen_save.py` 의
#    `/api/ext/repair-requests/approved`, `?peek=1` 은 DRY 전용). 삭제 직전에 물렸다면
#    아래 WHERE 가드가 0행 → 409 로 막는다. 막아만 두면 러너가 죽은 사이 `approved` 로
#    고인 행은 편집·삭제·resolve 가 전부 안 되는 영구 stuck 이 된다.
_DELETE_DRAFTISH = ('pending', 'failed', 'approved')
# 견적·업체선정·발주 = 돈 경로. 붙어 있으면 지우지 않고 사유를 돌려준다.
# 🔴 `stg_quote` 는 판단에 쓰지 않는다 — `repair_request_result` 가 저장 성공 시 항상 1 로
#    올리므로 "사람이 한 일" 이 아니다. 이걸 넣으면 저장된 건은 전부 삭제 불가가 된다.
_DELETE_BLOCKERS = (('stg_vendor', '벤더 제출'), ('stg_confirm', '벤더 컨펌·결재 상신'),
                    ('stg_order', '발주 완료'), ('sub_quotes', '제출견적'),
                    ('att_files', '견적서 첨부'), ('ord_vendors', '분할발주'),
                    ('quote_amt', '확정 견적금액'))


def _delete_blockers(db, dock_rid):
    """삭제를 막는 downstream 데이터 목록. dock_procure 행이 없으면 막을 것도 없다.

    🔴 **삭제와 같은 트랜잭션(`db`)에서** 읽는다. 밖에서 읽으면 검사 직후 벤더 제출·발주가
       붙어도 그대로 지워진다(`BEGIN IMMEDIATE` 가 그 사이 다른 writer 를 막아 준다).
    """
    if not dock_rid:
        return []
    cols = ','.join(k for k, _ in _DELETE_BLOCKERS)
    row = db.execute(f'SELECT {cols} FROM dock_procure WHERE id=?', (dock_rid,)).fetchone()
    if not row:
        return []
    return [label for key, label in _DELETE_BLOCKERS
            if row[key] not in (None, 0, '', '[]', '{}')]


@bp.route('/api/repair-requests/<int:rid>', methods=['DELETE'])
@admin_required
def repair_request_delete(rid):
    """신청 목록에서 삭제. 초안은 그대로, SVMS 저장본은 확인 문구를 받고 지운다(형 지시 2026-08-24).

    🔴 판정 **전부**를 삭제와 같은 트랜잭션 안에서 한다. 밖에서 읽으면 검사 직후 러너가 이 건을
       물거나(approved→saving→saved) 벤더 제출이 붙어도 그대로 지워진다. DELETE 에는 읽은
       status·rep_cd 를 WHERE 가드로 다시 걸어, 그 사이 바뀌면 0행 → 409 로 되돌린다.
    🔴 거부는 전부 fail-closed 다 — 낯선 상태는 "아마 초안" 으로 봐 주지 않는다.
    """
    d = request.get_json(silent=True) or {}
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('SELECT status,rep_cd,dock_rid FROM repair_request WHERE id=?',
                         (rid,)).fetchone()
        if not row:
            db.rollback(); return jsonify({'error': 'not found'}), 404
        status = row['status'] or ''
        rep_cd = row['rep_cd'] or None      # 빈 문자열 = 미저장(웹·iOS 규칙과 같은 결)
        if status in _DELETE_INFLIGHT:
            db.rollback()
            return jsonify({'error': 'SVMS 저장 진행 중인 건은 삭제할 수 없습니다. '
                                     '저장이 끝난 뒤(또는 SVMS 확인으로 상태를 정리한 뒤) 삭제하세요.',
                            'status': status}), 409
        if not rep_cd and status not in _DELETE_DRAFTISH:
            # SVMS 에 무엇이 있는지 모르는 이상행. 지우는 쪽이 더 위험하다.
            db.rollback()
            return jsonify({'error': f'삭제할 수 없는 상태입니다({status or "미지정"}). '
                                     'REP_CD 가 없는데 초안도 아닙니다 — 상태를 먼저 정리하세요.',
                            'status': status}), 409
        # 🔴 돈 경로는 초안이든 저장본이든 똑같이 막는다. REP_CD 가 있을 때만 검사하면
        #    저장 실패(failed)로 남은 건에 붙은 견적·발주가 조용히 사라진다.
        blockers = _delete_blockers(db, row['dock_rid'])
        if blockers:
            db.rollback()
            return jsonify({'error': '견적·업체선정·발주 데이터가 붙어 있어 삭제할 수 없습니다: '
                                     + ', '.join(blockers), 'blockers': blockers}), 409
        if rep_cd and d.get('confirmation') != _DELETE_CONFIRM:
            db.rollback()
            return jsonify({'error': f'SVMS 저장본 삭제에는 {_DELETE_CONFIRM} 확인이 필요합니다. '
                                     'SVMS 문서는 삭제되지 않고 TRMT 목록에서만 사라집니다.',
                            'need_confirmation': _DELETE_CONFIRM, 'rep_cd': rep_cd}), 400
        # 읽었던 상태 그대로일 때만 지운다. 바뀌었으면 0행 → 409(500 도, 조용한 삭제도 아니다).
        cur = db.execute('DELETE FROM repair_request WHERE id=? AND status=? '
                         'AND rep_cd IS ?', (rid, row['status'], row['rep_cd']))
        if not cur.rowcount:
            db.rollback()
            return jsonify({'error': '상태가 바뀌었습니다. 새로고침 후 다시 확인하세요.'}), 409
        # dock_procure 행은 이 신청서 전용 shim(견적엔진 진입점)이라 함께 지운다.
        # 🔴 `dock_procure_vessel` 은 건드리지 않는다 — 진짜 입거선박과 공용 키라
        #    지우면 Dock 발주현황에서 그 배가 통째로 사라진다.
        db.execute('DELETE FROM dock_procure WHERE id=?', (row['dock_rid'],)); db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify({'ok': True, 'id': rid, 'deleted': True,
                    'rep_cd': rep_cd, 'svms_kept': bool(rep_cd)})


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
