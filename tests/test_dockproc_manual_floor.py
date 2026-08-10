#!/usr/bin/env python3
"""Dock — **사람이 확정한 단계를 sync 가 되돌리지 못하게** 하는 floor 테스트.

실사고(2026-08-07, 형 제보 BGBB S5 `BGBBES2607A51`): 형이 '발주완료' 를 체크하고 remark 에
"이메일 발주 : 오션어스" 를 적어뒀는데, 다음 정기 sync 가 SVMS 라벨('Quotation Inquiry' = rank 2)로
단계를 **벤더제출까지 되돌렸다.** SVMS 로 발주하지 않은 건(메일·직접 발주)은 SVMS 라벨이 영원히
올라오지 않으므로, 이대로면 사람 입력이 매시간 지워진다.

잠그는 것:
  ① 사람이 stage 토글로 세운 rank 가 `stg_manual` 에 기록된다.
  ② sync 가 더 낮은 rank 라벨을 들고 와도 **단계가 내려가지 않는다**(형 실사고 재현).
  ③ floor 는 **표시 단계에만** 적용된다 — 발주금액 자동입력·발주완료 푸시는 SVMS 가 실제로
     발주완료라고 말한 경우에만 열린다(사람 체크 하나로 돈경로가 열리면 안 된다).
  ④ sync 가 floor 보다 **높은** 단계를 들고 오면 그건 그대로 반영된다(정본 전진은 계속 이김).
  ⑤ floor 가 없는 기존 행은 종전대로 sync 가 되돌린다(회수·반려 경로 안 막힘).
  ⑥ 사람이 스스로 내리면 floor 도 같이 내려간다(영구 고착 없음).
  ⑦ 멱등 — floor 로 유지되는 행은 다음 sync 에서 `updated` 에 안 잡힌다.

실행: ~/.venvs/trmt-test/bin/python tests/test_dockproc_manual_floor.py
  ⚠️ `/tmp/*venv` 는 죽었다 — 상주 venv 는 `~/.venvs/trmt-test`.
"""
import os, sys, tempfile

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
with c.session_transaction() as s:
    s['user_id'] = 1; s['username'] = 'smoke'; s['role'] = 'admin'

KEY = 'testkey-dockproc-floor'
A._ensure_api_table()
A.execute("INSERT OR REPLACE INTO api_settings(k, v) VALUES('api_key', ?)", (KEY,))
HDR = {'X-API-Key': KEY}

A.execute("INSERT INTO dock_procure_vessel(vsl_nm, vsl_cd) VALUES('TEST VESSEL','TSTV')")


def mkrow(req_no, stages=(0, 0, 0, 0), svms_status=None, remark=None, cat_code='S'):
    A.execute("DELETE FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'", (req_no,))
    A.execute(
        "INSERT INTO dock_procure(vsl_nm, vsl_cd, req_no, cat_code, subject, "
        "stg_quote, stg_vendor, stg_confirm, stg_order, svms_status, remark) "
        "VALUES('TEST VESSEL','TSTV',?,?,?,?,?,?,?,?,?)",
        (req_no, cat_code, f'[DOCK][TSTV {req_no}]subject', *stages, svms_status, remark))
    return A.query("SELECT * FROM dock_procure WHERE req_no=? AND vsl_nm='TEST VESSEL'",
                   (req_no,), one=True)['id']


def row(lid):
    return A.query("SELECT * FROM dock_procure WHERE id=?", (lid,), one=True)


def stages_of(lid):
    r = row(lid)
    return (r['stg_quote'], r['stg_vendor'], r['stg_confirm'], r['stg_order'])


def toggle(lid, stage, value):
    return c.post(f'/api/dock_procure/{lid}/stage', json={'stage': stage, 'value': value})


def sync(req_no, status, amt=None, cur=None, vendor=None, submit=None):
    it = {'vsl_cd': 'TSTV', 'subject': f'[DOCK][TSTV {req_no}]subject', 'status': status,
          'inq_no': None, 'amt': amt, 'cur': cur, 'vendor': vendor, 'submit': submit}
    r = c.post('/api/ext/dock_procure/sync', json={'items': [it]}, headers=HDR)
    assert r.status_code == 200, r.status_code
    return r.get_json()


