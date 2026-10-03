"""Vetting OBS 자동 메일 (HTTP 어댑터). 규칙은 `vetting_mail_service`.

  · 관리자 화면 섹션(`/daily-mail` 하단)·API `/api/vetting-mail/*` — admin_required.
  · 맥 러너 `/api/ext/vetting-mail/*` — X-API-Key. 발송은 러너가 Outlook 웹(Aside)으로만.
"""
from flask import Blueprint, jsonify, request, session

import vetting_mail_service as svc
from helpers_shared import admin_required, api_key_required

bp = Blueprint('routes_vetting_mail', __name__)


def _err(e):
    return jsonify({'error': e.message}), e.status


def _json():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


@bp.route('/api/vetting-mail/status')
@admin_required
def api_vetting_mail_status():
    return jsonify({'vessels': svc.list_settings(), 'runs': svc.list_runs(), 'closes': svc.recent_auto_closes()})


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

