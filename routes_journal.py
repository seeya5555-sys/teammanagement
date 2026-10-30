"""개인 마음 기록 — TRMT iOS 전용, 소유자 1인만 열람.

웹 UI 없음. 열람은 앱 Bearer 토큰(브라우저 쿠키 세션 거부) + 소유자 계정만.
적재는 맥 데쿠가 X-API-Key 로 /api/ext/journal 에 upsert 한다.
🔴 같은 앱에 다른 계정(가족 자산 계정 포함)이 있으므로 소유자 판정은 소유자 username 고정값으로 한다.
응답은 전부 Cache-Control: no-store (기기 HTTP 캐시에 본문이 남지 않게).
"""
import os
import re

from flask import Blueprint, g, jsonify, request, session

from app_core import execute, query
from helpers_shared import api_key_required

bp = Blueprint('routes_journal', __name__)

OWNER_USERNAME = os.environ.get('JOURNAL_OWNER_USERNAME', 'SS0094')
KINDS = {'day', 'note'}
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MAX_BODY = 200_000


def _owner_ok():
    """앱 Bearer + 소유자 계정만. 웹 쿠키 세션·다른 계정은 존재 자체를 숨긴다(404)."""
    return bool(getattr(g, '_token_auth', False)) and session.get('username') == OWNER_USERNAME


@bp.after_request
def _no_store(resp):
    if request.path.startswith('/api/journal'):
        resp.headers['Cache-Control'] = 'no-store, private'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers.pop('ETag', None)
    return resp


@bp.route('/api/journal')
def api_journal_list():
    if not _owner_ok():
        return jsonify({'error': 'not found'}), 404
    rows = query('SELECT id, entry_date, kind, title, substr(body, 1, 160) AS preview, updated_at '
                 'FROM personal_journal ORDER BY entry_date DESC, kind DESC, id DESC')
    return jsonify({'entries': [dict(r) for r in rows]})


@bp.route('/api/journal/<int:eid>')
def api_journal_get(eid):
    if not _owner_ok():
        return jsonify({'error': 'not found'}), 404
    r = query('SELECT id, entry_date, kind, title, body, updated_at FROM personal_journal WHERE id=?',
              (eid,), one=True)
    if not r:
        return jsonify({'error': 'not found'}), 404
    return jsonify(dict(r))


@bp.route('/api/ext/journal', methods=['PUT'])
@api_key_required
def api_ext_journal_put():
    d = request.get_json(silent=True) or {}
    entries = d.get('entries')
    if not isinstance(entries, list) or not entries or len(entries) > 200:
        return jsonify({'ok': False, 'error': 'entries 1~200'}), 400
    clean = []
    for e in entries:
        if not isinstance(e, dict):
            return jsonify({'ok': False, 'error': 'entry object'}), 400
        date, kind = str(e.get('entry_date') or ''), str(e.get('kind') or 'day')
        title, body = str(e.get('title') or '').strip(), str(e.get('body') or '')
        if not DATE_RE.match(date) or kind not in KINDS or not title or len(title) > 200 \
                or not body.strip() or len(body) > MAX_BODY:
            return jsonify({'ok': False, 'error': 'invalid entry', 'entry_date': date}), 400
        clean.append((date, kind, title, body))
    for date, kind, title, body in clean:
        execute("INSERT INTO personal_journal(entry_date, kind, title, body, updated_at) "
                "VALUES(?,?,?,?,datetime('now','localtime')) "
                "ON CONFLICT(entry_date, kind, title) DO UPDATE SET body=excluded.body, "
                "updated_at=excluded.updated_at", (date, kind, title, body))
    return jsonify({'ok': True, 'upserted': len(clean)})
