"""Vetting OBS 자동 메일 — request 비의존 서비스 (형 지시 2026-10-03).

매주 월 09:00 맥 러너가 SVMS Close report 로 Observation 자동 Close(ai_gemini.close_auto_judge) →
09:30 Open Observation 이 남은 SIRE 마다 Open 항목 엑셀을 첨부해 선박 담당자에게 확인 요청 메일.

불변식(서버 강제):
  · 수신처 = Daily 메일 설정(`daily_mail_settings`)에 등록된 선박별 To/CC 만. 이 기능은 주소를 받지 않는다.
  · 선박별 ON(`vetting_mail_settings.enabled`)이고 To 가 있어야 발송 대상.
  · vetting×ISO주 1 run(UNIQUE). claim 시 Open 항목 집합이 현재와 다르면 409(stale) — 엑셀과 본문이 어긋나지 않게.
  · sending/failed 는 자동 재발송하지 않는다(관리자 release 후에만).
"""
import json
import re
from datetime import date, datetime

from app_core import execute, execute_rc, query
from mail_common import MailServiceError
import daily_mail_service as dm

SUBJECT_TPL = '{vessel} - {sire_type}SIRE Inspection Status ({date})'
TEMPLATE_VARS = ('vessel', 'sire_type', 'date', 'inspection', 'open', 's', 'total', 'dear')
MANUAL_PREFIX = 'M'        # 수동 run 의 iso_week = 'M<요청id>' — 주차와 무관한 고정 키(요청×SIRE 1회, 자동 주간과 분리)
BODY_TPL = (
    'Dear {dear},\n'
    'Good day.\n'
    'Further to our previous correspondence regarding the recent SIRE inspection of M/T {vessel}'
    '{inspection}, we would appreciate receiving an update on the corrective actions for the {open} open '
    'observation{s} (out of {total} raised during the inspection).\n'
    'As the observations were forwarded earlier with a request for rectification details, kindly provide the '
    'latest status of each open item listed in the attached file, including:\n'
    '1) Corrective actions taken and/or planned\n'
    '2) Supporting evidence and photos, where applicable\n'
    '3) Expected completion date for any outstanding items\n'
    '4) Full corrective action report covering all open observations\n'
    'In addition, please ensure that all relevant supporting documents and evidence are uploaded to SVMS and '
    'provide your reply by email at the earliest opportunity.\n'
    'Your prompt cooperation is highly appreciated, as we need to closely monitor and follow up on the closure '
    'status of these observations.\n'
    'We look forward to receiving your update without further delay.\n'
    'Best regards,'
)


# 형 지시(2026-10-03): Vetting OBS 자동화(자동 Close·메일·회신반영)는 VLCC 전용.


def is_vlcc(vessel_type):
    return (vessel_type or '').strip().upper() == 'VLCC'


class VettingMailError(MailServiceError):
    pass


def _open_ids(vetting_id):
    return [int(r['id']) for r in query(
        "SELECT id FROM vt_findings WHERE vetting_id=? AND COALESCE(status,'Open')='Open' ORDER BY no, id",
        (vetting_id,))]


def get_template():
    row = query('SELECT subject_tpl, body_tpl, updated_by, updated_at FROM vetting_mail_template WHERE id=1', one=True)
    if row:
        return dict(row)
    return {'subject_tpl': SUBJECT_TPL, 'body_tpl': BODY_TPL, 'updated_by': None, 'updated_at': None}


def save_template(subject_tpl, body_tpl, user):
    subject_tpl = (subject_tpl or '').strip()
    body_tpl = (body_tpl or '').strip()
    if not subject_tpl or not body_tpl:
        raise VettingMailError(400, '제목/본문 템플릿은 비울 수 없습니다.')
    if len(subject_tpl) > 300 or len(body_tpl) > 8000:
        raise VettingMailError(400, '템플릿이 너무 깁니다(제목 300자 · 본문 8000자).')
    unknown = set(re.findall(r'\{([^{}]*)\}', subject_tpl + body_tpl)) - set(TEMPLATE_VARS)
    if unknown:
        raise VettingMailError(400, '알 수 없는 변수: ' + ', '.join('{%s}' % u for u in sorted(unknown)))
    execute('INSERT INTO vetting_mail_template(id, subject_tpl, body_tpl, updated_by, updated_at) '
            "VALUES(1, ?, ?, ?, datetime('now','localtime')) "
            'ON CONFLICT(id) DO UPDATE SET subject_tpl=excluded.subject_tpl, body_tpl=excluded.body_tpl, '
            'updated_by=excluded.updated_by, updated_at=excluded.updated_at', (subject_tpl, body_tpl, user))
    return get_template()


