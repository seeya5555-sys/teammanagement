"""Condition Survey (CS) mail: CONTAINER vessels only, separate OFF-by-default switches,
shared Daily recipients, pinned snapshots, one weekly mail per vessel.

Attachments are the exact 엑셀추출 workbooks (helpers_shared._cs_survey_workbook), one per
survey that still has >=1 Open finding. Replies append a dated English remark and may close a
finding only when the runner judged the rectification as actually completed. Never creates,
deletes or reopens findings.
"""
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app_core import execute, execute_rc, get_db, query
from mail_common import MailServiceError
import daily_mail_service as dm

SUBJECT = '{vessel} - Condition Survey ({quarter}): Rectification Status Required'
BODY = '''Dear {dear},

Please find attached the open Condition Survey findings for {vessel} ({quarter}). One Excel file is attached for each survey that still has open findings.

1) Please enter the rectification carried out or the action plan for each open finding in the "RECTIFICATION" column of the attached Excel file(s) and return the updated file(s) by replying to this email.
2) For completed findings, please describe the work completed and the completion date, and attach supporting photos where available.
3) For planned or ongoing findings, please include the proposed completion date and any arrangements for spare parts, service engineers or shore support.

Please keep the other columns unchanged so that your remarks can be matched to the relevant findings. A finding will be closed only when the rectification is reported as completed.

Thank you for your cooperation.'''
VARS = ('vessel', 'dear', 'quarter')
RECIPIENT_FIELDS = ('to_emails', 'cc_emails', 'dear_name')
# Distinct from Class's "[TRMT-CS " Outlook scan prefix: "[TRMT-COND " never contains it.
TAG_PREFIX = '[TRMT-COND '
TAG_RE = re.compile(r'\[TRMT-COND \d{4}W\d{2} [A-Z0-9]{1,8}\]')
CONTAINER_TYPES = ('CNTR', 'CONTAINER')
KOREAN = re.compile(r'[ᄀ-ᇿ㄰-㆏가-힯]')


class CsMailError(MailServiceError):
    pass


def today_kst():
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def is_container(vessel_type):
    """CS 화면(cs.js)은 'CNTR'로 저장·표시. 'CONTAINER' 표기도 같은 선종으로 본다."""
    return str(vessel_type or '').strip().upper() in CONTAINER_TYPES


def item_key(finding):
    return digest([finding['survey_id'], finding['category'], str(finding['no'] or ''),
                   finding['item'] or '', finding['description'] or ''])


def manifest_sha(shas):
    return hashlib.sha256('\n'.join(shas).encode()).hexdigest()


def surveys_for(vid):
    """Surveys with >=1 Open finding; every finding of those surveys (the workbook rows)."""
    out = []
    for s in query("SELECT id,year,quarter,inspection_date,vendor FROM cs_surveys WHERE vessel_id=? "
                   "AND EXISTS (SELECT 1 FROM cs_findings f WHERE f.survey_id=cs_surveys.id AND f.status='Open') "
                   "ORDER BY year,quarter,id", (vid,)):
        rows = query("SELECT id,survey_id,category,no,item,description,status FROM cs_findings WHERE survey_id=? "
                     "ORDER BY CASE category WHEN 'Defect' THEN 0 ELSE 1 END, no, id", (s['id'],))
        out.append({'survey_id': s['id'], 'year': s['year'], 'quarter': s['quarter'],
                    'inspection_date': s['inspection_date'], 'vendor': s['vendor'],
                    'findings': [dict(r) for r in rows]})
    return out


def open_findings(surveys):
    return [f for s in surveys for f in s['findings'] if f['status'] == 'Open']


def next_send(day=None):
    day = day or today_kst()
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    start = 1 if day == now.date() and (now.hour, now.minute) >= (7, 30) else 0
    for offset in range(start, 8):
        candidate = day + timedelta(days=offset)
        if candidate.weekday() == 0:
            return candidate.isoformat() + ' 07:30'
    return None


def template():
    row = query('SELECT subject_tpl,body_tpl,updated_at FROM cs_mail_template WHERE id=1', one=True)
    return dict(row) if row else {'subject_tpl': SUBJECT, 'body_tpl': BODY, 'updated_at': None}


