"""Vetting OBS 자동 메일 (HTTP 어댑터). 규칙은 `vetting_mail_service`.

  · 관리자 화면 섹션(`/daily-mail` 하단)·API `/api/vetting-mail/*` — admin_required.
  · 맥 러너 `/api/ext/vetting-mail/*` — X-API-Key. 발송은 러너가 Outlook 웹(Aside)으로만.
"""
from flask import Blueprint, jsonify, render_template, request, session

import vetting_mail_service as svc
from mail_common import error_response, json_body
from helpers_shared import admin_required, api_key_required

bp = Blueprint('routes_vetting_mail', __name__)


_err = error_response
_json = json_body


@bp.route('/vetting-mail')
@admin_required
def vetting_mail_page():
    return render_template('vetting_mail.html')


@bp.route('/api/vetting-mail/template', methods=['GET', 'PUT'])
@admin_required
def api_vetting_mail_template():
    if request.method == 'GET':
        return jsonify({'template': svc.get_template(), 'vars': list(svc.TEMPLATE_VARS)})
    d = _json()
    try:
        return jsonify(svc.save_template(d.get('subject_tpl'), d.get('body_tpl'), session.get('username') or 'admin'))
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/vetting-mail/manual/<int:vid>', methods=['POST'])
@admin_required
def api_vetting_mail_manual(vid):
    try:
        return jsonify(svc.request_manual(vid, session.get('username') or 'admin')), 201
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/ext/vetting-mail/manual')
@api_key_required
def api_ext_vetting_mail_manual_queue():
    return jsonify(svc.queued_requests())


@bp.route('/api/ext/vetting-mail/manual/<int:qid>/claim', methods=['POST'])
@api_key_required
def api_ext_vetting_mail_manual_claim(qid):
    try:
        return jsonify(svc.claim_request(qid))
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/ext/vetting-mail/manual/<int:qid>/state', methods=['POST'])
@api_key_required
def api_ext_vetting_mail_manual_state(qid):
    try:
        return jsonify(svc.finish_request(qid, _json()))
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/vetting-mail/status')
@admin_required
def api_vetting_mail_status():
    return jsonify({'vessels': svc.list_settings(), 'runs': svc.list_runs(), 'closes': svc.recent_auto_closes(),
                    'requests': svc.list_requests(), 'replies': svc.reply_status()})


@bp.route('/api/vetting-mail/settings/<int:vid>/enabled', methods=['POST'])
@admin_required
def api_vetting_mail_enabled(vid):
    try:
        return jsonify(svc.set_enabled(vid, bool(_json().get('enabled')), session.get('username') or 'admin'))
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/vetting-mail/runs/<int:rid>/release', methods=['POST'])
@admin_required
def api_vetting_mail_release(rid):
    try:
        return jsonify(svc.release_run(rid))
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/ext/vetting-mail/config')
@api_key_required
def api_ext_vetting_mail_config():
    try:
        return jsonify(svc.runner_config(request.args.get('week')))
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/ext/vetting-mail/runs', methods=['POST'])
@api_key_required
def api_ext_vetting_mail_claim():
    try:
        return jsonify(svc.claim_run(_json())), 201
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/ext/vetting-mail/runs/<int:rid>/state', methods=['POST'])
@api_key_required
def api_ext_vetting_mail_state(rid):
    try:
        return jsonify(svc.set_run_state(rid, _json()))
    except svc.VettingMailError as e:
        return _err(e)


@bp.route('/api/ext/vetting-mail/runs/pending')
@api_key_required
def api_ext_vetting_mail_pending():
    return jsonify(svc.pending_runs())