def _fill(tpl, vals):
    """지정 변수만 치환(str.format 미사용 — 중괄호 주입·속성접근 차단)."""
    return re.sub(r'\{(' + '|'.join(TEMPLATE_VARS) + r')\}', lambda m: str(vals[m.group(1)]), tpl)


def render(v, open_count, total, dear):
    """Dear = Daily 메일 설정의 선박별 Dear(빈칸이면 Sir/Madam) — Daily 자동화와 동일 규칙."""
    d = dm.format_mail_date(v['inspection_date']) if v['inspection_date'] else ''
    sire = (v['sire_type'] or '').strip()
    bits = [b for b in ((v['inspection_company'] or '').strip(), (v['port'] or '').strip(), d) if b]
    vals = {'vessel': v['vessel_name'], 'sire_type': (sire + ' ') if sire else '', 'date': d or v['report_number'] or '-',
            'inspection': (' (' + ', '.join(bits) + ')') if bits else '', 'open': open_count,
            's': '' if open_count == 1 else 's', 'total': total, 'dear': (dear or '').strip() or dm.DEFAULT_DEAR}
    t = get_template()
    return _fill(t['subject_tpl'], vals), _fill(t['body_tpl'], vals)


def list_settings():
    roster = dm.roster_vessel_ids()
    rows = query(
        "SELECT v.id AS vessel_id, v.name, COALESCE(s.to_emails,'') AS to_emails, COALESCE(s.cc_emails,'') AS cc_emails, "
        "       COALESCE(s.dear_name,'') AS dear_name, COALESCE(m.enabled,0) AS enabled, m.updated_by, m.updated_at, "
        "       (SELECT COUNT(*) FROM vettings vt WHERE vt.vessel_id=v.id AND EXISTS (SELECT 1 FROM vt_findings f "
        "          WHERE f.vetting_id=vt.id AND COALESCE(f.status,'Open')='Open')) AS open_vettings "
        "FROM vessels v LEFT JOIN daily_mail_settings s ON s.vessel_id=v.id "
        "LEFT JOIN vetting_mail_settings m ON m.vessel_id=v.id WHERE v.active=1 AND UPPER(TRIM(COALESCE(v.vessel_type,'')))='VLCC' ORDER BY v.name")
    return [dict(r) for r in rows if roster is None or r['vessel_id'] in roster]


def set_enabled(vessel_id, enabled, user):
    ves = query('SELECT vessel_type FROM vessels WHERE id=?', (vessel_id,), one=True)
    if not ves:
        raise VettingMailError(404, '선박이 없습니다.')
    if not is_vlcc(ves['vessel_type']):
        raise VettingMailError(400, 'Vetting OBS 메일은 VLCC 전용입니다.')
    if not dm.in_roster(vessel_id):
        raise VettingMailError(403, '담당 선박이 아닙니다.')
    enabled = 1 if enabled else 0
    cur = query('SELECT to_emails FROM daily_mail_settings WHERE vessel_id=?', (vessel_id,), one=True)
    if enabled and not (cur and dm.parse_emails(cur['to_emails'])[0]):
        raise VettingMailError(400, 'Daily 메일 설정에 To 주소가 없어 ON 할 수 없습니다.')
    execute("INSERT INTO vetting_mail_settings(vessel_id, enabled, updated_by, updated_at) "
            "VALUES(?,?,?,datetime('now','localtime')) ON CONFLICT(vessel_id) DO UPDATE SET "
            'enabled=excluded.enabled, updated_by=excluded.updated_by, updated_at=excluded.updated_at',
            (vessel_id, enabled, user))
    return {'vessel_id': vessel_id, 'enabled': enabled}


