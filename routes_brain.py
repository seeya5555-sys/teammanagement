"""브레인 — 데쿠 memory 지식 그래프(옵시디언식 2D/3D).

맥 데쿠가 memory/*.md 를 노드·링크 JSON 으로 만들어 X-API-Key 로 /api/ext/brain 에 통째 적재한다.
열람은 소유자 계정만(웹 세션·앱 Bearer 둘 다). 다른 계정(가족 자산 계정 포함)은 404.
개인 마음 기록(personal_journal)은 제목·날짜만 노드로 붙이고 본문은 이 경로로 절대 내보내지 않는다.
"""
import json
from datetime import date
import os

from flask import Blueprint, jsonify, render_template, request, session, abort

from app_core import execute, query
from helpers_shared import api_key_required, login_required

bp = Blueprint('routes_brain', __name__)

OWNER_USERNAME = os.environ.get('JOURNAL_OWNER_USERNAME', 'SS0094')
MAX_PAYLOAD = 8_000_000
MAX_NODES = 5000
CATS = {'project', 'area', 'resource', 'archive', 'hub', 'automation', 'update'}
STR_FIELDS = ('title', 'type', 'summary', 'body', 'section', 'updated', 'status')


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
        if any(n.get('id') == 'deku' for n in nodes):
            links.append({'source': 'deku', 'target': hub})
    if any(n.get('id') == 'deku' for n in nodes):
        _add_trmt(nodes, links)
    return jsonify({'nodes': nodes, 'links': links,
                    'generated_at': data.get('generated_at'),
                    'updated_at': row['updated_at'] if row else None})


def _last_action(raw):
    try:
        acts = json.loads(raw or '[]')
    except ValueError:
        return ''
    if not isinstance(acts, list) or not acts or not isinstance(acts[-1], dict):
        return ''
    a = acts[-1]
    return f"{a.get('date') or ''} {a.get('progress') or ''}".strip()[:400]


def _add_trmt(nodes, links):
    """TRMT 선박·미종결 현안을 DB 에서 직접 붙인다(동기화 불필요, 항상 최신)."""
    vrows = query('SELECT id, name, short_name, vsl_cd FROM vessels WHERE active=1 ORDER BY name')
    irows = query("SELECT id, vessel_id, item_topic, priority, status, issue_date, due_date, actions "
                  "FROM issues WHERE status IN ('Open','InProgress') ORDER BY due_date IS NULL, due_date")
    per = {}
    for r in irows:
        per[r['vessel_id']] = per.get(r['vessel_id'], 0) + 1
    nodes.append({'id': 'hub:trmt', 'title': f'TRMT 현안 {len(irows)}', 'cat': 'hub', 'type': 'hub',
                  'summary': f'선박 {len(vrows)}척 · 미종결 현안 {len(irows)}건(Open·InProgress).'})
    links.append({'source': 'deku', 'target': 'hub:trmt'})
    mem = [n for n in nodes if n.get('cat') in CATS - {'hub', 'automation', 'update'}]
    vid = set()
    for v in vrows:
        nid = f"vessel:{v['id']}"
        vid.add(v['id'])
        nodes.append({'id': nid, 'title': v['name'], 'cat': 'vessel', 'type': 'vessel',
                      'summary': f"{v['short_name'] or ''} {v['vsl_cd'] or ''} · 미종결 현안 {per.get(v['id'], 0)}건".strip()})
        links.append({'source': 'hub:trmt', 'target': nid})
        keys = {k.lower() for k in (v['name'], v['short_name']) if k and len(k) >= 5}
        for n in mem:
            hay = f"{n.get('title', '')} {n.get('summary', '')} {n.get('body', '')}".lower()
            if any(k in hay for k in keys):
                links.append({'source': nid, 'target': n['id']})
    today = date.today().isoformat()
    for r in irows:
        if r['vessel_id'] not in vid:
            continue
        overdue = bool(r['due_date'] and r['due_date'] < today)
        st = 'fail' if overdue else ('running' if r['status'] == 'InProgress' else 'ok')
        sub = [r['priority'], r['status'], r['due_date'] and f"마감 {r['due_date']}" + (' (지남)' if overdue else '')]
        nid = f"issue:{r['id']}"
        nodes.append({'id': nid, 'title': r['item_topic'], 'cat': 'issue', 'type': 'issue', 'status': st,
                      'date': r['issue_date'], 'summary': ' · '.join(x for x in sub if x),
                      'body': _last_action(r['actions']) and ('최근 진행: ' + _last_action(r['actions']))})
        links.append({'source': f"vessel:{r['vessel_id']}", 'target': nid})
    _add_mail(nodes, links, vid)


