"""AI NEWS — 손유석 전용 AI 뉴스 피드(형 지시 2026-11-13).

맥 데쿠가 Horizon(오픈소스) 스크레이퍼로 GitHub·Reddit 등의 AI 소식을 06:00/18:00 에 모아
X-API-Key 로 /api/ext/news 에 적재한다. 서버가 GPT mini 로 한국어 번역+중요도(0~10)를 한 번에 매기고
(실패 시 기존 공통 경로대로 Gemini 자동 백업), 6점 미만은 버리고 회차당 상위 15건만 저장한다.
8점 이상 = 중요(!). 핀하지 않은 기사는 적재 후 24시간이 지나면 자동 삭제, 핀한 기사는 영구 보관.
열람·핀은 소유자 계정만(다른 계정 404).
"""
import hashlib
import json
import os

from flask import Blueprint, abort, jsonify, render_template, request, session

from app_core import execute, get_db, query
from helpers_shared import _gemini_call_json, api_key_required, login_required

bp = Blueprint('routes_news', __name__)

OWNER_USERNAME = os.environ.get('JOURNAL_OWNER_USERNAME', 'SS0094')
NEWS_MODEL = os.environ.get('NEWS_OPENAI_MODEL', 'gpt-5.4-mini')
MAX_INCOMING = 60
KEEP_PER_BATCH = 15
MIN_SCORE = 6
IMPORTANT_SCORE = 8
AI_CHUNK = 30
AI_DEADLINE = 400   # 요청 전체 AI 시간 상한(초) — 수집기 타임아웃 600s 안
RETENTION_SQL = "datetime('now','localtime','-24 hours')"

PROMPT = """너는 AI 뉴스 편집자다. 독자는 한국 선박 기술감독으로, Claude·GPT·Gemini 같은 AI 도구와 에이전트를 업무 자동화에 적극 쓴다.
가산점 관심사: Claude/GPT/Gemini 신모델·가격·API·정책 변화, AI 에이전트·코딩 에이전트·MCP, AI 업계 큰 사건(대형 투자·IPO·경영진·규제), 해운·조선 AI.
감점: AI와 무관한 글, 단순 질문·잡담·밈, 근거 없는 소문, 마이너한 개인 프로젝트.
아래 각 항목에 대해 JSON 객체 하나만 출력한다:
{"items":[{"i":번호,"title_ko":"자연스러운 한국어 제목(고유명사·모델명·제품명은 원문 유지)","summary_ko":"핵심 1~2문장 한국어 요약","score":0~10 정수,"reason_ko":"점수 근거 한 문장"}]}
모든 항목을 빠짐없이 같은 번호로 돌려준다.

항목:
"""


def _owner_ok():
    return session.get('username') == OWNER_USERNAME


def _purge():
    execute(f"DELETE FROM news_item WHERE pinned=0 AND created_at < {RETENTION_SQL}")
    execute("DELETE FROM news_seen WHERE seen_at < datetime('now','localtime','-3 days')")


def _uid(url):
    return hashlib.sha1(url.strip().encode('utf-8')).hexdigest()


@bp.after_request
def _no_store(resp):
    if request.path.startswith('/api/news') or request.path == '/news':
        resp.headers['Cache-Control'] = 'no-store, private'
    return resp


@bp.route('/news')
@login_required
def news_page():
    if not _owner_ok():
        abort(404)
    return render_template('news.html')


def _row(r):
    d = dict(r)
    d['important'] = bool(d['score'] is not None and d['score'] >= IMPORTANT_SCORE)
    d['pinned'] = bool(d['pinned'])
    d['ai_ok'] = bool(d['ai_ok'])
    try:
        d['meta'] = json.loads(d.get('meta') or '{}')
    except ValueError:
        d['meta'] = {}
    return d


@bp.route('/api/news')
@login_required
def api_news_list():
    if not _owner_ok():
        return jsonify({'error': 'not found'}), 404
    _purge()
    view = request.args.get('view', 'latest')
    if view == 'pinned':
        rows = query("SELECT * FROM news_item WHERE pinned=1 ORDER BY pinned_at DESC, id DESC")
    else:
        rows = query("SELECT * FROM news_item ORDER BY batch_at DESC, (score IS NULL), score DESC, id DESC")
    last = query("SELECT MAX(batch_at) b FROM news_item", one=True)
    return jsonify({'items': [_row(r) for r in rows], 'last_batch': last['b'] if last else None,
                    'pinned_total': query("SELECT COUNT(*) c FROM news_item WHERE pinned=1", one=True)['c']})


@bp.route('/api/news/<int:item_id>/pin', methods=['POST'])
@login_required
def api_news_pin(item_id):
    if not _owner_ok():
        return jsonify({'error': 'not found'}), 404
    pinned = bool((request.get_json(silent=True) or {}).get('pinned', True))
    if not query("SELECT 1 FROM news_item WHERE id=?", (item_id,), one=True):
        return jsonify({'error': 'not found'}), 404
    execute("UPDATE news_item SET pinned=?, pinned_at=CASE WHEN ? THEN datetime('now','localtime') END WHERE id=?",
            (1 if pinned else 0, 1 if pinned else 0, item_id))
    return jsonify({'ok': True, 'pinned': pinned})


