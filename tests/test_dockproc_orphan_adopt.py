#!/usr/bin/env python3
"""Dock — **미적재(orphan) 청구 수집 → 배너 → [적재]** 경로를 잠그는 테스트.

배경(2026-08-05 형 실사고, `[BGBB S33]`): 발주현황 행은 종전에 INDEX 엑셀 업로드로만 생겼고
역동기화(`/api/ext/dock_procure/sync`)는 update-only 였다 → 엑셀에 없는 시트번호로 청구가
나가면 붙을 행이 없어 `unmatched` 카운터로만 세고 조용히 버려졌다. 형 화면에는 흔적이 0.
실측 결과 그렇게 사라진 실청구가 7건(BGBB S2/S18/S21~S24 + SAPS R27 = **발주완료**)이었다.

설계상 **자동생성은 하지 않는다**: 라인 삭제가 하드 DELETE 라 자동적재는 사람이 지운 행을 다음
sync 가 부활시키고, 태그 번호가 입거마다 재사용되므로 옛 입거 잔상까지 끌어올 수 있다.
그래서 sync 는 목록만 남기고(`api_settings.dockproc_orphans`) 사람이 배너에서 [적재] 를 누른다.

잠그는 것:
  ① sync 가 태그는 맞는데 행이 없는 청구를 orphan 으로 모은다. `dry=true` 는 **저장하지 않는다**.
  ② 저장은 **이번 payload 가 다룬 선박만** 갈아친다(남의 선박 배너를 지우지 않는다).
  ③ `_dockproc_orphans_of` 는 이미 행이 생긴 번호를 즉시 뺀다(다음 sync 를 안 기다림).
  ④ `POST /api/dock_procure/adopt` 는 **폴러가 남긴 목록 안의 것만** 적재한다(임의 행 생성 경로 금지).
     201 신규 / 200 멱등 / 409 목록 밖 / 404 미등록 선박 / 400 인자누락 / 로그인 필수.
  ⑤ 🔴 `_dockproc_adopt_svms` 는 **항상 3-tuple** 을 반환한다. 2-tuple 로 새면 `ValueError` 가
     `api_ext_reqgen_result` 의 트랜잭션을 rollback 시켜 **SVMS 엔 저장됐는데 카드는 `saving` 에
     갇히고 6h 뒤 failed** 가 된다(형이 실패로 봄). 도달 경로 = P·SY 시트 등 R/S/ST 아닌 시트번호.
  ⑥ 구매/수리 키 분리와 `key_state` 계약: 기존 키가 다르면 **덮지 않고** `conflict` 로 드러낸다.
  ⑦ 웹 화면 계약 — 배너 컨테이너·CSS·렌더·[적재] 배선이 다 있어야 한다(하나만 빠지면 형 화면엔
     여전히 아무것도 안 뜨고, 서버만 조용히 모으는 상태가 된다 = 이번 사고의 절반).

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_orphan_adopt.py
  ⚠️ `/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import json
import os
import sys
import tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
from source_bundle import shared_ns
A.DATABASE = DB
A.app.config['DATABASE'] = DB
A.app.config['TESTING'] = True
A.init_db(drop=False)
A._auto_migrate()

fails = []


def chk(cond, name, extra=''):
    print(('  ok  ' if cond else '  ❌  ') + name + (f' — {extra}' if extra and not cond else ''))
    if not cond:
        fails.append(name)


A.app.app_context().push()
c = A.app.test_client()
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

KEY = 'testkey-dockproc-orphan'
shared_ns._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}

A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('BELGIUM B','BGBB')")
A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('SAPPHIRE','SAPS')")

SUBJ_S18 = '[DOCK][BGBB S18]M/T BELGIUM B - MAIN ENGINE SPARE PARTS'
SUBJ_R27 = '[DOCK][SAPS R27]M/T SAPPHIRE - TURBOCHARGER OVERHAUL'


def item(vsl_cd, subj, status='VSL Approved', doc='PC', inq=None, alt=None):
    return {'vsl_cd': vsl_cd, 'subject': subj, 'status': status, 'doc': doc,
            'inq_no': inq, 'inq_alt': alt}


def orphans_raw():
    r = A.query("SELECT v FROM api_settings WHERE k='dockproc_orphans'", one=True)
    return json.loads(r['v']) if (r and r['v']) else {}


print('# 1) sync 가 미적재 청구를 모은다 — dry 는 저장하지 않는다')
pay = {'items': [item('BGBB', SUBJ_S18, doc='PC', inq='BGBBES2607B8X', alt='BGBBES2607B8')]}
j = c.post('/api/ext/dock_procure/sync', headers=HDR, json=dict(pay, dry=True)).get_json()
chk(j.get('orphans_n') == 1, 'dry 응답에도 orphans_n=1', j)
chk(orphans_raw() == {}, 'dry 는 api_settings 에 쓰지 않는다', orphans_raw())
j = c.post('/api/ext/dock_procure/sync', headers=HDR, json=pay).get_json()
chk(j.get('orphans_n') == 1 and (j.get('orphans') or [{}])[0].get('req_no') == 'S18',
    '실행 응답 orphans=[S18]', j.get('orphans'))
st = orphans_raw()
chk(list(st.keys()) == ['BGBB'] and len(st['BGBB']) == 1, '선박별로 저장된다', st)
o = st['BGBB'][0]
chk(o.get('key') == 'BGBBES2607B8', '구매(PC)는 REQ_NO(inq_alt)를 적재키로 남긴다', o)
chk(o.get('subject') == SUBJ_S18, '제목 원문 보존(적재 시 서버가 역파싱)', o)

print()
print('# 2) 다른 선박 payload 는 남의 배너를 지우지 않는다')
c.post('/api/ext/dock_procure/sync', headers=HDR,
       json={'items': [item('SAPS', SUBJ_R27, status='HQ Ordered', doc='MA', inq='SAPSME26062906')]})
st = orphans_raw()
chk(set(st.keys()) == {'BGBB', 'SAPS'}, 'BGBB 목록이 남아 있다', list(st.keys()))
chk((st.get('SAPS') or [{}])[0].get('key') == 'SAPSME26062906', '수리(MA)는 REP_CD(inq_no)를 남긴다', st.get('SAPS'))
#   같은 선박을 0건으로 다시 돌리면 그 선박 키는 사라진다(해소된 배너가 남지 않는다).
c.post('/api/ext/dock_procure/sync', headers=HDR,
       json={'items': [item('BGBB', 'no tag here at all', status='VSL Approved')]})
chk('BGBB' not in orphans_raw(), '같은 선박 0건 재동기화 → 배너 소멸', list(orphans_raw().keys()))
c.post('/api/ext/dock_procure/sync', headers=HDR, json=pay)     # 다시 채워두고 계속

print()
print('# 3) 화면용 목록은 이미 행이 생긴 번호를 즉시 뺀다')
chk([x['req_no'] for x in shared_ns._dockproc_orphans_of('BGBB')] == ['S18'], '행 없으면 나온다')
chk(shared_ns._dockproc_orphans_of('BGBB', ['s18']) == [], '행 있으면 대소문자 무관 빠진다')
chk(shared_ns._dockproc_orphans_of('') == [], '선박코드 없으면 빈 목록')
j = c.get('/api/dock_procure/lines?vsl_nm=BELGIUM+B').get_json()
chk([x['req_no'] for x in (j.get('orphans') or [])] == ['S18'], 'lines 응답이 배너 데이터를 내려준다', j.get('orphans'))

print()
print('# 4) adopt — 목록 안의 것만, 멱등하게')
chk(c.post('/api/dock_procure/adopt', json={'req_no': 'S18'}).status_code == 400, '인자 누락 400')
chk(c.post('/api/dock_procure/adopt', json={'vsl_cd': 'BGBB', 'req_no': 'S99'}).status_code == 409,
    '목록 밖 번호는 409 (화면에서 임의 행 생성 금지)')
r = c.post('/api/dock_procure/adopt', json={'vsl_cd': 'BGBB', 'req_no': 's18'})
j = r.get_json()
chk(r.status_code == 201 and j.get('created') is True, '신규 적재 201', (r.status_code, j))
row = A.query("SELECT * FROM dock_procure WHERE vsl_nm='BELGIUM B' AND req_no='S18'", one=True)
chk(row is not None, '행이 실제로 생겼다')
chk(row and row['cat_code'] == 'S' and row['prepared_by'] == 'OWNER' and row['source'] == 'SVMS',
    '분류·담당·출처가 규칙대로', dict(row) if row else None)
chk(row and row['subject'] == 'MAIN ENGINE SPARE PARTS', '제목은 태그·선명 뗀 본문', row and row['subject'])
chk(row and (row['svms_pc_req_no'] or '') == 'BGBBES2607B8' and not (row['svms_req_no'] or ''),
    '🔴 구매키는 svms_pc_req_no 에만 (섞으면 Phase ③ 상신이 깨진다)',
    row and (row['svms_req_no'], row['svms_pc_req_no']))
chk(row and row['remark'] == 'SVMS 청구 자동적재', '출처가 remark 에 남는다', row and row['remark'])
r2 = c.post('/api/dock_procure/adopt', json={'vsl_cd': 'BGBB', 'req_no': 'S18'})
j2 = r2.get_json()
chk(r2.status_code == 200 and j2.get('created') is False and j2.get('id') == j.get('id'),
    '두 번째는 200·created=False·같은 id (멱등)', (r2.status_code, j2))
chk(A.query("SELECT COUNT(*) n FROM dock_procure WHERE vsl_nm='BELGIUM B' AND req_no='S18'",
            one=True)['n'] == 1, '중복 행이 생기지 않는다')
chk(c.post('/api/dock_procure/adopt', json={'vsl_cd': 'SAPS', 'req_no': 'R27'}).status_code == 201,
    '수리(MA) 발주완료 건도 적재된다 (SAPS R27 실사례)')
chk((A.query("SELECT svms_req_no, svms_pc_req_no FROM dock_procure WHERE vsl_nm='SAPPHIRE' "
             "AND req_no='R27'", one=True)['svms_req_no'] or '') == 'SAPSME26062906',
    '🔴 수리키는 svms_req_no 에만')
#   선박코드는 배너에 있지만 입거선박 목록엔 없는 경우 — 404(행을 만들지 않는다).
A.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('dockproc_orphans',?)",
          (json.dumps({'ZZZZ': [{'vsl_cd': 'ZZZZ', 'req_no': 'S1', 'subject': '[DOCK][ZZZZ S1]X - Y',
                                 'status': 'VSL Approved', 'doc': 'PC', 'key': 'K1'}]},
                      ensure_ascii=False),))
chk(c.post('/api/dock_procure/adopt', json={'vsl_cd': 'ZZZZ', 'req_no': 'S1'}).status_code == 404,
    '입거선박 목록에 없는 선박은 404')
#   🔴 위 replace 로 BGBB 목록은 비었다 = sync 가 해소된 항목을 뺀 상태와 같다. 이때도 **행이 이미
#     있으면** 성공(200)이어야 한다 — 409 를 주면 두 탭·더블클릭에서 형이 '실패'로 읽는다.
r3 = c.post('/api/dock_procure/adopt', json={'vsl_cd': 'BGBB', 'req_no': 'S18'})
chk(r3.status_code == 200 and (r3.get_json() or {}).get('created') is False,
    '목록에서 빠졌지만 행이 이미 있으면 200 (409 아님)', (r3.status_code, r3.get_json()))
print('  -- 입력 계약(타입 위반은 500 이 아니라 400)')
for bad in ([1, 2], 'plain-string', {'vsl_cd': {'a': 1}, 'req_no': 'S18'},
            {'vsl_cd': 'BGBB', 'req_no': 5}, {'vsl_cd': ['BGBB'], 'req_no': ['S18']}):
    sc = c.post('/api/dock_procure/adopt', json=bad).status_code
    chk(sc == 400, f'{str(bad)[:34]} → 400', sc)
anon = A.app.test_client()
chk(anon.post('/api/dock_procure/adopt', json={'vsl_cd': 'BGBB', 'req_no': 'S18'}).status_code
    in (302, 401, 403), '로그인 없으면 막힌다(@login_required)')

print()
print('# 5) 🔴 R/S/ST 아닌 시트는 3-tuple 로 조용히 거절한다 (500·ValueError 금지)')
#   `_dockproc_adopt_svms` 가 2-tuple 로 나가면 reqgen 결과 트랜잭션이 rollback 되어
#   SVMS 엔 저장됐는데 카드는 saving→(6h)→failed 로 죽는다. 형이 "저장 실패"로 보게 되는 경로.
got = shared_ns._dockproc_adopt_svms('BELGIUM B', 'BGBB', 'P3', 'PAINT', None, 'PC', 'K9')
chk(isinstance(got, tuple) and len(got) == 3 and got == (None, False, 'none'),
    '반환은 항상 3-tuple, 비대상은 (None, False, "none")', got)
A.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('dockproc_orphans',?)",
          (json.dumps({'BGBB': [{'vsl_cd': 'BGBB', 'req_no': 'P3', 'subject': '[DOCK][BGBB P3]X - PAINT',
                                 'status': 'VSL Approved', 'doc': 'PC', 'key': 'K9'}]},
                      ensure_ascii=False),))
r = c.post('/api/dock_procure/adopt', json={'vsl_cd': 'BGBB', 'req_no': 'P3'})
chk(r.status_code == 400, 'adopt 도 500 이 아니라 400 으로 거절', r.status_code)
chk(A.query("SELECT COUNT(*) n FROM dock_procure WHERE req_no='P3'", one=True)['n'] == 0,
    '거절된 건은 행을 만들지 않는다')

print()
print('# 6) reqgen 저장 성공 → 자동적재(같은 트랜잭션) / 비대상은 500 없이 dock=None')


def mkdraft(sheet, vsl_cd, doc_type='PC', subj=None, req_no='BGBBES2608A3'):
    A.execute("INSERT INTO reqgen_draft(batch, doc_type, sheet, vsl_cd, vsl_nm, subj, status, req_no) "
              "VALUES('B1',?,?,?,?,?,'saving',?)",
              (doc_type, sheet, vsl_cd, 'BELGIUM B',
               subj or f'[DOCK][{vsl_cd} {sheet}]M/T BELGIUM B - AUXILIARY BOILER SPARE PARTS', req_no))
    return A.query("SELECT id FROM reqgen_draft ORDER BY id DESC", one=True)['id']


did = mkdraft('S33', 'BGBB')
j = c.post(f'/api/ext/reqgen/drafts/{did}/result', headers=HDR,
           json={'ok': True, 'req_no': 'BGBBES2608A3'}).get_json()
chk(j.get('applied') is True and (j.get('dock') or {}).get('created') is True,
    '저장 성공 시 행이 생긴다(S33 실사고 경로)', j)
chk(A.query("SELECT status FROM reqgen_draft WHERE id=?", (did,), one=True)['status'] == 'saved',
    '카드는 saved 로 확정')
chk((A.query("SELECT svms_pc_req_no FROM dock_procure WHERE req_no='S33'", one=True)
     or {'svms_pc_req_no': None})['svms_pc_req_no'] == 'BGBBES2608A3', '구매키가 채워진다')

did2 = mkdraft('S33', 'BGBB', req_no='BGBBES2608A9')     # 같은 시트 재저장 = 멱등 + 키 충돌
j2 = c.post(f'/api/ext/reqgen/drafts/{did2}/result', headers=HDR,
            json={'ok': True, 'req_no': 'BGBBES2608A9'}).get_json()
chk((j2.get('dock') or {}).get('created') is False, '같은 시트 재저장은 행을 또 만들지 않는다', j2)
chk((j2.get('dock') or {}).get('key_state') == 'conflict', '🔴 다른 키가 오면 conflict 로 드러낸다', j2)
chk(A.query("SELECT svms_pc_req_no FROM dock_procure WHERE req_no='S33'",
            one=True)['svms_pc_req_no'] == 'BGBBES2608A3', '기존 키를 덮지 않는다')

did3 = mkdraft('P7', 'BGBB')                             # R/S/ST 아닌 시트 — 🔴 여기서 500 나면 회귀
r3 = c.post(f'/api/ext/reqgen/drafts/{did3}/result', headers=HDR,
            json={'ok': True, 'req_no': 'BGBBES2608B1'})
chk(r3.status_code == 200 and (r3.get_json() or {}).get('dock') is None,
    '🔴 비대상 시트도 200·dock=None (트랜잭션 rollback 금지)', (r3.status_code, r3.get_json()))
chk(A.query("SELECT status FROM reqgen_draft WHERE id=?", (did3,), one=True)['status'] == 'saved',
    '🔴 카드가 saving 에 갇히지 않는다(6h 뒤 failed 방지)')

did4 = mkdraft('S41', 'ZZZZ')                            # 입거 트래커에 없는 선박
j4 = c.post(f'/api/ext/reqgen/drafts/{did4}/result', headers=HDR,
            json={'ok': True, 'req_no': 'ZZZZES1'}).get_json()
chk(j4.get('applied') is True and j4.get('dock') is None, '입거선박 아니면 적재 대상 아님', j4)
chk(A.query("SELECT COUNT(*) n FROM dock_procure WHERE req_no='S41'", one=True)['n'] == 0,
    '엉뚱한 선박 행을 만들지 않는다')

did5 = mkdraft('S42', 'BGBB')                            # 실패 결과는 적재하지 않는다
j5 = c.post(f'/api/ext/reqgen/drafts/{did5}/result', headers=HDR,
            json={'ok': False, 'result': 'ORA-06502'}).get_json()
chk(j5.get('dock') is None and A.query("SELECT COUNT(*) n FROM dock_procure WHERE req_no='S42'",
                                       one=True)['n'] == 0, '저장 실패건은 적재하지 않는다', j5)

print()
print('# 7) 웹 화면 계약 — 배너가 실제로 그려지고 눌리는가')
tpl = open('templates/dock_procure.html', encoding='utf-8').read()
chk('id="dp-orphans"' in tpl, '배너 컨테이너 존재')
chk('.dp-orphan{' in tpl, '배너 CSS 존재')
chk('function renderOrphans()' in tpl and 'renderOrphans();' in tpl, '렌더 함수가 render() 에서 불린다')
chk("DATA.orphans" in tpl, '서버가 준 orphans 를 쓴다(자체 판정 금지)')
chk("'/api/dock_procure/adopt'" in tpl, '[적재] 가 adopt 엔드포인트를 부른다')
chk('.dp-adopt' in tpl, '[적재] 버튼 배선 존재')
#   🔴 적재 성공 뒤 목록 재조회가 실패해도 '적재 실패' 로 말하면 안 된다 — 성공/재조회를 분리해 둔다.
chk("try{ await load(DATA.current); }catch" in tpl, '적재 성공과 목록 재조회 실패가 분리돼 있다')

print()
print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과')
sys.exit(1 if fails else 0)
