#!/usr/bin/env python3
"""수리신청서 신청 목록 **삭제 버튼**을 잠그는 테스트.

형 지시(2026-08-24): "trmt 웹 / IOS 앱에 '수리신청서' 탭에 신청목록 리스트 삭제가 가능하게
버튼만들어줘". 초안 삭제는 웹·iOS 둘 다 이미 있었고, 실제로 막혀 있던 것은 **SVMS 저장본**
(REP_CD 있는 건)이다 — 형 화면의 유일한 행이 그것이라 버튼이 하나도 안 보였다.

잠그는 것:
  ① 서버 계층 — 초안(pending/failed/approved)은 그대로, 저장 진행 중(saving)은 거부,
     SVMS 저장본은 확인 문구를 받고 삭제, REP_CD 없는 이상행은 fail-closed 거부.
  ② 🔴 견적·업체선정·발주 데이터가 붙은 건은 초안이든 저장본이든 거부(돈 경로).
     `stg_quote` 는 blocker 가 아니다 — 저장 성공 시 서버가 항상 1 로 올리므로 사람이 한 일이
     아니고, 넣으면 저장된 건 전부가 삭제 불가가 된다.
  ③ 🔴 TOCTOU — pre-check 와 DELETE 사이에 러너가 물어 REP_CD 가 생기면 조용히 지우지 않는다.
  ④ 화면 배선 — 웹은 규칙 파일을 실제로 불러 버튼 노출·확인·문구를 그것으로 정한다.
  ⑤ 웹 ↔ iOS 문구·확인 토큰 파리티(한쪽만 고치면 같은 삭제가 기기마다 다르게 물어본다).

실행: ~/.venvs/trmt-test/bin/python tests/test_repair_delete_button.py
"""
import os
import re
import sys
import tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A  # noqa: E402
import routes_repair_request as RR  # noqa: E402

A.DATABASE = DB
A.app.config['DATABASE'] = DB
A.app.config.update(TESTING=True, SECRET_KEY='test')
with A.app.app_context():
    A.init_db(drop=False)
    A.execute("INSERT INTO vessels(name,vsl_cd,active) VALUES('TEST VESSEL','TSTV',1)")
    A.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('automation_enabled','1')")

c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'

fails = []
_n = [0]


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


def make(**over):
    """신청서 하나 만들고 필요하면 상태를 직접 밀어 넣는다."""
    _n[0] += 1
    body = dict(client_request_id=f'del-{_n[0]}', vessel_id=1, subject='M/E repair',
                category='M/E', equipment='MAIN ENGINE', maker='', type_nm='', app_voy='001E',
                app_port_cd='KRPUS', app_dt='2026-08-14', cause='c', inspection='i',
                detail='d', stock='vendor', reason_cd='P', dept_cd='E',
                dock_yn=False, urgent_yn=False, critical_yn=False)
    r = c.post('/api/repair-requests', json=body)
    assert r.status_code == 201, r.get_data(as_text=True)
    rid = r.get_json()['id']
    if over:
        sets = ','.join(f'{k}=?' for k in over)
        with A.app.app_context():
            A.execute(f'UPDATE repair_request SET {sets} WHERE id=?', (*over.values(), rid))
    return rid


def dock_rid(rid):
    with A.app.app_context():
        return A.query('SELECT dock_rid FROM repair_request WHERE id=?', (rid,), one=True)['dock_rid']


def alive(rid):
    with A.app.app_context():
        return bool(A.query('SELECT id FROM repair_request WHERE id=?', (rid,), one=True))


# ① 초안 — 옛 계약 그대로(body 없이 200)
rid = make()
drid = dock_rid(rid)
r = c.delete(f'/api/repair-requests/{rid}')
chk(r.status_code == 200, '초안은 확인 문구 없이 삭제된다', r.get_data(as_text=True))
chk(not alive(rid), '초안 행이 실제로 사라진다')
with A.app.app_context():
    chk(not A.query('SELECT id FROM dock_procure WHERE id=?', (drid,), one=True),
        '연결된 dock_procure shim 도 같이 사라진다')
    # 🔴 선박 엔트리는 진짜 입거선박과 공용 키다. 같이 지우면 Dock 발주현황에서 배가 통째로 사라진다.
    chk(bool(A.query('SELECT vsl_nm FROM dock_procure_vessel WHERE vsl_nm=?',
                     ('TEST VESSEL',), one=True)),
        'dock_procure_vessel 은 남는다(진짜 입거선박과 공용 키)')

