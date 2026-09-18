"""On-demand Outlook evidence, isolated from business approval/closure state."""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from werkzeug.exceptions import HTTPException
from flask import Blueprint, abort, jsonify, request, session
from app_core import get_db, query
from helpers_shared import admin_required, api_key_required, _automation_enabled

bp = Blueprint('routes_followup', __name__)
KINDS = {'daily', 'cs', 'vt', 'aor', 'fundreq', 'invoice'}


def context(kind, target_id):
    if kind not in KINDS:
        abort(404)
    if kind == 'daily':
        row = query('SELECT i.*,v.name vessel_name FROM issues i JOIN vessels v ON v.id=i.vessel_id WHERE i.id=?', (target_id,), one=True)
        fields = ('item_topic', 'description', 'actions', 'due_date', 'status', 'updated_at', 'email_subject_norm')
        title_key, ref_key = 'item_topic', None
    elif kind in ('cs', 'vt'):
        table, parent, fk = ('cs_findings', 'cs_surveys', 'survey_id') if kind == 'cs' else ('vt_findings', 'vettings', 'vetting_id')
        extra = 'p.year,p.quarter,p.vendor' if kind == 'cs' else 'p.report_number,p.inspection_company'
        row = query(f'SELECT f.*,v.name vessel_name,p.inspection_date,{extra} FROM {table} f JOIN {parent} p ON p.id=f.{fk} JOIN vessels v ON v.id=p.vessel_id WHERE f.id=?', (target_id,), one=True)
        fields = ('no', 'category', 'item', 'description', 'remark', 'user_remark', 'status', 'updated_at', 'inspection_date', 'report_number', 'year', 'quarter', 'vendor', 'inspection_company')
        title_key, ref_key = 'item', 'report_number'
    else:
        row = query(f'SELECT * FROM {kind}_draft WHERE id=?', (target_id,), one=True)
        fields = ('aor_cd', 'opex_cd', 'inv_cd', 'inv_no', 'ref_no', 'vsl_cd', 'vsl_nm', 'subj', 'subject', 'amt', 'cur_cd', 'vndr_nm', 'inv_dt', 'proposed_comment', 'dn', 'why', 'status', 'created_at', 'email_subj', 'attach_files', 'attachments')
        title_key = 'subject' if kind == 'invoice' else 'subj'
        ref_key = {'aor':'aor_cd', 'fundreq':'opex_cd', 'invoice':'inv_no'}[kind]
    if not row:
        abort(404)
    row = dict(row)
    record = {k: row.get(k) for k in fields if k in row}
    vessel = row.get('vessel_name') or row.get('vsl_nm') or ''
    ctx = {'vessel_name': vessel, 'title': row.get(title_key) or row.get('description') or '',
           'reference': (row.get(ref_key) or '') if ref_key else '',
           'baseline': row.get('updated_at') or row.get('created_at') or '',
           'record': record, 'search_subject': row.get('email_subj') or row.get('email_subject_norm') or ''}
    # Reingest/content edits invalidate evidence even if timestamps did not change.
    ctx['fingerprint'] = hashlib.sha256(json.dumps(ctx, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return ctx


def business_admin():
    row = query('SELECT app_scope FROM users WHERE id=?', (session['user_id'],), one=True)
    if not row or row['app_scope'] != 'business':
        abort(403)


def public_job(row, ctx):
    d = dict(row)
    d['result'] = json.loads(d['result'] or '{}')
    d['stale'] = d['fingerprint'] != ctx['fingerprint']
    run = query('SELECT status FROM automation_run WHERE run_id=?', (d['job_id'],), one=True)
    if d['state'] == 'queued':
        d['state'] = ({'running':'running','failed':'error','done':'error'}.get(run['status'], 'queued') if run else 'error')
    d['delayed'] = d['state'] in ('queued','running') and (datetime.now()-datetime.fromisoformat(d['created_at'])).total_seconds()>900
    if d['stale']:
        d['state'] = 'stale'
        d['result'] = {}  # Never display a prior target revision as current evidence.
    d['context_subject'] = json.loads(d['context_json']).get('search_subject','')
    if not d['stale'] and d['state'] == 'candidate':
        decorate_evidence(d)

    d.pop('context_json', None)
    return d


@bp.get('/api/followup/<kind>/<int:target_id>')
@admin_required
def get_followup(kind, target_id):
    business_admin()
    ctx = context(kind, target_id)
    row = query('SELECT * FROM followup_job WHERE kind=? AND target_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (kind,target_id), one=True)
    tracking = query('SELECT enabled,next_check,reason,fingerprint,search_subject FROM followup_tracking WHERE kind=? AND target_id=?', (kind,target_id), one=True)
    return jsonify(context=ctx, job=public_job(row,ctx) if row else None,
                   tracking=dict(tracking) if tracking else None)


@bp.post('/api/followup/<kind>/<int:target_id>/scan')
@admin_required
def scan(kind, target_id):
    business_admin()
    if not _automation_enabled():
        return jsonify(error='자동화가 정지되어 있습니다'), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='invalid payload'), 400
    subject = data.get('search_subject')
    if not isinstance(subject,str) or not 8 <= len(subject.strip()) <= 300 or any(ord(c)<32 for c in subject):
        return jsonify(error='관련 메일 제목을 8~300자로 입력하세요'), 400
    db = get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        ctx = context(kind,target_id)
        if not ctx['vessel_name']:
            db.rollback()
            return jsonify(error='선박명이 없어 연결할 수 없습니다'), 409
        if data.get('fingerprint') != ctx['fingerprint']:
            db.rollback()
            return jsonify(error='원본이 변경되었습니다. 다시 열어 확인하세요'), 409
        busy = db.execute("SELECT j.job_id FROM followup_job j JOIN automation_run a ON a.run_id=j.job_id WHERE j.kind=? AND j.target_id=? AND a.status IN ('queued','running')", (kind,target_id)).fetchone()
        if busy:
            db.rollback()
            return jsonify(job_id=busy['job_id'], reused=True), 202
        # Bounded operator-driven queue, never mailbox-wide collection.
        count = db.execute("SELECT COUNT(*) FROM automation_run WHERE task='followup_scan' AND status IN ('queued','running')").fetchone()[0]
        if count >= 10:
            db.rollback()
            return jsonify(error='조회 대기 10건입니다. 완료 후 다시 시도하세요'), 429
        jid = uuid.uuid4().hex
        ctx['search_subject'] = subject.strip()
        set_comparison_baseline(db,kind,target_id,ctx)
        db.execute('INSERT INTO followup_job(job_id,kind,target_id,fingerprint,context_json,requested_by) VALUES(?,?,?,?,?,?)',
                   (jid,kind,target_id,ctx['fingerprint'],json.dumps(ctx,ensure_ascii=False),str(session['user_id'])))
        db.execute("INSERT INTO automation_run(run_id,task,mode,status,requested_by,params) VALUES(?,'followup_scan','verify','queued',?,?)", (jid,str(session['user_id']),json.dumps({'job_id':jid})))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(job_id=jid), 202


@bp.get('/api/ext/followup/jobs/<job_id>')
@api_key_required
def runner_job(job_id):
    row = query('SELECT * FROM followup_job WHERE job_id=?', (job_id,), one=True)
    if not row:
        abort(404)
    d = dict(row)
    ctx = context(d['kind'],d['target_id'])
    if d['fingerprint'] != ctx['fingerprint']:
        return jsonify(error='stale target'), 409
    return jsonify(job={'job_id':job_id,'kind':d['kind'],'target_id':d['target_id'], 'fingerprint':d['fingerprint'],'context':json.loads(d['context_json'])})


def clean_result(raw):
    if not isinstance(raw,dict):
        raise ValueError('invalid result')
    out = {}
    for key,limit in (('summary',1200),('provider',40),('model',120),('source_subject',600),('coverage',400)):
        value = raw.get(key,'')
        if not isinstance(value,str) or len(value)>limit:
            raise ValueError('invalid '+key)
        out[key]=value
    out['items']=[]
    if not isinstance(raw.get('items',[]),list) or len(raw.get('items',[]))>12:
        raise ValueError('invalid items')
    for item in raw.get('items',[]):
        if not isinstance(item,dict):
            raise ValueError('invalid item')
        clean={}
        for key,limit in (('label',120),('quote',1000),('interpretation',1000)):
            v=item.get(key,'')
            if not isinstance(v,str) or len(v)>limit:
                raise ValueError('invalid '+key)
            clean[key]=v
        if not clean['quote'].strip():
            raise ValueError('quote required')
        clean['item_id'] = evidence_id(clean['quote'])
        # Optional trusted runner metadata; never parsed from quote/model text here.
        meta = item.get('source', {})
        if meta:
            clean['source'] = clean_source(meta)
        if not any(x['item_id'] == clean['item_id'] for x in out['items']):
            out['items'].append(clean)
    for key in ('attachments','match_keys'):
        vals=raw.get(key,[])
        if not isinstance(vals,list) or len(vals)>20 or any(not isinstance(v,str) or len(v)>300 for v in vals):
            raise ValueError('invalid '+key)
        out[key]=vals
    return out


@bp.post('/api/ext/followup/jobs/<job_id>')
@api_key_required
def runner_result(job_id):
    d=request.get_json(silent=True)
    if not isinstance(d,dict) or d.get('state') not in ('candidate','unsearchable','error'):
        return jsonify(error='invalid state'),400
    try:
        result=clean_result(d.get('result',{}))
        if d['state']=='candidate' and (not result['items'] and not result['attachments']):
            raise ValueError('candidate needs evidence')
        if d['state']=='candidate' and not {'vessel','subject','reading-pane'}.issubset(result['match_keys']):
            raise ValueError('match keys required')
    except ValueError as e:
        return jsonify(error=str(e)),400
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT * FROM followup_job WHERE job_id=?',(job_id,)).fetchone()
        if not row:
            abort(404)
        ctx=context(row['kind'],row['target_id'])
        if d.get('fingerprint')!=row['fingerprint'] or row['fingerprint']!=ctx['fingerprint']:
            db.rollback(); return jsonify(error='stale target'),409
        if row['state']!='queued':
            db.rollback(); return jsonify(ok=True,already_saved=True)
        db.execute("UPDATE followup_job SET state=?,result=?,checked_at=datetime('now','localtime') WHERE job_id=? AND state='queued'", (d['state'],json.dumps(result,ensure_ascii=False),job_id))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(ok=True)


@bp.post('/api/followup/<kind>/<int:target_id>/reviewed')
@admin_required
def reviewed(kind,target_id):
    business_admin()
    d=request.get_json(silent=True) or {}
    if not isinstance(d,dict):
        return jsonify(error='invalid payload'),400
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        ctx=context(kind,target_id)
        cur=db.execute("UPDATE followup_job SET reviewed_at=datetime('now','localtime'),reviewed_by=? WHERE job_id=? AND kind=? AND target_id=? AND fingerprint=? AND state='candidate'", (str(session['user_id']),d.get('job_id'),kind,target_id,ctx['fingerprint']))
        if not cur.rowcount:
            db.rollback(); return jsonify(error='현재 근거가 아니거나 확인할 결과가 없습니다'),409
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(ok=True)


@bp.post('/api/followup/<kind>/<int:target_id>/cancel')
@admin_required
def cancel_queued(kind,target_id):
    business_admin()
    d=request.get_json(silent=True)
    if not isinstance(d,dict):
        return jsonify(error='invalid payload'),400
    db=get_db();db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute("SELECT job_id FROM followup_job WHERE job_id=? AND kind=? AND target_id=? AND state='queued'",(d.get('job_id'),kind,target_id)).fetchone()
        if not row:
            db.rollback();return jsonify(error='취소할 대기 건이 없습니다'),409
        cur=db.execute("UPDATE automation_run SET status='failed',finished_at=datetime('now','localtime'),summary='사용자가 조회 대기 취소' WHERE run_id=? AND task='followup_scan' AND status='queued'",(row['job_id'],))
        if not cur.rowcount:
            db.rollback();return jsonify(error='실행 중인 조회는 취소하지 않습니다'),409
        db.execute("UPDATE followup_job SET state='error',result=? WHERE job_id=?",(json.dumps({'summary':'사용자가 조회 대기 취소'},ensure_ascii=False),row['job_id']))
        db.commit()
    except Exception:
        db.rollback();raise
    return jsonify(ok=True)


def evidence_id(quote):
    return hashlib.sha256(' '.join(quote.casefold().split()).encode()).hexdigest()


def subject_key(value):
    return ' '.join(value.casefold().split())


def clean_source(meta):
    if not isinstance(meta, dict):
        raise ValueError('invalid source')
    out = {}
    for k, limit in (('message_id', 600), ('received_at', 80), ('timestamp_source', 80)):
        v = meta.get(k, '')
        if not isinstance(v, str) or len(v) > limit:
            raise ValueError('invalid source ' + k)
        out[k] = v
    if out['received_at']:
        stamp = datetime.fromisoformat(out['received_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
            raise ValueError('invalid received timestamp')
        if not out['message_id'] or out['timestamp_source'] != 'outlook-message-header':
            raise ValueError('unverified timestamp source')
    return out


def previous_evidence(db, kind, target_id, subject, before=None):
    # Bound history reads; ignore a different user-selected conversation.
    rows = db.execute("SELECT rowid,* FROM followup_job WHERE kind=? AND target_id=? AND state='candidate' ORDER BY rowid DESC LIMIT 100", (kind,target_id)).fetchall()
    for row in rows:
        if before is not None and row['rowid'] >= before:
            continue
        ctx = json.loads(row['context_json'])
        if subject_key(ctx.get('search_subject','')) == subject_key(subject):
            yield row


def set_comparison_baseline(db, kind, target_id, ctx):
    history = list(previous_evidence(db,kind,target_id,ctx['search_subject']))
    previous = next((p for p in history if p['reviewed_at']), history[0] if history else None)
    ctx['comparison_baseline'] = (previous['reviewed_at'] or previous['checked_at']) if previous else ''
    ctx['comparison_baseline_kind'] = ('review' if previous['reviewed_at'] else 'scan') if previous else ''


def observation_time(value):
    """Persisted server result times only; never mail/model dates. Legacy DB is KST."""
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo('Asia/Seoul'))
        return stamp.astimezone(ZoneInfo('Asia/Seoul'))
    except (AttributeError, ValueError, TypeError):
        return None


def first_observations(db, job, rowid):
    """Read the append-only result history, without the display's 100-row cutoff.

    Identity is (target, user-selected subject, normalized citation). Older mail
    newly extracted later is new *evidence observed*, never new mail received.
    Invalid historical timestamps fail closed instead of borrowing today's date.
    """
    wanted = {evidence_id(i['quote']) for i in job['result'].get('items', [])}
    first = {}
    prior_ids = set()
    prior_subject = False
    rows = db.execute("SELECT rowid,context_json,result,checked_at FROM followup_job WHERE kind=? AND target_id=? AND state='candidate' AND rowid<=? ORDER BY rowid", (job['kind'],job['target_id'],rowid))
    for row in rows:
        ctx = json.loads(row['context_json'])
        if subject_key(ctx.get('search_subject','')) != subject_key(job['context_subject']):
            continue
        prior_subject = prior_subject or row['rowid'] < rowid
        stamp = observation_time(row['checked_at'])
        for old in json.loads(row['result']).get('items', []):
            iid = evidence_id(old['quote'])
            if iid not in wanted:
                continue
            if row['rowid'] < rowid:
                prior_ids.add(iid)
            if iid not in first:
                first[iid] = stamp
            elif first[iid] is not None and stamp is not None:
                first[iid] = min(first[iid],stamp)
    return first, prior_ids, prior_subject


def decorate_evidence(job):
    db = get_db()
    rowid = db.execute('SELECT rowid FROM followup_job WHERE job_id=?', (job['job_id'],)).fetchone()[0]
    ctx = json.loads(db.execute('SELECT context_json FROM followup_job WHERE job_id=?', (job['job_id'],)).fetchone()[0])
    previous = list(previous_evidence(db,job['kind'],job['target_id'],job['context_subject'],rowid))
    first_seen, seen, has_previous = first_observations(db,job,rowid)
    old_attachments = set()
    for p in previous:
        result = json.loads(p['result'])
        old_attachments.update(result.get('attachments',[]))
    decisions = {r['item_id']:dict(r) for r in db.execute('SELECT item_id,decision,reviewed_at FROM followup_item_review WHERE job_id=?', (job['job_id'],))}
    # Carry human decisions over a re-scan of the same target revision and subject.
    for p in previous:
        if p['fingerprint'] != job['fingerprint']:
            continue
        for r in db.execute('SELECT item_id,decision,reviewed_at FROM followup_item_review WHERE job_id=?',(p['job_id'],)):
            decisions.setdefault(r['item_id'],dict(r))
        if p['reviewed_at']:
            for i in json.loads(p['result']).get('items',[]):
                decisions.setdefault(evidence_id(i['quote']),{'decision':'confirmed','reviewed_at':p['reviewed_at']})
    if job['reviewed_at']:
        for i in job['result'].get('items',[]):
            decisions.setdefault(evidence_id(i['quote']),{'decision':'confirmed','reviewed_at':job['reviewed_at']})
    baseline = ctx.get('comparison_baseline','')
    job['comparison_baseline'] = baseline
    job['comparison_baseline_kind'] = ctx.get('comparison_baseline_kind','')
    job['time_basis'] = 'trmt_first_saved'
    job['observation_policy'] = '동일 항목·지정 제목·인용의 TRMT 최초 결과 저장시각'
    base_time = observation_time(baseline)
    counts = {'first':0,'new':0,'repeat':0,'received_after':0,'observed_after':0,'observed_before':0,'observation_unknown':0}
    for item in job['result'].get('items',[]):
        iid = evidence_id(item['quote'])
        item['item_id'] = iid
        item['change'] = 'repeat' if iid in seen else 'new' if has_previous else 'first'
        first_time = first_seen.get(iid)
        item['first_seen_at'] = first_time.isoformat() if first_time else None
        item['observed_after_baseline'] = first_time > base_time if first_time and base_time else None
        item['observation_status'] = ('after' if item['observed_after_baseline'] else 'before') if item['observed_after_baseline'] is not None else ('initial' if not has_previous and not baseline else 'unknown')
        if item['observed_after_baseline'] is True:
            counts['observed_after'] += 1
        elif item['observed_after_baseline'] is False:
            counts['observed_before'] += 1
        else:
            counts['observation_unknown'] += 1
        item['review'] = decisions.get(iid)
        item['received_after_baseline'] = None
        meta = item.get('source',{})
        if baseline and meta.get('received_at') and meta.get('timestamp_source') == 'outlook-message-header' and meta.get('message_id'):
            try:
                base = datetime.fromisoformat(baseline)
                if base.tzinfo is None:
                    base = base.replace(tzinfo=ZoneInfo('Asia/Seoul'))
                item['received_after_baseline'] = datetime.fromisoformat(meta['received_at'].replace('Z','+00:00')) > base
            except (ValueError, TypeError):
                pass
        counts[item['change']] += 1
        counts['received_after'] += item['received_after_baseline'] is True
    job['changes'] = counts
    job['new_attachment_names'] = [a for a in job['result'].get('attachments',[]) if a not in old_attachments] if previous else []


@bp.post('/api/followup/<kind>/<int:target_id>/items/review')
@admin_required
def review_item(kind, target_id):
    business_admin()
    data = request.get_json(silent=True)
    if not isinstance(data,dict) or data.get('decision') not in ('confirmed','excluded','applied'):
        return jsonify(error='invalid decision'),400
    if data['decision']=='applied' and kind!='daily':
        return jsonify(error='Daily 진행이력만 반영할 수 있습니다'),400
    db=get_db();db.execute('BEGIN IMMEDIATE')
    try:
        ctx=context(kind,target_id)
        row=db.execute("SELECT * FROM followup_job WHERE job_id=? AND kind=? AND target_id=? AND state='candidate'", (data.get('job_id'),kind,target_id)).fetchone()
        if not row or row['fingerprint']!=ctx['fingerprint']:
            db.rollback();return jsonify(error='원본 또는 근거가 변경되었습니다. 재조회하세요'),409
        item=next((x for x in json.loads(row['result']).get('items',[]) if evidence_id(x['quote'])==data.get('item_id')),None)
        if not item:
            db.rollback();return jsonify(error='근거 항목이 없습니다'),404
        old=db.execute('SELECT decision FROM followup_item_review WHERE job_id=? AND item_id=?',(row['job_id'],data['item_id'])).fetchone()
        if old and old['decision']=='applied':
            db.rollback();return jsonify(ok=True,already_applied=True)
        if data['decision']=='applied':
            # Preview text is generated from the saved quote; never trusts client-supplied history.
            from helpers_shared import _issue_write_scope
            _issue_write_scope(target_id)
            # Dedupe the durable write ledger, not the bounded display history.
            applied=db.execute("SELECT 1 FROM followup_item_review r JOIN followup_job j ON j.job_id=r.job_id WHERE j.kind=? AND j.target_id=? AND r.item_id=? AND r.decision='applied' LIMIT 1",(kind,target_id,data['item_id'])).fetchone()
            if applied:
                db.rollback();return jsonify(ok=True,already_applied=True)
            issue=db.execute('SELECT actions FROM issues WHERE id=?',(target_id,)).fetchone()
            try:
                actions=json.loads(issue['actions'] or '[]')
                if not isinstance(actions,list):
                    raise ValueError()
            except (ValueError,TypeError):
                db.rollback();return jsonify(error='기존 진행이력 형식을 확인해야 합니다'),409
            actions.append({'date':datetime.now().date().isoformat(),
                            'progress': '[메일 근거 · 감독 선택 반영 / 완료 미확정]\n'+item['quote']+'\n출처: '+json.loads(row['context_json'])['search_subject'],
                            'important':False,'followup_job':row['job_id'],'followup_item':data['item_id']})
            db.execute("UPDATE issues SET actions=?,updated_at=datetime('now','localtime') WHERE id=?",(json.dumps(actions,ensure_ascii=False),target_id))
            # Target fingerprint changes after append. Preserve reviewed artifact but do not silently rebind.
        db.execute("INSERT INTO followup_item_review(job_id,item_id,decision,reviewed_by) VALUES(?,?,?,?) ON CONFLICT(job_id,item_id) DO UPDATE SET decision=excluded.decision,reviewed_by=excluded.reviewed_by,reviewed_at=datetime('now','localtime')", (row['job_id'],data['item_id'],data['decision'],str(session['user_id'])))
        db.commit()
    except Exception:
        db.rollback();raise
    return jsonify(ok=True)


@bp.post('/api/followup/<kind>/<int:target_id>/tracking')
@admin_required
def tracking(kind,target_id):
    business_admin()
    data=request.get_json(silent=True)
    if not isinstance(data,dict) or type(data.get('enabled')) is not bool:
        return jsonify(error='invalid tracking'),400
    subject=data.get('search_subject','')
    if not isinstance(subject,str) or not 8<=len(subject.strip())<=300 or any(ord(c)<32 for c in subject):
        return jsonify(error='관련 메일 제목을 8~300자로 입력하세요'),400
    db=get_db();db.execute('BEGIN IMMEDIATE')
    try:
        ctx=context(kind,target_id)
        if data.get('fingerprint')!=ctx['fingerprint']:
            db.rollback();return jsonify(error='원본 변경: 다시 열어 확인하세요'),409
        if data['enabled'] and not ctx['vessel_name']:
            db.rollback();return jsonify(error='선박 연결이 없습니다'),409
        count=db.execute('SELECT COUNT(*) FROM followup_tracking WHERE enabled=1 AND NOT(kind=? AND target_id=?)',(kind,target_id)).fetchone()[0]
        if data['enabled'] and count>=10:
            db.rollback();return jsonify(error='선택 추적은 최대 10건입니다'),429
        db.execute("INSERT INTO followup_tracking(kind,target_id,enabled,search_subject,fingerprint,requested_by) VALUES(?,?,?,?,?,?) ON CONFLICT(kind,target_id) DO UPDATE SET enabled=excluded.enabled,search_subject=excluded.search_subject,fingerprint=excluded.fingerprint,requested_by=excluded.requested_by,next_check=datetime('now','localtime'),reason=''", (kind,target_id,int(data['enabled']),subject.strip(),ctx['fingerprint'],str(session['user_id'])))
        db.commit()
    except Exception:
        db.rollback();raise
    return jsonify(ok=True)


def enqueue_tracked():
    """Existing authenticated autorun poll only; no new scheduler, max one per poll.

    Opt-in six-hour checks; content edits/revoked user permissions pause, never auto-rebind.
    """
    if not _automation_enabled():
        return
    db=get_db();db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute("SELECT COUNT(*) FROM automation_run WHERE task='followup_scan' AND status IN ('queued','running')").fetchone()[0]>=10:
            db.rollback();return
        rows=db.execute("SELECT * FROM followup_tracking WHERE enabled=1 AND next_check<=datetime('now','localtime') ORDER BY next_check LIMIT 10").fetchall()
        for t in rows:
            owner=db.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND role='admin' AND app_scope='business'", (t['requested_by'],)).fetchone()
            try:
                ctx=context(t['kind'],t['target_id']) if owner else None
            except HTTPException as exc:
                if exc.code!=404:
                    raise
                ctx=None
            if not ctx or ctx['fingerprint']!=t['fingerprint']:
                db.execute("UPDATE followup_tracking SET enabled=0,reason='원본 변경 또는 접근권한 변경 · 다시 확인 후 켜세요' WHERE kind=? AND target_id=?",(t['kind'],t['target_id']))
                continue
            busy=db.execute("SELECT 1 FROM followup_job j JOIN automation_run a ON a.run_id=j.job_id WHERE j.kind=? AND j.target_id=? AND a.status IN ('queued','running')",(t['kind'],t['target_id'])).fetchone()
            if busy:
                continue
            ctx['search_subject']=t['search_subject']
            set_comparison_baseline(db,t['kind'],t['target_id'],ctx)
            jid=uuid.uuid4().hex
            db.execute('INSERT INTO followup_job(job_id,kind,target_id,fingerprint,context_json,requested_by) VALUES(?,?,?,?,?,?)',(jid,t['kind'],t['target_id'],ctx['fingerprint'],json.dumps(ctx,ensure_ascii=False),t['requested_by']))
            db.execute("INSERT INTO automation_run(run_id,task,mode,status,requested_by,params) VALUES(?,'followup_scan','verify','queued',?,?)",(jid,t['requested_by'],json.dumps({'job_id':jid})))
            db.execute("UPDATE followup_tracking SET next_check=datetime('now','localtime','+6 hours'),reason='' WHERE kind=? AND target_id=?",(t['kind'],t['target_id']))
            break
        db.commit()
    except Exception:
        db.rollback();raise


@bp.get('/api/followup/indicators')
@admin_required
def indicators():
    business_admin()
    rows=query("SELECT j.* FROM followup_job j WHERE j.rowid=(SELECT MAX(k.rowid) FROM followup_job k WHERE k.kind=j.kind AND k.target_id=j.target_id) ORDER BY j.rowid DESC LIMIT 100")
    out=[]
    for row in rows:
        try:
            ctx=context(row['kind'],row['target_id'])
        except HTTPException as exc:
            if exc.code==404:
                continue
            raise
        job=public_job(row,ctx)
        if job['state']!='candidate' or job['reviewed_at']:
            continue
        count=sum(1 for i in job['result'].get('items',[]) if not i['review'])
        if count:
            out.append({'kind':row['kind'],'target_id':row['target_id'],'count':count})
    return jsonify(items=out)