def runner_config(iso_week=None):
    """Open Observation 이 남은 모든 vetting. enabled/To 없는 건 skip 사유와 함께 내려 보고에 쓴다."""
    week = iso_week or dm.iso_week_of()
    if not dm.valid_iso_week(week):
        raise VettingMailError(400, 'iso_week 형식 오류')
    roster = dm.roster_vessel_ids()
    rows = query(
        "SELECT vt.*, ve.name AS vessel_name, COALESCE(s.to_emails,'') AS to_emails, "
        "       COALESCE(s.cc_emails,'') AS cc_emails, COALESCE(s.dear_name,'') AS dear_name, "
        "       COALESCE(m.enabled,0) AS mail_enabled, "
        "       (SELECT COUNT(*) FROM vt_findings f WHERE f.vetting_id=vt.id) AS total "
        "FROM vettings vt JOIN vessels ve ON ve.id=vt.vessel_id "
        "LEFT JOIN daily_mail_settings s ON s.vessel_id=ve.id LEFT JOIN vetting_mail_settings m ON m.vessel_id=ve.id "
        "WHERE ve.active=1 AND UPPER(TRIM(COALESCE(ve.vessel_type,'')))='VLCC' ORDER BY ve.name, vt.inspection_date DESC, vt.id DESC")
    out = []
    for v in rows:
        if roster is not None and v['vessel_id'] not in roster:
            continue
        ids = _open_ids(v['id'])
        if not ids:
            continue
        to, _ = dm.parse_emails(v['to_emails'])
        cc, _ = dm.parse_emails(v['cc_emails'])
        subject, body = render(v, len(ids), v['total'], v['dear_name'])
        run = query('SELECT id, state, sent_at FROM vetting_mail_runs WHERE vetting_id=? AND iso_week=?',
                    (v['id'], week), one=True)
        skip = None
        if not v['mail_enabled']:
            skip = 'mail_off'
        elif not to:
            skip = 'no_to'
        out.append({'vetting_id': v['id'], 'vessel_id': v['vessel_id'], 'vessel': v['vessel_name'],
                    'report_number': v['report_number'], 'inspection_date': v['inspection_date'],
                    'open_ids': ids, 'open_count': len(ids), 'total': v['total'], 'to': to, 'cc': cc,
                    'subject': subject, 'body': body, 'skip': skip,
                    'run_this_week': dict(run) if run else None})
    return {'iso_week': week, 'vettings': out}


def claim_run(d):
    try:
        vid = int(d.get('vetting_id'))
    except (TypeError, ValueError):
        raise VettingMailError(400, 'vetting_id 필요')
    week = d.get('iso_week') or ''
    if not dm.valid_iso_week(week):
        raise VettingMailError(400, 'iso_week 형식 오류')
    req = None
    if d.get('request_id') is not None:          # 수동 실행: ON 여부 무관, 요청 1건당 SIRE 별 1 run
        req = query("SELECT * FROM vetting_mail_requests WHERE id=? AND state='processing'", (d.get('request_id'),), one=True)
        if not req:
            raise VettingMailError(409, '수동 요청 없음/이미 처리됨')
    v = query('SELECT vt.*, ve.name AS vessel_name, ve.vessel_type, (SELECT COUNT(*) FROM vt_findings f WHERE f.vetting_id=vt.id) AS total '
              'FROM vettings vt JOIN vessels ve ON ve.id=vt.vessel_id WHERE vt.id=?', (vid,), one=True)
    if not v:
        raise VettingMailError(404, 'vetting 없음')
    if not dm.in_roster(v['vessel_id']):
        raise VettingMailError(409, '담당 선박 아님')
    if not is_vlcc(v['vessel_type']):
        raise VettingMailError(409, 'VLCC 아님')
    if req is not None:
        if int(req['vessel_id']) != int(v['vessel_id']):
            raise VettingMailError(409, '요청 선박과 다름')
        week = f"{MANUAL_PREFIX}{req['id']}"
    else:
        m = query('SELECT enabled FROM vetting_mail_settings WHERE vessel_id=?', (v['vessel_id'],), one=True)
        if not (m and m['enabled']):
            raise VettingMailError(409, 'Vetting 메일 OFF')
    s = query('SELECT * FROM daily_mail_settings WHERE vessel_id=?', (v['vessel_id'],), one=True)
    to = dm.parse_emails(s['to_emails'])[0] if s else []
    if not to:
        raise VettingMailError(409, 'To 없음')
    ids = _open_ids(vid)
    if not ids or sorted(int(x) for x in (d.get('finding_ids') or [])) != sorted(ids):
        raise VettingMailError(409, 'Open 항목이 바뀜(stale) — 다시 준비')
    subject, _ = render(v, len(ids), v['total'], s['dear_name'])
    cc = dm.parse_emails(s['cc_emails'])[0]
    try:
        rid = execute('INSERT INTO vetting_mail_runs(vetting_id, vessel_id, iso_week, subject, to_emails, cc_emails, '
                      'finding_ids, excel_sha256) VALUES(?,?,?,?,?,?,?,?)',
                      (vid, v['vessel_id'], week, subject, '; '.join(to), '; '.join(cc), json.dumps(ids),
                       (d.get('excel_sha256') or '')[:64]))
    except Exception as e:
        if 'UNIQUE' in str(e):
            raise VettingMailError(409, '이번 주 이미 claim 됨')
        raise
    _, body = render(v, len(ids), v['total'], s['dear_name'])
    return {'id': rid, 'to_emails': '; '.join(to), 'cc_emails': '; '.join(cc), 'subject': subject, 'body': body,
            'manual': req is not None}


