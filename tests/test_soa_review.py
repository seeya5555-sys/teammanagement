#!/usr/bin/env python3
import base64, hashlib, json, os, sys, tempfile
from datetime import datetime, timedelta

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
chk(c.get('/api/ext/soa/reviews/open').status_code in (401,403), 'open-case API key required')
r=c.get('/api/ext/soa/reviews/open',headers=H)
chk(r.status_code==200 and r.get_json()['cases']==[{'sx_cd':SX,'status':'S'}],
    'open-case API returns non-final review case',r.get_data(as_text=True))
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
r=c.get('/api/ext/soa/reviews/open',headers=H)
chk(r.status_code==200 and all(x['sx_cd'] != SX for x in r.get_json()['cases']),
    'open-case API excludes final C case',r.get_data(as_text=True))
A.execute("UPDATE soa_review_case SET status='T' WHERE sx_cd=?", (SX,))
r=c.get('/api/ext/soa/reviews/open',headers=H)
chk(r.status_code==200 and all(x['sx_cd'] != SX for x in r.get_json()['cases']),
    'open-case API excludes final T case',r.get_data(as_text=True))
A.execute("UPDATE soa_review_attachment SET expires_at='2000-01-01 00:00:00' WHERE id=?", (aid,))
chk(c.get(f'/api/automation/soa/reviews/attachments/{aid}/pdf').status_code==404, 'expired PDF denied')

# ── needs_review: 리젝 결론 + SVMS 반영 끝난 건은 검토함에서 빠져야 함 ──────────────
SX2='ATGVCX2607240001'
def rline(seq, cfm='Y', rjt='N', rmk=None):
    d=line(seq, cfm); d['SX_CD']=SX2; d['RJT_YN']=rjt; d['RJT_RMK']=rmk
    d['machine_state']='rejected' if rjt=='Y' else ('confirmed' if cfm=='Y' else 'pending')
    d['exception']=(rjt=='Y' or cfm!='Y')
    return d
snap2={'sx_cd':SX2,'header_status':'S','vessel':'ATGV','owner_comp_id':'037',
       'lines':[rline('0001'),rline('0002',cfm='N',rjt='Y',rmk='Wrong cost (Invoice : 1,500usd)')]}
r=c.post('/api/ext/soa/reviews/snapshot',json=snap2,headers=H)
chk(r.status_code==200,'reject-case snapshot ingest',r.get_data(as_text=True))
case2=c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']
chk(case2['rejected_count']==1 and case2['exception_count']==1 and case2['open_exception_count']==0
    and case2['pending_count']==0,'rejected line counts as resolved exception',case2)
chk(case2['needs_review'] is False and not case2['can_approve'],'reject+pushed case leaves review inbox',case2)
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",('failed: runner blew up',SX2))
chk(c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']['needs_review'] is True,
    'failed action keeps case in inbox')
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",
          (json.dumps({'action':'push','status':'done','reconcile_required':False}),SX2))
chk(c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']['needs_review'] is False,
    'successful push result does not pin case')
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",
          (json.dumps({'action':'push','status':'done','reconcile_required':True}),SX2))
chk(c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']['needs_review'] is True,
    'reconcile_required keeps case in inbox')
for bad in ('{"status":"done","reconcile_required":"true"}', '{"status":"done_with_error"}', '{"action":"push"}',
            '"done"', '["done"]', '{oops', 'failed', 'partial: 3 of 5'):
    A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",(bad,SX2))
    got=c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']
    chk(got['action_failed'] is True and got['review_bucket']=='attention','불명확한 액션결과는 실패로 봄: '+bad,got['review_bucket'])
for good in ('done','OK','{"status":"ok","reconcile_required":false}','{"status":"done","reconcile_required":0}'):
    A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",(good,SX2))
    chk(c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']['action_failed'] is False,
        '명시적 성공만 성공으로 봄: '+good)
# read_only(C/T)여도 실패·잠금은 숨기지 않음
A.execute("UPDATE soa_review_case SET status='C',last_action_result=? WHERE sx_cd=?",
          (json.dumps({'action':'push','status':'done','reconcile_required':True}),SX2))
got=c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']
chk(got['read_only'] and got['needs_review'] is True and got['review_bucket']=='attention',
    'C 상태여도 reconcile 실패는 검토함에 노출',got)