def save_template(data, user):
    subject, body = str(data.get('subject_tpl') or '').strip(), str(data.get('body_tpl') or '').strip()
    unknown = set(re.findall(r'\{([^{}]*)\}', subject + body)) - set(VARS)
    if not subject or not body or len(subject) > 300 or len(body) > 8000 or unknown:
        raise CsMailError(400, '템플릿 제목·본문·변수를 확인하세요.')
    if re.search(r'[\r\n]', subject) or KOREAN.search(subject + body):
        raise CsMailError(400, '메일 템플릿은 영문으로 작성하세요.')
    execute("INSERT INTO cs_mail_template(id,subject_tpl,body_tpl,updated_by) VALUES(1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET subject_tpl=excluded.subject_tpl,body_tpl=excluded.body_tpl,updated_by=excluded.updated_by,updated_at=datetime('now','localtime')", (subject, body, user))
    return template()


def quarter_label(surveys):
    """'2026 Q3' / '2026 Q1, Q3' — surveys that still have Open findings (= attached workbooks)."""
    years = {}
    for s in surveys:
        years.setdefault(s['year'], []).append(f"Q{s['quarter']}")
    return ' / '.join(f"{y} {', '.join(qs)}" for y, qs in years.items())


def render(tpl, name, dear, quarter=''):
    values = {'vessel': name, 'dear': dear.strip() or dm.DEFAULT_DEAR, 'quarter': quarter}
    return re.sub(r'\{(vessel|dear|quarter)\}', lambda m: values[m.group(1)], tpl)


def settings(day=None):
    day = day or today_kst()
    roster = dm.roster_vessel_ids()
    if roster is None:      # fail-closed like Class
        return []
    tpl = template()
    out = []
    for contact in dm.list_settings():
        vid = contact['vessel_id']
        if vid not in roster or not is_container(contact.get('vessel_type')):
            continue
        row = query('SELECT enabled,to_emails,cc_emails,dear_name FROM cs_mail_settings WHERE vessel_id=?', (vid,), one=True)
        daily = {k: contact[k] for k in RECIPIENT_FIELDS}
        custom = {k: row is not None and row[k] is not None for k in RECIPIENT_FIELDS}
        contact = dict(contact, **{k: row[k] for k in RECIPIENT_FIELDS if custom[k]})
        surveys = surveys_for(vid)
        opened = open_findings(surveys)
        to, bad_to = dm.parse_emails(contact['to_emails'])
        cc, bad_cc = dm.parse_emails(contact['cc_emails'])
        week = dm.iso_week_of(day)
        tag = f"{TAG_PREFIX}{week} V{vid}]"
        subject = tag + ' ' + render(tpl['subject_tpl'], contact['name'], contact['dear_name'], quarter_label(surveys))
        body = render(tpl['body_tpl'], contact['name'], contact['dear_name'], quarter_label(surveys))
        blocker = '수신처 오류·To 누락' if not to or bad_to or bad_cc else ''
        if KOREAN.search(subject + body):
            blocker = '선명·Dear 영문 확인 필요'
        run = query('SELECT id,state FROM cs_mail_runs WHERE vessel_id=? AND iso_week IN (?,?) ORDER BY id DESC', (vid, week, 'C' + week), one=True)
        eligible = day.weekday() == 0 and bool(opened)
        out.append({'vessel_id': vid, 'name': contact['name'], 'vessel_type': contact.get('vessel_type') or '',
                    'enabled': int(row['enabled']) if row else 0,
                    'to_emails': contact['to_emails'], 'cc_emails': contact['cc_emails'], 'dear_name': contact['dear_name'],
                    'recipients_custom': custom, 'daily_recipients': daily,
                    'to': to, 'cc': cc, 'count': len(opened), 'survey_count': len(surveys),
                    'surveys': surveys, 'fingerprint': digest([contact['name'], surveys]),
                    'iso_week': week, 'tag': tag, 'subject': subject, 'body': body, 'signature': '자동화',
                    'blocker': blocker, 'cadence': 'weekly', 'eligible': eligible,
                    'reason': '매주 월요일 07:30' if opened else 'Open 지적 없음',
                    'next_send': next_send(day) if opened else None,
                    'run_this_week': dict(run) if run else None})
    return out