def set_run_state(rid, d):
    state = d.get('state')
    if state not in ('sent', 'failed'):
        raise VettingMailError(400, 'state 는 sent|failed')
    n = execute("UPDATE vetting_mail_runs SET state=?, error=?, sent_at=? WHERE id=? AND state='sending'",
                (state, (d.get('error') or '')[:1000] or None,
                 (d.get('sent_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')) if state == 'sent' else None, rid))
    row = query('SELECT id, state FROM vetting_mail_runs WHERE id=?', (rid,), one=True)
    if not row:
        raise VettingMailError(404, 'run 없음')
    if row['state'] != state:
        raise VettingMailError(409, f"이미 {row['state']}")
    return dict(row)


def list_runs(limit=60):
    return [dict(r) for r in query(
        'SELECT r.*, ve.name AS vessel_name, vt.report_number FROM vetting_mail_runs r '
        'JOIN vessels ve ON ve.id=r.vessel_id JOIN vettings vt ON vt.id=r.vetting_id '
        'ORDER BY r.id DESC LIMIT ?', (limit,))]


def release_run(rid):
    """실패 run 또는 2시간 넘게 sending 에 멈춘 run 만 해제(진행 중 발송과 겹쳐 중복발송되지 않게)."""
    row = query('SELECT state FROM vetting_mail_runs WHERE id=?', (rid,), one=True)
    if not row:
        raise VettingMailError(404, 'run 없음')
    n = execute_rc("DELETE FROM vetting_mail_runs WHERE id=? AND (state='failed' OR (state='sending' "
                   "AND created_at < datetime('now','localtime','-2 hours')))", (rid,))
    if not n:
        raise VettingMailError(409, '발송 완료/진행 중 run 은 release 불가(실패건 또는 2시간 경과 sending 만)')
    return {'released': rid}


def recent_auto_closes(limit=30):
    out = []
    for r in query('SELECT c.*, ve.name AS vessel_name, vt.report_number FROM vt_close_auto_runs c '
                   'JOIN vettings vt ON vt.id=c.vetting_id JOIN vessels ve ON ve.id=vt.vessel_id '
                   'ORDER BY c.id DESC LIMIT ?', (limit,)):
        d = dict(r)
        d['closed_ids'] = json.loads(d['closed_ids'] or '[]')
        d.pop('result_json', None)
        out.append(d)
    return out


# ── 회신 반영 (형 지시 2026-10-03: 회신 내용대로 Observation 자동 변경) ──────────────
#   · 대상 = 발송 완료(sent) 후 REPLY_WINDOW_DAYS 이내 run 의 스냅샷 항목(finding_ids)만.
#   · Close 는 Open→Closed 단방향 CAS. 재오픈·신규 항목 생성 없음.
#   · 애매(needs_review) = 상태 유지 + Remark 앞에 '[확인 필요]' + 이벤트. 모든 변경은 before 값과 메일 근거 기록.
REPLY_WINDOW_DAYS = 28
REVIEW_TAG = '[확인 필요] '


