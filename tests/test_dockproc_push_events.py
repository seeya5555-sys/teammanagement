#!/usr/bin/env python3
"""Dock 단계전이 → 푸시 이벤트 판정 + 델타 sync(`partial`) 계약 테스트.

배경(2026-08-06 형 지시 "15분 ㄱ"): 푸시 지연을 60분→15분으로 줄이려고 델타 폴(`dock_sync.py --fast`)을
붙였다. 델타는 **변화 후보 행만** 보내므로 서버가 부분 payload 를 전량으로 착각하면 안 된다.

핵심 계약:
  · `dock_quote`   = 제출수 **분자 증가** 시 1회. 라벨은 그대로일 수 있다(목록으로는 못 잡는 이벤트).
  · `dock_ordered` = `stg_order` 0→1 에서 행당 1회.
  · `dock_reject`  = 반려 라벨 **정확일치** 진입 시(이미 반려면 재발송 안 함).
  · `event_key` 에 **직전 `updated_at`(전이 회차 대용)** 이 들어간다 — 같은 전이가 되풀이돼도
        (제출 3→2→3, 같은 날 2차 반려) 두 번째부터 묻히지 않는다.
  · 🔴 판정 즉시 `push_outbox` 에 **행 UPDATE 보다 먼저** 적재한다 — 상태만 전이되고 알림은 못 간
        구간이 생기면 그 알림은 영구 미탐이 된다(다음 폴엔 "변화 없음" 으로 보이므로).
  · 🔴 `partial=true` 면 **미적재(orphan) 배너를 갱신하지 않는다** — 부분 payload 로 갈아치우면
        대기 중인 배너가 통째로 사라진다.
  · 푸시 미설정(키 없음)이어도 sync 자체는 200 으로 정상 완료된다(알림이 동기화를 막지 않는다).

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_push_events.py
"""
import os, sys, json, tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (clone 위치 무관)
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A

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

KEY = 'testkey-dockproc-push'
A.execute("INSERT OR REPLACE INTO api_settings (k,v) VALUES ('api_key',?)", (KEY,))
H = {'X-API-Key': KEY}


def row_of(rid):
    return A.query("SELECT * FROM dock_procure WHERE id=?", (rid,), one=True)


def mkrow(vsl_nm, vsl_cd, req_no, cat='S', **kw):
    A.execute("INSERT INTO dock_procure (vsl_nm, vsl_cd, req_no, cat_code, subject, source) "
              "VALUES (?,?,?,?,?, 'index')",
              (vsl_nm, vsl_cd, req_no, cat, kw.get('subject') or f'[{vsl_cd} {req_no}] TEST ITEM'))
    rid = A.query("SELECT id FROM dock_procure WHERE vsl_nm=? AND req_no=?",
                  (vsl_nm, req_no), one=True)['id']
    if kw:
        sets, vals = [], []
        for k, v in kw.items():
            if k == 'subject':
                continue
            sets.append(f"{k}=?"); vals.append(v)
        if sets:
            A.execute("UPDATE dock_procure SET " + ",".join(sets) + " WHERE id=?", tuple(vals) + (rid,))
    return rid


print('# 1) submit 파서 — 미지 형식은 None(모르는 값으로 알림 안 만듦)')
chk(A._dockproc_submit_pair('2/5') == (2, 5), '"2/5" → (2,5)')
chk(A._dockproc_submit_pair('(0/4)') == (0, 4), '괄호꼴 "(0/4)" → (0,4)')
chk(A._dockproc_submit_pair(' 3 / 3 ') == (3, 3), '공백 허용')
for bad in (None, '', 'x', '3/', '(0/1', '1/2/3'):
    chk(A._dockproc_submit_pair(bad) is None, f'미지 형식 → None: {bad!r}')

print('\n# 2) dock_quote — 분자 증가에서만, 라벨은 안 봄')
rid = mkrow('TEST VESSEL', 'TSTV', 'S1', svms_submit='1/3', svms_status='Quotation Inquiry')
r = row_of(rid)
ev = A._dockproc_push_events(r, 'Quotation Inquiry', 0, '2/3', None, None, None)
chk([e['kind'] for e in ev] == ['dock_quote'], '1/3 → 2/3 = 견적제출 1건', [e['kind'] for e in ev])
chk(ev[0]['event_key'] == f"dock_quote:{rid}:{r['updated_at']}:2/3",
    'event_key = 행+전이회차+제출수', ev[0]['event_key'])