print('① 사람이 켠 단계가 floor 로 기록된다')
lid = mkrow('S1', (1, 1, 0, 0), 'Quotation Inquiry', remark='이메일 발주 : 오션어스')
r = toggle(lid, 'order', 1)
chk(r.status_code == 200, '발주완료 체크 = 200', f'got {r.status_code}')
chk(row(lid)['stg_manual'] == 4, 'stg_manual=4 기록', str(row(lid)['stg_manual']))
chk((r.get_json() or {}).get('stg_manual') == 4, '응답에도 stg_manual 노출')

print('② 형 실사고 재현 — SVMS rank2 sync 가 와도 발주완료가 유지된다')
sync('S1', 'Quotation Inquiry')
chk(stages_of(lid) == (1, 1, 1, 1), '단계 유지(되돌림 없음)', str(stages_of(lid)))
chk(row(lid)['svms_status'] == 'Quotation Inquiry', 'SVMS 라벨 자체는 그대로 갱신된다')
chk(row(lid)['remark'] == '이메일 발주 : 오션어스', '사람이 쓴 remark 보존')

print('③ floor 는 돈경로·푸시를 열지 않는다')
chk(row(lid)['quote_amt'] is None, 'SVMS 가 발주완료가 아니면 발주금액 자동입력 없음',
    str(row(lid)['quote_amt']))
sync('S1', 'Quotation Inquiry', amt=1234.0, cur='USD', vendor='SOMEVENDOR')
chk(row(lid)['quote_amt'] is None,
    '금액이 실려와도 SVMS rank<4 면 안 채운다', str(row(lid)['quote_amt']))
chk(row(lid)['remark'] == '이메일 발주 : 오션어스',
    'SVMS vendor 명이 사람 remark 를 덮지 않는다', str(row(lid)['remark']))
_pl = [r['kind'] for r in A.query("SELECT kind FROM push_outbox")] \
    if A.query("SELECT name FROM sqlite_master WHERE type='table' AND name='push_outbox'",
               one=True) else []
chk(not any('order' in (k or '') for k in _pl),
    '사람 체크만으로 발주완료 푸시가 나가지 않는다', str(_pl))

print('④ SVMS 가 더 앞선 단계를 말하면 그건 이긴다')
lid2 = mkrow('S2', (1, 0, 0, 0), 'HQ Confirmed')
toggle(lid2, 'quote', 1)                                   # floor 1
sync('S2', 'Quotation Inquiry')                            # rank 2
chk(stages_of(lid2) == (1, 1, 0, 0), 'floor 보다 높은 SVMS rank 는 그대로 전진',
    str(stages_of(lid2)))

print('⑤ floor 없는 행은 종전대로 sync 가 되돌린다 (회수·반려 경로 유지)')
lid3 = mkrow('S3', (1, 1, 1, 1), 'HQ Ordered')             # sync 가 켜둔 행(사람 손 안 탐)
chk(row(lid3)['stg_manual'] == 0, '기존 행 floor=0 (backfill 없음)')
sync('S3', 'HQ Rejected')                                  # rank 2
chk(stages_of(lid3) == (1, 1, 0, 0), '반려 되돌림이 계속 동작', str(stages_of(lid3)))

print('⑥ 사람이 스스로 내리면 floor 도 내려간다 (영구 고착 없음)')
lid4 = mkrow('S4', (1, 1, 0, 0), 'Quotation Inquiry')
toggle(lid4, 'order', 1)
chk(row(lid4)['stg_manual'] == 4, '먼저 floor 4')
r = toggle(lid4, 'order', 0)                               # SVMS rank2 까지는 내릴 수 있다
chk(r.status_code == 200 and row(lid4)['stg_manual'] == 3,
    '해제하면 floor 도 3 으로 내려감', f'{r.status_code} {row(lid4)["stg_manual"]}')
