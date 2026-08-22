"""입거 Daily Report web/API boundary.

This module intentionally has no Dock Manager or SVMS write client.  The TRMT
draft is the source of truth; runner input is normalized, API-key protected,
and merged with the same optimistic-lock contract as the browser API.
"""
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
import uuid
import zipfile
from collections import namedtuple
from datetime import datetime
from io import BytesIO
from xml.etree import ElementTree

# 🔴 `send_file` 은 SVMS 러너 첨부 다운로드 라우트(`svms_attachment_bytes`)가 쓴다.
#    빠져 있으면 그 라우트가 요청을 받는 순간 NameError 500 이 되고(= 첨부가 영구히
#    SVMS 로 못 올라감) 다른 경로에서는 아무 증상이 없다. 경계 그래프 게이트가 잡았다.
from flask import (Blueprint, Response, jsonify, render_template, request, send_file,
                   send_from_directory)
from werkzeug.utils import secure_filename

from app_core import (ALLOWED_EXT, UPLOAD_DIR, app, ensure_heif_opener, execute, get_db,
                      query)
from helpers_shared import api_key_required, login_required

bp = Blueprint('routes_dock_daily', __name__)

FIXED = (
    ('shipyard', 'Shipyard', 10),
    ('survey', 'Survey', 30),
    ('vendor', 'Vendor', 40),
    ('remark', 'Remark', 50),
)
FIXED_KEYS = {x[0] for x in FIXED}
BLOCK_TYPES = {'item', 'paragraph', 'table', 'image'}
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
# The editor cards now auto-number every work item as "1) ", so any leading
# number the stored text already carries is dropped before the mail/SVMS
# renderers apply their own numbering.  Without this a saved "1) foo" would
# render as "1) 1) foo".
ITEM_NO_RE = re.compile(r'^\s*\d+\s*\)\s*')
MAX_ATTACHMENT = 20 * 1024 * 1024
MAX_OOXML_UNCOMPRESSED = 64 * 1024 * 1024
MAX_OOXML_PART = 8 * 1024 * 1024

# 🔴 자동 초안 수집 폐기 (형 지시 2026-08-21).  자동으로 끌어온 문장은 형이 쓰려던
# 문구가 아니어서, 보고서 본문은 사람이 쓰고 이어지는 작업만 "이전 일자 가져오기"로
# 복사하는 쪽으로 컨셉이 바뀌었다.
#
# 끄는 방식이 라우트 삭제가 아닌 이유: 이미 수집된 dock_auto 블록과 source_link 행이
# DB 에 남아 있고 그 provenance 표시(웹 배지 · iOS 배지)는 계속 동작해야 한다.  또 되돌릴
# 결정이면 이 상수 하나만 False 로 놓으면 되도록 남겨 둔다 -- 컬럼/러너/plist 는 지우지
# 않았다(`automation/dock-daily/runner.py`, `ai.openclaw.dock-daily` launchd job=disabled).
AUTO_DRAFT_INGEST_ENABLED = False


def _now():
    return datetime.now().replace(microsecond=0).isoformat(' ')


def _json(value, default):
    try:
        val = json.loads(value) if isinstance(value, str) else value
        return val if val is not None else default
    except (TypeError, ValueError):
        return default


def _date(value, required=False):
    value = (str(value or '')).strip()
    if not value and not required:
        return None
    if not DATE_RE.match(value):
        raise ValueError('date must be YYYY-MM-DD')
    return value


def _body():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def _dict(row):
    return dict(row) if row else None


_DRYDOCK_ID_RE = re.compile(r'^v_[A-Za-z0-9][A-Za-z0-9_.:-]*$')


def _drydock_id(value, field='drydock vessel id'):
    """Validate a Dock Manager opaque id without coercing it to an integer."""
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not _DRYDOCK_ID_RE.fullmatch(value.strip()):
        raise ValueError('%s must be a string beginning with v_' % field)
    return value.strip()


def _drydock_id_list(value, field='drydock vessel ids'):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('%s must be a JSON array' % field)
    out = []
    for item in value:
        item = _drydock_id(item, field)
        if item and item not in out:
            out.append(item)
    return out


def _project_response(row):
    out = _dict(row)
    if not out:
        return out
    out['drydock_source_vessel_ids'] = _json(out.get('drydock_source_vessel_ids_json'), [])
    # This is the web/runner contract: callers must not have to decode JSON
    # stored in the project row. Keep the original DB column for compatibility.
    ids = list(out['drydock_source_vessel_ids'])
    primary = out.get('drydock_primary_vessel_id')
    if primary and primary not in ids:
        ids.insert(0, primary)
    out['dock_manager_project_ids'] = ids
    if out.get('id'):
        out['sections'] = _sections(out['id'])
    return out


def _sections(project_id, include_disabled=True):
    q = ('SELECT id, project_id, section_key, label, sort_order, kind, enabled '
         'FROM dock_daily_section_def WHERE project_id=?')
    if not include_disabled:
        q += ' AND enabled=1'
    return [dict(r) for r in query(q + ' ORDER BY sort_order, id', (project_id,))]


def _seed_sections(db, project_id):
    for key, label, order in FIXED:
        db.execute('''INSERT OR IGNORE INTO dock_daily_section_def
                      (project_id, section_key, label, sort_order, kind, enabled)
                      VALUES (?,?,?,?,?,1)''', (project_id, key, label, order, 'fixed'))


def _project(pid):
    return query('''SELECT p.*, v.name vessel_name, v.vsl_cd vessel_vsl_cd, v.imo vessel_imo
                    FROM dock_daily_project p JOIN vessels v ON v.id=p.vessel_id
                    WHERE p.id=?''', (pid,), one=True)


def _report(rid):
    return query('''SELECT r.*, p.title project_title, p.vessel_id, p.vsl_cd project_vsl_cd,
                           p.imo project_imo, p.berthing_date, p.dock_in_date,
                           p.dock_out_date, p.departure_date, p.svms_dk_cd,
                           v.name vessel_name
                    FROM dock_daily_report r JOIN dock_daily_project p ON p.id=r.project_id
                    JOIN vessels v ON v.id=p.vessel_id WHERE r.id=?''', (rid,), one=True)


def _snapshot(rid):
    r = _report(rid)
    if not r:
        return {}
    blocks = [dict(x) for x in query(
        'SELECT * FROM dock_daily_block WHERE report_id=? ORDER BY section_key, sort_order, id', (rid,))]
    for x in blocks:
        x['content'] = _json(x.pop('content_json', '{}'), {})
    links = [dict(x) for x in query(
        'SELECT * FROM dock_daily_source_link WHERE report_id=? ORDER BY id', (rid,))]
    return {'report': {k: r[k] for k in ('id', 'project_id', 'report_date', 'status', 'revision',
                                         'email_subject', 'email_intro', 'safety_footer')},
            'blocks': blocks, 'sources': links}


def _svms_result_note(raw):
    """러너 결과에서 화면이 읽을 한 줄만 뽑는다.

    화면이 `svms_result_json` 을 직접 읽으면 러너 내부 필드 이름이 UI 계약이 되고, 첨부
    실패 목록 같은 구조가 그대로 노출된다.

    🔴 `partial` 은 **본문은 SVMS 에 들어갔고 첨부만 빠진** 상태다. 실패 건수를 반드시
       남긴다 -- 이 문장이 없으면 형이 첨부까지 올라간 줄 알고 SVMS 를 그대로 넘긴다.
    """
    data = _json(raw, None)
    if not isinstance(data, dict):
        return None
    parts = [str(data.get(key) or '').strip() for key in ('error', 'note')]
    att = data.get('attachments')
    if isinstance(att, dict) and att.get('failed'):
        # 🔴 `failed` 가 list 라고 가정하면 안 된다(올마이트). 러너가 숫자·문자열·dict 를
        #    보내면 `len()` 이 TypeError 로 터지고, 그건 **보고서 GET 500** 이 된다
        #    (= 상태를 확인하려는 순간 화면이 죽는다). 셀 수 없는 형태는 건수 없이 적는다.
        failed = att['failed']
        count = len(failed) if isinstance(failed, (list, tuple, set, dict)) else None
        parts.append('첨부 %d건 업로드 실패' % count if count is not None else '첨부 업로드 실패')
    return ' · '.join([p for p in parts if p])[:400] or None


def _report_json(rid):
    r = _report(rid)
    if not r:
        return None
    out = dict(r)
    # 🔴 claim token 은 맥 러너의 능력치다(이 토큰으로 ext API 가 첨부 원본을 내려준다).
    #    보고서 JSON 에 실으면 화면·앱 캐시·로그로 퍼지므로 응답 경계에서 걷어낸다.
    #    result JSON 도 원본을 넘기지 않고 사람이 읽을 한 줄(`svms_error`)로만 준다.
    out.pop('svms_claim_token', None)
    out['svms_error'] = _svms_result_note(out.pop('svms_result_json', None))
    # Keep the direct date fields for the existing web contract while also
    # exposing the nested shape consumed by the iOS client.
    out['itinerary'] = {
        'berthing': out.get('berthing_date'),
        'dry_dock_in': out.get('dock_in_date'),
        'dry_dock_out': out.get('dock_out_date'),
        'departure': out.get('departure_date'),
    }
    for key in ('auto_snapshot_json',):
        out[key] = _json(out.get(key), {})
    out['sections'] = _sections(r['project_id'])
    out['blocks'] = []
    for b in query('SELECT * FROM dock_daily_block WHERE report_id=? ORDER BY section_key, sort_order, id', (rid,)):
        x = dict(b)
        x['content'] = _json(x.pop('content_json', '{}'), {})
        out['blocks'].append(x)
    out['source_links'] = [dict(x) for x in query(
        'SELECT * FROM dock_daily_source_link WHERE report_id=? ORDER BY id', (rid,))]
    out['attachments'] = [dict(x) for x in query(
        'SELECT * FROM dock_daily_attachment WHERE report_id=? AND deleted_at IS NULL ORDER BY id', (rid,))]
    return out


def _auto_subject(vessel_name, report_date):
    """The mail subject a report is created with.

    Shared with the date-correction path in `report_put` on purpose.  That path
    decides whether a stored subject is still the generated one before it
    rewrites the date inside it; if the two sites built the string differently
    a corrected report would keep the wrong date in its subject forever while
    looking untouched.
    """
    return '[Dock] M/T %s - Dock Daily Report (%s)' % (vessel_name, _subject_date(report_date))


def _subject_date(report_date):
    """The `(8/20)` fragment the generated subject ends with."""
    return '%s/%s' % (report_date[5:7].lstrip('0'), report_date[8:10].lstrip('0'))


def _subject_with_date(subject, old_date, new_date):
    """Move the date inside a still-generated subject, or return None to leave it alone.

    Why the shape is matched instead of the whole string being compared to
    `_auto_subject(vessel_name, old_date)`: the vessel can be renamed after the
    report is created, and then no stored subject equals the freshly generated
    one -- so a date correction would silently keep `(8/20)` on an 8/30 report
    (올마이트 지적 2026-08-21).  Matching the generated prefix plus the exact
    old-date tail is narrow enough that a hand-written subject is not touched:
    it has to look like `[Dock] ... Dock Daily Report (8/20)` to qualify.

    A hand-typed subject that happens to be exactly that shape does get its date
    moved, and that is the right outcome -- the only edit is the date the author
    themself put there.
    """
    tail = ' (%s)' % _subject_date(old_date)
    if not subject or not subject.startswith('[Dock] ') or 'Dock Daily Report' not in subject:
        return None
    if not subject.endswith(tail):
        return None
    return subject[:-len(tail)] + ' (%s)' % _subject_date(new_date)


def _create_report(db, project_id, report_date, actor='system'):
    p = _project(project_id)
    if not p:
        return None
    existing = db.execute('SELECT id FROM dock_daily_report WHERE project_id=? AND report_date=?',
                          (project_id, report_date)).fetchone()
    if existing:
        return existing['id']
    subject = _auto_subject(p['vessel_name'], report_date)
    cur = db.execute('''INSERT INTO dock_daily_report
        (project_id, report_date, status, revision, auto_snapshot_json, email_subject, email_intro, safety_footer)
        VALUES (?,?,'auto_draft',1,'{}',?,?,?)''',
        (project_id, report_date, subject,
         '안녕하십니까.\n아래와 같이 금일 입거공사 진행사항을 보고드립니다.', ''))
    rid = cur.lastrowid
    db.execute('INSERT INTO dock_daily_report_revision(report_id,revision,snapshot_json,actor) VALUES (?,?,?,?)',
               (rid, 1, json.dumps(_snapshot(rid), ensure_ascii=False), actor))
    return rid


def _error(message, status=400, **extra):
    """HTTP 상태는 `status` 다.

    전엔 이 인자 이름이 `code` 였는데, 응답 본문에도 기계가 읽는 `code` 를 싣게 되면서
    `_error(msg, 409, code='date_taken')` 이 같은 인자에 두 번 바인딩돼 500 이 됐다.
    상태코드는 위치인자로만 쓰이고 있어 이름만 바꾼다.
    """
    out = {'error': message}
    out.update(extra)
    return jsonify(out), status


def _validate_active_window(auto_generate, active_from, active_to):
    """Validate the final persisted scheduler window, not just patch fields."""
    if active_from and active_to and active_from > active_to:
        raise ValueError('active_from must not be after active_to')
    if auto_generate and (not active_from or not active_to):
        raise ValueError('active_from and active_to are required when auto_generate is enabled')


@bp.route('/dock-daily')
@login_required
def dock_daily_page():
    return render_template('dock_daily.html')


@bp.route('/api/dock-daily/projects', methods=['GET'])
@login_required
def projects_get():
    rows = query('''SELECT p.*, v.name vessel_name, v.vsl_cd vessel_vsl_cd, v.imo vessel_imo,
                           (SELECT COUNT(*) FROM dock_daily_report r WHERE r.project_id=p.id) report_count
                    FROM dock_daily_project p JOIN vessels v ON v.id=p.vessel_id
                    ORDER BY p.active_from DESC, p.id DESC''')
    return jsonify([_project_response(x) for x in rows])


