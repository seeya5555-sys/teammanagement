"""Daily 업무관리 주간 업데이트 요청 메일 — request 비의존 서비스.

HTTP 어댑터는 `routes_daily_mail.py`. 여기는 상태 전이와 DB 규칙만 가진다.

불변식(서버가 강제 — 러너 버그가 있어도 깨지지 않게):
  · 이 기능은 issues 에 INSERT 하지 않는다. 모든 이슈 쓰기는 **run 의 issue_ids 스냅샷에
    든 기존 이슈**에만, 그 이슈가 Open/InProgress 일 때만 허용한다.
  · 선박×ISO주 1 run (UNIQUE). sending/failed run 은 자동 재발송 대상이 아니다.
  · 진행이력 append 는 기존 경로와 같은 CAS(`WHERE actions=?`) 규칙.
  · 같은 메일(message_id)·종류·이슈 이벤트는 1회(UNIQUE index) — 재폴링 dedup.
  · 'COC & Flag' 이슈 자동 Close 는 class_confirmed 근거가 있을 때만. 없으면 409 → 러너는 제안으로.
"""
import json
import re
from datetime import date, datetime, timedelta

from app_core import execute, execute_rc, query

OPEN_STATUSES = ('Open', 'InProgress')
TEMPLATE_VARS = ('vessel', 'count', 'due_date', 'dear')
DEFAULT_DEAR = 'Sir/Madam'   # 선박별 Dear 이름이 비었을 때
TAG_RE = re.compile(r'\[TRMT-DU (\d{4})W(\d{2}) ([A-Z0-9]{1,8})\]')
_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-\']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')
EVENT_KINDS_GENERIC = ('reply', 'close_suggest', 'unmatched', 'reminder', 'notify')
MAX_EMAILS = 20
REPLY_DUE_DAYS = 4          # {due_date} = 발송일 + 4일(월요일 발송 → 금요일)


class DailyMailError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# ── 순수 함수 ──────────────────────────────────────────────────────
def iso_week_of(d=None):
    d = d or date.today()
    y, w, _ = d.isocalendar()
    return f'{y}W{w:02d}'


def valid_iso_week(s):
    m = re.fullmatch(r'(\d{4})W(\d{2})', s or '')
    return bool(m) and 1 <= int(m.group(2)) <= 53


def vessel_code(vessel_row):
    code = re.sub(r'[^A-Z0-9]', '', str(vessel_row['vsl_cd'] or '').upper())[:8]
    return code or f"V{vessel_row['id']}"


def make_tag(iso_week, code):
    return f'[TRMT-DU {iso_week} {code}]'


def parse_emails(text):
    """';' / ',' / 공백 구분 → (정상 리스트, 잘못된 리스트). 중복 제거, 순서 유지."""
    out, bad, seen = [], [], set()
    for part in re.split(r'[;,\s]+', text or ''):
        p = part.strip()
        if not p:
            continue
        if not _EMAIL_RE.match(p):
            bad.append(p)
            continue
        k = p.lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out, bad


_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')  # locale 무관


