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

from flask import Blueprint, Response, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from app_core import ALLOWED_EXT, UPLOAD_DIR, app, execute, get_db, query
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


def _report_json(rid):
    r = _report(rid)
    if not r:
        return None
    out = dict(r)
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
    if sets:
        sets.append("updated_at=datetime('now','localtime')")
        execute('UPDATE dock_daily_project SET %s WHERE id=?' % ','.join(sets), (*vals, pid))
    for item in data.get('sections') or []:
        if not isinstance(item, dict) or not item.get('section_key'):
            continue
        key = str(item['section_key'])
        row = query('SELECT kind FROM dock_daily_section_def WHERE project_id=? AND section_key=?', (pid, key), one=True)
        if row and row['kind'] == 'special':
            # Fixed sections are part of the report contract. Only optional
            # project-specific sections may be renamed or disabled.
            execute('''UPDATE dock_daily_section_def SET label=COALESCE(?,label), enabled=COALESCE(?,enabled)
                       WHERE project_id=? AND section_key=?''',
                    (item.get('label'), None if 'enabled' not in item else (1 if item['enabled'] else 0), pid, key))
        elif key not in FIXED_KEYS and re.match(r'^[a-z0-9][a-z0-9_.-]{0,63}$', key):
            execute('''INSERT OR IGNORE INTO dock_daily_section_def
                       (project_id,section_key,label,sort_order,kind,enabled)
                       VALUES (?,?,?,?,?,?)''',
                    (pid, key, str(item.get('label') or key),
                     int(item.get('sort_order') or 20), 'special',
                     1 if item.get('enabled', True) else 0))
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
        # 메일 순서는 `_email` 의 `['shipyard'] + specials + ['survey','vendor','remark']` 가
        # 정한다.  여기서는 마지막 special 뒤에 붙이기만 한다.
        try:
            execute('INSERT INTO dock_daily_section_def'
                    ' (project_id,section_key,label,sort_order,kind,enabled) VALUES (?,?,?,?,?,?)',
                    (pid, key, label,
                     int(top['top'] if top and top['top'] is not None else 19) + 1, 'special', 1))
        except sqlite3.IntegrityError:
            continue
        return jsonify(_project_response(_project(pid))), 201
    return _error('section key is exhausted', 409)


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
        'SELECT id,project_id,report_date,status,revision,source_changed_after_final,updated_at '
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
        db.execute('BEGIN')
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
        section_updates = data.get('section_updates')
        if section_updates is not None:
            if not isinstance(section_updates, list):
                db.rollback(); return _error('section_updates must be an array')
            for item in section_updates:
                if not isinstance(item, dict):
                    db.rollback(); return _error('invalid section update')
                key = str(item.get('section_key') or '').strip()
                section = db.execute(
                    'SELECT kind FROM dock_daily_section_def WHERE project_id=? AND section_key=?',
                    (row['project_id'], key)).fetchone()
                if not section or section['kind'] != 'special':
                    db.rollback(); return _error('only special sections are configurable')
                db.execute('''UPDATE dock_daily_section_def
                              SET label=COALESCE(?,label), enabled=COALESCE(?,enabled), sort_order=COALESCE(?,sort_order)
                              WHERE project_id=? AND section_key=?''',
                           (item.get('label'), None if 'enabled' not in item else (1 if item['enabled'] else 0),
                            item.get('sort_order'), row['project_id'], key))
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
            if action == 'delete':
                bid = int(op.get('id') or 0)
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
            bid = int(op.get('id') or 0)
            existing = db.execute('SELECT id FROM dock_daily_block WHERE id=? AND report_id=?', (bid, rid)).fetchone() if bid else None
            if existing:
                db.execute('''UPDATE dock_daily_block SET section_key=?, parent_id=?, sort_order=?, block_type=?,
                              content_json=?, origin='manual', manual_override=1, updated_at=datetime('now','localtime')
                              WHERE id=? AND report_id=?''',
                           (sec, op.get('parent_id'), int(op.get('sort_order') or 0), typ,
                            json.dumps(content, ensure_ascii=False), bid, rid))
            else:
                db.execute('''INSERT INTO dock_daily_block(report_id,section_key,parent_id,sort_order,block_type,
                              content_json,origin,manual_override) VALUES (?,?,?,?,?,?, 'manual',1)''',
                           (rid, sec, op.get('parent_id'), int(op.get('sort_order') or 0), typ,
                            json.dumps(content, ensure_ascii=False)))
            changed = True
        if not changed:
            db.rollback()
            return jsonify(_report_json(rid))
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
    if block_id and not query('SELECT id FROM dock_daily_block WHERE id=? AND report_id=?', (block_id, rid), one=True):
        return _error('block not found', 404)
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
    aid = execute('''INSERT INTO dock_daily_attachment(report_id,block_id,stored_name,original_name,mime_type,size,sha256)
                     VALUES (?,?,?,?,?,?,?)''', (rid, block_id, stored, safe[:255], mime, len(raw), digest))
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