def set_enabled(vid, enabled, user):
    vessel = next((v for v in settings() if v['vessel_id'] == vid), None)
    if not vessel:
        raise CsMailError(403, '담당 CONTAINER 선박이 아닙니다.')
    if enabled and vessel['blocker']:
        raise CsMailError(400, vessel['blocker'])
    execute("INSERT INTO cs_mail_settings(vessel_id,enabled,updated_by) VALUES(?,?,?) "
            "ON CONFLICT(vessel_id) DO UPDATE SET enabled=excluded.enabled,updated_by=excluded.updated_by,updated_at=datetime('now','localtime')", (vid, int(bool(enabled)), user))
    return {'ok': True, 'vessel_id': vid, 'enabled': int(bool(enabled))}


def set_recipients(vid, data, user):
    """Per-vessel override. Missing key = keep; explicit null = follow Daily; reset=true = all follow Daily."""
    if not next((v for v in settings() if v['vessel_id'] == vid), None):
        raise CsMailError(403, '담당 CONTAINER 선박이 아닙니다.')
    cur = query('SELECT to_emails,cc_emails,dear_name FROM cs_mail_settings WHERE vessel_id=?', (vid,), one=True)
    values = {}
    for key in RECIPIENT_FIELDS:
        if data.get('reset'):
            raw = None
        elif key in data:
            raw = data[key]
        else:
            raw = cur[key] if cur else None
        if raw is not None and not isinstance(raw, str):
            raise CsMailError(400, f'{key} 형식 오류')
        values[key] = raw
    for key in ('to_emails', 'cc_emails'):
        if values[key] is not None:
            emails, bad = dm.parse_emails(values[key])
            if bad:
                raise CsMailError(400, '메일 주소 형식 오류: ' + ', '.join(bad))
            if len(emails) > dm.MAX_EMAILS:
                raise CsMailError(400, f'수신자는 To/CC 각각 {dm.MAX_EMAILS}명까지입니다.')
            values[key] = '; '.join(emails)
    if values['dear_name'] is not None:
        dear = re.sub(r'\s+', ' ', values['dear_name']).strip()
        if len(dear) > 120 or re.search(r'[{}<>]', dear) or KOREAN.search(dear):
            raise CsMailError(400, 'Dear 이름은 영문 120자 이내, 중괄호·<> 불가입니다.')
        values['dear_name'] = dear
    execute("INSERT INTO cs_mail_settings(vessel_id,to_emails,cc_emails,dear_name,updated_by) VALUES(?,?,?,?,?) "
            "ON CONFLICT(vessel_id) DO UPDATE SET to_emails=excluded.to_emails,cc_emails=excluded.cc_emails,dear_name=excluded.dear_name,"
            "updated_by=excluded.updated_by,updated_at=datetime('now','localtime')",
            (vid, values['to_emails'], values['cc_emails'], values['dear_name'], user))
    vessel = next(v for v in settings() if v['vessel_id'] == vid)
    if vessel['blocker'] and vessel['enabled']:
        # 잘못된 수신처로 ON 상태가 남지 않게 OFF 로 내림
        execute('UPDATE cs_mail_settings SET enabled=0 WHERE vessel_id=?', (vid,))
        vessel['enabled'] = 0
    return {k: vessel[k] for k in ('vessel_id', 'to_emails', 'cc_emails', 'dear_name', 'recipients_custom', 'enabled', 'blocker')}


def config(day=None):
    day = day or today_kst()
    vessels = [v for v in settings(day) if v['enabled'] and v['count']]
    for v in vessels:
        v.pop('surveys')
    return {'today': day.isoformat(), 'iso_week': dm.iso_week_of(day), 'vessels': vessels}


def _expected_rows(vessel):
    return [f['id'] for s in vessel['surveys'] for f in s['findings']]


