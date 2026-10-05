"""Daily 업무관리 — 주간 현안 업데이트 요청 메일 자동화 (HTTP 어댑터).

상태 규칙은 `daily_mail_service`. 여기는 인증·입출력만.
  · 관리자 화면/API (`/daily-mail`, `/api/daily-mail/*`) — admin_required + 쿠키 CSRF.
  · 맥 러너 (`/api/ext/daily-mail/*`) — X-API-Key. 발송·회신 처리는 러너가 Outlook.app 으로만 한다.
"""
import os
import uuid
from datetime import date

from flask import Blueprint, Response, jsonify, render_template, request, session
from werkzeug.utils import secure_filename

from app_core import UPLOAD_DIR, execute
from issue_export_service import build_issue_workbook
import daily_mail_service as svc
from mail_common import error_response, json_body
from helpers_shared import _issue_to_dict, _translate_rows_en, admin_required, api_key_required

bp = Blueprint('routes_daily_mail', __name__)

_ATTACH_EXT = {'jpg', 'jpeg', 'png', 'gif', 'heic', 'heif', 'webp', 'pdf'}


_err = error_response
_json = json_body


def _user():
    return session.get('username') or 'admin'


# ── 관리자 화면 ─────────────────────────────────────────────────────
@bp.route('/daily-mail')
@admin_required
def daily_mail_page():
    return render_template('daily_mail.html')


@bp.route('/api/daily-mail/settings')
@admin_required
def api_daily_mail_settings():
    return jsonify({'vessels': svc.list_settings(), 'template': svc.get_template(),
                    'vars': list(svc.TEMPLATE_VARS)})


@bp.route('/api/daily-mail/settings/<int:vid>', methods=['PUT'])
@admin_required
def api_daily_mail_setting_save(vid):
    d = _json()
    try:
        return jsonify(svc.save_setting(vid, d.get('to_emails'), d.get('cc_emails'),
                                        bool(d.get('enabled')), _user(), d['dear_name'] if 'dear_name' in d else None))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/daily-mail/settings/<int:vid>/enabled', methods=['POST'])