MAIL_DAYS = 30
# 메일 종류(key, 이름). 조회는 _mail_rows 안에서 종류별 고정 SQL 리터럴로만 한다(동적 SQL 금지).
MAIL_KINDS = (('daily', 'Daily'), ('vetting', 'Vetting'), ('class', 'Class'), ('cs', 'CS'))


def _mail_rows(key):
    if key == 'daily':
        return query("SELECT r.id, r.vessel_id, r.subject, r.to_emails, r.sent_at, r.state, r.reply_status AS reply "
                     "FROM daily_mail_runs r WHERE r.state IN ('sent','failed') "
                     "AND COALESCE(r.sent_at, r.created_at) >= date('now','localtime','-30 days') "
                     "ORDER BY COALESCE(r.sent_at, r.created_at) DESC LIMIT 200")
    if key == 'vetting':
        return query("SELECT r.id, r.vessel_id, r.subject, r.to_emails, r.sent_at, r.state, "
                     "CASE WHEN EXISTS(SELECT 1 FROM vetting_mail_events e WHERE e.run_id=r.id AND e.kind='reply') "
                     "THEN 'replied' ELSE 'pending' END AS reply "
                     "FROM vetting_mail_runs r WHERE r.state IN ('sent','failed') "
                     "AND COALESCE(r.sent_at, r.created_at) >= date('now','localtime','-30 days') "
                     "ORDER BY COALESCE(r.sent_at, r.created_at) DESC LIMIT 200")
    if key == 'class':
        return query("SELECT r.id, r.vessel_id, r.subject, r.to_emails, r.sent_at, r.state, "
                     "CASE WHEN EXISTS(SELECT 1 FROM class_mail_replies e WHERE e.run_id=r.id) THEN 'replied' ELSE 'pending' END AS reply "
                     "FROM class_mail_runs r WHERE r.state IN ('sent','failed') "
                     "AND COALESCE(r.sent_at, r.created_at) >= date('now','localtime','-30 days') "
                     "ORDER BY COALESCE(r.sent_at, r.created_at) DESC LIMIT 200")
    if key == 'cs':
        return query("SELECT r.id, r.vessel_id, r.subject, r.to_emails, r.sent_at, r.state, "
                     "CASE WHEN EXISTS(SELECT 1 FROM cs_mail_replies e WHERE e.run_id=r.id) THEN 'replied' ELSE 'pending' END AS reply "
                     "FROM cs_mail_runs r WHERE r.state IN ('sent','failed') "
                     "AND COALESCE(r.sent_at, r.created_at) >= date('now','localtime','-30 days') "
                     "ORDER BY COALESCE(r.sent_at, r.created_at) DESC LIMIT 200")
    return []


def _add_mail(nodes, links, vessel_ids):
    """메일 자동화 발송건(최근 30일): 제목·수신·날짜·회신여부만. 본문·첨부·회신 원문은 넣지 않는다."""
    nodes.append({'id': 'hub:mail', 'title': '메일', 'cat': 'hub', 'type': 'hub',
                  'summary': f'메일 자동화 발송건 최근 {MAIL_DAYS}일. 제목·수신처·날짜·회신 여부만(본문 없음).'})
    links.append({'source': 'deku', 'target': 'hub:mail'})
    for key, label in MAIL_KINDS:
        rows = _mail_rows(key)
        pend = sum(1 for r in rows if r['state'] == 'sent' and r['reply'] != 'replied')
        hub = f'hub:mail:{key}'
        nodes.append({'id': hub, 'title': f'{label} 메일 {len(rows)}', 'cat': 'hub', 'type': 'hub',
                      'summary': f'최근 {MAIL_DAYS}일 발송 {len(rows)}건 · 회신 대기 {pend}건.'})
        links.append({'source': 'hub:mail', 'target': hub})
        for r in rows:
            if r['state'] == 'failed':
                st, lab = 'fail', '발송 실패'
            elif r['reply'] == 'replied':
                st, lab = 'ok', '회신 받음'
            elif r['reply'] == 'partial':
                st, lab = 'running', '일부 회신'
            else:
                st, lab = 'disabled', '회신 대기'
            nid = f'mail:{key}:{r["id"]}'
            nodes.append({'id': nid, 'title': r['subject'] or f'{label} #{r["id"]}', 'cat': 'mail', 'type': key,
                          'status': st, 'date': (r['sent_at'] or '')[:10],
                          'summary': f"{lab} · 발송 {r['sent_at'] or '-'} · 수신 {(r['to_emails'] or '')[:200]}"})
            links.append({'source': hub, 'target': nid})
            if r['vessel_id'] in vessel_ids:
                links.append({'source': f"vessel:{r['vessel_id']}", 'target': nid})


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