def claim(data, canary=False):
    # Server time/cadence and shared recipients are authoritative; clients cannot override.
    vid = data.get('vessel_id')
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        vessel = next((v for v in settings() if v['vessel_id'] == vid), None)
        if not vessel or not vessel['enabled'] or vessel['blocker'] or not vessel['count'] or (not canary and not vessel['eligible']):
            raise CsMailError(409, '발송 대상·주기·ON/OFF·수신처 변경')
        source_week = vessel['iso_week']
        if vessel['run_this_week']:
            raise CsMailError(409, '이번 주 발송 시도 이미 있음')
        if canary:
            scheduled = next_send()
            if not scheduled:
                raise CsMailError(409, '유효한 다음 예약일 없음')
            vessel = next(v for v in settings(date.fromisoformat(scheduled[:10])) if v['vessel_id'] == vid)
            if vessel['run_this_week']:
                raise CsMailError(409, '이번 주 발송 시도 이미 있음')
        if data.get('fingerprint') != vessel['fingerprint'] or data.get('iso_week') != source_week:
            raise CsMailError(409, '엑셀 생성 후 Condition Survey 변경')
        attachments, reply_rows = data.get('attachments'), data.get('reply_rows')
        if (not isinstance(attachments, list) or not isinstance(reply_rows, list)
                or [a.get('survey_id') if isinstance(a, dict) else None for a in attachments] != [s['survey_id'] for s in vessel['surveys']]
                or any(not re.fullmatch(r'[a-f0-9]{64}', str(a.get('sha256') or '')) or not str(a.get('filename') or '').endswith('.xlsx') for a in attachments)):
            raise CsMailError(400, '첨부 스냅샷 오류')
        sha = str(data.get('excel_sha256') or '')
        if sha != manifest_sha([a['sha256'] for a in attachments]):
            raise CsMailError(400, '첨부 묶음 해시 오류')
        if [r.get('finding_id') if isinstance(r, dict) else None for r in reply_rows] != _expected_rows(vessel):
            raise CsMailError(400, '첨부 항목 불일치')
        files = [{'survey_id': a['survey_id'], 'filename': str(a['filename'])[:200], 'sha256': a['sha256']} for a in attachments]
        # Server pins identity/status/remark baseline itself; the client only proves the row order.
        live = {f['id']: f for s in vessel['surveys'] for f in s['findings']}
        remarks = {r['id']: r['remark'] or '' for r in conn.execute(
            'SELECT id,remark FROM cs_findings WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(list(live)),))}
        reply_rows = [{'finding_id': fid, 'survey_id': live[fid]['survey_id'], 'category': live[fid]['category'],
                       'no': str(live[fid]['no']), 'item': live[fid]['item'] or '', 'description': live[fid]['description'] or '',
                       'status': live[fid]['status'], 'remark': remarks.get(fid, '')} for fid in _expected_rows(vessel)]
        cur = conn.execute('INSERT INTO cs_mail_runs(vessel_id,iso_week,state,tag,subject,body,to_emails,cc_emails,snapshot,reply_rows,attachments,excel_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (vid, ('C' if canary else '') + vessel['iso_week'], 'sending', vessel['tag'], vessel['subject'], vessel['body'],
                            vessel['to_emails'], vessel['cc_emails'], dumps([vessel['name'], vessel['surveys']]), dumps(reply_rows), dumps(files), sha))
        # Baseline per finding: remark + status at send (manual status change detection).
        for row in reply_rows:
            conn.execute('INSERT INTO cs_mail_item_state(run_id,finding_id,last_remark,last_status) VALUES(?,?,?,?)',
                         (cur.lastrowid, row['finding_id'], row['remark'], row['status']))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise CsMailError(409, '동시 발송 시도 차단')
    except Exception:
        conn.rollback()
        raise
    return _public_run(query('SELECT * FROM cs_mail_runs WHERE id=?', (cur.lastrowid,), one=True))


def _public_run(row):
    run = dict(row)
    run['attachments'] = json.loads(run['attachments'])
    return run


def state(rid, data):
    wanted = data.get('state')
    if wanted not in ('sent', 'failed'):
        raise CsMailError(400, 'invalid state')
    row = query('SELECT * FROM cs_mail_runs WHERE id=?', (rid,), one=True)
    if not row:
        raise CsMailError(404, 'run not found')
    if row['state'] == wanted:
        return {'ok': True}
    if row['state'] != 'sending':
        raise CsMailError(409, '발송 상태 변경 충돌')
    changed = execute_rc("UPDATE cs_mail_runs SET state=?,sent_at=CASE WHEN ?='sent' THEN datetime('now','localtime') ELSE sent_at END,error=? WHERE id=? AND state='sending'", (wanted, wanted, str(data.get('error') or '')[:1000], rid))
    if changed != 1:
        raise CsMailError(409, '발송 상태 CAS 충돌')
    return {'ok': True}


def recover_sent(rid, data, actor):
    """Admin verifies Outlook Sent Items; never resends or releases weekly lock."""
    if data.get('confirmed_sent') is not True:
        raise CsMailError(400, 'Outlook 보낸 편지함 확인 필요')
    stamp = str(data.get('sent_at') or '')
    try:
        parsed = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Seoul'))
    except ValueError:
        raise CsMailError(400, '실제 발송시각은 한국시간 YYYY-MM-DD HH:MM:SS')
    if parsed > datetime.now(ZoneInfo('Asia/Seoul')):
        raise CsMailError(400, '미래 발송시각 불가')
    changed = execute_rc("UPDATE cs_mail_runs SET state='sent',sent_at=?,error=substr(COALESCE(error,'') || ?,1,1000) WHERE id=? AND state IN ('sending','failed')", (stamp, '\n관리자 발송확인: ' + actor, rid))
    if changed != 1:
        raise CsMailError(409, '복구 대상은 발송 확인 대기 또는 실패 기록')
    return {'ok': True}


def check_send(rid):
    run = query('SELECT * FROM cs_mail_runs WHERE id=?', (rid,), one=True)
    if not run or run['state'] != 'sending':
        raise CsMailError(409, '발송 run 상태 변경')
    day = None
    if run['iso_week'].startswith('C'):
        week = run['iso_week'][1:]
        day = date.fromisocalendar(int(week[:4]), int(week[5:]), 1)
    vessel = next((v for v in settings(day) if v['vessel_id'] == run['vessel_id']), None)
    if (not vessel or not vessel['enabled'] or not vessel['eligible'] or vessel['blocker']
            or digest(json.loads(run['snapshot'])) != vessel['fingerprint']
            or any(run[k] != vessel[k] for k in ('subject', 'body', 'to_emails', 'cc_emails'))):
        raise CsMailError(409, '발송 직전 항목·설정 변경')
    return {'ok': True}


def pending():
    rows = query("SELECT r.*,v.name vessel_name FROM cs_mail_runs r JOIN vessels v ON v.id=r.vessel_id WHERE r.state='sent' AND r.sent_at>=datetime('now','localtime','-21 days') ORDER BY r.id")
    out = []
    for row in rows:
        run = _public_run(row)
        run.pop('snapshot')
        run['reply_rows'] = json.loads(run['reply_rows'])
        run['processed_message_ids'] = [r['message_id'] for r in query('SELECT message_id FROM cs_mail_replies WHERE run_id=?', (run['id'],))]
        out.append(run)
    return out


def reply_date_prefix(received_at, remark):
    """Class 규칙과 동일: 회신일 [M/D] 접두. 이미 [M/D]로 시작하면 그대로."""
    if re.match(r'\[\d{1,2}/\d{1,2}\]', remark):
        return remark
    raw = str(received_at or '')[:10]
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        day = date.today()
    return f'[{day.month}/{day.day}]{remark}'


def source_fingerprint(original):
    return hashlib.sha256(re.sub(r'\s+', ' ', original or '').strip().casefold().encode()).hexdigest()


def _same_as_last_reply(conn, rid, key, fp):
    """Same raw already recorded for this finding in this run, or equals the latest raw of an
    earlier run for the same vessel (vessel re-sends last week's text = no new action)."""
    if conn.execute('SELECT 1 FROM cs_mail_reply_sources WHERE run_id=? AND item_key=? AND fp=?', (rid, key, fp)).fetchone():
        return True
    prev = conn.execute('SELECT s.fp FROM cs_mail_reply_sources s JOIN cs_mail_runs r ON r.id=s.run_id '
                        'WHERE s.item_key=? AND s.run_id<? AND r.vessel_id=(SELECT vessel_id FROM cs_mail_runs WHERE id=?) '
                        'ORDER BY s.run_id DESC, s.id DESC LIMIT 1', (key, rid, rid)).fetchone()
    return bool(prev) and prev['fp'] == fp


def apply_reply(rid, data):
    """All-or-nothing remark append (+ optional Open→Closed), CAS against concurrent manual edits.

    The runner decides `close`; the server never closes without it and never reopens.
    """
    msg = str(data.get('message_id') or '')
    sender = str(data.get('sender') or '').strip()
    updates = data.get('updates')
    if not msg or len(msg) > 180 or not sender or not isinstance(updates, list) or len(updates) > 300:
        raise CsMailError(400, '회신 식별자·발신자·조치내용 오류')
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        run = conn.execute("SELECT * FROM cs_mail_runs WHERE id=? AND state='sent'", (rid,)).fetchone()
        if not run:
            raise CsMailError(409, '발송 확인된 run이 아님')
        roster = dm.roster_vessel_ids()
        if roster is None or run['vessel_id'] not in roster:
            raise CsMailError(409, '담당 로스터 변경: 수동 확인 필요')
        if conn.execute('SELECT id FROM cs_mail_replies WHERE run_id=? AND message_id=?', (rid, msg)).fetchone():
            conn.rollback()
            return {'ok': True, 'duplicate': True, 'updated': 0, 'closed': 0}
        if updates and not sender_trusted(run, sender):
            # Unregistered sender: record for manual review only, no remark/status change.
            conn.execute('INSERT INTO cs_mail_replies(run_id,message_id,sender,received_at,updated,closed,note) VALUES(?,?,?,?,0,0,?)',
                         (rid, msg, sender[:200], str(data.get('received_at') or '')[:30], UNTRUSTED_NOTE))
            conn.commit()
            return {'ok': True, 'updated': 0, 'unchanged': 0, 'closed': 0, 'duplicate': False, 'held': UNTRUSTED_NOTE}
        _, surveys = json.loads(run['snapshot'])
        snapshot = {f['id']: dict(f, remark=None) for s in surveys for f in s['findings'] if f['status'] == 'Open'}
        sent_remarks = {r['finding_id']: r.get('remark', '') for r in json.loads(run['reply_rows'])}
        changes, seen, unchanged, sources, notes = [], set(), 0, [], []
        for update in updates:
            if not isinstance(update, dict):
                raise CsMailError(400, '회신 항목 형식 오류')
            origin = snapshot.get(update.get('finding_id'))
            remark = str(update.get('remark') or '').strip()
            close = update.get('close', False)
            if not origin or origin['id'] in seen or not remark or len(remark) > 12000 or not isinstance(close, bool):
                raise CsMailError(400, '회신 항목·조치내용 오류')
            seen.add(origin['id'])
            live = conn.execute('SELECT * FROM cs_findings WHERE id=?', (origin['id'],)).fetchone()
            if not live:
                continue        # finding deleted on the CS page after sending: nothing to update
            if live['survey_id'] != origin['survey_id'] or item_key(dict(live)) != item_key(origin):
                raise CsMailError(409, 'CS 항목 변경: 수동 확인 필요')
            key = item_key(origin)
            fp = source_fingerprint(remark)
            if _same_as_last_reply(conn, rid, key, fp):
                unchanged += 1
                sources.append((key, fp))
                continue
            sources.append((key, fp))
            tracked = conn.execute('SELECT last_remark,last_status FROM cs_mail_item_state WHERE run_id=? AND finding_id=?', (rid, origin['id'])).fetchone()
            base = tracked['last_remark'] if tracked else (sent_remarks.get(origin['id']) or '')
            if tracked and live['status'] != tracked['last_status']:
                # Status changed by hand after sending: never touch status; remark append only.
                close = False
                manual = True
                notes.append(f"{MANUAL_STATUS_NOTE} (finding {origin['id']})")
            else:
                manual = False
            previous = live['remark'] or ''
            if previous != base:
                raise CsMailError(409, '비고 동시 변경: 수동 확인 필요')
            updated = (previous.rstrip() + '\n\n' if previous.strip() else '') + reply_date_prefix(data.get('received_at'), remark)
            status = 'Closed' if close and live['status'] == 'Open' else live['status']
            changes.append((updated, status, origin['id'], previous, live['status'], live['survey_id'], manual))
        closed = 0
        for updated, status, fid, previous, old_status, sid, manual in changes:
            cur = conn.execute("UPDATE cs_findings SET remark=?,status=?,updated_at=datetime('now','localtime') WHERE id=? AND COALESCE(remark,'')=? AND status=?",
                               (updated, status, fid, previous, old_status))
            if cur.rowcount != 1:
                raise CsMailError(409, '비고 CAS 충돌')
            if status != old_status:
                closed += 1
                conn.execute("UPDATE cs_surveys SET updated_at=datetime('now','localtime') WHERE id=?", (sid,))
            conn.execute('INSERT INTO cs_mail_item_state(run_id,finding_id,last_remark,last_status) VALUES(?,?,?,?) ON CONFLICT(run_id,finding_id) DO UPDATE SET last_remark=excluded.last_remark,last_status=excluded.last_status', (rid, fid, updated, 'MANUAL' if manual else status))   # MANUAL: status locked for this run
        for key, fp in sources:
            conn.execute('DELETE FROM cs_mail_reply_sources WHERE run_id=? AND item_key=? AND fp=?', (rid, key, fp))
            conn.execute('INSERT INTO cs_mail_reply_sources(run_id,item_key,fp) VALUES(?,?,?)', (rid, key, fp))
        note = '; '.join([n for n in [str(data.get('note') or '')] + notes if n])[:500]
        conn.execute('INSERT INTO cs_mail_replies(run_id,message_id,sender,received_at,updated,closed,note) VALUES(?,?,?,?,?,?,?)',
                     (rid, msg, sender[:200], str(data.get('received_at') or '')[:30], len(changes) + unchanged, closed, note))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'ok': True, 'updated': len(changes), 'unchanged': unchanged, 'closed': closed, 'duplicate': False}


