#!/usr/bin/env python3
import base64, hashlib, json, os, sys, tempfile
from datetime import datetime, timedelta

os.chdir(os.path.expanduser('~/projects/teammanagement'))
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB
import app as A
A.DATABASE = DB; A.app.config['DATABASE'] = DB; A.app.config['TESTING'] = True
A.init_db(drop=False); A._auto_migrate(); A.app.app_context().push()
c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'
key = A.query("SELECT v FROM api_settings WHERE k='api_key'", one=True)
if not key:
    A.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','smokekey')"); apikey='smokekey'
else: apikey=key['v']
H={'X-API-Key':apikey}
fails=[]
def chk(x,n,extra=''):
    print(('  ok  ' if x else '  FAIL  ')+n+((' — '+str(extra)) if extra and not x else ''))
    if not x:fails.append(n)

SX='BGBBCX2605270002'
def line(seq, cfm='N'):
    return {'SX_CD':SX,'SX_SEQ':seq,'SOA_AMT':5.83,'SOA_CUR_CD':'USD','AMT_USD':5.83,
            'INV_NO':'INV-'+seq,'INV_NO_ORG':None,'FILE_REF_NO':'F'+seq,'REF_NO':'R'+seq,
            'EXP_CD':'010101','EXP_NM':'Agency','SOA_TP':'D','SOA_OPEX_TP':'O',
            'CFM_YN':cfm,'RJT_YN':'N','RJT_RMK':None,'STATUS2':'N','STATUS_RMK2':None,
            'SOA_VNDR_NM':'Vendor','source_hash':'h'+seq,'immutable_hash':'i'+seq,
            'machine_state':'confirmed' if cfm=='Y' else 'pending',
            'machine_reason':'' if cfm=='Y' else 'Confirm/Reject 미검증','exception':cfm!='Y'}
pdf=b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n'
def snap(cfm2='Y'):
    return {'sx_cd':SX,'header_status':'S','vessel':'BGBB','owner_comp_id':'037','lines':[line('0010'),line('0011',cfm2)],
            'attachments':[{'sx_seq':'0010','filename':'invoice.pdf','content_type':'application/pdf','size':len(pdf),
                            'sha256':hashlib.sha256(pdf).hexdigest(),'data_base64':base64.b64encode(pdf).decode()}]}

chk(A.get_db().execute('PRAGMA foreign_keys').fetchone()[0] == 1, 'FK cascade enabled')
chk(c.post('/api/ext/soa/reviews/snapshot',json=snap()).status_code in (401,403), 'snapshot API key required')
chk(c.get('/api/automation/soa/reviews').status_code == 200, 'admin session accepted')
with c.session_transaction() as s: saved=dict(s); s.clear()
chk(c.get('/api/automation/soa/reviews').status_code in (302,401,403), 'review admin auth required')
with c.session_transaction() as s: s.update(saved)
r=c.post('/api/ext/soa/reviews/snapshot',json=snap(),headers=H); j=r.get_json()
chk(r.status_code==200 and j['ok'],'snapshot ingest',r.get_data(as_text=True)); sv=j['snapshot_version']; dv=j['draft_version']
r=c.get('/api/automation/soa/reviews'); chk(r.status_code==200 and len(r.get_json()['cases'])==1,'admin list')
r=c.get('/api/automation/soa/reviews/'+SX); case=r.get_json()['case']
chk(case['line_count']==2 and len(case['lines'][0]['attachments'])==1,'detail lines+pdf',case)
aid=case['lines'][0]['attachments'][0]['id']
chk(c.get(f'/api/automation/soa/reviews/attachments/{aid}/pdf').status_code==200,'private pdf download')
r=c.post('/api/automation/soa/reviews/'+SX+'/action',json={'action':'refresh','snapshot_version':sv,'draft_version':dv})
refresh_run=r.get_json()['run_id']; chk(r.status_code==200,'queue refresh for done-unlock')
r=c.post(f'/api/ext/automation/{refresh_run}/done',json={'status':'failed','exit_code':1,'summary':'runner failed before result'},headers=H)
chk(r.status_code==200 and not c.get('/api/automation/soa/reviews/'+SX).get_json()['case']['locked'],
    'automation done fail-safe unlock')