A.execute("UPDATE soa_review_case SET last_action_result=NULL,queued_action='push',queued_run_id='rid-x' WHERE sx_cd=?",(SX2,))
got=c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']
chk(got['locked'] and got['needs_review'] is True,'C 상태여도 처리중 잠금은 노출',got)
A.execute("UPDATE soa_review_case SET queued_action=NULL,queued_run_id=NULL WHERE sx_cd=?",(SX2,))
got=c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']
chk(got['needs_review'] is False and got['review_bucket']=='closed' and got['approval_pending'] is False,
    'C 종결 건은 완료 섹션에도 안 뜸',got)
A.execute("UPDATE soa_review_case SET status='S' WHERE sx_cd=?",(SX2,))
chk(c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']['review_bucket']=='reject_waiting',
    'S+리젝 반영완료 = reject_waiting 버킷')
A.execute("UPDATE soa_review_case SET last_action_result=NULL WHERE sx_cd=?",(SX2,))
r=c.post('/api/ext/soa/reviews/snapshot',
         json=snap2|{'lines':[rline('0001'),rline('0002',cfm='N',rjt='N')]},headers=H)
chk(r.status_code==200 and c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']['needs_review'] is True,
    'SM 회신으로 리젝 해제되면 다시 검토 대상',r.get_data(as_text=True))
chk(len(c.get('/api/automation/soa/reviews').get_json()['cases'])==2,'list still returns every case')
# 전 라인 Confirm·미승인인데 스냅샷이 낡은(stale) 승인대기 건은 목록에서 사라지면 안 됨
r=c.post('/api/ext/soa/reviews/snapshot',json=snap2|{'lines':[rline('0001'),rline('0002')]},headers=H)
A.execute("UPDATE soa_review_case SET fresh_until='2000-01-01 00:00:00' WHERE sx_cd=?",(SX2,))
got=c.get('/api/automation/soa/reviews/'+SX2).get_json()['case']
chk(r.status_code==200 and got['all_confirmed'] and not got['fresh'] and not got['can_approve']
    and got['approval_pending'] is True and got['needs_review'] is True,'stale 승인대기 건 목록 유지',got)

# ── SVMS header STATUS 드리프트: R(SM 반려)·미지 코드도 동기화돼야 로컬이 안 굳음 ──────────
SX3='GYPSCX2606230002'
def gline(seq, cfm='Y', rjt='N', rmk=None):
    d=line(seq, cfm); d['SX_CD']=SX3; d['RJT_YN']=rjt; d['RJT_RMK']=rmk
    d['machine_state']='rejected' if rjt=='Y' else ('confirmed' if cfm=='Y' else 'pending')
    d['exception']=(rjt=='Y' or cfm!='Y')
    return d
def gsnap(status, lines_):
    return {'sx_cd':SX3,'header_status':status,'vessel':'GYPS','owner_comp_id':'037','lines':lines_}
r=c.post('/api/ext/soa/reviews/snapshot',json=gsnap('S',[gline('0001'),gline('0002',cfm='N',rjt='Y',rmk='Wrong cost')]),headers=H)
chk(r.status_code==200,'R 케이스 시드 ingest',r.get_data(as_text=True))
r=c.post('/api/ext/soa/reviews/snapshot',json=gsnap('R',[gline('0001'),gline('0002',cfm='N',rjt='Y',rmk='Wrong cost')]),headers=H)
chk(r.status_code==200,'SVMS R(반려) 상태 snapshot ingest 허용',r.get_data(as_text=True))
got=c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']
chk(got['status']=='R' and got['read_only'] and not got['editable'] and not got['can_push']
    and not got['can_approve'] and got['approval_pending'] is False,'R은 read-only fail-closed',got)
chk(got['needs_review'] is False and got['review_bucket']=='reject_waiting','R = SM 회신 대기 버킷',got)
r=c.post('/api/ext/soa/reviews/snapshot',json=gsnap('R',[gline('0001'),gline('0002')]),headers=H)
got=c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']
chk(r.status_code==200 and got['rejected_count']==0 and got['review_bucket']=='reject_waiting',
    '라인 리젝 0이어도 header R이면 회신 대기',got)
r=c.post('/api/ext/soa/reviews/snapshot',json=gsnap('X',[gline('0001'),gline('0002')]),headers=H)
got=c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']
chk(r.status_code==200 and got['status']=='X' and got['read_only'] and not got['can_approve'],
    '미지 상태 코드도 동기화하되 쓰기 금지',got)
chk(got['needs_review'] is True and got['review_bucket']=='attention',
    '미지 상태 코드는 조용히 종결시키지 않고 노출',got)
for bad_status in ('', '   ', 'ABC', '1S', 's;', 'R R'):
    rr=c.post('/api/ext/soa/reviews/snapshot',json=gsnap(bad_status,[gline('0001')]),headers=H)
    chk(rr.status_code==400,'형식 불량 STATUS 거부: '+repr(bad_status),rr.status_code)
