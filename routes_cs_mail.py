"""Condition Survey mail HTTP adapters; admin UI and API-key-only Mac runner (mirrors Class)."""
import base64
import hashlib

from flask import Blueprint, jsonify, render_template, request, session
from helpers_shared import _cs_survey_workbook, admin_required, api_key_required
import cs_mail_service as svc
from mail_common import error_response, json_body

bp = Blueprint('routes_cs_mail', __name__)

payload = json_body


def call(fn, *args, success=200):
    try:
        return jsonify(fn(*args)), success
    except svc.CsMailError as error:
        return error_response(error)


@bp.route('/cs-mail')
@admin_required
def cs_mail_page():
    return render_template('cs_mail.html')


@bp.route('/api/cs-mail/status')
@admin_required
def api_cs_mail_status():
    return jsonify(svc.status())


@bp.route('/api/cs-mail/settings/<int:vid>/enabled', methods=['POST'])
@admin_required
def api_cs_mail_enabled(vid):
    data = payload()
    if not isinstance(data.get('enabled'), bool):
        return jsonify({'error': 'enabled must be boolean'}), 400
    return call(svc.set_enabled, vid, data['enabled'], session.get('username') or 'admin')


@bp.route('/api/cs-mail/settings/<int:vid>/recipients', methods=['PUT'])
@admin_required
def api_cs_mail_recipients(vid):
    return call(svc.set_recipients, vid, payload(), session.get('username') or 'admin')


@bp.route('/api/cs-mail/runs/<int:rid>/recover-sent', methods=['POST'])
@admin_required
def api_cs_mail_recover_sent(rid):
    return call(svc.recover_sent, rid, payload(), session.get('username') or 'admin')


@bp.route('/api/cs-mail/template', methods=['PUT'])
@admin_required
def api_cs_mail_template():
    return call(svc.save_template, payload(), session.get('username') or 'admin')


@bp.route('/api/ext/cs-mail/config')
@api_key_required
def api_ext_cs_mail_config():
    if request.args.get('preview') == '1':
        vessels = svc.settings()
        for v in vessels:
            v.pop('surveys')
        return jsonify({'today': svc.today_kst().isoformat(), 'vessels': vessels})
    return jsonify(svc.config())


@bp.route('/api/ext/cs-mail/vessels/<int:vid>/prepare')
@api_key_required
def api_ext_cs_mail_prepare(vid):
    """Exact 엑셀추출 workbooks (same generator as the CS page button), one per survey with
    Open findings, plus row identity metadata and a source fingerprint. No operational write."""
    vessel = next((v for v in svc.settings() if v['vessel_id'] == vid), None)
    if not vessel or not vessel['count']:
        return jsonify({'error': 'no open condition survey findings in roster'}), 404
    files, reply_rows = [], []
    for survey in vessel['surveys']:
        built = _cs_survey_workbook(survey['survey_id'])
        if not built:
            return jsonify({'error': 'survey changed during preparation'}), 409
        bio, filename, rows = built
        if [r['id'] for r in rows] != [f['id'] for f in survey['findings']]:
            return jsonify({'error': 'survey changed during preparation'}), 409
        raw = bio.getvalue()
        files.append({'survey_id': survey['survey_id'], 'year': survey['year'], 'quarter': survey['quarter'],
                      'filename': filename, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'xlsx_base64': base64.b64encode(raw).decode()})
        reply_rows += [{'finding_id': r['id'], 'survey_id': survey['survey_id'], 'category': r['category'] or '',
                        'no': str(r['no'] if r['no'] is not None else ''), 'item': r['item'] or '',
                        'description': r['description'] or '', 'status': r['status'] or ''} for r in rows]
    again = next((v for v in svc.settings() if v['vessel_id'] == vid), None)
    if not again or again['fingerprint'] != vessel['fingerprint']:
        return jsonify({'error': 'survey changed during preparation'}), 409
    return jsonify({'vessel_id': vid, 'fingerprint': vessel['fingerprint'], 'iso_week': vessel['iso_week'],
                    'files': files, 'excel_sha256': svc.manifest_sha([f['sha256'] for f in files]),
                    'reply_rows': reply_rows})


@bp.route('/api/ext/cs-mail/runs', methods=['POST'])
@api_key_required
def api_ext_cs_mail_claim():
    return call(svc.claim, payload(), success=201)


@bp.route('/api/ext/cs-mail/canary', methods=['POST'])
@api_key_required
def api_ext_cs_mail_canary():
    data = payload()
    if data.get('confirmed_send') is not True:
        return jsonify({'error': 'explicit confirmed_send required'}), 400
    return call(svc.claim, data, True, success=201)


@bp.route('/api/ext/cs-mail/runs/<int:rid>/state', methods=['POST'])
@api_key_required
def api_ext_cs_mail_state(rid):
    return call(svc.state, rid, payload())


@bp.route('/api/ext/cs-mail/runs/pending')
@api_key_required
def api_ext_cs_mail_pending():
    return jsonify({'runs': svc.pending()})


@bp.route('/api/ext/cs-mail/runs/<int:rid>/reply', methods=['POST'])
@api_key_required
def api_ext_cs_mail_reply(rid):
    return call(svc.apply_reply, rid, payload())


@bp.route('/api/ext/cs-mail/runs/<int:rid>/check')
@api_key_required
def api_ext_cs_mail_check(rid):
    return call(svc.check_send, rid)


@bp.route('/api/ext/cs-mail/reminders')
@api_key_required
def api_ext_cs_mail_reminders():
    import mail_reminder_service as reminders
    return jsonify(reminders.pending('cs'))


@bp.route('/api/ext/cs-mail/runs/<int:rid>/reminder', methods=['POST'])
@api_key_required
def api_ext_cs_mail_reminder(rid):
    import mail_reminder_service as reminders
    try:
        return jsonify(reminders.action('cs', rid, json_body()))
    except reminders.ReminderError as error:
        return error_response(error)