# ① 저장 진행 중(saving) — 거부. 러너가 물고 있어 SVMS 생성 여부를 모른다.
rid = make(status='saving')
r = c.delete(f'/api/repair-requests/{rid}')
chk(r.status_code == 409, 'saving 은 삭제 거부(409)', r.get_data(as_text=True))
chk(alive(rid), 'saving 행이 살아 있다')
# 확인 문구를 붙여도 뚫리지 않아야 한다 — 러너가 물고 있는 구간이라 사람 확인의 문제가 아니다.
r = c.delete(f'/api/repair-requests/{rid}', json={'confirmation': RR._DELETE_CONFIRM})
chk(r.status_code == 409 and alive(rid),
    'saving 은 확인 문구로도 뚫리지 않는다', r.get_data(as_text=True))

# ① 저장 대기(approved) — 삭제 가능.
#    🔴 러너는 claim(status→saving) 뒤에만 SVMS 에 쓴다(`?peek=1` 은 DRY 전용). 즉 approved 는
#       아직 SVMS 전이고, 삭제 직전에 물렸다면 WHERE 가드가 409 로 막는다. 막아만 두면 러너가
#       죽은 사이 고인 행은 편집(_EDITABLE 아님)·삭제·resolve(saving/failed 만) 전부 불가가 된다.
rid = make(status='approved')
r = c.delete(f'/api/repair-requests/{rid}')
chk(r.status_code == 200, 'approved 는 삭제된다(영구 stuck 방지)', r.get_data(as_text=True))
chk(not alive(rid), 'approved 행이 사라진다')

# ① fail-closed — REP_CD 없는데 초안도 아닌 이상행은 확인 문구가 있어도 거부.
#    (DB CHECK 가 status 를 pending/approved/saving/saved/failed 로 묶으므로 도달 가능한
#     이상행은 'REP_CD 없는 saved' 뿐이다. 낯선 status 방어는 클라이언트 규칙 테스트가 잠근다.)
rid = make(status='saved')
r = c.delete(f'/api/repair-requests/{rid}', json={'confirmation': RR._DELETE_CONFIRM})
chk(r.status_code == 409 and alive(rid),
    'REP_CD 없는 saved 는 거부(fail-closed)', r.get_data(as_text=True))

# ① SVMS 저장본 — 확인 문구 필요
rid = make(status='saved', rep_cd='TSTREP001')
r = c.delete(f'/api/repair-requests/{rid}')
chk(r.status_code == 400, 'SVMS 저장본은 확인 문구 없이 거부(400)', r.get_data(as_text=True))
chk(r.get_json().get('need_confirmation') == RR._DELETE_CONFIRM,
    '거부 응답이 필요한 확인 문구를 알려준다', r.get_json())
chk('SVMS 문서는 삭제되지 않' in r.get_json().get('error', ''),
    '🔴 거부 문구가 SVMS 원본은 안 지워진다는 사실을 말한다', r.get_json().get('error'))
r = c.delete(f'/api/repair-requests/{rid}', json={'confirmation': '아무거나'})
chk(r.status_code == 400 and alive(rid), '틀린 확인 문구는 거부된다')
r = c.delete(f'/api/repair-requests/{rid}', json={'confirmation': RR._DELETE_CONFIRM})
chk(r.status_code == 200, 'SVMS 저장본은 확인 문구가 있으면 삭제된다', r.get_data(as_text=True))
chk(not alive(rid), 'SVMS 저장본 행이 실제로 사라진다')
chk(r.get_json().get('svms_kept') is True and r.get_json().get('rep_cd') == 'TSTREP001',
    '응답이 "SVMS 문서는 남았다" 를 명시한다(형이 SVMS 정리를 따로 해야 한다)', r.get_json())

# ② 돈 경로 blocker — 확인 문구가 있어도 거부
for col, val in (('stg_vendor', 1), ('stg_confirm', 1), ('stg_order', 1),
                 ('sub_quotes', '[{"cd":"V1"}]'), ('att_files', '[{"nm":"q.pdf"}]'),
                 ('ord_vendors', '[{"odr_no":"O1"}]'), ('quote_amt', 1200.0)):
    rid = make(status='saved', rep_cd=f'TSTBLK{col}')
    with A.app.app_context():
        A.execute(f'UPDATE dock_procure SET {col}=? WHERE id=?', (val, dock_rid(rid)))
    r = c.delete(f'/api/repair-requests/{rid}', json={'confirmation': RR._DELETE_CONFIRM})
    chk(r.status_code == 409 and alive(rid), f'{col} 붙은 건은 거부(돈 경로)', r.get_data(as_text=True))
    chk(bool(r.get_json().get('blockers')), f'{col} 거부 응답이 사유를 돌려준다', r.get_json())

