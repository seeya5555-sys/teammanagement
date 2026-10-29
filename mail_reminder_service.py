"""Shared bounded follow-up ledger. A claim is never evidence of a sent email."""
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app_core import get_db, query
import daily_mail_service as dm
from mail_common import MailServiceError


class ReminderError(MailServiceError):
    pass


def now_kst():
    return datetime.now(ZoneInfo('Asia/Seoul')).replace(tzinfo=None)


def _timestamp(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        raise ReminderError(409, 'invalid stored send timestamp')


VETTING_MAX_REMINDERS = 2            # 형 지시 2026-10-18: Vetting 주 2회
VETTING_SLOTS = (2, 4)               # 수(2)·금(4) 08:00 KST


CS_MAX_REMINDERS = 1                 # Condition Survey: 목요일 1회만
CS_REMIND_WEEKDAY = 3                # 목(3) 08:30 KST, 최초 발송 ISO 주 안에서만
RUN_KEYS = ('id','vessel_name','subject','to_emails','cc_emails','dear','sent_at','excel_sha256')


def max_reminders(mode):
    if mode == 'vetting':
        return VETTING_MAX_REMINDERS
    return CS_MAX_REMINDERS if mode == 'cs' else dm.MAX_REMINDERS


def ledger(mode):
    """(attempts, notices) tables. CS has its own ledger so the shared mode CHECK is never rebuilt."""
    return ('cs_mail_reminder_attempts', 'cs_mail_reminder_notices') if mode == 'cs' else ('mail_reminder_attempts', 'mail_reminder_notices')


def project(mode, run):
    out = {k: run[k] for k in RUN_KEYS}
    if mode == 'cs':
        out['attachments'] = run['attachments']   # per-survey originals (filename, sha256)
    return out


def cs_slot(sent):
    """Thursday 08:30 of the first-send ISO week, only if after the send; else None (no reminder)."""
    slot = (sent + timedelta(days=CS_REMIND_WEEKDAY - sent.weekday())).replace(hour=8, minute=30, second=0, microsecond=0)
    return slot if slot > sent else None


def in_cs_window(now):
    return now.weekday() == CS_REMIND_WEEKDAY


def next_vetting_slot(after):
    """after 이후(초과) 첫 수/금 08:00."""
    day = after.replace(hour=8, minute=0, second=0, microsecond=0)
    if day <= after:
        day += timedelta(days=1)
    while day.weekday() not in VETTING_SLOTS:
        day += timedelta(days=1)
    return day


def in_vetting_window(now):
    """외부 발송은 수·금 08:00~08:59 KST 안에서만. 놓친 슬롯은 늦게 보내지 않고 다음 슬롯."""
    return now.weekday() in VETTING_SLOTS and now.hour == 8


def same_iso_week(a, b):
    return a.isocalendar()[:2] == b.isocalendar()[:2]


def week_open(run, now):
    """형 지시 2026-10-18: 리마인드(Vetting·Class 공통)는 최초 발송한 그 주(ISO 주차) 안에서만. 주가 지나면 지난주 건은 안 나감."""
    return same_iso_week(_timestamp(run['sent_at']), now)


def reminder_due(mode, run, n):
    if mode == 'cs':
        return cs_slot(_timestamp(run['sent_at'])) or datetime.max
    if mode == 'vetting':
        return next_vetting_slot(_timestamp(run['sent_at'] if n == 1 else run['last_reminder_at']))
    base = _timestamp(run['sent_at'] if n == 1 else run['last_reminder_at'])
    return (base + timedelta(days=dm.REPLY_DUE_DAYS + 1 if n == 1 else 1)).replace(hour=8, minute=30, second=0, microsecond=0)


def _mode(mode):
    if mode not in ('class', 'vetting', 'cs'):
        raise ReminderError(400, 'invalid reminder mode')
    return mode


def _run(mode, rid):
    _mode(mode)
    row = query(f'SELECT r.*,v.name vessel_name,v.active FROM {mode}_mail_runs r JOIN vessels v ON v.id=r.vessel_id WHERE r.id=?', (rid,), one=True)
    if not row:
        raise ReminderError(404, 'run not found')
    run = dict(row)
    setting = query(f'SELECT enabled FROM {mode}_mail_settings WHERE vessel_id=?', (run['vessel_id'],), one=True)
    contacts = query('SELECT * FROM daily_mail_settings WHERE vessel_id=?', (run['vessel_id'],), one=True)
    roster = dm.roster_vessel_ids()
    if run['state'] != 'sent' or not run['active'] or not setting or not setting['enabled'] or roster is None or run['vessel_id'] not in roster or not contacts:
        raise ReminderError(409, 'run no longer eligible')
    key = 'vetting_id' if mode == 'vetting' else 'vessel_id'
    latest = query(f'SELECT id FROM {mode}_mail_runs WHERE {key}=? ORDER BY id DESC LIMIT 1', (run[key],), one=True)
    if latest['id'] != rid:
        raise ReminderError(409, 'superseded run')
    to, invalid = dm.parse_emails(contacts['to_emails'])
    cc, invalid_cc = dm.parse_emails(contacts['cc_emails'])
    if not to or invalid or invalid_cc:
        raise ReminderError(409, 'invalid current contacts')
    if mode == 'class':
        import class_mail_service as cs
        if not cs.items_for(run['vessel_id']):
            raise ReminderError(409, 'no current class items')
    elif mode == 'cs':
        import cs_mail_service as csm
        if not csm.still_open(run):
            raise ReminderError(409, 'no sent findings still open')
        run['attachments'] = json.loads(run['attachments'])
    elif not query("SELECT 1 FROM vt_findings WHERE vetting_id=? AND id IN (SELECT value FROM json_each(?)) AND COALESCE(status,'Open')='Open' LIMIT 1", (run['vetting_id'], run['finding_ids']), one=True):
        raise ReminderError(409, 'no sent findings still open')
    # Reply events are substantive in Vetting; receipt acknowledgements are not reply events.
    sql = (f'SELECT 1 FROM {mode}_mail_replies WHERE run_id=? AND updated>0 LIMIT 1' if mode in ('class', 'cs') else
           "SELECT 1 FROM vetting_mail_events WHERE run_id=? AND kind IN ('reply','update','close') LIMIT 1")
    if query(sql, (rid,), one=True):
        raise ReminderError(409, 'substantive reply received')
    if mode == 'class' and query("SELECT 1 FROM class_mail_replies WHERE run_id=? AND updated=0 AND COALESCE(note,'') NOT IN ('','수신 확인만 — 결과/조치 회신 아님') LIMIT 1", (rid,), one=True):
        raise ReminderError(409, 'reply interpretation pending review')
    # CS: a held/unreadable reply (note set, no update) is a reply awaiting review, not silence (AGENTS §H).
    if mode == 'cs' and query("SELECT 1 FROM cs_mail_replies WHERE run_id=? AND updated=0 AND COALESCE(note,'')<>'' LIMIT 1", (rid,), one=True):
        raise ReminderError(409, 'reply interpretation pending review')
    attempts_table, notices_table = ledger(mode)
    attempts = [dict(r) for r in query(f'SELECT * FROM {attempts_table} WHERE mode=? AND run_id=? ORDER BY n', (mode, rid))]
    sent = [r for r in attempts if r['state'] == 'sent']
    notified = query(f'SELECT notified_at FROM {notices_table} WHERE mode=? AND run_id=?', (mode, rid), one=True)
    if now_kst() - _timestamp(run['sent_at']) > timedelta(days=28):
        raise ReminderError(409, 'reply window expired')
    run.update(to_emails='; '.join(to), cc_emails='; '.join(cc), dear=contacts['dear_name'] or 'Captain',
               reminder_count=len(sent), last_reminder_at=sent[-1]['sent_at'] if sent else None,
               notified_at=notified['notified_at'] if notified else None,
               reminder_attempts=attempts)
    return run


def pending(mode):
    _mode(mode)
    out, blocked = [], []
    # 28일 창은 now_kst() 기준(146행 claim 판정과 동일 시계). SQL 'now' 를 쓰면 시계가 갈라져 테스트·판정 불일치.
    since = (now_kst() - timedelta(days=28)).strftime('%Y-%m-%d %H:%M:%S')
    for r in query(f"SELECT id FROM {mode}_mail_runs WHERE state='sent' AND sent_at>=? ORDER BY id", (since,)):
        try:
            run = _run(mode, r['id'])
            unresolved = [a for a in run['reminder_attempts'] if a['state'] != 'sent']
            if unresolved:
                # 지난 주차 건은 어차피 리마인드 대상이 아님(week_open) → 차단 경보 제외. 미정리 기록 자체는 보존.
                if not week_open(run, now_kst()):
                    continue
                blocked.append({'id':run['id'], 'vessel_name':run['vessel_name'], 'state':unresolved[-1]['state']})
                continue
            if run['reminder_count'] >= max_reminders(mode):
                if run['notified_at']:
                    continue
                due = (_timestamp(run['last_reminder_at']) + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
                if now_kst() < due:
                    continue
            elif now_kst() < reminder_due(mode, run, run['reminder_count'] + 1) or not week_open(run, now_kst()) or not in_window(mode, now_kst()):
                continue
            out.append({**project(mode, run), **{k: run[k] for k in ('reminder_count','last_reminder_at','notified_at','reminder_attempts')}})
        except ReminderError:
            pass
    return {'runs': out, 'blocked': blocked}


def in_window(mode, now):
    if mode == 'vetting':
        return in_vetting_window(now)
    if mode == 'cs':
        return in_cs_window(now)
    return True


def action(mode, rid, data):
    _mode(mode)
    attempts_table, notices_table = ledger(mode)
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        kind = data.get('kind')
        if kind == 'check':
            n, token = data.get('n'), data.get('token')
            if type(n) is not int or not 1 <= n <= max_reminders(mode) or not token:
                raise ReminderError(400, 'invalid reminder claim check')
            issued = db.execute(f"SELECT 1 FROM {attempts_table} WHERE mode=? AND run_id=? AND n=? AND token=? AND state='claimed'", (mode,rid,n,token)).fetchone()
            if not issued:
                raise ReminderError(409, 'claim token mismatch or no longer claimed')
            run = _run(mode, rid)
            if not week_open(run, now_kst()) or not in_window(mode, now_kst()):
                raise ReminderError(409, 'outside vetting send window')
            db.rollback()
            return {'ok': True, 'run': project(mode, run)}
        if kind == 'result':
            # Outcome must be recoverable even when a reply/disable occurred after claim.
            n, token, state = data.get('n'), data.get('token'), data.get('state')
            if type(n) is not int or n not in (1, 2) or state not in ('sent','failed','unknown') or not token:
                raise ReminderError(400, 'invalid reminder outcome')
            row = db.execute(f'SELECT * FROM {attempts_table} WHERE mode=? AND run_id=? AND n=? AND token=?', (mode, rid, n, token)).fetchone()
            if not row:
                raise ReminderError(409, 'claim token mismatch')
            if row['state'] != 'claimed':
                if row['state'] == state:
                    db.rollback()
                    return {'ok': True, 'duplicate': True}
                raise ReminderError(409, 'outcome already recorded')
            stamp = None
            if state == 'sent':
                try:
                    stamp = datetime.strptime(str(data.get('sent_at')), '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    raise ReminderError(400, 'actual KST sent_at required')
                if stamp > now_kst() or stamp < datetime.strptime(row['claimed_at'], '%Y-%m-%d %H:%M:%S'):
                    raise ReminderError(400, 'invalid actual sent time')
            db.execute(f'UPDATE {attempts_table} SET state=?,sent_at=?,error=? WHERE mode=? AND run_id=? AND n=?', (state, stamp.strftime('%Y-%m-%d %H:%M:%S') if stamp else None, str(data.get('error') or '')[:1000], mode, rid, n))
            result = {'ok': True, 'state': state}
        else:
            run = _run(mode, rid)
            if kind == 'notify':
                if run['reminder_count'] < max_reminders(mode):
                    raise ReminderError(409, 'successful reminder required')
                due = (_timestamp(run['last_reminder_at']) + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
                if now_kst() < due:
                    raise ReminderError(409, 'notification not due')
                cur = db.execute(f'INSERT OR IGNORE INTO {notices_table}(mode,run_id,notified_at) VALUES(?,?,?)', (mode, rid, now_kst().strftime('%Y-%m-%d %H:%M:%S')))
                result = {'ok': True, 'duplicate': cur.rowcount == 0}
            elif kind == 'claim':
                n = data.get('n')
                if type(n) is not int or not 1 <= n <= max_reminders(mode) or n != run['reminder_count'] + 1:
                    raise ReminderError(409, 'invalid reminder sequence')
                if any(a['n'] == n for a in run['reminder_attempts']):
                    raise ReminderError(409, 'reminder already claimed; reconcile outcome, do not resend')
                if now_kst() < reminder_due(mode, run, n) or not week_open(run, now_kst()) or not in_window(mode, now_kst()):
                    raise ReminderError(409, 'reminder not due')
                token = uuid.uuid4().hex
                db.execute(f'INSERT INTO {attempts_table}(mode,run_id,n,token,state,claimed_at) VALUES(?,?,?,?,?,?)', (mode,rid,n,token,'claimed',now_kst().strftime('%Y-%m-%d %H:%M:%S')))
                result = {'ok': True, 'token': token, 'n': n, 'run': project(mode, run)}
            else:
                raise ReminderError(400, 'invalid reminder action')
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