def format_mail_date(value):
    """형 지정 메일 날짜 형식: '07th Oct 2026'(일 2자리+서수). 파싱 불가면 원문."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return str(value)
    suf = 'th' if 11 <= d.day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d.day % 10, 'th')
    return f"{d.day:02d}{suf} {_MONTHS[d.month - 1]} {d.year}"


_LITERAL_GREETING = re.compile(r'^(\s*Dear\s+)Sir\s*/\s*Madam\b', re.I)


def render_template(tpl, vessel, count, due_date, dear=''):
    """{vessel}/{count}/{due_date}/{dear} 만 치환. str.format 을 쓰지 않는다(중괄호 주입·속성접근 차단).
    템플릿에 {dear} 가 없고 첫 인사가 'Dear Sir/Madam' 이면 그 자리를 선박별 이름으로 쓴다(형 2026-10-03)."""
    tpl = tpl or ''
    if '{dear}' not in tpl:
        tpl = _LITERAL_GREETING.sub(lambda m: m.group(1) + '{dear}', tpl, count=1)
    vals = {'vessel': str(vessel), 'count': str(count), 'due_date': format_mail_date(due_date),
            'dear': (dear or '').strip() or DEFAULT_DEAR}
    return re.sub(r'\{(vessel|count|due_date|dear)\}', lambda m: vals[m.group(1)], tpl)


def due_date_for(sent_day=None):
    return ((sent_day or date.today()) + timedelta(days=REPLY_DUE_DAYS)).isoformat()


def _json_list(raw):
    try:
        v = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ── 설정/템플릿 ─────────────────────────────────────────────────────
def get_template():
    row = query('SELECT subject_tpl, body_tpl, updated_by, updated_at FROM daily_mail_template WHERE id=1', one=True)
    return dict(row) if row else {'subject_tpl': '', 'body_tpl': '', 'updated_by': None, 'updated_at': None}


def save_template(subject_tpl, body_tpl, user):
    subject_tpl = (subject_tpl or '').strip()
    body_tpl = (body_tpl or '').strip()
    if not subject_tpl or not body_tpl:
        raise DailyMailError(400, '제목/본문 템플릿은 비울 수 없습니다.')
    if len(subject_tpl) > 300 or len(body_tpl) > 8000:
        raise DailyMailError(400, '템플릿이 너무 깁니다(제목 300자 · 본문 8000자).')
    unknown = set(re.findall(r'\{([^{}]*)\}', subject_tpl + body_tpl)) - set(TEMPLATE_VARS)
    if unknown:
        raise DailyMailError(400, '알 수 없는 변수: ' + ', '.join('{%s}' % u for u in sorted(unknown)))
    execute('INSERT INTO daily_mail_template(id, subject_tpl, body_tpl, updated_by, updated_at) '
            "VALUES(1, ?, ?, ?, datetime('now','localtime')) "
            'ON CONFLICT(id) DO UPDATE SET subject_tpl=excluded.subject_tpl, body_tpl=excluded.body_tpl, '
            'updated_by=excluded.updated_by, updated_at=excluded.updated_at',
            (subject_tpl, body_tpl, user))
    return get_template()


def open_issue_rows(vessel_id):
    return query(
        "SELECT i.*, v.name AS vessel_name, v.vessel_type AS vessel_type "
        "FROM issues i JOIN vessels v ON v.id=i.vessel_id "
        "WHERE i.vessel_id=? AND i.status IN ('Open','InProgress') "
        "ORDER BY i.issue_date ASC, i.id ASC", (vessel_id,))


# 형 지시(2026-10-03): Daily 업무현황과 같은 담당 로스터(손유석 supervisor_vessels)만 대상.
# Daily(app.js onlySupId)와 동일하게 손유석 감독 레코드가 없으면 전체(active) 유지.
ROSTER_SUPERVISOR = '손유석'


def roster_vessel_ids():
    sup = query('SELECT id FROM supervisors WHERE TRIM(name)=?', (ROSTER_SUPERVISOR,), one=True)
    if not sup:
        return None
    return {r['vessel_id'] for r in query(
        'SELECT vessel_id FROM supervisor_vessels WHERE supervisor_id=?', (sup['id'],))}


def in_roster(vessel_id):
    ids = roster_vessel_ids()
    return ids is None or vessel_id in ids


def list_settings():
    roster = roster_vessel_ids()
    rows = query(
        "SELECT v.id AS vessel_id, v.name, v.vsl_cd, v.vessel_type, "
        "       COALESCE(s.to_emails,'') AS to_emails, COALESCE(s.cc_emails,'') AS cc_emails, "
        "       COALESCE(s.dear_name,'') AS dear_name, COALESCE(s.enabled,0) AS enabled, s.updated_by, s.updated_at, "
        "       (SELECT COUNT(*) FROM issues i WHERE i.vessel_id=v.id "
        "         AND i.status IN ('Open','InProgress')) AS open_count "
        "FROM vessels v LEFT JOIN daily_mail_settings s ON s.vessel_id=v.id "
        "WHERE v.active=1 ORDER BY v.name")
    out = []
    for r in rows:
        if roster is not None and r['vessel_id'] not in roster:
            continue
        d = dict(r)
        d['code'] = vessel_code({'vsl_cd': d['vsl_cd'], 'id': d['vessel_id']})
        out.append(d)
    return out


def save_setting(vessel_id, to_text, cc_text, enabled, user, dear_name=None):
    if not query('SELECT 1 FROM vessels WHERE id=?', (vessel_id,), one=True):
        raise DailyMailError(404, '선박이 없습니다.')
    if not in_roster(vessel_id):
        raise DailyMailError(403, '담당 선박이 아닙니다.')
    to_list, bad_to = parse_emails(to_text)
    cc_list, bad_cc = parse_emails(cc_text)
    if bad_to or bad_cc:
        raise DailyMailError(400, '메일 주소 형식 오류: ' + ', '.join(bad_to + bad_cc))
    if len(to_list) > MAX_EMAILS or len(cc_list) > MAX_EMAILS:
        raise DailyMailError(400, f'수신자는 To/CC 각각 {MAX_EMAILS}명까지입니다.')
    if dear_name is None:   # 필드 누락(구 클라이언트) = 기존 값 유지, 명시적 '' 만 초기화
        cur = query('SELECT dear_name FROM daily_mail_settings WHERE vessel_id=?', (vessel_id,), one=True)
        dear_name = cur['dear_name'] if cur else ''
    dear_name = re.sub(r'\s+', ' ', str(dear_name or '')).strip()
    if len(dear_name) > 120 or re.search(r'[{}<>]', dear_name):
        raise DailyMailError(400, 'Dear 이름은 120자 이내, 중괄호·<> 불가입니다.')
    enabled = 1 if enabled else 0
    if enabled and not to_list:
        raise DailyMailError(400, 'To 주소 없이 ON 할 수 없습니다.')
    execute('INSERT INTO daily_mail_settings(vessel_id, to_emails, cc_emails, dear_name, enabled, updated_by, updated_at) '
            "VALUES(?,?,?,?,?,?,datetime('now','localtime')) "
            'ON CONFLICT(vessel_id) DO UPDATE SET to_emails=excluded.to_emails, cc_emails=excluded.cc_emails, '
            'dear_name=excluded.dear_name, '
            'enabled=excluded.enabled, updated_by=excluded.updated_by, updated_at=excluded.updated_at',
            (vessel_id, '; '.join(to_list), '; '.join(cc_list), dear_name, enabled, user))
    return {'vessel_id': vessel_id, 'to_emails': '; '.join(to_list),
            'cc_emails': '; '.join(cc_list), 'dear_name': dear_name, 'enabled': enabled}


def runner_config(iso_week=None, today=None):
    """러너용: enabled=1 이고 To 가 있는 선박만. 이번 주 run 여부를 같이 준다."""
    iso_week = iso_week or iso_week_of(today)
    tpl = get_template()
    due = due_date_for(today)
    vessels = []
    for s in list_settings():
        if not s['enabled']:
            continue
        to_list, _ = parse_emails(s['to_emails'])
        if not to_list:
            continue
        cc_list, _ = parse_emails(s['cc_emails'])
        ids = [r['id'] for r in open_issue_rows(s['vessel_id'])]
        run = query('SELECT id, state, tag FROM daily_mail_runs WHERE vessel_id=? AND iso_week=?',
                    (s['vessel_id'], iso_week), one=True)
        tag = make_tag(iso_week, s['code'])
        vessels.append({
            'vessel_id': s['vessel_id'], 'name': s['name'], 'code': s['code'],
            'enabled': 1, 'to': to_list, 'cc': cc_list,
            'open_count': len(ids), 'open_issue_ids': ids, 'tag': tag,
            'dear': s['dear_name'] or DEFAULT_DEAR,
            'subject': tag + ' ' + render_template(tpl['subject_tpl'], s['name'], len(ids), due, s['dear_name']),
            'body': render_template(tpl['body_tpl'], s['name'], len(ids), due, s['dear_name']),
            'run_this_week': dict(run) if run else None,
        })
    return {'iso_week': iso_week, 'due_date': due, 'template': tpl, 'vessels': vessels}


# ── run ────────────────────────────────────────────────────────────
def get_run(run_id):
    row = query('SELECT * FROM daily_mail_runs WHERE id=?', (run_id,), one=True)
    if not row:
        raise DailyMailError(404, 'run not found')
    return row


def claim_run(d):
    """발송 직전 claim. 같은 선박×주가 이미 있으면 409(무엇이든 — 재발송 금지)."""
    try:
        vessel_id = int(d.get('vessel_id'))
    except (TypeError, ValueError):
        raise DailyMailError(400, 'vessel_id required')
    iso_week = str(d.get('iso_week') or '')
    if not valid_iso_week(iso_week):
        raise DailyMailError(400, 'iso_week must be YYYYWww')
    v = query('SELECT id, name, vsl_cd FROM vessels WHERE id=?', (vessel_id,), one=True)
    if not v:
        raise DailyMailError(404, 'vessel not found')
    if not in_roster(vessel_id):
        raise DailyMailError(409, 'vessel not in roster')
    st = query('SELECT enabled, to_emails, cc_emails FROM daily_mail_settings WHERE vessel_id=?',
               (vessel_id,), one=True)
    if not st or not st['enabled']:
        raise DailyMailError(409, 'vessel mail disabled', code='disabled')
    code = vessel_code(v)
    tag = make_tag(iso_week, code)
    if d.get('tag') and d['tag'] != tag:
        raise DailyMailError(400, 'tag mismatch', expected=tag)
    open_ids = {r['id'] for r in open_issue_rows(vessel_id)}
    ids = d.get('issue_ids')
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        raise DailyMailError(400, 'issue_ids (non-empty int list) required')
    if not set(ids) <= open_ids:
        raise DailyMailError(409, 'issue_ids include non-open or foreign issues',
                             code='issue_scope', invalid=sorted(set(ids) - open_ids))
    existing = query('SELECT id, state FROM daily_mail_runs WHERE vessel_id=? AND iso_week=?',
                     (vessel_id, iso_week), one=True)
    if existing:
        raise DailyMailError(409, 'already claimed for this week', code='duplicate',
                             run=dict(existing))
    try:
        rid = execute(
            'INSERT INTO daily_mail_runs(vessel_id, vessel_code, iso_week, tag, state, subject, '
            'to_emails, cc_emails, issue_ids, excel_path, excel_sha256) '
            "VALUES(?,?,?,?,'sending',?,?,?,?,?,?)",
            (vessel_id, code, iso_week, tag, (d.get('subject') or '')[:500],
             st['to_emails'], st['cc_emails'], json.dumps(sorted(ids)),
             (d.get('excel_path') or '')[:500] or None, (d.get('excel_sha256') or '')[:64] or None))
    except Exception as exc:          # UNIQUE race — 다른 러너가 먼저 claim
        if 'UNIQUE' in str(exc):
            raise DailyMailError(409, 'already claimed for this week', code='duplicate')
        raise
    return dict(get_run(rid))


def set_run_state(run_id, d):
    run = get_run(run_id)
    state = d.get('state')
    if state not in ('sent', 'failed'):
        raise DailyMailError(400, "state must be 'sent' or 'failed'")
    if run['state'] != 'sending':
        if run['state'] == state:
            return dict(run)       # 응답 유실 후 재시도 — 멱등
        raise DailyMailError(409, f"run is {run['state']}")
    sent_at = (d.get('sent_at') or '').strip() or _now()
    if state == 'sent':
        execute_rc("UPDATE daily_mail_runs SET state='sent', sent_at=?, error=NULL WHERE id=? AND state='sending'",
                   (sent_at, run_id))
    else:
        execute_rc("UPDATE daily_mail_runs SET state='failed', error=? WHERE id=? AND state='sending'",
                   ((d.get('error') or '')[:1000], run_id))
    return dict(get_run(run_id))


def release_run(run_id):
    """관리자: sending/failed run 을 지워 다음 send-weekly 가 다시 보낼 수 있게.
    실제로 안 나갔는지 Outlook 보낸편지함에서 사람이 확인한 뒤에만 쓴다."""
    run = get_run(run_id)
    if run['state'] == 'sent':
        raise DailyMailError(409, '발송 완료된 run 은 해제할 수 없습니다.')
    if query('SELECT 1 FROM daily_mail_events WHERE run_id=? LIMIT 1', (run_id,), one=True):
        raise DailyMailError(409, '이벤트가 있는 run 은 해제할 수 없습니다.')
    rc = execute_rc("DELETE FROM daily_mail_runs WHERE id=? AND state IN ('sending','failed') "
                    'AND NOT EXISTS (SELECT 1 FROM daily_mail_events e WHERE e.run_id=daily_mail_runs.id)',
                    (run_id,))
    if not rc:
        raise DailyMailError(409, 'run changed concurrently')
    return {'released': run_id}


def _run_public(run):
    d = dict(run)
    d['issue_ids'] = _json_list(d.get('issue_ids'))
    return d


def pending_runs(days=21):
    """회신 폴링/리마인드 대상: 최근 N일 sent run. 이슈 현재상태·처리된 message_id 포함."""
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    out = []
    for run in query("SELECT r.*, v.name AS vessel_name FROM daily_mail_runs r "
                     "JOIN vessels v ON v.id=r.vessel_id "
                     "WHERE r.state='sent' AND COALESCE(r.sent_at, r.created_at) >= ? ORDER BY r.id",
                     (since,)):
        d = _run_public(run)
        ids = d['issue_ids']
        issues = []
        if ids:
            ph = ','.join('?' * len(ids))
            for i in query(f'SELECT id, item_topic, priority, status, due_date FROM issues WHERE id IN ({ph}) ORDER BY id', ids):
                issues.append(dict(i))
        touched = {r['issue_id'] for r in query(
            "SELECT DISTINCT issue_id FROM daily_mail_events WHERE run_id=? AND kind IN ('update','close','close_suggest') "
            "AND issue_id IS NOT NULL", (d['id'],))}
        for i in issues:
            i['updated'] = i['id'] in touched
        d['issues'] = issues
        d['processed_message_ids'] = [r['message_id'] for r in query(
            "SELECT DISTINCT message_id FROM daily_mail_events WHERE run_id=? AND kind='reply' "
            "AND message_id IS NOT NULL", (d['id'],))]
        out.append(d)
    return out


def _recompute_reply_status(run_id):
    run = get_run(run_id)
    if not query("SELECT 1 FROM daily_mail_events WHERE run_id=? AND kind='reply' LIMIT 1", (run_id,), one=True):
        return
    ids = set(_json_list(run['issue_ids']))
    touched = {r['issue_id'] for r in query(
        "SELECT DISTINCT issue_id FROM daily_mail_events WHERE run_id=? "
        "AND kind IN ('update','close','close_suggest') AND issue_id IS NOT NULL", (run_id,))}
    status = 'replied' if ids and ids <= touched else 'partial'
    execute('UPDATE daily_mail_runs SET reply_status=? WHERE id=?', (status, run_id))


# ── 이벤트 ──────────────────────────────────────────────────────────
def _sent_run_with_issue(run_id, issue_id):
    run = get_run(run_id)
    if run['state'] != 'sent':
        raise DailyMailError(409, 'run is not sent')
    if issue_id is not None:
        if issue_id not in _json_list(run['issue_ids']):
            raise DailyMailError(409, 'issue not part of this run', code='issue_scope')
        cur = query('SELECT vessel_id FROM issues WHERE id=?', (issue_id,), one=True)
        if cur and cur['vessel_id'] != run['vessel_id']:
            raise DailyMailError(409, 'issue moved to another vessel', code='issue_scope')
    return run


def _insert_event(run_id, kind, issue_id, message_id, evidence, payload, state=None):
    """dedup insert. 이미 있으면 None."""
    message_id = (message_id or '').strip()[:500] or None
    try:
        return execute(
            'INSERT INTO daily_mail_events(run_id, kind, issue_id, message_id, evidence, payload, state) '
            'VALUES(?,?,?,?,?,?,?)',
            (run_id, kind, issue_id, message_id, (evidence or '')[:4000],
             json.dumps(payload or {}, ensure_ascii=False)[:20000], state))
    except Exception as exc:
        if 'UNIQUE' in str(exc):
            return None
        raise


def record_event(run_id, d):
    kind = d.get('kind')
    if kind not in EVENT_KINDS_GENERIC:
        raise DailyMailError(400, 'kind must be one of ' + ','.join(EVENT_KINDS_GENERIC))
    issue_id = d.get('issue_id')
    if issue_id is not None and not isinstance(issue_id, int):
        raise DailyMailError(400, 'issue_id must be int')
    if kind == 'close_suggest' and issue_id is None:
        raise DailyMailError(400, 'close_suggest needs issue_id')
    if kind in ('reply', 'unmatched', 'close_suggest') and not (d.get('message_id') or '').strip():
        raise DailyMailError(400, 'message_id required')
    _sent_run_with_issue(run_id, issue_id)
    if kind == 'close_suggest':
        cur = query('SELECT status FROM issues WHERE id=?', (issue_id,), one=True)
        if not cur or cur['status'] not in OPEN_STATUSES:
            raise DailyMailError(409, 'issue is not open', code='not_open')
    payload = d.get('payload') if isinstance(d.get('payload'), dict) else {}
    eid = _insert_event(run_id, kind, issue_id, d.get('message_id'), d.get('evidence'), payload,
                        state='open' if kind == 'close_suggest' else None)
    if eid is None:
        return {'duplicate': True}
    if kind == 'reply':
        at = (d.get('received_at') or '').strip() or _now()
        execute("UPDATE daily_mail_runs SET last_reply_at=MAX(COALESCE(last_reply_at,''),?) WHERE id=?",
                (at, run_id))
        _recompute_reply_status(run_id)
    elif kind == 'close_suggest':
        _recompute_reply_status(run_id)
    elif kind == 'reminder':
        execute('UPDATE daily_mail_runs SET reminder_count=reminder_count+1, last_reminder_at=? WHERE id=?',
                ((d.get('sent_at') or '').strip() or _now(), run_id))
    elif kind == 'notify':
        execute('UPDATE daily_mail_runs SET notified_at=COALESCE(notified_at, ?) WHERE id=?', (_now(), run_id))
    return {'id': eid, 'duplicate': False}


def append_action_cas(issue_id, entry, attempts=3, vessel_id=None, require_open=False):
    """기존 `/api/ext/issues/<id>/actions` 와 같은 CAS append. 밀리면 최신 원문으로 재시도.
    vessel_id/require_open 을 주면 읽은 뒤 선박 재배정·동시 Close 를 다시 확인한다(CAS 는 actions 원문)."""
    for _ in range(attempts):
        row = query('SELECT actions, vessel_id, status FROM issues WHERE id=?', (issue_id,), one=True)
        if not row:
            raise DailyMailError(404, 'issue not found')
        if vessel_id is not None and row['vessel_id'] != vessel_id:
            raise DailyMailError(409, 'issue moved to another vessel', code='issue_scope')
        if require_open and row['status'] not in OPEN_STATUSES:
            raise DailyMailError(409, 'issue is not open', code='not_open')
        raw = row['actions']
        actions = _json_list(raw)
        merged = actions + [entry]
        new_raw = json.dumps(merged, ensure_ascii=False)
        guard = (" AND status IN ('Open','InProgress')" if require_open else '') + \
                (' AND vessel_id=?' if vessel_id is not None else '')
        extra = (vessel_id,) if vessel_id is not None else ()
        if raw is None:
            rc = execute_rc('UPDATE issues SET actions=?, updated_at=datetime("now","localtime") '
                            'WHERE id=? AND actions IS NULL' + guard, (new_raw, issue_id) + extra)
        else:
            rc = execute_rc('UPDATE issues SET actions=?, updated_at=datetime("now","localtime") '
                            'WHERE id=? AND actions=?' + guard, (new_raw, issue_id, raw) + extra)
        if rc:
            return len(merged)
    raise DailyMailError(409, 'concurrent update, retry')


def _entry(d, prefix):
    adate = (d.get('date') or '').strip()[:10]
    try:
        adate = datetime.strptime(adate, '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        adate = date.today().isoformat()
    sender = re.sub(r'\s+', ' ', str(d.get('sender') or '')).strip()[:120]
    text = (d.get('progress') or '').strip()
    src = f'[{prefix}' + (f' · {sender}' if sender else '') + ']'
    return {'date': adate, 'progress': f'{src} {text}'.strip(), 'important': False}


def run_id_vessel(run_id):
    return get_run(run_id)['vessel_id']


def append_update(run_id, issue_id, d):
    if not (d.get('message_id') or '').strip():
        raise DailyMailError(400, 'message_id required')
    progress = (d.get('progress') or '').strip()
    if not progress:
        raise DailyMailError(400, 'progress required')
    if len(progress) > 4000:
        raise DailyMailError(400, 'progress too long')
    _sent_run_with_issue(run_id, issue_id)
    cur = query('SELECT status FROM issues WHERE id=?', (issue_id,), one=True)
    if not cur:
        raise DailyMailError(404, 'issue not found')
    if cur['status'] not in OPEN_STATUSES:
        raise DailyMailError(409, 'issue is not open', code='not_open')
    payload = {'sender': d.get('sender'), 'date': d.get('date'), 'source': d.get('source') or 'reply_mail',
               'confidence': d.get('confidence')}
    eid = _insert_event(run_id, 'update', issue_id, d.get('message_id'), progress, payload)
    if eid is None:
        return {'duplicate': True}
    try:
        n = append_action_cas(issue_id, _entry(d, '회신메일'), vessel_id=run_id_vessel(run_id), require_open=True)
    except DailyMailError:
        execute('DELETE FROM daily_mail_events WHERE id=?', (eid,))   # 재시도 가능하게 dedup 키 반납
        raise
    _recompute_reply_status(run_id)
    return {'id': eid, 'duplicate': False, 'actions_count': n}


def close_issue(run_id, issue_id, d, actor='daily-mail'):
    """근거와 함께 Close. 되돌리기용 prev_status 를 이벤트에 남긴다."""
    evidence = (d.get('evidence') or '').strip()
    if not evidence:
        raise DailyMailError(400, 'evidence required')
    if not (d.get('message_id') or '').strip():
        raise DailyMailError(400, 'message_id required')
    _sent_run_with_issue(run_id, issue_id)
    cur = query('SELECT status, priority FROM issues WHERE id=?', (issue_id,), one=True)
    if not cur:
        raise DailyMailError(404, 'issue not found')
    if cur['status'] not in OPEN_STATUSES:
        raise DailyMailError(409, 'issue is not open', code='not_open')
    if cur['priority'] == 'COC & Flag' and not d.get('class_confirmed'):
        raise DailyMailError(409, 'COC & Flag issue needs explicit class confirmation', code='needs_suggestion')
    payload = {'prev_status': cur['status'], 'sender': d.get('sender'), 'date': d.get('date'),
               'class_confirmed': bool(d.get('class_confirmed')), 'basis': d.get('basis'), 'actor': actor}
    eid = _insert_event(run_id, 'close', issue_id, d.get('message_id'), evidence, payload, state='done')
    if eid is None:
        return {'duplicate': True}
    run = get_run(run_id)
    rc = execute_rc("UPDATE issues SET status='Closed', updated_at=datetime('now','localtime') "
                    'WHERE id=? AND status=? AND priority=? AND vessel_id=?',
                    (issue_id, cur['status'], cur['priority'], run['vessel_id']))
    if not rc:
        execute('DELETE FROM daily_mail_events WHERE id=?', (eid,))
        raise DailyMailError(409, 'issue changed concurrently, retry')
    try:
        append_action_cas(issue_id, _entry({'date': d.get('date'), 'sender': d.get('sender'),
                                            'progress': 'Closed — ' + evidence[:500]}, '회신메일 자동 Close'))
    except DailyMailError:
        pass        # 상태 전환과 근거(이벤트)는 이미 확정. 진행이력 한 줄은 보조라 실패해도 되돌리지 않는다.
    _recompute_reply_status(run_id)
    return {'id': eid, 'duplicate': False}


# ── 관리자 조치 ─────────────────────────────────────────────────────
def approve_suggestion(event_id, user):
    ev = query("SELECT * FROM daily_mail_events WHERE id=? AND kind='close_suggest'", (event_id,), one=True)
    if not ev:
        raise DailyMailError(404, 'suggestion not found')
    if ev['state'] != 'open':
        raise DailyMailError(409, f"suggestion is {ev['state']}")
    cur = query('SELECT status FROM issues WHERE id=?', (ev['issue_id'],), one=True)
    if not cur:
        raise DailyMailError(404, 'issue not found')
    if cur['status'] not in OPEN_STATUSES:
        execute("UPDATE daily_mail_events SET state='rejected', resolved_by=?, resolved_at=? WHERE id=?",
                (user, _now(), event_id))
        raise DailyMailError(409, '이미 Close 된 이슈입니다.')
    rc = execute_rc("UPDATE daily_mail_events SET state='approved', resolved_by=?, resolved_at=? "
                    "WHERE id=? AND state='open'", (user, _now(), event_id))
    if not rc:
        raise DailyMailError(409, 'suggestion changed concurrently')
    rc = execute_rc("UPDATE issues SET status='Closed', updated_at=datetime('now','localtime') "
                    'WHERE id=? AND status=?', (ev['issue_id'], cur['status']))
    if not rc:
        execute("UPDATE daily_mail_events SET state='open', resolved_by=NULL, resolved_at=NULL WHERE id=?", (event_id,))
        raise DailyMailError(409, 'issue changed concurrently, retry')
    execute("INSERT INTO daily_mail_events(run_id, kind, issue_id, message_id, evidence, payload, state, resolved_by) "
            "VALUES(?, 'close', ?, NULL, ?, ?, 'done', ?)",
            (ev['run_id'], ev['issue_id'], ev['evidence'],
             json.dumps({'prev_status': cur['status'], 'actor': user, 'approved_suggestion': event_id},
                        ensure_ascii=False), user))
    try:
        append_action_cas(ev['issue_id'], {'date': date.today().isoformat(), 'important': False,
                                           'progress': f'[회신메일 Close 제안 승인 · {user}] {ev["evidence"][:500]}'})
    except DailyMailError:
        pass
    return {'ok': True}


def reject_suggestion(event_id, user):
    rc = execute_rc("UPDATE daily_mail_events SET state='rejected', resolved_by=?, resolved_at=? "
                    "WHERE id=? AND kind='close_suggest' AND state='open'", (user, _now(), event_id))
    if not rc:
        raise DailyMailError(409, 'suggestion not open')
    return {'ok': True}


def reopen_close(event_id, user):
    ev = query("SELECT * FROM daily_mail_events WHERE id=? AND kind='close'", (event_id,), one=True)
    if not ev:
        raise DailyMailError(404, 'close event not found')
    if ev['state'] != 'done':
        raise DailyMailError(409, '이미 되돌렸습니다.')
    newer = query("SELECT 1 FROM daily_mail_events WHERE kind='close' AND issue_id=? AND id>? LIMIT 1",
                  (ev['issue_id'], event_id), one=True)
    if newer:
        raise DailyMailError(409, '이후 다른 Close 기록이 있어 이 기록으로는 되돌릴 수 없습니다.')
    try:
        prev = json.loads(ev['payload'] or '{}').get('prev_status')
    except (TypeError, ValueError):
        prev = None
    prev = prev if prev in OPEN_STATUSES else 'Open'
    rc = execute_rc("UPDATE issues SET status=?, updated_at=datetime('now','localtime') "
                    "WHERE id=? AND status='Closed'", (prev, ev['issue_id']))
    if not rc:
        raise DailyMailError(409, '이슈가 Closed 상태가 아닙니다(이미 다른 곳에서 변경).')
    execute("UPDATE daily_mail_events SET state='reverted', resolved_by=?, resolved_at=? WHERE id=?",
            (user, _now(), event_id))
    try:
        append_action_cas(ev['issue_id'], {'date': date.today().isoformat(), 'important': False,
                                           'progress': f'[회신메일 자동 Close 취소 · {user}] {prev} 로 되돌림'})
    except DailyMailError:
        pass
    return {'ok': True, 'status': prev}


def week_status(iso_week=None):
    iso_week = iso_week or iso_week_of()
    runs = [_run_public(r) for r in query(
        'SELECT r.*, v.name AS vessel_name FROM daily_mail_runs r JOIN vessels v ON v.id=r.vessel_id '
        'WHERE r.iso_week=? ORDER BY v.name', (iso_week,))]
    sent = [r for r in runs if r['state'] == 'sent']
    run_ids = [r['id'] for r in runs]
    unmatched = 0
    if run_ids:
        ph = ','.join('?' * len(run_ids))
        unmatched = query(f"SELECT COUNT(*) AS n FROM daily_mail_events WHERE kind='unmatched' AND run_id IN ({ph})",
                          run_ids, one=True)['n']
    suggestions = [dict(r) for r in query(
        "SELECT e.*, i.item_topic, i.priority, i.status AS issue_status, v.name AS vessel_name, r.tag "
        "FROM daily_mail_events e JOIN daily_mail_runs r ON r.id=e.run_id "
        "JOIN vessels v ON v.id=r.vessel_id LEFT JOIN issues i ON i.id=e.issue_id "
        "WHERE e.kind='close_suggest' AND e.state='open' ORDER BY e.id DESC LIMIT 200")]
    closes = [dict(r) for r in query(
        "SELECT e.*, i.item_topic, i.status AS issue_status, v.name AS vessel_name, r.tag "
        "FROM daily_mail_events e JOIN daily_mail_runs r ON r.id=e.run_id "
        "JOIN vessels v ON v.id=r.vessel_id LEFT JOIN issues i ON i.id=e.issue_id "
        "WHERE e.kind='close' AND e.created_at >= datetime('now','localtime','-30 days') "
        "ORDER BY e.id DESC LIMIT 100")]
    unmatched_rows = [dict(r) for r in query(
        "SELECT e.id, e.run_id, e.message_id, e.evidence, e.created_at, v.name AS vessel_name, r.tag "
        "FROM daily_mail_events e JOIN daily_mail_runs r ON r.id=e.run_id JOIN vessels v ON v.id=r.vessel_id "
        "WHERE e.kind='unmatched' AND e.created_at >= datetime('now','localtime','-14 days') "
        "ORDER BY e.id DESC LIMIT 50")]
    return {
        'iso_week': iso_week,
        'counts': {
            'sent': len(sent),
            'replied': sum(1 for r in sent if r['reply_status'] in ('replied', 'partial')),
            'no_reply': sum(1 for r in sent if r['reply_status'] == 'pending'),
            'reminders': sum(r['reminder_count'] for r in runs),
            'needs_review': len(suggestions) + unmatched,
            'stuck': sum(1 for r in runs if r['state'] in ('sending', 'failed')),
        },
        'runs': runs, 'suggestions': suggestions, 'closes': closes, 'unmatched': unmatched_rows,
    }


def record_attachment(run_id, issue_id, message_id, filename):
    """첨부 저장 전 dedup 예약. 이미 처리된 (메일, 이슈, 파일명)이면 None."""
    _sent_run_with_issue(run_id, issue_id)
    key = f'{(message_id or "").strip()}#{filename}'
    return _insert_event(run_id, 'attachment', issue_id, key, filename, {'filename': filename})