# 🔴 초안에도 같은 blocker 를 적용한다 — REP_CD 있을 때만 검사하면 저장 실패(failed)로 남은
#    건에 붙은 견적·발주가 조용히 사라진다.
for status in ('pending', 'failed'):
    rid = make(status=status)
    with A.app.app_context():
        A.execute('UPDATE dock_procure SET stg_vendor=1 WHERE id=?', (dock_rid(rid),))
    r = c.delete(f'/api/repair-requests/{rid}')
    chk(r.status_code == 409 and alive(rid),
        f'{status} 초안도 돈 경로가 붙으면 거부', r.get_data(as_text=True))

# 🔴 stg_quote 는 blocker 가 아니다 — 저장 성공 시 서버가 항상 1 로 올린다.
rid = make(status='saved', rep_cd='TSTQUOTE1')
with A.app.app_context():
    A.execute('UPDATE dock_procure SET stg_quote=1 WHERE id=?', (dock_rid(rid),))
r = c.delete(f'/api/repair-requests/{rid}', json={'confirmation': RR._DELETE_CONFIRM})
chk(r.status_code == 200, 'stg_quote=1 은 삭제를 막지 않는다(사람이 한 일이 아니다)',
    r.get_data(as_text=True))

# 빈 문자열/빈 JSON 배열은 "붙어 있다" 가 아니다 — 웹 규칙(빈 REP_CD=미저장)과 같은 결.
rid = make(status='saved', rep_cd='TSTEMPTY1')
with A.app.app_context():
    A.execute("UPDATE dock_procure SET sub_quotes='',att_files='[]' WHERE id=?", (dock_rid(rid),))
r = c.delete(f'/api/repair-requests/{rid}', json={'confirmation': RR._DELETE_CONFIRM})
chk(r.status_code == 200, '빈 값·빈 배열은 blocker 가 아니다', r.get_data(as_text=True))

# ③ TOCTOU — 판정에 쓴 status·rep_cd 가 실제 행과 다르면 조용히 지우지 않는다.
#    negative control: DELETE 의 WHERE 가드가 없으면 이 테스트는 "지워짐" 으로 실패한다.
#    (판정 SELECT 를 삭제와 같은 트랜잭션에 넣었으므로, 가드는 그 위의 이중 잠금이다.)
rid = make()  # 실제 행은 pending / rep_cd NULL


class _RacingCur:
    """러너가 물어 SVMS 에 저장된 것처럼 보이는 행을 돌려준다."""

    def __init__(self, drid):
        self._row = {'status': 'saved', 'rep_cd': 'TSTRACE1', 'dock_rid': drid}

    def fetchone(self):
        return self._row


class _RacingConn:
    def __init__(self, real, drid):
        self._real, self._drid = real, drid

    def execute(self, sql, *a, **kw):
        if 'SELECT status,rep_cd,dock_rid FROM repair_request' in sql:
            return _RacingCur(self._drid)
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


orig_get_db = RR.get_db
_drid = dock_rid(rid)
RR.get_db = lambda: _RacingConn(orig_get_db(), _drid)
try:
    r = c.delete(f'/api/repair-requests/{rid}', json={'confirmation': RR._DELETE_CONFIRM})
finally:
    RR.get_db = orig_get_db
chk(r.status_code == 409, '판정값과 실제 행이 다르면 409(조용한 삭제 금지)', r.get_data(as_text=True))
chk(alive(rid), '🔴 러너가 만든 REP_CD 를 가진 행이 살아 있다', )
with A.app.app_context():
    chk(bool(A.query('SELECT id FROM dock_procure WHERE id=?', (dock_rid(rid),), one=True)),
        'dock_procure 도 롤백된다(부분삭제 0)')

r = c.delete('/api/repair-requests/999999')
chk(r.status_code == 404, '없는 건은 404')

# ④ 웹 배선
tpl = open('templates/repair_requests.html', encoding='utf-8').read()
chk('js/repair_delete_rule.js' in tpl, '웹이 규칙 파일을 실제로 불러온다')
chk('RepairDeleteRule.deleteRule(r).visible' in tpl, '버튼 노출을 규칙이 정한다')
chk('for(const p of rule.prompts)' in tpl, '확인 문구를 규칙에서 순서대로 물어본다')
chk('rule.token?{confirmation:rule.token}:undefined' in tpl,
    '확인 문구는 규칙의 token 을 그대로 보낸다(초안은 body 없음)')