rr=c.post('/api/ext/soa/reviews/snapshot',json=gsnap(' r ',[gline('0001'),gline('0002')]),headers=H)
chk(rr.status_code==200 and c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']['status']=='R',
    'STATUS는 trim+대문자 정규화 후 저장',rr.get_data(as_text=True))
# 이미 SVMS에서 종결(C)된 뒤의 '쓰기 전 중단' 실패 기록만 무의미 — 그 외 실패는 계속 붙잡는다
STALE_PRE=json.dumps({'action':'approve','status':'stale','applied_seqs':[],'reconcile_required':False,
                      'error':'not approvable STATUS=C lines=29'})
r=c.post('/api/ext/soa/reviews/snapshot',json=gsnap('C',[gline('0001'),gline('0002')]),headers=H)
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",(STALE_PRE,SX3))
got=c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']
chk(r.status_code==200 and got['action_failed'] is True and got['reconcile_required'] is False
    and got['needs_review'] is False and got['review_bucket']=='closed',
    'C 종결+전라인 Confirm+pre-write 중단이면 잔존하지 않음',got)
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",
          ('failed: SOA review approve failed: RuntimeError: not approvable STATUS=C lines=29',SX3))
got=c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']
chk(got['needs_review'] is True and got['review_bucket']=='attention',
    '평문 실패(크래시 fail-safe)는 C여도 절대 안 숨김 — 쓰기 여부 불명',got)
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",
          (json.dumps({'action':'approve','status':'stale','applied_seqs':['0001'],'reconcile_required':False}),SX3))
chk(c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']['needs_review'] is True,
    'stale이어도 applied_seqs 있으면 노출')
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",
          (json.dumps({'action':'approve','status':'failed','reconcile_required':True}),SX3))
got=c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']
chk(got['reconcile_required'] is True and got['needs_review'] is True,'C여도 부분성공(reconcile)은 계속 노출',got)
r=c.post('/api/ext/soa/reviews/snapshot',json=gsnap('C',[gline('0001'),gline('0002')]),headers=H)
got=c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']
chk(r.status_code==200 and got['reconcile_required'] is True and got['needs_review'] is True,
    'reconcile 기록은 이후 snapshot ingest에도 살아남음',got)
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",(STALE_PRE,SX3))
r=c.post('/api/ext/soa/reviews/snapshot',json=gsnap('C',[gline('0001'),gline('0002',cfm='N')]),headers=H)
A.execute("UPDATE soa_review_case SET last_action_result=? WHERE sx_cd=?",(STALE_PRE,SX3))
got=c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']
chk(r.status_code==200 and got['all_confirmed'] is False and got['needs_review'] is True,
    'C+pre-write 실패인데 미검증 라인 남으면 계속 노출',got)
chk(c.post('/api/ext/soa/reviews/snapshot',json=gsnap('C',[gline('0001'),gline('0002')]),headers=H).status_code==200
    and c.get('/api/automation/soa/reviews/'+SX3).get_json()['case']['last_action_result'] is None,
    'reconcile 아닌 낡은 실패기록은 snapshot ingest가 정리함')

# ── pre-write 판정은 형식이 조금이라도 어긋나면 fail-closed ─────────────────
_PW=A._soa_review_action_pre_write
chk(_PW('{"action":"approve","status":"stale","applied_seqs":[]}'),'정상 pre-write 결과만 True')
chk(not _PW('{"status":"stale","applied_seqs":[]}'),'action 누락이면 pre-write 아님')
chk(not _PW('{"action":"push","status":"stale","applied_seqs":[]}'),'다른 action이면 pre-write 아님')
chk(not _PW('{"action":"approve","status":"stale","applied_seqs":"0001"}'),
    'applied_seqs 형식이 다르면 fail-closed')
chk(not _PW('{"action":"approve","status":"stale","applied_seqs":{"0001":1}}'),
    'applied_seqs dict도 fail-closed')
chk(not _PW('{"action":"approve","status":"stale","applied_seqs":[],"reconcile_required":true}'),
    'reconcile면 pre-write 아님')
chk(not _PW('failed: timeout'),'평문 실패는 절대 pre-write 아님')

# ── 기존 prod DB(좁은 CHECK) 자동 마이그레이션 ─────────────────────────────
import sqlite3 as _sq
OLD_DB = tempfile.mktemp(suffix='.db')
_old_schema = open(A.SCHEMA_FILE, encoding='utf-8').read().replace(
    "CHECK (status GLOB '[A-Z]' OR status GLOB '[A-Z][A-Z]')", "CHECK (status IN ('C','T','D','S'))")