chk('2/3' in ev[0]['body'] and 'TEST VESSEL' in ev[0]['title'], '본문/제목에 선박·수치', ev[0])
chk(A._dockproc_push_events(r, 'Quotation Inquiry', 0, '1/3', None, None, None) == [],
    '같은 수치는 이벤트 없음')
chk(A._dockproc_push_events(r, 'Quotation Inquiry', 0, '0/3', None, None, None) == [],
    '분자 감소(회수)는 알림 안 함')
# 🔴 올마이트 블로커: 2→1→2 재증가. 키가 수치뿐이면 두 번째 2/3 이 dup 으로 묻혔다.
k1 = ev[0]['event_key']
A.execute("UPDATE dock_procure SET svms_submit='1/3', "
          "updated_at=datetime('now','localtime','+1 second') WHERE id=?", (rid,))
ev_re = A._dockproc_push_events(row_of(rid), 'Quotation Inquiry', 0, '2/3', None, None, None)
chk(len(ev_re) == 1 and ev_re[0]['event_key'] != k1,
    '🔴 감소 후 같은 수치로 재증가 = 새 키로 다시 나감', (k1, ev_re and ev_re[0]['event_key']))
chk(A._dockproc_push_events(r, 'Submit', 0, None, None, None, None) == [],
    '제출수 미전송(상세조회 실패)은 알림 안 함 — 모르면 조용히')
r0 = row_of(mkrow('TEST VESSEL', 'TSTV', 'S2', svms_status='Quotation Inquiry'))
chk([e['kind'] for e in A._dockproc_push_events(r0, 'Quotation Inquiry', 0, '1/2', None, None, None)]
    == ['dock_quote'], '기존값 NULL + 첫 제출 = 알림')

print('\n# 3) dock_ordered — stg_order 0→1 에서만')
r1 = row_of(mkrow('TEST VESSEL', 'TSTV', 'S3', svms_status='Approval(Procssing)'))
ev = A._dockproc_push_events(r1, 'Ordered', 1, None, '에버런스', 12345.6, 'USD')
chk([e['kind'] for e in ev] == ['dock_ordered'], '발주완료 1건', [e['kind'] for e in ev])
chk('에버런스' in ev[0]['body'] and 'USD 12,346' in ev[0]['body'], '업체·금액 표기', ev[0]['body'])
chk(ev[0]['event_key'] == f"dock_ordered:{r1['id']}:{r1['updated_at']}",
    'event_key = 행+전이회차', ev[0]['event_key'])
r2 = row_of(mkrow('TEST VESSEL', 'TSTV', 'S4', stg_order=1, svms_status='Ordered'))
chk(A._dockproc_push_events(r2, 'Ordered', 1, None, '에버런스', 1.0, 'USD') == [],
    '이미 발주완료면 재발송 안 함')
ev = A._dockproc_push_events(r1, 'Ordered', 1, None, None, None, None)
chk(len(ev) == 1 and '업체 미상' in ev[0]['body'] and 'None' not in ev[0]['body'],
    '업체·금액 없어도 본문에 None 이 새지 않음', ev[0]['body'])

print('\n# 4) dock_reject — 라벨 정확일치, 진입 시 1회')
r3 = row_of(mkrow('TEST VESSEL', 'TSTV', 'S5', svms_status='Approval(Procssing)'))
_ev_rej = A._dockproc_push_events(r3, 'HQ Rejected', 0, None, None, None, None)
chk([e['kind'] for e in _ev_rej] == ['dock_reject'], '결재 반려 진입 = 알림')
k_rej = _ev_rej[0]['event_key'] if _ev_rej else None
r4 = row_of(mkrow('TEST VESSEL', 'TSTV', 'S6', svms_status='HQ Rejected'))
chk(A._dockproc_push_events(r4, 'HQ Rejected', 0, None, None, None, None) == [],
    '이미 반려면 재발송 안 함')
chk(A._dockproc_push_events(r3, 'HQ Rejected(2)', 0, None, None, None, None) == [],
    '🔴 부분일치 금지 — 라벨 정확일치 아니면 반려 아님')
