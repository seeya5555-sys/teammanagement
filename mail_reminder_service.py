"""Shared bounded follow-up ledger. A claim is never evidence of a sent email."""
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


def _mode(mode):
    if mode not in ('class', 'vetting'):
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
    key = 'vessel_id' if mode == 'class' else 'vetting_id'
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
    elif not query("SELECT 1 FROM vt_findings WHERE vetting_id=? AND id IN (SELECT value FROM json_each(?)) AND COALESCE(status,'Open')='Open' LIMIT 1", (run['vetting_id'], run['finding_ids']), one=True):
        raise ReminderError(409, 'no sent findings still open')
    # Reply events are substantive in Vetting; receipt acknowledgements are not reply events.
    sql = ('SELECT 1 FROM class_mail_replies WHERE run_id=? AND updated>0 LIMIT 1' if mode == 'class' else
           "SELECT 1 FROM vetting_mail_events WHERE run_id=? AND kind IN ('reply','update','close') LIMIT 1")
    if query(sql, (rid,), one=True):
        raise ReminderError(409, 'substantive reply received')
    if mode == 'class' and query("SELECT 1 FROM class_mail_replies WHERE run_id=? AND updated=0 AND COALESCE(note,'') NOT IN ('','수신 확인만 — 결과/조치 회신 아님') LIMIT 1", (rid,), one=True):
        raise ReminderError(409, 'reply interpretation pending review')
    attempts = [dict(r) for r in query('SELECT * FROM mail_reminder_attempts WHERE mode=? AND run_id=? ORDER BY n', (mode, rid))]
    sent = [r for r in attempts if r['state'] == 'sent']
    notified = query('SELECT notified_at FROM mail_reminder_notices WHERE mode=? AND run_id=?', (mode, rid), one=True)
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
    for r in query(f"SELECT id FROM {mode}_mail_runs WHERE state='sent' AND sent_at>=datetime('now','localtime','-28 days') ORDER BY id"):
        try:
            run = _run(mode, r['id'])
            unresolved = [a for a in run['reminder_attempts'] if a['state'] != 'sent']
            if unresolved:
                blocked.append({'id':run['id'], 'vessel_name':run['vessel_name'], 'state':unresolved[-1]['state']})
                continue
            if run['reminder_count'] >= dm.MAX_REMINDERS:
                if run['notified_at']:
                    continue
                due = (_timestamp(run['last_reminder_at']) + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
                if now_kst() < due:
                    continue
            else:
                base = _timestamp(run['last_reminder_at'] or run['sent_at'])
                due = (base + timedelta(days=1 if run['reminder_count'] else dm.REPLY_DUE_DAYS + 1)).replace(hour=8, minute=30, second=0, microsecond=0)
                if now_kst() < due:
                    continue
            out.append({k: run[k] for k in ('id','vessel_name','subject','to_emails','cc_emails','dear','sent_at','reminder_count','last_reminder_at','notified_at','reminder_attempts','excel_sha256')})
        except ReminderError:
            pass
    return {'runs': out, 'blocked': blocked}


def action(mode, rid, data):
    _mode(mode)
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        kind = data.get('kind')
        if kind == 'check':
            n, token = data.get('n'), data.get('token')
            if type(n) is not int or not 1 <= n <= dm.MAX_REMINDERS or not token:
                raise ReminderError(400, 'invalid reminder claim check')
            issued = db.execute("SELECT 1 FROM mail_reminder_attempts WHERE mode=? AND run_id=? AND n=? AND token=? AND state='claimed'", (mode,rid,n,token)).fetchone()
            if not issued:
                raise ReminderError(409, 'claim token mismatch or no longer claimed')
            run = _run(mode, rid)
            db.rollback()
            return {'ok': True, 'run': {k:run[k] for k in ('id','vessel_name','subject','to_emails','cc_emails','dear','sent_at','excel_sha256')}}
        if kind == 'result':
            # Outcome must be recoverable even when a reply/disable occurred after claim.
            n, token, state = data.get('n'), data.get('token'), data.get('state')
            if type(n) is not int or n not in (1, 2) or state not in ('sent','failed','unknown') or not token:
                raise ReminderError(400, 'invalid reminder outcome')
            row = db.execute('SELECT * FROM mail_reminder_attempts WHERE mode=? AND run_id=? AND n=? AND token=?', (mode, rid, n, token)).fetchone()
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
            db.execute('UPDATE mail_reminder_attempts SET state=?,sent_at=?,error=? WHERE mode=? AND run_id=? AND n=?', (state, stamp.strftime('%Y-%m-%d %H:%M:%S') if stamp else None, str(data.get('error') or '')[:1000], mode, rid, n))
            result = {'ok': True, 'state': state}
        else:
            run = _run(mode, rid)
            if kind == 'notify':
                if run['reminder_count'] < dm.MAX_REMINDERS:
                    raise ReminderError(409, 'successful reminder required')
                due = (_timestamp(run['last_reminder_at']) + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
                if now_kst() < due:
                    raise ReminderError(409, 'notification not due')
                cur = db.execute('INSERT OR IGNORE INTO mail_reminder_notices(mode,run_id,notified_at) VALUES(?,?,?)', (mode, rid, now_kst().strftime('%Y-%m-%d %H:%M:%S')))
                result = {'ok': True, 'duplicate': cur.rowcount == 0}
            elif kind == 'claim':
                n = data.get('n')
                if type(n) is not int or not 1 <= n <= dm.MAX_REMINDERS or n != run['reminder_count'] + 1:
                    raise ReminderError(409, 'invalid reminder sequence')
                if any(a['n'] == n for a in run['reminder_attempts']):
                    raise ReminderError(409, 'reminder already claimed; reconcile outcome, do not resend')
                base = _timestamp(run['sent_at'] if n == 1 else run['last_reminder_at'])
                due = (base + timedelta(days=dm.REPLY_DUE_DAYS + 1 if n == 1 else 1)).replace(hour=8, minute=30, second=0, microsecond=0)
                if now_kst() < due:
                    raise ReminderError(409, 'reminder not due')
                token = uuid.uuid4().hex
                db.execute('INSERT INTO mail_reminder_attempts(mode,run_id,n,token,state,claimed_at) VALUES(?,?,?,?,?,?)', (mode,rid,n,token,'claimed',now_kst().strftime('%Y-%m-%d %H:%M:%S')))
                result = {'ok': True, 'token': token, 'n': n, 'run': {k:run[k] for k in ('id','vessel_name','subject','to_emails','cc_emails','dear','sent_at','excel_sha256')}}
            else:
                raise ReminderError(400, 'invalid reminder action')
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