UNTRUSTED_NOTE = '회신 확인 필요(발신자 미등록)'
MANUAL_STATUS_NOTE = '수동 상태 변경 감지 — 상태 변경 안 함, 확인 필요'


def sender_trusted(run, sender):
    """Sender address must be in the run's recorded To/CC or the vessel's current To/CC settings."""
    found = re.findall(r'[A-Za-z0-9._%+\-\']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', sender or '')
    if len(found) != 1:
        return False
    allowed = set()
    for text in (run['to_emails'], run['cc_emails']):
        allowed.update(a.lower() for a in dm.parse_emails(text)[0])
    contacts = (query('SELECT to_emails,cc_emails FROM daily_mail_settings WHERE vessel_id=?', (run['vessel_id'],), one=True),
                query('SELECT to_emails,cc_emails FROM cs_mail_settings WHERE vessel_id=?', (run['vessel_id'],), one=True))
    for contact in contacts:
        if contact:
            for text in (contact['to_emails'], contact['cc_emails']):
                allowed.update(a.lower() for a in dm.parse_emails(text or '')[0])
    return found[0].lower() in allowed


def still_open(run):
    """Reminder guard: at least one finding that was Open at send is still Open."""
    ids = [r['finding_id'] for r in json.loads(run['reply_rows']) if r.get('status') == 'Open']
    if not ids:
        return False
    return bool(query("SELECT 1 FROM cs_findings WHERE status='Open' AND id IN (SELECT value FROM json_each(?)) LIMIT 1", (json.dumps(ids),), one=True))


