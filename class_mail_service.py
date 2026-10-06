"""Class Status mail: separate switches, shared Daily recipients, pinned snapshots.

Only reply remarks update action_taken. Never creates, closes or deletes findings.
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

SUBJECT = '{vessel} - Class Status: Action Plan and Progress Required'
BODY = '''Dear {dear},

Please find attached the current Condition of Class (COC) and Statutory items for {vessel}.

1) Please enter the action plan or progress for each item in the "Action Taken" column of the attached Excel file and return the updated file by replying to this email.
2) For planned or ongoing actions, please include the proposed completion date and schedule, including any arrangements for repairs, attendance or approval.
3) For completed actions, please describe the work completed and the completion date, and attach supporting evidence where available.

Please keep the other columns unchanged so that your remarks can be matched to the relevant items.

Thank you for your cooperation.'''
VARS = ('vessel', 'dear')
TAG_RE = re.compile(r'\[TRMT-CS \d{4}W\d{2} [A-Z0-9]{1,8}\]')
CATEGORIES = {'COC': 'Condition of Class (COC)', 'STATUTORY': 'Statutory (Flag)'}


class ClassMailError(MailServiceError):
    pass


def today_kst():
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def item_key(item):
    return digest([item['category'], str(item['no'] or ''), item['description'] or ''])


def items_for(vid):
    snap = query('SELECT * FROM class_status WHERE vessel_id=? ORDER BY updated_at DESC,id DESC LIMIT 1', (vid,), one=True)
    if not snap:
        return []
    rows = query("SELECT id,category,no,issued_date,description,due_date,action_taken,importance "
                 "FROM class_status_items WHERE cs_id=? AND category IN ('COC','STATUTORY') ORDER BY category,no,id", (snap['id'],))
    return [dict(r) for r in rows]


def cadence(items, day=None):
    day = day or today_kst()
    if not items:
        return {'cadence': 'none', 'eligible': False, 'reason': '지적 없음', 'nearest_due': None}
    dates = []
    for row in items:
        try:
            dates.append(date.fromisoformat(str(row['due_date'] or '').strip()))
        except ValueError:
            return {'cadence': 'blocked', 'eligible': False, 'reason': 'Due date 누락·오류 확인 필요', 'nearest_due': None}
    nearest = min(dates)
    weekly = (nearest - day).days <= 30
    eligible = day.weekday() == 0 and (weekly or (day.day - 1) // 7 + 1 in (1, 3))
    return {'cadence': 'weekly' if weekly else 'first_third', 'eligible': eligible,
            'reason': '매주 월요일 07:30' if weekly else '매월 첫째·셋째 월요일 07:30', 'nearest_due': nearest.isoformat()}


def next_send(items, day=None):
    day = day or today_kst()
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    start = 1 if day == now.date() and (now.hour, now.minute) >= (7, 30) else 0
    for offset in range(start, 36):
        candidate = day + timedelta(days=offset)
        if cadence(items, candidate)['eligible']:
            return candidate.isoformat() + ' 07:30'
    return None


def template():
    row = query('SELECT subject_tpl,body_tpl,updated_at FROM class_mail_template WHERE id=1', one=True)
    return dict(row) if row else {'subject_tpl': SUBJECT, 'body_tpl': BODY, 'updated_at': None}


def save_template(data, user):
    subject, body = str(data.get('subject_tpl') or '').strip(), str(data.get('body_tpl') or '').strip()
    unknown = set(re.findall(r'\{([^{}]*)\}', subject + body)) - set(VARS)
    if not subject or not body or len(subject) > 300 or len(body) > 8000 or unknown:
        raise ClassMailError(400, '템플릿 제목·본문·변수를 확인하세요.')
    if re.search(r'[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af\r\n]', subject) or re.search(r'[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]', body):
        raise ClassMailError(400, '메일 템플릿은 영문으로 작성하세요.')
    execute("INSERT INTO class_mail_template(id,subject_tpl,body_tpl,updated_by) VALUES(1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET subject_tpl=excluded.subject_tpl,body_tpl=excluded.body_tpl,updated_by=excluded.updated_by,updated_at=datetime('now','localtime')", (subject, body, user))
    return template()


def render(tpl, name, dear):
    values = {'vessel': name, 'dear': dear.strip() or dm.DEFAULT_DEAR}
    return re.sub(r'\{(vessel|dear)\}', lambda m: values[m.group(1)], tpl)


def settings(day=None):
    day = day or today_kst()
    # Unlike the legacy fallback, missing roster is fail-closed for new sending.
    roster = dm.roster_vessel_ids()
    if roster is None:
        return []
    tpl = template()
    out = []
    for contact in dm.list_settings():
        vid = contact['vessel_id']
        if vid not in roster:
            continue
        row = query('SELECT enabled FROM class_mail_settings WHERE vessel_id=?', (vid,), one=True)
        items = items_for(vid)
        to, bad_to = dm.parse_emails(contact['to_emails'])
        cc, bad_cc = dm.parse_emails(contact['cc_emails'])
        rule = cadence(items, day)
        tag = f"[TRMT-CS {dm.iso_week_of(day)} V{vid}]"
        subject = tag + ' ' + render(tpl['subject_tpl'], contact['name'], contact['dear_name'])
        body = render(tpl['body_tpl'], contact['name'], contact['dear_name'])
        blocker = ('수신처 오류·To 누락' if not to or bad_to or bad_cc else '')
        if re.search(r'[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]', subject + body):
            blocker = '선명·Dear 영문 확인 필요'
        run = query('SELECT id,state FROM class_mail_runs WHERE vessel_id=? AND iso_week IN (?,?) ORDER BY id DESC', (vid, dm.iso_week_of(day), 'C' + dm.iso_week_of(day)), one=True)
        out.append({'vessel_id': vid, 'name': contact['name'], 'enabled': int(row['enabled']) if row else 0,
                    'to_emails': contact['to_emails'], 'cc_emails': contact['cc_emails'], 'dear_name': contact['dear_name'],
                    'to': to, 'cc': cc, 'count': len(items), 'coc': sum(i['category'] == 'COC' for i in items),
                    'statutory': sum(i['category'] == 'STATUTORY' for i in items), 'items': items,
                    'fingerprint': digest(items), 'iso_week': dm.iso_week_of(day), 'tag': tag,
                    'subject': subject, 'body': body, 'signature': '자동화', 'blocker': blocker,
                    'next_send': next_send(items, day), 'run_this_week': dict(run) if run else None, **rule})
    return out


def set_enabled(vid, enabled, user):
    vessel = next((v for v in settings() if v['vessel_id'] == vid), None)
    if not vessel:
        raise ClassMailError(403, '담당 선박이 아닙니다.')
    if enabled and vessel['blocker']:
        raise ClassMailError(400, vessel['blocker'])
    execute("INSERT INTO class_mail_settings(vessel_id,enabled,updated_by) VALUES(?,?,?) "
            "ON CONFLICT(vessel_id) DO UPDATE SET enabled=excluded.enabled,updated_by=excluded.updated_by,updated_at=datetime('now','localtime')", (vid, int(bool(enabled)), user))
    return {'ok': True, 'vessel_id': vid, 'enabled': int(bool(enabled))}


def config(day=None):
    day = day or today_kst()
    return {'today': day.isoformat(), 'iso_week': dm.iso_week_of(day),
            'vessels': [v for v in settings(day) if v['enabled'] and v['count']]}


def claim(data, canary=False):
    # Server time/cadence and shared recipients are authoritative; clients cannot override.
    vid = data.get('vessel_id')
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        vessel = next((v for v in settings() if v['vessel_id'] == vid), None)
        if not vessel or not vessel['enabled'] or vessel['blocker'] or not vessel['count'] or vessel['cadence'] == 'blocked' or (not canary and not vessel['eligible']):
            raise ClassMailError(409, '발송 대상·주기·ON/OFF·수신처 변경')
        source_week = vessel['iso_week']
        if vessel['run_this_week']:
            raise ClassMailError(409, '이번 주 발송 시도 이미 있음')
        if canary:
            scheduled = next_send(vessel['items'])
            if not scheduled:
                raise ClassMailError(409, '유효한 다음 예약일 없음')
            scheduled_day = date.fromisoformat(scheduled[:10])
            if scheduled_day.weekday() != 0:
                raise ClassMailError(409, '카나리 예약 기준일은 월요일이어야 함')
            vessel = next(v for v in settings(scheduled_day) if v['vessel_id'] == vid)
        if vessel['run_this_week']:
            raise ClassMailError(409, '이번 주 발송 시도 이미 있음')
        if data.get('fingerprint') != vessel['fingerprint'] or data.get('iso_week') != source_week:
            raise ClassMailError(409, '엑셀 생성 후 Class Status 변경')
        sha = str(data.get('excel_sha256') or '')
        reply_rows = data.get('reply_rows')
        if not re.fullmatch(r'[a-f0-9]{64}', sha) or not isinstance(reply_rows, list) or len(reply_rows) != len(vessel['items']):
            raise ClassMailError(400, '첨부 스냅샷 오류')
        if [r.get('item_id') for r in reply_rows] != [i['id'] for i in vessel['items']]:
            raise ClassMailError(400, '첨부 항목 불일치')
        cur = conn.execute('INSERT INTO class_mail_runs(vessel_id,iso_week,state,tag,subject,body,to_emails,cc_emails,snapshot,reply_rows,excel_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (vid, ('C' if canary else '') + vessel['iso_week'], 'sending', vessel['tag'], vessel['subject'], vessel['body'],
                            vessel['to_emails'], vessel['cc_emails'], dumps(vessel['items']), dumps(reply_rows), sha))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ClassMailError(409, '동시 발송 시도 차단')
    except Exception:
        conn.rollback()
        raise
    return dict(query('SELECT * FROM class_mail_runs WHERE id=?', (cur.lastrowid,), one=True))


def state(rid, data):
    wanted = data.get('state')
    if wanted not in ('sent', 'failed'):
        raise ClassMailError(400, 'invalid state')
    row = query('SELECT * FROM class_mail_runs WHERE id=?', (rid,), one=True)
    if not row:
        raise ClassMailError(404, 'run not found')
    if row['state'] == wanted:
        return {'ok': True}
    if row['state'] != 'sending':
        raise ClassMailError(409, '발송 상태 변경 충돌')
    changed = execute_rc("UPDATE class_mail_runs SET state=?,sent_at=CASE WHEN ?='sent' THEN datetime('now','localtime') ELSE sent_at END,error=? WHERE id=? AND state='sending'", (wanted, wanted, str(data.get('error') or '')[:1000], rid))
    if changed != 1:
        raise ClassMailError(409, '발송 상태 CAS 충돌')
    return {'ok': True}


def recover_sent(rid, data, actor):
    """Admin verifies Outlook Sent Items; never resends or releases weekly lock."""
    if data.get('confirmed_sent') is not True:
        raise ClassMailError(400, 'Outlook 보낸 편지함 확인 필요')
    stamp = str(data.get('sent_at') or '')
    try:
        parsed = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Seoul'))
    except ValueError:
        raise ClassMailError(400, '실제 발송시각은 한국시간 YYYY-MM-DD HH:MM:SS')
    if parsed > datetime.now(ZoneInfo('Asia/Seoul')):
        raise ClassMailError(400, '미래 발송시각 불가')
    changed = execute_rc("UPDATE class_mail_runs SET state='sent',sent_at=?,error=substr(COALESCE(error,'') || ?,1,1000) WHERE id=? AND state IN ('sending','failed')", (stamp, '\n관리자 발송확인: ' + actor, rid))
    if changed != 1:
        raise ClassMailError(409, '복구 대상은 발송 확인 대기 또는 실패 기록')
    return {'ok': True}


def pending():
    rows = query("SELECT r.*,v.name vessel_name FROM class_mail_runs r JOIN vessels v ON v.id=r.vessel_id WHERE r.state='sent' AND r.sent_at>=datetime('now','localtime','-21 days') ORDER BY r.id")
    out = []
    for row in rows:
        run = dict(row)
        run['snapshot'] = json.loads(run['snapshot'])
        run['reply_rows'] = json.loads(run['reply_rows'])
        for exported, origin in zip(run['reply_rows'], run['snapshot']):
            tracked = query('SELECT reply_baseline FROM class_mail_item_state WHERE run_id=? AND item_key=?', (run['id'], item_key(origin)), one=True)
            exported['initial_action'] = exported['action']
            if tracked:
                exported['action'] = tracked['reply_baseline']
        run['processed_message_ids'] = [r['message_id'] for r in query('SELECT message_id FROM class_mail_replies WHERE run_id=?', (run['id'],))]
        out.append(run)
    return out


def reply_date_prefix(received_at, remark):
    """자동 조치사항 맨 앞에 회신일 [M/D] (형 지시 2026-10-06). 이미 [M/D]로 시작하면 그대로."""
    if re.match(r'\[\d{1,2}/\d{1,2}\]', remark):
        return remark
    raw = str(received_at or '')[:10]
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        day = date.today()
    return f'[{day.month}/{day.day}]{remark}'


def apply_reply(rid, data):
    """All-or-nothing remark append, identity rebind after daily snapshot refresh, CAS.

    A snapshot disappearance/ambiguity or concurrent manual edit fails without overwriting.
    """
    msg = str(data.get('message_id') or '')
    sender = str(data.get('sender') or '').strip()
    updates = data.get('updates')
    if not msg or len(msg) > 180 or not sender or not isinstance(updates, list) or len(updates) > 300:
        raise ClassMailError(400, '회신 식별자·발신자·조치내용 오류')
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        run = conn.execute("SELECT * FROM class_mail_runs WHERE id=? AND state='sent'", (rid,)).fetchone()
        if not run:
            raise ClassMailError(409, '발송 확인된 run이 아님')
        roster = dm.roster_vessel_ids()
        if roster is None or run['vessel_id'] not in roster:
            raise ClassMailError(409, '담당 로스터 변경: 수동 확인 필요')
        if conn.execute('SELECT id FROM class_mail_replies WHERE run_id=? AND message_id=?', (rid, msg)).fetchone():
            conn.rollback()
            return {'ok': True, 'duplicate': True, 'updated': 0}
        snapshot = {i['id']: i for i in json.loads(run['snapshot'])}
        current = items_for(run['vessel_id'])
        changes, seen = [], set()
        for update in updates:
            origin = snapshot.get(update.get('item_id'))
            remark = str(update.get('remark') or '').strip()
            if not origin or origin['id'] in seen or not remark or len(remark) > 12000:
                raise ClassMailError(400, '회신 항목·조치내용 오류')
            original_remark = update.get('remark_original', remark)
            if (not isinstance(original_remark, str) or not original_remark.strip() or len(original_remark) > 12000
                    or ('remark_original' in update and not re.search(r'[가-힣]', remark)
                        and not (remark == original_remark.strip() and re.fullmatch(r'(BWTS|EGCS|COC|ME|AE|S/W|F/W|T/C|UTM)', original_remark.strip())))):
                raise ClassMailError(400, '회신 원문·한국어 번역 오류')
            original_remark = original_remark.strip()
            seen.add(origin['id'])
            matches = [i for i in current if item_key(i) == item_key(origin)]
            if len(matches) != 1:
                raise ClassMailError(409, 'Class 항목 변경·삭제·중복: 수동 확인 필요')
            live = matches[0]
            tracked = conn.execute('SELECT last_action FROM class_mail_item_state WHERE run_id=? AND item_key=?', (rid, item_key(origin))).fetchone()
            base = tracked['last_action'] if tracked else (origin['action_taken'] or '')
            previous = live['action_taken'] or ''
            if previous != base:
                raise ClassMailError(409, '조치사항 동시 변경: 수동 확인 필요')
            # Existing manual text remains intact; only actual reply remarks are appended.
            updated = (previous.rstrip() + '\n\n' if previous.strip() else '') + reply_date_prefix(data.get('received_at'), remark)
            exported = next(r for r in json.loads(run['reply_rows']) if r['item_id'] == origin['id'])
            baseline_row = conn.execute('SELECT reply_baseline FROM class_mail_item_state WHERE run_id=? AND item_key=?', (rid, item_key(origin))).fetchone()
            reply_base = baseline_row['reply_baseline'] if baseline_row else exported['action']
            reply_baseline = (reply_base.rstrip() + '\n\n' if reply_base.strip() else '') + original_remark
            changes.append((updated, live['id'], previous, item_key(origin), reply_baseline))
        for updated, iid, previous, identity, reply_baseline in changes:
            cur = conn.execute("UPDATE class_status_items SET action_taken=?,updated_at=datetime('now','localtime') WHERE id=? AND COALESCE(action_taken,'')=?", (updated, iid, previous))
            if cur.rowcount != 1:
                raise ClassMailError(409, '조치사항 CAS 충돌')
            conn.execute('INSERT INTO class_mail_item_state(run_id,item_key,last_action,reply_baseline) VALUES(?,?,?,?) ON CONFLICT(run_id,item_key) DO UPDATE SET last_action=excluded.last_action,reply_baseline=excluded.reply_baseline', (rid, identity, updated, reply_baseline))
        note = str(data.get('note') or '')[:1000]
        conn.execute('INSERT INTO class_mail_replies(run_id,message_id,sender,received_at,updated,note) VALUES(?,?,?,?,?,?)', (rid, msg, sender[:200], str(data.get('received_at') or '')[:30], len(changes), note))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'ok': True, 'updated': len(changes), 'duplicate': False}


def status():
    vessels = settings()
    for v in vessels:
        v.pop('items')
    runs = [dict(r) for r in query('SELECT r.id,r.vessel_id,v.name vessel_name,r.iso_week,r.state,r.sent_at,r.error FROM class_mail_runs r JOIN vessels v ON v.id=r.vessel_id ORDER BY r.id DESC LIMIT 100')]
    replies = [dict(r) for r in query('SELECT p.*,v.name vessel_name FROM class_mail_replies p JOIN class_mail_runs r ON r.id=p.run_id JOIN vessels v ON v.id=r.vessel_id ORDER BY p.id DESC LIMIT 100')]
    return {'vessels': vessels, 'runs': runs, 'replies': replies, 'template': template(), 'timezone': 'Asia/Seoul'}


def check_send(rid):
    run = query('SELECT * FROM class_mail_runs WHERE id=?', (rid,), one=True)
    if not run or run['state'] != 'sending':
        raise ClassMailError(409, '발송 run 상태 변경')
    day = None
    if run['iso_week'].startswith('C'):
        week = run['iso_week'][1:]
        day = date.fromisocalendar(int(week[:4]), int(week[5:]), 1)
    vessel = next((v for v in settings(day) if v['vessel_id'] == run['vessel_id']), None)
    if (not vessel or not vessel['enabled'] or not vessel['eligible'] or vessel['blocker']
            or digest(json.loads(run['snapshot'])) != vessel['fingerprint']
            or any(run[k] != vessel[k] for k in ('subject','body','to_emails','cc_emails'))):
        raise ClassMailError(409, '발송 직전 항목·설정 변경')
    return {'ok': True}
