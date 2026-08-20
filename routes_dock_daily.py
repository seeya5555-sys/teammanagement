"""입거 Daily Report web/API boundary.

This module intentionally has no Dock Manager or SVMS write client.  The TRMT
draft is the source of truth; runner input is normalized, API-key protected,
and merged with the same optimistic-lock contract as the browser API.
"""
import hashlib
import html
import json
import mimetypes
import os
import re
import uuid
import zipfile
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
MAX_ATTACHMENT = 20 * 1024 * 1024
MAX_OOXML_UNCOMPRESSED = 64 * 1024 * 1024
MAX_OOXML_PART = 8 * 1024 * 1024


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


def _create_report(db, project_id, report_date, actor='system'):
    p = _project(project_id)
    if not p:
        return None
    existing = db.execute('SELECT id FROM dock_daily_report WHERE project_id=? AND report_date=?',
                          (project_id, report_date)).fetchone()
    if existing:
        return existing['id']
    subject = '[Dock] M/T %s - Dock Daily Report (%s)' % (p['vessel_name'],
                                                            '%s/%s' % (report_date[5:7].lstrip('0'), report_date[8:10].lstrip('0')))
    cur = db.execute('''INSERT INTO dock_daily_report
        (project_id, report_date, status, revision, auto_snapshot_json, email_subject, email_intro, safety_footer)
        VALUES (?,?,'auto_draft',1,'{}',?,?,?)''',
        (project_id, report_date, subject,
         '안녕하십니까.\n아래와 같이 금일 입거공사 진행사항을 보고드립니다.', ''))
    rid = cur.lastrowid
    db.execute('INSERT INTO dock_daily_report_revision(report_id,revision,snapshot_json,actor) VALUES (?,?,?,?)',
               (rid, 1, json.dumps(_snapshot(rid), ensure_ascii=False), actor))
    return rid


def _error(message, code=400, **extra):
    out = {'error': message}
    out.update(extra)
    return jsonify(out), code


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
    if row['revision'] != expected:
        db.rollback(); return None, _error('revision conflict', 409, current_revision=row['revision'])
    if row['status'] == 'final':
        db.rollback(); return None, _error('final report is locked', 409, current_revision=row['revision'])
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


def _plain(block):
    c = _json(block.get('content_json'), {})
    if block.get('block_type') == 'table':
        cols = c.get('columns') or []
        rows = c.get('rows') or []
        return '\n'.join([' | '.join(map(str, cols))] + [' | '.join(map(str, x)) for x in rows])
    if block.get('block_type') == 'image': return '[Image] ' + str(c.get('caption') or '')
    return str(c.get('title') or c.get('body') or c.get('text') or '')