chk("IN ('C','T','D','S')" in _old_schema, '구 스키마 fixture 생성')
_con=_sq.connect(OLD_DB); _con.executescript(_old_schema)
_cid=_con.execute("INSERT INTO soa_review_case (sx_cd,status,draft_version,last_action_result) "
                  "VALUES ('MIGRCX2600000001','S',3,'keep-me')").lastrowid
_con.execute("INSERT INTO soa_review_line (case_id,sx_seq,line_no,source_hash) VALUES (?,'0001',0,'h1')",(_cid,))
_con.execute("INSERT INTO soa_review_audit (case_id,action) VALUES (?,'seed')",(_cid,))
_con.execute("CREATE TRIGGER trg_migr_probe AFTER UPDATE OF status ON soa_review_case "
             "BEGIN INSERT INTO soa_review_audit (case_id,action) VALUES (NEW.id,'trg'); END")
_con.execute("CREATE INDEX ix_migr_partial ON soa_review_case (sx_cd) WHERE draft_version>0")
# 마이그레이션 중간 실패 유도: 이름이 겹치는 view는 DROP TABLE로 안 지워져 CREATE 단계에서 죽는다.
_con.execute("CREATE VIEW soa_review_case__new AS SELECT 1 AS x")
_con.commit(); _con.close()
_saved_db=A.DATABASE
try:
    A.DATABASE=OLD_DB; A._auto_migrate()          # 실패 경로
finally:
    A.DATABASE=_saved_db
_con=_sq.connect(OLD_DB)
chk(_con.execute("SELECT status,last_action_result FROM soa_review_case").fetchone()==('S','keep-me')
    and _con.execute('SELECT COUNT(*) FROM soa_review_line').fetchone()[0]==1
    and "IN ('C','T','D','S')" in _con.execute(
        "SELECT sql FROM sqlite_master WHERE name='soa_review_case'").fetchone()[0],
    '마이그레이션 중간 실패 시 원본 테이블·자식행 그대로 롤백')
chk(A.SOA_REVIEW_SCHEMA_DEGRADED is True,'마이그레이션 실패하면 degraded 플래그로 드러남')
_con.execute('DROP VIEW soa_review_case__new'); _con.commit(); _con.close()
_saved_db=A.DATABASE
try:
    A.DATABASE=OLD_DB; A._auto_migrate(); A._auto_migrate()   # idempotent
finally:
    A.DATABASE=_saved_db
chk(A.SOA_REVIEW_SCHEMA_DEGRADED is False,'마이그레이션 성공하면 degraded 해제')
_con=_sq.connect(OLD_DB); _con.execute('PRAGMA foreign_keys=ON')
chk(_con.execute("SELECT status,draft_version,last_action_result FROM soa_review_case "
                 "WHERE sx_cd='MIGRCX2600000001'").fetchone()==('S',3,'keep-me'),'마이그레이션 후 기존 case 보존')
chk(_con.execute('SELECT COUNT(*) FROM soa_review_line').fetchone()[0]==1
    and _con.execute('SELECT COUNT(*) FROM soa_review_audit').fetchone()[0]==1
    and not _con.execute('PRAGMA foreign_key_check').fetchall(),'자식 행 보존 · FK 무결')
_ixs={r[1] for r in _con.execute('PRAGMA index_list(soa_review_case)').fetchall()}
chk(len(_ixs)>=4 and 'ix_migr_partial' in _ixs,'인덱스 재생성(partial index 포함)')
chk(_con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='trg_migr_probe'")
    .fetchone()[0]==1,'트리거 DDL 보존')
chk(not _con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='soa_review_case__new' "
                     "AND type='table'").fetchone()[0],'임시 테이블 잔재 없음')
_con.execute("UPDATE soa_review_case SET status='R'"); _con.commit()
chk(_con.execute('SELECT status FROM soa_review_case').fetchone()[0]=='R','마이그레이션 후 R 저장 가능')
chk(_con.execute("SELECT COUNT(*) FROM soa_review_audit WHERE action='trg'").fetchone()[0]==1,
    '보존된 트리거가 실제로 동작')
try:
    _con.execute("UPDATE soa_review_case SET status='oops'"); _con.commit(); _bad=False
except Exception: _bad=True; _con.rollback()
chk(_bad,'형식 불량 status는 DB CHECK가 계속 거부')
_con.close()
try: os.unlink(OLD_DB)
except OSError: pass

try: os.unlink(DB)
except OSError: pass
if fails:
    print('FAIL',fails); raise SystemExit(1)
print('PASS: SOA review backend')