def pending_runs():
    rows = query("SELECT r.*, ve.name AS vessel_name, vt.report_number FROM vetting_mail_runs r "
                 "JOIN vessels ve ON ve.id=r.vessel_id JOIN vettings vt ON vt.id=r.vetting_id "
                 "WHERE r.state='sent' AND r.sent_at >= datetime('now','localtime',?) "
                 "AND r.id = (SELECT r2.id FROM vetting_mail_runs r2 WHERE r2.vetting_id=r.vetting_id "
                 "            AND r2.state='sent' ORDER BY r2.sent_at DESC, r2.id DESC LIMIT 1) "   # 자동/수동 무관 최신 발송분만 팔로우업
                 "ORDER BY r.id",
                 (f'-{REPLY_WINDOW_DAYS} days',))
    out = []
    for r in rows:
        ids = json.loads(r['finding_ids'] or '[]')
        fs = query('SELECT id, no, item, description, status FROM vt_findings WHERE vetting_id=? ORDER BY no, id',
                   (r['vetting_id'],))
        done = [x['message_id'] for x in query(
            "SELECT DISTINCT message_id FROM vetting_mail_events WHERE run_id=? AND kind='reply'", (r['id'],))]
        out.append({'id': r['id'], 'vetting_id': r['vetting_id'], 'vessel_name': r['vessel_name'],
                    'report_number': r['report_number'], 'subject': r['subject'], 'to_emails': r['to_emails'],
                    'cc_emails': r['cc_emails'], 'sent_at': r['sent_at'],
                    'findings': [dict(f) for f in fs if int(f['id']) in ids],
                    'processed_message_ids': done})
    return {'runs': out}


def _run_finding(rid, fid):
    run = query("SELECT * FROM vetting_mail_runs WHERE id=?", (rid,), one=True)
    if not run:
        raise VettingMailError(404, 'run 없음')
    if run['state'] != 'sent':
        raise VettingMailError(409, '발송 완료 run 아님')
    if int(fid) not in json.loads(run['finding_ids'] or '[]'):
        raise VettingMailError(409, '이 메일로 보낸 항목 아님')
    f = query('SELECT * FROM vt_findings WHERE id=? AND vetting_id=?', (fid, run['vetting_id']), one=True)
    if not f:
        raise VettingMailError(404, '항목 없음')
    return run, f


def _event(rid, fid, kind, mid, evidence, before):
    try:
        execute('INSERT INTO vetting_mail_events(run_id, finding_id, kind, message_id, evidence, before_json) '
                'VALUES(?,?,?,?,?,?)', (rid, fid, kind, mid, (evidence or '')[:3900],
                                        json.dumps(before, ensure_ascii=False) if before is not None else None))
        return True
    except Exception as e:
        if 'UNIQUE' in str(e):
            return False
        raise


# ── 수동 실행 (형 지시 2026-10-03: 원할 때만, 자동 주간과 별개) ──────────────────────
def request_manual(vessel_id, user):
    ves = query('SELECT vessel_type FROM vessels WHERE id=?', (vessel_id,), one=True)
    if not ves:
        raise VettingMailError(404, '선박이 없습니다.')
    if not is_vlcc(ves['vessel_type']):
        raise VettingMailError(400, 'Vetting OBS 메일은 VLCC 전용입니다.')
    if not dm.in_roster(vessel_id):
        raise VettingMailError(403, '담당 선박이 아닙니다.')
    s = query('SELECT to_emails FROM daily_mail_settings WHERE vessel_id=?', (vessel_id,), one=True)
    if not (s and dm.parse_emails(s['to_emails'])[0]):
        raise VettingMailError(400, 'Daily 메일 설정에 To 주소가 없습니다.')
    if not any(_open_ids(r['id']) for r in query('SELECT id FROM vettings WHERE vessel_id=?', (vessel_id,))):
        raise VettingMailError(400, 'Open Observation 이 없습니다.')
    execute("UPDATE vetting_mail_requests SET state='failed', result='2시간 넘게 처리 중 — 중단 처리(보낸편지함 확인)', "
            "done_at=datetime('now','localtime') WHERE vessel_id=? AND state='processing' "
            "AND created_at < datetime('now','localtime','-2 hours')", (vessel_id,))
    try:
        rid = execute('INSERT INTO vetting_mail_requests(vessel_id, requested_by) VALUES(?,?)', (vessel_id, user))
    except Exception as e:
        if 'UNIQUE' in str(e):
            raise VettingMailError(409, '이미 대기 중인 수동 요청이 있습니다.')
        raise
    return {'id': rid, 'state': 'queued'}