def _render_section(rid, key, label, as_html=False):
    blocks = _blocks_for(rid, key)
    if not blocks:
        return '<p>NIL</p>' if as_html else 'NIL'
    vals = []
    for i, b in enumerate(blocks, 1):
        txt = _plain(b)
        if as_html and b.get('block_type') == 'image':
            c = _json(b.get('content_json'), {})
            aid = int(c.get('attachment_id') or 0) if str(c.get('attachment_id') or '').isdigit() else 0
            image = ('<img src="/api/dock-daily/attachments/%d" alt="%s" style="max-width:100%%">' %
                     (aid, html.escape(c.get('caption') or 'dock image'))) if aid else html.escape(txt)
            vals.append('<div class="dd-block"><b>%s.</b> %s</div>' % (i, image))
        elif as_html and b.get('block_type') == 'table':
            c = _json(b.get('content_json'), {})
            cols = c.get('columns') or []
            rows = c.get('rows') or []
            head = ''.join('<th>%s</th>' % html.escape(str(v)) for v in cols)
            body = ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % html.escape(str(v)) for v in row)
                           for row in rows if isinstance(row, (list, tuple)))
            vals.append('<div class="dd-block"><b>%s.</b><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (i, head, body))
        else:
            vals.append(('<div class="dd-block"><b>%s.</b> %s</div>' % (i, html.escape(txt))) if as_html else '%d. %s' % (i, txt))
    return '\n'.join(vals) if not as_html else ''.join(vals)


def _email_items(rid, key):
    """Flatten card text into the numbered work-item rows used by Outlook."""
    items = []
    for block in _blocks_for(rid, key):
        items.extend(line.strip() for line in _plain(block).splitlines() if line.strip())
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
    spacer = '<p style="margin:0;line-height:1.5">&nbsp;</p>'
    chunks = [
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:11pt;line-height:1.5;color:#222">',
        '<p style="margin:0"><b>수 신 :</b> %s</p>' % html.escape(mail_to),
        '<p style="margin:0"><b>발 신 :</b> %s</p>' % html.escape(mail_from),
        spacer,
    ]
    for intro_line in intro_lines:
        chunks.extend(['<p style="margin:0">%s</p>' % html.escape(intro_line), spacer])
    chunks.extend([
        '<p style="margin:0 0 4px"><b>VESSEL ITINERARY</b></p>',
        '<table style="border-collapse:collapse;width:390px;max-width:100%;margin:0">',
    ])
    for label, value in itinerary:
        shown = _mail_date(value)
        lines.append('%s\t%s' % (label, shown))
        chunks.append('<tr><td style="border:1px solid #777;padding:4px 8px;width:55%%">%s</td>'
                      '<td style="border:1px solid #777;padding:4px 8px"><b>%s</b></td></tr>' %
                      (html.escape(label), html.escape(shown)))
    chunks.extend(['</table>', spacer])
    for section_no, key in enumerate(order, 1):
        sec = bykey.get(key) or {'section_key': key, 'label': key.title()}
        items = _email_items(rid, key)
        lines.extend(['', '%d. %s' % (section_no, sec['label'])])
        chunks.append('<p style="margin:0 0 6px"><b>%d. &nbsp;%s</b></p>' %
                      (section_no, html.escape(sec['label'])))
        for item_no, item in enumerate(items, 1):
            lines.append('%d) %s' % (item_no, item))
            chunks.append('<p style="margin:3px 0 3px 24px">%d) &nbsp;%s</p>' %
                          (item_no, html.escape(item)))
        chunks.append(spacer)
    safety_footer = r['safety_footer'] or ''
    if safety_footer == 'Safety first. Please advise if any unsafe condition is observed.':
        safety_footer = ''
    if safety_footer:
        lines.extend(['', safety_footer])
        chunks.append('<p style="margin-top:24px">%s</p>' % html.escape(safety_footer))
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
    syd = _render_section(rid, 'shipyard', 'Shipyard')
    vendor = _render_section(rid, 'vendor', 'Vendor')
    rest = []
    for s in sections:
        if s['section_key'] not in {'shipyard', 'vendor'}:
            rest.append('%s\n%s' % (s['label'], _render_section(rid, s['section_key'], s['label'])))
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


@bp.route('/api/ext/dock-daily/projects/<int:pid>/merge', methods=['POST'])
@api_key_required
def ext_merge_project(pid):
    return _runner_merge(pid, request.get_json(silent=True) or {})


@bp.route('/api/ext/dock-daily/projects', methods=['GET'])
@api_key_required
def ext_projects_get():
    rows = query('''SELECT p.*, v.name vessel_name
                    FROM dock_daily_project p JOIN vessels v ON v.id=p.vessel_id
                    WHERE p.auto_generate=1 ORDER BY p.active_from, p.id''')
    return jsonify({'projects': [_project_response(row) for row in rows]})


@bp.route('/api/ext/dock-daily/reports/<int:rid>/merge', methods=['POST'])
@api_key_required
def ext_merge_report(rid):
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
    data = request.get_json(silent=True) or {}
    try: pid = int(data.get('project_id'))
    except (TypeError, ValueError): return _error('project_id is required')
    return _runner_merge(pid, data)
