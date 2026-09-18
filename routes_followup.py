"""On-demand Outlook evidence, isolated from business approval/closure state."""
import hashlib
import json
import uuid
from datetime import datetime
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
    d.pop('context_json', None)
    return d


@bp.get('/api/followup/<kind>/<int:target_id>')
@admin_required
def get_followup(kind, target_id):
    business_admin()
    ctx = context(kind, target_id)
    row = query('SELECT * FROM followup_job WHERE kind=? AND target_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (kind,target_id), one=True)
    return jsonify(context=ctx, job=public_job(row,ctx) if row else None)


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
