"""브레인 — 데쿠 memory 지식 그래프(옵시디언식 2D/3D).

맥 데쿠가 memory/*.md 를 노드·링크 JSON 으로 만들어 X-API-Key 로 /api/ext/brain 에 통째 적재한다.
열람은 소유자 계정만(웹 세션·앱 Bearer 둘 다). 다른 계정(가족 자산 계정 포함)은 404.
개인 마음 기록(personal_journal)은 제목·날짜만 노드로 붙이고 본문은 이 경로로 절대 내보내지 않는다.
"""
import json
import os

from flask import Blueprint, jsonify, render_template, request, session, abort

from app_core import execute, query
from helpers_shared import api_key_required, login_required

bp = Blueprint('routes_brain', __name__)

OWNER_USERNAME = os.environ.get('JOURNAL_OWNER_USERNAME', 'SS0094')
MAX_PAYLOAD = 8_000_000
MAX_NODES = 5000
CATS = {'project', 'area', 'resource', 'archive'}
STR_FIELDS = ('title', 'type', 'summary', 'body', 'section', 'updated')


def _owner_ok():
    return session.get('username') == OWNER_USERNAME


@bp.after_request
def _no_store(resp):
    if request.path.startswith('/api/brain') or request.path == '/brain':
        resp.headers['Cache-Control'] = 'no-store, private'
    return resp


@bp.route('/brain')
@login_required
def brain_page():
    if not _owner_ok():
        abort(404)
    return render_template('brain.html')


@bp.route('/api/brain/graph')
@login_required
def api_brain_graph():
    if not _owner_ok():
        return jsonify({'error': 'not found'}), 404
    row = query('SELECT payload, updated_at FROM brain_graph WHERE id=1', one=True)
    data = json.loads(row['payload']) if row else {'nodes': [], 'links': []}
    nodes, links = list(data.get('nodes') or []), list(data.get('links') or [])
    # 마음 기록: 제목·날짜만. 본문은 앱(/api/journal) 전용.
    jrows = query('SELECT id, entry_date, kind, title FROM personal_journal '
                  'ORDER BY entry_date DESC, id DESC LIMIT 300')
    if jrows:
        hub = 'personal:hub'
        nodes.append({'id': hub, 'title': '마음 기록', 'cat': 'personal', 'hub': True,
                      'summary': '형 개인 기록 모음. 본문은 TRMT 앱에서만 열람.'})
        for j in jrows:
            nid = f"personal:{j['id']}"
            nodes.append({'id': nid, 'title': j['title'], 'cat': 'personal',
                          'date': j['entry_date'], 'summary': ''})
            links.append({'source': hub, 'target': nid})
    return jsonify({'nodes': nodes, 'links': links,
                    'generated_at': data.get('generated_at'),
                    'updated_at': row['updated_at'] if row else None})


@bp.route('/api/ext/brain', methods=['PUT'])
@api_key_required
def api_ext_brain_put():
    raw = request.get_data(cache=False)
    if len(raw) > MAX_PAYLOAD:
        return jsonify({'ok': False, 'error': 'payload too large'}), 413
    try:
        d = json.loads(raw or b'{}')
    except ValueError:
        return jsonify({'ok': False, 'error': 'invalid json'}), 400
    if not isinstance(d, dict):
        return jsonify({'ok': False, 'error': 'object required'}), 400
    nodes, links = d.get('nodes'), d.get('links')
    if not isinstance(nodes, list) or not isinstance(links, list) or not nodes or len(nodes) > MAX_NODES:
        return jsonify({'ok': False, 'error': 'nodes 1~5000, links list'}), 400
    ids = set()
    clean_nodes = []
    for n in nodes:
        nid = n.get('id') if isinstance(n, dict) else None
        if not isinstance(nid, str) or not nid or len(nid) > 200 or nid.startswith('personal:') \
                or nid in ids or not isinstance(n.get('cat'), str) or n['cat'] not in CATS:
            return jsonify({'ok': False, 'error': 'invalid node', 'id': nid if isinstance(nid, str) else None}), 400
        ids.add(nid)
        c = {'id': nid, 'cat': n['cat'], 'superseded': bool(n.get('superseded'))}
        for k in STR_FIELDS:
            v = n.get(k)
            if v is not None:
                c[k] = str(v)[:20000 if k == 'body' else 1000]
        clean_nodes.append(c)
    clean_links = []
    for l in links:
        if not isinstance(l, dict):
            continue
        s_, t_ = l.get('source'), l.get('target')
        if isinstance(s_, str) and isinstance(t_, str) and s_ in ids and t_ in ids:
            clean_links.append({'source': s_, 'target': t_})
    payload = json.dumps({'nodes': clean_nodes, 'links': clean_links,
                          'generated_at': str(d.get('generated_at') or '')[:40]}, ensure_ascii=False)
    execute("INSERT INTO brain_graph(id, payload, updated_at) VALUES(1, ?, datetime('now','localtime')) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (payload,))
    return jsonify({'ok': True, 'nodes': len(clean_nodes), 'links': len(clean_links)})