def _clean(raw):
    out, seen = [], set()
    for it in raw:
        if not isinstance(it, dict):
            continue
        url, title = str(it.get('url') or '').strip(), str(it.get('title') or '').strip()
        if not url.startswith(('http://', 'https://')) or not title or len(url) > 1000:
            continue
        uid = _uid(url)
        if uid in seen:
            continue
        seen.add(uid)
        meta = it.get('meta') if isinstance(it.get('meta'), dict) else {}
        out.append({'uid': uid, 'url': url, 'title': title[:400],
                    'content': str(it.get('content') or '')[:1500],
                    'source': str(it.get('source') or 'web')[:40],
                    'meta': json.dumps({k: meta[k] for k in list(meta)[:12]}, ensure_ascii=False, default=str)[:2000]})
    return out


def _score_translate(items):
    """items → {idx: {...}} ; AI 실패 시 빈 dict(호출부가 원문 저장)."""
    import time as _t
    result = {}
    t0 = _t.monotonic()
    for start in range(0, len(items), AI_CHUNK):
        if _t.monotonic() - t0 > AI_DEADLINE - 280:   # 남은 시간이 공통 호출 예산(270s)보다 적으면 중단
            break
        chunk = items[start:start + AI_CHUNK]
        text = PROMPT + '\n\n'.join(
            f"{start + n + 1}. [{it['source']}] {it['title']}\n{it['content'][:800]}" for n, it in enumerate(chunk))
        res = _gemini_call_json([{'text': text}], openai_model=NEWS_MODEL)
        rows = res.get('items') if isinstance(res, dict) else None
        if not isinstance(rows, list):
            continue
        for r in rows:
            try:
                i = int(r.get('i')) - 1
                score = max(0, min(10, int(r.get('score'))))
            except (TypeError, ValueError, AttributeError):
                continue
            if start <= i < start + len(chunk) and i not in result:   # 이 청크 번호만·첫 응답만
                result[i] = {'title_ko': str(r.get('title_ko') or '')[:400],
                             'summary_ko': str(r.get('summary_ko') or '')[:1200],
                             'reason_ko': str(r.get('reason_ko') or '')[:400], 'score': score}
    return result


@bp.route('/api/ext/news', methods=['POST'])
@api_key_required
def api_ext_news_ingest():
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not isinstance(d.get('items'), list):
        return jsonify({'ok': False, 'error': 'items list required'}), 400
    if len(d['items']) > MAX_INCOMING:
        return jsonify({'ok': False, 'error': f'max {MAX_INCOMING} items'}), 413
    _purge()
    # 이미 채점된 URL(저장·탈락 불문, 3일)과 채점 완료 기사는 다시 채점하지 않는다 — 토큰 절약·재등장 방지.
    # AI 실패로 원문만 저장된 기사(ai_ok=0)는 다음 회차에 재채점해 갱신한다.
    items = [it for it in _clean(d['items'])
             if not query("SELECT 1 FROM news_seen WHERE uid=?", (it['uid'],), one=True)
             and not query("SELECT 1 FROM news_item WHERE uid=? AND ai_ok=1", (it['uid'],), one=True)]
    if not items:
        return jsonify({'ok': True, 'received': len(d['items']), 'new': 0, 'stored': 0, 'ai_ok': True})
    ai = _score_translate(items)
    ai_ok = bool(ai)
    if ai_ok:
        ranked = sorted((i for i in ai if ai[i]['score'] >= MIN_SCORE), key=lambda i: -ai[i]['score'])
    else:   # AI 전부 실패 — 원문 그대로 상위 몇 건만 보존(수집기가 텔레그램 경보)
        ranked = list(range(min(len(items), 5)))
    batch = None   # 회차 시각은 서버 시계 기준(수집기 시계 불신)
    db = get_db()
    # 실제로 점수를 받은 항목만 seen — 실패 청크·누락 항목은 다음 회차 재시도
    db.executemany("INSERT OR IGNORE INTO news_seen(uid) VALUES(?)", [(items[i]['uid'],) for i in ai])
    stored = 0
    for i in ranked[:KEEP_PER_BATCH]:
        it, a = items[i], ai.get(i, {})
        db.execute(
            "INSERT INTO news_item(uid,url,source,title_en,content_en,title_ko,summary_ko,reason_ko,score,ai_ok,meta,batch_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,COALESCE(?,datetime('now','localtime'))) "
            "ON CONFLICT(uid) DO UPDATE SET title_ko=excluded.title_ko, summary_ko=excluded.summary_ko, "
            "reason_ko=excluded.reason_ko, score=excluded.score, ai_ok=excluded.ai_ok "
            "WHERE news_item.ai_ok=0 AND excluded.ai_ok=1",
            (it['uid'], it['url'], it['source'], it['title'], it['content'], a.get('title_ko') or it['title'],
             a.get('summary_ko') or '', a.get('reason_ko') or '', a.get('score'), 1 if ai_ok else 0, it['meta'], batch))
        stored += 1
    kept = set(ranked[:KEEP_PER_BATCH])
    for i in ai:   # 이전 회차 AI 실패로 원문 저장됐다가 이번에 탈락한 기사 정리(핀은 보존)
        if i not in kept:
            db.execute("DELETE FROM news_item WHERE uid=? AND ai_ok=0 AND pinned=0", (items[i]['uid'],))
    db.commit()
    return jsonify({'ok': True, 'received': len(d['items']), 'new': len(items), 'scored': len(ai),
                    'stored': stored, 'ai_ok': ai_ok})