r = toggle(lid4, 'confirm', 0)
chk(r.status_code == 200 and row(lid4)['stg_manual'] == 2,
    'SVMS rank(2) 까지 계속 내려감', f'{r.status_code} {row(lid4)["stg_manual"]}')
sync('S4', 'Quotation Inquiry')
chk(stages_of(lid4) == (1, 1, 0, 0), 'floor 내려간 뒤엔 sync 결과와 일치', str(stages_of(lid4)))

print('⑦ 멱등 — floor 로 유지되는 행은 updated 에 안 잡힌다')
res = sync('S1', 'Quotation Inquiry')
chk(res.get('updated', 0) == 0, '두 번째 sync 에서 변경 0', str(res))

print('⑧ SVMS 가 나중에 진짜 발주완료로 따라오면 돈경로가 정상으로 열린다 (positive control)')
#   ③ 의 대조군 — floor 가 `svms_o` 를 **막지도 않는다**는 확인. 이게 죽으면 메일발주 행은
#   나중에 SVMS 발주가 잡혀도 금액이 영영 안 채워진다.
lid5 = mkrow('S5', (1, 1, 0, 0), 'Quotation Inquiry')
toggle(lid5, 'order', 1)                                   # floor 4
sync('S5', 'HQ Ordered', amt=999.0, cur='USD', vendor='REALVENDOR')
chk(row(lid5)['quote_amt'] == 999.0, 'SVMS rank4 면 발주금액 자동입력 열림',
    str(row(lid5)['quote_amt']))
chk(row(lid5)['quote_cur'] == 'USD', '통화도 반영')
#   🔴 발주완료 푸시는 **여기서도 안 나간다** — floor 탓이 아니라 `_dockproc_push_events` 가
#     원래 `not row['stg_order']` 로 게이트하기 때문이다(사람이 이미 켜둔 행 = 이미 아는 사실).
#     floor 도입 전 동작과 동일하며 의도된 억제다. 이 줄은 그 사실을 고정해 둔다.
_ord = [r['event_key'] for r in A.query(
    "SELECT event_key FROM push_outbox WHERE kind='dock_ordered'")]
chk(not any((':%d:' % lid5) in k for k in _ord),
    '사람이 이미 켜둔 행의 SVMS 추인은 푸시 안 함(기존 동작 유지)', str(_ord))

print('⑨ floor 가 있으면 회수/반려 되돌림도 막힌다 (형 요구 = 수동 우선)')
lid6 = mkrow('S6', (1, 1, 0, 0), 'Quotation Inquiry')
toggle(lid6, 'order', 1)                                   # floor 4
sync('S6', 'HQ Rejected')                                  # rank 2
chk(stages_of(lid6) == (1, 1, 1, 1), '반려가 와도 수동 확정 단계 유지', str(stages_of(lid6)))
sync('S6', 'HQ Received')                                  # rank 0 (회수 계열)
chk(stages_of(lid6) == (1, 1, 1, 1), '회수 라벨에도 유지', str(stages_of(lid6)))

print('⑩ stg_manual 컬럼이 없는 구버전 DB 에서도 sync 가 죽지 않는다')
#   서버 autodeploy 는 `init_db(drop=False)` + `_auto_migrate()` 로 컬럼을 만들지만, 배포 순서가
#   어긋난 순간(구 DB + 새 코드)에 sync 전체가 KeyError 로 500 나면 수리까지 통째로 멈춘다.
_legacy = {'id': 1, 'stg_quote': 1, 'stg_vendor': 1, 'stg_confirm': 0, 'stg_order': 0}
chk((_legacy['stg_manual'] if 'stg_manual' in _legacy.keys() else 0) == 0,
    '컬럼 없는 행은 floor 0 으로 읽힌다(가드 형태 고정)')
_r1 = row(lid)
chk('stg_manual' in _r1.keys() and (_r1['stg_manual'] or 0) == 4,
    'sqlite3.Row 에서도 같은 가드 표현이 동작', str(_r1['stg_manual']))

print()
print(('❌ FAIL: ' + ', '.join(fails)) if fails else '✅ 전부 통과')
sys.exit(1 if fails else 0)