# 🔴 옛 배선: 삭제 버튼이 편집가능 블록 안에 있어 SVMS 저장본에는 아예 안 그려졌다.
edit_block = re.search(r"\['pending','failed'\]\.includes\(r\.status\)&&!r\.rep_cd\?`(.*?)`:''", tpl)
chk(bool(edit_block) and 'del(' not in edit_block.group(1),
    '삭제 버튼이 편집가능 블록 밖으로 나왔다(저장본에도 그려진다)',
    edit_block.group(1) if edit_block else 'block not found')
chk('TRMT에서만삭제' not in tpl and 'approved' not in tpl.split('<script')[-1].split('deleteRule')[0],
    'template 에 확인 토큰·상태 목록 사본이 없다')
chk('const gen=++loadGen' in tpl,
    '목록 로드에 세대 가드가 있다(삭제 직후 옛 응답이 지운 행을 되살리면 안 된다)')

# ⑤ 웹 ↔ iOS 파리티
js = open('static/js/repair_delete_rule.js', encoding='utf-8').read()
chk(f"CONFIRM_TOKEN = '{RR._DELETE_CONFIRM}'" in js, '웹 토큰 = 서버 토큰', RR._DELETE_CONFIRM)


def js_list(name, text):
    return re.findall(r"'([\w]*)'", re.search(rf'var {name} = \[(.*?)\];', text).group(1))


chk(list(RR._DELETE_INFLIGHT) == js_list('INFLIGHT', js), '웹 in-flight 목록 = 서버 목록',
    js_list('INFLIGHT', js))
chk(list(RR._DELETE_DRAFTISH) == js_list('DRAFTISH', js), '웹 초안 whitelist = 서버 whitelist',
    js_list('DRAFTISH', js))


def prompt_strings(body, kind):
    """확인 문구 리터럴만 뽑아 비교한다. REP_CD 보간 표기는 언어마다 달라 지운다.

    🔴 주석 줄은 먼저 버린다 — 주석 안 인용부호("지워도 SVMS 에 남는다" 같은 설명)가 섞이면
       문구가 같은데도 파리티가 깨졌다고 거짓 실패한다.
    """
    body = '\n'.join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith(('//', '*', '/*')))
    if kind == 'js':
        body = body.replace("' + repCd + '", '')
        lits = re.findall(r"'((?:[^'\\]|\\.)*)'", body)
    else:
        body = body.replace('\\(rep)', '')
        lits = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
    out = []
    for s in lits:
        s = s.replace('\\n', ' ').strip()
        if re.search(r'[가-힣]', s):
            out.append(re.sub(r'\s+', ' ', s))
    return out


ios = os.path.expanduser('~/.openclaw/workspace/trmt-mobile/ios/TRMT/Sources/'
                         'Models/RepairRequest.swift')
if not os.path.exists(ios):
    print('  ⚠️ SKIP — iOS 소스 경로 없음(%s). 웹·서버 계약만 검사함.' % ios)
else:
    swift = open(ios, encoding='utf-8').read()
    chk(f'confirmToken = "{RR._DELETE_CONFIRM}"' in swift, 'iOS 토큰 = 서버 토큰')

    def swift_list(name):
        return re.findall(r'"([\w]*)"', re.search(rf'static let {name} = \[(.*?)\]',
                                                  swift).group(1))

    chk(list(RR._DELETE_INFLIGHT) == swift_list('inFlight'), 'iOS in-flight 목록 = 서버 목록',
        swift_list('inFlight'))
    chk(list(RR._DELETE_DRAFTISH) == swift_list('draftish'), 'iOS 초안 whitelist = 서버 whitelist',
        swift_list('draftish'))
    # 🔴 빈 문자열 REP_CD 를 저장본으로 보면 iOS 만 "REP_CD ()" 를 물어본다.
    chk('!rep.isEmpty' in swift, 'iOS 도 빈 REP_CD 를 미저장으로 본다')
    web_p = prompt_strings(js.split('function deleteRule')[1], 'js')
    ios_p = prompt_strings(swift.split('var deleteRule')[1].split('struct RepairDeleteRule')[0],
                           'swift')
    chk(web_p == ios_p and len(web_p) >= 4, '웹↔iOS 확인 문구 동일', f'web={web_p} ios={ios_p}')

print()
print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과')
sys.exit(1 if fails else 0)