# 🔴 올마이트 블로커: 같은 날 2차 반려. 키가 날짜였을 땐 두 번째가 통째로 묻혔다.
A.execute("UPDATE dock_procure SET svms_status='Submit', "
          "updated_at=datetime('now','localtime','+1 second') WHERE id=?", (r3['id'],))
ev2 = A._dockproc_push_events(row_of(r3['id']), 'HQ Rejected', 0, None, None, None, None)
chk(len(ev2) == 1 and ev2[0]['event_key'] != k_rej,
    '🔴 재상신→재반려(같은 날)는 새 키로 다시 나감 — 묻히지 않음', (k_rej, ev2 and ev2[0]['event_key']))

print('\n# 5) 여러 이벤트가 한 전이에 동시 발생하면 둘 다 나감')
r5 = row_of(mkrow('TEST VESSEL', 'TSTV', 'S7', svms_submit='0/2', svms_status='Quotation Inquiry'))
ev = A._dockproc_push_events(r5, 'Ordered', 1, '2/2', '딘텍', 100.0, 'USD')
chk(sorted(e['kind'] for e in ev) == ['dock_ordered', 'dock_quote'], '견적제출+발주완료 동시', ev)
chk(len({e['collapse'] for e in ev}) == 2, 'collapse_id 가 서로 달라 알림이 안 덮임')

print('\n# 6) 🔴 partial=true 는 미적재(orphan) 배너를 갈아치우지 않는다')
A.execute("INSERT OR REPLACE INTO api_settings (k,v) VALUES (?,?)",
          (A._DOCKPROC_ORPHAN_KEY, json.dumps({'TSTV': [{'vsl_cd': 'TSTV', 'req_no': 'S99',
                                                         'subject': '[TSTV S99] 미적재', 'status': 'HQ Confirmed'}]})))
before = A._dockproc_orphans_all().get('TSTV')
res = c.post('/api/ext/dock_procure/sync', headers=H,
             json={'partial': True, 'items': [
                 {'vsl_cd': 'TSTV', 'inq_no': 'X1', 'subject': '[TSTV S1] TEST ITEM',
                  'status': 'Quotation Inquiry', 'submit': '2/3', 'doc': 'PC'}]})
chk(res.status_code == 200 and res.get_json().get('partial') is True, 'partial 응답 플래그', res.status_code)
chk(A._dockproc_orphans_all().get('TSTV') == before, '배너 그대로 유지', A._dockproc_orphans_all().get('TSTV'))
res = c.post('/api/ext/dock_procure/sync', headers=H,
             json={'items': [{'vsl_cd': 'TSTV', 'inq_no': 'X2', 'subject': '[TSTV S1] TEST ITEM',
                              'status': 'Quotation Inquiry', 'submit': '3/3', 'doc': 'PC'}]})
chk(res.status_code == 200 and not A._dockproc_orphans_all().get('TSTV'),
    '전량 sync 는 종전대로 배너를 갱신(0건이면 사라짐)', A._dockproc_orphans_all().get('TSTV'))

print('\n# 7) 푸시 미설정이어도 sync 는 정상 완료 — 알림이 동기화를 막지 않는다')
res = c.post('/api/ext/dock_procure/sync', headers=H,
             json={'partial': True, 'items': [
                 {'vsl_cd': 'TSTV', 'inq_no': 'X3', 'subject': '[TSTV S3] TEST ITEM',
                  'status': 'Ordered', 'vendor': '에버런스', 'amt': 500, 'cur': 'USD',
                  'ordered_evidence': True, 'doc': 'PC'}]})
j = res.get_json()
chk(res.status_code == 200, 'sync 200', res.status_code)
chk(isinstance(j.get('pushed'), list), '응답에 push 결과 배열', j.get('pushed'))
chk(len(j['pushed']) == 1 and j['pushed'][0]['kind'] == 'dock_ordered', '발주완료 1건 보고', j['pushed'])
# 🔴 등록기기 0대는 **실패가 아니다**(`_push_dispatch` 계약: ok=True·sent=0·claim 유지 = 과거 이벤트
#    폭주 방지). 여기서 확인할 건 "sync 가 발송 결과를 숨기지 않고 그대로 실어준다"는 것이다.
chk(j['pushed'][0]['sent'] == 0, '등록기기 0대면 sent=0 으로 정직하게 보고', j['pushed'])
chk(row_of(A.query("SELECT id FROM dock_procure WHERE req_no='S3'", one=True)['id'])['stg_order'] == 1,
    '푸시 실패와 무관하게 단계는 반영됨')