A.execute("UPDATE soa_review_case SET fresh_until='2000-01-01 00:00:00' WHERE sx_cd=?", (SX,))
chk(c.get('/api/automation/soa/reviews/'+SX).get_json()['case']['fresh'] is False, 'stale fixture')
r=c.put('/api/automation/soa/reviews/'+SX+'/draft',json={'draft_version':dv,'lines':[{'sx_seq':'0010','decision':'confirm','remark':''}]})
chk(r.status_code==200,'stale snapshot draft save',r.get_data(as_text=True)); case=r.get_json()['case']; dv=case['draft_version']
chk(case['can_push'] and not case['can_approve'],'stale draft enables CAS-protected push',case)
chk(c.put('/api/automation/soa/reviews/'+SX+'/draft',json={'draft_version':dv-1,'lines':[]}).status_code==409,'draft CAS conflict')

r=c.post('/api/automation/soa/reviews/'+SX+'/action',json={'action':'push','snapshot_version':sv,'draft_version':dv})
chk(r.status_code==200,'queue push',r.get_data(as_text=True)); run_id=r.get_json()['run_id']
chk(c.post('/api/ext/soa/reviews/snapshot',json=snap(),headers=H).status_code==409,
    'foreign snapshot cannot overwrite queued draft')
r=c.get('/api/ext/soa/reviews/'+SX+f'/command?action=push&snapshot_version={sv}&draft_version={dv}',headers=H); cmd=r.get_json()
chk(r.status_code==200 and cmd['locked'] and cmd['draft_lines'][0]['decision']=='confirm','locked command',cmd)
# Push runner posts a fresh, fully-confirmed source snapshot before result.
r=c.post('/api/ext/soa/reviews/snapshot',json=snap('Y') | {'run_id':run_id,'lines':[line('0010','Y'),line('0011','Y')]},headers=H)
chk(r.status_code==200,'post-push snapshot',r.get_data(as_text=True)); sv2=r.get_json()['snapshot_version']; dv2=r.get_json()['draft_version']
chk(c.post('/api/ext/soa/reviews/'+SX+'/result',json={'action':'push','status':'done','run_id':'wrong'},headers=H).status_code==409,
    'stale result run_id rejected')
r=c.post('/api/ext/soa/reviews/'+SX+'/result',json={'action':'push','status':'done','run_id':run_id,'applied_seqs':['0010']},headers=H)
chk(r.status_code==200,'push result unlock',r.get_data(as_text=True))
case=c.get('/api/automation/soa/reviews/'+SX).get_json()['case']
chk(case['can_approve'] and not case['locked'],'all-confirm approval gate',case)
A.execute("UPDATE soa_review_case SET fresh_until='2000-01-01 00:00:00' WHERE sx_cd=?", (SX,))
case=c.get('/api/automation/soa/reviews/'+SX).get_json()['case']
chk(not case['can_approve'] and
    c.post('/api/automation/soa/reviews/'+SX+'/action',json={'action':'approve','snapshot_version':sv2,'draft_version':dv2}).status_code==409,
    'stale approval remains blocked')
A.execute("UPDATE soa_review_case SET fresh_until=? WHERE sx_cd=?",
          ((datetime.now() + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S'), SX))
r=c.post('/api/automation/soa/reviews/'+SX+'/action',json={'action':'approve','snapshot_version':sv2,'draft_version':dv2})
chk(r.status_code==200,'queue approve',r.get_data(as_text=True)); approve_run=r.get_json()['run_id']
r=c.post('/api/ext/soa/reviews/'+SX+'/result',json={'action':'approve','status':'done','run_id':approve_run,'soa_status':'C'},headers=H)
chk(r.status_code==200,'approve result',r.get_data(as_text=True))
case=c.get('/api/automation/soa/reviews/'+SX).get_json()['case']
chk(case['status']=='C' and case['read_only'] and not case['can_push'] and not case['can_approve'],'C readonly',case)
A.execute("UPDATE soa_review_attachment SET expires_at='2000-01-01 00:00:00' WHERE id=?", (aid,))
chk(c.get(f'/api/automation/soa/reviews/attachments/{aid}/pdf').status_code==404, 'expired PDF denied')

try: os.unlink(DB)
except OSError: pass
if fails:
    print('FAIL',fails); raise SystemExit(1)
print('PASS: SOA review backend')