@bp.route('/api/dock-daily/projects', methods=['POST'])
@login_required
def projects_post():
    data = _body()
    try:
        vessel_id = int(data.get('vessel_id'))
        title = str(data.get('title') or '').strip()
        if not title or not query('SELECT id FROM vessels WHERE id=? AND active=1', (vessel_id,), one=True):
            raise ValueError('active vessel_id and title are required')
        dates = {k: _date(data.get(k)) for k in
                 ('berthing_date', 'dock_in_date', 'dock_out_date', 'departure_date', 'active_from', 'active_to')}
        _validate_active_window(bool(data.get('auto_generate')), dates['active_from'], dates['active_to'])
        primary = _drydock_id(data.get('drydock_primary_vessel_id'))
        source_ids = _drydock_id_list(data.get('drydock_source_vessel_ids',
                                                data.get('dock_manager_project_ids', [])))
        if not primary and data.get('dock_manager_project_ids'):
            primary = source_ids[0] if source_ids else None
    except (TypeError, ValueError) as e:
        return _error(str(e))
    v = query('SELECT vsl_cd, imo FROM vessels WHERE id=?', (vessel_id,), one=True)
    db = get_db()
    try:
        db.execute('BEGIN')
        cur = db.execute('''INSERT INTO dock_daily_project
            (vessel_id,vsl_cd,imo,title,berthing_date,dock_in_date,dock_out_date,departure_date,
             active_from,active_to,auto_generate,drydock_primary_vessel_id,drydock_source_vessel_ids_json,svms_dk_cd)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (vessel_id, data.get('vsl_cd') or v['vsl_cd'], data.get('imo') or v['imo'], title,
             dates['berthing_date'], dates['dock_in_date'], dates['dock_out_date'], dates['departure_date'],
             dates['active_from'], dates['active_to'], 1 if data.get('auto_generate') else 0,
             primary, json.dumps(source_ids, ensure_ascii=False),
             data.get('svms_dk_cd')))
        pid = cur.lastrowid
        _seed_sections(db, pid)
        for i, item in enumerate(data.get('special_sections') or data.get('sections') or []):
            if not isinstance(item, dict):
                continue
            key = str(item.get('section_key') or '').strip()
            label = str(item.get('label') or key).strip()
            if not key or key in FIXED_KEYS or not re.match(r'^[a-z0-9][a-z0-9_.-]{0,63}$', key):
                continue
            db.execute('''INSERT OR IGNORE INTO dock_daily_section_def
                          (project_id,section_key,label,sort_order,kind,enabled) VALUES (?,?,?,?,?,?)''',
                       (pid, key, label, 20 + i, 'special', 1 if item.get('enabled', True) else 0))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(_project_response(_project(pid))), 201


def _final_content_dates(db, pid, section_key):
    """확정본에 이 섹션의 내용이 남아 있는 날짜들.

    🔴 `section_delete` 만 이걸 봤고 `enabled=0` 은 안 봤다.  두 길의 결과는 형이 보는
    화면에서 똑같다 -- 감춘 섹션은 메일·SVMS 렌더에서 그냥 빠지므로, 확정본에 든 표
    한 장이 **삭제는 409 로 막히는데 체크 해제로는 조용히 사라진다**.  그러면 "확정" 이
    뜻하는 게 없어진다.  확정취소 → 정리 → 재확정이 정상 경로다.
    """
    return sorted({x['report_date'] for x in db.execute(
        "SELECT DISTINCT r.report_date FROM dock_daily_block b"
        " JOIN dock_daily_report r ON r.id=b.report_id"
        " WHERE r.project_id=? AND b.section_key=? AND r.status='final'",
        (pid, section_key)).fetchall()})


def _bump_sibling_reports(db, pid, exclude_id=None):
    """섹션 정의가 바뀌면 그 프로젝트의 열려 있는 보고서 revision 을 모두 올린다.

    🔴 섹션 목록은 **프로젝트** 값인데 CAS 는 보고서 하나만 잠근다.  올리지 않으면 다른
    일자를 열어 둔 기기가 옛 목록(옛 순서·옛 enabled)을 그대로 되돌려 보내 방금 바꾼
    설정을 조용히 되돌린다 -- 양쪽 어디에도 409 가 뜨지 않는다.  순서를 보낼 때 목록
    전체를 echo 하는 클라이언트가 있으니 이건 예외가 아니라 기본 경로다.
    `section_delete` 가 블록 삭제 뒤 하는 일과 같은 이유다.

    🔴 **확정본도 올린다.**  내 첫 구현은 "PUT 이 `final_locked` 라 되살릴 주체가 없다" 고
    보고 건너뛰었는데 틀렸다(올마이트 blocking) -- `report_status` 가 확정취소를 열어 뒀고,
    그 라우트는 revision 만 맞으면 통과한다.  건너뛰면 확정본의 revision 이 그대로 유효해서
    낡은 기기가 ①확정취소 성공 ②그 응답으로 얻은 revision 으로 옛 섹션 목록 echo 라는
    2단계로 방금 바꾼 설정을 되돌릴 수 있다.  올려 두면 ①이 먼저 409 로 끊긴다.
    비용은 확정본 스냅샷 행 한 줄이고, 되돌림은 형이 눈으로 못 찾는 손실이다.
    """
    for row in db.execute("SELECT id FROM dock_daily_report"
                          " WHERE project_id=?", (pid,)).fetchall():
        rid = row['id']
        if rid == exclude_id:
            continue
        db.execute("UPDATE dock_daily_report SET revision=revision+1,"
                   " updated_at=datetime('now','localtime') WHERE id=?", (rid,))
        current = db.execute('SELECT revision FROM dock_daily_report WHERE id=?',
                             (rid,)).fetchone()['revision']
        db.execute('INSERT INTO dock_daily_report_revision(report_id,revision,snapshot_json,actor)'
                   ' VALUES (?,?,?,?)',
                   (rid, current, json.dumps(_snapshot(rid), ensure_ascii=False), session_actor()))


@bp.route('/api/dock-daily/projects/<int:pid>', methods=['PATCH'])
@login_required
def projects_patch(pid):
    current = _project(pid)
    if not current:
        return _error('project not found', 404)
    data = _body()
    parsed_dates = {}
    try:
        for key in ('berthing_date', 'dock_in_date', 'dock_out_date', 'departure_date', 'active_from', 'active_to'):
            if key in data:
                parsed_dates[key] = _date(data[key])
        final_auto = bool(data.get('auto_generate')) if 'auto_generate' in data else bool(current['auto_generate'])
        final_from = parsed_dates.get('active_from', current['active_from'])
        final_to = parsed_dates.get('active_to', current['active_to'])
        _validate_active_window(final_auto, final_from, final_to)
    except ValueError as e:
        return _error(str(e))
    allowed = {'title', 'vsl_cd', 'imo', 'svms_dk_cd', 'auto_generate', 'drydock_primary_vessel_id'}
    sets, vals = [], []
    for key in allowed:
        if key in data:
            sets.append('%s=?' % key)
            if key == 'drydock_primary_vessel_id':
                try: vals.append(_drydock_id(data[key]))
                except ValueError as e: return _error(str(e))
            else:
                vals.append(1 if key == 'auto_generate' and data[key] else 0 if key == 'auto_generate' else data[key])
    source_ids = None
    if 'drydock_source_vessel_ids' in data or 'dock_manager_project_ids' in data:
        try:
            source_ids = _drydock_id_list(data.get('drydock_source_vessel_ids',
                                                   data.get('dock_manager_project_ids')))
        except ValueError as e:
            return _error(str(e))
        sets.append('drydock_source_vessel_ids_json=?')
        vals.append(json.dumps(source_ids, ensure_ascii=False))
    for key in ('berthing_date', 'dock_in_date', 'dock_out_date', 'departure_date', 'active_from', 'active_to'):
        if key in data:
            sets.append('%s=?' % key); vals.append(parsed_dates[key])
    # 🔴 한 요청에 프로젝트 본문과 섹션 여러 개가 들어온다.  `execute()` 는 문장마다
    # commit 하므로(app_core), 중간에서 하나라도 터지면 앞의 UPDATE·INSERT 는 이미
    # 저장된 채로 500 이 나간다 -- 호출자는 "아무것도 저장 안 됨" 으로 읽고 재시도해
    # 같은 일을 두 번 한다.  `projects_post` 와 같은 한 트랜잭션으로 묶는다.
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        if sets:
            sets.append("updated_at=datetime('now','localtime')")
            db.execute('UPDATE dock_daily_project SET %s WHERE id=?' % ','.join(sets), (*vals, pid))
        section_defs_changed = False
        for item in data.get('sections') or []:
            if not isinstance(item, dict) or not item.get('section_key'):
                continue
            key = str(item['section_key'])
            row = db.execute('SELECT kind, label, enabled FROM dock_daily_section_def'
                             ' WHERE project_id=? AND section_key=?', (pid, key)).fetchone()
            if row and row['kind'] == 'special':
                # Fixed sections are part of the report contract. Only optional
                # project-specific sections may be renamed or disabled.
                enabled = None if 'enabled' not in item else (1 if item['enabled'] else 0)
                if enabled == 0 and row['enabled']:
                    finals = _final_content_dates(db, pid, key)
                    if finals:
                        db.rollback()
                        return _error('확정된 보고서에 이 섹션의 내용이 있습니다.'
                                      ' 확정을 취소한 뒤 감추세요.',
                                      409, code='final_report_has_content', dates=finals)
                db.execute('''UPDATE dock_daily_section_def SET label=COALESCE(?,label), enabled=COALESCE(?,enabled)
                              WHERE project_id=? AND section_key=?''',
                           (item.get('label'), enabled, pid, key))
                label = item.get('label')
                if (label is not None and label != row['label']) or (
                        enabled is not None and enabled != row['enabled']):
                    section_defs_changed = True
            elif key not in FIXED_KEYS and re.match(r'^[a-z0-9][a-z0-9_.-]{0,63}$', key):
                try:
                    order = int(item.get('sort_order') or 20)
                except (TypeError, ValueError):
                    # 400 으로 끊는다.  전엔 여기서 ValueError 가 그대로 올라가 500 이 됐고,
                    # 그 시점에 프로젝트 UPDATE 와 앞선 섹션 INSERT 는 이미 커밋돼 있었다.
                    db.rollback(); return _error('sort_order must be an integer')
                cur = db.execute('''INSERT OR IGNORE INTO dock_daily_section_def
                                    (project_id,section_key,label,sort_order,kind,enabled)
                                    VALUES (?,?,?,?,?,?)''',
                                 (pid, key, str(item.get('label') or key), order, 'special',
                                  1 if item.get('enabled', True) else 0))
                if cur.rowcount:
                    section_defs_changed = True
        if section_defs_changed:
            _bump_sibling_reports(db, pid)
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_project_response(_project(pid)))


@bp.post('/api/dock-daily/projects/<int:pid>/sections')
@login_required
def create_section(pid):
    """제목만 받아 새 섹션을 만든다(형 지시 2026-08-21).

    표는 다른 카드의 하위항목이 아니라 **제목을 가진 자기 섹션**이어야 한다.  그 섹션이
    곧 special 섹션이므로 새 저장소는 필요 없지만, `section_key` 를 클라이언트가 만들면
    웹과 앱에 규칙이 두 벌 생기고 서로 다른 키를 뱉는다.  그래서 키는 서버가 만든다.

    프로젝트 PATCH 로도 같은 일을 할 수 있지만 그 라우트는 `title` 같은 프로젝트 본문을
    함께 받는다 -- 앱에서 섹션 하나 추가하려고 프로젝트 전체를 보내면 빈 값으로 덮을
    위험이 생기므로 최소 입력만 받는 전용 라우트를 둔다.
    """
    if not _project(pid):
        # `_project` 는 없으면 None 을 준다(abort 하지 않는다). 확인 없이 넘기면
        # 섹션 행만 남고 응답을 만들다 500 이 난다.
        return _error('project not found', 404)
    data = request.get_json(silent=True) or {}
    label = str(data.get('label') or '').strip()
    if not label:
        return _error('label is required')
    if len(label) > 60:
        return _error('label is too long')
    # 제목은 대개 한글이라 제목에서 key 를 만들 수 없다.  이름은 형이 쓴 그대로 두고
    # key 는 프로젝트 안에서만 유일하면 되므로 남는 번호를 쓴다.
    #
    # 🔴 SELECT→계산→INSERT 는 그 자체로 경쟁 구간이다.  같은 프로젝트에 두 요청이
    # 겹치면 둘 다 같은 `sec_N` 을 골라 `UNIQUE(project_id,section_key)` 를 때리고,
    # 잡지 않으면 형에게 500 이 간다.  충돌은 정상 상황이므로 다음 번호로 다시 넣는다.
    for _ in range(20):
        used = {r['section_key'] for r in
                query('SELECT section_key FROM dock_daily_section_def WHERE project_id=?', (pid,))}
        index = 1
        while ('sec_%d' % index) in used or ('sec_%d' % index) in FIXED_KEYS:
            index += 1
        key = 'sec_%d' % index
        top = query('SELECT MAX(sort_order) AS top FROM dock_daily_section_def WHERE project_id=?',
                    (pid,), one=True)
        # 맨 뒤에 붙인다.  화면(`sort_order`)·메일·SVMS 가 모두 이 값을 따르므로 새 섹션은
        # 형이 옮기기 전까지 맨 아래에 보이고 메일에서도 맨 아래로 나간다.
        try:
            execute('INSERT INTO dock_daily_section_def'
                    ' (project_id,section_key,label,sort_order,kind,enabled) VALUES (?,?,?,?,?,?)',
                    (pid, key, label,
                     int(top['top'] if top and top['top'] is not None else 19) + 1, 'special', 1))
        except sqlite3.IntegrityError:
            continue
        body = _project_response(_project(pid))
        # 🔴 방금 만든 key 를 명시한다.  클라이언트가 응답 목록의 차집합으로 되짚으면,
        # 다른 기기가 같은 순간에 섹션을 추가했을 때 **남의 섹션**을 고른다(올마이트 지적
        # 2026-08-22).  위 루프는 충돌 시 번호를 건너뛰므로 규칙을 다시 계산하는 것도 틀린다.
        body['created_section_key'] = key
        return jsonify(body), 201
    return _error('section key is exhausted', 409)


@bp.route('/api/dock-daily/projects/<int:pid>/sections/<section_key>', methods=['DELETE'])
@login_required
def section_delete(pid, section_key):
    """섹션을 목록에서 아주 지운다(형 지시 2026-08-22).

    지금까지 없앨 방법은 `enabled=0` 뿐이었다.  숨긴 섹션은 화면·메일에서 안 보일 뿐
    목록에는 그대로 남아, 잘못 만든 빈 카드가 프로젝트마다 쌓인다.

    🔴 **블록도 같이 지운다.**  `dock_daily_block.section_key` 는 텍스트일 뿐 FK 가
    아니라서 섹션 행만 지우면 블록이 고아로 남는다.  그냥 두면 두 가지가 터진다 --
    ① 어떤 화면에서도 안 보이는데 DB 에는 남아 있고, ② `create_section` 이 **비어 있는
    가장 작은 번호**를 다시 쓰므로 `sec_1` 을 지운 뒤 새 섹션을 만들면 그 자리에 옛
    블록들이 되살아난다.

    🔴 확정본에 내용이 있으면 거절한다.  확정된 보고서의 본문을 지우는 길을 열어 두면
    "확정" 이 뜻하는 게 없어진다.  확정 취소 -> 삭제 -> 재확정이 정상 경로다.

    내용이 있는 섹션은 `confirm=delete-section` 을 요구한다.  형이 지우는 건 대개 빈
    카드이므로 빈 섹션은 한 번에 지워지고, 내용이 있을 때만 앱이 개수를 보여준다.
    """
    if not _project(pid):
        return _error('project not found', 404)
    confirm = _body().get('confirm')
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        section = db.execute('SELECT id, kind, label FROM dock_daily_section_def'
                             ' WHERE project_id=? AND section_key=?', (pid, section_key)).fetchone()
        if not section:
            db.rollback(); return _error('section not found', 404)
        if section['kind'] != 'special':
            # Shipyard/Survey/Vendor/Remark 는 메일 서식과 SVMS 필드가 이름으로 물려
            # 있는 보고서 계약이다. 지우면 그 자리에 다시 만들 수단도 없다.
            db.rollback()
            return _error('fixed sections cannot be deleted', 409, code='fixed_section')
        # 이 프로젝트의 모든 보고서에서 이 섹션에 든 블록.  일자별이 아니라 프로젝트
        # 전체다 -- 섹션 자체가 프로젝트 값이므로 지우면 모든 일자에서 사라진다.
        rows = db.execute('''SELECT b.id AS block_id, b.report_id, r.report_date, r.status
                             FROM dock_daily_block b
                             JOIN dock_daily_report r ON r.id=b.report_id
                             WHERE r.project_id=? AND b.section_key=?''',
                          (pid, section_key)).fetchall()
        finals = sorted({x['report_date'] for x in rows if x['status'] == 'final'})
        if finals:
            db.rollback()
            return _error('확정된 보고서에 이 섹션의 내용이 있습니다. 확정을 취소한 뒤 지우세요.',
                          409, code='final_report_has_content', dates=finals)
        if rows and confirm != 'delete-section':
            db.rollback()
            return _error('section is not empty', 409, code='section_not_empty',
                          blocks=len(rows),
                          dates=sorted({x['report_date'] for x in rows}))
        report_ids = sorted({x['report_id'] for x in rows})
        if rows:
            # 블록 삭제는 FK(`ON DELETE SET NULL`)로 첨부의 `block_id` 만 끊는다.  첨부
            # 자체는 블록 삭제 op 와 같은 계약으로 tombstone 한다 -- 파일은 남기고
            # 보고서 삭제 때 함께 쓸린다(`_delete_cascade` 가 `deleted_at` 행도 센다).
            db.executemany("UPDATE dock_daily_attachment SET deleted_at=datetime('now','localtime')"
                           ' WHERE block_id=? AND deleted_at IS NULL',
                           [(x['block_id'],) for x in rows])
            db.executemany('DELETE FROM dock_daily_block WHERE id=?',
                           [(x['block_id'],) for x in rows])
        for rid in report_ids:
            # 🔴 revision 을 올린다.  안 올리면 그 보고서를 열어 둔 다른 기기가 옛
            # revision 으로 저장에 성공해, 방금 지운 블록을 자기 화면에서 되살린다.
            db.execute("UPDATE dock_daily_report SET revision=revision+1,"
                       " updated_at=datetime('now','localtime') WHERE id=?", (rid,))
            db.execute('INSERT INTO dock_daily_report_revision(report_id,revision,snapshot_json,actor)'
                       ' VALUES (?,?,?,?)',
                       (rid, db.execute('SELECT revision FROM dock_daily_report WHERE id=?',
                                        (rid,)).fetchone()['revision'],
                        json.dumps(_snapshot(rid), ensure_ascii=False), session_actor()))
        cur = db.execute('DELETE FROM dock_daily_section_def WHERE id=?', (section['id'],))
        if cur.rowcount != 1:
            db.rollback(); return _error('section not found', 404)
        db.commit()
    except Exception:
        db.rollback(); raise
    body = _project_response(_project(pid))
    body['deleted_section_key'] = section_key
    body['deleted_blocks'] = len(rows)
    return jsonify(body)


def _purge_files(stored_names):
    """Remove attachment blobs; returns how many were actually unlinked.

    Called post-commit on purpose: a file removed before a transaction that
    then rolls back would leave a row pointing at nothing.  A blob that cannot
    be removed is logged and skipped rather than raised -- the row is already
    gone and the request already succeeded, so raising would report a false
    failure.  The count returned is the real one; reporting the number of rows
    found instead would hide a leak that nothing else can detect.

    Names are re-validated even though we generate them, because the value
    travels through the DB between write and delete.  `basename` plus the
    realpath containment check keeps a crafted name inside UPLOAD_DIR; a
    symlink swapped in between the check and the unlink is still possible and
    is not defended here (it needs write access to UPLOAD_DIR itself).
    """
    root = os.path.realpath(UPLOAD_DIR)
    removed = 0
    for name in stored_names:
        if not name or name != os.path.basename(name):
            app.logger.warning('dock-daily: refusing odd attachment name %r', name)
            continue
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath((path, root)) != root:
            app.logger.warning('dock-daily: attachment %r escapes UPLOAD_DIR', name)
            continue
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            app.logger.warning('dock-daily: attachment blob %r left on disk: %s', name, exc)
    return removed


_Cascade = namedtuple('_Cascade', 'label select_row select_blobs delete_row')

# Each cascade carries its three statements as callables, not as SQL strings in
# a table.  Two reasons, in order:
#   * `'... FROM %s' % table` is a dynamic-SQL site even when every caller
#     passes a literal, and so is `db.execute(target.sql, …)` -- the scanner in
#     tests/test_sql_construction_contract.py reads the call site, not the
#     provenance.  Keeping the SQL literal *at the execute* leaves that fence
#     exactly where it was instead of spending a review exemption on it.
#   * The project cascade's blob query is not the report one with a table name
#     swapped; it has to reach through dock_daily_report. A per-target function
#     says that plainly.
_CASCADE_PROJECT = _Cascade(
    'project',
    lambda db, i: db.execute('SELECT * FROM dock_daily_project WHERE id=?', (i,)),
    lambda db, i: db.execute('SELECT stored_name FROM dock_daily_attachment WHERE report_id IN'
                             ' (SELECT id FROM dock_daily_report WHERE project_id=?)', (i,)),
    lambda db, i: db.execute('DELETE FROM dock_daily_project WHERE id=?', (i,)))

_CASCADE_REPORT = _Cascade(
    'report',
    lambda db, i: db.execute('SELECT * FROM dock_daily_report WHERE id=?', (i,)),
    lambda db, i: db.execute('SELECT stored_name FROM dock_daily_attachment WHERE report_id=?', (i,)),
    lambda db, i: db.execute('DELETE FROM dock_daily_report WHERE id=?', (i,)))

# The row read carries its report's status so the lock can be checked inside the
# transaction like the other two cascades.  The join is LEFT on purpose: a row
# whose report is already gone should still be deletable, or its blob is stranded
# with no route that can reach it.
_CASCADE_ATTACHMENT = _Cascade(
    'attachment',
    lambda db, i: db.execute('SELECT a.*, r.status AS report_status FROM dock_daily_attachment a'
                             ' LEFT JOIN dock_daily_report r ON r.id=a.report_id WHERE a.id=?', (i,)),
    lambda db, i: db.execute('SELECT stored_name FROM dock_daily_attachment WHERE id=?', (i,)),
    lambda db, i: db.execute('DELETE FROM dock_daily_attachment WHERE id=?', (i,)))


def _delete_cascade(target, row_id, guard=None, also=None):
    """Delete one dock-daily row and purge the attachment blobs under it.

    Row removal cascades in the schema (`PRAGMA foreign_keys = ON` is set per
    request in app_core), but blobs on disk do not, so their names are read
    inside the same `BEGIN IMMEDIATE` as the delete.  Reading them before the
    transaction -- or checking the row's status before it -- opened a window
    where an upload or a 확정 landing in between was decided on stale state:
    the uploaded file would keep its blob while its row cascaded away, an
    orphan nothing would ever collect.

    `deleted_at` rows are included: it only hides a row from the report, the
    file stays on disk, so skipping them would leak them forever.

    `guard(row)` runs inside the transaction and may return an error response
    to abort.  `also(db, row)` runs there too, after the delete and before the
    commit, for repair work that has to land atomically with it -- a reference
    cleared in a second transaction could be seen half-done by a render in
    between.  Returns (payload, error); a rowcount of 0 means another request
    won the race and is reported as 404, not as a success that did nothing.
    """
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = target.select_row(db, row_id).fetchone()
        if not row:
            db.rollback()
            return None, _error('%s not found' % target.label, 404)
        if guard:
            refused = guard(row)
            if refused:
                db.rollback()
                return None, refused
        names = [r['stored_name'] for r in target.select_blobs(db, row_id).fetchall()]
        cur = target.delete_row(db, row_id)
        if cur.rowcount != 1:
            db.rollback()
            return None, _error('%s not found' % target.label, 404)
        if also:
            also(db, row)
        db.commit()
    except Exception:
        db.rollback(); raise
    removed = _purge_files(names)
    return {'deleted': row_id, 'attachments_found': len(names),
            'attachments_removed': removed}, None


@bp.route('/api/dock-daily/projects/<int:pid>', methods=['DELETE'])
@login_required
def project_delete(pid):
    """Delete a project with every report, block, source link, revision and
    attachment under it.

    There is no undo and no soft-delete column here, so the caller has to name
    the operation.  A mis-click on the sidebar row or a stray fetch must not be
    able to erase a whole dock.
    """
    if _body().get('confirm') != 'delete-project':
        # Checked before the existence lookup so a bare probe cannot use the
        # 404/409 split to enumerate project ids.
        return _error('confirm=delete-project is required', 409)
    payload, err = _delete_cascade(_CASCADE_PROJECT, pid)
    return err if err else jsonify(payload)


@bp.route('/api/dock-daily/projects/<int:pid>/reports')
@login_required
def reports_get(pid):
    if not _project(pid):
        return _error('project not found', 404)
    return jsonify([dict(x) for x in query(
        'SELECT id,project_id,report_date,status,revision,source_changed_after_final,updated_at,'
        # 목록에도 SVMS 반영 상태를 싣는다 -- 어느 일자가 이미 SVMS 로 넘어갔는지 보고서를
        # 하나씩 열어보지 않고 알아야 한다. 🔴 여기서는 컬럼을 열거해야 한다(`SELECT *`
        # 로 넓히면 `svms_claim_token` 까지 목록에 실린다).
        'svms_sync_status,svms_dk_seq '
        'FROM dock_daily_report WHERE project_id=? ORDER BY report_date DESC', (pid,))])


@bp.route('/api/dock-daily/projects/<int:pid>/reports/generate', methods=['POST'])
@login_required
def report_generate(pid):
    if not _project(pid):
        return _error('project not found', 404)
    data = _body()
    try:
        report_date = _date(data.get('report_date'), True)
    except ValueError as e:
        return _error(str(e))
    db = get_db()
    try:
        # 🔴 `BEGIN IMMEDIATE`.  `_create_report` 는 SELECT→INSERT 이고 컬럼에
        # `UNIQUE(project_id, report_date)` 가 걸려 있다.  deferred BEGIN 이면 "생성"
        # 을 두 번 누른 순간 둘 다 빈 것을 보고, 뒤에 들어온 INSERT 가 IntegrityError
        # 로 500 이 된다 -- 이 라우트는 원래 이미 있는 보고서를 200 으로 돌려주는
        # 멱등 계약이다.
        db.execute('BEGIN IMMEDIATE')
        rid = _create_report(db, pid, report_date, session_actor())
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_report_json(rid)), 200


def session_actor():
    from flask import session
    return session.get('username') or session.get('display_name') or 'user'


@bp.route('/api/dock-daily/reports/<int:rid>')
@login_required
def report_get(rid):
    out = _report_json(rid)
    return jsonify(out) if out else _error('report not found', 404)


def _op_int(value, default=0):
    """블록 op 의 정수 필드 하나.  읽을 수 없으면 ValueError (호출부가 400 으로 바꾼다).

    `bool` 은 int 의 하위형이라 `int(True)==1` 로 조용히 통과한다 -- `sort_order:true`
    가 1번 자리로 들어가면 순서가 어긋나는데 아무 데서도 에러가 안 난다.
    """
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        raise ValueError('boolean is not an integer')
    return int(value)


def _cas_begin(rid, expected):
    db = get_db()
    db.execute('BEGIN IMMEDIATE')
    row = db.execute('SELECT * FROM dock_daily_report WHERE id=?', (rid,)).fetchone()
    if not row:
        db.rollback(); return None, _error('report not found', 404)
    # 409 는 종류가 셋이고(revision 충돌 / 확정잠금 / 날짜중복) 사람이 할 일이 서로 다르다.
    # `code` 를 함께 주는 이유: 클라이언트가 `error` 문자열로 갈라 읽으면 문구 한 글자만
    # 바뀌어도 조용히 엉뚱한 안내("다른 사용자가 먼저 저장함")를 낸다(올마이트 지적).
    if row['revision'] != expected:
        db.rollback(); return None, _error('revision conflict', 409, code='revision_conflict',
                                           current_revision=row['revision'])
    if row['status'] == 'final':
        db.rollback(); return None, _error('final report is locked', 409, code='final_locked',
                                           current_revision=row['revision'])
    return row, None


@bp.route('/api/dock-daily/reports/<int:rid>', methods=['PUT'])
@login_required
def report_put(rid):
    data = _body()
    if not isinstance(data.get('revision'), int):
        return _error('revision is required', 400)
    row, err = _cas_begin(rid, data['revision'])
    if err:
        return err
    db = get_db()
    try:
        changed = False
        section_defs_changed = False
        section_updates = data.get('section_updates')
        if section_updates is not None:
            if not isinstance(section_updates, list):
                db.rollback(); return _error('section_updates must be an array')
            for item in section_updates:
                if not isinstance(item, dict):
                    db.rollback(); return _error('invalid section update')
                key = str(item.get('section_key') or '').strip()
                section = db.execute(
                    'SELECT kind, label, enabled, sort_order FROM dock_daily_section_def'
                    ' WHERE project_id=? AND section_key=?',
                    (row['project_id'], key)).fetchone()
                if not section:
                    db.rollback(); return _error('only special sections are configurable')
                # 🔴 고정 섹션은 **순서만** 바꿀 수 있다(형 지시 2026-08-22).  Shipyard/Survey/
                # Vendor/Remark 는 SVMS 필드(`RMK_SYD`/`RMK_VNDR`)와 메일 서식이 이름으로
                # 물려 있어 label 을 바꾸거나 꺼 버리면 그 계약이 깨진다.  그래서 위치만
                # 열어 주고, label·enabled 가 함께 오면 거절한다 -- 조용히 무시하면
                # 클라이언트는 이름이 바뀐 줄 알고 옛 값을 화면에 남긴다.
                #
                # 순서를 보낼 때 목록 전체를 그대로 되돌려 보내는 클라이언트가 있으므로,
                # **값이 그대로인** label 은 변경이 아니라고 보고 통과시킨다.
                # 🔴 `sort_order` 는 정수여야 한다(올마이트 지적).  SQLite 는 형을 가리지
                # 않아서 `"3"`·`3.5` 를 그대로 저장하고, 그 뒤 `ORDER BY sort_order` 는
                # 문자열/실수와 정수를 섞어 정렬한다 -- 앱·메일·SVMS 순서가 한꺼번에
                # 어긋나는데 아무 데서도 에러가 나지 않는다.  `bool` 은 int 의 하위형이라
                # 따로 걷어낸다.
                order = item.get('sort_order')
                if order is not None and (isinstance(order, bool) or not isinstance(order, int)):
                    db.rollback(); return _error('sort_order must be an integer')
                label = item.get('label')
                if section['kind'] != 'special':
                    if 'enabled' in item or (label is not None and label != section['label']):
                        db.rollback()
                        return _error('fixed sections accept sort_order only')
                    label = None
                enabled = (None if 'enabled' not in item or section['kind'] != 'special'
                           else (1 if item['enabled'] else 0))
                # 감추기도 확정본 내용을 지운다 -- `section_delete` 와 같은 가드를 쓴다.
                if enabled == 0 and section['enabled']:
                    finals = _final_content_dates(db, row['project_id'], key)
                    if finals:
                        db.rollback()
                        return _error('확정된 보고서에 이 섹션의 내용이 있습니다.'
                                      ' 확정을 취소한 뒤 감추세요.',
                                      409, code='final_report_has_content', dates=finals)
                db.execute('''UPDATE dock_daily_section_def
                              SET label=COALESCE(?,label), enabled=COALESCE(?,enabled), sort_order=COALESCE(?,sort_order)
                              WHERE project_id=? AND section_key=?''',
                           (label, enabled, order, row['project_id'], key))
                # 값이 실제로 바뀐 것만 센다.  목록을 그대로 echo 하는 저장이 형제 보고서
                # revision 을 올리면 아무것도 안 바꾼 저장마다 다른 기기가 409 를 맞는다.
                if ((label is not None and label != section['label'])
                        or (enabled is not None and enabled != section['enabled'])
                        or (order is not None and order != section['sort_order'])):
                    section_defs_changed = True
                changed = True
        meta = data.get('metadata') if isinstance(data.get('metadata'), dict) else data
        updates = []
        if 'report_date' in meta:
            # 날짜를 잘못 입력했을 때 고치는 경로(형 지시 2026-08-21).  삭제 후 재생성으로
            # 고치면 그 날짜에 이미 쓴 본문·첨부가 같이 날아가므로 제자리 정정이 필요하다.
            #
            # ⚠️ 옮기는 건 날짜와 (자동생성이면) 제목뿐이다.  이미 수집된 dock_auto 블록과
            # source_link 는 옛 날짜의 이벤트에서 온 것이고 여기서 다시 수집하지 않는다.
            # 자동수집을 재실행하면 사람이 고친 문장을 덮으므로, 내용 판단은 사람에게 남긴다.
            #
            # 컬럼은 NOT NULL 이고 UNIQUE(project_id, report_date) 다.  빈 값은 400,
            # 이미 존재하는 날짜는 409 로 끊는다 -- IntegrityError 를 500 으로 흘리면
            # 호출자는 "저장 실패"만 받고 왜 실패했는지 알 수 없다.
            try:
                new_date = _date(meta['report_date'], True)
            except ValueError as e:
                db.rollback(); return _error(str(e))
            if new_date != row['report_date']:
                clash = db.execute(
                    'SELECT id FROM dock_daily_report WHERE project_id=? AND report_date=? AND id<>?',
                    (row['project_id'], new_date, rid)).fetchone()
                if clash:
                    db.rollback()
                    return _error('that date already has a report', 409, code='date_taken',
                                  conflicting_report_id=clash['id'])
                updates.append(('report_date', new_date))
                # 자동생성 제목은 날짜를 품고 있다.  사람이 손대지 않은 제목만 새 날짜로
                # 다시 만든다 -- 손으로 쓴 제목을 덮는 쪽이 더 큰 손실이다.  같은 요청이
                # email_subject 를 명시했으면 그쪽이 이긴다(SET 절에 같은 컬럼을 두 번
                # 넣지 않기 위한 것이기도 하다).
                if 'email_subject' not in meta:
                    moved = _subject_with_date(row['email_subject'], row['report_date'], new_date)
                    if moved is not None:
                        updates.append(('email_subject', moved))
        for key in ('email_subject', 'email_intro', 'safety_footer'):
            if key in meta:
                updates.append((key, meta[key] if meta[key] is not None else ''))
        if meta.get('status') in ('auto_draft', 'editing', 'final'):
            updates.append(('status', meta['status']))
        if updates:
            db.execute("UPDATE dock_daily_report SET %s WHERE id=?" % ','.join('%s=?' % k for k, _ in updates),
                       tuple(v for _, v in updates) + (rid,)); changed = True
        ops = data.get('operations')
        if ops is None:
            ops = data.get('block_operations') or []
        if not isinstance(ops, list):
            db.rollback(); return _error('operations must be an array')
        for op in ops:
            if not isinstance(op, dict):
                db.rollback(); return _error('invalid block operation')
            action = op.get('op') or op.get('action') or 'upsert'
            # 🔴 정수 필드는 여기서 400 으로 끊는다.  전엔 `int(op.get('id') or 0)` 이
            # 그대로 터져 500 이 갔다 -- 본문 계약 위반은 이 라우트의 다른 검사들처럼
            # 400 이어야 하고, 500 은 형에게 "서버가 죽었다" 로 읽힌다.
            try:
                bid = _op_int(op.get('id'))
                order = _op_int(op.get('sort_order'))
            except (TypeError, ValueError):
                db.rollback(); return _error('block id and sort_order must be integers')
            if action == 'delete':
                cur = db.execute('DELETE FROM dock_daily_block WHERE id=? AND report_id=?', (bid, rid))
                if cur.rowcount: changed = True
                db.execute("UPDATE dock_daily_attachment SET deleted_at=datetime('now','localtime') WHERE report_id=? AND block_id=?", (rid, bid))
                continue
            sec = str(op.get('section_key') or '').strip()
            typ = str(op.get('block_type') or op.get('type') or 'paragraph')
            if not sec or typ not in BLOCK_TYPES or not query('SELECT id FROM dock_daily_section_def WHERE project_id=? AND section_key=?',
                                                              (row['project_id'], sec), one=True):
                db.rollback(); return _error('invalid section or block_type')
            content = op.get('content', op.get('content_json', {}))
            if isinstance(content, str):
                content = _json(content, {})
            if not isinstance(content, dict):
                db.rollback(); return _error('content must be an object')
            # `parent_id` 는 FK 다.  없는 id 를 그대로 넣으면 IntegrityError 가 500 으로
            # 새어 나가고, 무엇이 틀렸는지 응답에 아무것도 안 남는다.
            parent_id = op.get('parent_id')
            if parent_id is not None:
                try:
                    parent_id = _op_int(parent_id, None)
                except (TypeError, ValueError):
                    db.rollback(); return _error('parent_id must be an integer')
            if parent_id is not None and not db.execute(
                    'SELECT id FROM dock_daily_block WHERE id=? AND report_id=?',
                    (parent_id, rid)).fetchone():
                db.rollback(); return _error('parent block not found', 404)
            existing = db.execute('SELECT id FROM dock_daily_block WHERE id=? AND report_id=?', (bid, rid)).fetchone() if bid else None
            if existing:
                db.execute('''UPDATE dock_daily_block SET section_key=?, parent_id=?, sort_order=?, block_type=?,
                              content_json=?, origin='manual', manual_override=1, updated_at=datetime('now','localtime')
                              WHERE id=? AND report_id=?''',
                           (sec, parent_id, order, typ,
                            json.dumps(content, ensure_ascii=False), bid, rid))
            else:
                db.execute('''INSERT INTO dock_daily_block(report_id,section_key,parent_id,sort_order,block_type,
                              content_json,origin,manual_override) VALUES (?,?,?,?,?,?, 'manual',1)''',
                           (rid, sec, parent_id, order, typ,
                            json.dumps(content, ensure_ascii=False)))
            changed = True
        if not changed:
            db.rollback()
            return jsonify(_report_json(rid))
        if section_defs_changed:
            _bump_sibling_reports(db, row['project_id'], exclude_id=rid)
        newrev = row['revision'] + 1
        cur = db.execute("UPDATE dock_daily_report SET revision=?, status=CASE WHEN status='auto_draft' THEN 'editing' ELSE status END, updated_at=datetime('now','localtime') WHERE id=? AND revision=?",
                         (newrev, rid, row['revision']))
        if cur.rowcount != 1:
            db.rollback(); return _error('revision conflict', 409, current_revision=row['revision'])
        # Revision numbers identify the resulting snapshot.  The initial
        # revision is recorded by _create_report; every successful PUT records
        # the new revision after its operations have been applied.
        db.execute('INSERT INTO dock_daily_report_revision(report_id,revision,snapshot_json,actor) VALUES (?,?,?,?)',
                   (rid, newrev, json.dumps(_snapshot(rid), ensure_ascii=False), session_actor()))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_report_json(rid))


COPY_MODES = ('replace', 'append')


@bp.route('/api/dock-daily/reports/<int:rid>/copy-from', methods=['POST'])
@login_required
def report_copy_from(rid):
    """이전 일자 보고서 본문을 이 보고서로 그대로 당겨온다(형 지시 2026-08-21).

    자동초안 폐기와 한 쌍인 경로다.  입거공사는 전날 작업이 그대로 이어지는 날이
    많아서, 자동으로 문장을 만들어 주는 것보다 사람이 쓴 어제 문장을 복사해 고치는
    쪽이 실제 작업 방식에 맞는다.

    🔴 복사되는 것과 안 되는 것 -- 이 목록이 계약이다:
      · 섹션 카드 본문(item/paragraph/table)은 내용 그대로 복사된다.  다만 origin 은
        'manual', manual_override=1 로 들어간다.  당겨온 순간 그 문장은 사람이 고른
        문장이고, 원본이 달고 있던 자동수집 provenance 는 이 날짜의 근거가 아니다.
      · image 블록·첨부는 따라오지 않는다.  첨부는 실제 파일이고 stored_name 이
        UNIQUE 라 행만 복사하면 한쪽을 지울 때 다른 쪽 파일까지 사라진다.  프로젝트에서
        사라진 섹션의 블록도 같이 건너뛴다.  건너뛴 개수는 응답에 실어 화면에서 알린다.
      · 일정(ITINERARY)은 프로젝트 열이라 애초에 보고서별로 다르지 않다.
      · 메일 제목은 날짜를 품고 있어 절대 복사하지 않는다.  인사말·안전문구는
        `replace` 일 때만 따라온다(`append` 는 지금 쓰고 있는 머리말을 건드리지 않는다).

    `replace` 는 이 보고서의 기존 카드를 지우지만 첨부는 지우지 않는다 -- 파일은
    사람이 올린 것이고, 본문 교체가 파일 삭제까지 뜻할 이유가 없다.  블록이 사라지면
    FK 가 attachment.block_id 를 NULL 로 풀어 첨부 목록에는 그대로 남는다.
    """
    data = _body()
    if not isinstance(data.get('revision'), int):
        return _error('revision is required', 400)
    try:
        source_id = int(data.get('source_report_id'))
    except (TypeError, ValueError):
        return _error('source_report_id is required')
    mode = str(data.get('mode') or 'replace')
    if mode not in COPY_MODES:
        return _error('mode must be replace or append')
    if source_id == rid:
        return _error('source_report_id must be another report', 400, code='same_report')
    row, err = _cas_begin(rid, data['revision'])
    if err:
        return err
    db = get_db()
    try:
        src = db.execute('SELECT * FROM dock_daily_report WHERE id=?', (source_id,)).fetchone()
        if not src:
            db.rollback(); return _error('source report not found', 404)
        # 다른 프로젝트(=다른 선박) 보고서를 당겨오면 조용히 엉뚱한 배의 작업내역이
        # 섞인다.  같은 프로젝트 안에서만 허용한다.
        if src['project_id'] != row['project_id']:
            db.rollback(); return _error('source report belongs to another project', 400,
                                         code='cross_project')
        # 🔴 "이전 일자" 는 화면 문구가 아니라 계약이다.  서버가 안 보면 웹 드롭다운만이
        # 유일한 방어선이고, 앱 버그·러너·curl 이 **뒷날 진행사항으로 앞날 기록을 덮을**
        # 수 있다(`replace` 는 기존 카드를 지운다).  날짜는 'YYYY-MM-DD' 라 문자열
        # 비교가 곧 날짜 비교다.
        if src['report_date'] >= row['report_date']:
            db.rollback(); return _error('source report must be an earlier date', 400,
                                         code='not_earlier')
        sections = {x['section_key'] for x in _sections(row['project_id'], include_disabled=True)}
        deleted = 0
        if mode == 'replace':
            deleted = db.execute('DELETE FROM dock_daily_block WHERE report_id=?', (rid,)).rowcount
        bases = {}
        if mode == 'append':
            for b in db.execute('SELECT section_key, MAX(sort_order) m FROM dock_daily_block '
                                'WHERE report_id=? GROUP BY section_key', (rid,)):
                bases[b['section_key']] = (b['m'] or 0) + 1
        src_blocks = db.execute('SELECT * FROM dock_daily_block WHERE report_id=? '
                                'ORDER BY section_key, sort_order, id', (source_id,)).fetchall()
        idmap = {}; copied = 0; skipped = 0; seq = {}
        for b in src_blocks:
            if b['block_type'] == 'image' or b['section_key'] not in sections:
                skipped += 1
                continue
            if mode == 'append':
                n = seq.get(b['section_key'], 0); seq[b['section_key']] = n + 1
                order = bases.get(b['section_key'], 0) + n
            else:
                order = b['sort_order']
            cur = db.execute('''INSERT INTO dock_daily_block(report_id,section_key,parent_id,sort_order,
                                block_type,content_json,origin,manual_override)
                                VALUES (?,?,NULL,?,?,?,'manual',1)''',
                             (rid, b['section_key'], order, b['block_type'], b['content_json']))
            idmap[b['id']] = cur.lastrowid
            copied += 1
        # 부모가 자식보다 먼저 나온다는 보장이 없으므로 전부 넣은 뒤 다시 잇는다.
        # 부모가 건너뛴 블록이면 자식은 최상위로 남는다 -- 없는 부모를 가리키게 두는
        # 쪽이 더 나쁘다.
        for b in src_blocks:
            new_id = idmap.get(b['id'])
            if new_id and b['parent_id'] and idmap.get(b['parent_id']):
                db.execute('UPDATE dock_daily_block SET parent_id=? WHERE id=?',
                           (idmap[b['parent_id']], new_id))
        # 🔴 인사말/안전문구도 "바뀐 것" 으로 센다(올마이트 지적 2026-08-21).  앞서는
        # `copied`/`deleted` 만 봤는데, 원본이 이미지 카드만 가진 빈 보고서면 두 값이
        # 0 이라 아래 rollback 이 이 UPDATE 까지 되돌리고도 200 을 줬다 -- 계약은 replace
        # 에서 인사말이 따라온다고 말하는데 조용히 안 따라오는 상태였다.
        meta = 0
        if mode == 'replace':
            meta = db.execute('UPDATE dock_daily_report SET email_intro=?, safety_footer=? '
                              'WHERE id=? AND (IFNULL(email_intro,"")<>IFNULL(?,"") '
                              'OR IFNULL(safety_footer,"")<>IFNULL(?,""))',
                              (src['email_intro'], src['safety_footer'], rid,
                               src['email_intro'], src['safety_footer'])).rowcount
        if not copied and not deleted and not meta:
            db.rollback()
            out = _report_json(rid)
            out.update(copied_blocks=0, skipped_blocks=skipped, source_report_id=source_id)
            return jsonify(out)
        newrev = row['revision'] + 1
        cur = db.execute("UPDATE dock_daily_report SET revision=?, "
                         "status=CASE WHEN status='auto_draft' THEN 'editing' ELSE status END, "
                         "updated_at=datetime('now','localtime') WHERE id=? AND revision=?",
                         (newrev, rid, row['revision']))
        if cur.rowcount != 1:
            db.rollback(); return _error('revision conflict', 409, code='revision_conflict',
                                         current_revision=row['revision'])
        db.execute('INSERT INTO dock_daily_report_revision(report_id,revision,snapshot_json,actor) '
                   'VALUES (?,?,?,?)',
                   (rid, newrev, json.dumps(_snapshot(rid), ensure_ascii=False), session_actor()))
        db.commit()
    except Exception:
        db.rollback(); raise
    out = _report_json(rid)
    out.update(copied_blocks=copied, skipped_blocks=skipped, source_report_id=source_id)
    return jsonify(out)


@bp.route('/api/dock-daily/reports/<int:rid>/status', methods=['POST'])
@login_required
def report_status(rid):
    """확정 / 확정취소 -- the only route allowed to move a report out of `final`.

    `_cas_begin` refuses every write to a final row, and that refusal is what
    makes 확정 a lock at all.  Routing the release through it would mean the
    lock could never be opened, so this endpoint runs its own CAS instead of
    reusing that helper.  It is deliberately the single exception: keeping the
    bypass in one status-only route means no content write can ever ride along
    with it, which is what `_cas_begin` exists to guarantee.

    The revision bump is not cosmetic.  It is what tells another tab holding
    the old revision that its view is stale, and it lands a snapshot row naming
    the actor -- an unlock has to be attributable, not silent.
    """
    data = _body()
    want = data.get('status')
    if want not in ('final', 'editing'):
        return _error("status must be 'final' or 'editing'")
    if not isinstance(data.get('revision'), int) or isinstance(data.get('revision'), bool):
        return _error('revision is required', 400)
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM dock_daily_report WHERE id=?', (rid,)).fetchone()
        if not row:
            db.rollback(); return _error('report not found', 404)
        if row['revision'] != data['revision']:
            db.rollback(); return _error('revision conflict', 409, current_revision=row['revision'])
        if row['status'] == want:
            # Already there -- a double click is not an error.  The caller's
            # revision was still checked above, so this is not a blind no-op
            # that hides a stale client.
            db.rollback(); return jsonify(_report_json(rid))
        newrev = row['revision'] + 1
        # `source_changed_after_final` means "the Dock Manager source moved
        # after this was 확정". Nothing ever cleared it, which stayed invisible
        # while 확정 was one-way: with a release path, a re-확정 would inherit
        # the previous round's warning and a reopened draft would keep flying a
        # flag about a lock it no longer has.
        cur = db.execute("UPDATE dock_daily_report SET status=?, revision=?, source_changed_after_final=0,"
                         " updated_at=datetime('now','localtime') WHERE id=? AND revision=?",
                         (want, newrev, rid, row['revision']))
        if cur.rowcount != 1:
            db.rollback(); return _error('revision conflict', 409, current_revision=row['revision'])
        db.execute('INSERT INTO dock_daily_report_revision(report_id,revision,snapshot_json,actor) VALUES (?,?,?,?)',
                   (rid, newrev, json.dumps(_snapshot(rid), ensure_ascii=False), session_actor()))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_report_json(rid))


@bp.route('/api/dock-daily/reports/<int:rid>', methods=['DELETE'])
@login_required
def report_delete(rid):
    """Delete one report date and everything hanging off it.

    A `final` report is edit-locked, so deleting one must not be a plain
    click-through: the caller has to name what it is doing.  `report_status`
    can release the lock now, but that is a separate decision from erasing the
    day, and a mis-click on a sidebar row must not be able to do either.
    """
    confirm = _body().get('confirm')

    def guard(row):
        # Read inside the transaction: a 확정 that lands between the check and
        # the delete would otherwise let an unconfirmed request through.
        if row['status'] == 'final' and confirm != 'delete-final':
            return _error('final report requires confirm=delete-final', 409)
        return None

    payload, err = _delete_cascade(_CASCADE_REPORT, rid, guard)
    return err if err else jsonify(payload)


def _file_mime(data, ext, declared):
    declared = (declared or '').lower().split(';')[0]
    if ext in {'jpg', 'jpeg'} and data[:3] == b'\xff\xd8\xff': return 'image/jpeg'
    if ext == 'png' and data[:8] == b'\x89PNG\r\n\x1a\n': return 'image/png'
    if ext == 'gif' and data[:6] in (b'GIF87a', b'GIF89a'): return 'image/gif'
    if ext == 'pdf' and data[:5] == b'%PDF-': return 'application/pdf'
    if ext in {'webp'} and data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'image/webp'
    if ext in {'heic', 'heif'} and len(data) > 12 and data[4:8] == b'ftyp': return 'image/heic'
    if ext == 'bmp' and data[:2] == b'BM': return 'image/bmp'
    if ext in {'docx','xlsx','pptx'} and data[:4] == b'PK\x03\x04':
        return {'docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'pptx':'application/vnd.openxmlformats-officedocument.presentationml.presentation'}[ext]
    if ext in {'doc','xls','ppt','msg'} and data[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return {'doc':'application/msword','xls':'application/vnd.ms-excel',
                'ppt':'application/vnd.ms-powerpoint','msg':'application/vnd.ms-outlook'}[ext]
    if ext in {'jpg','jpeg','png','gif','webp','heic','heif','bmp','pdf','docx','xlsx','pptx','doc','xls','ppt','msg'}: return None
    return declared if declared in {'text/plain', 'text/csv'} else None


@bp.route('/api/dock-daily/reports/<int:rid>/attachments', methods=['POST'])
@login_required
def attachment_post(rid):
    r = _report(rid)
    if not r: return _error('report not found', 404)
    if r['status'] == 'final': return _error('final report is locked', 409)
    f = request.files.get('file') or request.files.get('attachment')
    if not f or not f.filename: return _error('file is required')
    raw = f.read(MAX_ATTACHMENT + 1)
    if not raw: return _error('empty file')
    if len(raw) > MAX_ATTACHMENT: return _error('file too large', 413)
    safe = secure_filename(f.filename)
    ext = (safe.rsplit('.', 1)[-1].lower() if '.' in safe else '')
    if ext not in ALLOWED_EXT: return _error('unsupported file type')
    mime = _file_mime(raw, ext, f.mimetype)
    if not mime: return _error('file content/type mismatch')
    block_id = request.form.get('block_id', type=int)
    digest = hashlib.sha256(raw).hexdigest()
    stored = 'dock_daily_' + uuid.uuid4().hex + '.' + ext
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.realpath(os.path.join(UPLOAD_DIR, stored))
    if os.path.commonpath((path, os.path.realpath(UPLOAD_DIR))) != os.path.realpath(UPLOAD_DIR):
        return _error('unsafe path', 400)
    try:
        with open(path, 'xb') as fh: fh.write(raw)
    except OSError:
        return _error('file storage failed', 500)
    # 🔴 상태·블록 확인과 INSERT 는 한 트랜잭션 안이다.  위의 `_report` 읽기는 이 아래를
    # 못 지킨다 -- 확인과 INSERT 사이에 확정이 끼면 **확정 뒤에 추가된 첨부**가 확정본에
    # 붙고, 그건 `attachment_delete` 가 다시 409 로 거절해서 확정취소 없이는 못 지운다.
    #
    # 🔴 실패하면 방금 쓴 파일을 지운다.  행 없는 blob 은 어떤 purge 경로도 못 찾는다
    # (전부 행을 훑는다) -- 영구 고아 파일이 되고, 그게 `_purge_files` 가 드러내려고
    # 존재하는 바로 그 실패다.
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        fresh = db.execute('SELECT status FROM dock_daily_report WHERE id=?', (rid,)).fetchone()
        if not fresh:
            db.rollback(); _purge_files([stored]); return _error('report not found', 404)
        if fresh['status'] == 'final':
            db.rollback(); _purge_files([stored]); return _error('final report is locked', 409)
        if block_id and not db.execute('SELECT id FROM dock_daily_block WHERE id=? AND report_id=?',
                                       (block_id, rid)).fetchone():
            db.rollback(); _purge_files([stored]); return _error('block not found', 404)
        cur = db.execute('''INSERT INTO dock_daily_attachment(report_id,block_id,stored_name,original_name,mime_type,size,sha256)
                            VALUES (?,?,?,?,?,?,?)''',
                         (rid, block_id, stored, safe[:255], mime, len(raw), digest))
        aid = cur.lastrowid
        db.commit()
    except Exception:
        db.rollback(); _purge_files([stored]); raise
    return jsonify(_dict(query('SELECT * FROM dock_daily_attachment WHERE id=?', (aid,), one=True))), 201


@bp.route('/api/dock-daily/attachments/<int:aid>')
@login_required
def attachment_get(aid):
    row = query('SELECT * FROM dock_daily_attachment WHERE id=? AND deleted_at IS NULL', (aid,), one=True)
    if not row: return _error('attachment not found', 404)
    response = send_from_directory(UPLOAD_DIR, row['stored_name'], mimetype=row['mime_type'], as_attachment=False,
                                   download_name=row['original_name'])
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.route('/api/dock-daily/attachments/<int:aid>', methods=['DELETE'])
@login_required
def attachment_delete(aid):
    """Remove one attachment: its row and its blob.

    No `deleted_at` tombstone here, unlike the block path.  There, hiding the
    row is enough because deleting the block that owns it is what happened, and
    the report delete still sweeps the blob later.  A single attachment removed
    from a report that lives on has no such sweep: leaving the row would keep
    the file on disk with nothing that ever revisits it, and leaving the blob is
    the one failure mode `_purge_files` exists to make visible.

    `final` is refused rather than gated behind a confirm token.  Uploading to a
    확정본 already gets 409 `final report is locked`; letting a delete through
    would mean the edit lock only holds in one direction, so the same content
    could be changed by removing instead of adding.  확정 취소 is the way in --
    that is a decision the user makes explicitly, on the report.

    An image block pointing at this attachment has its reference cleared in the
    same transaction.  The block itself stays: it carries the caption the user
    wrote, and deleting someone's paragraph as a side effect of removing a file
    is a bigger surprise than the file going.

    Leaving the reference behind was the third option and the wrong one.  The
    live mail body does not render images today (`_render_section` is only
    called with `as_html=False`, and its html branch has no caller yet), so
    nothing is visibly broken *right now* -- which is exactly why it would be
    missed: a stale id resolves to 404 for whoever renders it first, and the
    only place recording that the file ever existed is the row being deleted.
    Cleared, the block takes the same path as an image block that never got a
    file and shows its caption.
    """
    def guard(row):
        if row['report_status'] == 'final':
            return _error('final report is locked', 409)
        return None

    def unlink(db, row):
        # Read-modify-write in Python rather than json_set(): content_json is
        # free-form per block type and a JSON1 rewrite would also normalise
        # every other key in it.  Only image blocks can hold the reference.
        rows = db.execute("SELECT id, content_json FROM dock_daily_block"
                          " WHERE report_id=? AND block_type='image'", (row['report_id'],)).fetchall()
        for block in rows:
            content = _json(block['content_json'], {})
            if not isinstance(content, dict):
                continue
            touched = False
            if str(content.get('attachment_id') or '') == str(aid):
                content['attachment_id'] = None
                touched = True
            # 격자 카드는 지운 사진의 자리만 비운다. 나머지 장과 캡션·열 수는 남는다.
            for entry in content.get('images') or []:
                if isinstance(entry, dict) and str(entry.get('attachment_id') or '') == str(aid):
                    entry['attachment_id'] = None
                    touched = True
            if not touched:
                continue
            db.execute('UPDATE dock_daily_block SET content_json=? WHERE id=?',
                       (json.dumps(content, ensure_ascii=False), block['id']))
        if row['report_id'] is None:
            return
        # 🔴 블록을 안 고쳤어도 올린다.  내 첫 구현은 `edited` 일 때만 올렸는데, 아직 어느
        # 블록도 안 가리키는 첨부가 정확히 그 구멍이다(올마이트 blocking) -- 다른 기기가
        # 첨부 카드에서 올려 둔 사진을 사진 카드에 **로컬로** 끼워 넣은 상태에서 이쪽이
        # 그 첨부를 지우면, 저쪽 revision 은 그대로 유효해서 저장이 통과하고 **지워진
        # attachment_id** 가 블록에 박힌다.  첨부 목록 자체가 `_report_json` 의 일부이므로
        # 첨부 삭제는 그 자체로 보고서 상태 변경이다.
        #
        # 🔴 블록을 고쳤으면 더더욱 올린다.  안 올리면 그 보고서를 열어 둔 다른 기기가
        # **아직 유효한** revision 으로 저장에 성공하고(upsert 는 content_json 을 통째로
        # 덮는다) 방금 끊어낸 attachment_id 를 되살린다 -- 지운 사람은 깨끗이 지워졌다고
        # 믿는데 메일에는 "첨부 파일을 찾을 수 없습니다" 가 나간다.  `section_delete` 가
        # 같은 이유로 하는 일이다.
        db.execute("UPDATE dock_daily_report SET revision=revision+1,"
                   " updated_at=datetime('now','localtime') WHERE id=?", (row['report_id'],))
        current = db.execute('SELECT revision FROM dock_daily_report WHERE id=?',
                             (row['report_id'],)).fetchone()['revision']
        db.execute('INSERT INTO dock_daily_report_revision(report_id,revision,snapshot_json,actor)'
                   ' VALUES (?,?,?,?)',
                   (row['report_id'], current,
                    json.dumps(_snapshot(row['report_id']), ensure_ascii=False), session_actor()))

    payload, err = _delete_cascade(_CASCADE_ATTACHMENT, aid, guard, unlink)
    return err if err else jsonify(payload)


def _safe_zip_xml(zf, path):
    info = zf.getinfo(path)
    if info.file_size > MAX_OOXML_PART:
        raise ValueError('OOXML part is too large')
    raw = zf.read(info)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('OOXML entities are not allowed')
    return ElementTree.fromstring(raw)


def _ooxml_text(raw, ext):
    """Small dependency-free browser preview for modern Office files."""
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        infos = zf.infolist()
        if len(infos) > 2000 or sum(x.file_size for x in infos) > MAX_OOXML_UNCOMPRESSED:
            raise ValueError('OOXML archive is too large')
        if any(x.file_size > MAX_OOXML_PART or
               (x.compress_size and x.file_size / x.compress_size > 200) for x in infos):
            raise ValueError('OOXML archive has unsafe compression')
        if ext == 'docx':
            root = _safe_zip_xml(zf, 'word/document.xml')
            paragraphs = []
            for p in root.iter():
                if p.tag.endswith('}p'):
                    text = ''.join(x.text or '' for x in p.iter() if x.tag.endswith('}t')).strip()
                    if text: paragraphs.append(text)
            return '<h2>Word 미리보기</h2>' + ''.join('<p>%s</p>' % html.escape(x) for x in paragraphs)
        if ext == 'xlsx':
            shared = []
            if 'xl/sharedStrings.xml' in zf.namelist():
                sr = _safe_zip_xml(zf, 'xl/sharedStrings.xml')
                shared = [''.join(x.text or '' for x in si.iter() if x.tag.endswith('}t'))
                          for si in sr if si.tag.endswith('}si')]
            sheets = sorted(x for x in zf.namelist() if re.match(r'^xl/worksheets/sheet\d+\.xml$', x))
            chunks = []
            for index, path in enumerate(sheets[:10], 1):
                root = _safe_zip_xml(zf, path); rows = []
                for row in (x for x in root.iter() if x.tag.endswith('}row')):
                    vals = []
                    for cell in (x for x in row if x.tag.endswith('}c')):
                        val = next((x.text or '' for x in cell if x.tag.endswith('}v')), '')
                        if cell.attrib.get('t') == 's' and val.isdigit() and int(val) < len(shared): val = shared[int(val)]
                        vals.append(val)
                    rows.append(vals)
                width = max([len(x) for x in rows] or [0])
                body = ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % html.escape(str(row[i] if i < len(row) else '')) for i in range(width)) for row in rows[:500])
                chunks.append('<h2>Sheet %d</h2><table>%s</table>' % (index, body))
            return ''.join(chunks) or '<p>표시할 셀이 없습니다.</p>'
        if ext == 'pptx':
            slides = sorted(x for x in zf.namelist() if re.match(r'^ppt/slides/slide\d+\.xml$', x))
            out = []
            for index, path in enumerate(slides[:100], 1):
                root = _safe_zip_xml(zf, path)
                text = ' '.join(x.text or '' for x in root.iter() if x.tag.endswith('}t')).strip()
                out.append('<section><h2>Slide %d</h2><p>%s</p></section>' % (index, html.escape(text)))
            return ''.join(out)
    return ''


@bp.route('/api/dock-daily/attachments/<int:aid>/preview')
@login_required
def attachment_preview(aid):
    row = query('SELECT * FROM dock_daily_attachment WHERE id=? AND deleted_at IS NULL', (aid,), one=True)
    if not row: return _error('attachment not found', 404)
    ext = row['original_name'].rsplit('.', 1)[-1].lower() if '.' in row['original_name'] else ''
    if row['mime_type'].startswith('image/') or row['mime_type'] == 'application/pdf':
        response = send_from_directory(UPLOAD_DIR, row['stored_name'], mimetype=row['mime_type'], as_attachment=False,
                                       download_name=row['original_name'])
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    path = os.path.realpath(os.path.join(UPLOAD_DIR, row['stored_name']))
    if os.path.commonpath((path, os.path.realpath(UPLOAD_DIR))) != os.path.realpath(UPLOAD_DIR):
        return _error('unsafe path', 400)
    try:
        with open(path, 'rb') as fh: raw = fh.read(MAX_ATTACHMENT + 1)
        if ext in {'docx','xlsx','pptx'}: content = _ooxml_text(raw, ext)
        elif ext in {'txt','csv'}: content = '<pre>%s</pre>' % html.escape(raw.decode('utf-8', 'replace'))
        else: content = '<p>이 구형 Office 형식은 브라우저 미리보기를 지원하지 않습니다. DOCX/XLSX/PPTX로 저장하면 내용 미리보기가 가능합니다.</p>'
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        content = '<p>파일 내용을 미리보기로 변환하지 못했습니다.</p>'
    page = '<!doctype html><meta charset="utf-8"><style>body{font:14px system-ui;margin:24px;color:#29261f}table{border-collapse:collapse;max-width:100%%}td{border:1px solid #ddd;padding:6px}pre{white-space:pre-wrap}</style>%s' % content
    return Response(page, content_type='text/html; charset=utf-8', headers={
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'",
        'X-Content-Type-Options': 'nosniff',
    })


def _blocks_for(rid, section):
    return [dict(x) for x in query('''SELECT id,block_type,content_json,sort_order FROM dock_daily_block
                                      WHERE report_id=? AND section_key=? ORDER BY sort_order,id''', (rid, section))]


# 메일 본문에 싣는 사진 규격. 사진은 URL 로 넣을 수 없다 -- 첨부 라우트는
# `login_required` 이고 경로도 상대라서 메일 클라이언트는 이미지 대신 로그인
# 리다이렉트를 받는다. 그래서 바이트를 본문에 직접 싣는다(data URI).
MAIL_IMAGE_MAX_PX = 900        # 본문에 싣기 전 줄이는 긴 변
MAIL_IMAGE_SHOW_PX = 520       # 메일에서 보이는 폭
MAIL_IMAGE_QUALITY = 80
# 사진 data URI 문자 수 합계 상한. 메일 **전체** 크기 상한이 아니다 -- 글·표·캡션은
# 여기서 세지 않는다. 사진이 본문 크기의 대부분이라 사진만 묶어 잡는다.
MAIL_IMAGE_BUDGET = 3 * 1024 * 1024
MAIL_IMAGE_MAX_COUNT = 12      # 한 통에 실을 사진 장수(예산과 별개의 상한)
MAIL_IMAGE_MAX_COLUMNS = 4     # 사진 격자 열 수 상한(도크 리포트와 같은 1~4)
MAIL_BODY_PX = 540             # 들여쓰기(52px) 뒤 남는 본문 폭. 격자 셀 폭 계산 기준
MAIL_IMAGE_GAP_PX = 6
MAIL_IMAGE_FRAME_PAD_PX = 4   # 사진 프레임 안쪽 여백(좌우 폭 계산에도 포함)
# 격자 칸의 가로:세로. 2열 이상에서는 이 비율의 캔버스에 사진 전체를 맞춰 넣어
# 가로·세로 사진이 섞여도 칸 높이를 같게 유지한다(형 지시 2026-08-21).
#
# 🔴 메일에서는 CSS 로 못 한다 -- Word/Outlook HTML 엔진은 `object-fit`·`aspect-ratio`
# 를 무시한다. 그래서 **서버가 JPEG 를 미리 흰 4:3 캔버스에 aspect-fit** 해서 싣는다.
# 남는 부분은 letterbox 로 두고 원본 프레임은 자르지 않는다.
MAIL_IMAGE_CELL_RATIO = (4, 3)
# 열기 전 거르는 픽셀 상한. 20MB 업로드 게이트를 통과한 파일도 압축률이 높으면
# 디코드 후 메모리는 그 수십 배가 된다.
MAIL_IMAGE_MAX_PIXELS = 40 * 1000 * 1000


def _attachment_path(stored_name):
    """`UPLOAD_DIR` 안에 실제로 있는 경로만 돌려준다(경로 탈출 차단)."""
    root = os.path.realpath(UPLOAD_DIR)
    path = os.path.realpath(os.path.join(root, stored_name or ''))
    if os.path.commonpath((path, root)) != root or not os.path.isfile(path):
        return None
    return path


def _inline_image(stored_name, budget, show_px=MAIL_IMAGE_SHOW_PX, decode_px=MAIL_IMAGE_MAX_PX,
                  ratio=None):
    """첨부 사진을 메일 본문용 data URI 로 만든다.

    돌려주는 값은 `(정보, 사유)` 이고 둘 중 하나만 채워진다. 사유는 화면에 그대로
    보여준다 -- 사진이 조용히 빠지면 형은 보냈다고 생각하고 메일을 보내게 된다.

    `ratio` 를 주면 그 가로:세로의 흰 캔버스에 **사진 전체를 aspect-fit** 해서 표시
    크기를 고정한다. 메일에서는 CSS 를 못 쓰므로 서버가 letterbox 를 바이트에 굽는다.
    """
    # 예산이 이미 없으면 파일을 열지도 않는다. 변환한 뒤에 거절하면 사진을 많이 붙인
    # 보고서 하나가 미리보기 한 번에 서버 메모리·CPU 를 다 쓰게 된다.
    if budget <= 0:
        return None, '본문 사진 용량 상한을 넘었습니다'
    path = _attachment_path(stored_name)
    if not path:
        return None, '첨부 파일을 찾을 수 없습니다'
    try:
        from PIL import Image, ImageOps
    except Exception:                                  # pragma: no cover - 배포 의존성
        return None, '서버에 이미지 변환 모듈이 없습니다'
    ensure_heif_opener()                               # 아이폰 HEIC 첨부도 본문에 싣는다
    try:
        with Image.open(path) as src:
            # `open` 은 헤더만 읽는다. 디코드 전에 크기로 먼저 거른다.
            pixels = (src.size[0] or 0) * (src.size[1] or 0)
            if not pixels:
                return None, '사진 크기를 읽을 수 없습니다'
            if pixels > MAIL_IMAGE_MAX_PIXELS:
                return None, '사진 해상도가 너무 큽니다'
            # 아이폰 사진은 회전값을 EXIF 로 들고 온다. 그대로 저장하면 옆으로 눕는다.
            image = ImageOps.exif_transpose(src) or src
            if image.mode != 'RGB':
                image = image.convert('RGB')           # 알파·팔레트는 JPEG 로 못 나간다
            # 표시 크기와 디코드 크기는 다르다. 격자에서 작게 보이는 사진에
            # 큰 바이트를 싣는 건 낭비이고, 표시폭보다 작게 실으면 흐려진다.
            long_edge = max(1, min(MAIL_IMAGE_MAX_PX, int(decode_px)))
            if ratio:
                # 고정 비율 캔버스 크기. 원본이 줄 수 있는 최대까지만 잡아 작은 사진을
                # 바이트 단계에서 억지로 확대하지 않는다(표시 크기는 아래에서 통일).
                cap = min(image.size[0], image.size[1] * ratio[0] / ratio[1])
                target_w = max(1, int(min(long_edge, cap)))
                target_h = max(1, round(target_w * ratio[1] / ratio[0]))
                image = ImageOps.pad(image, (target_w, target_h), color=(255, 255, 255),
                                     centering=(0.5, 0.5))
            else:
                image.thumbnail((long_edge, long_edge))
            width, height = image.size
            buf = BytesIO()
            image.save(buf, format='JPEG', quality=MAIL_IMAGE_QUALITY, optimize=True)
    except Exception:
        # HEIC 처럼 Pillow 가 못 읽는 형식이 여기로 온다. 첨부 자체는 첨부 카드에 남아
        # 있지만, 그 파일이 이 메일에 첨부된다는 보장은 없다 -- 그러니 그렇게 안내하지
        # 않는다(올마이트 지적). 본문에 못 넣었다는 사실만 말한다.
        return None, '이 형식은 본문에 넣을 수 없습니다'
    encoded = base64.b64encode(buf.getvalue()).decode('ascii')
    if len(encoded) > budget:
        return None, '본문 사진 용량 상한을 넘었습니다'
    if not width or not height:
        return None, '사진 크기를 읽을 수 없습니다'
    if ratio:
        # 🔴 표시 크기는 **실제 픽셀에 맞추지 않는다**. 작은 사진까지 픽셀 폭으로 줄이면
        # 그 칸만 좁아져 격자가 다시 어긋난다 -- `width`/`height` 는 HTML 속성이라 실제보다
        # 크게 줘도 렌더러가 늘려준다. 정렬을 우선하고 약간 흐려지는 쪽을 택한다.
        # 높이를 비율에서 다시 계산하는 이유도 같다(픽셀에서 역산하면 반올림으로 1px 씩
        # 어긋나 형이 본 그 들쭉날쭉이 작은 규모로 남는다).
        shown = max(1, min(MAIL_IMAGE_SHOW_PX, int(show_px)))
        return {'uri': 'data:image/jpeg;base64,' + encoded, 'width': shown,
                'height': max(1, round(shown * ratio[1] / ratio[0])),
                'cost': len(encoded)}, None
    shown = max(1, min(MAIL_IMAGE_SHOW_PX, int(show_px), width))
    return {'uri': 'data:image/jpeg;base64,' + encoded, 'width': shown,
            'height': max(1, round(height * shown / width)), 'cost': len(encoded)}, None


def _image_gallery(content):
    """사진 카드를 `(사진 목록, 열 수)` 로 편다. 도크 리포트 사진 섹션과 같은 계약.

    옛 카드는 사진 한 장을 `attachment_id`/`caption` 으로 들고 있었다. 그 카드도 계속
    열려야 하므로 한 장짜리 격자로 읽는다 -- 마이그레이션 없이 두 포맷을 같이 받는다.

    🔴 도크 리포트는 사진을 `static/uploads/dock/` 공개 URL 로 들고 있지만 여기서는
    `attachment_id` **참조**만 쓴다. 입거 Daily 의 업로드 정본은 첨부 카드이고
    (20MB·형식 게이트가 거기 있다) 파일 라우트가 `login_required` 라서 URL 이 무의미하다.
    """
    raw = content.get('images')
    items = []
    if isinstance(raw, (list, tuple)):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            aid = str(entry.get('attachment_id') or '')
            items.append({'attachment_id': int(aid) if aid.isdigit() else None,
                          'caption': str(entry.get('caption') or '')})
    elif content.get('attachment_id') or content.get('caption'):
        aid = str(content.get('attachment_id') or '')
        items.append({'attachment_id': int(aid) if aid.isdigit() else None,
                      'caption': str(content.get('caption') or '')})
    try:
        columns = int(content.get('columns') or 1)
    except (TypeError, ValueError):
        columns = 1
    return items, max(1, min(MAIL_IMAGE_MAX_COLUMNS, columns))


def _table_grid(content):
    """표 내용을 사각형으로 맞춘다. 앱 정규화(`DockDailyTableContent`)와 같은 규칙.

    🔴 헤더보다 긴 행은 자르지 않는다 -- 그 칸도 형이 적은 값이다. 열을 넓혀서 맞춘다.
    서버가 다시 맞추는 이유는 표를 만든 곳이 앱만이 아닐 수 있기 때문이다(옛 데이터).
    """
    def text(value):
        return '' if value is None else str(value)     # `None` 이 'None' 으로 보이지 않게

    raw_cols, raw_rows = content.get('columns'), content.get('rows')
    # 문자열이 오면 글자 단위로 분해되므로 리스트만 받는다(올마이트 지적).
    cols = [text(v) for v in raw_cols] if isinstance(raw_cols, (list, tuple)) else []
    rows = []
    for row in raw_rows if isinstance(raw_rows, (list, tuple)) else []:
        if isinstance(row, (list, tuple)):
            rows.append([text(v) for v in row])
        elif row not in (None, ''):
            # 배열이 아닌 행은 버리지 않는다 -- 형이 적은 값일 수 있다. 한 칸 행으로 살린다.
            rows.append([text(row)])
    width = max([len(cols)] + [len(r) for r in rows] or [0])
    if not width:
        return [], []
    cols = cols + [''] * (width - len(cols))
    rows = [r + [''] * (width - len(r)) for r in rows]
    return cols, rows


def _mail_entries(rid, key):
    """섹션 카드를 메일 항목 목록으로 편다.

    글 카드는 줄마다 항목 하나(종전 동작), 표·사진 카드는 카드 자체가 항목 하나가
    된다. 번호는 종류에 상관없이 이어진다 -- 형이 카드를 놓은 순서가 메일 순서다.

    사진은 여기서 바이트로 바꾸지 않고 첨부가 살아 있는지만 확인한다. 실제 인라인은
    `_email` 이 메일 한 통 전체의 예산을 들고 하며, 섹션마다 따로 세면 사진이 많은
    보고서에서 메일 한 통 크기에 상한이 없어진다.
    """
    files = {row['id']: row for row in query(
        'SELECT id, stored_name FROM dock_daily_attachment'
        ' WHERE report_id=? AND deleted_at IS NULL', (rid,))}
    out = []
    for block in _blocks_for(rid, key):
        typ = block.get('block_type')
        content = _json(block.get('content_json'), {})
        if not isinstance(content, dict):
            content = {}
        if typ == 'table':
            cols, rows = _table_grid(content)
            if cols or rows:
                out.append({'kind': 'table', 'columns': cols, 'rows': rows})
            continue
        if typ == 'image':
            items, columns = _image_gallery(content)
            photos = []
            for entry in items:
                row = files.get(entry['attachment_id']) if entry['attachment_id'] else None
                photos.append({'caption': entry['caption'],
                               'attachment': row['stored_name'] if row else None,
                               'note': None if row else '연결된 사진이 없습니다'})
            if photos:
                out.append({'kind': 'image', 'grid': columns, 'photos': photos})
            continue
        for line in _plain(block).splitlines():
            stripped = ITEM_NO_RE.sub('', line).strip()
            if stripped:
                out.append({'kind': 'text', 'text': stripped})
    return out


def _plain(block):
    c = _json(block.get('content_json'), {})
    if block.get('block_type') == 'table':
        cols, rows = _table_grid(c)
        return '\n'.join(' | '.join(x) for x in [cols] + rows if any(x))
    if block.get('block_type') == 'image':
        items, _ = _image_gallery(c)
        return '\n'.join('[Image] ' + (x['caption'] or '') for x in items) or '[Image] '
    return str(c.get('title') or c.get('body') or c.get('text') or '')


def _render_section(rid, key):
    """SVMS `RMK*` 본문. 메일과 같은 한 줄 = 한 항목 번호를 쓴다(두 번 번호가 붙지 않게).

    표·사진은 넣지 않는다 -- 형 지시("svms에는 표랑 사진을 본문에 포함할 필요 없음").
    파이프로 편 표 문자열은 RMK 에서 표로 읽히지도 않는다.
    """
    items = _item_lines(rid, key)
    if items == ['NIL']:
        return 'NIL'
    return '\n'.join('%d) %s' % (no, item) for no, item in enumerate(items, 1))


def _item_lines(rid, key, media=False):
    """카드 글을 작업 항목으로 편다. 카드가 들고 있던 번호는 떼고 다시 붙인다.

    `media=False`(SVMS 기본): 표·사진 카드는 아예 건너뛴다. 메일 본문은 이 함수가
    아니라 `_mail_entries` 를 쓴다 -- 거기서는 표가 표로, 사진이 사진으로 나간다.
    """
    items = []
    for block in _blocks_for(rid, key):
        if not media and block.get('block_type') in {'table', 'image'}:
            continue
        for line in _plain(block).splitlines():
            stripped = ITEM_NO_RE.sub('', line).strip()
            if stripped:
                items.append(stripped)
    return items or ['NIL']


def _mail_date(value):
    return value.replace('-', '.') if value else '-'


def _email(rid):
    r = _report(rid)
    sections = _sections(r['project_id'], include_disabled=False)
    bykey = {x['section_key']: x for x in sections}
    # 🔴 메일 순서는 화면 순서와 같아야 한다 -- 둘 다 `sort_order` 다(형 지시 2026-08-22).
    #
    # 전에는 `['shipyard'] + specials + ['survey','vendor','remark']` 로 못박혀 있었다.
    # 그래서 `create_section` 이 `MAX+1`(=마지막)로 만든 special 은 앱·웹에서 맨 아래
    # 보이는데 메일에서는 Shipyard 바로 뒤로 튀어 올라갔다.  형이 카드 순서를 바꿀 수
    # 있게 된 이상 이 어긋남은 그대로 거짓말이 된다.  `_svms` 는 원래 `_sections()` 순서를
    # 따랐으므로 이 변경으로 세 출구가 같아진다.
    order = [x['section_key'] for x in sections]
    subject = r['email_subject'] or '[Dock] M/T %s - Dock Daily Report (%s)' % (r['vessel_name'], r['report_date'])
    intro = r['email_intro'] or ''
    # Reports created before the Korean mail format was adopted retain the
    # former English defaults in the database. Render those legacy defaults
    # with the current wording without overwriting any user-edited content.
    if intro == 'Dear all,\nPlease find the dock daily report below.':
        intro = '안녕하십니까.\n아래와 같이 금일 입거공사 진행사항을 보고드립니다.'
    mail_to = '곽인섭 팀장님 / 탱커관리 3팀'
    mail_from = '손유석 감독 / 탱커관리 3팀'
    itinerary = [('BERTHING', r['berthing_date']), ('DRY DOCK IN', r['dock_in_date']),
                 ('DRY DOCK OUT', r['dock_out_date']), ('DEPARTURE', r['departure_date'])]
    intro_lines = intro.splitlines() or ['']
    lines = ['수 신 : %s' % mail_to, '발 신 : %s' % mail_from, '']
    for intro_line in intro_lines:
        lines.extend([intro_line, ''])
    lines.append('VESSEL ITINERARY')
    # Text that is inside a table must sit inside a <p> inside the <td>.
    #
    # Measured on real Outlook iOS pastes (cap height in the screenshots,
    # converted through the device scale):
    #   <p> text                    cap 16px -> ~11.4pt  == the 11pt we declare
    #   text directly inside <td>   cap 11px -> ~8pt     == ~0.7x, whatever we declare
    # The table figure did not move while the declaration was added to the
    # wrapping <div>, then to every <table> and <td>, then to a <span> around
    # each text node. Removing the tables did fix the size, which is what the
    # owner confirmed; it also removed the itinerary borders, which he wants
    # back. So the table returns with every cell's text wrapped in a <p>.
    #
    # This is a hypothesis, not a measured result: what was measured is a
    # standalone <p> outside any table. A <p> inside a <td> has never been
    # pasted, and the mechanism behind the divergence is not established, so it
    # may well come out small too. If it does, the next thing to try is
    # borderless paragraphs with a border-bottom per line -- also unmeasured.
    # Other mail clients were not measured at all.
    #
    # Outlook iOS 는 `<p margin-left>`/`text-indent` 를 붙여넣을 때 버린다(형 실측
    # 2026-08-22). 번호 항목은 borderless presentation table 로 24px spacer 를 실제
    # 셀로 만든다. 번호 28px 뒤 본문이 52px 에서 시작해 사진·표와 같은 기준선에 맞는다.
    cell_font = 'font-family:Arial,Helvetica,sans-serif;font-size:11pt'
    cell_box = 'border:1px solid #777;padding:3px 9px;%s' % cell_font

    def run(inner):
        """Wrap already-escaped markup. Callers escape user text before passing it."""
        return '<span style="%s">%s</span>' % (cell_font, inner)

    def cell(inner):
        """A table cell whose text lives in a <p>, not directly in the <td>.

        Same contract as run(): inner is already-escaped markup, inserted
        verbatim. Callers escape user text before passing it.
        """
        return '<td style="%s"><p style="margin:0;%s">%s</p></td>' % (
            cell_box, cell_font, run(inner))

    spacer = '<p style="margin:0;line-height:1.5">%s</p>' % run('&nbsp;')
    chunks = [
        '<div style="%s;line-height:1.5;color:#222">' % cell_font,
        '<p style="margin:0">%s</p>' % run('<b>수 신 :</b> %s' % html.escape(mail_to)),
        '<p style="margin:0">%s</p>' % run('<b>발 신 :</b> %s' % html.escape(mail_from)),
        spacer,
    ]
    for intro_line in intro_lines:
        chunks.extend(['<p style="margin:0">%s</p>' % run(html.escape(intro_line)), spacer])
    chunks.append('<p style="margin:0 0 4px">%s</p>' % run('<b>VESSEL ITINERARY</b>'))
    chunks.append('<table style="border-collapse:collapse;margin:0;%s">' % cell_font)
    for label, value in itinerary:
        shown = _mail_date(value)
        lines.append('%s\t%s' % (label, shown))
        chunks.append('<tr>%s%s</tr>' % (cell(html.escape(label)),
                                         cell('<b>%s</b>' % html.escape(shown))))
    chunks.append('</table>')
    chunks.append(spacer)
    def item(no, inner):
        """번호가 붙은 작업 항목 한 줄. inner 는 이미 escape 된 markup."""
        p = '<p style="margin:0;%s">%%s</p>' % cell_font
        return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                'width="100%%" style="width:100%%;border-collapse:collapse;'
                'margin:0 0 3px 0;%s"><tr>'
                '<td width="24" style="width:24px;vertical-align:top;%s">%s</td>'
                '<td width="28" style="width:28px;vertical-align:top;white-space:nowrap;%s">%s</td>'
                '<td style="vertical-align:top;%s">%s</td></tr></table>'
                % (cell_font, cell_font, p % run('&nbsp;'),
                   cell_font, p % run('%d)' % no), cell_font, p % run(inner)))

    def table(entry, indent_px=52):
        """카드의 표를 메일 표로. 셀 텍스트는 `cell()` 계약대로 `<td>` 안 `<p>` 에 넣는다.

        직접 `<td>` 에 넣으면 Outlook 붙여넣기에서 11pt 선언과 무관하게 작게 붙는다
        (ITINERARY 표에서 실측한 그 문제). `<thead>` 는 쓰지 않는다 -- Word HTML
        엔진이 무시하는 경우가 있어 굵게로만 헤더를 표시한다.

        `indent_px` 는 **표 섹션에서만 0** 이다(형 지시 2026-08-22). 그 섹션은 제목이 곧
        표의 제목이라 들여쓸 상위 항목이 없다. 반대로 글 항목과 섞여 있는 표는 52px 를
        지켜야 위 번호 항목들과 같은 기준선에 선다.
        """
        head = ''.join(cell('<b>%s</b>' % (html.escape(v) or '&nbsp;')) for v in entry['columns'])
        body = ''.join('<tr>%s</tr>' % ''.join(cell(html.escape(v) or '&nbsp;') for v in row)
                       for row in entry['rows'])
        return ('<table style="border-collapse:collapse;margin:0 0 8px %dpx;%s">%s%s</table>'
                % (indent_px, cell_font, '<tr>%s</tr>' % head if head else '', body))

    # 캡션은 도크 리포트 `.dde-img-caption-inp` 계약대로 **가운데 정렬 이탤릭**이다
    # (형 지시 2026-08-21).
    caption_font = ('font-family:Arial,Helvetica,sans-serif;font-size:9pt;'
                    'color:#4B5563;font-style:italic;text-align:center')

    def photo_grid(entry, budget, count):
        """사진 카드를 `columns` 열 격자로. 도크 리포트 사진 섹션과 같은 배치다.

        돌려주는 값은 `(html 조각들, 글 줄들, 남은 예산, 실은 장수)`. 예산과 장수를 인자로
        받아 돌려주는 이유는 상한이 **메일 한 통 전체**의 것이기 때문이다 -- 카드마다 새로
        세면 사진이 많은 보고서에서 상한이 사라진다.

        열이 늘면 사진이 작게 보이므로 그만큼 작은 바이트만 싣는다(표시폭의 2배까지).
        """
        cols = entry['grid']
        # Outlook 은 td 의 padding/border 도 폭에 더한다. 이미지 폭만 본문폭으로 나누면
        # 프레임을 씌운 순간 표가 MAIL_BODY_PX 를 넘어 줄바꿈·가로잘림이 생길 수 있다.
        frame_extra = cols * (MAIL_IMAGE_FRAME_PAD_PX * 2 + 2)  # 좌우 padding + 1px border
        cell_px = max(60, (MAIL_BODY_PX - frame_extra) // cols)
        # 고정 비율 letterbox 는 **한 줄에 옆 칸이 있을 때만** 쓴다. 1열 카드는 높이를
        # 맞출 옆 칸이 없으므로 원본 비율 그대로 보여준다.
        cell_ratio = MAIL_IMAGE_CELL_RATIO if cols > 1 else None
        cells = []
        for photo in entry['photos']:
            image, note = None, photo['note']
            if photo['attachment']:
                # 🔴 상한은 **성공 장수가 아니라 연 파일 수**로 센다. 성공만 세면 예산이
                # 작게 남은 뒤부터는 상한에 걸리지 않은 채로 형이 넣은 사진을 전부 열어
                # 디코드하게 된다 -- "예산 없으면 파일을 열지 않는다" 와 같은 이유다.
                if count >= MAIL_IMAGE_MAX_COUNT:
                    note = '본문 사진 장수 상한(%d장)을 넘었습니다' % MAIL_IMAGE_MAX_COUNT
                else:
                    count += 1
                    image, note = _inline_image(photo['attachment'], budget,
                                                show_px=cell_px, decode_px=cell_px * 2,
                                                ratio=cell_ratio)
                    if image:
                        budget -= image['cost']
            cells.append((image, photo['caption'], note))

        # 사진과 캡션을 **같은 td** 안에 넣어 하나의 프레임으로 보이게 한다. 사진 행과
        # 캡션 행을 따로 만들면 Outlook 에서 둘 사이가 벌어져 격자로 읽히지 않는다.
        frame = ('border:1px solid #9CA3AF;padding:%dpx;vertical-align:top;'
                 'text-align:center') % MAIL_IMAGE_FRAME_PAD_PX
        # 캡션은 형 지시대로 `<내용>` 꺾쇠로 감싼다. 빈 캡션은 감싸지 않는다 -- 내용 없는
        # `<>` 만 남으면 형이 안 적은 것이 적힌 것처럼 보인다.
        # 🔴 이미 꺾쇠가 있는 캡션은 다시 감싸지 않는다 -- `<<내용>>` 이 된다(옛 데이터에
        # 형이 직접 꺾쇠를 적어둔 경우, 올마이트 지적).
        def wrap(caption):
            text = (caption or '').strip()
            if not text:
                return ''
            if len(text) > 1 and text.startswith('<') and text.endswith('>'):
                return text
            return '<%s>' % text

        chunks_out, text_out, rows = [], [], []
        for start in range(0, len(cells), cols):
            row = cells[start:start + cols]
            framed_tds = []
            for image, caption, note in row:
                if image:
                    inner = ('<img src="%s" width="%d" height="%d" alt="%s"'
                             ' style="display:block;border:0">'
                             % (image['uri'], image['width'], image['height'],
                                html.escape(caption or 'dock photo')))
                    text_out.append('- %s' % (wrap(caption) or '사진'))
                else:
                    # 못 실은 이유는 본문에 남긴다. 조용히 빠지면 형은 실렸다고 생각한다.
                    #
                    # 🔴 `<td>` 에 글을 바로 넣지 않고 `<p>` 로 감싼다 -- `cell()`/`item()`
                    # 과 같은 계약이다. Outlook 붙여넣기는 `<td>` 직속 텍스트를 11pt
                    # 선언과 무관하게 ~8pt 로 붙이므로, 감싸지 않으면 "사진이 안 실렸다"
                    # 는 이 경고만 읽기 힘든 크기로 나가 형이 못 보고 발송한다.
                    reason = note or '본문에 넣지 못했습니다'
                    inner = ('<p style="margin:0;mso-margin-top-alt:0;mso-margin-bottom-alt:0;'
                             '%s">%s</p>' % (cell_font, run(html.escape('(%s)' % reason))))
                    text_out.append('- %s (%s)' % (wrap(caption) or '사진', reason))
                marked = wrap(caption)
                caption_html = ''
                if marked:
                    text = html.escape(marked)
                    caption_html = ('<p style="margin:5px 0 0;mso-margin-top-alt:5px;'
                                    'mso-margin-bottom-alt:0;%s"><span style="%s">%s</span></p>'
                                    % (caption_font, caption_font, text))
                framed_tds.append('<td style="%s">%s%s</td>' % (frame, inner, caption_html))
            # 마지막 줄이 덜 찬 경우 무테 빈 칸으로 열 폭만 유지한다. 빈 프레임을 그리면
            # 실제 사진이 없는 자리도 사진 칸처럼 보여 혼동된다.
            filler = ('<td style="padding:%dpx;vertical-align:top">&nbsp;</td>'
                      % MAIL_IMAGE_FRAME_PAD_PX)
            framed_tds.extend([filler] * (cols - len(framed_tds)))
            rows.append('<tr>%s</tr>' % ''.join(framed_tds))
        if rows:
            chunks_out.append('<table style="border-collapse:collapse;margin:0 0 8px 52px">%s</table>'
                              % ''.join(rows))
        return chunks_out, text_out, budget, count

    budget, photos = MAIL_IMAGE_BUDGET, 0
    for section_no, key in enumerate(order, 1):
        sec = bykey.get(key) or {'section_key': key, 'label': key.title()}
        entries = _mail_entries(rid, key) or [{'kind': 'text', 'text': 'NIL'}]
        lines.extend(['', '%d. %s' % (section_no, sec['label'])])
        chunks.append('<p style="margin:0 0 6px">%s</p>' %
                      run('<b>%d. &nbsp;%s</b>' % (section_no, html.escape(sec['label']))))
        # 🔴 표·사진은 항목 번호를 받지 않는다 -- 형 지시대로 하위항목이 아니라 그 자리에
        # 놓인 블록이다. 번호는 글 항목끼리만 이어진다.
        item_no = 0
        # 🔴 표 섹션의 표는 들여쓰지 않는다(형 지시 2026-08-22). 그 섹션은 **제목이 곧 표의
        # 제목**이라 들여쓸 상위 항목이 없다.
        #
        # 판정은 `special` **이면서** 내용이 표뿐일 때로 좁힌다. 둘 다 필요하다:
        # - 고정 섹션(Shipyard/Survey/Vendor/Remark)의 제목은 표 제목이 아니라 분류명이다.
        #   거기 표만 남아 있어도 표는 그 분류 아래 놓인 항목이라 52px 를 지킨다(올마이트).
        # - special 이라도 글·사진이 섞여 있으면 위 번호 항목과 기준선을 맞춰야 한다.
        table_indent = (0 if sec.get('kind') == 'special'
                        and all(e['kind'] == 'table' for e in entries) else 52)
        for entry in entries:
            if entry['kind'] == 'table':
                lines.extend(' | '.join(row) for row in [entry['columns']] + entry['rows'])
                chunks.append(table(entry, table_indent))
                continue
            if entry['kind'] == 'image':
                grid, texts, budget, photos = photo_grid(entry, budget, photos)
                lines.extend(texts)
                chunks.extend(grid)
                continue
            item_no += 1
            lines.append('%d) %s' % (item_no, entry['text']))
            chunks.append(item(item_no, html.escape(entry['text'])))
        chunks.append(spacer)
    safety_footer = r['safety_footer'] or ''
    if safety_footer == 'Safety first. Please advise if any unsafe condition is observed.':
        safety_footer = ''
    if safety_footer:
        lines.extend(['', safety_footer])
        chunks.append('<p style="margin-top:24px">%s</p>' % run(html.escape(safety_footer)))
    chunks.append('</div>')
    return {'subject': subject, 'to': mail_to, 'from': mail_from, 'html': ''.join(chunks),
            'text': '\n'.join(lines), 'order': order}


@bp.route('/api/dock-daily/reports/<int:rid>/email-preview')
@login_required
def email_preview(rid):
    if not _report(rid): return _error('report not found', 404)
    return jsonify(_email(rid))


def _svms_limit(raw):
    """`RMK_*` 최대 byte 계약 하나를 읽는다. 값이 계약으로 못 읽히면 None.

    None 은 "한도 미설정" 이고, 미설정이면 publish 를 막는다(DESIGN §9 안전계약 3).
    🔴 `"4,000"` 같은 오타나 `"0"` 을 "설정됨" 으로 세면 안 된다 -- 앞의 구현은 raw
    문자열의 truthiness 로 게이트를 통과시키면서 화면에는 `null` 을 보여 줘, 한도가
    없는 상태로 버튼이 열렸다.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _svms_payload_hash(preview):
    raw = json.dumps(preview['fields'], ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _svms(rid):
    r = _report(rid)
    sections = _sections(r['project_id'], include_disabled=False)
    syd = _render_section(rid, 'shipyard')
    vendor = _render_section(rid, 'vendor')
    # SVMS Daily Report 계약: Shipyard/Vendor는 전용 필드, Survey+Remark만 RMK.
    # EGCS 등 Special을 RMK에 섞으면 형이 지정한 SVMS 화면 매핑과 달라진다.
    rest = []
    for key in ('survey', 'remark'):
        section = next((s for s in sections if s['section_key'] == key), None)
        if section:
            text = _render_section(rid, key)
            if text:
                rest.append('%s\n%s' % (section['label'], text))
    # 🔴 DK_CD 는 프로젝트에 등록된 SVMS Dock No 하나뿐이다. 없을 때 선박코드(`vsl_cd`)로
    # 대체하면 안 된다 -- 둘은 다른 키이고(예: DK_CD=`KWPSMD2603250001`), 대체값을
    # 보여 주면 미리보기가 "이 dock 에 반영된다" 고 거짓말을 한다.
    dk_cd = (r['svms_dk_cd'] or '').strip()
    fields = {'DK_CD': dk_cd, 'DK_SEQ': r['svms_dk_seq'],
              'DR_DT': r['report_date'].replace('-', ''), 'RMK_SYD': syd, 'RMK_VNDR': vendor,
              'RMK': '\n\n'.join(rest)}
    limits = {'RMK_SYD': _svms_limit(os.environ.get('SVMS_DOCK_DAILY_MAX_SYD')),
              'RMK_VNDR': _svms_limit(os.environ.get('SVMS_DOCK_DAILY_MAX_VNDR')),
              'RMK': _svms_limit(os.environ.get('SVMS_DOCK_DAILY_MAX_RMK'))}
    byte_counts = {k: len((v or '').encode('utf-8')) for k, v in fields.items() if k.startswith('RMK')}
    # 🔴 한도를 **넘었는지** 도 본다. 앞의 구현은 한도가 설정됐는지만 보고 초과를 통과시켜,
    # 계약상 SVMS 가 못 받는 본문에도 "반영 준비 완료" 를 띄웠다. 자동 truncate 는 금지이므로
    # 판정만 하고 자르지 않는다 -- 줄이는 건 사람이 문장을 고르는 일이다.
    over = sorted(k for k, limit in limits.items() if limit is not None and byte_counts[k] > limit)
    blockers = []
    if not dk_cd:
        blockers.append('DK_CD 미설정')
    if any(v is None for v in limits.values()):
        blockers.append('RMK byte 한도 계약 미설정')
    if over:
        blockers.append('byte 한도 초과: %s' % ', '.join(over))
    return {'fields': fields, 'byte_counts': byte_counts, 'limits': limits,
            'over_limit': over, 'blockers': blockers,
            'publishable': not blockers, 'encoding': 'UTF-8'}


@bp.route('/api/dock-daily/reports/<int:rid>/svms-preview')
@login_required
def svms_preview(rid):
    return jsonify(_svms(rid)) if _report(rid) else _error('report not found', 404)


@bp.route('/api/dock-daily/reports/<int:rid>/svms-publish', methods=['POST'])
@login_required
def svms_publish(rid):
    r = _report(rid)
    if not r: return _error('report not found', 404)
    data = _body()
    if data.get('confirmation') != 'user_preview_approved':
        return _error('SVMS 반영 확인이 필요합니다', 400, code='confirmation_required')
    if r['status'] != 'final':
        return _error('확정된 보고서만 SVMS에 반영할 수 있습니다', 409, code='final_required')
    preview = _svms(rid)
    if not preview['publishable']:
        return _error('SVMS 저장 계약을 충족하지 못했습니다', 409,
                      code='preview_blocked', blockers=preview['blockers'])
    # 서버는 SVMS에 직접 붙지 않는다. 이 승인 플래그만 맥 runner가 claim한다.
    db = get_db()
    now = _now()
    status = r['svms_sync_status'] or 'preview_only'
    if status in ('approved', 'submitting'):
        return jsonify({'id': rid, 'status': status, 'queued': True})
    if status in ('unknown', 'partial'):
        return _error('SVMS 저장 여부가 확정되지 않아 자동 재전송할 수 없습니다', 409,
                      code='manual_reconcile_required')
    if status == 'synced' and r['svms_dk_seq']:
        return jsonify({'id': rid, 'status': status, 'queued': False,
                        'dk_seq': r['svms_dk_seq'], 'message': '이미 SVMS에 반영됨'}), 200
    cur = db.execute("UPDATE dock_daily_report SET svms_sync_status='approved', svms_approved_by=?, "
                     "svms_claim_token=NULL, svms_claimed_at=NULL, svms_result_json=NULL, "
                     "svms_approved_revision=?, svms_approved_hash=?, "
                     "updated_at=datetime('now','localtime') WHERE id=? AND status='final' "
                     "AND revision=? AND svms_sync_status NOT IN ('approved','submitting','synced','unknown','partial')",
                     (session_actor(), r['revision'], _svms_payload_hash(preview), rid, r['revision']))
    if cur.rowcount != 1:
        db.rollback()
        return _error('보고서 상태가 바뀌었습니다. 다시 미리보기 하세요', 409, code='approval_race')
    db.commit()
    return jsonify({'id': rid, 'status': 'approved', 'queued': True,
                    'message': 'SVMS 반영 대기열에 등록했습니다'}), 202


@bp.route('/api/dock-daily/reports/<int:rid>/svms-reconcile', methods=['POST'])
@login_required
def svms_reconcile(rid):
    """`unknown`/`partial` 을 사람이 SVMS 화면에서 본 결과로 닫는다.

    🔴 서버가 SVMS 에 "저장됐니?" 를 물어서 자동 판정하는 건 불가능하다. `SP_SET_DOCK_DR`
       는 비멱등이라 재호출이 곧 중복 행이고(카나리 실측), 서버는 사내망 SVMS 에 닿지도
       않는다. 그래서 이 라우트는 **판정하지 않고 사람이 본 것을 기록**한다.

    🔴 이 경로가 없으면 `unknown`/`partial` 은 영구 고착이다 -- 재상신은 상태로 막혀 있고
       상태를 내릴 방법이 없어서, 형이 SVMS 에서 눈으로 확인해도 화면이 계속 "결과 불명"
       으로 남는다(올마이트 blocking).
    """
    r = _report(rid)
    if not r:
        return _error('report not found', 404)
    data = _body()
    if data.get('confirmation') != 'user_checked_svms':
        return _error('SVMS 화면 확인이 필요합니다', 400, code='confirmation_required')
    status = (r['svms_sync_status'] or 'preview_only')
    if status not in ('unknown', 'partial'):
        return _error('수동 확인이 필요한 상태가 아닙니다', 409, code='reconcile_not_applicable')
    resolution = str(data.get('resolution') or '').strip()
    if resolution not in ('synced', 'not_saved'):
        return _error("resolution must be 'synced' or 'not_saved'")
    seq = str(data.get('dk_seq') or '').strip()
    if resolution == 'synced':
        # 🔴 DK_SEQ 없이 반영됨으로 닫으면 어느 행이 들어간 건지 영구히 모른다.
        #    나중에 대조·정정할 근거가 사라지므로 필수로 받는다.
        if not seq:
            return _error('SVMS에서 확인한 DK_SEQ를 입력하세요', 400, code='dk_seq_required')
        if not seq.isdigit() or len(seq) > 8:
            return _error('DK_SEQ는 숫자만 입력하세요', 400, code='dk_seq_invalid')
        seq = seq.zfill(4)  # SVMS 는 `0002` 처럼 4자 0패딩으로 보여준다.
    new_status = 'synced' if resolution == 'synced' else 'failed'
    note = str(data.get('note') or '').strip()[:200]
    # 🔴 `not_saved` 는 `failed` 로 내려 **상신을 다시 열어준다**. 형이 SVMS 에서 저장이
    #    없음을 확인했다는 뜻이므로 이때의 재상신은 중복 행을 만들지 않는다.
    record = {'note': ('사람 확인: ' + (note or ('DK_SEQ %s 반영 확인' % seq if resolution == 'synced'
                                                else 'SVMS에 저장 안 된 것으로 확인'))),
              'manual_reconcile': True, 'resolved_from': status,
              'by': session_actor(), 'at': _now()}
    db = get_db()
    cur = db.execute("UPDATE dock_daily_report SET svms_sync_status=?, "
                     "svms_dk_seq=COALESCE(?,svms_dk_seq), svms_result_json=?, "
                     "svms_synced_at=CASE WHEN ?='synced' THEN datetime('now','localtime') "
                     "ELSE svms_synced_at END, svms_claim_token=NULL, svms_claimed_at=NULL, "
                     # CAS 는 위에서 읽은 **그 상태**로 좁힌다. `IN ('unknown','partial')` 로
                     # 두면 읽은 뒤 partial→unknown 으로 바뀐 job 도 같은 판정으로 닫힌다.
                     "updated_at=datetime('now','localtime') WHERE id=? "
                     "AND svms_sync_status=?",
                     (new_status, seq or None, json.dumps(record, ensure_ascii=False),
                      new_status, rid, status))
    if cur.rowcount != 1:
        db.rollback()
        return _error('보고서 상태가 바뀌었습니다. 다시 확인하세요', 409, code='approval_race')
    db.commit()
    return jsonify({'id': rid, 'status': new_status, 'dk_seq': seq or (r['svms_dk_seq'] or None),
                    'message': '수동 확인 결과를 기록했습니다'})


@bp.route('/api/ext/dock-daily/svms-claim', methods=['POST'])
@api_key_required
def svms_claim():
    """맥 runner 전용 CAS claim. 승인된 final 보고서만 외부 write로 넘어간다."""
    data = request.get_json(silent=True) or {}
    try:
        limit = max(1, min(int(data.get('limit', 1)), 10))
    except (TypeError, ValueError):
        return _error('limit must be an integer')
    db = get_db()
    # submitting stale는 자동 재전송하지 않고 사람 재검토 상태로 떨어뜨린다.
    db.execute("UPDATE dock_daily_report SET svms_sync_status='unknown', svms_result_json=? "
               "WHERE svms_sync_status='submitting' AND svms_claimed_at IS NOT NULL "
               "AND svms_claimed_at < datetime('now','localtime','-6 hours')",
               (json.dumps({'error': 'stale SVMS claim; manual review required'}, ensure_ascii=False),))
    jobs = []
    for row in db.execute("SELECT * FROM dock_daily_report WHERE svms_sync_status='approved' "
                          "AND status='final' ORDER BY id LIMIT ?", (limit,)).fetchall():
        token = uuid.uuid4().hex
        cur = db.execute("UPDATE dock_daily_report SET svms_sync_status='submitting', "
                         "svms_claim_token=?, svms_claimed_at=datetime('now','localtime') "
                         "WHERE id=? AND svms_sync_status='approved' AND status='final'",
                         (token, row['id']))
        if cur.rowcount != 1:
            continue
        fresh = db.execute('SELECT r.*, p.svms_dk_cd AS project_svms_dk_cd '
                           'FROM dock_daily_report r JOIN dock_daily_project p ON p.id=r.project_id '
                           'WHERE r.id=?', (row['id'],)).fetchone()
        preview = _svms(row['id'])
        if (row['svms_approved_revision'] != row['revision'] or
                row['svms_approved_hash'] != _svms_payload_hash(preview) or
                not preview['publishable']):
            db.execute("UPDATE dock_daily_report SET svms_sync_status='failed', svms_result_json=? WHERE id=?",
                       (json.dumps({'error': 'approval snapshot no longer matches final report'}, ensure_ascii=False), row['id']))
            continue
        attachments = [dict(a) for a in db.execute(
            "SELECT id, original_name FROM dock_daily_attachment WHERE report_id=? "
            "AND deleted_at IS NULL ORDER BY id", (row['id'],)).fetchall()]
        jobs.append({'report_id': row['id'], 'claim_token': token,
                     'dk_cd': (fresh['project_svms_dk_cd'] or '').strip(),
                     'dk_seq': (fresh['svms_dk_seq'] or '').strip(),
                     'dr_dt': fresh['report_date'].replace('-', ''),
                     'rmk_syd': preview['fields']['RMK_SYD'],
                     'rmk_vndr': preview['fields']['RMK_VNDR'],
                     'rmk': preview['fields']['RMK'],
                     'attachments': attachments})
    db.commit()
    return jsonify({'count': len(jobs), 'jobs': jobs})


@bp.route('/api/ext/dock-daily/attachments/<int:aid>/bytes')
@api_key_required
def svms_attachment_bytes(aid):
    try:
        report_id = int(request.args.get('report_id'))
    except (TypeError, ValueError):
        return _error('report_id is required')
    claim_token = (request.args.get('claim_token') or '').strip()
    row = query("SELECT a.stored_name, a.original_name, a.mime_type FROM dock_daily_attachment a "
                "JOIN dock_daily_report r ON r.id=a.report_id "
                "WHERE a.id=? AND a.report_id=? AND a.deleted_at IS NULL "
                "AND r.svms_sync_status='submitting' AND r.svms_claim_token=?",
                (aid, report_id, claim_token), one=True)
    if not row:
        return _error('attachment not found', 404)
    path = _attachment_path(row['stored_name'])
    if not path or not os.path.isfile(path):
        return _error('attachment file not found', 404)
    return send_file(path, mimetype=row['mime_type'], as_attachment=True,
                     download_name=os.path.basename(row['original_name']))


@bp.route('/api/ext/dock-daily/svms-result', methods=['POST'])
@api_key_required
def svms_result():
    """맥 runner 결과 회신. claim token이 맞는 현재 job만 갱신한다."""
    data = request.get_json(silent=True) or {}
    try:
        rid = int(data.get('report_id'))
    except (TypeError, ValueError):
        return _error('report_id is required')
    token = str(data.get('claim_token') or '').strip()
    if not token:
        return _error('claim_token is required')
    status = str(data.get('status') or '').strip()
    if status not in ('synced', 'partial', 'failed', 'unknown'):
        return _error('invalid SVMS result status')
    db = get_db()
    row = db.execute("SELECT * FROM dock_daily_report WHERE id=? AND svms_sync_status='submitting' "
                     "AND svms_claim_token=?", (rid, token)).fetchone()
    if not row:
        return _error('claim 불일치 또는 상태 변경됨', 409)
    seq = str(data.get('dk_seq') or '').strip() or None
    if status == 'synced' and not seq:
        return _error('synced result requires dk_seq', 400)
    # 🔴 claim token 을 결과 JSON 에 그대로 저장하면 응답에서 걷어내도 DB·백업·debug
    #    export 에 남는다(올마이트). 저장 **전에** 지운다 -- 이 컬럼은 사람이 읽을 결과
    #    기록이고, 토큰은 이 UPDATE 로 어차피 무효화된다.
    stored = {k: v for k, v in data.items() if k != 'claim_token'}
    db.execute("UPDATE dock_daily_report SET svms_sync_status=?, svms_dk_seq=COALESCE(?,svms_dk_seq), "
               "svms_readback_hash=?, svms_result_json=?, svms_synced_at=CASE WHEN ?='synced' "
               "THEN datetime('now','localtime') ELSE svms_synced_at END, svms_claim_token=NULL, "
               "svms_claimed_at=NULL, updated_at=datetime('now','localtime') WHERE id=? "
               "AND svms_sync_status='submitting' AND svms_claim_token=?",
               (status, seq, data.get('readback_hash'),
                json.dumps(stored, ensure_ascii=False)[:10000], status, rid, token))
    db.commit()
    return jsonify({'report_id': rid, 'status': status, 'dk_seq': seq})


def _event_hash(event):
    raw = json.dumps({k: event.get(k) for k in ('date', 'kind', 'title', 'body', 'progress', 'important', 'suggested_section')},
                     ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _runner_merge(pid, data):
    p = _project(pid)
    if not p: return _error('project not found', 404)
    if not p['auto_generate']:
        return _error('project is not opted in for automatic generation', 409)
    # A runner snapshot is a replaceable, complete source view. Missing flags
    # are rejected so a transport/serialization bug cannot look complete.
    if data.get('complete') is not True or data.get('partial') is not False:
        return _error('complete=true and partial=false are required; existing draft was preserved', 409, partial=True)
    events = data.get('events')
    if not isinstance(events, list): return _error('events must be an array')
    report_date = str(data.get('report_date') or (events[0].get('date') if events and isinstance(events[0], dict) else '')).strip()
    try: _date(report_date, True)
    except ValueError as e: return _error(str(e))
    if ((p['active_from'] and report_date < p['active_from']) or
            (p['active_to'] and report_date > p['active_to'])):
        return _error('report date is outside project active range', 409)
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        rid = _create_report(db, pid, report_date, 'dock-runner')
        r = db.execute('SELECT * FROM dock_daily_report WHERE id=?', (rid,)).fetchone()
        sections = {x['section_key']: x for x in _sections(pid, include_disabled=True)}
        changed = False; unmapped = 0; applied = 0; missing = 0; excluded_unzoned = 0
        seen_sources = set()
        for event in events:
            if not isinstance(event, dict) or str(event.get('date') or '') != report_date:
                db.rollback(); return _error('event date mismatch', 400)
            table = str(event.get('source_table') or '').strip(); sid = str(event.get('source_id') or '').strip()
            subkey = str(event.get('source_subkey') or '').strip()
            if not table or not sid or not subkey:
                db.rollback(); return _error('source_table, source_id and stable source_subkey are required')
            seen_sources.add((table, sid, subkey))
            source_updated_at = str(event.get('source_updated_at') or '').strip()
            # Dock timestamps without an offset cannot be safely assigned to the
            # KST business date and are intentionally excluded from auto-merge.
            if not source_updated_at or not re.search(r'(?:Z|[+-]\d{2}:\d{2})$', source_updated_at):
                excluded_unzoned += 1
                continue
            source_hash = str(event.get('source_hash') or _event_hash(event))
            link = db.execute('''SELECT * FROM dock_daily_source_link WHERE report_id=? AND source_system=?
                                AND source_table=? AND source_id=? AND source_subkey=?''',
                              (rid, 'dock_manager', table, sid, subkey)).fetchone()
            if link and link['source_hash'] == source_hash and link['missing_at'] is None and not event.get('source_missing'):
                continue
            if r['status'] == 'final':
                db.execute("UPDATE dock_daily_report SET source_changed_after_final=1, updated_at=datetime('now','localtime') WHERE id=?", (rid,))
                if link:
                    db.execute("UPDATE dock_daily_source_link SET source_hash=?, missing_at=? WHERE id=?", (source_hash, _now() if event.get('source_missing') else None, link['id']))
                continue
            key = str(event.get('suggested_section') or 'unmapped').strip().lower()
            if key not in sections or not sections[key]['enabled']:
                unmapped += 1
                if link:
                    db.execute('UPDATE dock_daily_source_link SET source_hash=?, missing_at=? WHERE id=?', (source_hash, _now() if event.get('source_missing') else None, link['id']))
                continue
            block = db.execute('SELECT id,manual_override FROM dock_daily_block WHERE id=?', (link['block_id'],)).fetchone() if link and link['block_id'] else None
            if block and block['manual_override']:
                db.execute('UPDATE dock_daily_source_link SET source_hash=?, missing_at=? WHERE id=?', (source_hash, _now() if event.get('source_missing') else None, link['id']))
                continue
            content = {'title': event.get('title') or event.get('kind') or 'Dock event', 'body': event.get('body') or '',
                       'progress': event.get('progress') or '', 'important': bool(event.get('important')),
                       'kind': event.get('kind') or ''}
            if event.get('source_missing'):
                content['source_missing'] = True; missing += 1
            if block:
                db.execute("UPDATE dock_daily_block SET section_key=?, content_json=?, origin='dock_auto', updated_at=datetime('now','localtime') WHERE id=?",
                           (key, json.dumps(content, ensure_ascii=False), block['id']))
                bid = block['id']
            else:
                cur = db.execute('''INSERT INTO dock_daily_block(report_id,section_key,sort_order,block_type,content_json,origin)
                                    VALUES (?,?,?,?,?,'dock_auto')''', (rid, key, 0, 'item', json.dumps(content, ensure_ascii=False)))
                bid = cur.lastrowid
            if link:
                db.execute("UPDATE dock_daily_source_link SET block_id=?, source_hash=?, source_updated_at=?, missing_at=? WHERE id=?",
                           (bid, source_hash, source_updated_at, _now() if event.get('source_missing') else None, link['id']))
            else:
                db.execute('''INSERT INTO dock_daily_source_link(report_id,block_id,source_system,source_table,source_id,
                              source_subkey,source_updated_at,source_hash) VALUES (?,?,?,?,?,?,?,?)''',
                           (rid, bid, 'dock_manager', table, sid, subkey, source_updated_at, source_hash))
            changed = True; applied += 1
        # The adapter response is a complete view for this project/date. An
        # absent source therefore means the prior auto block is stale. Delete
        # only untouched auto blocks; preserve manual overrides and provenance.
        for link in db.execute('''SELECT l.*, b.manual_override, b.origin
                                  FROM dock_daily_source_link l
                                  LEFT JOIN dock_daily_block b ON b.id=l.block_id
                                  WHERE l.report_id=? AND l.source_system='dock_manager'
                                    AND l.missing_at IS NULL''', (rid,)).fetchall():
            identity = (link['source_table'], link['source_id'], link['source_subkey'])
            if identity in seen_sources:
                continue
            missing += 1
            if r['status'] == 'final':
                db.execute("UPDATE dock_daily_report SET source_changed_after_final=1, updated_at=datetime('now','localtime') WHERE id=?", (rid,))
            elif link['block_id'] and not link['manual_override'] and link['origin'] == 'dock_auto':
                db.execute('DELETE FROM dock_daily_block WHERE id=? AND report_id=?', (link['block_id'], rid))
                changed = True
            db.execute('UPDATE dock_daily_source_link SET missing_at=? WHERE id=?', (_now(), link['id']))
        if changed:
            newrev = r['revision'] + 1
            cur = db.execute("UPDATE dock_daily_report SET revision=?, updated_at=datetime('now','localtime') WHERE id=? AND revision=?",
                             (newrev, rid, r['revision']))
            if cur.rowcount != 1:
                db.rollback(); return _error('revision conflict; runner will retry next run', 409, current_revision=r['revision'])
        db.execute("UPDATE dock_daily_report SET auto_snapshot_json=? WHERE id=?",
                   (json.dumps(_snapshot(rid), ensure_ascii=False), rid))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify({'report_id': rid, 'revision': _report(rid)['revision'], 'applied': applied,
                    'unmapped': unmapped, 'missing': missing, 'excluded_unzoned': excluded_unzoned,
                    'complete': True, 'partial': False})


def _ingest_retired():
    """자동수집 입구를 닫는 단일 지점.  열려 있으면 None.

    410 을 쓰는 이유: 러너가 계속 돌더라도 "일시적 실패"로 재시도하지 않고 폐기된
    경로라는 걸 로그에서 바로 알 수 있어야 한다.  404 로 숨기면 러너 쪽에서 배포
    사고와 구분되지 않는다.
    """
    if AUTO_DRAFT_INGEST_ENABLED:
        return None
    return _error('automatic draft ingestion has been retired', 410, code='auto_draft_retired')


@bp.route('/api/ext/dock-daily/projects/<int:pid>/merge', methods=['POST'])
@api_key_required
def ext_merge_project(pid):
    return _ingest_retired() or _runner_merge(pid, request.get_json(silent=True) or {})


@bp.route('/api/ext/dock-daily/projects', methods=['GET'])
@api_key_required
def ext_projects_get():
    # 목록도 같이 닫는다.  여기만 열어 두면 러너는 "대상 있음"을 보고 merge 에서만
    # 410 을 맞아, 폐기된 기능이 매일 실패 알림을 내는 모양이 된다.
    retired = _ingest_retired()
    if retired:
        return retired
    rows = query('''SELECT p.*, v.name vessel_name
                    FROM dock_daily_project p JOIN vessels v ON v.id=p.vessel_id
                    WHERE p.auto_generate=1 ORDER BY p.active_from, p.id''')
    return jsonify({'projects': [_project_response(row) for row in rows]})


@bp.route('/api/ext/dock-daily/reports/<int:rid>/merge', methods=['POST'])
@api_key_required
def ext_merge_report(rid):
    retired = _ingest_retired()
    if retired:
        return retired
    report = _report(rid)
    if not report:
        return _error('report not found', 404)
    data = request.get_json(silent=True) or {}
    data.setdefault('report_date', report['report_date'])
    result = _runner_merge(report['project_id'], data)
    return result


@bp.route('/api/ext/dock-daily/merge', methods=['POST'])
@api_key_required
def ext_merge():
    retired = _ingest_retired()
    if retired:
        return retired
    data = request.get_json(silent=True) or {}
    try: pid = int(data.get('project_id'))
    except (TypeError, ValueError): return _error('project_id is required')
    return _runner_merge(pid, data)