print('\n# 8) dry 는 아무 것도 발송하지 않음')
res = c.post('/api/ext/dock_procure/sync', headers=H,
             json={'dry': True, 'items': [
                 {'vsl_cd': 'TSTV', 'inq_no': 'X4', 'subject': '[TSTV S5] TEST ITEM',
                  'status': 'HQ Rejected', 'doc': 'PC'}]})
chk(res.get_json().get('pushed') == [], 'dry → pushed 0건', res.get_json().get('pushed'))

print('\n# 9) 🔴 대기함(push_outbox) — 발송 실패가 영구 미탐이 되지 않는다')
# 이게 없으면: 행은 이미 갱신됐으므로 다음 폴은 "변화 없음" 으로 보고 그 알림은 영영 안 간다.
_real = A._push_dispatch
A._push_dispatch = lambda *a, **k: {'ok': False, 'reason': 'all_failed', 'sent': 0, 'failed': 1}
mkrow('TEST VESSEL', 'TSTV', 'S8', svms_status='Submit')     # 아직 반려 아님 = 전이가 생기는 행
res = c.post('/api/ext/dock_procure/sync', headers=H,
             json={'partial': True, 'items': [
                 {'vsl_cd': 'TSTV', 'inq_no': 'X5', 'subject': '[TSTV S8] TEST ITEM',
                  'status': 'HQ Rejected', 'doc': 'PC'}]})
ob = A.query("SELECT * FROM push_outbox")
chk(res.status_code == 200, '발송 실패해도 sync 는 200', res.status_code)
chk(len(ob) == 1 and ob[0]['kind'] == 'dock_reject' and ob[0]['tries'] == 1,
    '🔴 실패건이 대기함에 tries=1 로 남음', [(r['event_key'], r['tries']) for r in ob])
_pending_key = ob[0]['event_key']
chk(ob[0]['link'] == 'trmt://dock' and ob[0]['collapse_id'], 'link·collapse_id 도 함께 보존', dict(ob[0]))

# 다음 폴 = 새 변화가 없어도 대기함을 이어받아 재시도한다.
A._push_dispatch = _real
res = c.post('/api/ext/dock_procure/sync', headers=H, json={'partial': True, 'items': []})
j = res.get_json()
chk([p['key'] for p in j.get('pushed') or []] == [_pending_key],
    '🔴 변화 0건인 폴이 지난 실패건을 재발송', j.get('pushed'))
chk(A.query("SELECT * FROM push_outbox") == [], '성공하면 대기함에서 사라짐',
    A.query("SELECT event_key FROM push_outbox"))

# 무한재시도 금지 — 한도 넘으면 버리고 로그만 남긴다(형에게 늦은 알림은 오보에 가깝다).
A._push_dispatch = lambda *a, **k: {'ok': False, 'reason': 'all_failed', 'sent': 0, 'failed': 1}
A.execute("INSERT INTO push_outbox (event_key, kind, title, body, tries) VALUES "
          "('dock_quote:zombie', 'dock_quote', 't', 'b', ?)", (A._PUSH_OUTBOX_MAX_TRIES,))
out = A._push_outbox_drain()
A._push_dispatch = _real
chk([o['reason'] for o in out] == ['dropped'] and A.query("SELECT * FROM push_outbox") == [],
    '한도 초과건은 포기하고 대기함에서 제거', out)

print('\n# 10) 딥링크는 앱 allowlist 에 있는 값만 쓴다')
src = open('app.py').read()
chk("link='trmt://dock'" in src, "sync 푸시 링크 = trmt://dock")
ios = os.path.expanduser('~/.openclaw/workspace/trmt-mobile/ios/TRMT/Sources/Intents/TRMTAppIntents.swift')
if os.path.exists(ios):
    chk('"trmt://dock"' in open(ios).read(), '🔴 iOS IntentLinkInbox allowlist 에 등재됨(없으면 탭이 무반응)')
else:
    print('  --  iOS 소스 없음(웹 전용 환경) — 스킵')

print()
if fails:
    print(f'❌ 실패 {len(fails)}건: ' + ', '.join(fails)); sys.exit(1)
print('✅ 전부 통과')