def queued_requests():
    return {'requests': [dict(r) for r in query(
        "SELECT q.id, q.vessel_id, ve.name AS vessel_name, q.requested_by, q.created_at FROM vetting_mail_requests q "
        "JOIN vessels ve ON ve.id=q.vessel_id WHERE q.state='queued' ORDER BY q.id")]}


def claim_request(qid):
    """러너 1개만 요청을 잡는다(queued→processing 원자 전환)."""
    if not execute_rc("UPDATE vetting_mail_requests SET state='processing' WHERE id=? AND state='queued'", (qid,)):
        raise VettingMailError(409, '이미 처리 중/완료된 요청')
    return {'id': qid, 'state': 'processing'}


def finish_request(qid, d):
    state = d.get('state')
    if state not in ('done', 'failed'):
        raise VettingMailError(400, 'state 는 done|failed')
    if state == 'done' and query("SELECT 1 FROM vetting_mail_runs WHERE iso_week=? AND state!='sent'",
                                 (f'{MANUAL_PREFIX}{qid}',), one=True):
        state = 'failed'                    # 발송 확정 안 된 run 이 있으면 done 으로 닫지 않는다
    n = execute_rc("UPDATE vetting_mail_requests SET state=?, result=?, done_at=datetime('now','localtime') "
                   "WHERE id=? AND state='processing'", (state, (d.get('result') or '')[:2000], qid))
    if not n:
        raise VettingMailError(409, '처리 중 요청 아님')
    return {'id': qid, 'state': state}


def list_requests(limit=30):
    return [dict(r) for r in query(
        'SELECT q.*, ve.name AS vessel_name FROM vetting_mail_requests q JOIN vessels ve ON ve.id=q.vessel_id '
        'ORDER BY q.id DESC LIMIT ?', (limit,))]


def reply_status():
    """SIRE 별 가장 최근 발송분(자동·수동 무관)의 회신 여부·반영 결과 — 웹/앱 '회신현황' 섹션."""
    rows = query("SELECT r.id, r.vetting_id, r.iso_week, r.sent_at, r.finding_ids, ve.name AS vessel_name, vt.report_number "
                 "FROM vetting_mail_runs r JOIN vessels ve ON ve.id=r.vessel_id JOIN vettings vt ON vt.id=r.vetting_id "
                 "WHERE r.state='sent' AND r.id = (SELECT r2.id FROM vetting_mail_runs r2 WHERE r2.vetting_id=r.vetting_id "
                 "  AND r2.state='sent' ORDER BY r2.sent_at DESC, r2.id DESC LIMIT 1) "
                 "ORDER BY ve.name, r.sent_at DESC")
    out = []
    for r in rows:
        ev = query("SELECT kind, evidence, created_at FROM vetting_mail_events WHERE run_id=? ORDER BY id", (r['id'],))
        replies = [e for e in ev if e['kind'] == 'reply']
        cnt = {k: sum(1 for e in ev if e['kind'] == k) for k in ('close', 'update', 'needs_review')}
        last = replies[-1] if replies else None
        ids = json.loads(r['finding_ids'] or '[]')
        open_now = query("SELECT COUNT(*) AS n FROM vt_findings WHERE id IN (SELECT value FROM json_each(?)) "
                         "AND COALESCE(status,'Open')='Open'", (r['finding_ids'] or '[]',), one=True)['n']
        out.append({'run_id': r['id'], 'vessel_name': r['vessel_name'], 'report_number': r['report_number'],
                    'manual': (r['iso_week'] or '').startswith(MANUAL_PREFIX), 'sent_at': r['sent_at'],
                    'sent_count': len(ids), 'open_now': open_now, 'replied': bool(replies),
                    'reply_count': len(replies), 'last_reply_at': last['created_at'] if last else None,
                    'last_reply_from': (last['evidence'] or '').split(' · ')[0] if last else None,
                    'closed': cnt['close'], 'updated': cnt['update'], 'needs_review': cnt['needs_review']})
    return out