@admin_required
def api_daily_mail_setting_enabled(vid):
    try:
        return jsonify(svc.set_enabled(vid, bool(_json().get('enabled')), _user()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/daily-mail/template', methods=['PUT'])
@admin_required
def api_daily_mail_template_save():
    d = _json()
    try:
        return jsonify(svc.save_template(d.get('subject_tpl'), d.get('body_tpl'), _user()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/daily-mail/status')
@admin_required
def api_daily_mail_status():
    week = request.args.get('week') or None
    if week and not svc.valid_iso_week(week):
        return jsonify({'error': 'week must be YYYYWww'}), 400
    return jsonify(svc.week_status(week))


@bp.route('/api/daily-mail/suggestions/<int:eid>/approve', methods=['POST'])
@admin_required
def api_daily_mail_suggestion_approve(eid):
    try:
        return jsonify(svc.approve_suggestion(eid, _user()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/daily-mail/suggestions/<int:eid>/reject', methods=['POST'])
@admin_required
def api_daily_mail_suggestion_reject(eid):
    try:
        return jsonify(svc.reject_suggestion(eid, _user()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/daily-mail/closes/<int:eid>/reopen', methods=['POST'])
@admin_required
def api_daily_mail_close_reopen(eid):
    try:
        return jsonify(svc.reopen_close(eid, _user()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/daily-mail/runs/<int:rid>/release', methods=['POST'])
@admin_required
def api_daily_mail_run_release(rid):
    try:
        return jsonify(svc.release_run(rid))
    except svc.DailyMailError as e:
        return _err(e)


# ── 맥 러너 (X-API-Key) ─────────────────────────────────────────────
@bp.route('/api/ext/daily-mail/config')
@api_key_required
def api_ext_daily_mail_config():
    week = request.args.get('week') or None
    if week and not svc.valid_iso_week(week):
        return jsonify({'error': 'week must be YYYYWww'}), 400
    return jsonify(svc.runner_config(week))


@bp.route('/api/ext/daily-mail/vessels/<int:vid>/export.xlsx')
@api_key_required
def api_ext_daily_mail_export(vid):
    """선박 1척의 Open/InProgress 이슈 영문 xlsx (Issue ID · 회신 칸 포함)."""
    raw = svc.open_issue_rows(vid)
    rows = [_issue_to_dict(r) for r in raw]
    if not rows:
        return jsonify({'error': 'no open issues'}), 404
    if request.args.get('translate', '1') != '0':
        _translate_rows_en(rows)
    try:
        data, fname, _ = build_issue_workbook(rows, en=True, sub_text='Open / In Progress',
                                              exported_by='TRMT')
    except ImportError:
        return jsonify({'error': 'openpyxl 미설치'}), 500
    resp = Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp.headers['Content-Disposition'] = f'attachment; filename="{fname}"'
    resp.headers['X-Issue-Ids'] = ','.join(str(r['id']) for r in rows)
    resp.headers['X-Issue-Fp'] = svc.issue_fingerprint(raw)   # 번역 전 원문 지문(러너 준비분 최신성 확인)
    return resp


@bp.route('/api/ext/daily-mail/runs', methods=['POST'])
@api_key_required
def api_ext_daily_mail_run_claim():
    try:
        return jsonify(svc.claim_run(_json())), 201
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/ext/daily-mail/runs/<int:rid>/state', methods=['POST'])
@api_key_required
def api_ext_daily_mail_run_state(rid):
    try:
        return jsonify(svc.set_run_state(rid, _json()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/ext/daily-mail/runs/pending')
@api_key_required
def api_ext_daily_mail_runs_pending():
    return jsonify({'runs': svc.pending_runs(), 'today': date.today().isoformat()})


@bp.route('/api/ext/daily-mail/runs/<int:rid>/events', methods=['POST'])
@api_key_required
def api_ext_daily_mail_event(rid):
    try:
        return jsonify(svc.record_event(rid, _json()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/ext/daily-mail/runs/<int:rid>/issues/<int:iid>/progress', methods=['POST'])
@api_key_required
def api_ext_daily_mail_progress(rid, iid):
    try:
        return jsonify(svc.append_update(rid, iid, _json()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/ext/daily-mail/runs/<int:rid>/new-issues', methods=['POST'])
@api_key_required
def api_ext_daily_mail_new_issue(rid):
    try:
        return jsonify(svc.create_issue_from_reply(rid, _json()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/ext/daily-mail/runs/<int:rid>/issues/<int:iid>/close', methods=['POST'])
@api_key_required
def api_ext_daily_mail_close(rid, iid):
    try:
        return jsonify(svc.close_issue(rid, iid, _json()))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/ext/daily-mail/runs/<int:rid>/issues/<int:iid>/close-suggest', methods=['POST'])
@api_key_required
def api_ext_daily_mail_close_suggest(rid, iid):
    d = _json()
    d['kind'] = 'close_suggest'
    d['issue_id'] = iid
    try:
        return jsonify(svc.record_event(rid, d))
    except svc.DailyMailError as e:
        return _err(e)


@bp.route('/api/ext/daily-mail/runs/<int:rid>/issues/<int:iid>/attachments', methods=['POST'])
@api_key_required
def api_ext_daily_mail_attachment(rid, iid):
    """회신 메일의 사진/PDF 를 이슈 첨부로. (메일, 이슈, 파일명) 1회."""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'file required'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in _ATTACH_EXT:
        return jsonify({'error': 'only image/pdf allowed'}), 400
    msg = (request.form.get('message_id') or '').strip()
    if not msg:
        return jsonify({'error': 'message_id required'}), 400
    try:
        eid = svc.record_attachment(rid, iid, msg, f.filename[:200])
    except svc.DailyMailError as e:
        return _err(e)
    if eid is None:
        return jsonify({'duplicate': True})
    stored = f'{uuid.uuid4().hex}.{ext}'
    path = os.path.join(UPLOAD_DIR, stored)
    f.save(path)
    aid = execute('INSERT INTO attachments(issue_id, filename, stored_name, file_size, mime_type, uploaded_by) '
                  'VALUES(?,?,?,?,?,?)',
                  (iid, secure_filename(f.filename) or stored, stored, os.path.getsize(path),
                   f.mimetype or '', 'daily-mail'))
    return jsonify({'id': aid, 'duplicate': False}), 201