def _inline_image(stored_name, budget, show_px=MAIL_IMAGE_SHOW_PX, decode_px=MAIL_IMAGE_MAX_PX):
    """첨부 사진을 메일 본문용 data URI 로 만든다.

    돌려주는 값은 `(정보, 사유)` 이고 둘 중 하나만 채워진다. 사유는 화면에 그대로
    보여준다 -- 사진이 조용히 빠지면 형은 보냈다고 생각하고 메일을 보내게 된다.
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
    specials = [x for x in sections if x['kind'] == 'special']
    bykey = {x['section_key']: x for x in sections}
    order = ['shipyard'] + [x['section_key'] for x in specials] + ['survey', 'vendor', 'remark']
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
    # The numbered work items stay plain paragraphs; they never needed a table.
    # Their hanging indent keeps wrapped text under the text rather than under
    # the number, sized for a two digit number ('10)'), approximate not metric
    # exact.
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
        return ('<p style="margin:0 0 3px 52px;text-indent:-30px">%s</p>' %
                run('%d)&nbsp;&nbsp;%s' % (no, inner)))

    def table(entry):
        """카드의 표를 메일 표로. 셀 텍스트는 `cell()` 계약대로 `<td>` 안 `<p>` 에 넣는다.

        직접 `<td>` 에 넣으면 Outlook 붙여넣기에서 11pt 선언과 무관하게 작게 붙는다
        (ITINERARY 표에서 실측한 그 문제). `<thead>` 는 쓰지 않는다 -- Word HTML
        엔진이 무시하는 경우가 있어 굵게로만 헤더를 표시한다.
        """
        head = ''.join(cell('<b>%s</b>' % (html.escape(v) or '&nbsp;')) for v in entry['columns'])
        body = ''.join('<tr>%s</tr>' % ''.join(cell(html.escape(v) or '&nbsp;') for v in row)
                       for row in entry['rows'])
        return ('<table style="border-collapse:collapse;margin:0 0 8px 52px;%s">%s%s</table>'
                % (cell_font, '<tr>%s</tr>' % head if head else '', body))

    caption_font = ('font-family:Arial,Helvetica,sans-serif;font-size:9pt;'
                    'color:#4B5563;font-style:italic')

    def photo_grid(entry, budget, count):
        """사진 카드를 `columns` 열 격자로. 도크 리포트 사진 섹션과 같은 배치다.

        돌려주는 값은 `(html 조각들, 글 줄들, 남은 예산, 실은 장수)`. 예산과 장수를 인자로
        받아 돌려주는 이유는 상한이 **메일 한 통 전체**의 것이기 때문이다 -- 카드마다 새로
        세면 사진이 많은 보고서에서 상한이 사라진다.

        열이 늘면 사진이 작게 보이므로 그만큼 작은 바이트만 싣는다(표시폭의 2배까지).
        """
        cols = entry['grid']
        cell_px = max(60, (MAIL_BODY_PX - MAIL_IMAGE_GAP_PX * (cols - 1)) // cols)
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
                                                show_px=cell_px, decode_px=cell_px * 2)
                    if image:
                        budget -= image['cost']
            cells.append((image, photo['caption'], note))

        pad = 'padding:0 %dpx 2px 0;vertical-align:top' % MAIL_IMAGE_GAP_PX
        chunks_out, text_out, rows = [], [], []
        for start in range(0, len(cells), cols):
            row = cells[start:start + cols]
            # 캡션 줄은 그 줄에 캡션이 하나라도 있을 때만 만든다(도크 리포트와 같은 규칙).
            has_caption = any((caption or '').strip() for _, caption, _ in row)
            picture_tds, caption_tds = [], []
            for image, caption, note in row:
                if image:
                    inner = ('<img src="%s" width="%d" height="%d" alt="%s"'
                             ' style="display:block;border:0">'
                             % (image['uri'], image['width'], image['height'],
                                html.escape(caption or 'dock photo')))
                    text_out.append('- %s' % (caption or '사진'))
                else:
                    # 못 실은 이유는 본문에 남긴다. 조용히 빠지면 형은 실렸다고 생각한다.
                    reason = note or '본문에 넣지 못했습니다'
                    inner = run(html.escape('(%s)' % reason))
                    text_out.append('- %s (%s)' % (caption or '사진', reason))
                picture_tds.append('<td style="%s">%s</td>' % (pad, inner))
                if has_caption:
                    text = html.escape(caption) if (caption or '').strip() else '&nbsp;'
                    caption_tds.append('<td style="%s"><p style="margin:0;%s">'
                                       '<span style="%s">%s</span></p></td>'
                                       % (pad, caption_font, caption_font, text))
            # 마지막 줄이 덜 찬 경우 빈 칸으로 채워 열 폭을 흐트러뜨리지 않는다.
            filler = '<td style="%s">&nbsp;</td>' % pad
            picture_tds.extend([filler] * (cols - len(picture_tds)))
            rows.append('<tr>%s</tr>' % ''.join(picture_tds))
            if caption_tds:
                caption_tds.extend([filler] * (cols - len(caption_tds)))
                rows.append('<tr>%s</tr>' % ''.join(caption_tds))
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
        for entry in entries:
            if entry['kind'] == 'table':
                lines.extend(' | '.join(row) for row in [entry['columns']] + entry['rows'])
                chunks.append(table(entry))
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


def _svms(rid):
    r = _report(rid)
    sections = _sections(r['project_id'], include_disabled=False)
    syd = _render_section(rid, 'shipyard')
    vendor = _render_section(rid, 'vendor')
    rest = []
    for s in sections:
        if s['section_key'] not in {'shipyard', 'vendor'}:
            rest.append('%s\n%s' % (s['label'], _render_section(rid, s['section_key'])))
    fields = {'DK_CD': r['svms_dk_cd'] or r['project_vsl_cd'], 'DK_SEQ': r['svms_dk_seq'],
              'DR_DT': r['report_date'].replace('-', ''), 'RMK_SYD': syd, 'RMK_VNDR': vendor,
              'RMK': '\n\n'.join(rest)}
    limits = {'RMK_SYD': os.environ.get('SVMS_DOCK_DAILY_MAX_SYD'),
              'RMK_VNDR': os.environ.get('SVMS_DOCK_DAILY_MAX_VNDR'),
              'RMK': os.environ.get('SVMS_DOCK_DAILY_MAX_RMK')}
    byte_counts = {k: len((v or '').encode('utf-8')) for k, v in fields.items() if k.startswith('RMK')}
    return {'fields': fields, 'byte_counts': byte_counts,
            'limits': {k: int(v) if v and v.isdigit() else None for k, v in limits.items()},
            'publishable': bool(r['svms_dk_cd'] and all(limits.values())),
            'encoding': 'UTF-8'}


@bp.route('/api/dock-daily/reports/<int:rid>/svms-preview')
@login_required
def svms_preview(rid):
    return jsonify(_svms(rid)) if _report(rid) else _error('report not found', 404)


@bp.route('/api/dock-daily/reports/<int:rid>/svms-publish', methods=['POST'])
@login_required
def svms_publish(rid):
    # Deliberately fail closed: no external SVMS client exists in this MVP.
    if not _report(rid): return _error('report not found', 404)
    return _error('SVMS publish is disabled; preview only', 503, disabled=True)


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