def followup_rows():
    runs = [dict(r) for r in query('SELECT r.id,r.vessel_id,v.name vessel_name,r.iso_week,r.state,r.sent_at,r.error,r.reply_rows FROM cs_mail_runs r JOIN vessels v ON v.id=r.vessel_id WHERE r.id=(SELECT MAX(x.id) FROM cs_mail_runs x WHERE x.vessel_id=r.vessel_id) ORDER BY v.name')]
    out = []
    for run in runs:
        replies = [dict(r) for r in query('SELECT * FROM cs_mail_replies WHERE run_id=? ORDER BY id', (run['id'],))]
        substantive = [r for r in replies if r['updated'] > 0]
        last = substantive[-1] if substantive else None
        try:
            sent_count = sum(1 for r in json.loads(run.pop('reply_rows') or '[]') if r.get('status') == 'Open')
        except (ValueError, TypeError):
            sent_count = 0
        out.append(dict(run, reply_state=('replied' if substantive else 'pending') if run['state'] == 'sent' else 'not_sent',
                        last_reply_at=(last['received_at'] or last['created_at']) if last else None,
                        sender=last['sender'] if last else '', update_count=sum(r['updated'] for r in replies),
                        closed_count=sum(r['closed'] for r in replies),
                        review_count=sum(bool(r['note']) for r in replies), sent_count=sent_count))
    return out


def status():
    vessels = settings()
    for v in vessels:
        v.pop('surveys')
    runs = [dict(r) for r in query('SELECT r.id,r.vessel_id,v.name vessel_name,r.iso_week,r.state,r.sent_at,r.error FROM cs_mail_runs r JOIN vessels v ON v.id=r.vessel_id ORDER BY r.id DESC LIMIT 100')]
    replies = [dict(r) for r in query('SELECT p.*,v.name vessel_name FROM cs_mail_replies p JOIN cs_mail_runs r ON r.id=p.run_id JOIN vessels v ON v.id=r.vessel_id ORDER BY p.id DESC LIMIT 100')]
    return {'vessels': vessels, 'runs': runs, 'replies': replies, 'followup': followup_rows(),
            'template': template(), 'timezone': 'Asia/Seoul'}
